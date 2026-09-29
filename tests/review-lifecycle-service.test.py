#!/usr/bin/env python3
"""Application reconciliation keeps Author, Reviewer and Judge authority apart."""
from __future__ import annotations

import json
import unittest

from aek.application.review_lifecycle import prepare_lifecycle
from aek.core.review.lifecycle import IssueState


def legacy_issue(issue_id: str = "R-1") -> dict[str, object]:
    return {
        "id": issue_id,
        "severity": "major",
        "location": "详细设计.md D2",
        "problem": "A5 的关闭顺序错误",
        "required_change": "先 VERIFIED 再发布 PASS gate",
    }


def claims(snapshot_digest: str, fingerprint: str,
           doc_hash: str = "d" * 64) -> str:
    return json.dumps({
        "schema_version": 1,
        "stage": "详细设计",
        "base_snapshot_digest": snapshot_digest,
        "doc_hashes": {"详细设计.md": doc_hash},
        "claims": [{"fingerprint": fingerprint, "state": "FIXED"}],
    }, ensure_ascii=False)


class ReviewLifecycleServiceTests(unittest.TestCase):
    def test_legacy_issue_is_normalized_but_not_trusted_to_close(self) -> None:
        snapshot = prepare_lifecycle(
            None, stage="详细设计", subject_digest="a" * 64,
            request_digest="b" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(legacy_issue(),), iteration=1, max_iterations=3,
        )
        row = snapshot.issues[0]
        self.assertEqual(row.state, IssueState.OPEN)
        self.assertEqual(row.occurrences[0].evidence, legacy_issue()["problem"])
        self.assertEqual(row.occurrences[0].affected_ids, ("A5", "D2"))

    def test_author_claim_plus_bound_clean_review_advances_to_verified(self) -> None:
        first = prepare_lifecycle(
            None, stage="详细设计", subject_digest="a" * 64,
            request_digest="b" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(legacy_issue(),), iteration=1, max_iterations=3,
        )
        fingerprint = first.issues[0].fingerprint
        verified = prepare_lifecycle(
            first, stage="详细设计", subject_digest="1" * 64,
            request_digest="2" * 64,
            doc_hashes={"详细设计.md": "d" * 64},
            issues=(), iteration=2, max_iterations=3,
            claims_text=claims(first.snapshot_digest, fingerprint),
        )
        self.assertEqual(verified.issues[0].state, IssueState.VERIFIED)
        # Retrying the same validated review and claim is idempotent.
        self.assertEqual(
            prepare_lifecycle(
                verified, stage="详细设计", subject_digest="1" * 64,
                request_digest="2" * 64,
                doc_hashes={"详细设计.md": "d" * 64},
                issues=(), iteration=2, max_iterations=3,
                claims_text=claims(first.snapshot_digest, fingerprint),
            ),
            verified,
        )

    def test_reappearing_issue_reopens_instead_of_verifying(self) -> None:
        first = prepare_lifecycle(
            None, stage="详细设计", subject_digest="a" * 64,
            request_digest="b" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(legacy_issue(),), iteration=1, max_iterations=3,
        )
        fingerprint = first.issues[0].fingerprint
        reopened = prepare_lifecycle(
            first, stage="详细设计", subject_digest="1" * 64,
            request_digest="2" * 64,
            doc_hashes={"详细设计.md": "d" * 64},
            issues=(legacy_issue("R-2"),), iteration=2, max_iterations=3,
            claims_text=claims(first.snapshot_digest, fingerprint),
        )
        self.assertEqual(reopened.issues[0].state, IssueState.REOPENED)
        self.assertEqual(len(reopened.issues[0].occurrences), 2)

    def test_stale_or_unknown_claim_fails_closed(self) -> None:
        first = prepare_lifecycle(
            None, stage="详细设计", subject_digest="a" * 64,
            request_digest="b" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            issues=(legacy_issue(),), iteration=1, max_iterations=3,
        )
        for claim_text in (
            claims("0" * 64, first.issues[0].fingerprint),
            claims(first.snapshot_digest, "9" * 64),
        ):
            with self.assertRaises(ValueError):
                prepare_lifecycle(
                    first, stage="详细设计", subject_digest="1" * 64,
                    request_digest="2" * 64,
                    doc_hashes={"详细设计.md": "d" * 64},
                    issues=(), iteration=2, max_iterations=3,
                    claims_text=claim_text,
                )


if __name__ == "__main__":
    unittest.main()
