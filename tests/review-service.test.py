#!/usr/bin/env python3
"""Application ReviewService sends only bound bytes and verifies receipts."""
from __future__ import annotations

import hashlib
import json
import unittest

from aek.application.review import (
    full_review_request, load_review_request, load_review_snapshot, plan_review,
    review_request_bytes, review_snapshot_bytes, reviewer_receipt,
    validate_review_result,
)
from aek.core.review.incremental import ReviewSnapshot, SectionSnapshot


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_snapshot(target: str) -> tuple[ReviewSnapshot, dict, dict]:
    documents = {"设计.md": target + "\nconsumer"}
    sections = {("设计.md", "target"): target,
                ("设计.md", "consumer"): "consumer"}
    snapshot = ReviewSnapshot(
        "详细设计", (("设计.md", sha(documents["设计.md"])),),
        (SectionSnapshot("设计.md", "target", sha(target), ("D1",), ()),
         SectionSnapshot("设计.md", "consumer", sha("consumer"), (), ("D1",))),
        "f" * 64, True, False)
    return snapshot, documents, sections


class ReviewServiceTests(unittest.TestCase):
    def test_incremental_payload_contains_only_proven_closure(self) -> None:
        old, old_docs, _old_sections = make_snapshot("before")
        current, documents, sections = make_snapshot("after")
        request = plan_review(
            old, current, prior_review_was_full=True,
            reviewed_doc_hashes=old.doc_hashes, documents=documents,
            section_texts=sections,
            supporting_context={"reviews/hard-checks.json": "{}"})
        self.assertEqual(request.mode, "incremental")
        self.assertEqual(request.full_documents, ())
        self.assertEqual(request.supporting_context,
                         (("reviews/hard-checks.json", "{}"),))
        self.assertEqual({item[:2] for item in request.section_documents},
                         set(sections))
        self.assertEqual(len(request.request_digest), 64)
        self.assertEqual(load_review_request(review_request_bytes(request)), request)
        self.assertEqual(load_review_snapshot(review_snapshot_bytes(current)), current)

        result = json.dumps({
            "stage": "详细设计", "doc_hashes": dict(current.doc_hashes),
            "issues": [],
        })
        validated = validate_review_result(
            request, result, reviewer_receipt(request))
        self.assertEqual(validated.state, "VALID")

    def test_bad_incremental_receipt_requires_full_resend(self) -> None:
        old, _old_docs, _old_sections = make_snapshot("before")
        current, documents, sections = make_snapshot("after")
        request = plan_review(
            old, current, prior_review_was_full=True,
            reviewed_doc_hashes=old.doc_hashes, documents=documents,
            section_texts=sections)
        checked = validate_review_result(request, "{}", "{}")
        self.assertEqual(checked.state, "FULL_REQUIRED")
        resent = full_review_request(
            current, documents=documents, section_texts=sections,
            reason=checked.reason, prior_doc_hashes=old.doc_hashes)
        self.assertEqual(resent.mode, "full")
        self.assertEqual(dict(resent.full_documents), documents)
        self.assertEqual(resent.section_documents, ())

    def test_section_payload_mismatch_falls_back_to_full(self) -> None:
        old, _old_docs, _old_sections = make_snapshot("before")
        current, documents, sections = make_snapshot("after")
        sections[("设计.md", "target")] = "tampered"
        request = plan_review(
            old, current, prior_review_was_full=True,
            reviewed_doc_hashes=old.doc_hashes, documents=documents,
            section_texts=sections)
        self.assertEqual(request.mode, "full")
        self.assertEqual(dict(request.full_documents), documents)

    def test_stale_full_document_snapshot_is_rejected(self) -> None:
        old, _old_docs, _old_sections = make_snapshot("before")
        current, documents, sections = make_snapshot("after")
        documents["设计.md"] += " changed"
        with self.assertRaises(ValueError):
            plan_review(
                old, current, prior_review_was_full=True,
                reviewed_doc_hashes=old.doc_hashes, documents=documents,
                section_texts=sections)

    def test_persisted_request_and_snapshot_reject_unknown_fields(self) -> None:
        current, documents, sections = make_snapshot("after")
        request = full_review_request(
            current, documents=documents, section_texts=sections,
            reason="TEST")
        request_data = json.loads(review_request_bytes(request))
        request_data["ignored"] = True
        with self.assertRaises(ValueError):
            load_review_request(json.dumps(request_data).encode())
        snapshot_data = json.loads(review_snapshot_bytes(current))
        snapshot_data["ignored"] = True
        with self.assertRaises(ValueError):
            load_review_snapshot(json.dumps(snapshot_data).encode())


if __name__ == "__main__":
    unittest.main()
