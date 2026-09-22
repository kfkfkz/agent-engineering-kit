"""Deterministic route evaluation shared by CLI and future entry points."""
from __future__ import annotations

import re
from typing import Any

from aek.core.context.budget import resolve_context_budget


ROUTES = ("direct", "bounded", "standard", "initiative")
RISK_OVERLAY_CHECKS = {"performance_capacity": ["performance-review"]}
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
    if card["reversibility"] == "moderate":
        require("bounded", "MODERATE_REVERSIBILITY", "回退需要明确步骤")
    if card["reversibility"] == "hard":
        require("standard", "HARD_TO_REVERSE", "变更难以回滚")
    if footprint["modules"] > 1:
        require("standard", "CROSS_MODULE", "影响多个模块")
    if footprint["sessions"] > 1:
        require("standard", "MULTI_SESSION", "预计需要多个实施会话")
    if card["coordination"] == "multi_contributor":
        require("standard", "COORDINATION", "需要多人协调")
    for risk in card["risk_overlays"]:
        if risk == "cross_repo":
            require("initiative", "RISK_CROSS_REPO", "跨仓库风险覆盖项")
        else:
            require("standard", f"RISK_{risk.upper()}", f"风险覆盖项: {risk}")
    if (footprint["repos"] > 1 or footprint["sessions"] >= 3
            or card["coordination"] == "multi_team"):
        require("initiative", "INITIATIVE_SCALE", "多仓库、较长多会话或多团队协作")
    return minimum, reasons


def _policy(governance: dict[str, Any]) -> tuple[str | None, str, str, list[str]]:
    minimum = None
    review_depth = "quick"
    receipt_mode = "formal" if governance.get("profile", "strict") == "strict" else "inline"
    checks: list[str] = []
    actions = governance.get("all_actions", [])
    if not isinstance(actions, list):
        raise RoutingError("governance all_actions 必须是数组")
    for action in actions:
        if not isinstance(action, str):
            raise RoutingError("governance action 必须是字符串")
        if action.startswith("min_route:"):
            candidate = action.split(":", 1)[1]
            if candidate not in ROUTES:
                raise RoutingError(f"治理动作的 min_route 非法: {candidate}")
            minimum = candidate if minimum is None else _promote(minimum, candidate)
        elif action.startswith("review_depth:"):
            depth = action.split(":", 1)[1]
            if depth not in ("quick", "thorough"):
                raise RoutingError(f"治理动作的 review_depth 非法: {depth}")
            if depth == "thorough":
                review_depth = depth
        elif action.startswith("required_check:"):
            check = action.split(":", 1)[1]
            if not check:
                raise RoutingError("required_check 动作不得为空")
            if check not in checks:
                checks.append(check)
        elif action.startswith("receipt_mode:"):
            mode = action.split(":", 1)[1]
            if mode not in ("inline", "formal"):
                raise RoutingError(f"治理动作的 receipt_mode 非法: {mode}")
            if mode == "formal":
                receipt_mode = mode
        elif action in ("receipt", "independent_review"):
            receipt_mode = "formal"
            if action == "independent_review":
                review_depth = "thorough"
    return minimum, review_depth, receipt_mode, checks


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
    policy_min, review_depth, receipt_mode, policy_checks = _policy(governance)
    if policy_min is not None:
        previous = minimum
        minimum = _promote(minimum, policy_min)
        if minimum != previous:
            reasons.append({"code": "GOVERNANCE_MIN_ROUTE",
                            "message": f"治理规则要求至少 {policy_min}",
                            "minimum": policy_min})
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
    risk_checks = [check for risk in card["risk_overlays"]
                   for check in RISK_OVERLAY_CHECKS.get(risk, [])]
    for check in ROUTE_CHECKS[effective] + risk_checks + card["required_checks"] + policy_checks:
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
        "governance": {"profile": governance.get("profile", "strict"),
                       "matched_rules": governance.get("matched_rules", []),
                       "required_actions": governance.get("required_actions", [])},
        "reasons": reasons,
        "ok": route_valid and route_efficient and not unplanned,
    }
