#!/usr/bin/env python3
"""The installer and release artifact use one durable semantic version."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import installer.registry as registry


class VersionTests(unittest.TestCase):
    def test_version_file_is_the_installer_authority(self) -> None:
        root = Path(__file__).resolve().parent.parent
        expected = (root / "VERSION").read_text(encoding="ascii").strip()
        self.assertRegex(expected, r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")
        with patch.object(registry, "_KIT_VERSION", None):
            self.assertEqual(registry.current_kit_version(), expected)
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"当前代码版本为 **{expected}**", readme)
        self.assertIn(f"git clone --branch v{expected} --depth 1", readme)
        self.assertIn(f"## {expected} —", (root / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_valid_version_file_is_used_without_git_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="ascii")
            with patch.object(registry, "KIT_DIR", root), \
                    patch.object(registry, "_KIT_VERSION", None), \
                    patch.object(registry.subprocess, "run",
                                 side_effect=AssertionError("valid VERSION must not invoke git")):
                self.assertEqual(registry.current_kit_version(), "9.8.7")


if __name__ == "__main__":
    unittest.main()
