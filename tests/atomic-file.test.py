#!/usr/bin/env python3
"""Portable atomic sidecar publication used by review and recovery stores."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aek.adapters.atomic_file import AtomicFileInvalid, AtomicFileStore


class AtomicFileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = AtomicFileStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_write_read_replace_and_delete_are_idempotent(self) -> None:
        self.store.write("state.json", b"old")
        self.assertEqual(self.store.read("state.json"), b"old")
        self.store.write("state.json", b"new")
        self.assertEqual(self.store.read("state.json"), b"new")
        self.store.delete("state.json")
        self.store.delete("state.json")
        self.assertIsNone(self.store.read("state.json"))

    def test_publish_failure_keeps_old_bytes_and_cleans_temporary(self) -> None:
        self.store.write("state.json", b"old")
        with patch("aek.adapters.atomic_file.os.replace",
                   side_effect=OSError("injected before publish")):
            with self.assertRaises(OSError):
                self.store.write("state.json", b"new")
        self.assertEqual(self.store.read("state.json"), b"old")
        self.assertEqual(
            [item for item in self.root.iterdir()
             if item.name.startswith(".aek-atomic-")],
            [],
        )

    def test_fsync_failures_have_recoverable_old_or_new_terminal_state(self) -> None:
        self.store.write("state.json", b"old")
        with patch("aek.adapters.atomic_file.os.fsync",
                   side_effect=OSError("injected before replace")):
            with self.assertRaises(OSError):
                self.store.write("state.json", b"new")
        self.assertEqual(self.store.read("state.json"), b"old")
        self.assertEqual(
            [item for item in self.root.iterdir()
             if item.name.startswith(".aek-atomic-")], [])

        with patch("aek.adapters.atomic_file._sync_directory",
                   side_effect=OSError("injected after replace")):
            with self.assertRaises(OSError):
                self.store.write("state.json", b"new")
        # Publication happened atomically; recovery reads a complete new file.
        self.assertEqual(self.store.read("state.json"), b"new")
        self.assertEqual(
            [item for item in self.root.iterdir()
             if item.name.startswith(".aek-atomic-")], [])

    def test_rejects_escape_symlink_and_oversized_content(self) -> None:
        for name in ("../state.json", "nested/state.json", "", "."):
            with self.assertRaises((ValueError, AtomicFileInvalid)):
                self.store.write(name, b"x")
        if hasattr(os, "symlink"):
            target = self.root / "target"
            target.write_bytes(b"user")
            link = self.root / "state.json"
            try:
                link.symlink_to(target)
            except OSError:
                pass
            else:
                with self.assertRaises(AtomicFileInvalid):
                    self.store.write("state.json", b"managed")
                self.assertEqual(target.read_bytes(), b"user")
        with self.assertRaises(ValueError):
            AtomicFileStore(self.root, max_bytes=2).write("large", b"123")


if __name__ == "__main__":
    unittest.main()
