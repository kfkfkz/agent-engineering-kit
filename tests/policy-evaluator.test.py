#!/usr/bin/env python3
"""Governance rule matching is a pure, deterministic Core decision."""
from __future__ import annotations

import unittest

from aek.core.policy.evaluator import PolicyError, evaluate_policy


class PolicyEvaluatorTests(unittest.TestCase):
    def test_matches_normalized_diff_and_merges_actions(self) -> None:
        policy = {"profile": "strict", "default": {"require": ["receipt"]},
                  "rules": [{"id": "security", "require": ["independent_review"],
                             "match": {"paths": ["security/**"]}},
                            {"id": "sql", "require": ["db_screen"],
                             "match": {"added_lines_regex": [r"SELECT\s+"]}}]}
        result = evaluate_policy(
            {"security/check.py", "data/query.py"},
            [("data/query.py", 5, "SELECT * FROM orders")], set(), policy)
        self.assertEqual([row.rule_id for row in result.matched], ["security", "sql"])
        self.assertEqual(result.required_actions, ["independent_review", "db_screen"])
        self.assertTrue(result.needs_formal_receipt)
        self.assertTrue(result.needs_independent_review)
        self.assertEqual(result.changed_files, ["data/query.py", "security/check.py"])

    def test_invalid_regex_is_a_policy_error(self) -> None:
        policy = {"profile": "lightweight", "default": {"require": ["inline_review"]},
                  "rules": [{"id": "bad", "require": ["receipt"],
                             "match": {"added_lines_regex": ["["]}}]}
        with self.assertRaises(PolicyError):
            evaluate_policy({"a.py"}, [], set(), policy)

    def test_path_matching_is_case_stable_across_hosts(self) -> None:
        policy = {"profile": "strict", "default": {"require": ["receipt"]},
                  "rules": [{"id": "case", "require": ["security_review"],
                             "match": {"paths": ["security/*.py"],
                                       "files": ["secret.py"]}}]}
        result = evaluate_policy({"Security/Secret.py"}, [], set(), policy)
        self.assertEqual(result.matched, [])


if __name__ == "__main__":
    unittest.main()
