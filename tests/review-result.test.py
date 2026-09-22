#!/usr/bin/env python3
"""Review input is a validated sensor result bound to full document hashes."""
from __future__ import annotations

import json
import unittest

from aek.core.review.result import validate_review_payload


class ReviewResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = {"概要设计.md": "a" * 64}

    def test_clean_and_normalized_issue(self) -> None:
        clean = json.dumps({"stage": "概要设计", "doc_hashes": self.docs, "issues": []})
        result, error = validate_review_payload(clean, "概要设计", self.docs)
        self.assertEqual(error, "")
        self.assertEqual(result.issues, ())
        payload = json.dumps({"stage": "概要设计", "doc_hashes": self.docs,
                              "issues": [{"id": "R1", "severity": "MAJOR",
                                          "location": "§2", "problem": "gap",
                                          "required_change": "fix"}]})
        result, error = validate_review_payload(payload, "概要设计", self.docs)
        self.assertEqual(error, "")
        self.assertEqual(result.issues[0]["severity"], "major")

    def test_full_hash_and_schema_fail_closed(self) -> None:
        for payload in (
            {"stage": "概要设计", "doc_hashes": {"概要设计.md": "b" * 64}, "issues": []},
            {"stage": "概要设计", "doc_hashes": {}, "issues": []},
            {"stage": "详细设计", "doc_hashes": self.docs, "issues": []},
            {"stage": "概要设计", "doc_hashes": self.docs, "issues": [{"id": "R1"}]},
        ):
            result, error = validate_review_payload(
                json.dumps(payload), "概要设计", self.docs)
            self.assertIsNone(result)
            self.assertTrue(error)

    def test_unhashable_issue_id_is_a_validation_error(self) -> None:
        payload = {"stage": "概要设计", "doc_hashes": self.docs,
                   "issues": [{"id": ["R1"], "severity": "minor", "location": "§2",
                               "problem": "gap", "required_change": "fix"}]}
        result, error = validate_review_payload(
            json.dumps(payload), "概要设计", self.docs)
        self.assertIsNone(result)
        self.assertIn("id", error)


if __name__ == "__main__":
    unittest.main()
