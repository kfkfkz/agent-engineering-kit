"""Recover and validate dynamic plan state without authorizing a stage SKIP.

This is the read-side transition boundary. Stage-specific decisions still need
current gate/document/prerequisite snapshots before doc-gate may consume them.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from aek.core.artifact.plan import (
    ArtifactPlan, PlanCredential, StageBinding, bind_stage, build_plan,
    evaluate_binding, freeze_plan, load_binding_bytes, load_credential_bytes,
    load_plan_bytes, resolved_stage_prerequisites,
)
from aek.core.artifact.registry import ARTIFACT_REGISTRY, ArtifactRegistry
from aek.core.planning.facts import (
    FACT_CATALOG, FactResolution, merge_fact_observations,
)
from aek.core.planning.policy import preview_plan


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ChangeScopeScanner = Callable[[Path, str], object]


class PlanSnapshotPort(Protocol):
    def snapshot(self) -> dict[str, bytes]: ...


class PlanMutationPort(PlanSnapshotPort, Protocol):
    def snapshot_with_digest(self) -> tuple[dict[str, bytes], str]: ...
    def publish(self, changes: Mapping[str, bytes | None], *,
                expected_consumers: str, fault_after: int | None = None) -> None: ...
    def rollback_all(self, *, expected_consumers: str,
                     fault_after: int | None = None) -> None: ...


@dataclass(frozen=True)
class DynamicPlanState:
    state: str
    reason: str
    plan: ArtifactPlan | None = None
    credential: PlanCredential | None = None
    bindings: tuple[StageBinding, ...] = ()


def evaluate_stage_snapshot(
    state: DynamicPlanState, stage: str, *, gate_digest: str,
    doc_hashes: Mapping[str, str],
    prerequisite_fingerprints: Mapping[str, str],
) -> str:
    """Compare a persisted binding with independently captured current inputs.

    The caller must supply gate/document/prerequisite values from a trusted
    current snapshot; persisted binding fields are never their own evidence.
    """
    if not isinstance(state, DynamicPlanState) or not isinstance(stage, str):
        return "INVALID"
    if state.state == "INVALID":
        return "INVALID"
    if state.state in {"ABSENT", "PREPARED"}:
        return "UNBOUND"
    if state.state != "FROZEN" or state.plan is None or state.credential is None:
        return "INVALID"
    matching = tuple(item for item in state.bindings if item.stage == stage)
    if len(matching) > 1:
        return "INVALID"
    if not matching:
        return "UNBOUND"
    return evaluate_binding(
        matching[0], state.plan, state.credential,
        gate_digest=gate_digest, doc_hashes=doc_hashes,
        prerequisite_fingerprints=prerequisite_fingerprints)


def evaluate_stage_states(
    state: DynamicPlanState,
    snapshots: Mapping[str, Mapping[str, object]], *,
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> dict[str, str]:
    """Evaluate every stage and propagate upstream invalidation transitively.

    A stage without a current snapshot remains UNBOUND. A persisted binding
    can only remain BOUND when its own inputs and every direct/transitive
    prerequisite are BOUND. Unknown stages or snapshot fields fail the entire
    map closed instead of being silently ignored.
    """
    stages = tuple(registry.groups)
    invalid_map = {stage: "INVALID" for stage in stages}
    if (not isinstance(snapshots, Mapping)
            or any(stage not in registry.groups for stage in snapshots)):
        return invalid_map
    direct: dict[str, str] = {}
    required_fields = {"gate_digest", "doc_hashes", "prerequisite_fingerprints"}
    for stage in stages:
        snapshot = snapshots.get(stage)
        if snapshot is None:
            direct[stage] = "UNBOUND"
            continue
        if (not isinstance(snapshot, Mapping)
                or set(snapshot) != required_fields
                or not isinstance(snapshot["gate_digest"], str)
                or not isinstance(snapshot["doc_hashes"], Mapping)
                or not isinstance(snapshot["prerequisite_fingerprints"], Mapping)):
            direct[stage] = "INVALID"
            continue
        direct[stage] = evaluate_stage_snapshot(
            state, stage, gate_digest=snapshot["gate_digest"],
            doc_hashes=snapshot["doc_hashes"],
            prerequisite_fingerprints=snapshot["prerequisite_fingerprints"],
        )

    resolved: dict[str, str] = {}
    visiting: set[str] = set()

    def resolve(stage: str) -> str:
        if stage in resolved:
            return resolved[stage]
        if stage in visiting:
            return "INVALID"
        visiting.add(stage)
        own = direct[stage]
        plan_parents = (resolved_stage_prerequisites(state.plan, stage, registry)
                        if state.plan is not None
                        else registry.get_group(stage).prerequisites)
        parents = tuple(resolve(parent) for parent in plan_parents)
        visiting.remove(stage)
        if own in {"INVALID", "UNBOUND", "STALE"}:
            result = own
        elif any(parent == "INVALID" for parent in parents):
            result = "INVALID"
        elif any(parent != "BOUND" for parent in parents):
            result = "STALE"
        else:
            result = "BOUND"
        resolved[stage] = result
        return result

    return {stage: resolve(stage) for stage in stages}


def _git_current_facts(
    repository_root: Path, base_commit: str, scan_scope: ChangeScopeScanner,
) -> tuple[str, dict[str, FactResolution]]:
    """Derive current facts from built-in producers, never caller-supplied false.

    A complete, plain-document Git delta can prove all planning facts false.
    Any code or unclassified delta stays unknown; that can only increase
    required work, never authorize SKIP.
    """
    scan = scan_scope(repository_root, base_commit)
    subject_digest = getattr(scan, "subject_digest", None)
    observations = getattr(scan, "observations", None)
    if (not isinstance(subject_digest, str) or not _DIGEST.fullmatch(subject_digest)
            or not isinstance(observations, tuple)):
        raise ValueError("change-scope adapter returned an invalid snapshot")
    facts = {fact_id: merge_fact_observations(
        fact_id, subject_digest,
        tuple(item for item in observations if item.fact_id == fact_id))
        for fact_id in FACT_CATALOG}
    return subject_digest, facts


def build_git_current_plan(
    repository_root: Path, base_commit: str, route: str,
    *, scan_scope: ChangeScopeScanner,
    required_overlays: tuple[str, ...] = (),
) -> ArtifactPlan:
    """Build a conservative plan from a freshly captured Git-visible scope."""
    subject, facts = _git_current_facts(repository_root, base_commit, scan_scope)
    return build_plan(preview_plan(route, subject, facts,
                                   required_overlays=required_overlays),
                      facts, required_overlays=required_overlays)


def _canonical(value: object) -> bytes:
    return json.dumps(asdict(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def git_plan_input_digest(plan: ArtifactPlan, base_commit: str) -> str:
    """Bind a credential to the exact Git base and all plan input versions."""
    if (not isinstance(plan, ArtifactPlan)
            or not isinstance(base_commit, str) or not _COMMIT.fullmatch(base_commit)):
        raise ValueError("plan input identity is invalid")
    payload = {
        "schema_version": 1,
        "base_commit": base_commit,
        "subject_digest": plan.subject_digest,
        "route": plan.route,
        "profile_version": plan.profile_version,
        "policy_version": plan.policy_version,
        "registry_version": plan.registry_version,
        "facts_digest": plan.facts_digest,
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def prepare_git_current_plan(
    store: PlanMutationPort, repository_root: Path, base_commit: str, route: str,
    *, scan_scope: ChangeScopeScanner,
    required_overlays: tuple[str, ...] = (),
) -> DynamicPlanState:
    """Publish a conservative PREPARED plan and invalidate old consumers."""
    plan = build_git_current_plan(repository_root, base_commit, route,
                                  scan_scope=scan_scope,
                                  required_overlays=required_overlays)
    snapshot, consumers = store.snapshot_with_digest()
    raw_plan = _canonical(plan)
    changes: dict[str, bytes | None] = {"artifact-plan.json": raw_plan}
    if snapshot.get("artifact-plan.json") != raw_plan:
        for name in snapshot:
            if name == "artifact-plan.gate.json" or name.endswith(
                    ".plan-binding.json"):
                changes[name] = None
    changes = {name: value for name, value in changes.items()
               if snapshot.get(name) != value}
    if changes:
        store.publish(changes, expected_consumers=consumers)
    current = build_git_current_plan(repository_root, base_commit, route,
                                     scan_scope=scan_scope,
                                     required_overlays=required_overlays)
    if current != plan:
        return DynamicPlanState("INVALID", "Git scope changed while preparing")
    # Build does not receive the credential input identity. Preserve an
    # identical existing credential but report only PREPARED; authorization
    # must go through freeze/status with the expected input digest.
    return DynamicPlanState("PREPARED", "plan prepared; credential not evaluated", plan)


def freeze_git_current_plan(
    store: PlanMutationPort, repository_root: Path, base_commit: str,
    input_digest: str, *, scan_scope: ChangeScopeScanner, frozen_at: str,
) -> DynamicPlanState:
    """Freeze only the plan reproduced by the current trusted Git facts."""
    snapshot, consumers = store.snapshot_with_digest()
    raw_plan = snapshot.get("artifact-plan.json")
    if raw_plan is None:
        return DynamicPlanState("ABSENT", "no prepared plan")
    try:
        plan = load_plan_bytes(raw_plan)
        expected = build_git_current_plan(repository_root, base_commit, plan.route,
            scan_scope=scan_scope,
            required_overlays=tuple(item.artifact_id for item in plan.items
                                    if item.reason_code == "GOVERNANCE_OVERLAY"))
        if expected != plan:
            return DynamicPlanState("INVALID", "prepared plan is stale")
        existing_raw = snapshot.get("artifact-plan.gate.json")
        existing = (load_credential_bytes(existing_raw)
                    if existing_raw is not None else None)
        if (existing is not None and existing.plan_digest == plan.digest
                and existing.input_digest == input_digest
                and existing.generation_mode == "native"):
            credential = existing
        else:
            credential = freeze_plan(plan, input_digest=input_digest,
                                     generation_mode="native", frozen_at=frozen_at)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return DynamicPlanState("INVALID", str(exc))
    raw_credential = _canonical(credential)
    changes: dict[str, bytes | None] = {
        "artifact-plan.gate.json": raw_credential}
    if snapshot.get("artifact-plan.gate.json") != raw_credential:
        changes.update({name: None for name in snapshot
                        if name.endswith(".plan-binding.json")})
    changes = {name: value for name, value in changes.items()
               if snapshot.get(name) != value}
    if changes:
        store.publish(changes, expected_consumers=consumers)
    return read_git_current_dynamic_state(
        store, repository_root, base_commit, input_digest,
        scan_scope=scan_scope)


def bind_current_stage(
    store: PlanMutationPort, repository_root: Path, base_commit: str,
    input_digest: str, stage: str, *, gate_digest: str,
    doc_hashes: Mapping[str, str],
    prerequisite_fingerprints: Mapping[str, str],
    scan_scope: ChangeScopeScanner,
) -> DynamicPlanState:
    """Publish one binding; callers still re-evaluate current stage inputs."""
    state = read_git_current_dynamic_state(
        store, repository_root, base_commit, input_digest,
        scan_scope=scan_scope)
    if state.state != "FROZEN" or state.plan is None or state.credential is None:
        return state
    try:
        binding = bind_stage(
            state.plan, state.credential, stage, gate_digest=gate_digest,
            doc_hashes=doc_hashes,
            prerequisite_fingerprints=prerequisite_fingerprints)
    except (KeyError, TypeError, ValueError) as exc:
        return DynamicPlanState("INVALID", str(exc), state.plan, state.credential,
                                state.bindings)
    snapshot, consumers = store.snapshot_with_digest()
    if (snapshot.get("artifact-plan.json") != _canonical(state.plan)
            or snapshot.get("artifact-plan.gate.json") != _canonical(state.credential)):
        return DynamicPlanState("INVALID", "plan changed before stage binding")
    name = f"{stage}.plan-binding.json"
    raw_binding = _canonical(binding)
    if snapshot.get(name) != raw_binding:
        store.publish({name: raw_binding}, expected_consumers=consumers)
    return read_git_current_dynamic_state(
        store, repository_root, base_commit, input_digest,
        scan_scope=scan_scope)


def bind_frozen_stage(
    store: PlanMutationPort, expected_input_digest: str, stage: str, *,
    gate_digest: str, doc_hashes: Mapping[str, str],
    prerequisite_fingerprints: Mapping[str, str],
) -> DynamicPlanState:
    """Bind execution-time documents to an already authenticated plan.

    Git scope is revalidated at prepare/freeze and by the final route-drift
    gate. During execution, the frozen credential is the plan authority while
    current gate/document/prerequisite bytes are rechecked per stage.
    """
    state = read_frozen_dynamic_state(store, expected_input_digest)
    if state.state != "FROZEN" or state.plan is None or state.credential is None:
        return state
    try:
        binding = bind_stage(
            state.plan, state.credential, stage, gate_digest=gate_digest,
            doc_hashes=doc_hashes,
            prerequisite_fingerprints=prerequisite_fingerprints)
    except (KeyError, TypeError, ValueError) as exc:
        return DynamicPlanState("INVALID", str(exc), state.plan, state.credential,
                                state.bindings)
    snapshot, consumers = store.snapshot_with_digest()
    if (snapshot.get("artifact-plan.json") != _canonical(state.plan)
            or snapshot.get("artifact-plan.gate.json") != _canonical(state.credential)):
        return DynamicPlanState("INVALID", "plan changed before stage binding")
    name = f"{stage}.plan-binding.json"
    raw_binding = _canonical(binding)
    if snapshot.get(name) != raw_binding:
        store.publish({name: raw_binding}, expected_consumers=consumers)
    return read_frozen_dynamic_state(store, expected_input_digest)


def rollback_dynamic_state(store: PlanMutationPort) -> None:
    """Remove all dynamic state through the recoverable PlanStore protocol."""
    _snapshot, consumers = store.snapshot_with_digest()
    store.rollback_all(expected_consumers=consumers)


def read_git_current_dynamic_state(
    store: PlanSnapshotPort, repository_root: Path, base_commit: str,
    expected_input_digest: str, *, scan_scope: ChangeScopeScanner,
) -> DynamicPlanState:
    """Validate a persisted plan against a new trusted scope capture."""
    try:
        subject, facts = _git_current_facts(repository_root, base_commit, scan_scope)
    except (OSError, ValueError, RuntimeError) as exc:
        return DynamicPlanState("INVALID", f"current Git scope unavailable: {exc}")
    return read_dynamic_state(store, subject, expected_input_digest, facts)


def read_frozen_dynamic_state(
    store: PlanSnapshotPort, expected_input_digest: str,
) -> DynamicPlanState:
    """Read a post-freeze plan using its credential as the fact authority."""
    if (not isinstance(expected_input_digest, str)
            or not _DIGEST.fullmatch(expected_input_digest)):
        raise ValueError("expected plan input identity is invalid")
    try:
        snapshot = store.snapshot()
        raw_plan = snapshot.get("artifact-plan.json")
        if raw_plan is None:
            if snapshot:
                return DynamicPlanState("INVALID", "orphan credential or binding")
            return DynamicPlanState("ABSENT", "no dynamic sidecars")
        plan = load_plan_bytes(raw_plan)
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        return DynamicPlanState("INVALID", str(exc))

    class CapturedSnapshot:
        def snapshot(self) -> dict[str, bytes]:
            return dict(snapshot)

    return read_dynamic_state(
        CapturedSnapshot(), plan.subject_digest, expected_input_digest,
        _credential_authority=True)


def read_dynamic_state(
    store: PlanSnapshotPort, expected_subject_digest: str, expected_input_digest: str,
    verified_facts: Mapping[str, FactResolution] | None = None,
    *, _credential_authority: bool = False,
) -> DynamicPlanState:
    if (not callable(getattr(store, "snapshot", None))
            or not isinstance(expected_subject_digest, str)
            or not _DIGEST.fullmatch(expected_subject_digest)
            or not isinstance(expected_input_digest, str)
            or not _DIGEST.fullmatch(expected_input_digest)):
        raise ValueError("expected plan identity is invalid")
    try:
        snapshot = store.snapshot()
        if not snapshot:
            return DynamicPlanState("ABSENT", "no dynamic sidecars")
        raw_plan = snapshot.get("artifact-plan.json")
        raw_credential = snapshot.get("artifact-plan.gate.json")
        binding_names = sorted(name for name in snapshot
                               if name.endswith(".plan-binding.json"))
        if raw_plan is None:
            return DynamicPlanState("INVALID", "orphan credential or binding")
        plan = load_plan_bytes(raw_plan)
        if plan.subject_digest != expected_subject_digest:
            return DynamicPlanState("INVALID", "plan subject changed")
        if raw_credential is None:
            if binding_names:
                return DynamicPlanState("INVALID", "binding without credential")
            return DynamicPlanState("PREPARED", "plan has no freeze credential", plan)
        credential = load_credential_bytes(raw_credential)
        if (credential.plan_digest != plan.digest
                or credential.input_digest != expected_input_digest):
            return DynamicPlanState("INVALID", "plan input or credential is stale")
        if verified_facts is None and not _credential_authority:
            return DynamicPlanState("INVALID", "current verified facts are unavailable")
        if verified_facts is not None:
            overlays = tuple(item.artifact_id for item in plan.items
                             if item.reason_code == "GOVERNANCE_OVERLAY")
            rebuilt = build_plan(
                preview_plan(plan.route, expected_subject_digest, verified_facts,
                             required_overlays=overlays),
                verified_facts, required_overlays=overlays)
            if rebuilt != plan:
                return DynamicPlanState("INVALID", "plan no longer matches verified facts")
        bindings: list[StageBinding] = []
        for name in binding_names:
            binding = load_binding_bytes(snapshot[name])
            if name != f"{binding.stage}.plan-binding.json":
                return DynamicPlanState("INVALID", "binding filename and stage disagree")
            if evaluate_binding(
                    binding, plan, credential, gate_digest=binding.gate_digest,
                    doc_hashes=dict(binding.doc_hashes),
                    prerequisite_fingerprints=dict(binding.prerequisite_fingerprints)
            ) != "BOUND":
                return DynamicPlanState("INVALID", "binding conflicts with plan")
            bindings.append(binding)
        return DynamicPlanState("FROZEN", "plan credential verified", plan,
                                credential, tuple(bindings))
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        return DynamicPlanState("INVALID", str(exc))
