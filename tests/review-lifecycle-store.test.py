#!/usr/bin/env python3
"""Review gate publication recovers both documented crash windows."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aek.adapters.review_lifecycle_store import LifecycleConflict, LifecycleGateStore
from aek.core.review.lifecycle import (
    IssueAuthority,
    IssueState,
    initialize_lifecycle,
    transition_issue,
)


class InjectedCrash(RuntimeError):
    pass


def verified_snapshot():
    issue = {
        "id": "R-1", "severity": "major", "location": "D2",
        "problem": "wrong order", "evidence": "crash window",
        "required_change": "gate first", "affected_ids": ["D2"],
    }
    opened = initialize_lifecycle(
        stage="详细设计", subject_digest="a" * 64,
        request_digest="b" * 64,
        doc_hashes={"详细设计.md": "c" * 64},
        issues=(issue,), iteration=2, max_iterations=3,
    )
    fingerprint = opened.issues[0].fingerprint
    fixed = transition_issue(
        opened, fingerprint, IssueState.FIXED, IssueAuthority.AUTHOR,
        input_digest="d" * 64)
    return transition_issue(
        fixed, fingerprint, IssueState.VERIFIED, IssueAuthority.REVIEWER,
        input_digest="e" * 64)


class LifecycleGateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.reviews = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def gate(self) -> dict[str, object]:
        return {
            "stage": "详细设计", "status": "PASS",
            "doc_hashes": {"详细设计.md": "c" * 64},
        }

    def test_crash_before_gate_leaves_verified_and_does_not_authorize_close(self) -> None:
        store = LifecycleGateStore(self.reviews, "详细设计")
        verified = verified_snapshot()

        def crash(point: str) -> None:
            if point == "after_verified":
                raise InjectedCrash(point)

        with self.assertRaises(InjectedCrash):
            store.publish_pass_gate(
                verified, self.gate(), expected_snapshot_digest=None,
                checkpoint=crash)
        self.assertEqual(store.load_lifecycle().issues[0].state,
                         IssueState.VERIFIED)
        self.assertIsNone(store.load_gate())
        self.assertEqual(store.recover().issues[0].state, IssueState.VERIFIED)

    def test_crash_after_gate_is_idempotently_materialized_closed(self) -> None:
        store = LifecycleGateStore(self.reviews, "详细设计")
        verified = verified_snapshot()

        def crash(point: str) -> None:
            if point == "after_gate":
                raise InjectedCrash(point)

        with self.assertRaises(InjectedCrash):
            store.publish_pass_gate(
                verified, self.gate(), expected_snapshot_digest=None,
                checkpoint=crash)
        gate = store.load_gate()
        self.assertEqual(gate["lifecycle_snapshot_digest"],
                         verified.snapshot_digest)
        self.assertEqual(store.load_lifecycle().issues[0].state,
                         IssueState.VERIFIED)
        closed = store.recover()
        self.assertEqual(closed.issues[0].state, IssueState.CLOSED)
        self.assertEqual(store.recover(), closed)

    def test_compare_and_swap_rejects_stale_writer(self) -> None:
        store = LifecycleGateStore(self.reviews, "详细设计")
        verified = verified_snapshot()
        store.write_lifecycle(verified, expected_snapshot_digest=None)
        with self.assertRaises(LifecycleConflict):
            store.write_lifecycle(
                verified, expected_snapshot_digest="0" * 64)

    def test_gate_bytes_are_canonical_and_bound(self) -> None:
        store = LifecycleGateStore(self.reviews, "详细设计")
        verified = verified_snapshot()
        closed, gate = store.publish_pass_gate(
            verified, self.gate(), expected_snapshot_digest=None)
        raw = (self.reviews / "详细设计.gate.json").read_bytes()
        self.assertEqual(json.loads(raw), gate)
        self.assertEqual(closed.issues[0].state, IssueState.CLOSED)


if __name__ == "__main__":
    unittest.main()
