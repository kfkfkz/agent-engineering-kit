"""Three-state ImpactFact catalog and order-independent evidence merge.

Adapters must verify evidence bytes, scope and freshness before constructing an
observation. The Core still binds observations to one subject and known producer.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATES = {"true", "false", "unknown"}
_ALIASES = MappingProxyType({
    "api_change": "public_api_change",
    "ui_change": "ui_behavior_change",
    "workflow_change": "business_flow_change",
    "cross_module": "cross_module_change",
})


@dataclass(frozen=True)
class FactSpec:
    id: str
    required_producers: tuple[str, ...]
    # A complete negative proof may take one of these bounded paths. This is
    # not a missing-producer waiver: every producer in the chosen path must
    # explicitly attest false over the current subject and complete scope.
    negative_producer_sets: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if not self.required_producers or len(set(self.required_producers)) != len(self.required_producers):
            raise ValueError("fact producers must be unique and non-empty")
        if self.negative_producer_sets and any(
            not group or not set(group) <= set(self.required_producers)
            or len(set(group)) != len(group)
            for group in self.negative_producer_sets
        ):
            raise ValueError("negative proof path contains unknown or duplicate producer")


FACT_CATALOG: Mapping[str, FactSpec] = MappingProxyType({
    item.id: item for item in (
        FactSpec("public_api_change", ("diff_scanner", "api_surface_parser")),
        FactSpec("database_change", ("diff_scanner", "sql_config_scanner")),
        FactSpec("ui_behavior_change", ("diff_scanner", "ui_path_registry")),
        FactSpec("business_flow_change", ("diff_scanner", "trace_analyzer")),
        FactSpec("cross_module_change", ("diff_scanner", "trace_analyzer")),
        FactSpec("security_sensitive", ("security_rules", "diff_scanner")),
        FactSpec("irreversible_change", (
            "migration_operation_scanner", "human_credential"),
            (("migration_operation_scanner",), ("human_credential",))),
        FactSpec("performance_capacity", (
            "access_change_scanner", "sql_performance_screen",
            "capacity_declaration", "trace_analyzer"),
            (("access_change_scanner",),
             ("sql_performance_screen", "capacity_declaration", "trace_analyzer"))),
    )
})


def normalize_fact_id(fact_id: str) -> tuple[str, bool]:
    if not isinstance(fact_id, str):
        raise ValueError("fact ID must be a string")
    normalized = _ALIASES.get(fact_id, fact_id)
    if normalized not in FACT_CATALOG:
        raise ValueError(f"unknown fact ID: {fact_id}")
    return normalized, normalized != fact_id


@dataclass(frozen=True)
class FactObservation:
    fact_id: str
    producer_id: str
    state: str
    subject_digest: str
    evidence_digest: str
    scope_complete: bool

    def __post_init__(self) -> None:
        normalize_fact_id(self.fact_id)
        if not isinstance(self.producer_id, str) or not self.producer_id:
            raise ValueError("producer ID must be non-empty")
        if not isinstance(self.state, str) or self.state not in _STATES:
            raise ValueError(f"unknown fact state: {self.state}")
        if not isinstance(self.subject_digest, str) or not _DIGEST.fullmatch(self.subject_digest):
            raise ValueError("subject digest must be SHA-256")
        if not isinstance(self.evidence_digest, str) or not _DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("evidence digest must be SHA-256")
        if type(self.scope_complete) is not bool:
            raise ValueError("scope_complete must be boolean")


@dataclass(frozen=True)
class FactResolution:
    fact_id: str
    state: str
    subject_digest: str
    evidence_digests: tuple[str, ...]
    diagnostics: tuple[str, ...]
    digest: str


def merge_fact_observations(
    fact_id: str, subject_digest: str,
    observations: tuple[FactObservation, ...],
) -> FactResolution:
    normalized, alias_used = normalize_fact_id(fact_id)
    if not isinstance(subject_digest, str) or not _DIGEST.fullmatch(subject_digest):
        raise ValueError("subject digest must be SHA-256")
    if not isinstance(observations, tuple):
        raise ValueError("observations must be an immutable tuple")
    spec = FACT_CATALOG[normalized]
    by_producer: dict[str, FactObservation] = {}
    diagnostics: set[str] = set()
    if alias_used:
        diagnostics.add("DEPRECATED_ALIAS")
    for item in observations:
        if not isinstance(item, FactObservation):
            raise ValueError("observation must be a FactObservation")
        item_fact_id, item_alias = normalize_fact_id(item.fact_id)
        if item_alias:
            diagnostics.add("DEPRECATED_ALIAS")
        if item_fact_id != normalized:
            raise ValueError("observation belongs to another fact")
        if item.producer_id not in spec.required_producers:
            raise ValueError(f"unregistered producer: {item.producer_id}")
        if item.subject_digest != subject_digest:
            raise ValueError("observation subject is stale or mismatched")
        if item.producer_id in by_producer:
            raise ValueError(f"duplicate producer: {item.producer_id}")
        by_producer[item.producer_id] = item

    has_true = any(item.state == "true" for item in by_producer.values())
    has_false = any(
        item.state == "false" and item.scope_complete
        for item in by_producer.values())
    negative_paths = spec.negative_producer_sets or (spec.required_producers,)
    all_false = any(all(
        producer in by_producer
        and by_producer[producer].state == "false"
        and by_producer[producer].scope_complete
        for producer in path) for path in negative_paths)
    if has_true:
        state = "true"
        if has_false:
            diagnostics.add("TRUE_FALSE_CONFLICT")
    elif all_false:
        state = "false"
    else:
        state = "unknown"

    ordered = [by_producer[key] for key in sorted(by_producer)]
    evidence_digests = tuple(sorted({item.evidence_digest for item in ordered}))
    canonical = {
        "fact_id": normalized,
        "subject_digest": subject_digest,
        "state": state,
        "observations": [
            {"producer_id": item.producer_id, "state": item.state,
             "evidence_digest": item.evidence_digest,
             "scope_complete": item.scope_complete}
            for item in ordered
        ],
    }
    digest = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return FactResolution(
        normalized, state, subject_digest, evidence_digests,
        tuple(sorted(diagnostics)), digest)
