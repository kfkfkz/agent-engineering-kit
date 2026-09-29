"""Pure WorkUnit lifecycle and resume decisions."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import re

from aek.core.context.identity import (
    IdentityEnvelope,
    IdentityState,
    compare_identity,
    decode_identity,
    identity_as_dict,
)


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")


class WorkState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    STALE = "STALE"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    idempotent: bool
    required_components: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not _ID.fullmatch(self.step_id):
            raise ValueError("work step id is invalid")
        if type(self.idempotent) is not bool:
            raise ValueError("work step idempotency must be boolean")
        if (not isinstance(self.required_components, tuple)
                or not self.required_components):
            raise ValueError("work step identity components are required")


@dataclass(frozen=True)
class WorkStep:
    step_id: str
    idempotent: bool
    required_components: tuple[str, ...]
    state: WorkState = WorkState.NOT_STARTED
    receipt_ref: str = ""
    reason_code: str = ""
    next_step: str = ""


@dataclass(frozen=True)
class WorkUnitSnapshot:
    schema_version: int
    work_unit_id: str
    repo_identity: str
    task_identity: str
    writer_id: str
    writer_epoch: int
    identity: IdentityEnvelope
    carrier_refs: tuple[str, ...]
    local_state_required: bool
    steps: tuple[WorkStep, ...]
    applied_events: tuple[str, ...]
    snapshot_digest: str

    def by_step(self) -> dict[str, WorkStep]:
        return {item.step_id: item for item in self.steps}


@dataclass(frozen=True)
class StepResumeDecision:
    step_id: str
    action: str
    reason_code: str


@dataclass(frozen=True)
class ResumePlan:
    decisions: tuple[StepResumeDecision, ...]

    def by_step(self) -> dict[str, StepResumeDecision]:
        return {item.step_id: item for item in self.decisions}


_TRANSITIONS = {
    WorkState.NOT_STARTED: {WorkState.IN_PROGRESS, WorkState.SKIPPED,
                            WorkState.NEEDS_HUMAN},
    WorkState.IN_PROGRESS: {WorkState.COMPLETED, WorkState.FAILED_RETRYABLE,
                            WorkState.NEEDS_HUMAN, WorkState.STALE},
    WorkState.FAILED_RETRYABLE: {WorkState.IN_PROGRESS, WorkState.NEEDS_HUMAN,
                                 WorkState.STALE},
    WorkState.COMPLETED: {WorkState.STALE},
    WorkState.STALE: {WorkState.IN_PROGRESS, WorkState.NEEDS_HUMAN,
                      WorkState.SKIPPED},
    WorkState.SKIPPED: {WorkState.STALE},
    WorkState.NEEDS_HUMAN: set(),
}


def _payload(snapshot: WorkUnitSnapshot, *, include_digest: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "work_unit_id": snapshot.work_unit_id,
        "repo_identity": snapshot.repo_identity,
        "task_identity": snapshot.task_identity,
        "writer_id": snapshot.writer_id,
        "writer_epoch": snapshot.writer_epoch,
        "identity": identity_as_dict(snapshot.identity),
        "carrier_refs": list(snapshot.carrier_refs),
        "local_state_required": snapshot.local_state_required,
        "steps": [{
            "step_id": item.step_id, "idempotent": item.idempotent,
            "required_components": list(item.required_components),
            "state": item.state.value, "receipt_ref": item.receipt_ref,
            "reason_code": item.reason_code, "next_step": item.next_step,
        } for item in snapshot.steps],
        "applied_events": list(snapshot.applied_events),
    }
    if include_digest:
        payload["snapshot_digest"] = snapshot.snapshot_digest
    return payload


def _digest(snapshot: WorkUnitSnapshot) -> str:
    raw = json.dumps(_payload(snapshot, include_digest=False),
                     ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _seal(snapshot: WorkUnitSnapshot) -> WorkUnitSnapshot:
    return replace(snapshot, snapshot_digest=_digest(snapshot))


def create_work_unit(
    *, repo_identity: str, task_identity: str, writer_id: str,
    identity: IdentityEnvelope, steps: tuple[StepSpec, ...],
    carrier_refs: tuple[str, ...] = (),
) -> WorkUnitSnapshot:
    for value, label in ((repo_identity, "repo"), (task_identity, "task"),
                         (writer_id, "writer")):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"work unit {label} identity is invalid")
    identity_as_dict(identity)
    if (not isinstance(steps, tuple) or not steps
            or any(not isinstance(item, StepSpec) for item in steps)
            or len({item.step_id for item in steps}) != len(steps)):
        raise ValueError("work unit steps are invalid")
    if (not isinstance(carrier_refs, tuple)
            or any(not isinstance(item, str) or not item.strip()
                   for item in carrier_refs)):
        raise ValueError("work unit carrier refs are invalid")
    work_unit_id = hashlib.sha256(json.dumps(
        {"repo": repo_identity, "task": task_identity}, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    snapshot = WorkUnitSnapshot(
        1, work_unit_id, repo_identity, task_identity, writer_id, 1, identity,
        carrier_refs, not bool(carrier_refs),
        tuple(WorkStep(item.step_id, item.idempotent,
                       item.required_components) for item in steps),
        (), "0" * 64)
    return _seal(snapshot)


def transition_step(
    snapshot: WorkUnitSnapshot, step_id: str, state: WorkState, *,
    writer_id: str, writer_epoch: int, event_id: str,
    receipt_ref: str = "", reason_code: str = "", next_step: str = "",
) -> WorkUnitSnapshot:
    if event_id in snapshot.applied_events:
        return snapshot
    if writer_id != snapshot.writer_id or writer_epoch != snapshot.writer_epoch:
        raise ValueError("work unit writer epoch conflict")
    if not isinstance(event_id, str) or not _ID.fullmatch(event_id):
        raise ValueError("work unit event id is invalid")
    current = snapshot.by_step().get(step_id)
    if current is None or not isinstance(state, WorkState):
        raise ValueError("work unit transition target is invalid")
    if state not in _TRANSITIONS[current.state]:
        raise ValueError(f"illegal work step transition: {current.state}->{state}")
    if receipt_ref and (not isinstance(receipt_ref, str)
                        or not _ID.fullmatch(receipt_ref)):
        raise ValueError("work step receipt ref is invalid")
    effective_receipt = receipt_ref or current.receipt_ref
    if state == WorkState.COMPLETED and not current.idempotent \
            and not effective_receipt:
        raise ValueError("non-idempotent completion requires a receipt ref")
    if state == WorkState.NEEDS_HUMAN:
        if not reason_code or not next_step:
            raise ValueError("NEEDS_HUMAN requires a reason and one next step")
    elif next_step:
        raise ValueError("only NEEDS_HUMAN may carry a next step")
    updated = replace(current, state=state, receipt_ref=effective_receipt,
                      reason_code=reason_code, next_step=next_step)
    steps = tuple(updated if item.step_id == step_id else item
                  for item in snapshot.steps)
    return _seal(replace(
        snapshot, steps=steps,
        applied_events=snapshot.applied_events + (event_id,),
        snapshot_digest="0" * 64))


def takeover_work_unit(
    snapshot: WorkUnitSnapshot, *, expected_snapshot_digest: str,
    new_writer_id: str, current_identity: IdentityEnvelope,
) -> WorkUnitSnapshot:
    if snapshot.snapshot_digest != expected_snapshot_digest:
        raise ValueError("work unit takeover compare-and-swap failed")
    if not isinstance(new_writer_id, str) or not _ID.fullmatch(new_writer_id):
        raise ValueError("new work unit writer id is invalid")
    identity_as_dict(current_identity)
    return _seal(replace(
        snapshot, writer_id=new_writer_id,
        writer_epoch=snapshot.writer_epoch + 1,
        snapshot_digest="0" * 64))


def resume_work_unit(
    snapshot: WorkUnitSnapshot, current_identity: IdentityEnvelope,
    receipt_states: dict[str, str],
) -> ResumePlan:
    identity_as_dict(current_identity)
    if (not isinstance(receipt_states, dict)
            or any(not isinstance(key, str)
                   or value not in {"STARTED", "COMMITTED"}
                   for key, value in receipt_states.items())):
        raise ValueError("work unit receipt observations are invalid")
    decisions: list[StepResumeDecision] = []
    for step in snapshot.steps:
        comparison = compare_identity(
            snapshot.identity, current_identity, step.required_components)
        receipt_state = receipt_states.get(step.receipt_ref, "") \
            if step.receipt_ref else ""
        if step.state == WorkState.NEEDS_HUMAN:
            action, reason = "human", step.reason_code or "NEEDS_HUMAN"
        elif not step.idempotent and step.state in {
                WorkState.IN_PROGRESS, WorkState.COMPLETED}:
            if receipt_state == "COMMITTED":
                action, reason = "reuse", "NON_IDEMPOTENT_COMMITTED"
            else:
                action, reason = "human", (
                    "NON_IDEMPOTENT_STARTED" if receipt_state == "STARTED"
                    else "NON_IDEMPOTENT_RECEIPT_UNKNOWN")
        elif comparison.state == IdentityState.INVALID:
            action, reason = "human", "IDENTITY_INVALID"
        elif comparison.state == IdentityState.STALE:
            action, reason = "recompute", "IDENTITY_STALE"
        elif step.state in {WorkState.COMPLETED, WorkState.SKIPPED}:
            action, reason = "reuse", "VALID_COMPLETION"
        else:
            action, reason = "recompute", "INCOMPLETE_STEP"
        decisions.append(StepResumeDecision(step.step_id, action, reason))
    return ResumePlan(tuple(decisions))


def work_unit_as_dict(snapshot: WorkUnitSnapshot) -> dict[str, object]:
    if _digest(snapshot) != snapshot.snapshot_digest:
        raise ValueError("work unit snapshot digest mismatch")
    return _payload(snapshot, include_digest=True)


def decode_work_unit(payload: object) -> WorkUnitSnapshot:
    expected = {
        "schema_version", "work_unit_id", "repo_identity", "task_identity",
        "writer_id", "writer_epoch", "identity", "carrier_refs",
        "local_state_required", "steps", "applied_events", "snapshot_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected \
            or payload.get("schema_version") != 1:
        raise ValueError("work unit snapshot schema is invalid")
    try:
        step_values = payload["steps"]
        if not isinstance(step_values, list):
            raise ValueError
        steps: list[WorkStep] = []
        step_keys = {"step_id", "idempotent", "required_components", "state",
                     "receipt_ref", "reason_code", "next_step"}
        for item in step_values:
            if not isinstance(item, dict) or set(item) != step_keys:
                raise ValueError
            spec = StepSpec(item["step_id"], item["idempotent"],
                            tuple(item["required_components"]))
            steps.append(WorkStep(
                spec.step_id, spec.idempotent, spec.required_components,
                WorkState(item["state"]), item["receipt_ref"],
                item["reason_code"], item["next_step"]))
        snapshot = WorkUnitSnapshot(
            1, payload["work_unit_id"], payload["repo_identity"],
            payload["task_identity"], payload["writer_id"],
            payload["writer_epoch"], decode_identity(payload["identity"]),
            tuple(payload["carrier_refs"]), payload["local_state_required"],
            tuple(steps), tuple(payload["applied_events"]),
            payload["snapshot_digest"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("work unit snapshot fields are invalid") from exc
    if (type(snapshot.writer_epoch) is not int or snapshot.writer_epoch < 1
            or type(snapshot.local_state_required) is not bool
            or any(not isinstance(value, str) or not _ID.fullmatch(value)
                   for value in (snapshot.work_unit_id, snapshot.repo_identity,
                                 snapshot.task_identity, snapshot.writer_id))
            or not isinstance(snapshot.snapshot_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot.snapshot_digest)
            or any(not isinstance(item, str) or not item.strip()
                   for item in snapshot.carrier_refs)
            or any(not isinstance(item, str) or not _ID.fullmatch(item)
                   for item in snapshot.applied_events)
            or len({item.step_id for item in snapshot.steps}) != len(snapshot.steps)
            or any((item.state == WorkState.NEEDS_HUMAN)
                   != bool(item.reason_code and item.next_step)
                   for item in snapshot.steps)
            or any(item.next_step and item.state != WorkState.NEEDS_HUMAN
                   for item in snapshot.steps)
            or _digest(snapshot) != snapshot.snapshot_digest):
        raise ValueError("work unit snapshot digest or invariants are invalid")
    return snapshot
