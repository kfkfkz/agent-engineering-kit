#!/usr/bin/env python3
"""Code review evidence must use Memory and the code graph before text search."""
from __future__ import annotations

import unittest

from aek.application.identity import bind_receipt
from aek.application.review_evidence import ReviewEvidenceReuse, plan_code_review_evidence
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
from aek.core.context.identity import build_identity


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

    def test_trusted_current_handoff_omits_duplicate_lookups(self) -> None:
        def identity(*, query="a", generation="a", deps="a"):
            return build_identity({
                "subject": {"worktree": "a" * 64, "dependencies": deps * 64},
                "artifact": {"diff": "a" * 64},
                "policy": {"rules": "a" * 64},
                "context": {"query": query * 64, "generation": generation * 64},
                "evidence": {"provenance": "a" * 64},
            })

        refs = {"memory": "host:memory", "graph": "host:graph"}
        receipts = {
            ref: bind_receipt(identity(), {"kind": kind, "ref": ref, "complete": True})
            for kind, ref in refs.items()
        }

        def plan(memory_identity, graph_identity, lookup=receipts.get):
            return plan_code_review_evidence(
                memory=plan_knowledge_lookup(self.memory_capabilities()),
                codebase=self.fresh_graph(), include_textual_checks=False,
                reuse=ReviewEvidenceReuse(
                    {"memory": memory_identity, "graph": graph_identity}, refs, lookup))

        current = plan(identity(), identity())
        self.assertEqual(current.reused_refs, tuple(refs.values()))
        self.assertEqual(current.allowed_tools, ("targeted_source_read",))
        stale_graph = plan(identity(), identity(generation="b"))
        self.assertEqual(stale_graph.reused_refs, ("host:memory",))
        self.assertIn("search_graph", stale_graph.allowed_tools)
        stale_memory = plan(identity(query="b"), identity())
        self.assertEqual(stale_memory.reused_refs, ("host:graph",))
        self.assertIn("memory_recall", stale_memory.allowed_tools)
        untrusted = plan(identity(), identity(), lookup=lambda ref: None)
        self.assertEqual(untrusted.reused_refs, ())
        self.assertIn("memory_recall", untrusted.allowed_tools)
        self.assertIn("search_graph", untrusted.allowed_tools)


if __name__ == "__main__":
    unittest.main()
