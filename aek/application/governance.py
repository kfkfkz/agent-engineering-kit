"""Application boundary for deterministic governance evaluation."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from aek.core.policy.evaluator import GovernanceResult, evaluate_policy
from aek.core.policy.requirements import (
    GovernanceRequirements, RequirementError, normalize_security_checks,
)
from aek.core.security.policy import SecurityPolicy


def evaluate_governance(
    diff_text: str, policy: dict[str, Any], *,
    changed_files: Callable[[str], set[str]],
    added_lines: Callable[[str], list[tuple[str, int, str]]],
    deleted_files: Callable[[str], set[str]],
) -> GovernanceResult:
    """Parse one immutable diff snapshot, then invoke the pure policy judge."""
    if not isinstance(diff_text, str) or not isinstance(policy, dict):
        raise ValueError("governance evaluation needs text and policy")
    return evaluate_policy(
        changed_files(diff_text), added_lines(diff_text),
        deleted_files(diff_text), policy)


def security_requirements(
    policy: SecurityPolicy, *, stage: str,
    dependency_required: bool = False, review_required: bool = False,
) -> GovernanceRequirements:
    """Consume controlled applicability facts, never request JSON booleans."""
    if (not isinstance(policy, SecurityPolicy)
            or type(dependency_required) is not bool
            or type(review_required) is not bool):
        raise RequirementError("security applicability inputs are invalid")
    checks = []
    if dependency_required:
        checks.append("dependency-design-check" if stage == "design"
                      else "dependency-delivery-check")
    if review_required:
        checks.append("security-review")
    if dependency_required or review_required:
        checks.extend(f"security-scan:{tool}" for tool in policy.required_scanners)
    return normalize_security_checks(checks, stage=stage)


def apply_security_requirements(
    route_result: dict[str, Any], requirements: GovernanceRequirements,
) -> dict[str, Any]:
    """Extend the decided route snapshot before original Composer evidence."""
    if (not isinstance(route_result, dict)
            or not isinstance(requirements, GovernanceRequirements)):
        raise RequirementError("security requirement merge inputs are invalid")
    result = deepcopy(route_result)
    try:
        governance = result["governance"]
        if (result["route_verdict"]["status"] != "DECIDED"
                or governance["verdict"] != "DECIDED"):
            raise RequirementError("route and governance must be decided")
        risk = governance["risk_requirements"]
        provenance = governance["provenance"]
        checks = result["requirements"]["required_checks"]
        if (not isinstance(checks, list) or not isinstance(provenance, list)
                or not isinstance(risk, dict)):
            raise RequirementError("route requirements are invalid")
        for kind, values in requirements.requirements_dict().items():
            if not isinstance(risk[kind], list):
                raise RequirementError("governance requirements are invalid")
            for value in values:
                if value not in risk[kind]:
                    risk[kind].append(value)
                    provenance.extend(row for row in requirements.provenance_dicts()
                                      if row["kind"] == kind
                                      and row["requirement"] == value)
                if kind == "checks" and value not in checks:
                    checks.append(value)
    except (KeyError, TypeError) as exc:
        raise RequirementError("route requirements are incomplete") from exc
    return result
