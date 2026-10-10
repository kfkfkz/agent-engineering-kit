"""Independent verifier results are bound to pinned trusted script bytes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from adapters.check_runner import run_checks  # noqa: E402
from adapters.scenario_file import load_scenario  # noqa: E402


class IndependentCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "candidate"
        self.workspace.mkdir()
        (self.root / "checks").mkdir()
        self.script = self.root / "checks" / "verify.py"

    def scenario(self, source, extra_args=()):
        self.script.write_text(source, encoding="utf-8")
        value = {
            "schema_version": 1, "id": "SC-001", "name": "behavior", "category": "bug",
            "task": {"prompt": "Make answer correct"},
            "fixture": {"path": "fixture", "commit": "a" * 40, "sha256": "b" * 64},
            "execution": {"agents": ["codex"], "variants": ["vanilla", "aek"],
                          "timeout_seconds": 20, "repeat": 1},
            "evaluation": {
                "checks": [{"id": "answer", "entrypoint": "checks/verify.py",
                            "sha256": hashlib.sha256(self.script.read_bytes()).hexdigest(),
                            "argv": ["{python}", "{check}", "{workspace}", *extra_args],
                            "timeout_seconds": 2, "kind": "outcome"}],
                "allowed_paths": ["answer.txt"], "forbidden_paths": [],
            },
        }
        path = self.root / "scenario.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return load_scenario(path)

    def test_independent_assertion_overrules_candidate_success_message(self):
        scenario = self.scenario(
            "from pathlib import Path\nimport sys\n"
            "sys.exit(0 if (Path(sys.argv[1])/'answer.txt').read_text()=='correct' else 1)\n")
        (self.workspace / "answer.txt").write_text("wrong", encoding="utf-8")
        (self.workspace / "agent-report.txt").write_text("PASS: everything done", encoding="utf-8")
        failed = run_checks(scenario, self.workspace)
        self.assertEqual(failed.status, "FAIL")
        self.assertEqual(failed.checks[0].exit_code, 1)
        (self.workspace / "answer.txt").write_text("correct", encoding="utf-8")
        passed = run_checks(scenario, self.workspace)
        self.assertEqual(passed.status, "PASS")
        self.assertEqual(passed.checks[0].script_sha256, scenario.scenario.checks[0].sha256)
        self.assertEqual(passed.scope, "development_independent_checks")

    def test_changed_check_digest_is_rejected_without_running_it(self):
        loaded = self.scenario("raise SystemExit(0)\n")
        self.script.write_text(
            "from pathlib import Path\nPath('should-not-run').write_text('bad')\n",
            encoding="utf-8")
        result = run_checks(loaded, self.workspace)
        self.assertEqual(result.status, "INFRA_ERROR")
        self.assertEqual(result.checks[0].reason_code, "CHECK_SOURCE_INVALID")
        self.assertFalse((self.workspace / "should-not-run").exists())

    def test_timeout_and_output_limit_are_infrastructure_results(self):
        sources = (
            ("import time\ntime.sleep(30)\n", "PROCESS_TIMEOUT"),
            ("import sys\nsys.stdout.write('secret-canary'*20000)\n", "OUTPUT_LIMIT_EXCEEDED"),
        )
        for source, expected in sources:
            with self.subTest(reason=expected):
                result = run_checks(self.scenario(source), self.workspace)
                self.assertEqual(result.status, "INFRA_ERROR")
                self.assertEqual(result.checks[0].reason_code, expected)
                self.assertNotIn("secret-canary", repr(result))

    def test_verifier_error_does_not_become_code_assertion_failure(self):
        result = run_checks(self.scenario("raise SystemExit(2)\n"), self.workspace)
        self.assertEqual(result.status, "INFRA_ERROR")
        self.assertEqual(result.checks[0].reason_code, "CHECK_EXECUTION_ERROR")

    def test_missing_verifier_is_not_pass(self):
        loaded = self.scenario("raise SystemExit(0)\n")
        self.script.unlink()
        result = run_checks(loaded, self.workspace)
        self.assertEqual(result.status, "INFRA_ERROR")

    def test_shell_metacharacters_remain_one_literal_argument(self):
        loaded = self.scenario(
            "import sys\nassert sys.argv[2] == '; touch should-not-run'\n",
            extra_args=("; touch should-not-run",))
        result = run_checks(loaded, self.workspace)
        self.assertEqual(result.status, "PASS")
        self.assertFalse((self.workspace / "should-not-run").exists())

    def test_changed_scenario_cannot_reuse_old_verification_identity(self):
        loaded = self.scenario(
            "from pathlib import Path\nPath('should-not-run').write_text('bad')\n")
        value = json.loads(loaded.source.read_text(encoding="utf-8"))
        value["task"]["prompt"] = "changed after selection"
        loaded.source.write_text(json.dumps(value), encoding="utf-8")
        result = run_checks(loaded, self.workspace)
        self.assertEqual(result.status, "INFRA_ERROR")
        self.assertEqual(result.checks[0].reason_code, "SCENARIO_SOURCE_CHANGED")
        self.assertFalse((self.workspace / "should-not-run").exists())

    def test_verifier_inside_candidate_tree_is_not_independent(self):
        loaded = self.scenario("raise SystemExit(0)\n")
        result = run_checks(loaded, self.root)
        self.assertEqual(result.status, "INFRA_ERROR")
        self.assertEqual(result.checks[0].reason_code, "CHECK_TRUST_BOUNDARY_INVALID")

    def test_existing_evaluator_can_load_a_trusted_scenario_from_any_cwd(self):
        loaded = self.scenario("raise SystemExit(0)\n")
        entry = Path(__file__).resolve().parent / "benchmark" / "evaluate.py"
        result = subprocess.run([sys.executable, str(entry), "--repo", str(self.workspace),
                                 "--scenario-file", str(loaded.source), "--json"],
                                cwd=self.workspace, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["task_correctness"]["status"], "PASS")
        self.assertEqual(report["scope"], "development_independent_checks")
        self.assertEqual(report["artifact_evidence"]["status"], "NOT_EVALUATED")


if __name__ == "__main__":
    unittest.main()
