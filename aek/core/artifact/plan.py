"""Canonical, pure dynamic ArtifactPlan and stage-binding contracts.

Persistence/locking and doc-gate activation are separate. A preview cannot
authorize SKIP until it is promoted under a validated transaction credential.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from aek.core.artifact.registry import ARTIFACT_REGISTRY, ArtifactRegistry
from aek.core.planning.facts import FACT_CATALOG, FactResolution
from aek.core.planning.policy import PlanPreview, preview_plan


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ROUTES = ("direct", "bounded", "standard", "initiative")
_CLASSIFICATIONS = {"required", "optional", "skipped"}


def _normal(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {str(_normal(k)): _normal(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    return value


def _digest(value: dict[str, object]) -> str:
    data = json.dumps(_normal(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hash(value: str, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be SHA-256")
    return value


def _hash_map(value: Mapping[str, str], field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or any(
            not isinstance(key, str) or not key or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest) for key, digest in value.items()):
        raise ValueError(f"invalid {field} hash map")
    return tuple(sorted(value.items()))


@dataclass(frozen=True)
class PlanItem:
    artifact_id: str
    classification: str
    reason_code: str
    evidence_ref_ids: tuple[str, ...]
    source_fact_digests: tuple[str, ...]
    resolved_prerequisites: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactPlan:
    schema_version: int
    subject_digest: str
    route: str
    profile_version: int
    policy_version: int
    registry_version: int
    facts_digest: str
    items: tuple[PlanItem, ...]
    digest: str

    def by_id(self, artifact_id: str) -> PlanItem:
        for item in self.items:
            if item.artifact_id == artifact_id:
                return item
        raise KeyError(artifact_id)


def _plan_payload(plan: ArtifactPlan) -> dict[str, object]:
    return {
        "schema_version": plan.schema_version,
        "subject_digest": plan.subject_digest,
        "route": plan.route,
        "profile_version": plan.profile_version,
        "policy_version": plan.policy_version,
        "registry_version": plan.registry_version,
        "facts_digest": plan.facts_digest,
        "items": [
            {name: getattr(item, name) for name in PlanItem.__dataclass_fields__}
            for item in plan.items
        ],
    }


def _resolved_prerequisites(
    classifications: Mapping[str, str], group_id: str,
    registry: ArtifactRegistry,
) -> tuple[str, ...]:
    """Resolve skipped groups to the nearest participating predecessors."""
    active = {
        stage: any(classifications[artifact_id] != "skipped"
                   for artifact_id in group.artifact_ids)
        for stage, group in registry.groups.items()
    }
    result: list[str] = []
    seen: set[str] = set()

    def expand(stage: str) -> None:
        for parent in registry.get_group(stage).prerequisites:
            if active[parent]:
                if parent not in seen:
                    seen.add(parent)
                    result.append(parent)
            else:
                expand(parent)

    expand(group_id)
    return tuple(result)


def _resolved_closure(
    classifications: Mapping[str, str], group_id: str,
    registry: ArtifactRegistry,
) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []

    def collect(stage: str) -> None:
        for parent in _resolved_prerequisites(classifications, stage, registry):
            collect(parent)
            if parent not in seen:
                seen.add(parent)
                result.append(parent)

    collect(group_id)
    return tuple(result)


def resolved_stage_prerequisites(
    plan: ArtifactPlan, stage: str,
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> tuple[str, ...]:
    """Return current direct predecessors after bypassing fully skipped groups."""
    if not isinstance(plan, ArtifactPlan) or plan.digest != _digest(_plan_payload(plan)):
        raise ValueError("invalid plan")
    registry.get_group(stage)
    classifications = {item.artifact_id: item.classification for item in plan.items}
    return _resolved_prerequisites(classifications, stage, registry)


def build_plan(
    preview: PlanPreview, facts: Mapping[str, FactResolution],
    *, required_overlays: tuple[str, ...] = (),
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> ArtifactPlan:
    if not isinstance(preview, PlanPreview) or preview.selected_route not in _ROUTES:
        raise ValueError("invalid plan preview")
    if _ROUTES.index(preview.selected_route) < _ROUTES.index(preview.minimum_route):
        raise ValueError("route is below the policy minimum")
    if set(facts) != set(FACT_CATALOG) or any(
            not isinstance(f, FactResolution) or f.fact_id != key
            or f.subject_digest != preview.subject_digest or not _DIGEST.fullmatch(f.digest)
            for key, f in facts.items()):
        raise ValueError("plan requires eight current canonical facts")
    # Re-evaluate the policy so callers cannot pass a forged advisory preview.
    expected = preview_plan(preview.selected_route, preview.subject_digest,
                            facts, required_overlays=required_overlays,
                            registry=registry)
    if preview != expected:
        raise ValueError("preview does not match the policy")
    fact_digest = _digest({key: facts[key].digest for key in sorted(facts)})
    classifications = {item.artifact_id: item.classification
                       for item in preview.items}
    items: list[PlanItem] = []
    for decision in preview.items:
        if decision.classification not in _CLASSIFICATIONS:
            raise ValueError("unknown artifact classification")
        source_facts = tuple(sorted((
            fact for fact in facts.values()
            if fact.digest in decision.source_fact_digests), key=lambda fact: fact.fact_id))
        evidence_ids = tuple(sorted({evidence for fact in source_facts
                                     for evidence in fact.evidence_digests}))
        group_id = registry.get_artifact(decision.artifact_id).group_id
        items.append(PlanItem(
            artifact_id=decision.artifact_id,
            classification=decision.classification,
            reason_code=decision.reason_code,
            evidence_ref_ids=evidence_ids,
            source_fact_digests=decision.source_fact_digests,
            resolved_prerequisites=_resolved_closure(
                classifications, group_id, registry),
        ))
    shell = ArtifactPlan(1, preview.subject_digest, preview.selected_route,
                         1, preview.policy_version, registry.schema_version,
                         fact_digest, tuple(items), "")
    return ArtifactPlan(**{**shell.__dict__, "digest": _digest(_plan_payload(shell))})


@dataclass(frozen=True)
class PlanCredential:
    schema_version: int
    plan_digest: str
    input_digest: str
    generation_mode: str
    frozen_at: str
    credential_digest: str


def _credential_payload(credential: PlanCredential) -> dict[str, object]:
    return {name: getattr(credential, name) for name in PlanCredential.__dataclass_fields__
            if name not in {"frozen_at", "credential_digest"}}


def _credential_valid(credential: PlanCredential) -> bool:
    return (isinstance(credential, PlanCredential)
            and credential.schema_version == 1
            and isinstance(credential.frozen_at, str) and bool(credential.frozen_at)
            and isinstance(credential.generation_mode, str)
            and credential.generation_mode in {"native", "legacy-static"}
            and isinstance(credential.plan_digest, str)
            and _DIGEST.fullmatch(credential.plan_digest) is not None
            and isinstance(credential.input_digest, str)
            and _DIGEST.fullmatch(credential.input_digest) is not None
            and credential.credential_digest == _digest(_credential_payload(credential)))


def freeze_plan(
    plan: ArtifactPlan, *, input_digest: str, generation_mode: str,
    frozen_at: str,
) -> PlanCredential:
    if not isinstance(plan, ArtifactPlan) or plan.digest != _digest(_plan_payload(plan)):
        raise ValueError("plan digest is invalid")
    _hash(input_digest, "input digest")
    if generation_mode not in {"native", "legacy-static"}:
        raise ValueError("unknown plan generation mode")
    if not isinstance(frozen_at, str) or not frozen_at:
        raise ValueError("freeze time is required")
    shell = PlanCredential(1, plan.digest, input_digest, generation_mode,
                           frozen_at, "")
    return PlanCredential(**{**shell.__dict__, "credential_digest":
                             _digest(_credential_payload(shell))})


@dataclass(frozen=True)
class StageBinding:
    schema_version: int
    stage: str
    gate_digest: str
    plan_credential_digest: str
    doc_hashes: tuple[tuple[str, str], ...]
    skipped_ids: tuple[str, ...]
    prerequisite_fingerprints: tuple[tuple[str, str], ...]
    binding_digest: str


def _binding_payload(binding: StageBinding) -> dict[str, object]:
    return {name: getattr(binding, name) for name in StageBinding.__dataclass_fields__
            if name != "binding_digest"}


def bind_stage(
    plan: ArtifactPlan, credential: PlanCredential, stage: str, *,
    gate_digest: str, doc_hashes: Mapping[str, str],
    prerequisite_fingerprints: Mapping[str, str],
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> StageBinding:
    if not isinstance(plan, ArtifactPlan) or plan.digest != _digest(_plan_payload(plan)):
        raise ValueError("invalid plan")
    if not _credential_valid(credential) or credential.plan_digest != plan.digest:
        raise ValueError("credential does not bind the plan")
    _hash(gate_digest, "stage gate digest")
    group = registry.get_group(stage)
    expected_docs = {registry.get_artifact(item).filename for item in group.artifact_ids
                     if plan.by_id(item).classification != "skipped"}
    if not expected_docs:
        raise ValueError("a fully skipped stage has no gate binding")
    if set(doc_hashes) != expected_docs:
        raise ValueError("binding must hash exactly the participating documents")
    if set(prerequisite_fingerprints) != set(
            resolved_stage_prerequisites(plan, stage, registry)):
        raise ValueError("binding must include exact prerequisite fingerprints")
    skipped = tuple(sorted(item for item in group.artifact_ids
                           if plan.by_id(item).classification == "skipped"))
    shell = StageBinding(1, stage, gate_digest, credential.credential_digest,
                         _hash_map(doc_hashes, "document"), skipped,
                         _hash_map(prerequisite_fingerprints, "prerequisite"), "")
    return StageBinding(**{**shell.__dict__, "binding_digest":
                           _digest(_binding_payload(shell))})


def evaluate_binding(
    binding: StageBinding, plan: ArtifactPlan, credential: PlanCredential, *,
    gate_digest: str, doc_hashes: Mapping[str, str],
    prerequisite_fingerprints: Mapping[str, str],
) -> str:
    if (not isinstance(binding, StageBinding) or binding.schema_version != 1
            or binding.binding_digest != _digest(_binding_payload(binding))):
        return "INVALID"
    if (not isinstance(plan, ArtifactPlan) or plan.digest != _digest(_plan_payload(plan))
            or not _credential_valid(credential)):
        return "INVALID"
    try:
        authored = bind_stage(
            plan, credential, binding.stage, gate_digest=binding.gate_digest,
            doc_hashes=dict(binding.doc_hashes),
            prerequisite_fingerprints=dict(binding.prerequisite_fingerprints),
        )
        if binding != authored:
            return "INVALID"
        current_docs = _hash_map(doc_hashes, "document")
        current_parents = _hash_map(prerequisite_fingerprints, "prerequisite")
    except (ValueError, KeyError, TypeError):
        return "INVALID"
    if (binding.plan_credential_digest != credential.credential_digest
            or credential.plan_digest != plan.digest
            or binding.gate_digest != gate_digest
            or binding.doc_hashes != current_docs
            or binding.prerequisite_fingerprints != current_parents):
        return "STALE"
    return "BOUND"


def _load_canonical_object(raw: bytes, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(raw, bytes) or len(raw) > 2_000_000:
        raise ValueError("plan sidecar must be bounded bytes")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate plan sidecar key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("plan sidecar is malformed") from exc
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise ValueError("plan sidecar has unknown or missing fields")
    if raw != json.dumps(_normal(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8"):
        raise ValueError("plan sidecar is not canonical JSON")
    return value


def _string_tuple(value: object, field: str, *, digests: bool = False) -> tuple[str, ...]:
    if (not isinstance(value, list) or any(
            not isinstance(item, str) or not item
            or (digests and not _DIGEST.fullmatch(item)) for item in value)
            or len(value) != len(set(value))):
        raise ValueError(f"invalid {field}")
    return tuple(value)


def load_plan_bytes(raw: bytes, registry: ArtifactRegistry = ARTIFACT_REGISTRY) -> ArtifactPlan:
    """Decode a persisted plan without accepting unknown decision-critical fields."""
    value = _load_canonical_object(raw, frozenset(ArtifactPlan.__dataclass_fields__))
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["profile_version"]) is not int
            or value["profile_version"] != 1
            or type(value["policy_version"]) is not int
            or value["policy_version"] != 2
            or type(value["registry_version"]) is not int
            or value["registry_version"] != registry.schema_version
            or value["route"] not in _ROUTES):
        raise ValueError("unsupported plan version or route")
    for name in ("subject_digest", "facts_digest", "digest"):
        _hash(value[name], name)
    raw_items = value["items"]
    if not isinstance(raw_items, list) or len(raw_items) != len(registry.artifacts):
        raise ValueError("plan must contain every registered artifact")
    items: list[PlanItem] = []
    for raw_item in raw_items:
        if (not isinstance(raw_item, dict)
                or frozenset(raw_item) != frozenset(PlanItem.__dataclass_fields__)):
            raise ValueError("plan item has unknown or missing fields")
        artifact_id = raw_item["artifact_id"]
        if not isinstance(artifact_id, str) or artifact_id not in registry.artifacts:
            raise ValueError("unknown plan artifact")
        classification = raw_item["classification"]
        reason = raw_item["reason_code"]
        if (not isinstance(classification, str)
                or classification not in _CLASSIFICATIONS
                or not isinstance(reason, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", reason)):
            raise ValueError("invalid plan decision")
        item = PlanItem(
            artifact_id, classification, reason,
            _string_tuple(raw_item["evidence_ref_ids"], "evidence IDs", digests=True),
            _string_tuple(raw_item["source_fact_digests"], "fact digests", digests=True),
            _string_tuple(raw_item["resolved_prerequisites"], "prerequisites"),
        )
        if reason == "FACT_FALSE" and (not item.evidence_ref_ids
                                        or not item.source_fact_digests):
            raise ValueError("false decision lacks evidence")
        items.append(item)
    if tuple(item.artifact_id for item in items) != tuple(registry.artifacts):
        raise ValueError("plan artifact order changed")
    plan = ArtifactPlan(
        1, value["subject_digest"], value["route"], 1, 2,
        registry.schema_version, value["facts_digest"], tuple(items), value["digest"])
    classifications = {item.artifact_id: item.classification for item in plan.items}
    if any(item.resolved_prerequisites != _resolved_closure(
            classifications, registry.get_artifact(item.artifact_id).group_id,
            registry) for item in plan.items):
        raise ValueError("plan prerequisite closure changed")
    if plan.digest != _digest(_plan_payload(plan)):
        raise ValueError("plan digest is invalid")
    return plan


def load_credential_bytes(raw: bytes) -> PlanCredential:
    value = _load_canonical_object(raw, frozenset(PlanCredential.__dataclass_fields__))
    credential = PlanCredential(**value)
    if not _credential_valid(credential):
        raise ValueError("plan credential is invalid")
    return credential


def load_binding_bytes(raw: bytes) -> StageBinding:
    value = _load_canonical_object(raw, frozenset(StageBinding.__dataclass_fields__))
    for name in ("doc_hashes", "prerequisite_fingerprints"):
        pairs = value[name]
        if (not isinstance(pairs, list) or any(
                not isinstance(pair, list) or len(pair) != 2
                or not isinstance(pair[0], str) or not pair[0]
                or not isinstance(pair[1], str) or not _DIGEST.fullmatch(pair[1])
                for pair in pairs)):
            raise ValueError(f"invalid binding {name}")
        mapped = dict(pairs)
        if len(mapped) != len(pairs) or tuple(sorted(mapped.items())) != tuple(
                tuple(pair) for pair in pairs):
            raise ValueError(f"duplicate or unordered binding {name}")
        value[name] = tuple(tuple(pair) for pair in pairs)
    value["skipped_ids"] = _string_tuple(value["skipped_ids"], "skipped IDs")
    binding = StageBinding(**value)
    if (type(binding.schema_version) is not int or binding.schema_version != 1
            or not isinstance(binding.stage, str)
            or binding.stage not in ARTIFACT_REGISTRY.groups
            or not isinstance(binding.gate_digest, str)
            or not _DIGEST.fullmatch(binding.gate_digest)
            or not isinstance(binding.plan_credential_digest, str)
            or not _DIGEST.fullmatch(binding.plan_credential_digest)
            or not isinstance(binding.binding_digest, str)
            or binding.binding_digest != _digest(_binding_payload(binding))):
        raise ValueError("stage binding is invalid")
    return binding
