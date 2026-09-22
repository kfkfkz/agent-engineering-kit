#!/usr/bin/env python3
"""Route budgets control real Memory candidate/open deliveries."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aek.adapters.memory import fallback_candidates, read_candidate
from aek.adapters.capsule import CapsuleStore
from aek.adapters.telemetry import ContextLedger
from aek.application.memory_recall import recall_context
from aek.core.context.budget import resolve_context_budget


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
                self.repo / ".repo-memory-kit/context/capsules"))

    def test_direct_delivers_no_memory_body(self) -> None:
        result = self.recall("direct")
        self.assertEqual(result.candidates.candidates, ())
        self.assertEqual(result.expanded, ())
        self.assertEqual(result.report.observed_channels, ("memory_candidate",))

    def test_bounded_caps_candidates_and_opened_bodies(self) -> None:
        result = self.recall("bounded")
        self.assertEqual(len(result.candidates.candidates), 5)
        self.assertEqual(len(result.expanded), 2)
        self.assertEqual(result.report.coverage, "partial")
        self.assertIn("host_native", result.report.unobserved_channels)

    def test_standard_builds_and_reuses_source_bound_capsule(self) -> None:
        first = self.recall("standard")
        second = self.recall("standard")
        self.assertIsNotNone(first.capsule)
        self.assertFalse(first.capsule_reused)
        self.assertTrue(second.capsule_reused)
        self.assertIn("capsule_loader", second.report.observed_channels)


if __name__ == "__main__":
    unittest.main()
