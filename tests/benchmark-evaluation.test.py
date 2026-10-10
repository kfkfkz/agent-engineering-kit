"""Benchmark correctness is measured only when trusted verification ran."""
from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parent / "benchmark" / "evaluate.py"
spec = importlib.util.spec_from_file_location("benchmark_evaluation", ENTRY)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


class BenchmarkEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        for args in (("init", "-q"), ("config", "user.name", "Benchmark Test"),
                     ("config", "user.email", "test@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                           capture_output=True)
        (self.repo / "source.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"],
                       check=True)

    def test_missing_verification_is_not_a_success_or_failure(self):
        scenario = benchmark.Scenario(id="empty-check", name="empty", description="")
        result = benchmark.evaluate_scenario(self.repo, scenario, "HEAD").to_dict()
        correctness = result["task_correctness"]
        self.assertFalse(correctness["success"])
        self.assertEqual(correctness["status"], "NOT_EVALUATED")
        self.assertEqual(benchmark.run_tests(self.repo, "")[0], False)

    def test_whitespace_verification_is_not_evaluated(self):
        result = benchmark.run_verification(self.repo, " \n\t ")
        self.assertEqual(result.status.value, "NOT_EVALUATED")
        self.assertFalse(result.success)

    def test_actual_verifier_success_and_assertion_failure_remain_distinct(self):
        for code, expected in (("print('checked')", "PASS"),
                               ("assert False, 'wrong result'", "FAIL")):
            with self.subTest(expected=expected):
                argv = [sys.executable, "-c", code]
                command = (subprocess.list2cmdline(argv) if os.name == "nt"
                           else shlex.join(argv))
                result = benchmark.run_verification(self.repo, command)
                self.assertEqual(result.status.value, expected)
                self.assertEqual(result.success, expected == "PASS")

    def test_timeout_or_spawn_failure_are_infrastructure_errors(self):
        for error in (subprocess.TimeoutExpired("verifier", 120),
                      FileNotFoundError("secret-canary")):
            with self.subTest(error=type(error).__name__):
                with patch.object(benchmark.subprocess, "run", side_effect=error):
                    result = benchmark.run_verification(self.repo, "trusted-check")
                self.assertEqual(result.status.value, "INFRA_ERROR")
                self.assertFalse(result.success)
                self.assertNotIn("secret-canary", result.detail)

    def test_command_unavailable_and_signal_termination_are_not_code_failures(self):
        for returncode in (126, 127, -15):
            with self.subTest(returncode=returncode):
                outcome = subprocess.CompletedProcess("check", returncode,
                                                      stdout="", stderr="unavailable")
                with patch.object(benchmark.subprocess, "run", return_value=outcome):
                    result = benchmark.run_verification(self.repo, "trusted-check")
                self.assertEqual(result.status.value, "INFRA_ERROR")
                self.assertFalse(result.success)

    def test_pair_report_does_not_turn_missing_tests_into_failure_symbols(self):
        scenario = benchmark.Scenario(id="empty-check", name="empty", description="")
        result = benchmark.evaluate_scenario(self.repo, scenario, "HEAD")
        report = benchmark.compare_results(result, result)
        row = next(line for line in report.splitlines() if "Task Correctness" in line)
        self.assertEqual(row.count("NOT_EVALUATED"), 2)
        self.assertNotIn("✗", row)


if __name__ == "__main__":
    unittest.main()
