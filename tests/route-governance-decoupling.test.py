#!/usr/bin/env python3
"""Task workload route and change-risk governance remain orthogonal."""
from __future__ import annotations

import unittest

from aek.application.routing import RoutingError, evaluate_route


def card(*, route: str = "bounded", behavior: str = "local",
         modules: int = 1, sessions: int = 1,
         risks: list[str] | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "route": route,
        "work_kind": "feature",
        "intent_gaps": "none",
        "behavior_change": behavior,
        "footprint": {"planned_paths": ["src/**"], "modules": modules,
                      "repos": 1, "sessions": sessions},
        "reversibility": "easy",
        "risk_overlays": risks or [],
        "coordination": "single",
        "uncertainty": "low",
        "route_override": None,
        "required_checks": [],
        "escalate_if": [],
    }


def governance(actions: list[str]) -> dict[str, object]:
    return {
        "profile": "strict",
        "all_actions": actions,
        "required_actions": actions,
        "matched_rules": [{"rule_id": "legacy", "required": actions}],
        "changed_files": ["src/auth.py"],
    }


class RouteGovernanceDecouplingTests(unittest.TestCase):
    def test_low_work_high_risk_keeps_route_and_adds_requirements(self) -> None:
        result = evaluate_route(card(), governance([
            "receipt", "independent_review", "min_route:standard",
            "required_check:security-review",
        ]))
        self.assertEqual(result["minimum_route"], "bounded")
        self.assertEqual(result["effective_route"], "bounded")
        self.assertTrue(result["route_valid"])
        self.assertEqual(result["execution_profile"]["planning_depth"], "compact")
        self.assertEqual(result["requirements"]["review_depth"], "thorough")
        self.assertIn("security-review", result["requirements"]["required_checks"])
        self.assertIn("impact-analysis", result["requirements"]["required_checks"])
        self.assertEqual(result["governance"]["verdict"], "DECIDED")
        self.assertEqual(
            result["governance"]["deprecated_findings"][0]["action"],
            "min_route:standard",
        )
        self.assertNotIn(
            "GOVERNANCE_MIN_ROUTE",
            {reason["code"] for reason in result["reasons"]},
        )

    def test_risk_overlay_does_not_change_workload_route(self) -> None:
        result = evaluate_route(
            card(risks=["security_privacy"]), governance([]))
        self.assertEqual(result["minimum_route"], "bounded")
        self.assertEqual(result["effective_route"], "bounded")
        self.assertIn("security-review", result["requirements"]["required_checks"])

    def test_high_work_low_risk_keeps_decomposition_without_fake_risk(self) -> None:
        result = evaluate_route(
            card(route="standard", modules=2), governance([]))
        self.assertEqual(result["minimum_route"], "standard")
        self.assertEqual(result["effective_route"], "standard")
        self.assertEqual(result["governance"]["risk_requirements"], {
            "checks": [], "reviews": [], "receipts": ["formal"],
            "artifacts": [],
        })
        self.assertNotIn("security-review", result["requirements"]["required_checks"])

    def test_unknown_legacy_min_route_fails_closed(self) -> None:
        with self.assertRaises(RoutingError):
            evaluate_route(card(), governance(["min_route:extreme"]))


if __name__ == "__main__":
    unittest.main()
