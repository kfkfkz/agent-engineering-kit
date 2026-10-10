"""CLI capability diagnostics must not start a model or claim isolation."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from adapters.cli import probe_cli  # noqa: E402


class CliProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake_cli(self, version, help_text, *, version_rc=0, help_rc=0):
        script = self.root / "fake.py"
        # Reject every invocation other than the two read-only probes.
        script.write_text(
            "import sys\n"
            "args = sys.argv[1:]\n"
            f"if args == ['--version']:\n print({version!r}); sys.exit({version_rc})\n"
            f"if args in (['--help'], ['exec', '--help']):\n print({help_text!r}); sys.exit({help_rc})\n"
            "raise SystemExit(99)\n", encoding="utf-8")
        return (sys.executable, str(script))

    def test_codex_probe_is_readonly_and_not_live_readiness(self):
        command = self.fake_cli(
            "codex-cli 0.162.0-alpha.2",
            "Options:\n --json\n --model <MODEL>\n --ephemeral\n --sandbox <MODE>\n"
            " --ignore-user-config\n --ignore-rules\n")
        result = probe_cli("codex", command, self.root)
        report = result.to_dict()
        self.assertEqual(report["status"], "COMPATIBLE")
        self.assertEqual(report["version"], "0.162.0-alpha.2")
        self.assertTrue(report["capabilities"]["structured_events"])
        self.assertFalse(report["capabilities"]["max_turns"])
        self.assertEqual(report["authentication"], "NOT_EVALUATED")
        self.assertEqual(report["isolation"], "NOT_EVALUATED")
        self.assertFalse(report["live_ready"])
        self.assertEqual(len(report["probes"]), 2)
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_probe_results_never_retain_raw_cli_output(self):
        result = probe_cli("codex", self.fake_cli("secret-canary", "unused"), self.root)
        self.assertEqual(result.status, "INCOMPATIBLE")
        self.assertNotIn("secret-canary", repr(result))
        self.assertNotIn("secret-canary", json.dumps(result.to_dict()))

    def test_doctor_cli_missing_binary_is_json_diagnostic_from_any_cwd(self):
        entry = Path(__file__).resolve().parent / "benchmark" / "run.py"
        missing = self.root / "missing-cli"
        result = subprocess.run(
            [sys.executable, str(entry), "doctor", "--agent", "codex", "--cli", str(missing)],
            cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "UNAVAILABLE")
        self.assertEqual(report["reason_code"], "CLI_VERSION_PROBE_FAILED")
        self.assertFalse(report["live_ready"])
        self.assertNotIn(str(missing), result.stdout)

    def test_claude_requires_stream_json_in_output_format_declaration(self):
        basic = ("Options:\n --print\n --model <model>\n --no-session-persistence\n"
                 " --verbose\n --bare\n --max-budget-usd <amount>\n")
        compatible = probe_cli("claude", self.fake_cli(
            "2.1.294 (Claude Code)", basic +
            " --output-format <format>\n     choices: text, json, stream-json\n"), self.root)
        self.assertEqual(compatible.status, "COMPATIBLE")
        self.assertTrue(dict(compatible.capabilities)["max_budget_usd"])
        self.assertFalse(dict(compatible.capabilities)["max_turns"])
        wrong_option = probe_cli("claude", self.fake_cli(
            "2.1.294 (Claude Code)", basic +
            " --output-format <format>\n     choices: text, json\n"
            " --input-format <format>\n     choices: stream-json\n"), self.root)
        self.assertEqual(wrong_option.status, "INCOMPATIBLE")
        self.assertFalse(dict(wrong_option.capabilities)["structured_events"])

    def test_prose_does_not_make_an_unsupported_option_available(self):
        result = probe_cli("codex", self.fake_cli(
            "codex-cli 0.162.0",
            "Options:\n --model <MODEL>\n     Future releases might have --json and --ephemeral\n"
            " --sandbox <MODE>\n"), self.root)
        self.assertEqual(result.status, "INCOMPATIBLE")
        self.assertFalse(dict(result.capabilities)["structured_events"])
        self.assertFalse(dict(result.capabilities)["ephemeral_session"])

    def test_failed_or_truncated_help_does_not_report_missing_features(self):
        for help_text, rc in (("secret-canary", 2), ("secret-canary" * 20000, 0)):
            with self.subTest(rc=rc):
                result = probe_cli("codex", self.fake_cli("codex-cli 0.162.0", help_text,
                                                         help_rc=rc), self.root)
                self.assertEqual(result.status, "UNAVAILABLE")
                self.assertEqual(result.reason_code, "CLI_HELP_PROBE_FAILED")
                self.assertTrue(all(value is None for _, value in result.capabilities))
                self.assertNotIn("secret-canary", repr(result))

    def test_invalid_probe_budget_is_rejected_before_process_start(self):
        for timeout in (True, 0, 31, "1"):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "INVALID_PROBE_TIMEOUT"):
                probe_cli("codex", ("never-run",), self.root, timeout_seconds=timeout)

    def test_codex_invocation_is_explicit_safe_argv_and_does_not_execute(self):
        from adapters.cli import build_invocation
        command = self.fake_cli("codex-cli 0.162.0",
                                "Options:\n --json\n --model <MODEL>\n --ephemeral\n --sandbox <MODE>\n"
                                " --ignore-user-config\n --ignore-rules\n")
        probe = probe_cli("codex", command, self.root)
        prompt = '--malicious-option ; $(private-canary) "quoted"\n任务'
        argv = build_invocation(probe, command, model="fixture-model", prompt=prompt)
        self.assertEqual(argv[:len(command)], command)
        self.assertEqual(argv[len(command)], "exec")
        self.assertEqual(argv[-2:], ("--", prompt))
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        with self.assertRaisesRegex(ValueError, "TURN_LIMIT_UNSUPPORTED"):
            build_invocation(probe, command, model="fixture-model", prompt="task", max_turns=3)

    def test_claude_invocation_uses_explicit_mcp_and_no_inherited_settings(self):
        from adapters.cli import build_invocation
        command = self.fake_cli("2.1.295 (Claude Code)",
                                "Options:\n --print\n --model <MODEL>\n --no-session-persistence\n --verbose\n"
                                " --output-format <format>\n   choices: text, json, stream-json\n"
                                " --setting-sources <sources>\n --strict-mcp-config\n --mcp-config <files>\n")
        probe = probe_cli("claude", command, self.root)
        argv = build_invocation(probe, command, model="fixture-model", prompt="--task", mcp_config="/run/mcp.json")
        self.assertEqual(argv[-2:], ("--", "--task"))
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "/run/mcp.json")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--bare", argv)  # Explicit AEK project skills must remain discoverable.
        self.assertNotIn("--dangerously-skip-permissions", argv)
        for config in (None, "../../private.json", "/run/../private.json", "/home/user/settings.json"):
            with self.subTest(config=config), self.assertRaises(ValueError):
                build_invocation(probe, command, model="fixture-model", prompt="task", mcp_config=config)

    def test_invocation_cannot_reuse_probe_from_another_command(self):
        from adapters.cli import build_invocation
        command = self.fake_cli("codex-cli 0.162.0",
                                "Options:\n --json\n --model <MODEL>\n --ephemeral\n --sandbox <MODE>\n"
                                " --ignore-user-config\n --ignore-rules\n")
        probe = probe_cli("codex", command, self.root)
        with self.assertRaisesRegex(ValueError, "PROBE_COMMAND_MISMATCH"):
            build_invocation(probe, ("other-cli",), model="fixture-model", prompt="task")


if __name__ == "__main__":
    unittest.main()
