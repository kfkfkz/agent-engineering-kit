#!/usr/bin/env python3
"""Review issue lifecycle keeps sensor facts and trusted closure separate."""
from __future__ import annotations

import unittest

from aek.core.review.lifecycle import (
    IssueAuthority,
    IssueState,
    ingest_review_occurrences,
    initialize_lifecycle,
    lifecycle_bytes,
    load_lifecycle,
    project_pass_gate,
    transition_issue,
)


def issue(issue_id: str = "REV-1") -> dict[str, object]:
    return {
        "id": issue_id,
        "severity": "major",
        "location": "详细设计.md §D2",
        "problem": "Gate 发布前写 CLOSED 会留下错误授权。",
        "evidence": "崩溃窗口可留下 lifecycle=CLOSED 且 gate 缺失。",
        "required_change": "先发布 VERIFIED，CLOSED 仅由匹配 PASS gate 派生。",
        "affected_ids": ["D2", "A5"],
    }


class ReviewLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = initialize_lifecycle(
            stage="详细设计",
            subject_digest="a" * 64,
            request_digest="b" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(issue(),),
            iteration=1,
            max_iterations=3,
        )
        self.fingerprint = self.snapshot.issues[0].fingerprint

    def test_stable_issue_identity_ignores_temporary_review_id(self) -> None:
        other = initialize_lifecycle(
            stage="详细设计",
            subject_digest="a" * 64,
            request_digest="d" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(issue("REV-99"),),
            iteration=2,
            max_iterations=3,
        )
        self.assertEqual(other.issues[0].fingerprint, self.fingerprint)
        self.assertNotEqual(
            other.issues[0].occurrences[0].issue_id,
            self.snapshot.issues[0].occurrences[0].issue_id,
        )

    def test_author_can_claim_fixed_but_cannot_close(self) -> None:
        fixed = transition_issue(
            self.snapshot, self.fingerprint, IssueState.FIXED,
            IssueAuthority.AUTHOR, input_digest="d" * 64,
        )
        self.assertEqual(fixed.issues[0].state, IssueState.FIXED)
        with self.assertRaises(ValueError):
            transition_issue(
                fixed, self.fingerprint, IssueState.CLOSED,
                IssueAuthority.AUTHOR, input_digest="e" * 64,
            )

    def test_closed_requires_verified_state_and_matching_pass_gate(self) -> None:
        fixed = transition_issue(
            self.snapshot, self.fingerprint, IssueState.FIXED,
            IssueAuthority.AUTHOR, input_digest="d" * 64,
        )
        verified = transition_issue(
            fixed, self.fingerprint, IssueState.VERIFIED,
            IssueAuthority.REVIEWER, input_digest="e" * 64,
        )
        with self.assertRaises(ValueError):
            transition_issue(
                verified, self.fingerprint, IssueState.CLOSED,
                IssueAuthority.JUDGE, input_digest="f" * 64,
            )
        with self.assertRaises(ValueError):
            transition_issue(
                verified, self.fingerprint, IssueState.CLOSED,
                IssueAuthority.JUDGE, input_digest="f" * 64,
                gate_digest="1" * 64,
            )
        closed = project_pass_gate(
            verified, stage="详细设计", status="PASS",
            lifecycle_snapshot_digest=verified.snapshot_digest,
            gate_digest="1" * 64,
        )
        self.assertEqual(closed.issues[0].state, IssueState.CLOSED)
        self.assertEqual(closed.issues[0].gate_digest, "1" * 64)
        reopened = transition_issue(
            closed, self.fingerprint, IssueState.REOPENED,
            IssueAuthority.REVIEWER, input_digest="2" * 64,
        )
        self.assertEqual(reopened.issues[0].state, IssueState.REOPENED)
        self.assertEqual(len(reopened.events), 4)

    def test_iteration_limit_blocks_without_deleting_history(self) -> None:
        at_limit = initialize_lifecycle(
            stage="详细设计",
            subject_digest="a" * 64,
            request_digest="b" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(issue(),),
            iteration=3,
            max_iterations=3,
        )
        blocked = transition_issue(
            at_limit, at_limit.issues[0].fingerprint, IssueState.BLOCKED,
            IssueAuthority.JUDGE, input_digest="d" * 64,
        )
        self.assertEqual(blocked.issues[0].state, IssueState.BLOCKED)
        self.assertEqual(len(blocked.issues[0].occurrences), 1)

    def test_occurrence_is_appended_and_closed_issue_reopens(self) -> None:
        fixed = transition_issue(
            self.snapshot, self.fingerprint, IssueState.FIXED,
            IssueAuthority.AUTHOR, input_digest="d" * 64,
        )
        verified = transition_issue(
            fixed, self.fingerprint, IssueState.VERIFIED,
            IssueAuthority.REVIEWER, input_digest="e" * 64,
        )
        closed = project_pass_gate(
            verified,
            stage="详细设计",
            status="PASS",
            lifecycle_snapshot_digest=verified.snapshot_digest,
            gate_digest="f" * 64,
        )
        next_round = ingest_review_occurrences(
            closed,
            subject_digest="1" * 64,
            request_digest="2" * 64,
            doc_hashes={"详细设计.md": "3" * 64},
            issues=(issue("REV-2"),),
            iteration=2,
        )
        record = next_round.issues[0]
        self.assertEqual(record.state, IssueState.REOPENED)
        self.assertEqual(len(record.occurrences), 2)
        self.assertEqual(record.occurrences[-1].issue_id, "REV-2")
        self.assertIsNone(record.gate_digest)

    def test_lifecycle_serialization_is_canonical_and_tamper_evident(self) -> None:
        encoded = lifecycle_bytes(self.snapshot)
        self.assertEqual(load_lifecycle(encoded), self.snapshot)
        with self.assertRaises(ValueError):
            load_lifecycle(encoded + b"\n")
        tampered = encoded.replace(b'"iteration":1', b'"iteration":2')
        with self.assertRaises(ValueError):
            load_lifecycle(tampered)

    def test_pass_gate_projection_is_bound_and_idempotent(self) -> None:
        fixed = transition_issue(
            self.snapshot, self.fingerprint, IssueState.FIXED,
            IssueAuthority.AUTHOR, input_digest="d" * 64,
        )
        verified = transition_issue(
            fixed, self.fingerprint, IssueState.VERIFIED,
            IssueAuthority.REVIEWER, input_digest="e" * 64,
        )
        for bad in (
            {"status": "FAIL", "lifecycle_snapshot_digest": verified.snapshot_digest},
            {"status": "PASS", "lifecycle_snapshot_digest": "0" * 64},
        ):
            with self.assertRaises(ValueError):
                project_pass_gate(
                    verified, stage="详细设计", gate_digest="f" * 64,
                    **bad,
                )
        closed = project_pass_gate(
            verified,
            stage="详细设计",
            status="PASS",
            lifecycle_snapshot_digest=verified.snapshot_digest,
            gate_digest="f" * 64,
        )
        self.assertEqual(closed.issues[0].state, IssueState.CLOSED)
        self.assertEqual(
            project_pass_gate(
                closed,
                stage="详细设计",
                status="PASS",
                lifecycle_snapshot_digest=verified.snapshot_digest,
                gate_digest="f" * 64,
            ),
            closed,
        )


if __name__ == "__main__":
    unittest.main()
