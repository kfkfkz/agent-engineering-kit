"""Application orchestration for recoverable WorkUnits."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from aek.core.context.identity import IdentityEnvelope
from aek.core.work_unit import (
    ResumePlan,
    StepSpec,
    WorkState,
    WorkUnitSnapshot,
    create_work_unit,
    resume_work_unit,
    takeover_work_unit,
    transition_step,
)


class WorkUnitPort(Protocol):
    def create(self, snapshot: WorkUnitSnapshot) -> None: ...
    def load(self, work_unit_id: str) -> WorkUnitSnapshot | None: ...
    def write(self, snapshot: WorkUnitSnapshot, *,
              expected_snapshot_digest: str) -> None: ...


@dataclass(frozen=True)
class OpenedWorkUnit:
    snapshot: WorkUnitSnapshot
    persisted_locally: bool
    selected_carriers: tuple[str, ...]


class WorkUnitService:
    def __init__(self, store: WorkUnitPort) -> None:
        self.store = store

    def open_or_create(
        self, *, repo_identity: str, task_identity: str, writer_id: str,
        identity: IdentityEnvelope, steps: tuple[StepSpec, ...],
        carrier_refs: tuple[str, ...] = (),
    ) -> OpenedWorkUnit:
        candidate = create_work_unit(
            repo_identity=repo_identity, task_identity=task_identity,
            writer_id=writer_id, identity=identity, steps=steps,
            carrier_refs=carrier_refs)
        if carrier_refs:
            return OpenedWorkUnit(candidate, False, carrier_refs)
        current = self.store.load(candidate.work_unit_id)
        if current is None:
            self.store.create(candidate)
            current = candidate
        return OpenedWorkUnit(current, True, ())

    def takeover(
        self, work_unit_id: str, *, expected_snapshot_digest: str,
        new_writer_id: str, current_identity: IdentityEnvelope,
    ) -> WorkUnitSnapshot:
        current = self.store.load(work_unit_id)
        if current is None:
            raise ValueError("work unit does not exist")
        updated = takeover_work_unit(
            current, expected_snapshot_digest=expected_snapshot_digest,
            new_writer_id=new_writer_id, current_identity=current_identity)
        self.store.write(
            updated, expected_snapshot_digest=expected_snapshot_digest)
        return updated

    def transition(
        self, work_unit_id: str, *, expected_snapshot_digest: str,
        step_id: str, state: WorkState, writer_id: str, writer_epoch: int,
        event_id: str, receipt_ref: str = "", reason_code: str = "",
        next_step: str = "",
    ) -> WorkUnitSnapshot:
        current = self.store.load(work_unit_id)
        if current is None or current.snapshot_digest != expected_snapshot_digest:
            raise ValueError("work unit transition precondition failed")
        updated = transition_step(
            current, step_id, state, writer_id=writer_id,
            writer_epoch=writer_epoch, event_id=event_id,
            receipt_ref=receipt_ref, reason_code=reason_code,
            next_step=next_step)
        if updated != current:
            self.store.write(
                updated, expected_snapshot_digest=expected_snapshot_digest)
        return updated

    def resume(
        self, work_unit_id: str, current_identity: IdentityEnvelope,
        receipt_state: Callable[[str], str | None],
    ) -> ResumePlan:
        current = self.store.load(work_unit_id)
        if current is None:
            raise ValueError("work unit does not exist")
        observations = {
            step.receipt_ref: state
            for step in current.steps if step.receipt_ref
            for state in [receipt_state(step.receipt_ref)]
            if state is not None
        }
        return resume_work_unit(current, current_identity, observations)
