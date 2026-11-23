"""The compact suite runner retains all failure locations and original output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class SuiteSummaryTests(unittest.TestCase):
    def test_summary_retains_failures_and_refuses_to_overwrite_a_log(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            tests = repo / "tests"
            tests.mkdir()
            runner = tests / "run-python-tests.py"
            shutil.copyfile(Path(__file__).with_name(runner.name), runner)
            for name, code in (("pass", 0), ("failure", 3), ("error", 7)):
                (tests / f"{name}.test.py").write_text(
                    f"import sys\nprint({name!r}, file=sys.stderr)\nsys.exit({code})\n",
                    encoding="utf-8")
            path = repo / "full log.txt"
            command = [sys.executable, "-X", "utf8", str(runner), "--summary-log", str(path)]
            result = subprocess.run(command, capture_output=True, check=False, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            summary = json.loads(result.stdout)
            raw = path.read_bytes()
            self.assertEqual(summary, {
                "status": "FAIL", "passed": 1,
                "failures": [{"suite": "tests/error.test.py", "exit_code": 7},
                             {"suite": "tests/failure.test.py", "exit_code": 3}],
                "full_result_ref": str(path),
                "result_digest": hashlib.sha256(raw).hexdigest(),
            })
            self.assertIn(b"\nerror", raw)
            self.assertIn(b"\nfailure", raw)
            repeated = subprocess.run(command, capture_output=True, check=False, timeout=20)
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(path.read_bytes(), raw)

            scoped_path = repo / "selected.log"
            selected = subprocess.run(command[:-1] + [
                str(scoped_path), "--suite", "pass.test.py", "--suite", "failure.test.py",
            ], capture_output=True, check=False, timeout=20)
            self.assertEqual(selected.returncode, 1, selected.stderr)
            filtered = json.loads(selected.stdout)
            self.assertEqual(filtered["passed"], 1)
            self.assertEqual(filtered["failures"], [{"suite": "tests/failure.test.py", "exit_code": 3}])
            self.assertNotIn(b"error.test.py", scoped_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
