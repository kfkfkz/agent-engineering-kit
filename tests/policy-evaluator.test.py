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

    def test_legacy_min_route_is_explicitly_migrated_not_routed(self) -> None:
        policy = {"profile": "lightweight", "default": {"require": []},
                  "rules": [{"id": "legacy",
                             "require": ["min_route:standard"],
                             "match": {"paths": ["auth/**"]}}]}
        result = evaluate_policy({"auth/login.py"}, [], set(), policy)
        payload = result.to_dict()
        self.assertIn("impact-analysis", payload["risk_requirements"]["checks"])
        self.assertIn("thorough", payload["risk_requirements"]["reviews"])
        self.assertEqual(payload["deprecated_findings"][0]["action"],
                         "min_route:standard")
        self.assertNotIn("minimum_route", payload)

    def test_unknown_legacy_min_route_fails_closed(self) -> None:
        policy = {"profile": "strict", "default": {"require": []},
                  "rules": [{"id": "bad", "require": ["min_route:extreme"],
                             "match": {"paths": ["**"]}}]}
        with self.assertRaises(PolicyError):
            evaluate_policy({"a.py"}, [], set(), policy)


if __name__ == "__main__":
    unittest.main()
