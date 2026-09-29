"""CAS and crash recovery for lifecycle plus authoritative review gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from aek.adapters.atomic_file import AtomicFileStore
from aek.core.review.lifecycle import (
    IssueState,
    LifecycleSnapshot,
    lifecycle_bytes,
    load_lifecycle,
    project_pass_gate,
)


Checkpoint = Callable[[str], None]


class LifecycleConflict(RuntimeError):
    """The lifecycle changed after the caller read its precondition."""


class LifecycleGateStore:
    """Publish VERIFIED → PASS gate → materialized CLOSED under an outer lock."""

    def __init__(self, reviews: Path, stage: str) -> None:
        if (not isinstance(stage, str) or not stage.strip()
                or "/" in stage or "\\" in stage):
            raise ValueError("review stage is invalid")
        self.files = AtomicFileStore(Path(reviews))
        self.stage = stage
        self.lifecycle_name = f"{stage}.issue-lifecycle.json"
        self.gate_name = f"{stage}.gate.json"

    def load_lifecycle(self) -> LifecycleSnapshot | None:
        raw = self.files.read(self.lifecycle_name)
        return None if raw is None else load_lifecycle(raw)

    def load_gate(self) -> dict[str, object] | None:
        raw = self.files.read(self.gate_name)
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("review gate is malformed") from exc
        if not isinstance(value, dict):
            raise ValueError("review gate must be an object")
        return value

    def write_lifecycle(
        self,
        snapshot: LifecycleSnapshot,
        *,
        expected_snapshot_digest: str | None,
    ) -> None:
        current = self.load_lifecycle()
        current_digest = None if current is None else current.snapshot_digest
        if current_digest != expected_snapshot_digest:
            raise LifecycleConflict("lifecycle compare-and-swap failed")
        self.files.write(self.lifecycle_name, lifecycle_bytes(snapshot))

    @staticmethod
    def _gate_bytes(gate: dict[str, object]) -> bytes:
        return json.dumps(gate, ensure_ascii=False, sort_keys=True,
                          indent=2).encode("utf-8")

    def publish_pass_gate(
        self,
        verified: LifecycleSnapshot,
        gate: dict[str, object],
        *,
        expected_snapshot_digest: str | None,
        checkpoint: Checkpoint | None = None,
    ) -> tuple[LifecycleSnapshot, dict[str, object]]:
        """Publish in recovery-safe order; checkpoint exists for crash tests."""
        if (not isinstance(gate, dict) or gate.get("stage") != self.stage
                or gate.get("status") != "PASS"
                or "lifecycle_snapshot_digest" in gate
                or any(item.state == IssueState.BLOCKED
                       for item in verified.issues)):
            raise ValueError("PASS gate is not eligible for lifecycle closure")
        self.write_lifecycle(
            verified, expected_snapshot_digest=expected_snapshot_digest)
        if checkpoint is not None:
            checkpoint("after_verified")
        bound_gate = dict(gate)
        bound_gate["lifecycle_snapshot_digest"] = verified.snapshot_digest
        gate_bytes = self._gate_bytes(bound_gate)
        self.files.write(self.gate_name, gate_bytes)
        if checkpoint is not None:
            checkpoint("after_gate")
        gate_digest = hashlib.sha256(gate_bytes).hexdigest()
        closed = project_pass_gate(
            verified, stage=self.stage, status="PASS",
            lifecycle_snapshot_digest=verified.snapshot_digest,
            gate_digest=gate_digest)
        self.write_lifecycle(
            closed, expected_snapshot_digest=verified.snapshot_digest)
        if checkpoint is not None:
            checkpoint("after_closed")
        return closed, bound_gate

    def publish_non_pass_gate(
        self,
        snapshot: LifecycleSnapshot,
        gate: dict[str, object],
        *,
        expected_snapshot_digest: str | None,
    ) -> dict[str, object]:
        """Atomically publish a non-authorizing decision bound to its snapshot."""
        if (not isinstance(gate, dict) or gate.get("stage") != self.stage
                or gate.get("status") not in {"NEEDS_REVISION", "BLOCKED"}
                or "lifecycle_snapshot_digest" in gate):
            raise ValueError("non-PASS gate is invalid")
        self.write_lifecycle(
            snapshot, expected_snapshot_digest=expected_snapshot_digest)
        bound_gate = dict(gate)
        bound_gate["lifecycle_snapshot_digest"] = snapshot.snapshot_digest
        self.files.write(self.gate_name, self._gate_bytes(bound_gate))
        return bound_gate

    def recover(self) -> LifecycleSnapshot | None:
        """Complete a gate-backed projection, or leave pre-gate VERIFIED intact."""
        snapshot = self.load_lifecycle()
        if snapshot is None:
            return None
        raw_gate = self.files.read(self.gate_name)
        if raw_gate is None:
            return snapshot
        try:
            gate = json.loads(raw_gate.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("review gate is malformed") from exc
        if not isinstance(gate, dict) or gate.get("stage") != self.stage:
            raise ValueError("review gate stage is invalid")
        if gate.get("status") != "PASS":
            return snapshot
        lifecycle_digest = gate.get("lifecycle_snapshot_digest")
        if not isinstance(lifecycle_digest, str):
            # A legacy or pre-publication gate cannot authorize this lifecycle.
            return snapshot
        try:
            closed = project_pass_gate(
                snapshot, stage=self.stage, status="PASS",
                lifecycle_snapshot_digest=lifecycle_digest,
                gate_digest=hashlib.sha256(raw_gate).hexdigest())
        except ValueError:
            # The lifecycle may have been published before replacing an older
            # gate.  Preserve VERIFIED; only an exactly bound PASS can close it.
            return snapshot
        if closed != snapshot:
            self.write_lifecycle(
                closed, expected_snapshot_digest=snapshot.snapshot_digest)
        return closed
