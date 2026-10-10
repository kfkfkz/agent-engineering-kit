"""Bad budget purposes fail before retrieval, without raw inputs or tracebacks."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MemoryContextInputTests(unittest.TestCase):
    def test_cli_rejects_freeform_purpose_before_creating_context_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            purpose = "private-canary 依赖迁移的影响面与历史约束"
            result = subprocess.run([sys.executable, str(ROOT / "memory-recall"), "dependency migration", str(repo),
                                     "--context-json", "--route", "standard", "--stage", "design", "--purpose", purpose,
                                     "--subject", "d" * 64, "--session", "purpose-test"],
                                    cwd=repo, capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("private-canary", result.stdout + result.stderr)
            self.assertIn("target_evidence", result.stderr)
            self.assertFalse((repo / ".repo-memory-kit").exists())

    def test_session_path_cannot_access_or_truncate_outside_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "docs").mkdir(parents=True)
            outside = root / "outside-dddddddddddd.jsonl"
            original = b'{"private":"owned-canary"}'  # No newline: ledger recovery used to truncate it.
            outside.write_bytes(original)
            result = subprocess.run([sys.executable, str(ROOT / "memory-recall"), "query", str(repo),
                                     "--context-json", "--route", "direct", "--subject", "d" * 64,
                                     "--session", "../../../outside"], cwd=repo,
                                    capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(outside.read_bytes(), original)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((outside.parent / (outside.name + ".lock")).exists())


if __name__ == "__main__":
    unittest.main()
