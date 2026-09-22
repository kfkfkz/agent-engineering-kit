#!/usr/bin/env python3
"""Fault-injected plan-sidecar publication and recovery."""
from __future__ import annotations

import tempfile
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
        original_open = plan_transaction.os.open
        fake_binary = 1 << 29
        observed: list[tuple[str, int]] = []

        def recording_open(path, flags, *args, **kwargs):
            observed.append((str(path), flags))
            return original_open(path, flags & ~fake_binary, *args, **kwargs)

        with mock.patch.object(plan_transaction, "_BINARY", fake_binary), \
                mock.patch.object(plan_transaction.os, "open", recording_open):
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
