#!/usr/bin/env python3
"""The installer and release artifact use one durable semantic version."""
from __future__ import annotations

import unittest
from pathlib import Path

import installer.registry as registry


class VersionTests(unittest.TestCase):
    def test_version_file_is_the_installer_authority(self) -> None:
        root = Path(__file__).resolve().parent.parent
        expected = (root / "VERSION").read_text(encoding="ascii").strip()
        registry._KIT_VERSION = None
        self.assertEqual(expected, "1.0.3")
        self.assertEqual(registry.current_kit_version(), expected)


if __name__ == "__main__":
    unittest.main()
