"""Read-only governance defaults diagnostics through the doctor entry point."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from installer.doctor import governance_defaults_finding, run_doctor_checks
from installer.governance import default_governance_policy


class GovernanceDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / ".repo-memory-kit" / "governance.json"
        self.policy.parent.mkdir()

    def test_missing_defaults_are_advisory_and_never_rewrite_team_rules(self):
        before = b'{"version":1,"rules":[]}'
        self.policy.write_bytes(before)
        report = run_doctor_checks(self.root)
        finding = next(f for f in report.findings
                       if f.spec_id == "governance-defaults")
        self.assertEqual(finding.status, "policy_update_available")
        self.assertIn("baseline=unknown", finding.detail)
        self.assertIn("DB-SQL-001", finding.detail)
        self.assertEqual(self.policy.read_bytes(), before)
        self.assertNotIn("POLICY_UPDATE_AVAILABLE", report.statuses)

    def test_current_defaults_and_reordered_lists_do_not_suggest_migration(self):
        policy = default_governance_policy()
        policy["rules"].reverse()
        for rule in policy["rules"]:
            rule["require"].reverse()
            for values in rule["match"].values():
                values.reverse()
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        finding = governance_defaults_finding(self.root)
        self.assertEqual(finding.status, "managed")
        self.assertIn("missing=none; different=none", finding.detail)
        # Content equality cannot prove which release originally seeded it.
        self.assertIn("baseline=unknown", finding.detail)

    def test_custom_changes_are_reported_without_claiming_weaker_policy(self):
        policy = default_governance_policy()
        policy["rules"][1]["require"].append("required_check:team-review")
        policy["rules"].append({"id": "TEAM-001", "require": ["receipt"],
                                "match": {"files": ["*.java"]}})
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        finding = governance_defaults_finding(self.root)
        self.assertEqual(finding.status, "policy_update_available")
        self.assertIn("different=DB-001", finding.detail)
        self.assertIn("additional=1", finding.detail)
        self.assertIn("不代表策略更弱", finding.detail)

    def test_additional_team_rules_alone_do_not_suggest_migration(self):
        policy = default_governance_policy()
        policy["rules"].append({"id": "TEAM-001", "require": ["receipt"]})
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        finding = governance_defaults_finding(self.root)
        self.assertEqual(finding.status, "managed")
        self.assertIn("additional=1", finding.detail)

    def test_missing_invalid_unknown_and_oversized_policy_remain_unverifiable(self):
        self.assertEqual(governance_defaults_finding(self.root).status,
                         "unverifiable")
        self.assertFalse(self.policy.exists())
        invalid = (
            b'{"secret":"secret-canary"', b'\xff', b'[]',
            b'{"version":true,"rules":[]}',
            b'{"version":99,"rules":[]}',
            b'{"version":1,"rules":[{"id":"DUP"},{"id":"DUP"}]}',
            b'{"version":1,"rules":[{"id":"RULE","require":false}]}',
            b'[' * 20000 + b'0' + b']' * 20000,
            b' ' * (1024 * 1024 + 1),
        )
        for raw in invalid:
            with self.subTest(size=len(raw), prefix=raw[:30]):
                self.policy.write_bytes(raw)
                finding = governance_defaults_finding(self.root)
                self.assertEqual(finding.status, "unverifiable")
                self.assertNotIn("secret-canary", finding.detail)
                self.assertEqual(self.policy.read_bytes(), raw)

    def test_symlink_policy_is_not_followed(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(default_governance_policy()), encoding="utf-8")
        try:
            self.policy.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable on this platform")
        self.assertEqual(governance_defaults_finding(self.root).status,
                         "unverifiable")
        self.assertTrue(self.policy.is_symlink())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_special_policy_file_does_not_block_doctor(self):
        os.mkfifo(self.policy)
        self.assertEqual(governance_defaults_finding(self.root).status,
                         "unverifiable")

    def test_defaults_are_returned_as_independent_objects(self):
        first = default_governance_policy()
        first["rules"][0]["require"].clear()
        self.assertIn("receipt", default_governance_policy()["rules"][0]["require"])


if __name__ == "__main__":
    unittest.main()
