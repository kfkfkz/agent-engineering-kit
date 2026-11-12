#!/usr/bin/env python3
"""Fault-injected plan-sidecar publication and recovery."""
from __future__ import annotations

import tempfile
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

import aek.adapters.plan_transaction as plan_transaction
from aek.adapters.plan_transaction import PlanStore, PlanConflict, PlanInvalid


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reviews = Path(self.tmp.name) / "reviews"
        self.reviews.mkdir()
        self.store = PlanStore(self.reviews)
        self.plan = self.reviews / "artifact-plan.json"
        self.credential = self.reviews / "artifact-plan.gate.json"

    def test_closeout_transaction_recovers_all_boundaries_and_refuses_source_drift(self):
        source = self.reviews.parent / "任务清单.md"
        source.write_bytes("完成事实。\n".encode("utf-8"))
        gate = b'{"stage":"plan","status":"PASS"}'
        (self.reviews / "计划.gate.json").write_bytes(gate)
        target = self.reviews / "计划.closeout.json"
        for cut in range(4):
            with self.subTest(cut=cut):
                if target.exists():
                    target.unlink()
                with self.store.locked_view(source.parent) as view:
                    context = {"package_ref": source.parent.as_posix(), "source_root": view.source_identity,
                               "policy_digest": "a" * 64, "source_set": [source.name]}
                    epoch = {"stage": "计划", "context": context,
                             "source_hashes": {source.name: hashlib.sha256(source.read_bytes()).hexdigest()},
                             "dependency_hashes": {}}
                    content = json.dumps({"stage": "计划", "status": "PASS", "kind": "execution_closeout",
                        "doc_hashes": epoch["source_hashes"], "plan_gate_sha256": hashlib.sha256(gate).hexdigest()},
                        ensure_ascii=False).encode("utf-8")
                    with self.assertRaises(RuntimeError):
                        view.commit_closeout(content, expected_consumers=view.consumer_digest, epoch=epoch,
                                             context_loader=lambda stage: context, fault_after=cut)
                if cut < 3:
                    with self.assertRaises(PlanInvalid):
                        self.store.recover()  # No generic recovery without actual source checks.
                    original = source.read_bytes()
                    source.write_bytes(original + b"changed")
                    with self.store.locked_view(source.parent) as view, self.assertRaises(PlanInvalid):
                        view.recover_closeout(lambda stage: context)
                    self.assertTrue((self.reviews / "artifact-plan.tx.json").exists())
                    source.write_bytes(original)
                with self.store.locked_view(source.parent) as view:
                    view.recover_closeout(lambda stage: context)
                    self.assertEqual(view.read_closeout(), content)
                self.assertFalse((self.reviews / "artifact-plan.tx.json").exists())
        with self.assertRaises(ValueError):
            self.store.publish({target.name: b"arbitrary"}, expected_consumers=self.store.consumer_digest())

    def test_clean_publish_and_rollback(self) -> None:
        baseline = self.store.consumer_digest()
        self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                            "artifact-plan.gate.json": b'{"schema_version":1}'},
                           expected_consumers=baseline)
        self.assertTrue(self.plan.is_file())
        self.assertTrue(self.credential.is_file())
        self.assertFalse((self.reviews / "artifact-plan.tx.json").exists())
        self.store.rollback_all(expected_consumers=self.store.consumer_digest())
        self.assertFalse(self.plan.exists())
        self.assertFalse(self.credential.exists())

    @unittest.skipIf(os.name == "nt", "POSIX ancestor substitution; native reparse tests cover Windows")
    def test_symlink_ancestor_is_rejected_without_writing_outside(self) -> None:
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.reviews.parent, target_is_directory=True)
        with self.assertRaises(PlanInvalid):
            PlanStore(alias / "reviews").consumer_digest()
        self.assertFalse((self.reviews / ".artifact-plan.lock").exists())

    def test_locked_material_view_reads_the_source_and_expires_on_exit(self) -> None:
        source = self.reviews.parent / "需求分析.md"
        raw = "确切原文。\r\n".encode("utf-8")
        source.write_bytes(raw)
        expected = self.store.consumer_digest()
        with self.store.locked_view(self.reviews.parent) as view:
            self.assertEqual(view.read_source(source.name), raw)
            self.assertEqual(view.snapshot(), {})
            self.assertEqual(view.consumer_digest, expected)
            self.assertTrue(view.source_identity)
            for name in ("../outside", "/需求分析.md", "unknown.md", ".artifact-plan.lock"):
                with self.assertRaises(ValueError):
                    view.read_source(name)
        for read in (lambda: view.read_source(source.name), view.snapshot,
                     lambda: view.consumer_digest, lambda: view.source_identity):
            with self.assertRaises(PlanInvalid):
                read()
        self.assertEqual(source.read_bytes(), raw)

    def test_locked_view_can_publish_with_original_cas_without_reacquiring_lock(self) -> None:
        # A regression to re-locking must fail with a deadline, not hang CI.
        program = """import sys
from pathlib import Path
from aek.adapters.plan_transaction import PlanStore, PlanConflict, PlanInvalid
store=PlanStore(Path(sys.argv[1]))
with store.locked_view(store.reviews.parent) as view:
    expected=view.consumer_digest
    view.publish({'artifact-plan.json':b'original plan'},expected_consumers=expected)
    assert view.snapshot()['artifact-plan.json']==b'original plan'
    try:
        view.publish({'artifact-plan.json':b'stale overwrite'},expected_consumers=expected)
    except PlanConflict:
        pass
    else:
        raise AssertionError('stale CAS accepted')
    try:
        view.publish({'需求分析.gate.json':b'unauthorized Gate'},expected_consumers=view.consumer_digest)
    except ValueError:
        pass
    else:
        raise AssertionError('Gate writable through the plan API')
try:
    view.publish({'artifact-plan.json':b'expired'},expected_consumers=expected)
except PlanInvalid:
    pass
else:
    raise AssertionError('expired view accepted')
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(self.reviews)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.plan.read_bytes(), b"original plan")
        self.assertFalse((self.reviews / "需求分析.gate.json").exists())

    def test_locked_source_view_uses_the_document_limit_not_the_sidecar_limit(self) -> None:
        source = self.reviews.parent / "需求分析.md"
        raw = b"x" * (2 * 1024 * 1024)
        source.write_bytes(raw)
        with self.store.locked_view(self.reviews.parent) as view:
            self.assertEqual(view.read_source(source.name), raw)
            source.write_bytes(raw + b"x")
            with self.assertRaises(ValueError):
                view.read_source(source.name)

    @unittest.skipIf(os.name == "nt", "POSIX rename window; Windows ancestor handles prohibit substitution")
    def test_snapshot_cannot_switch_to_a_replaced_reviews_directory(self) -> None:
        self.plan.write_bytes(b"original approved plan")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / self.plan.name).write_bytes(b"unrelated plan")
        original_open = os.open
        replaced = False

        def substitute_at_read(path, flags, *args, **kwargs):
            nonlocal replaced
            if str(path).endswith("artifact-plan.json") and not replaced:
                replaced = True
                self.reviews.rename(self.reviews.with_name("pinned-reviews"))
                self.reviews.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        # The wrapper forwards dir_fd unchanged; preserve the platform's
        # capability signal instead of making ui_filesystem see a mock API.
        with mock.patch.object(os, "open", substitute_at_read), \
                mock.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {substitute_at_read}):
            try:
                snapshot = self.store.snapshot()
            except PlanInvalid:
                snapshot = None  # Refusing the changed root is also safe.
        self.assertTrue(replaced)
        if snapshot is not None:
            self.assertEqual(snapshot["artifact-plan.json"], b"original approved plan")
        self.assertEqual((outside / self.plan.name).read_bytes(), b"unrelated plan")

    @unittest.skipIf(os.name == "nt", "POSIX rename window; Windows ancestor handles prohibit substitution")
    def test_publication_and_recovery_keep_the_locked_parent_directory(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / self.plan.name).write_bytes(b"unrelated plan")
        pinned = self.reviews.with_name("pinned-reviews")
        expected = self.store.consumer_digest()
        original_replace = os.replace
        replaced = False

        def substitute_at_publish(source, target, *args, **kwargs):
            nonlocal replaced
            if str(target).endswith("artifact-plan.tx.json") and not replaced:
                replaced = True
                self.reviews.rename(pinned)
                self.reviews.symlink_to(outside, target_is_directory=True)
            return original_replace(source, target, *args, **kwargs)

        with mock.patch.object(os, "replace", substitute_at_publish):
            self.store.publish({"artifact-plan.json": b"approved plan",
                                "artifact-plan.gate.json": b"approved credential"},
                               expected_consumers=expected)
        self.assertTrue(replaced)
        self.assertEqual((pinned / self.plan.name).read_bytes(), b"approved plan")
        self.assertEqual((pinned / self.credential.name).read_bytes(), b"approved credential")
        self.assertFalse((pinned / "artifact-plan.tx.json").exists())
        self.assertEqual(sorted(p.name for p in outside.iterdir()), [self.plan.name])
        self.assertEqual((outside / self.plan.name).read_bytes(), b"unrelated plan")


    def test_crash_before_first_publish_discards_prepared(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=0)
        self.assertFalse(self.plan.exists())
        self.assertEqual(PlanStore(self.reviews).recover(), "ROLLED_BACK")
        self.assertFalse((self.reviews / "artifact-plan.tx.json").exists())

    def test_partial_publish_rolls_forward(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                                "artifact-plan.gate.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=1)
        self.assertTrue(self.plan.exists())
        self.assertFalse(self.credential.exists())
        self.assertEqual(PlanStore(self.reviews).recover(), "ROLLED_FORWARD")
        self.assertTrue(self.credential.exists())

    def test_noop_entry_does_not_fake_partial_publish(self) -> None:
        self.plan.write_bytes(b'{"schema_version":1}')
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                                "artifact-plan.gate.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=0)
        self.assertEqual(PlanStore(self.reviews).recover(), "ROLLED_BACK")
        self.assertFalse(self.credential.exists())

    def test_external_conflict_retains_evidence(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                                "artifact-plan.gate.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=1)
        self.credential.write_bytes(b'user-owned')
        with self.assertRaises(PlanInvalid):
            PlanStore(self.reviews).recover()
        self.assertEqual(self.credential.read_bytes(), b'user-owned')
        self.assertTrue((self.reviews / "artifact-plan.tx.json").exists())

    def test_other_consumer_change_during_recovery_is_invalid(self) -> None:
        (self.reviews / "需求分析.gate.json").write_bytes(b'first')
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                                "artifact-plan.gate.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=1)
        (self.reviews / "需求分析.gate.json").write_bytes(b'second')
        with self.assertRaises(PlanInvalid):
            PlanStore(self.reviews).recover()
        self.assertFalse(self.credential.exists())

    def test_rollback_all_partial_crash_rolls_forward(self) -> None:
        self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                            "artifact-plan.gate.json": b'{"schema_version":1}'},
                           expected_consumers=self.store.consumer_digest())
        with self.assertRaises(RuntimeError):
            self.store.rollback_all(expected_consumers=self.store.consumer_digest(),
                                    fault_after=1)
        self.assertEqual(PlanStore(self.reviews).recover(), "ROLLED_FORWARD")
        self.assertFalse(self.plan.exists())
        self.assertFalse(self.credential.exists())

    def test_corrupt_journal_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=0)
        journal = self.reviews / "artifact-plan.tx.json"
        journal.write_bytes(b'{"schema_version":')
        with self.assertRaises(PlanInvalid):
            PlanStore(self.reviews).recover()
        self.assertEqual(journal.read_bytes(), b'{"schema_version":')

    def test_consumer_cas_conflict(self) -> None:
        before = self.store.consumer_digest()
        (self.reviews / "需求分析.gate.json").write_bytes(b'changed')
        with self.assertRaises(PlanConflict):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}'},
                               expected_consumers=before)
        self.assertFalse(self.plan.exists())

    def test_unsafe_filename_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.publish({"../outside": b'x'},
                               expected_consumers=self.store.consumer_digest())

    def test_snapshot_recovers_before_reading_partial_publication(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                                "artifact-plan.gate.json": b'{"schema_version":1}'},
                               expected_consumers=self.store.consumer_digest(),
                               fault_after=1)
        snapshot = PlanStore(self.reviews).snapshot()
        self.assertEqual(snapshot["artifact-plan.json"], b'{"schema_version":1}')
        self.assertEqual(snapshot["artifact-plan.gate.json"], b'{"schema_version":1}')
        self.assertFalse((self.reviews / "artifact-plan.tx.json").exists())

    def test_snapshot_rejects_orphan_publish_temp(self) -> None:
        (self.reviews / ".artifact-plan-write-orphan").write_bytes(b'incomplete')
        with self.assertRaises(PlanInvalid):
            self.store.snapshot()

    def test_snapshot_and_consumer_cas_are_one_view(self) -> None:
        snapshot, digest = self.store.snapshot_with_digest()
        self.assertEqual(snapshot, {})
        (self.reviews / "需求分析.gate.json").write_bytes(b'concurrent gate')
        with self.assertRaises(PlanConflict):
            self.store.publish({"artifact-plan.json": b'new'},
                               expected_consumers=digest)
        self.assertFalse(self.plan.exists())

    def test_sidecar_and_lock_descriptors_always_request_binary_mode(self) -> None:
        """Windows text descriptors translate CRLF and invalidate size checks."""
        gate = self.reviews / "需求分析.gate.json"
        gate.write_bytes(b'{\r\n  "status": "PASS"\r\n}\r\n')
        original_open = plan_transaction.fs.open_file
        fake_binary = 1 << 29
        observed: list[tuple[str, int]] = []

        def recording_open(directory, name, flags, *args, **kwargs):
            observed.append((name, flags))
            native_flags = (flags & ~fake_binary) | getattr(os, "O_BINARY", 0)
            return original_open(directory, name, native_flags, *args, **kwargs)

        with mock.patch.object(plan_transaction, "_BINARY", fake_binary), \
                mock.patch.object(plan_transaction.fs, "open_file", recording_open):
            snapshot = self.store.snapshot()
        self.assertEqual(snapshot, {})  # legacy gates affect CAS but stay internal
        relevant = [(path, flags) for path, flags in observed
                    if path.endswith((".artifact-plan.lock", gate.name))]
        self.assertTrue(relevant)
        self.assertTrue(all(flags & fake_binary for _path, flags in relevant))

    def test_rollback_retry_recovers_even_when_all_targets_are_absent(self) -> None:
        self.store.publish({"artifact-plan.json": b'{"schema_version":1}',
                            "artifact-plan.gate.json": b'{"schema_version":1}'},
                           expected_consumers=self.store.consumer_digest())
        expected = self.store.consumer_digest()
        with self.assertRaises(RuntimeError):
            self.store.rollback_all(expected_consumers=expected, fault_after=2)
        self.assertFalse(self.plan.exists())
        self.assertFalse(self.credential.exists())
        self.assertTrue((self.reviews / "artifact-plan.tx.json").exists())
        PlanStore(self.reviews).rollback_all(expected_consumers=expected)
        self.assertFalse((self.reviews / "artifact-plan.tx.json").exists())


if __name__ == "__main__":
    unittest.main()
