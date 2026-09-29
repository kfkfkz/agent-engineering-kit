"""Deterministic route evaluation shared by CLI and future entry points."""
from __future__ import annotations

import re
from typing import Any

from aek.core.context.budget import resolve_context_budget
from aek.core.policy.requirements import (
    RequirementError,
    normalize_governance_actions,
)


ROUTES = ("direct", "bounded", "standard", "initiative")
RISK_OVERLAY_REQUIREMENTS = {
    "public_contract": {"checks": ["contract-review"]},
    "schema_migration": {"checks": ["migration-plan", "rollback-verification",
                                      "sql-performance-screen"]},
    "security_privacy": {"checks": ["security-review"],
                         "reviews": ["independent"]},
    "irreversible": {"checks": ["rollback-verification"],
                     "reviews": ["human"]},
    "cross_service": {"checks": ["integration-test"]},
    "cross_repo": {"checks": ["integration-gate"]},
    "concurrency_consistency": {"checks": ["concurrency-review"]},
    "core_data_flow": {"checks": ["impact-analysis"]},
    "compliance": {"checks": ["compliance-review"],
                   "reviews": ["independent"]},
    "performance_capacity": {"checks": ["performance-review"]},
}
RISK_OVERLAY_CHECKS = {
    risk: list(requirements.get("checks", []))
    for risk, requirements in RISK_OVERLAY_REQUIREMENTS.items()
}
ROUTE_CHECKS = {
    "direct": ["targeted-verification"],
    "bounded": ["acceptance-criteria", "targeted-tests", "quick-review"],
    "standard": ["impact-analysis", "applicable-design", "targeted-tests",
                 "thorough-review", "route-drift-check"],
    "initiative": ["full-sdd", "work-unit-decomposition", "integration-gate",
                   "thorough-review", "route-drift-check"],
}
ROUTE_EXECUTION_PROFILES = {
    "direct": {"context_depth": "minimal", "planning_depth": "none",
               "design_depth": "none", "verification_depth": "targeted",
               "receipt_detail": "compact"},
    "bounded": {"context_depth": "targeted", "planning_depth": "compact",
                "design_depth": "decision_only", "verification_depth": "targeted",
                "receipt_detail": "compact"},
    "standard": {"context_depth": "impact", "planning_depth": "structured",
                 "design_depth": "applicable", "verification_depth": "impact",
                 "receipt_detail": "full"},
    "initiative": {"context_depth": "program", "planning_depth": "work_units",
                   "design_depth": "full_sdd", "verification_depth": "integration",
                   "receipt_detail": "full"},
}

class RoutingError(ValueError):
    """The already-validated card or governance result is inconsistent."""


def _promote(current: str, candidate: str) -> str:
    return ROUTES[max(ROUTES.index(current), ROUTES.index(candidate))]


def _minimum(card: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    footprint = card["footprint"]
    minimum = "direct" if card["behavior_change"] == "none" else "bounded"
    reasons: list[dict[str, str]] = []

    def require(route: str, code: str, message: str) -> None:
        nonlocal minimum
        previous = minimum
        minimum = _promote(minimum, route)
        if minimum != previous or route == minimum:
            reasons.append({"code": code, "message": message, "minimum": route})

    if card["behavior_change"] == "local":
        reasons.append({"code": "LOCAL_BEHAVIOR", "message": "存在局部可观察行为变化",
                        "minimum": "bounded"})
    if card["behavior_change"] == "public":
        require("standard", "PUBLIC_BEHAVIOR", "改变公共可观察行为")
    if card["intent_gaps"] == "minor":
        require("bounded", "MINOR_INTENT_GAPS", "存在需在实施单元内闭合的意图缺口")
    if card["intent_gaps"] == "material":
        require("standard", "INTENT_GAPS", "存在影响结果的意图缺口")
    if card["uncertainty"] == "medium":
        require("bounded", "MEDIUM_UNCERTAINTY", "存在中等不确定性")
    if card["uncertainty"] == "high":
        require("standard", "HIGH_UNCERTAINTY", "实现或影响面存在高不确定性")
    if footprint["modules"] > 1:
        require("standard", "CROSS_MODULE", "影响多个模块")
    if footprint["sessions"] > 1:
        require("standard", "MULTI_SESSION", "预计需要多个实施会话")
    if card["coordination"] == "multi_contributor":
        require("standard", "COORDINATION", "需要多人协调")
    if (footprint["repos"] > 1 or footprint["sessions"] >= 3
            or card["coordination"] == "multi_team"):
        require("initiative", "INITIATIVE_SCALE", "多仓库、较长多会话或多团队协作")
    return minimum, reasons


def _policy(governance: dict[str, Any]) -> dict[str, Any]:
    actions = governance.get("all_actions", [])
    try:
        normalized = normalize_governance_actions(
            actions, governance.get("profile", "strict"))
    except RequirementError as exc:
        raise RoutingError(str(exc)) from exc
    return {
        "review_depth": normalized.review_depth,
        "receipt_mode": normalized.receipt_mode,
        "requirements": normalized.requirements_dict(),
        "deprecated_findings": list(normalized.deprecated_findings),
        "provenance": normalized.provenance_dicts(),
    }


def _matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        escaped = re.escape(pattern).replace(r"\*\*/", "(?:[^/]*/)*")
        escaped = escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        escaped = escaped.replace(r"\?", "[^/]")
        if re.fullmatch(escaped, path) is not None:
            return True
    return False


def evaluate_route(card: dict[str, Any], governance: dict[str, Any]) -> dict[str, Any]:
    """Return the complete, presenter-independent route decision."""
    minimum, reasons = _minimum(card)
    policy = _policy(governance)
    risk_requirements = {
        kind: list(values)
        for kind, values in policy["requirements"].items()
    }
    risk_provenance = list(policy["provenance"])
    for risk in card["risk_overlays"]:
        overlay = RISK_OVERLAY_REQUIREMENTS.get(risk, {})
        for kind, values in overlay.items():
            for value in values:
                if value not in risk_requirements[kind]:
                    risk_requirements[kind].append(value)
                    risk_provenance.append({
                        "kind": kind, "requirement": value,
                        "source": f"risk_overlay:{risk}",
                    })
    review_depth = ("thorough" if any(
        item in {"thorough", "independent", "human"}
        for item in risk_requirements["reviews"])
                    else policy["review_depth"])
    receipt_mode = policy["receipt_mode"]
    selected = card["route"]
    route_valid = ROUTES.index(selected) >= ROUTES.index(minimum)
    override = card.get("route_override")
    if not route_valid:
        route_fit, route_efficient = "too_light", True
    elif ROUTES.index(selected) > ROUTES.index(minimum):
        if override is None:
            route_fit, route_efficient = "too_heavy", False
            reasons.append({"code": "ROUTE_TOO_HEAVY",
                            "message": f"所选 {selected} 高于足够安全的 {minimum}，但没有用户或项目明确要求",
                            "minimum": minimum})
        else:
            route_fit, route_efficient = "overridden", True
            reasons.append({"code": "ROUTE_OVERRIDE",
                            "message": f"按 {override['kind']} 采用高于最低值的 {selected}: {override['reason']}",
                            "minimum": minimum})
    else:
        route_fit, route_efficient = "exact", True
    effective = minimum if route_fit in ("too_light", "too_heavy") else selected
    if not route_valid:
        reasons.append({"code": "ROUTE_TOO_LIGHT",
                        "message": f"所选 {selected} 低于最低安全路线 {minimum}",
                        "minimum": minimum})
    changed = governance.get("changed_files", [])
    if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
        raise RoutingError("governance changed_files 必须是字符串数组")
    unplanned = sorted(path for path in changed
                       if not _matches(path, card["footprint"]["planned_paths"]))
    if unplanned:
        reasons.append({"code": "SCOPE_DRIFT",
                        "message": "最终 diff 含 Route Card 未计划路径: " + ", ".join(unplanned),
                        "minimum": minimum})
    checks: list[str] = []
    for check in (ROUTE_CHECKS[effective] + card["required_checks"]
                  + risk_requirements["checks"]):
        if check not in checks:
            checks.append(check)
    if effective in ("standard", "initiative"):
        review_depth = "thorough"
    return {
        "schema_version": 1, "selected_route": selected,
        "minimum_route": minimum, "recommended_route": minimum,
        "effective_route": effective, "route_valid": route_valid,
        "route_efficient": route_efficient, "route_fit": route_fit,
        "route_override": override, "drift_detected": bool(unplanned),
        "changed_files": sorted(changed), "unplanned_files": unplanned,
        "work_kind": card["work_kind"], "risk_overlays": card["risk_overlays"],
        "execution_profile": dict(ROUTE_EXECUTION_PROFILES[effective]),
        "context_budget": resolve_context_budget(
            effective, "routing", review_depth,
            tuple(card["risk_overlays"])).as_dict(),
        "requirements": {"review_depth": review_depth,
                         "receipt_mode": receipt_mode,
                         "required_checks": checks},
        "route_verdict": {"status": "DECIDED", "minimum": minimum,
                          "effective": effective,
                          "reason_codes": [item["code"] for item in reasons]},
        "governance": {"profile": governance.get("profile", "strict"),
                       "verdict": "DECIDED",
                       "matched_rules": governance.get("matched_rules", []),
                       "required_actions": governance.get("required_actions", []),
                       "risk_requirements": risk_requirements,
                       "provenance": risk_provenance,
                       "deprecated_findings": policy["deprecated_findings"]},
        "reasons": reasons,
        "ok": route_valid and route_efficient and not unplanned,
    }
