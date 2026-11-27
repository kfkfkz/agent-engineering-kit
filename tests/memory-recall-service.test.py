#!/usr/bin/env python3
"""Route budgets control real Memory candidate/open deliveries."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aek.adapters.capsule import CapsuleStore
from aek.adapters.memory import StaleCandidate, fallback_candidates, read_candidate
from aek.adapters.telemetry import ContextLedger
from aek.application.memory_recall import recall_context
from aek.core.context.budget import resolve_context_budget
from aek.core.context.lookup import (
    CapabilitySnapshot,
    plan_knowledge_lookup,
)


class MemoryRecallServiceTests(unittest.TestCase):
    def test_same_task_can_resume_across_stages_without_rewriting_history(self) -> None:
        ledger = ContextLedger(self.repo / "resumed.jsonl")
        first_events = None
        for stage in ("design", "execution", "design"):
            budget = resolve_context_budget("standard", stage, "thorough")
            result = recall_context(self.repo, "database", budget, ledger,
                session_id="stable-task", subject_digest="e" * 64,
                risk_required=False,
                candidate_search=lambda root, query: fallback_candidates(
                    root, query, limit=budget.memory_candidate_limit),
                candidate_read=read_candidate,
                capsule_store=CapsuleStore(self.repo / "capsules"))
            events = ledger.read()
            if first_events is None:
                first_events = events
            self.assertEqual(events[:len(first_events)], first_events)
            self.assertEqual(result.report.delivered_bytes,
                             sum(row.delivered_bytes for row in events if row.stage == stage))
            self.assertEqual(result.report.stream_digest, events[-1].event_hash)
            self.assertEqual(result.report.coverage, "partial")
            self.assertIn("host_native", result.report.unobserved_channels)
        self.assertEqual(tuple(row.sequence for row in events), tuple(range(1, len(events) + 1)))
        self.assertEqual(ledger.total_bytes(), sum(row.delivered_bytes for row in events))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        memory = self.repo / "docs/memory/pitfalls"
        memory.mkdir(parents=True)
        for index in range(8):
            (memory / f"item-{index}.md").write_text(
                f"# database item {index}\n\nbody {index}\n", encoding="utf-8")

    def recall(self, route: str, *, known_digests=(), stage="routing",
               stream=None, candidate_read=read_candidate, expand_paths=None,
               risk_required=False):
        budget = resolve_context_budget(route, stage, "quick")
        stream = stream or route
        ledger = ContextLedger(self.repo / f"{stream}.jsonl")
        return recall_context(
            self.repo, "database", budget, ledger,
            session_id=f"session-{stream}", subject_digest="a" * 64,
            risk_required=risk_required,
            candidate_search=lambda repo, query: fallback_candidates(
                repo, query, limit=budget.memory_candidate_limit),
            candidate_read=candidate_read,
            capsule_store=CapsuleStore(
                self.repo / ".repo-memory-kit/context/capsules"),
            lookup_plan=plan_knowledge_lookup(CapabilitySnapshot(
                True, True, True, True, True, ("test:tools/list",))),
            known_digests=known_digests, expand_paths=expand_paths)

    def test_summary_returns_references_without_opening_bodies_or_capsule(self):
        def unexpected_read(*args):
            self.fail("candidate-only recall opened a body")

        report = self.recall("standard", candidate_read=unexpected_read,
                             expand_paths=(), risk_required=True)
        self.assertTrue(report.candidates.candidates)
        self.assertEqual(report.reason, "CANDIDATES_ONLY")
        self.assertEqual(report.expanded, ())
        self.assertIsNone(report.capsule)
        self.assertEqual(report.tool_calls["memory_open"], 0)
        self.assertEqual(report.report.observed_channels, ("memory_candidate",))
        self.assertTrue(report.needs_more_candidates)

    def test_expansion_opens_only_the_selected_current_candidate(self):
        opened = []

        def record_read(repo, ref, generation):
            opened.append(ref.path)
            return read_candidate(repo, ref, generation)

        summary = self.recall("bounded", expand_paths=())
        chosen = summary.candidates.candidates[2]
        result = self.recall("bounded", expand_paths=(chosen.path,), candidate_read=record_read)
        self.assertEqual(opened, [chosen.path])
        self.assertEqual([ref.content_digest for ref in result.expanded], [chosen.content_digest])
        missing = self.recall("bounded", expand_paths=("docs/unmatched.md",),
                              candidate_read=record_read, risk_required=True)
        self.assertEqual(opened, [chosen.path])
        self.assertEqual(missing.reason, "REQUESTED_CANDIDATE_NOT_FOUND")
        self.assertTrue(missing.needs_more_candidates)
        self.assertEqual(missing.uncertainty, "truncated_unknown")

    def test_direct_delivers_no_memory_body(self) -> None:
        result = self.recall("direct")
        self.assertEqual(result.candidates.candidates, ())
        self.assertEqual(result.expanded, ())
        self.assertEqual(result.report.observed_channels, ("memory_candidate",))
        self.assertEqual(result.selected_path, "mcp")
        self.assertEqual(result.reason_code, "MCP_AVAILABLE")
        self.assertEqual(result.tool_calls["candidate_search"], 1)

    def test_bounded_caps_candidates_and_opened_bodies(self) -> None:
        result = self.recall("bounded")
        self.assertEqual(len(result.candidates.candidates), 5)
        self.assertEqual(len(result.expanded), 2)
        self.assertEqual(result.report.coverage, "partial")
        self.assertIn("host_native", result.report.unobserved_channels)
        self.assertGreater(result.delivered_bytes, 0)
        self.assertEqual(result.tool_calls["memory_open"], 2)

    def test_bounded_counts_utf8_bodies_across_calls_and_resets_by_stage(self) -> None:
        memory = self.repo / "docs/memory/pitfalls"
        for index, chars in ((0, 20000), (1, 11000)):
            (memory / f"item-{index}.md").write_text(
                "# database\n" + "界" * chars, encoding="utf-8")
        first = self.recall("bounded")
        self.assertEqual(first.reason, "MEMORY_BYTE_LIMIT")
        self.assertEqual(first.uncertainty, "truncated_unknown")
        self.assertEqual([Path(item.path).name for item in first.expanded], ["item-1.md"])
        self.assertEqual(Path(first.candidates.candidates[0].path).name, "item-0.md")
        self.assertFalse(self.recall("bounded").expanded)
        reused = self.recall("bounded", known_digests=(first.expanded[0].content_digest,))
        self.assertEqual([item.path for item in reused.reused], [first.expanded[0].path])
        ledger = ContextLedger(self.repo / "bounded.jsonl")
        self.assertEqual(sum(event.delivered_bytes for event in ledger.read()
                             if event.channel == "memory_open"),
                         len(first.expanded[0].content.encode("utf-8")))
        self.assertEqual(len(self.recall("bounded", stage="execution").expanded), 1)

    def test_rejected_route_or_stale_source_preserves_the_ledger(self) -> None:
        first = self.recall("standard", stream="shared")
        path = self.repo / "shared.jsonl"
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "route transition"):
            self.recall("bounded", stream="shared", stage="design")
        self.assertEqual(path.read_bytes(), before)

        def stale(*_args):
            raise StaleCandidate("source changed")

        with self.assertRaises(StaleCandidate):
            self.recall("standard", stream="shared", candidate_read=stale)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.recall("standard", stream="shared",
            known_digests=tuple(item.content_digest for item in first.expanded)).reused)

    def test_structured_sdd_recall_agrees_with_corpus_on_fallback_and_zvec(self):
        cli = Path(__file__).resolve().parents[1] / "memory-recall"
        rel = "docs/03-SDD/014-依赖迁移/需求分析.md"
        path = self.repo / rel
        path.parent.mkdir(parents=True)
        path.write_text("# dependency migration\n\nApproved SDD material.\n", encoding="utf-8")
        for source in ("fallback", "zvec"):
            with self.subTest(source=source):
                if source == "zvec":
                    if importlib.util.find_spec("zvec") is None:
                        break
                    subprocess.run([sys.executable, "-X", "utf8", str(cli), "--rebuild",
                                    str(self.repo)], check=True, capture_output=True, timeout=30)
                command = [sys.executable, "-X", "utf8", str(cli), "dependency",
                           str(self.repo), "--context-json", "--route", "standard",
                           "--session", f"sdd-{source}", "--subject", "f" * 64,
                           "--type", "sdd"]
                result = json.loads(subprocess.check_output(command, timeout=20))
                self.assertEqual(result["candidates"]["source"], source)
                self.assertEqual([item["path"] for item in result["expanded"]], [rel])
                self.assertIn("Approved SDD material.", result["expanded"][0]["content"])

    def test_standard_builds_and_reuses_source_bound_capsule(self) -> None:
        first = self.recall("standard")
        second = self.recall("standard")
        self.assertIsNotNone(first.capsule)
        self.assertFalse(first.capsule_reused)
        self.assertTrue(second.capsule_reused)
        self.assertIn("capsule_loader", second.report.observed_channels)
        self.assertEqual(second.tool_calls["capsule"], 1)

    def test_known_current_bodies_are_not_resent_and_changed_sources_are_delivered(self):
        first = self.recall("standard")
        known = tuple(item.content_digest for item in first.expanded)
        second = self.recall("standard", known_digests=known)
        self.assertEqual(second.expanded, ())
        self.assertEqual([(item.path, item.content_digest) for item in second.reused],
                         [(item.path, item.content_digest) for item in first.expanded])
        self.assertEqual(second.tool_calls["memory_open"], first.tool_calls["memory_open"])
        self.assertLess(second.delivered_bytes - first.delivered_bytes, first.delivered_bytes)
        (self.repo / first.expanded[0].path).write_text(
            "# database item 0\n\nnew body\n", encoding="utf-8")
        changed = self.recall("standard", known_digests=known)
        self.assertEqual([item.path for item in changed.expanded], [first.expanded[0].path])
        self.assertEqual(len(changed.reused), len(known) - 1)
        self.assertFalse(self.recall("standard", stage="design", known_digests=known).reused)

    def test_known_digest_without_scoped_delivery_cannot_suppress_body(self):
        path = self.repo / "docs/memory/pitfalls/item-0.md"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.recall("standard", known_digests=(digest,))
        self.assertTrue(result.expanded)
        self.assertFalse(result.reused)

    def test_explicit_cli_applies_top_type_status_and_module_before_expansion(self):
        playbooks = self.repo / "docs/memory/playbooks"
        playbooks.mkdir()
        for name, status, module in (("a-draft", "draft", "storage"),
                                     ("b-other-module", "confirmed", "web"),
                                     ("c-match", "confirmed", "storage"),
                                     ("d-match", "confirmed", "storage")):
            (playbooks / f"{name}.md").write_text(
                f"---\nstatus: {status}\nmodule: {module}\n---\n# database {name}\n\nbody\n",
                encoding="utf-8")
        cli = Path(__file__).resolve().parents[1] / "memory-recall"
        command = [sys.executable, "-X", "utf8", str(cli), "database", str(self.repo),
                   "--context-json", "--route", "standard", "--session", "filter-test",
                   "--subject", "f" * 64, "--type", "playbook", "--status", "confirmed",
                   "--module", "stor"]
        result = json.loads(subprocess.check_output(command + ["--top", "1"], timeout=20))
        expected = "docs/memory/playbooks/c-match.md"
        self.assertEqual([item["path"] for item in result["candidates"]["candidates"]], [expected])
        self.assertEqual([item["path"] for item in result["expanded"]], [expected])
        self.assertTrue(result["candidates"]["has_more"])
        empty = json.loads(subprocess.check_output(command + ["--top", "0"], timeout=20))
        self.assertEqual(empty["candidates"]["candidates"], [])
        self.assertEqual(empty["expanded"], [])

    def test_no_match_is_explicitly_unknown_not_negative(self) -> None:
        budget = resolve_context_budget("bounded", "routing", "quick")
        result = recall_context(
            self.repo, "does-not-exist", budget,
            ContextLedger(self.repo / "missing.jsonl"),
            session_id="session-missing", subject_digest="b" * 64,
            risk_required=False,
            candidate_search=lambda repo, query: fallback_candidates(
                repo, query, limit=budget.memory_candidate_limit),
            candidate_read=read_candidate,
            capsule_store=CapsuleStore(
                self.repo / ".repo-memory-kit/context/capsules"))
        self.assertEqual(result.uncertainty, "no_match_unknown")

    def test_review_purpose_is_preserved_in_context_ledger(self) -> None:
        budget = resolve_context_budget("bounded", "closeout", "quick")
        ledger = ContextLedger(self.repo / "review.jsonl")
        recall_context(
            self.repo, "database", budget, ledger,
            session_id="session-review", subject_digest="c" * 64,
            risk_required=False, purpose="review_required",
            candidate_search=lambda repo, query: fallback_candidates(
                repo, query, limit=budget.memory_candidate_limit),
            candidate_read=read_candidate,
            capsule_store=CapsuleStore(
                self.repo / ".repo-memory-kit/context/capsules"))
        events = ledger.read()
        self.assertTrue(events)
        self.assertEqual({event.purpose for event in events},
                         {"review_required"})

    def test_freeform_purpose_is_rejected_before_search_or_delivery(self) -> None:
        calls = []
        ledger = ContextLedger(self.repo / "invalid-purpose.jsonl")

        def search(_repo, _query):
            calls.append("search")
            raise AssertionError("invalid input reached retrieval")

        with self.assertRaisesRegex(ValueError, "purpose"):
            recall_context(self.repo, "dependency migration", resolve_context_budget("standard", "design", "thorough"),
                           ledger, session_id="purpose-test", subject_digest="d" * 64,
                           risk_required=False, purpose="依赖迁移的影响分析与历史约束核查",
                           candidate_search=search, candidate_read=read_candidate,
                           capsule_store=CapsuleStore(self.repo / "capsules"))
        self.assertEqual(calls, [])
        self.assertEqual(ledger.read(), ())

    def test_invalid_identity_is_rejected_before_accessing_an_external_ledger(self) -> None:
        outside = self.repo / "outside.jsonl"
        original = b'{"private":"owned-canary"}'
        outside.write_bytes(original)
        for session, subject in (("../../../outside", "d" * 64), ("safe-session", "../outside")):
            with self.subTest(session=session), self.assertRaises(ValueError):
                recall_context(self.repo, "database", resolve_context_budget("direct", "routing", "quick"),
                               ContextLedger(outside), session_id=session, subject_digest=subject,
                               risk_required=False, candidate_search=lambda *_: self.fail("invalid input reached lookup"),
                               candidate_read=read_candidate)
            self.assertEqual(outside.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
