"""Compose the authoritative, identity-bindable Delivery Gate result.

The route evaluator decides workflow depth and governance decides risk
requirements.  This service keeps those verdicts separate while joining every
required action to its policy/fact provenance and current evidence.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aek.application.identity import IdentityBoundReceipt, bind_receipt
from aek.application.review_evidence import ReviewEvidencePlan
from aek.core.context.identity import (
    IdentityComparison,
    IdentityEnvelope,
    IdentityState,
)


_KINDS = ("checks", "reviews", "receipts", "artifacts")
_EVIDENCE_STATES = frozenset({"PASS", "FAIL", "PENDING", "NOT_APPLICABLE"})
_VERDICTS = frozenset({"READY", "NOT_READY", "NEEDS_HUMAN_REVIEW"})


class DeliveryGateError(ValueError):
    """A Delivery Gate input cannot be interpreted without guessing."""


def _string_list(value: object, label: str) -> list[str]:
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)):
        raise DeliveryGateError(f"{label} must be a string list")
    return list(dict.fromkeys(value))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeliveryGateError(f"{label} must be a mapping")
    return value


def _rule_sources(governance: Mapping[str, Any]) -> dict[str, list[str]]:
    rows = governance.get("matched_rules")
    if not isinstance(rows, list):
        raise DeliveryGateError("governance matched_rules must be a list")
    sources: dict[str, list[str]] = {}
    for row in rows:
        item = _mapping(row, "matched rule")
        rule_id = item.get("rule_id")
        required = item.get("required")
        if (not isinstance(rule_id, str) or not rule_id
                or not isinstance(required, list)
                or any(not isinstance(action, str) or not action
                       for action in required)):
            raise DeliveryGateError("matched rule schema is invalid")
        for action in required:
            sources.setdefault(action, []).append(rule_id)
            sources.setdefault(f"legacy:{action}", []).append(rule_id)
    return sources


def _provenance(governance: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    rows = governance.get("provenance")
    if not isinstance(rows, list):
        raise DeliveryGateError("governance provenance must be a list")
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        item = _mapping(row, "governance provenance")
        kind = item.get("kind")
        requirement = item.get("requirement")
        source = item.get("source")
        if (kind not in _KINDS or not isinstance(requirement, str)
                or not requirement or not isinstance(source, str)
                or not source):
            raise DeliveryGateError("governance provenance schema is invalid")
        result.setdefault((kind, requirement), source)
    return result


def _requirement_origins(
    *, kind: str, requirement: str, source: str | None,
    effective_route: str, profile: str,
    rule_sources: Mapping[str, list[str]],
) -> tuple[list[str], list[str]]:
    if source is None:
        origin = f"route:{effective_route}"
        return [origin], [origin]
    rule_ids = list(dict.fromkeys(rule_sources.get(source, ())))
    if rule_ids:
        return rule_ids, [f"rule_match:{rule_id}" for rule_id in rule_ids]
    if source.startswith("profile:"):
        return [source], [source]
    if source.startswith("risk_overlay:"):
        return ["kit:risk-overlay"], [source]
    if source.startswith("legacy:"):
        return ["kit:legacy-migration"], [source]
    if source == requirement or source.endswith(f":{requirement}"):
        policy = f"profile:{profile}"
        return [policy], [f"governance_action:{source}"]
    raise DeliveryGateError(
        f"cannot resolve provenance for {kind}:{requirement}")


def _evidence_row(
    requirement_id: str,
    evidence_by_requirement: Mapping[str, object],
) -> tuple[str, list[str]]:
    raw = evidence_by_requirement.get(requirement_id, {
        "status": "PENDING", "evidence_refs": []})
    item = _mapping(raw, f"evidence {requirement_id}")
    if set(item) != {"status", "evidence_refs"}:
        raise DeliveryGateError(
            f"evidence {requirement_id} has unknown or missing fields")
    status = item.get("status")
    if status not in _EVIDENCE_STATES:
        raise DeliveryGateError(f"evidence {requirement_id} status is invalid")
    refs = _string_list(item.get("evidence_refs"),
                        f"evidence {requirement_id} refs")
    if status == "PASS" and not refs:
        raise DeliveryGateError(
            f"passing evidence {requirement_id} needs a reference")
    return status, refs


def compose_delivery_gate(
    *, route_result: Mapping[str, object], identity: IdentityComparison,
    review_plan: ReviewEvidencePlan,
    evidence_by_requirement: Mapping[str, object],
) -> dict[str, object]:
    """Return the deterministic machine result for one immutable snapshot."""
    if (not isinstance(route_result, Mapping)
            or route_result.get("schema_version") != 1
            or not isinstance(identity, IdentityComparison)
            or not isinstance(review_plan, ReviewEvidencePlan)
            or not isinstance(evidence_by_requirement, Mapping)):
        raise DeliveryGateError("delivery gate inputs are invalid")
    effective_route = route_result.get("effective_route")
    if not isinstance(effective_route, str) or not effective_route:
        raise DeliveryGateError("effective route is invalid")
    route_verdict = _mapping(route_result.get("route_verdict"),
                             "route verdict")
    governance = _mapping(route_result.get("governance"), "governance")
    if route_verdict.get("status") != "DECIDED" \
            or governance.get("verdict") != "DECIDED":
        raise DeliveryGateError("route and governance must both be decided")
    profile = governance.get("profile")
    if not isinstance(profile, str) or not profile:
        raise DeliveryGateError("governance profile is invalid")

    requirements = _mapping(route_result.get("requirements"), "requirements")
    values: dict[str, list[str]] = {
        "checks": _string_list(requirements.get("required_checks"),
                               "required checks")}
    risk = _mapping(governance.get("risk_requirements"),
                    "risk requirements")
    for kind in ("reviews", "receipts", "artifacts"):
        values[kind] = _string_list(risk.get(kind), f"risk {kind}")
    # Governance checks are expected in required_checks, but union them here so
    # a malformed presenter cannot silently drop a policy requirement.
    for requirement in _string_list(risk.get("checks"), "risk checks"):
        if requirement not in values["checks"]:
            values["checks"].append(requirement)

    provenance = _provenance(governance)
    rule_sources = _rule_sources(governance)
    expected_ids = {
        f"{kind}:{requirement}"
        for kind in _KINDS for requirement in values[kind]
    }
    unknown_evidence = sorted(set(evidence_by_requirement) - expected_ids)
    if unknown_evidence:
        raise DeliveryGateError(
            "evidence names unknown requirements: " + ", ".join(unknown_evidence))

    rows: list[dict[str, object]] = []
    pending: list[str] = []
    failed: list[str] = []
    for kind in _KINDS:
        for requirement in values[kind]:
            requirement_id = f"{kind}:{requirement}"
            policy_ids, fact_ids = _requirement_origins(
                kind=kind, requirement=requirement,
                source=provenance.get((kind, requirement)),
                effective_route=effective_route, profile=profile,
                rule_sources=rule_sources)
            status, refs = _evidence_row(
                requirement_id, evidence_by_requirement)
            rows.append({
                "requirement_id": requirement_id,
                "kind": kind,
                "requirement": requirement,
                "policy_ids": policy_ids,
                "fact_ids": fact_ids,
                "evidence_refs": refs,
                "status": status,
            })
            if status == "PENDING":
                pending.append(requirement_id)
            elif status != "PASS":
                failed.append(requirement_id)

    reasons: list[str] = []
    if route_result.get("ok") is not True:
        reasons.append("ROUTE_OR_SCOPE_NOT_READY")
    if identity.state != IdentityState.VALID:
        reasons.extend(identity.reason_codes or (f"IDENTITY_{identity.state.value}",))
    if not review_plan.ready_for_review:
        reasons.extend(review_plan.blocking_reasons)
    if failed:
        reasons.append("REQUIREMENT_FAILED")
    if pending:
        reasons.append("REQUIREMENT_PENDING")

    human_pending = any(item in {
        "reviews:independent", "reviews:human"} for item in pending)
    if reasons:
        verdict = ("NEEDS_HUMAN_REVIEW" if human_pending
                   and not failed and route_result.get("ok") is True
                   and identity.state == IdentityState.VALID
                   and review_plan.ready_for_review else "NOT_READY")
    else:
        verdict = "READY"

    return {
        "schema_version": 1,
        "verdict": verdict,
        "route_verdict": dict(route_verdict),
        "governance_verdict": governance["verdict"],
        "identity": {
            "state": identity.state.value,
            "changed": list(identity.changed),
            "missing": list(identity.missing),
            "reason_codes": list(identity.reason_codes),
        },
        "review_evidence": review_plan.as_dict(),
        "requirements": rows,
        "pending_requirements": pending,
        "failed_requirements": failed,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def bind_delivery_gate_receipt(
    identity: IdentityEnvelope, result: Mapping[str, object],
) -> IdentityBoundReceipt:
    """Bind a machine gate result to the exact release-candidate identity."""
    if (not isinstance(result, Mapping) or result.get("schema_version") != 1
            or result.get("verdict") not in _VERDICTS):
        raise DeliveryGateError("delivery gate result is invalid")
    return bind_receipt(identity, result)
