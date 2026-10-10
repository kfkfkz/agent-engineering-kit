"""Public synthetic cases have independently verified bad/good reference behavior."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "benchmark"))
from adapters.check_runner import run_checks  # noqa: E402
from adapters.workspace import prepare_pair  # noqa: E402
from scenarios.catalog import materialize_scenario, reference_files  # noqa: E402


class SyntheticScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_simple_bug_baseline_fails_and_independent_reference_passes(self):
        loaded = materialize_scenario("SC-001", self.root)
        prepared = prepare_pair(loaded, self.root)
        workspace = prepared.workspace("vanilla").path
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        self.assertFalse((workspace / "checks").exists())
        self.assertFalse((workspace / "expected").exists())
        for path, content in reference_files("SC-001"):
            (workspace / path).write_bytes(content)
        self.assertEqual(run_checks(loaded, workspace).status, "PASS")
        again = materialize_scenario("SC-001", self.root)
        self.assertEqual(loaded.scenario.fixture_commit, again.scenario.fixture_commit)
        self.assertEqual(loaded.scenario.fixture_sha256, again.scenario.fixture_sha256)
        self.assertEqual(loaded.scenario.digest, again.scenario.digest)

    def test_crud_feature_uses_behavior_checks_not_document_presence(self):
        loaded = materialize_scenario("SC-002", self.root)
        workspace = prepare_pair(loaded, self.root).workspace("aek").path
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        (workspace / "DESIGN.md").write_text("CRUD completed. PASS.", encoding="utf-8")
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        for path, content in reference_files("SC-002"):
            (workspace / path).write_bytes(content)
        self.assertEqual(run_checks(loaded, workspace).status, "PASS")

    def test_concurrency_case_rejects_duplicate_commits_and_accepts_atomic_reference(self):
        loaded = materialize_scenario("SC-003", self.root)
        workspace = prepare_pair(loaded, self.root).workspace("vanilla").path
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        for path, content in reference_files("SC-003"):
            (workspace / path).write_bytes(content)
        self.assertEqual(run_checks(loaded, workspace).status, "PASS")

    def test_candidate_success_text_and_output_flood_do_not_become_pass(self):
        loaded = materialize_scenario("SC-001", self.root)
        workspace = prepare_pair(loaded, self.root).workspace("vanilla").path
        (workspace / "app.py").write_text("print('PASS: complete')\n", encoding="utf-8")
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        (workspace / "app.py").write_text("print('secret-canary'*20000)\n", encoding="utf-8")
        result = run_checks(loaded, workspace)
        self.assertEqual(result.status, "INFRA_ERROR")
        self.assertNotIn("secret-canary", repr(result))

    def test_unknown_scenario_cannot_select_paths_or_create_output(self):
        for value in ("../SC-001", "SC-999", [], None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "UNKNOWN_PUBLIC_SCENARIO"):
                materialize_scenario(value, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sqlite_migration_preserves_legacy_data_and_rolls_back_injected_failure(self):
        loaded = materialize_scenario("SC-004", self.root)
        workspace = prepare_pair(loaded, self.root).workspace("aek").path
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        for path, content in reference_files("SC-004"):
            (workspace / path).write_bytes(content)
        self.assertEqual(run_checks(loaded, workspace).status, "PASS")

    def test_history_constraint_is_same_for_both_variants_and_judged_by_behavior(self):
        loaded = materialize_scenario("SC-005", self.root)
        prepared = prepare_pair(loaded, self.root)
        left, right = prepared.workspace("vanilla").path, prepared.workspace("aek").path
        path = "docs/memory/external-keys.md"
        self.assertEqual((left / path).read_bytes(), (right / path).read_bytes())
        baseline = run_checks(loaded, left)
        self.assertEqual(baseline.status, "FAIL")
        self.assertEqual([check.kind for check in baseline.checks], ["outcome", "memory"])
        (left / "RECEIPT.md").write_text("memory history decision retained PASS", encoding="utf-8")
        self.assertEqual(run_checks(loaded, left).status, "FAIL")
        for filename, content in reference_files("SC-005"):
            (left / filename).write_bytes(content)
        self.assertEqual(run_checks(loaded, left).status, "PASS")

    def test_external_commit_recovery_is_verified_by_independent_service_state(self):
        loaded = materialize_scenario("SC-006", self.root)
        workspace = prepare_pair(loaded, self.root).workspace("vanilla").path
        self.assertEqual(run_checks(loaded, workspace).status, "FAIL")
        for path, content in reference_files("SC-006"):
            (workspace / path).write_bytes(content)
        self.assertEqual(run_checks(loaded, workspace).status, "PASS")


if __name__ == "__main__":
    unittest.main()
