#!/usr/bin/env python3
"""WorkUnit Application uses existing carriers first and local CAS only as fallback."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aek.adapters.work_unit_store import WorkUnitStore
from aek.application.work_unit import WorkUnitService
from aek.core.context.identity import build_identity
from aek.core.work_unit import StepSpec, WorkState


def envelope():
    return build_identity({
        "subject": {"source": "a" * 64}, "artifact": {"plan": "b" * 64},
        "policy": {"rules": "c" * 64}, "context": {"profile": "d" * 64},
        "evidence": {"tests": "e" * 64}})


class WorkUnitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "work-units"
        root.mkdir()
        self.store = WorkUnitStore(root)
        self.service = WorkUnitService(self.store)
        self.steps = (StepSpec("publish", False, ("subject.source",)),)

    def test_existing_carrier_avoids_duplicate_local_state(self) -> None:
        opened = self.service.open_or_create(
            repo_identity="repo-a", task_identity="task-a",
            writer_id="writer-a", identity=envelope(), steps=self.steps,
            carrier_refs=("artifact-plan:TASK-1",))
        self.assertFalse(opened.persisted_locally)
        self.assertIsNone(self.store.load(opened.snapshot.work_unit_id))

    def test_local_fallback_takeover_and_transition_use_cas(self) -> None:
        opened = self.service.open_or_create(
            repo_identity="repo-a", task_identity="task-b",
            writer_id="writer-a", identity=envelope(), steps=self.steps)
        taken = self.service.takeover(
            opened.snapshot.work_unit_id,
            expected_snapshot_digest=opened.snapshot.snapshot_digest,
            new_writer_id="writer-b", current_identity=envelope())
        started = self.service.transition(
            taken.work_unit_id, expected_snapshot_digest=taken.snapshot_digest,
            step_id="publish", state=WorkState.IN_PROGRESS,
            writer_id="writer-b", writer_epoch=2, event_id="start-1",
            receipt_ref="request-1")
        plan = self.service.resume(
            started.work_unit_id, envelope(),
            lambda request: "COMMITTED" if request == "request-1" else None)
        self.assertEqual(plan.by_step()["publish"].action, "reuse")

    def test_reopened_service_uses_receipt_after_committed_step_becomes_stale(self):
        snapshot = self.service.open_or_create(
            repo_identity="repo-a", task_identity="task-c",
            writer_id="writer-a", identity=envelope(), steps=self.steps).snapshot
        for state, event in ((WorkState.IN_PROGRESS, "begin"),
                             (WorkState.COMPLETED, "commit"),
                             (WorkState.STALE, "invalidate")):
            snapshot = self.service.transition(
                snapshot.work_unit_id, expected_snapshot_digest=snapshot.snapshot_digest,
                step_id="publish", state=state, writer_id="writer-a",
                writer_epoch=1, event_id=event, receipt_ref="request-1")
        reopened = WorkUnitService(WorkUnitStore(self.store.root))
        plan = reopened.resume(snapshot.work_unit_id, envelope(),
                               lambda request: "COMMITTED")
        self.assertEqual(plan.by_step()["publish"].action, "reuse")
        unknown = reopened.resume(snapshot.work_unit_id, envelope(),
                                  lambda request: None)
        self.assertEqual(unknown.by_step()["publish"].action, "human")


if __name__ == "__main__":
    unittest.main()
