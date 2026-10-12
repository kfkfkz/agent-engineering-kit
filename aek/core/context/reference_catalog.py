"""Single authority for repo-delivery route/stage reference selection."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


ROUTES = ("direct", "bounded", "standard", "initiative")
STAGES = ("route", "closeout")


@dataclass(frozen=True)
class ReferenceSpec:
    id: str
    source_path: str
    routes: tuple[str, ...]
    stages: tuple[str, ...]
    authority: str

    def __post_init__(self) -> None:
        if not self.id or not self.authority:
            raise ValueError("reference id and authority must be non-empty")
        parts = self.source_path.split("/")
        if (len(parts) != 4 or parts[:3] != ["skills", "repo-delivery", "references"]
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in self.source_path):
            raise ValueError("reference source must stay under repo-delivery/references")
        if not self.source_path.endswith(".md"):
            raise ValueError("reference source must be Markdown")
        if not self.routes or any(route not in ROUTES for route in self.routes):
            raise ValueError("reference has an unknown or empty route set")
        if not self.stages or any(stage not in STAGES for stage in self.stages):
            raise ValueError("reference has an unknown or empty stage set")


class ReferenceCatalog:
    def __init__(self, references: tuple[ReferenceSpec, ...]) -> None:
        by_id: dict[str, ReferenceSpec] = {}
        authorities: set[str] = set()
        for reference in references:
            if reference.id in by_id:
                raise ValueError(f"duplicate reference id: {reference.id}")
            if reference.authority in authorities:
                raise ValueError(f"duplicate authority: {reference.authority}")
            by_id[reference.id] = reference
            authorities.add(reference.authority)
        self._references: Mapping[str, ReferenceSpec] = MappingProxyType(by_id)

    @property
    def references(self) -> Mapping[str, ReferenceSpec]:
        return self._references

    def for_route(self, route: str, stage: str) -> tuple[ReferenceSpec, ...]:
        if route not in ROUTES:
            raise ValueError(f"unknown route: {route}")
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        selected = tuple(
            reference for reference in self._references.values()
            if route in reference.routes and stage in reference.stages
        )
        if not selected:
            raise ValueError(f"route/stage has no reference: {route}/{stage}")
        if stage == "closeout" and tuple(item.id for item in selected) != ("evidence-closeout",):
            raise ValueError(f"closeout reference is invalid: {route}")
        if stage == "route" and any(item.id == "evidence-closeout" for item in selected):
            raise ValueError(f"closeout reference selected too early: {route}")
        return selected


REFERENCE_CATALOG = ReferenceCatalog((
    ReferenceSpec(
        id="direct",
        source_path="skills/repo-delivery/references/direct.md",
        routes=("direct",), stages=("route",),
        authority="repo-delivery.route.direct",
    ),
    ReferenceSpec(
        id="bounded",
        source_path="skills/repo-delivery/references/bounded.md",
        routes=("bounded",), stages=("route",),
        authority="repo-delivery.route.bounded",
    ),
    ReferenceSpec(
        id="standard",
        source_path="skills/repo-delivery/references/standard.md",
        routes=("standard", "initiative"), stages=("route",),
        authority="repo-delivery.route.standard",
    ),
    ReferenceSpec(
        id="initiative",
        source_path="skills/repo-delivery/references/initiative.md",
        routes=("initiative",), stages=("route",),
        authority="repo-delivery.route.initiative",
    ),
    ReferenceSpec(
        id="evidence-closeout",
        source_path="skills/repo-delivery/references/evidence-closeout.md",
        routes=ROUTES, stages=("closeout",),
        authority="repo-delivery.evidence.closeout",
    ),
))


SECURITY_RISKS = ("dependencies", "application", "agent_configuration",
                  "workflow", "baseline")
SECURITY_STAGES = ("design", "execution", "closeout")


@dataclass(frozen=True)
class SecurityReferenceSpec:
    id: str
    source_path: str
    risks: tuple[str, ...]
    authority: str

    def __post_init__(self) -> None:
        parts = self.source_path.split("/")
        if (not self.id or not self.authority or len(parts) != 4
                or parts[:3] != ["skills", "security-review", "references"]
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in self.source_path or not self.source_path.endswith(".md")
                or not self.risks or any(risk not in SECURITY_RISKS for risk in self.risks)):
            raise ValueError("invalid security reference specification")


class SecurityReferenceCatalog:
    """Select only declared risk topics; selection grants no execution rights."""

    def __init__(self, references: tuple[SecurityReferenceSpec, ...]) -> None:
        by_id = {reference.id: reference for reference in references}
        if (len(by_id) != len(references)
                or len({item.authority for item in references}) != len(references)):
            raise ValueError("duplicate security reference identity")
        self.references: Mapping[str, SecurityReferenceSpec] = MappingProxyType(by_id)

    def for_risks(self, risks: tuple[str, ...], stage: str) -> tuple[SecurityReferenceSpec, ...]:
        if (not isinstance(stage, str) or stage not in SECURITY_STAGES
                or not isinstance(risks, tuple)
                or any(not isinstance(risk, str) or risk not in SECURITY_RISKS for risk in risks)):
            raise ValueError("unknown security reference risk or stage")
        return tuple(reference for reference in self.references.values()
                     if set(reference.risks) & set(risks))


SECURITY_REFERENCE_CATALOG = SecurityReferenceCatalog((
    SecurityReferenceSpec("security-dependencies",
                          "skills/security-review/references/dependencies.md",
                          ("dependencies",), "security-review.dependencies"),
    SecurityReferenceSpec("security-source",
                          "skills/security-review/references/source-review.md",
                          ("application", "agent_configuration"), "security-review.source"),
    SecurityReferenceSpec("security-workflow",
                          "skills/security-review/references/workflow.md",
                          ("workflow",), "security-review.workflow"),
    SecurityReferenceSpec("security-baseline",
                          "skills/security-review/references/baseline.md",
                          ("baseline",), "security-review.baseline"),
))
