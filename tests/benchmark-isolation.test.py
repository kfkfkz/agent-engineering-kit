"""Isolation diagnostics must distinguish probing from live-run readiness."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))


class IsolationProbeTests(unittest.TestCase):
    def test_isolated_execution_rejects_bad_pin_unowned_or_overlapping_directories(self):
        from adapters.isolation import run_isolated_process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = tuple(root / name for name in ("workspace", "home", "cache", "session"))
            for path in paths:
                path.mkdir()
            backend = root / "unexecuted-backend"
            backend.write_bytes(b"input validation fixture")
            with self.assertRaises(ValueError):
                run_isolated_process(("/usr/bin/true",), root, *paths,
                                     backend=backend, backend_sha256="0" * 64, timeout_seconds=3)
            with self.assertRaises(ValueError):
                run_isolated_process(("/usr/bin/true",), root, paths[0], paths[0], paths[2], paths[3],
                                     backend=backend, backend_sha256="0" * 64, timeout_seconds=3)
            with self.assertRaises(ValueError):
                run_isolated_process(("/usr/bin/true",), root, paths[0], root.parent, paths[2], paths[3],
                                     backend=backend, backend_sha256="0" * 64, timeout_seconds=3)

    def test_execution_uses_closed_namespace_and_redacts_process_bodies(self):
        from adapters.isolation import run_isolated_process
        from adapters.process import ProcessResult
        if sys.platform != "linux":
            with self.assertRaisesRegex(ValueError, "PLATFORM_UNSUPPORTED"):
                run_isolated_process(("/usr/bin/true",), Path("."), *(Path(".") for _ in range(4)),
                                     backend_sha256="0" * 64, timeout_seconds=3)
            return
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = tuple(root / name for name in ("workspace", "home", "cache", "session"))
            for path in paths:
                path.mkdir()
            backend = root / "trusted-backend"
            backend.write_bytes(b"explicit process seam")
            process = ProcessResult(0, "PROCESS_COMPLETED", b"private-canary", b"", "a" * 64, "b" * 64, 0.1)
            with patch("adapters.isolation.run_process", return_value=process) as launch:
                result = run_isolated_process(("/usr/bin/true",), root, *paths, backend=backend,
                                              backend_sha256=hashlib.sha256(backend.read_bytes()).hexdigest(), timeout_seconds=3)
            command = launch.call_args.args[0]
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertNotIn("--share-net", command)
            self.assertEqual(launch.call_args.kwargs["environment"], {"PATH": "/usr/bin:/bin"})
            self.assertNotIn("private-canary", repr(result))
            self.assertRegex(result.policy_sha256, r"^[a-f0-9]{64}$")

    def test_missing_backend_fails_closed_without_model_calls(self):
        from adapters.isolation import probe_linux_isolation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-backend"
            report = probe_linux_isolation(path)
            self.assertEqual(report["status"], "UNAVAILABLE")
            self.assertFalse(report["live_ready"])
            self.assertEqual(report["model_calls"], 0)
            self.assertNotIn(str(path), json.dumps(report))

    def test_probe_requires_all_checks_and_records_only_bounded_evidence(self):
        from adapters.isolation import probe_linux_isolation
        from adapters.process import ProcessResult
        if sys.platform != "linux":
            report = probe_linux_isolation()
            self.assertEqual(report["status"], "UNAVAILABLE")
            self.assertEqual(report["reason_code"], "PLATFORM_NOT_SUPPORTED")
            return
        checks = {"external_file_unreadable": True, "symlink_escape_blocked": True,
                  "parent_environment_absent": True, "host_network_unreachable": True}
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory) / "trusted-test-backend"
            backend.write_bytes(b"explicit trusted process seam")
            result = ProcessResult(0, "PROCESS_COMPLETED", json.dumps(checks).encode(),
                                   b"", "a" * 64, "b" * 64, 0.1)
            with patch("adapters.isolation.run_process", return_value=result) as process:
                report = probe_linux_isolation(backend)
            self.assertEqual(report["status"], "PROBED")
            self.assertFalse(report["live_ready"])
            self.assertEqual(report["checks"], checks)
            command = process.call_args.args[0]
            self.assertIn("--unshare-all", command)
            self.assertIn("--unshare-user", command)
            self.assertIn("--clearenv", command)
            self.assertIn("--new-session", command)
            self.assertNotIn("--share-net", command)
            self.assertNotIn(str(backend), json.dumps(report))
            checks["external_file_unreadable"] = False
            bad = ProcessResult(0, "PROCESS_COMPLETED", json.dumps(checks).encode(),
                                b"private-canary", "a" * 64, "b" * 64, 0.1)
            with patch("adapters.isolation.run_process", return_value=bad):
                failed = probe_linux_isolation(backend)
            self.assertEqual(failed["status"], "FAILED")
            self.assertNotIn("private-canary", json.dumps(failed))

    def test_isolation_cli_discloses_unavailable_backend_without_private_path(self):
        import subprocess
        entry = Path(__file__).resolve().parent / "benchmark" / "run.py"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(entry), "isolation-doctor", "--backend",
                                     str(Path(directory) / "missing")], cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "UNAVAILABLE")
            self.assertFalse(report["live_ready"])
            self.assertNotIn(directory, result.stdout)

    def test_nonregular_backend_is_rejected_without_blocking(self):
        import subprocess
        if sys.platform != "linux":
            from adapters.isolation import probe_linux_isolation
            self.assertEqual(probe_linux_isolation()["reason_code"], "PLATFORM_NOT_SUPPORTED")
            return
        entry = Path(__file__).resolve().parent / "benchmark" / "run.py"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backend-fifo"
            os.mkfifo(path)
            result = subprocess.run([sys.executable, str(entry), "isolation-doctor", "--backend", str(path)],
                                    cwd=directory, capture_output=True, text=True, timeout=2)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
