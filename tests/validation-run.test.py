"""Run real verification processes through the compact log carrier."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "validation-run"


class ValidationRunTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.log = self.repo / "验证 日志.txt"
        self.scope = self.repo / "范围.json"
        self.receipt = self.repo / "检查回执.json"

    def run_check(self, *command: str, options=(), cwd=None):
        result = subprocess.run([sys.executable, str(RUNNER), "--log", str(self.log),
                                 *options, "--", *command], cwd=cwd or self.repo, capture_output=True,
                                timeout=15, check=False)
        self.assertEqual(result.stderr, b"")
        self.assertLess(len(result.stdout), 768)
        return result.returncode, json.loads(result.stdout)

    def bound_check(self, *, reuse=False, command=None, cwd=None):
        options = ["--scope", str(self.scope), "--receipt", str(self.receipt)]
        if reuse:
            options.append("--reuse")
        if command is None:
            command = (sys.executable, "-c", "from pathlib import Path; import os; "
                       "Path('once').touch(exist_ok=False); os.write(1, b'validated')")
        return self.run_check(*command, options=options, cwd=cwd)

    def assert_log(self, report, content: bytes) -> None:
        self.assertEqual(Path(report["full_result_ref"]), self.log)
        self.assertEqual(self.log.read_bytes(), content)
        self.assertEqual(report["result_digest"], hashlib.sha256(content).hexdigest())
        self.assertEqual(report["log_bytes"], len(content))

    def test_large_success_is_compact_with_verifiable_complete_log(self) -> None:
        code, summary = self.run_check(
            sys.executable, "-c", "import os; os.write(1, b'x' * 131072); os.write(2, b' stderr')")
        self.assertEqual((code, summary["status"], summary["returncode"]), (0, "PASS", 0))
        self.assert_log(summary, b"x" * 131072 + b" stderr")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_failed_check_returns_real_exit_code_and_log(self) -> None:
        code, summary = self.run_check(
            sys.executable, "-c", "import os; os.write(2, b'case.py:12 failed'); raise SystemExit(7)")
        self.assertEqual((code, summary["status"], summary["returncode"], summary["exit_code"]),
                         (7, "FAIL", 7, 7))
        self.assert_log(summary, b"case.py:12 failed")

    def test_existing_log_is_preserved_and_command_never_runs(self) -> None:
        self.log.write_bytes(b"prior result")
        code, summary = self.run_check(
            sys.executable, "-c", "from pathlib import Path; Path('executed').touch()")
        self.assertEqual((code, summary["status"], summary["reason_code"]),
                         (2, "ERROR", "LOG_NOT_CREATED"))
        self.assertEqual(self.log.read_bytes(), b"prior result")
        self.assertFalse((self.repo / "executed").exists())

    def test_missing_command_is_not_reported_as_a_completed_check(self) -> None:
        code, summary = self.run_check(str(self.repo / "not-installed"))
        self.assertEqual((code, summary["status"], summary["returncode"], summary["reason_code"]),
                         (127, "ERROR", None, "COMMAND_NOT_STARTED"))
        self.assert_log(summary, self.log.read_bytes())

    def test_current_scope_reuses_receipt_without_running_again(self) -> None:
        self.scope.write_text('{"revision":"r1","target":"125","tenant":"test"}', encoding="utf-8")
        code, original = self.bound_check()
        self.assertEqual((code, original["status"]), (0, "PASS"))
        self.assertEqual(json.loads(self.receipt.read_bytes()), original)
        code, reused = self.bound_check(reuse=True)
        self.assertEqual(code, 0)
        self.assertEqual(reused, {**original, "reused": True})
        self.assert_log(reused, b"validated")

    def test_stale_revision_tenant_workdir_or_command_cannot_reuse(self) -> None:
        original = '{"revision":"r1","target":"125","tenant":"test"}'
        self.scope.write_text(original, encoding="utf-8")
        self.assertEqual(self.bound_check()[0], 0)
        for stale in (original.replace("r1", "r2"), original.replace("test", "other")):
            with self.subTest(snapshot=stale):
                self.scope.write_text(stale, encoding="utf-8")
                code, summary = self.bound_check(reuse=True)
                self.assertEqual((code, summary["reason_code"]), (2, "EVIDENCE_SCOPE_MISMATCH"))
        self.scope.write_text(original, encoding="utf-8")
        command = (sys.executable, "-c", "from pathlib import Path; Path('unexpected').touch()")
        self.assertEqual(self.bound_check(reuse=True, command=command)[1]["reason_code"],
                         "EVIDENCE_SCOPE_MISMATCH")
        self.assertFalse((self.repo / "unexpected").exists())
        other = self.repo / "other-worktree"
        other.mkdir()
        self.assertEqual(self.bound_check(reuse=True, cwd=other)[1]["reason_code"],
                         "EVIDENCE_SCOPE_MISMATCH")

    def test_missing_or_changed_log_and_unfinished_receipt_are_unverified(self) -> None:
        self.scope.write_text("current", encoding="utf-8")
        self.assertEqual(self.bound_check()[0], 0)
        self.log.write_bytes(b"unexpected")
        self.assertEqual(self.bound_check(reuse=True)[1]["reason_code"], "LOG_UNVERIFIED")
        self.log.unlink()
        self.assertEqual(self.bound_check(reuse=True)[1]["reason_code"], "LOG_UNVERIFIED")
        self.receipt.write_bytes(b'{"status":"PASS"')
        self.assertEqual(self.bound_check(reuse=True)[1]["reason_code"], "RECEIPT_UNREADABLE")

    def test_failed_receipt_preserves_failure_without_reexecuting(self) -> None:
        self.scope.write_text("current", encoding="utf-8")
        command = (sys.executable, "-c", "from pathlib import Path; "
                   "Path('once').touch(exist_ok=False); raise SystemExit(7)")
        self.assertEqual(self.bound_check(command=command)[0], 7)
        code, summary = self.bound_check(reuse=True, command=command)
        self.assertEqual((code, summary["status"], summary["returncode"], summary["reused"]),
                         (7, "FAIL", 7, True))

    def test_scope_changed_during_command_does_not_publish_a_pass(self) -> None:
        self.scope.write_text("before", encoding="utf-8")
        command = (sys.executable, "-c", "from pathlib import Path; import sys; "
                   "Path(sys.argv[1]).write_bytes(b'after')", str(self.scope))
        code, summary = self.bound_check(command=command)
        self.assertEqual((code, summary["reason_code"]), (2, "SCOPE_CHANGED"))
        self.assertEqual(summary["check_result"]["returncode"], 0)
        self.assertEqual(self.bound_check(reuse=True, command=command)[1]["reason_code"],
                         "RECEIPT_UNREADABLE")

    @unittest.skipIf(os.name == "nt", "POSIX child signal exit status")
    def test_signal_failure_preserves_native_status(self) -> None:
        code, summary = self.run_check(
            sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual((code, summary["status"], summary["returncode"], summary["exit_code"]),
                         (143, "FAIL", -signal.SIGTERM, 143))


if __name__ == "__main__":
    unittest.main()
