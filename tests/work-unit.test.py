#!/usr/bin/env python3
"""WorkUnit state, takeover, identity revalidation and receipt-safe resume."""
from __future__ import annotations

import unittest

from aek.core.context.identity import build_identity
from aek.core.work_unit import (
    StepSpec,
    WorkState,
    create_work_unit,
    resume_work_unit,
    takeover_work_unit,
    transition_step,
)


def identity(source: str = "a", policy: str = "b"):
    return build_identity({
        "subject": {"source": source * 64},
        "artifact": {"plan": "c" * 64},
        "policy": {"rules": policy * 64},
        "context": {"profile": "d" * 64},
        "evidence": {"tests": "e" * 64},
    })


class WorkUnitTests(unittest.TestCase):
    def snapshot(self):
        return create_work_unit(
            repo_identity="repo-a", task_identity="task-1",
            writer_id="writer-a", identity=identity(),
            carrier_refs=("artifact-plan:TASK-1",),
            steps=(
                StepSpec("edit", True, ("subject.source", "policy.rules")),
                StepSpec("publish", False, ("subject.source",)),
            ))

    def test_seven_states_are_public_contract(self) -> None:
        self.assertEqual(
            {state.value for state in WorkState},
            {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "FAILED_RETRYABLE",
             "NEEDS_HUMAN", "STALE", "SKIPPED"})

    def test_transition_is_event_idempotent_and_checks_writer_epoch(self) -> None:
        initial = self.snapshot()
        started = transition_step(
            initial, "edit", WorkState.IN_PROGRESS, writer_id="writer-a",
            writer_epoch=1, event_id="event-1")
        self.assertEqual(
            transition_step(started, "edit", WorkState.IN_PROGRESS,
                            writer_id="writer-a", writer_epoch=1,
                            event_id="event-1"), started)
        with self.assertRaises(ValueError):
            transition_step(started, "edit", WorkState.COMPLETED,
                            writer_id="writer-old", writer_epoch=1,
                            event_id="event-2")

    def test_takeover_increments_epoch_and_rejects_wrong_precondition(self) -> None:
        initial = self.snapshot()
        taken = takeover_work_unit(
            initial, expected_snapshot_digest=initial.snapshot_digest,
            new_writer_id="writer-b", current_identity=identity())
        self.assertEqual(taken.writer_epoch, 2)
        self.assertEqual(taken.writer_id, "writer-b")
        with self.assertRaises(ValueError):
            takeover_work_unit(
                taken, expected_snapshot_digest=initial.snapshot_digest,
                new_writer_id="writer-c", current_identity=identity())

    def test_resume_reuses_valid_idempotent_and_recomputes_stale(self) -> None:
        current = self.snapshot()
        current = transition_step(
            current, "edit", WorkState.IN_PROGRESS, writer_id="writer-a",
            writer_epoch=1, event_id="e1")
        current = transition_step(
            current, "edit", WorkState.COMPLETED, writer_id="writer-a",
            writer_epoch=1, event_id="e2")
        valid = resume_work_unit(current, identity(), {})
        stale = resume_work_unit(current, identity(source="f"), {})
        self.assertEqual(valid.by_step()["edit"].action, "reuse")
        self.assertEqual(stale.by_step()["edit"].action, "recompute")

    def test_non_idempotent_step_never_reexecutes_without_receipt_proof(self) -> None:
        current = self.snapshot()
        current = transition_step(
            current, "publish", WorkState.IN_PROGRESS, writer_id="writer-a",
            writer_epoch=1, event_id="p1", receipt_ref="request-7")
        committed = resume_work_unit(
            current, identity(), {"request-7": "COMMITTED"})
        started = resume_work_unit(
            current, identity(), {"request-7": "STARTED"})
        missing = resume_work_unit(current, identity(), {})
        self.assertEqual(committed.by_step()["publish"].action, "reuse")
        self.assertEqual(started.by_step()["publish"].action, "human")
        self.assertEqual(missing.by_step()["publish"].action, "human")

    def test_non_idempotent_receipts_survive_failed_and_stale_steps(self) -> None:
        started = transition_step(
            self.snapshot(), "publish", WorkState.IN_PROGRESS,
            writer_id="writer-a", writer_epoch=1, event_id="started",
            receipt_ref="request-7")
        completed = transition_step(
            started, "publish", WorkState.COMPLETED,
            writer_id="writer-a", writer_epoch=1, event_id="completed")
        snapshots = (
            transition_step(started, "publish", WorkState.FAILED_RETRYABLE,
                            writer_id="writer-a", writer_epoch=1,
                            event_id="failed"),
            transition_step(started, "publish", WorkState.STALE,
                            writer_id="writer-a", writer_epoch=1,
                            event_id="stale-started"),
            transition_step(completed, "publish", WorkState.STALE,
                            writer_id="writer-a", writer_epoch=1,
                            event_id="stale-completed"),
        )
        for snapshot in snapshots:
            for receipt, expected in (("COMMITTED", "reuse"),
                                      ("STARTED", "human"), (None, "human")):
                with self.subTest(state=snapshot.by_step()["publish"].state,
                                  receipt=receipt):
                    observations = {"request-7": receipt} if receipt else {}
                    decision = resume_work_unit(
                        snapshot, identity(), observations).by_step()["publish"]
                    self.assertEqual(decision.action, expected)

    def test_needs_human_preserves_exactly_one_next_step(self) -> None:
        current = self.snapshot()
        current = transition_step(
            current, "publish", WorkState.IN_PROGRESS, writer_id="writer-a",
            writer_epoch=1, event_id="h1", receipt_ref="request-8")
        blocked = transition_step(
            current, "publish", WorkState.NEEDS_HUMAN, writer_id="writer-a",
            writer_epoch=1, event_id="h2", reason_code="OUTCOME_UNKNOWN",
            next_step="query request-8 in the external system")
        step = blocked.by_step()["publish"]
        self.assertEqual(step.reason_code, "OUTCOME_UNKNOWN")
        self.assertEqual(step.next_step,
                         "query request-8 in the external system")

    def test_committed_external_action_does_not_authorize_changed_identity(self):
        started = transition_step(
            self.snapshot(), "publish", WorkState.IN_PROGRESS,
            writer_id="writer-a", writer_epoch=1, event_id="begin",
            receipt_ref="request-7")
        decision = resume_work_unit(
            started, identity(source="f"),
            {"request-7": "COMMITTED"}).by_step()["publish"]
        self.assertEqual(decision.action, "human")
        self.assertEqual(decision.reason_code,
                         "NON_IDEMPOTENT_COMMITTED_IDENTITY_STALE")

    def test_never_started_and_skipped_external_steps_need_no_commit_receipt(self):
        initial = self.snapshot()
        skipped = transition_step(
            initial, "publish", WorkState.SKIPPED,
            writer_id="writer-a", writer_epoch=1, event_id="skip")
        self.assertEqual(resume_work_unit(initial, identity(), {}).by_step()[
            "publish"].action, "recompute")
        self.assertEqual(resume_work_unit(skipped, identity(), {}).by_step()[
            "publish"].action, "reuse")


if __name__ == "__main__":
    unittest.main()
