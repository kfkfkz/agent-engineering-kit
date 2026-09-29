#!/usr/bin/env python3
"""Codebase-first query classification and one-refresh-per-batch barrier."""
from __future__ import annotations

import unittest

from aek.core.codebase_context import (
    CapabilityState,
    CodeIdentity,
    CodebaseObservation,
    DirtyState,
    QueryKind,
    classify_query,
    build_code_identity,
    mark_dirty,
    plan_barrier,
    verify_observation,
)


class CodebaseContextTests(unittest.TestCase):
    def test_code_identity_binds_revision_dirty_and_path_set(self) -> None:
        first = build_code_identity(
            repo_id="repo-a", worktree_id="wt-a", vcs_kind="git",
            revision="abc123", tracked_diff_digest="a" * 64,
            untracked_code_digest="b" * 64, path_set_digest="c" * 64)
        second = build_code_identity(
            repo_id="repo-a", worktree_id="wt-a", vcs_kind="git",
            revision="abc123", tracked_diff_digest="a" * 64,
            untracked_code_digest="d" * 64, path_set_digest="c" * 64)
        self.assertIsInstance(first, CodeIdentity)
        self.assertNotEqual(first.identity_digest, second.identity_digest)

    def test_structural_and_textual_queries_are_distinct(self) -> None:
        self.assertEqual(classify_query("who calls OrderService"),
                         QueryKind.STRUCTURAL)
        self.assertEqual(classify_query("find exact error text in yaml"),
                         QueryKind.TEXTUAL)

    def test_continuous_code_writes_share_one_dirty_epoch(self) -> None:
        state = DirtyState.clean("identity-a")
        first = mark_dirty(state, batch_id="batch-1", identity_digest="identity-b",
                           paths=("aek/a.py",), reason="agent_write")
        second = mark_dirty(first, batch_id="batch-1", identity_digest="identity-c",
                            paths=("aek/b.py",), reason="generator_write")
        docs = mark_dirty(second, batch_id="batch-2", identity_digest="identity-d",
                          paths=("docs/readme.md",), reason="docs_write")
        self.assertEqual(second.dirty_epoch, 1)
        self.assertEqual(second.changed_paths, ("aek/a.py", "aek/b.py"))
        self.assertEqual(docs, second)

    def test_path_classification_is_pure_and_preserves_dotfiles(self) -> None:
        state = mark_dirty(
            DirtyState.clean("identity-a"), batch_id="batch-1",
            identity_digest="identity-b",
            paths=(".gitignore", "config/settings.json", "src/app.py"),
            reason="git_update")
        self.assertEqual(state.changed_paths, (".gitignore", "src/app.py"))

    def test_configured_does_not_imply_visible_or_indexed(self) -> None:
        plan = plan_barrier(
            QueryKind.STRUCTURAL, CapabilityState(True, False, False, False),
            DirtyState.clean("identity-a"), candidate_paths=("aek/a.py",))
        self.assertEqual(plan.required_action, "BOUNDED_SOURCE")
        self.assertEqual(plan.reason_code, "MCP_NOT_VISIBLE")

    def test_unknown_freshness_checks_status_with_full_path_union(self) -> None:
        dirty = mark_dirty(
            DirtyState.clean("identity-a"), batch_id="batch-1",
            identity_digest="identity-b", paths=("aek/a.py", "aek/b.py"),
            reason="git_update")
        plan = plan_barrier(
            QueryKind.STRUCTURAL, CapabilityState(True, True, True, False),
            dirty, candidate_paths=("aek/b.py", "aek/c.py"))
        self.assertEqual(plan.required_action, "CHECK_STATUS")
        self.assertEqual(plan.required_paths,
                         ("aek/a.py", "aek/b.py", "aek/c.py"))

    def test_auto_refresh_proof_selects_graph_first(self) -> None:
        dirty = mark_dirty(
            DirtyState.clean("identity-a"), batch_id="batch-1",
            identity_digest="identity-b", paths=("aek/a.py",),
            reason="git_update")
        plan = plan_barrier(
            QueryKind.STRUCTURAL, CapabilityState(True, True, True, False),
            dirty, candidate_paths=("aek/c.py",))
        observation = CodebaseObservation(
            canonical_root="/repo", expected_root="/repo",
            generation="g2", identity_digest="identity-b",
            covered_paths=plan.required_paths,
            metadata_match_paths=plan.required_paths)
        result = verify_observation(plan, dirty, observation)
        self.assertEqual(result.required_action, "USE_GRAPH")
        self.assertEqual(result.reason_code, "AUTO_REFRESH_CURRENT")
        self.assertEqual(result.primary_tool, "search_graph")

    def test_stale_observation_refreshes_at_most_once_per_epoch(self) -> None:
        dirty = mark_dirty(
            DirtyState.clean("identity-a"), batch_id="batch-1",
            identity_digest="identity-b", paths=("aek/a.py",),
            reason="git_update")
        plan = plan_barrier(
            QueryKind.STRUCTURAL, CapabilityState(True, True, True, False),
            dirty, candidate_paths=())
        stale = CodebaseObservation(
            canonical_root="/repo", expected_root="/repo", generation="g1",
            identity_digest="identity-a", covered_paths=("aek/a.py",),
            metadata_match_paths=())
        refresh = verify_observation(plan, dirty, stale)
        self.assertEqual(refresh.required_action, "REFRESH_ONCE")
        attempted = dirty.with_refresh_started()
        stopped = verify_observation(plan, attempted, stale)
        self.assertEqual(stopped.required_action, "BOUNDED_SOURCE")
        self.assertEqual(stopped.confidence, "degraded")


if __name__ == "__main__":
    unittest.main()
