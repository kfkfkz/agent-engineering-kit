#!/usr/bin/env python3
"""Context ledger contracts: exact bytes, fail-closed recovery, partial coverage."""
from __future__ import annotations

import hashlib
import tempfile
import threading
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aek.adapters.telemetry import ContextLedger, LedgerInvalid
from aek.core.context.telemetry import ContextEventInput, build_context_report


def event(channel: str = "aek_skill", content: str = "材料") -> ContextEventInput:
    encoded = content.encode("utf-8")
    return ContextEventInput(
        route="bounded", stage="routing", source_kind="reference",
        channel=channel, source_digest=hashlib.sha256(encoded).hexdigest(),
        delivered_bytes=len(encoded), delivered_chars=len(content),
        logical_bytes=len(encoded), purpose="route_required", cache_hit=False,
        session_id="test-session", subject_digest="a" * 64,
        logical_path="skills/repo-delivery/SKILL.md",
        occurred_at="2026-09-20T00:00:00Z", note="",
        duplicate_delivery=False,
    )


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "context-ledger.jsonl"
        self.ledger = ContextLedger(self.path)

    def test_exact_bytes_and_rebuild(self) -> None:
        first = self.ledger.append(event())
        second = self.ledger.append(event(content="abc"))
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(second.previous_hash, first.event_hash)
        loaded = self.ledger.read()
        self.assertEqual(loaded, (first, second))
        report = build_context_report(
            loaded, coverage="complete", expected_channels=("aek_skill",),
            observed_channels=("aek_skill",), bypass_detected=False,
        )
        self.assertEqual(report.delivered_bytes, 9)
        self.assertEqual(report.delivered_chars, 5)
        self.assertEqual(report.coverage, "complete")
        self.assertEqual(report.findings, ())
        self.assertEqual(report.stream_digest, second.event_hash)
        self.assertEqual(first.session_id, "test-session")
        self.assertEqual(first.subject_digest, "a" * 64)
        self.assertEqual(first.logical_path, "skills/repo-delivery/SKILL.md")
        self.assertEqual(len(first.event_id), 64)
        self.assertIn(("reference", 9, 5, 2), report.by_source_kind)

    def test_instrumented_text_api_never_persists_content(self) -> None:
        row = self.ledger.append_text(
            "机密材料", route="bounded", stage="routing",
            source_kind="reference", channel="aek_skill",
            purpose="route_required", cache_hit=False,
            session_id="test-session", subject_digest="a" * 64,
            logical_path="skills/repo-delivery/SKILL.md",
            occurred_at="2026-09-20T00:00:00Z",
        )
        self.assertEqual(row.delivered_bytes, len("机密材料".encode("utf-8")))
        self.assertEqual(row.delivered_chars, len("机密材料"))
        self.assertNotIn("机密材料", self.path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            self.ledger.append_text(
                "password=abc", route="bounded", stage="routing",
                source_kind="reference", channel="aek_skill",
                purpose="arbitrary", cache_hit=False,
                session_id="test-session", subject_digest="a" * 64,
                logical_path="skills/repo-delivery/SKILL.md",
                occurred_at="2026-09-20T00:00:00Z",
            )

    def test_event_rejects_unsafe_source_identity(self) -> None:
        base = event().__dict__
        for changed in ({"logical_path": "../secret"},
                        {"subject_digest": "wrong"},
                        {"note": "password=secret"}):
            with self.assertRaises(ValueError):
                ContextEventInput(**{**base, **changed})

    def test_partial_cannot_claim_complete(self) -> None:
        self.ledger.append(event())
        report = build_context_report(
            self.ledger.read(), coverage="partial",
            expected_channels=("aek_skill", "host_tool"),
            observed_channels=("aek_skill",), bypass_detected=False,
        )
        self.assertEqual(report.coverage, "partial")
        self.assertEqual(report.unobserved_channels, ("host_tool",))
        with self.assertRaises(ValueError):
            build_context_report(
                self.ledger.read(), coverage="complete",
                expected_channels=("aek_skill", "host_tool"),
                observed_channels=("aek_skill",), bypass_detected=False,
            )
        with self.assertRaises(ValueError):
            build_context_report(
                self.ledger.read(), coverage="complete",
                expected_channels=("aek_skill",),
                observed_channels=("aek_skill",), bypass_detected=True,
            )
        with self.assertRaises(ValueError):
            build_context_report(
                self.ledger.read(), coverage="complete",
                expected_channels=("aek_skill",),
                observed_channels=("aek_skill", "unknown_channel"),
                bypass_detected=False,
            )

    def test_tail_recovery_only(self) -> None:
        first = self.ledger.append(event())
        with self.path.open("ab") as stream:
            stream.write(b'{"schema_version":1,"sequence":2')
        second = self.ledger.append(event(content="safe"))
        self.assertEqual(second.sequence, 2)
        self.assertEqual(second.previous_hash, first.event_hash)
        self.assertEqual(len(self.ledger.read()), 2)

    def test_read_recovers_incomplete_tail_before_reporting(self) -> None:
        self.ledger.append(event())
        with self.path.open("ab") as stream:
            stream.write(b'{"schema_version":1')
        self.assertEqual(len(self.ledger.read()), 1)
        self.assertEqual(self.path.read_bytes()[-1:], b"\n")

    def test_middle_corruption_is_invalid_and_not_overwritten(self) -> None:
        self.ledger.append(event())
        self.ledger.append(event(content="safe"))
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b'"sequence":1', b'"sequence":9', 1))
        damaged = self.path.read_bytes()
        with self.assertRaises(LedgerInvalid):
            self.ledger.read()
        with self.assertRaises(LedgerInvalid):
            self.ledger.append(event())
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_duplicate_json_key_is_not_accepted_as_same_event(self) -> None:
        self.ledger.append(event())
        committed = self.path.read_bytes()
        self.path.write_bytes(committed.replace(
            b'"sequence":1', b'"sequence":9,"sequence":1', 1))
        with self.assertRaises(LedgerInvalid):
            self.ledger.read()

    def test_concurrent_append_has_no_sequence_gap(self) -> None:
        errors: list[Exception] = []

        def append() -> None:
            try:
                self.ledger.append(event(content="x"))
            except Exception as exc:  # collect thread failures for parent assertion
                errors.append(exc)

        threads = [threading.Thread(target=append) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(
            tuple(item.sequence for item in self.ledger.read()),
            tuple(range(1, 13)),
        )

    def test_cross_process_append_has_one_hash_chain(self) -> None:
        root = Path(__file__).resolve().parent.parent
        worker = r'''
import sys
from pathlib import Path
from aek.adapters.telemetry import ContextLedger

ledger = ContextLedger(Path(sys.argv[1]))
for _ in range(8):
    ledger.append_text(
        "x", route="bounded", stage="execution", source_kind="code",
        channel="code_loader", purpose="target_evidence", cache_hit=False,
        session_id="process-capacity", subject_digest="b" * 64,
        logical_path="src/code.py", occurred_at="2026-09-20T00:00:00Z")
'''
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        children = [subprocess.Popen(
            [sys.executable, "-c", worker, str(self.path)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)]
        failures = []
        for child in children:
            _stdout, stderr = child.communicate(timeout=30)
            if child.returncode:
                failures.append(stderr)
        self.assertEqual(failures, [])
        rows = self.ledger.read()
        self.assertEqual(len(rows), 32)
        self.assertEqual(tuple(row.sequence for row in rows), tuple(range(1, 33)))

    def test_single_writer_append_reuses_validated_prefix(self) -> None:
        original = ContextLedger._read_rows
        calls = 0

        def counted(fd):
            nonlocal calls
            calls += 1
            return original(fd)

        with patch.object(ContextLedger, "_read_rows", staticmethod(counted)):
            for _ in range(128):
                self.ledger.append(event(content="x"))
        self.assertLessEqual(calls, 2, "append rescanned the whole stream per event")
        self.assertEqual(len(self.ledger.read()), 128)

    def test_external_rewrite_invalidates_append_cache(self) -> None:
        self.ledger.append(event())
        committed = self.path.read_bytes()
        self.path.write_bytes(committed.replace(b'"sequence":1', b'"sequence":9', 1))
        with self.assertRaises(LedgerInvalid):
            self.ledger.append(event(content="x"))

    def test_final_symlink_is_not_followed(self) -> None:
        target = Path(self.tmp.name) / "user-owned.txt"
        target.write_text("keep", encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        with self.assertRaises((OSError, LedgerInvalid)):
            self.ledger.append(event())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
