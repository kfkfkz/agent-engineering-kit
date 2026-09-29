"""Normalize governance actions without changing task workload route."""
from __future__ import annotations

from dataclasses import dataclass


LEGACY_MIN_ROUTE_MIGRATION = {
    "direct": {"checks": ("targeted-verification",), "reviews": (),
               "receipts": (), "artifacts": ()},
    "bounded": {"checks": ("acceptance-criteria", "targeted-tests"),
                "reviews": ("quick",), "receipts": (), "artifacts": ()},
    "standard": {"checks": ("impact-analysis", "applicable-design",
                              "targeted-tests", "route-drift-check"),
                 "reviews": ("thorough",), "receipts": (), "artifacts": ()},
    "initiative": {"checks": ("integration-gate", "route-drift-check"),
                   "reviews": ("thorough",), "receipts": (),
                   "artifacts": ("full-sdd", "work-unit-decomposition")},
}


class RequirementError(ValueError):
    """A governance action cannot be migrated without losing intent."""


@dataclass(frozen=True)
class GovernanceRequirements:
    checks: tuple[str, ...]
    reviews: tuple[str, ...]
    receipts: tuple[str, ...]
    artifacts: tuple[str, ...]
    review_depth: str
    receipt_mode: str
    provenance: tuple[tuple[str, str, str], ...]
    deprecated_findings: tuple[dict[str, object], ...]

    def requirements_dict(self) -> dict[str, list[str]]:
        return {"checks": list(self.checks), "reviews": list(self.reviews),
                "receipts": list(self.receipts),
                "artifacts": list(self.artifacts)}

    def provenance_dicts(self) -> list[dict[str, str]]:
        return [{"kind": kind, "requirement": requirement, "source": source}
                for kind, requirement, source in self.provenance]


def normalize_governance_actions(
    actions: list[str], profile: str,
) -> GovernanceRequirements:
    if profile not in {"strict", "lightweight"}:
        raise RequirementError("governance profile is invalid")
    if not isinstance(actions, list) or any(not isinstance(item, str) or not item
                                            for item in actions):
        raise RequirementError("governance actions must be non-empty strings")
    receipt_mode = "formal" if profile == "strict" else "inline"
    values: dict[str, list[str]] = {
        "checks": [], "reviews": [], "receipts": [], "artifacts": []}
    provenance: list[tuple[str, str, str]] = []
    deprecated: list[dict[str, object]] = []

    def add(kind: str, value: str, source: str) -> None:
        if not value:
            raise RequirementError(f"{kind} requirement must not be empty")
        if value not in values[kind]:
            values[kind].append(value)
            provenance.append((kind, value, source))

    add("receipts", receipt_mode, f"profile:{profile}")
    for action in actions:
        if action.startswith("min_route:"):
            route = action.split(":", 1)[1]
            migration = LEGACY_MIN_ROUTE_MIGRATION.get(route)
            if migration is None:
                raise RequirementError(f"legacy min_route is invalid: {route}")
            for kind, requirements in migration.items():
                for requirement in requirements:
                    add(kind, requirement, f"legacy:{action}")
            deprecated.append({
                "code": "DEPRECATED_MIN_ROUTE", "action": action,
                "mapped_requirements": {
                    kind: list(requirements)
                    for kind, requirements in migration.items()},
            })
        elif action.startswith("review_depth:"):
            depth = action.split(":", 1)[1]
            if depth not in {"quick", "thorough"}:
                raise RequirementError(f"review depth is invalid: {depth}")
            add("reviews", depth, action)
        elif action.startswith(("required_check:", "require_check:")):
            add("checks", action.split(":", 1)[1], action)
        elif action.startswith("require_review:"):
            add("reviews", action.split(":", 1)[1], action)
        elif action.startswith("require_receipt:"):
            add("receipts", action.split(":", 1)[1], action)
        elif action.startswith("require_artifact:"):
            add("artifacts", action.split(":", 1)[1], action)
        elif action.startswith("receipt_mode:"):
            mode = action.split(":", 1)[1]
            if mode not in {"inline", "formal"}:
                raise RequirementError(f"receipt mode is invalid: {mode}")
            if mode == "formal":
                receipt_mode = "formal"
            add("receipts", mode, action)
        elif action == "receipt":
            receipt_mode = "formal"
            add("receipts", "formal", action)
        elif action == "independent_review":
            receipt_mode = "formal"
            add("receipts", "formal", action)
            add("reviews", "independent", action)
        elif action == "inline_review":
            add("reviews", "inline", action)
        else:
            # Pre-1.0 policies also used bare check names (for example
            # db_screen). Preserve them as explicit checks.
            add("checks", action, action)
    review_depth = ("thorough" if any(
        item in {"thorough", "independent", "human"}
        for item in values["reviews"]) else "quick")
    return GovernanceRequirements(
        tuple(values["checks"]), tuple(values["reviews"]),
        tuple(values["receipts"]), tuple(values["artifacts"]),
        review_depth, receipt_mode, tuple(provenance), tuple(deprecated))
