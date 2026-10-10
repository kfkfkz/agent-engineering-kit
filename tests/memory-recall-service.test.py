#!/usr/bin/env python3
"""Route budgets control real Memory candidate/open deliveries."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aek.adapters.capsule import CapsuleStore
from aek.adapters.memory import fallback_candidates, read_candidate
from aek.adapters.telemetry import ContextLedger
from aek.application.memory_recall import recall_context
from aek.core.context.budget import resolve_context_budget
from aek.core.context.lookup import (
    CapabilitySnapshot,
    plan_knowledge_lookup,
)


class MemoryRecallServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        memory = self.repo / "docs/memory/pitfalls"
        memory.mkdir(parents=True)
        for index in range(8):
            (memory / f"item-{index}.md").write_text(
                f"# database item {index}\n\nbody {index}\n", encoding="utf-8")

    def recall(self, route: str):
        budget = resolve_context_budget(route, "routing", "quick")
        ledger = ContextLedger(self.repo / f"{route}.jsonl")
        return recall_context(
            self.repo, "database", budget, ledger,
            session_id=f"session-{route}", subject_digest="a" * 64,
            risk_required=False,
            candidate_search=lambda repo, query: fallback_candidates(
                repo, query, limit=budget.memory_candidate_limit),
            candidate_read=read_candidate,
            capsule_store=CapsuleStore(
                self.repo / ".repo-memory-kit/context/capsules"),
            lookup_plan=plan_knowledge_lookup(CapabilitySnapshot(
                True, True, True, True, True, ("test:tools/list",))))

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

    def test_standard_builds_and_reuses_source_bound_capsule(self) -> None:
        first = self.recall("standard")
        second = self.recall("standard")
        self.assertIsNotNone(first.capsule)
        self.assertFalse(first.capsule_reused)
        self.assertTrue(second.capsule_reused)
        self.assertIn("capsule_loader", second.report.observed_channels)
        self.assertEqual(second.tool_calls["capsule"], 1)

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
