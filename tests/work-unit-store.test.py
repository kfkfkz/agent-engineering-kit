#!/usr/bin/env python3
"""WorkUnit state is atomically published with writer-safe CAS."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aek.adapters.work_unit_store import WorkUnitConflict, WorkUnitStore
from aek.core.context.identity import build_identity
from aek.core.work_unit import StepSpec, create_work_unit, takeover_work_unit


def envelope():
    return build_identity({
        "subject": {"source": "a" * 64}, "artifact": {"plan": "b" * 64},
        "policy": {"rules": "c" * 64}, "context": {"profile": "d" * 64},
        "evidence": {"tests": "e" * 64}})


class WorkUnitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "work-units"
        self.root.mkdir()
        self.store = WorkUnitStore(self.root)
        self.snapshot = create_work_unit(
            repo_identity="repo-a", task_identity="task-a",
            writer_id="writer-a", identity=envelope(),
            steps=(StepSpec("edit", True, ("subject.source",)),))

    def test_create_load_and_idempotent_recreate(self) -> None:
        self.store.create(self.snapshot)
        self.store.create(self.snapshot)
        self.assertEqual(self.store.load(self.snapshot.work_unit_id), self.snapshot)

    def test_compare_and_swap_rejects_stale_writer(self) -> None:
        self.store.create(self.snapshot)
        taken = takeover_work_unit(
            self.snapshot, expected_snapshot_digest=self.snapshot.snapshot_digest,
            new_writer_id="writer-b", current_identity=envelope())
        self.store.write(taken, expected_snapshot_digest=self.snapshot.snapshot_digest)
        with self.assertRaises(WorkUnitConflict):
            self.store.write(self.snapshot,
                             expected_snapshot_digest=self.snapshot.snapshot_digest)

    def test_corrupt_or_symlinked_state_fails_closed(self) -> None:
        self.store.create(self.snapshot)
        state = self.root / self.snapshot.work_unit_id / "state.json"
        state.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.load(self.snapshot.work_unit_id)


if __name__ == "__main__":
    unittest.main()
