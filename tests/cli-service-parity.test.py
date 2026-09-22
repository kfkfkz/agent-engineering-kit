#!/usr/bin/env python3
"""CLI compatibility wrappers and Application services make equal decisions."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class CliServiceParityTests(unittest.TestCase):
    def test_route_application_matches_legacy_oracle(self) -> None:
        module = load("route_eval_parity", ROOT / "route-eval")
        card = {
            "version": 1, "route": "standard", "work_kind": "feature",
            "intent_gaps": "minor", "behavior_change": "public",
            "footprint": {"planned_paths": ["src/**"], "modules": 2,
                          "repos": 1, "sessions": 1},
            "reversibility": "moderate", "risk_overlays": ["performance_capacity"],
            "coordination": "single", "uncertainty": "medium",
            "route_override": None, "required_checks": ["contract-test"],
            "escalate_if": [],
        }
        governance = {
            "profile": "strict", "all_actions": ["required_check:capacity"],
            "changed_files": ["src/orders/query.py"],
            "matched_rules": [], "required_actions": [],
        }
        self.assertEqual(module.evaluate(card, governance),
                         module._legacy_evaluate(card, governance))

    def test_governance_application_matches_legacy_oracle(self) -> None:
        module = load("governance_eval_parity", ROOT / "governance-eval")
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 old
+execute(user_input)
"""
        policy = {"version": 1, "profile": "strict",
                  "default": {"require": ["receipt"]},
                  "rules": [{"id": "SEC", "require": ["independent_review"],
                             "match": {"added_lines_regex": ["execute\\("]}}]}
        self.assertEqual(module.evaluate_diff(diff, policy),
                         module._legacy_evaluate_diff(diff, policy))


if __name__ == "__main__":
    unittest.main()
