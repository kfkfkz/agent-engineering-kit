"""Current ArtifactPlan evidence, anchored outside the Agent's writable output.

Canonical hashes prove integrity, not who approved a document. The evaluation
controller must supply trusted plan/credential/binding anchors; Agent-authored
sidecars alone cannot satisfy process evidence. This does not grade outcomes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from aek.application.identity import decode_bound_receipt, verify_bound_receipt
from aek.core.artifact.plan import (
    evaluate_binding,
    load_binding_bytes,
    load_credential_bytes,
    load_plan_bytes,
    resolved_stage_prerequisites,
)
from aek.core.artifact.registry import ARTIFACT_REGISTRY
from aek.core.context.identity import IdentityEnvelope


@dataclass(frozen=True)
class PlanAnchor:
    plan_digest: str
    credential_digest: str
    subject_digest: str


@dataclass(frozen=True)
class ArtifactObservation:
    artifact_id: str
    classification: str
    status: str
    reason_code: str


@dataclass(frozen=True)
class ArtifactEvidenceResult:
    status: str
    artifacts: tuple[ArtifactObservation, ...]
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceiptEvidenceResult:
    status: str
    identity_state: str
    verification_status: str | None
    reason_codes: tuple[str, ...] = ()


def evaluate_receipt(
    payload: Mapping[str, object], current_identity: IdentityEnvelope,
    required_components: tuple[str, ...], *, trusted_receipt_digest: str | None,
    status_path: tuple[str, ...] = ("status",), expected_status: str = "PASS",
) -> ReceiptEvidenceResult:
    if (not isinstance(required_components, tuple) or not required_components
            or not isinstance(status_path, tuple) or not status_path
            or not all(isinstance(key, str) and key for key in status_path)
            or not isinstance(expected_status, str) or expected_status not in {"PASS", "READY"}):
        return ReceiptEvidenceResult("INVALID", "INVALID", None, ("RECEIPT_REQUIREMENTS_INVALID",))
    try:
        receipt = decode_bound_receipt(payload)
    except (TypeError, ValueError):
        return ReceiptEvidenceResult("INVALID", "INVALID", None, ("RECEIPT_CONTENT_INVALID",))
    if trusted_receipt_digest is None:
        return ReceiptEvidenceResult("NOT_EVALUATED", "UNKNOWN", None, ("TRUST_ANCHOR_MISSING",))
    if receipt.receipt_digest != trusted_receipt_digest:
        return ReceiptEvidenceResult("INVALID", "INVALID", None, ("RECEIPT_ANCHOR_MISMATCH",))
    comparison = verify_bound_receipt(receipt, current_identity, required_components)
    if comparison.state.value != "VALID":
        return ReceiptEvidenceResult(comparison.state.value, comparison.state.value, None,
                                     comparison.reason_codes)
    verdict = json.loads(receipt.payload_json)
    for key in status_path:
        verdict = verdict.get(key) if isinstance(verdict, dict) else None
    if verdict == expected_status:
        status = "PASS"
    elif isinstance(verdict, str) and verdict in {"FAIL", "NOT_READY", "NOT READY"}:
        status = "FAIL"
    else:
        status = "NOT_EVALUATED"
    return ReceiptEvidenceResult(status, "VALID", verdict if isinstance(verdict, str) else None,
                                 () if status == "PASS" else ("RECEIPT_VERDICT_NOT_PASS",))


def evaluate_artifacts(
    plan_bytes: bytes, credential_bytes: bytes, *, anchor: PlanAnchor | None,
    current_subject_digest: str, documents: Mapping[str, bytes],
    bindings: Mapping[str, bytes], gate_digests: Mapping[str, str],
    prerequisite_fingerprints: Mapping[str, Mapping[str, str]],
    trusted_binding_digests: Mapping[str, str],
) -> ArtifactEvidenceResult:
    try:
        plan = load_plan_bytes(plan_bytes)
        credential = load_credential_bytes(credential_bytes)
    except (TypeError, ValueError):
        return ArtifactEvidenceResult("INVALID", (), ("PLAN_CONTENT_INVALID",))
    if anchor is None:
        return ArtifactEvidenceResult("NOT_EVALUATED", (), ("TRUST_ANCHOR_MISSING",))
    if (plan.digest != anchor.plan_digest
            or credential.credential_digest != anchor.credential_digest
            or credential.plan_digest != plan.digest
            or plan.subject_digest != anchor.subject_digest):
        return ArtifactEvidenceResult("INVALID", (), ("PLAN_ANCHOR_MISMATCH",))
    if current_subject_digest != anchor.subject_digest:
        return ArtifactEvidenceResult("STALE", (), ("SUBJECT_CHANGED",))
    needed = {ARTIFACT_REGISTRY.get_artifact(item.artifact_id).group_id
              for item in plan.items if item.classification == "required"}
    for _ in ARTIFACT_REGISTRY.groups:
        needed |= {parent for stage in tuple(needed)
                   for parent in resolved_stage_prerequisites(plan, stage)}
    states = {}
    for stage in ARTIFACT_REGISTRY.groups:
        active = [item for item in plan.items
                  if ARTIFACT_REGISTRY.get_artifact(item.artifact_id).group_id == stage
                  and item.classification != "skipped"]
        if not active or stage not in needed:
            continue
        try:
            binding = load_binding_bytes(bindings[stage])
            if binding.stage != stage or binding.binding_digest != trusted_binding_digests[stage]:
                states[stage] = ("INVALID", "BINDING_ANCHOR_MISMATCH")
                continue
            hashes = {}
            for item in active:
                filename = ARTIFACT_REGISTRY.get_artifact(item.artifact_id).filename
                hashes[filename] = hashlib.sha256(documents[filename]).hexdigest()
            state = evaluate_binding(
                binding, plan, credential, gate_digest=gate_digests[stage],
                doc_hashes=hashes, prerequisite_fingerprints=prerequisite_fingerprints[stage])
            states[stage] = ("PASS" if state == "BOUND" else state,
                             "BINDING_CURRENT" if state == "BOUND" else "BINDING_" + state)
        except KeyError:
            states[stage] = ("NOT_EVALUATED", "REQUIRED_EVIDENCE_MISSING")
        except (TypeError, ValueError):
            states[stage] = ("INVALID", "BINDING_CONTENT_INVALID")
    # A supplied old prerequisite fingerprint cannot hide observed parent drift.
    for _ in ARTIFACT_REGISTRY.groups:
        for stage in needed:
            if states.get(stage, ("NOT_EVALUATED", ""))[0] != "PASS":
                continue
            parent_states = {states.get(parent, ("NOT_EVALUATED", ""))[0]
                             for parent in resolved_stage_prerequisites(plan, stage)}
            for priority in ("INVALID", "STALE", "NOT_EVALUATED"):
                if priority in parent_states:
                    states[stage] = (priority, "PREREQUISITE_EVIDENCE_" + priority)
                    break
    rows = []
    for item in plan.items:
        if item.classification == "skipped":
            status, reason = "SKIPPED", item.reason_code
        elif item.classification == "optional":
            status, reason = "OPTIONAL", "NOT_REQUIRED"
        else:
            stage = ARTIFACT_REGISTRY.get_artifact(item.artifact_id).group_id
            status, reason = states.get(stage, ("NOT_EVALUATED", "REQUIRED_EVIDENCE_MISSING"))
        rows.append(ArtifactObservation(item.artifact_id, item.classification, status, reason))
    required = [row for row in rows if row.classification == "required"]
    status = "PASS"
    for priority in ("INVALID", "STALE", "NOT_EVALUATED"):
        if any(row.status == priority for row in required):
            status = priority
            break
    return ArtifactEvidenceResult(status, tuple(rows))
