#!/usr/bin/env python3
"""Real safe-file archive behavior, not a mocked approval store."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from aek.adapters.document_material_store import MaterialArchive, MaterialCorrupt
from aek.adapters import ui_filesystem as fs


class MaterialArchiveTests(unittest.TestCase):
    def test_published_exact_bytes_are_readable_by_digest_and_retries_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "中文需求" / "reviews"
            root.mkdir(parents=True)
            archive = MaterialArchive(root)
            raw = "实际原始材料。\r\n".encode()
            sha = hashlib.sha256(raw).hexdigest()
            self.assertEqual(archive.put(raw, expected_sha256=sha), sha)
            self.assertEqual(archive.get(sha), raw)
            self.assertEqual(archive.put(raw, expected_sha256=sha), sha)
            self.assertEqual(archive.get(sha), raw)

    def test_missing_reads_are_read_only_and_bad_identities_do_not_create_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviews"
            root.mkdir()
            archive = MaterialArchive(root)
            with self.assertRaises(FileNotFoundError):
                archive.get("a" * 64)
            self.assertEqual(list(root.iterdir()), [])
            for bad in ("../outside", "a" * 63, "A" * 64, None):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        archive.put(b"material", expected_sha256=bad)
                    with self.assertRaises(ValueError):
                        archive.get(bad)
            with self.assertRaises(ValueError):
                archive.put(b"material", expected_sha256="a" * 64)
            self.assertEqual(list(root.iterdir()), [])

    def test_corrupted_existing_blob_is_not_overwritten_or_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviews"
            (root / "materials").mkdir(parents=True)
            archive = MaterialArchive(root)
            raw = b"original material\n"
            sha = hashlib.sha256(raw).hexdigest()
            target = root / "materials" / sha
            target.write_bytes(b"user replacement")
            with self.assertRaises(MaterialCorrupt):
                archive.get(sha)
            with self.assertRaises(MaterialCorrupt):
                archive.put(raw, expected_sha256=sha)
            self.assertEqual(target.read_bytes(), b"user replacement")

    @unittest.skipIf(os.name == "nt", "native reparse cases run in the shared filesystem suite")
    def test_symlink_leaf_and_parent_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviews"
            (root / "materials").mkdir(parents=True)
            raw = b"private material\n"
            sha = hashlib.sha256(raw).hexdigest()
            outside = Path(directory) / "outside"
            outside.write_bytes(raw)
            leaf = root / "materials" / sha
            leaf.symlink_to(outside)
            archive = MaterialArchive(root)
            for operation in (lambda: archive.get(sha), lambda: archive.put(raw, expected_sha256=sha)):
                with self.assertRaises(OSError):
                    operation()
            self.assertEqual(outside.read_bytes(), raw)
            leaf.unlink()
            (root / "materials").rmdir()
            (root / "materials").symlink_to(Path(directory), target_is_directory=True)
            with self.assertRaises(OSError):
                archive.put(raw, expected_sha256=sha)
            self.assertEqual(outside.read_bytes(), raw)

    def test_concurrent_processes_publish_one_exact_immutable_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviews"
            root.mkdir()
            script = (
                "import hashlib,sys; from pathlib import Path; "
                "from aek.adapters.document_material_store import MaterialArchive; "
                "raw=b'concurrent material\\n'; sha=hashlib.sha256(raw).hexdigest(); "
                "a=MaterialArchive(Path(sys.argv[1])); "
                "assert a.put(raw,expected_sha256=sha)==sha; assert a.get(sha)==raw")
            workers = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(root)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
            try:
                for worker in workers:
                    out, err = worker.communicate(timeout=20)
                    self.assertEqual(worker.returncode, 0, (out, err))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                    worker.communicate()
            names = [item.name for item in (root / "materials").iterdir()]
            self.assertEqual(names, [hashlib.sha256(b"concurrent material\n").hexdigest()])

    def test_short_or_failed_write_never_publishes_partial_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviews"
            root.mkdir()
            raw = b"material\n"
            sha = hashlib.sha256(raw).hexdigest()
            archive = MaterialArchive(root)
            for failure in (OSError("controlled write failure"), 0):
                with self.subTest(failure=failure):
                    patch = {"side_effect": failure} if isinstance(failure, OSError) else {"return_value": failure}
                    with mock.patch("aek.adapters.document_material_store.os.write", **patch):
                        with self.assertRaises(OSError):
                            archive.put(raw, expected_sha256=sha)
                    with self.assertRaises(FileNotFoundError):
                        archive.get(sha)
                    self.assertEqual(list((root / "materials").iterdir()), [])

    def test_competing_wrong_target_is_preserved_in_the_final_no_replace_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reviews"
            root.mkdir()
            raw = b"intended material\n"
            sha = hashlib.sha256(raw).hexdigest()
            target = root / "materials" / sha
            original = fs.rename_noreplace
            def competing_write(*args):
                target.write_bytes(b"competing user content")
                return original(*args)
            with mock.patch("aek.adapters.document_material_store.fs.rename_noreplace", side_effect=competing_write):
                with self.assertRaises(MaterialCorrupt):
                    MaterialArchive(root).put(raw, expected_sha256=sha)
            self.assertEqual(target.read_bytes(), b"competing user content")
            self.assertEqual([item.name for item in (root / "materials").iterdir()], [sha])


if __name__ == "__main__":
    unittest.main()
