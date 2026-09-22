"""Pure Artifact classification preview; no dynamic SKIP is published here.

The preview is advisory until T5 binds a validated ArtifactPlan to a subject.
Existing doc-gate behaviour remains authoritative in the meantime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from aek.core.artifact.registry import ARTIFACT_REGISTRY, ArtifactRegistry
from aek.core.planning.facts import (
    FACT_CATALOG, FactResolution, merge_fact_observations, normalize_fact_id,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ROUTES = ("direct", "bounded", "standard", "initiative")
_BASE_REQUIRED = {
    "standard": {"requirements-analysis", "outline-design", "detail-design", "tasks", "test-plan"},
    "initiative": {"requirements-analysis", "outline-design", "business-flow",
                   "detail-design", "tasks", "test-plan"},
}
_CONDITIONAL = {
    "api-design": "public_api_change",
    "database-design": "database_change",
    "ui-design": "ui_behavior_change",
    "business-flow": "business_flow_change",
}
_STANDARD_RISKS = {
    "public_api_change", "cross_module_change", "security_sensitive",
    "irreversible_change", "performance_capacity",
}


@dataclass(frozen=True)
class ArtifactDecision:
    artifact_id: str
    classification: str
    reason_code: str
    source_fact_digests: tuple[str, ...]


@dataclass(frozen=True)
class PlanPreview:
    policy_version: int
    subject_digest: str
    selected_route: str
    minimum_route: str
    items: tuple[ArtifactDecision, ...]
    required_checks: tuple[str, ...]

    def by_id(self, artifact_id: str) -> ArtifactDecision:
        for item in self.items:
            if item.artifact_id == artifact_id:
                return item
        raise KeyError(artifact_id)


def preview_plan(
    route: str, subject_digest: str, facts: Mapping[str, FactResolution],
    *, required_overlays: tuple[str, ...] = (),
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> PlanPreview:
    if route not in _ROUTES:
        raise ValueError(f"unknown route: {route}")
    if not isinstance(subject_digest, str) or not _DIGEST.fullmatch(subject_digest):
        raise ValueError("subject digest must be SHA-256")
    if not isinstance(facts, Mapping):
        raise ValueError("facts must be a mapping")
    normalized: dict[str, FactResolution] = {}
    for raw_id, resolution in facts.items():
        fact_id, _ = normalize_fact_id(raw_id)
        if fact_id in normalized:
            raise ValueError(f"duplicate fact alias: {fact_id}")
        if (not isinstance(resolution, FactResolution)
                or resolution.fact_id != fact_id
                or resolution.subject_digest != subject_digest
                or resolution.state not in {"true", "false", "unknown"}):
            raise ValueError(f"invalid or stale fact resolution: {fact_id}")
        normalized[fact_id] = resolution
    for fact_id in FACT_CATALOG:
        normalized.setdefault(
            fact_id, merge_fact_observations(fact_id, subject_digest, ()))

    if (not isinstance(required_overlays, tuple)
            or any(not isinstance(item, str) for item in required_overlays)
            or len(set(required_overlays)) != len(required_overlays)):
        raise ValueError("required overlays must be unique")
    if any(artifact_id not in registry.artifacts for artifact_id in required_overlays):
        raise ValueError("unknown overlay artifact")
    overlays = set(required_overlays)

    all_false = all(item.state == "false" for item in normalized.values())
    minimum = route
    if route == "direct" and not all_false:
        minimum = "bounded"
    if any(normalized[fact_id].state != "false" for fact_id in _STANDARD_RISKS):
        if _ROUTES.index(minimum) < _ROUTES.index("standard"):
            minimum = "standard"
    if overlays and _ROUTES.index(minimum) < _ROUTES.index("standard"):
        minimum = "standard"

    base = _BASE_REQUIRED.get(route, set())
    items: list[ArtifactDecision] = []
    for artifact_id in registry.artifacts:
        fact_id = _CONDITIONAL.get(artifact_id)
        fact = normalized[fact_id] if fact_id else None
        if artifact_id in overlays:
            classification, reason = "required", "GOVERNANCE_OVERLAY"
        elif fact is not None and fact.state != "false":
            classification = "required"
            reason = "FACT_TRUE" if fact.state == "true" else "FACT_UNKNOWN"
        elif artifact_id in base:
            classification, reason = "required", f"ROUTE_{route.upper()}_BASELINE"
        elif fact is not None:
            classification, reason = "skipped", "FACT_FALSE"
        elif route == "direct" and all_false:
            classification, reason = "skipped", "ROUTE_DIRECT_TARGETED_VERIFICATION"
        elif route == "bounded" and all_false:
            classification, reason = "skipped", "PROFILE_BOUNDED_COMPACT_CARRIER"
        else:
            classification, reason = "skipped", "ROUTE_NOT_REQUIRED"
        items.append(ArtifactDecision(
            artifact_id, classification, reason,
            (fact.digest,) if fact else (),
        ))

    checks: list[str] = []
    if normalized["database_change"].state != "false":
        checks.append("sql-performance-screen")
    if normalized["performance_capacity"].state != "false":
        checks.append("performance-review")
    if normalized["security_sensitive"].state != "false":
        checks.append("security-review")
    if normalized["irreversible_change"].state != "false":
        checks.extend(("rollback-design", "human-review"))
    # v2 makes negative proof paths conditional; a frozen plan must not silently
    # reinterpret a v1 fact bundle under a changed policy.
    return PlanPreview(2, subject_digest, route, minimum, tuple(items), tuple(checks))
