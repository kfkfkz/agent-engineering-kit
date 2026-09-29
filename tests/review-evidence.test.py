#!/usr/bin/env python3
"""Code review evidence must use Memory and the code graph before text search."""
from __future__ import annotations

import unittest

from aek.application.review_evidence import plan_code_review_evidence
from aek.core.codebase_context import (
    CapabilityState,
    DirtyState,
    QueryKind,
    plan_barrier,
)
from aek.core.context.lookup import (
    CapabilitySnapshot,
    LookupAttempt,
    plan_knowledge_lookup,
)


class ReviewEvidencePlanTests(unittest.TestCase):
    def memory_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            mcp_configured=True, mcp_visible=True, mcp_compatible=True,
            cli_available=True, bounded_text_available=True,
            evidence=("host:tools/list",))

    def fresh_graph(self):
        return plan_barrier(
            QueryKind.STRUCTURAL,
            CapabilityState(True, True, True, True),
            DirtyState.clean("identity-a"), candidate_paths=())

    def test_review_orders_memory_then_graph_then_source(self) -> None:
        plan = plan_code_review_evidence(
            memory=plan_knowledge_lookup(self.memory_capabilities()),
            codebase=self.fresh_graph(), include_textual_checks=False)

        self.assertEqual(
            tuple(step.channel for step in plan.steps),
            ("memory_recall_mcp", "codebase_graph", "targeted_source"))
        self.assertEqual(plan.steps[1].required_tools[0], "search_graph")
        self.assertIn("check_index_coverage", plan.steps[1].required_tools)
        self.assertNotIn("rg", plan.allowed_tools)
        self.assertTrue(plan.ready_for_review)
        self.assertEqual(plan.confidence, "high")

    def test_text_search_is_bounded_and_runs_after_structural_evidence(self) -> None:
        plan = plan_code_review_evidence(
            memory=plan_knowledge_lookup(self.memory_capabilities()),
            codebase=self.fresh_graph(), include_textual_checks=True)

        self.assertEqual(plan.steps[-1].channel, "bounded_text")
        self.assertEqual(plan.steps[-1].required_tools, ("rg",))
        self.assertGreater(
            tuple(step.channel for step in plan.steps).index("bounded_text"),
            tuple(step.channel for step in plan.steps).index("codebase_graph"))

    def test_unresolved_freshness_barrier_stops_review_before_grep(self) -> None:
        barrier = plan_barrier(
            QueryKind.STRUCTURAL,
            CapabilityState(True, True, True, False),
            DirtyState.clean("identity-a"),
            candidate_paths=("aek/application/review.py",))
        plan = plan_code_review_evidence(
            memory=plan_knowledge_lookup(self.memory_capabilities()),
            codebase=barrier, include_textual_checks=True)

        self.assertFalse(plan.ready_for_review)
        self.assertEqual(plan.steps[-1].channel, "codebase_freshness_barrier")
        self.assertNotIn("rg", plan.allowed_tools)
        self.assertIn("FRESHNESS_PROOF_REQUIRED", plan.blocking_reasons)

    def test_started_memory_failure_does_not_fall_through_to_cli_or_grep(self) -> None:
        memory = plan_knowledge_lookup(
            self.memory_capabilities(),
            attempts=(LookupAttempt("mcp", "after_start", "TIMEOUT"),))
        plan = plan_code_review_evidence(
            memory=memory, codebase=self.fresh_graph(),
            include_textual_checks=True)

        self.assertFalse(plan.ready_for_review)
        self.assertEqual(tuple(step.channel for step in plan.steps), ())
        self.assertIn("MCP_OUTCOME_UNKNOWN", plan.blocking_reasons)
        self.assertNotIn("memory_recall_cli", plan.allowed_tools)
        self.assertNotIn("rg", plan.allowed_tools)

    def test_missing_graph_is_explicitly_degraded_not_complete(self) -> None:
        fallback = plan_barrier(
            QueryKind.STRUCTURAL,
            CapabilityState(True, False, False, False),
            DirtyState.clean("identity-a"),
            candidate_paths=("aek/application/review.py",))
        plan = plan_code_review_evidence(
            memory=plan_knowledge_lookup(self.memory_capabilities()),
            codebase=fallback, include_textual_checks=False)

        self.assertTrue(plan.ready_for_review)
        self.assertEqual(plan.confidence, "degraded")
        self.assertIn("MCP_NOT_VISIBLE", plan.limitations)
        self.assertEqual(plan.steps[1].channel, "bounded_source")
        self.assertFalse(plan.may_claim_complete_structure)


if __name__ == "__main__":
    unittest.main()
