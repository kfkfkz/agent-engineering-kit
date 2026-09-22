#!/usr/bin/env python3
"""Evidence references bind exact repository bytes without path escape."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from aek.adapters.evidence import EvidenceRef, EvidenceInvalid, read_verified_evidence


class EvidenceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        (self.root / "src").mkdir()
        self.source = self.root / "src" / "query.sql"
        self.source.write_bytes(b"select id from customer where id = ?\n")
        self.subject = "a" * 64

    def ref(self, **changes: object) -> EvidenceRef:
        payload = dict(
            logical_path="src/query.sql",
            source_digest=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            subject_digest=self.subject,
            producer_id="sql_config_scanner", source_kind="repo_file",
            summary_code="SOURCE_INSPECTED",
        )
        payload.update(changes)
        return EvidenceRef(**payload)

    def test_exact_current_bytes_are_returned_with_metadata_only_receipt(self) -> None:
        verified, content = read_verified_evidence(
            self.root, self.ref(), expected_subject_digest=self.subject)
        self.assertEqual(content, self.source.read_bytes())
        self.assertEqual(verified.logical_path, "src/query.sql")
        self.assertEqual(verified.source_digest,
                         hashlib.sha256(content).hexdigest())
        self.assertNotIn("select", repr(verified))

    def test_stale_subject_or_source_digest_is_rejected(self) -> None:
        with self.assertRaises(EvidenceInvalid):
            read_verified_evidence(
                self.root, self.ref(), expected_subject_digest="b" * 64)
        original_ref = self.ref()
        self.source.write_bytes(b"changed")
        with self.assertRaises(EvidenceInvalid):
            read_verified_evidence(
                self.root, original_ref,
                expected_subject_digest=self.subject)

    def test_traversal_symlink_and_secret_summary_are_rejected(self) -> None:
        for path in ("../outside", "/etc/passwd", "src/../../outside",
                     "C:/Windows/win.ini", "src\\query.sql"):
            with self.assertRaises(EvidenceInvalid):
                read_verified_evidence(
                    self.root, self.ref(logical_path=path),
                    expected_subject_digest=self.subject)
        outside = Path(self.tmp.name) / "outside.sql"
        outside.write_bytes(b"private")
        try:
            (self.root / "src" / "linked.sql").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(EvidenceInvalid):
            read_verified_evidence(
                self.root, self.ref(logical_path="src/linked.sql",
                                    source_digest=hashlib.sha256(b"private").hexdigest()),
                expected_subject_digest=self.subject)
        outside_dir = Path(self.tmp.name) / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "query.sql").write_bytes(b"private")
        (self.root / "alias").symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises(EvidenceInvalid):
            read_verified_evidence(
                self.root, self.ref(logical_path="alias/query.sql",
                                    source_digest=hashlib.sha256(b"private").hexdigest()),
                expected_subject_digest=self.subject)
        with self.assertRaises(EvidenceInvalid):
            read_verified_evidence(
                self.root, self.ref(summary_code="password=secret"),
                expected_subject_digest=self.subject)

    def test_non_regular_source_is_rejected_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs unavailable")
        fifo = self.root / "src" / "queue"
        os.mkfifo(fifo)
        with self.assertRaises(EvidenceInvalid):
            read_verified_evidence(
                self.root, self.ref(logical_path="src/queue"),
                expected_subject_digest=self.subject)


if __name__ == "__main__":
    unittest.main()
