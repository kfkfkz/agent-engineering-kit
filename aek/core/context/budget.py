"""Deterministic default context budget for a routed engineering task.

This policy limits routine expansion, not mandatory evidence: user/root rules,
governance and safety material remain outside truncation limits.
"""
from __future__ import annotations

from dataclasses import dataclass

from aek.core.context.reference_catalog import REFERENCE_CATALOG


_DEFAULTS = {
    # scope, code files/bytes, Memory candidates/open/bytes, stage bytes, docs
    "direct": ("target_and_tests", 6, 96 * 1024, 0, 0, 0,
               128 * 1024, "target_only"),
    "bounded": ("target_module_and_tests", 20, 256 * 1024, 5, 2, 48 * 1024,
                384 * 1024, "current_artifact_only"),
    "standard": ("impact_closure", 60, 768 * 1024, 8, 3, 96 * 1024,
                 1024 * 1024, "capsule_and_current_artifact"),
    "initiative": ("work_unit_closure", 100, 1536 * 1024, 12, 5, 160 * 1024,
                   2 * 1024 * 1024, "capsule_and_current_artifact"),
}
_STAGES = {"routing", "design", "execution", "closeout"}
_MANDATORY = ("user_request", "root_instructions", "governance", "safety_required")
_EXPANSION_PURPOSES = {
    "target_evidence", "risk_required", "review_required", "user_requested",
    "reroute_evidence",
}
_OPTIONAL_SOURCES = {"target_code", "test", "memory", "document", "review",
                     "reference", "tool", "capsule"}


@dataclass(frozen=True)
class ContextBudget:
    schema_version: int
    route: str
    stage: str
    code_scope: str
    code_file_limit: int
    code_byte_limit: int
    memory_candidate_limit: int
    memory_expand_limit: int
    memory_byte_limit: int
    stage_byte_limit: int
    document_policy: str
    required_reference_ids: tuple[str, ...]
    review_depth: str
    duplicate_read_policy: str
    over_budget_action: str
    mandatory_sources: tuple[str, ...]
    risk_overlays: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "route": self.route,
            "stage": self.stage,
            "code_scope": self.code_scope,
            "code_file_limit": self.code_file_limit,
            "code_byte_limit": self.code_byte_limit,
            "memory_candidate_limit": self.memory_candidate_limit,
            "memory_expand_limit": self.memory_expand_limit,
            "memory_byte_limit": self.memory_byte_limit,
            "stage_byte_limit": self.stage_byte_limit,
            "document_policy": self.document_policy,
            "required_reference_ids": list(self.required_reference_ids),
            "review_depth": self.review_depth,
            "duplicate_read_policy": self.duplicate_read_policy,
            "over_budget_action": self.over_budget_action,
            "mandatory_sources": list(self.mandatory_sources),
            "risk_overlays": list(self.risk_overlays),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class ExpansionDecision:
    action: str
    source: str
    purpose: str | None
    new_total_bytes: int
    over_budget: bool
    finding: str | None


def decide_context_expansion(
    budget: ContextBudget, *, used_bytes: int, requested_bytes: int,
    source: str, purpose: str | None = None,
) -> ExpansionDecision:
    """Decide an explicit soft-budget expansion; the Ledger remains authoritative.

    Required instructions and safety evidence are never truncated. Optional
    material over the route limit needs a controlled reason or a reroute.
    """
    if not isinstance(budget, ContextBudget):
        raise ValueError("invalid context budget")
    if (type(used_bytes) is not int or used_bytes < 0
            or type(requested_bytes) is not int or requested_bytes < 0):
        raise ValueError("context byte counts must be non-negative integers")
    if (not isinstance(source, str)
            or source not in _OPTIONAL_SOURCES | set(budget.mandatory_sources)):
        raise ValueError("unknown context source")
    if purpose is not None and (not isinstance(purpose, str)
                                or purpose not in _EXPANSION_PURPOSES):
        raise ValueError("unknown or unsafe expansion purpose")
    total = used_bytes + requested_bytes
    over = total > budget.stage_byte_limit
    if source in budget.mandatory_sources:
        return ExpansionDecision("ALLOW_MANDATORY", source, purpose, total, over,
                                 "MANDATORY_CONTEXT_OVER_BUDGET" if over else None)
    if not over:
        return ExpansionDecision("ALLOW", source, purpose, total, False, None)
    if purpose is None:
        return ExpansionDecision("REQUIRE_PURPOSE_OR_REROUTE", source, None,
                                 total, True, "CONTEXT_OVER_BUDGET")
    return ExpansionDecision("ALLOW_WITH_FINDING", source, purpose, total,
                             True, "REASONED_CONTEXT_EXPANSION")


def resolve_context_budget(
    route: str, stage: str, review_depth: str,
    risk_overlays: tuple[str, ...] = (),
) -> ContextBudget:
    """Return a versioned default; reasoned expansion is handled by the ledger."""
    if route not in _DEFAULTS:
        raise ValueError(f"unknown route: {route}")
    if stage not in _STAGES:
        raise ValueError(f"unknown stage: {stage}")
    if review_depth not in {"quick", "thorough"}:
        raise ValueError(f"unknown review depth: {review_depth}")
    if not isinstance(risk_overlays, tuple) or any(
        not isinstance(value, str) or not value for value in risk_overlays
    ) or len(set(risk_overlays)) != len(risk_overlays):
        raise ValueError("risk overlays must be unique non-empty strings")
    (code_scope, code_files, code_bytes, candidates, expanded, memory_bytes,
     stage_bytes, docs) = _DEFAULTS[route]
    reference_stage = "closeout" if stage == "closeout" else "route"
    required_references = tuple(
        item.id for item in REFERENCE_CATALOG.for_route(route, reference_stage))
    mandatory_sources = _MANDATORY + tuple(
        f"risk_overlay:{item}" for item in sorted(risk_overlays))
    return ContextBudget(
        schema_version=1,
        route=route,
        stage=stage,
        code_scope=code_scope,
        code_file_limit=code_files,
        code_byte_limit=code_bytes,
        memory_candidate_limit=candidates,
        memory_expand_limit=expanded,
        memory_byte_limit=memory_bytes,
        stage_byte_limit=stage_bytes,
        document_policy=docs,
        required_reference_ids=required_references,
        review_depth=review_depth,
        duplicate_read_policy="count_every_delivery",
        over_budget_action="require_purpose_or_reroute",
        mandatory_sources=mandatory_sources,
        risk_overlays=tuple(sorted(risk_overlays)),
        reason_codes=("ROUTE_DEFAULT", "GOVERNANCE_REVIEW_DEPTH") +
        (("RISK_OVERLAY",) if risk_overlays else ()),
    )
