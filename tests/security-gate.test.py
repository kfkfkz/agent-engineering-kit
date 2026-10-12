#!/usr/bin/env python3
"""Version-locked security policies compose through the original Delivery Gate."""
from __future__ import annotations

import ast
import copy
import json
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "8641ce8ed16b3eee9cda00506a10434fa3bfbe66"
CLI = runpy.run_path(str(ROOT / "governance-eval"))
from aek.application import governance as governance_service
from aek.application.delivery_gate import DeliveryGateError, compose_delivery_gate
from aek.application.review_evidence import ReviewEvidencePlan
from aek.application.routing import evaluate_route
from aek.core.context.identity import IdentityComparison, IdentityState
from aek.core.security.policy import SecurityPolicy
from installer import governance as installer_governance


def security_config():
    return {"schema_version": 1, "default_scope": "targeted",
            "dynamic_verification": "disabled", "required_scanners": [],
            "optional_scanners": ["trivy", "semgrep", "gitleaks"],
            "max_duration_seconds": 300, "max_output_bytes": 2000000,
            "max_items": 10000, "max_advisory_age_days": 7}


def v2_policy():
    return {"version": 2, "rules": [
        {"id": "SECURITY-CAPABILITY", "require": {
            "schema_version": 1, "capabilities": ["security-review-v1"],
            "actions": []}},
        {"id": "TEAM", "require": ["required_check:team-check"],
         "match": {"files": ["*.py"]}}],
        "security_review": security_config()}


def original_103_loader():
    """Execute the unmodified pinned real functions, never a rewritten parser."""
    def source(path):
        return subprocess.run(["git", "show", f"{BASELINE}:{path}"],
                              cwd=ROOT, check=True, text=True,
                              capture_output=True).stdout

    module = types.ModuleType("_aek_103_real_requirements")
    sys.modules[module.__name__] = module
    exec(compile(source("aek/core/policy/requirements.py"),
                 "1.0.3/requirements.py", "exec"), module.__dict__)
    tree = ast.parse(source("governance-eval"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "load_policy")
    defaults = next(node for node in tree.body
                    if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "PROFILE_DEFAULTS")
    namespace = {"Path": Path, "Any": object, "json": json,
                 "PolicyError": CLI["PolicyError"],
                 "RequirementError": module.RequirementError,
                 "normalize_governance_actions": module.normalize_governance_actions}
    exec(compile(ast.Module(body=[defaults, function], type_ignores=[]),
                 "1.0.3/governance-eval", "exec"), namespace)
    return namespace["load_policy"], module.normalize_governance_actions


class SecurityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy_path = Path(self.temp.name) / "governance.json"

    def load(self, value):
        self.policy_path.write_text(json.dumps(value), encoding="utf-8")
        return CLI["load_policy"](self.policy_path)

    def test_v2_guard_is_decoded_and_keeps_regular_rules(self):
        decoded = self.load(v2_policy())
        self.assertEqual(decoded["version"], 2)
        self.assertEqual(decoded["security_review"], security_config())
        result = CLI["evaluate_diff"](
            "--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-a\n+b\n", decoded)
        self.assertIn("team-check", result.risk_requirements["checks"])
        self.assertNotIn("schema_version", result.all_actions)

    def test_real_cli_accepts_supported_v2_and_rejects_missing_guard_without_writes(self):
        marker_dir = Path(self.temp.name) / ".repo-memory-kit"
        marker_dir.mkdir()
        policy_path = marker_dir / "governance.json"
        policy = v2_policy()
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        before = policy_path.read_bytes()
        argv = [sys.executable, str(ROOT / "governance-eval"),
                self.temp.name, "--json"]
        accepted = subprocess.run(argv, input="--- a/x.py\n+++ b/x.py\n",
                                  text=True, capture_output=True, check=False)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("team-check", json.loads(accepted.stdout)["risk_requirements"]["checks"])
        self.assertEqual(policy_path.read_bytes(), before)
        policy["rules"] = policy["rules"][1:]
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        before = policy_path.read_bytes()
        refused = subprocess.run(argv, input="", text=True,
                                 capture_output=True, check=False)
        self.assertEqual(refused.returncode, 3)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(policy_path.read_bytes(), before)

    def test_explicit_project_policy_can_change_advisory_age_within_bound(self):
        policy = v2_policy()
        policy["security_review"]["max_advisory_age_days"] = 30
        self.assertEqual(self.load(policy)["security_review"]
                         ["max_advisory_age_days"], 30)

    def test_v2_rejects_unknown_nested_fields_and_duplicate_keys(self):
        mutations = []
        value = v2_policy()
        value["rules"][0]["require"]["unknown"] = True
        mutations.append(value)
        value = v2_policy()
        value["rules"][1]["match"]["unknown"] = ["*.py"]
        mutations.append(value)
        value = v2_policy()
        value["rules"][1]["unknown"] = True
        mutations.append(value)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(CLI["PolicyError"]):
                self.load(value)
        raw = json.dumps(v2_policy()).replace('"max_items": 10000',
                                              '"max_items": 1, "max_items": 10000')
        self.policy_path.write_text(raw, encoding="utf-8")
        with self.assertRaises(CLI["PolicyError"]):
            CLI["load_policy"](self.policy_path)

    def test_v2_rejects_missing_guard_unknown_schema_and_dangerous_limits(self):
        cases = []
        for key, value in (("version", True), ("version", 99), ("unknown", True)):
            policy = v2_policy()
            policy[key] = value
            cases.append(policy)
        policy = v2_policy()
        policy["rules"] = policy["rules"][1:]
        cases.append(policy)
        for key, value in (("schema_version", True), ("schema_version", 2),
                           ("capabilities", ["future-security-v2"]),
                           ("actions", ["receipt"])):
            policy = v2_policy()
            policy["rules"][0]["require"][key] = value
            cases.append(policy)
        for key, value in (("schema_version", True), ("unknown", True),
                           ("required_scanners", ["unknown"]),
                           ("optional_scanners", ["trivy", "trivy"]),
                           ("default_scope", []), ("dynamic_verification", "enabled"),
                           ("max_duration_seconds", 301), ("max_output_bytes", 2000001),
                           ("max_items", 10001), ("max_items", True),
                           ("max_advisory_age_days", 0), ("max_advisory_age_days", 366)):
            policy = v2_policy()
            policy["security_review"][key] = value
            cases.append(policy)
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(CLI["PolicyError"]):
                self.load(policy)

    def test_v2_duplicate_rule_identity_is_rejected(self):
        policy = v2_policy()
        policy["rules"].append(copy.deepcopy(policy["rules"][1]))
        with self.assertRaises(CLI["PolicyError"]):
            self.load(policy)

    def test_malformed_policy_encoding_is_controlled_rejection(self):
        self.policy_path.write_bytes(b'\xff')
        with self.assertRaises(CLI["PolicyError"]):
            CLI["load_policy"](self.policy_path)

    def test_v1_and_custom_profile_match_real_103_loader(self):
        original, normalizer = original_103_loader()
        value = {"version": 1, "custom": "preserved legacy ignore",
                 "rules": [{"id": "TEAM", "require": ["db_screen"],
                            "match": {"files": ["*.py"]}}]}
        self.policy_path.with_name("governance").write_text("lightweight")
        decoded = self.load(value)
        self.assertEqual(decoded, original(self.policy_path))
        self.assertEqual(normalizer(["unknown-capability-check"], "strict").checks,
                         ("unknown-capability-check",))
        value["rules"].append({"id": "CUSTOM-WITHOUT-ACTIONS", "match": {}})
        self.assertEqual(self.load(value), original(self.policy_path))
        self.policy_path.write_text(json.dumps(v2_policy()), encoding="utf-8")
        with self.assertRaisesRegex(CLI["PolicyError"], "require"):
            original(self.policy_path)

    def test_new_defaults_are_explicit_and_diagnostics_do_not_migrate_policy(self):
        legacy = installer_governance.default_governance_policy()
        self.assertEqual(legacy["version"], 1)
        explicit = installer_governance.default_security_governance_policy()
        self.assertEqual(explicit["version"], 2)
        self.assertEqual(explicit["security_review"], security_config())
        self.load(explicit)
        before = self.policy_path.read_bytes()
        status, detail = installer_governance.compare_governance_defaults(explicit)
        self.assertEqual(status, "managed")
        self.assertIn("schema=2", detail)
        self.assertEqual(self.policy_path.read_bytes(), before)
        explicit["security_review"]["max_items"] = 100
        changed, _ = installer_governance.compare_governance_defaults(explicit)
        self.assertEqual(changed, "policy_update_available")
        legacy["rules"].append(copy.deepcopy(explicit["rules"][0]))
        with self.assertRaises(ValueError):
            installer_governance.compare_governance_defaults(legacy)

    def test_security_requirements_preserve_original_route_and_gate_checks(self):
        policy = self.load(v2_policy())
        policy["security_review"]["required_scanners"] = ["trivy"]
        governance = CLI["evaluate_diff"](
            "--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-a\n+b\n", policy)
        card = {"version": 1, "route": "bounded", "work_kind": "feature",
                "intent_gaps": "none", "behavior_change": "local",
                "footprint": {"planned_paths": ["src/**"], "modules": 1,
                              "repos": 1, "sessions": 1},
                "reversibility": "easy", "risk_overlays": [],
                "coordination": "single", "uncertainty": "low",
                "route_override": None, "required_checks": [], "escalate_if": []}
        route = evaluate_route(card, governance.to_dict())
        before = copy.deepcopy(route)
        requirements = governance_service.security_requirements(
            SecurityPolicy.from_dict(policy["security_review"]), stage="design",
            dependency_required=True, review_required=True)
        result = governance_service.apply_security_requirements(route, requirements)
        self.assertEqual(route, before)
        self.assertEqual(result["effective_route"], "bounded")
        self.assertEqual(result["governance"]["risk_requirements"]["checks"],
                         ["team-check", "dependency-design-check", "security-review",
                          "security-scan:trivy"])
        self.assertIn("targeted-tests", result["requirements"]["required_checks"])
        self.assertIn("formal", result["governance"]["risk_requirements"]["receipts"])
        evidence = {f"checks:{name}": {"status": "PASS", "evidence_refs": ["tests.json"]}
                    for name in result["requirements"]["required_checks"]}
        evidence["receipts:formal"] = {"status": "PASS", "evidence_refs": ["receipt.json"]}
        plan = ReviewEvidencePlan(steps=(), blocking_reasons=(), limitations=(),
                                  confidence="high", may_claim_complete_structure=False)
        identity = IdentityComparison(IdentityState.VALID)
        ready = compose_delivery_gate(route_result=result, identity=identity,
                                      review_plan=plan, evidence_by_requirement=evidence)
        self.assertEqual(ready["verdict"], "READY")
        self.assertTrue(next(row for row in ready["requirements"]
                             if row["requirement"] == "dependency-design-check")
                        ["fact_ids"][0].startswith("risk_overlay:security-policy:"))
        del evidence["checks:security-scan:trivy"]
        pending = compose_delivery_gate(route_result=result, identity=identity,
                                        review_plan=plan, evidence_by_requirement=evidence)
        self.assertEqual(pending["verdict"], "NOT_READY")
        self.assertIn("checks:security-scan:trivy", pending["pending_requirements"])
        # Existing Composer owns the mixed human/technical pending semantics.
        result["governance"]["risk_requirements"]["reviews"].append("independent")
        result["governance"]["matched_rules"][0]["required"].append("independent_review")
        result["governance"]["provenance"].append({
            "kind": "reviews", "requirement": "independent",
            "source": "independent_review"})
        evidence["reviews:independent"] = {"status": "PENDING", "evidence_refs": []}
        mixed = compose_delivery_gate(route_result=result, identity=identity,
                                      review_plan=plan, evidence_by_requirement=evidence)
        self.assertEqual(mixed["verdict"], "NEEDS_HUMAN_REVIEW")
        self.assertEqual(set(mixed["pending_requirements"]),
                         {"checks:security-scan:trivy", "reviews:independent"})
        evidence["checks:security-scan:trivy"] = {"status": "FAIL", "evidence_refs": ["violation.json"]}
        failed = compose_delivery_gate(route_result=result, identity=identity,
                                       review_plan=plan, evidence_by_requirement=evidence)
        self.assertEqual(failed["verdict"], "NOT_READY")
        evidence["checks:security-scan:trivy"] = {"status": "NOT_APPLICABLE", "evidence_refs": []}
        inapplicable = compose_delivery_gate(route_result=result, identity=identity,
                                             review_plan=plan, evidence_by_requirement=evidence)
        self.assertEqual(inapplicable["verdict"], "NOT_READY")
        self.assertIn("checks:security-scan:trivy", inapplicable["failed_requirements"])
        evidence["checks:security-scan:trivy"] = {"status": "PASS", "evidence_refs": ["current.json"]}
        evidence["reviews:independent"] = {"status": "PASS", "evidence_refs": ["review.json"]}
        stale = compose_delivery_gate(
            route_result=result, identity=IdentityComparison(IdentityState.STALE),
            review_plan=plan, evidence_by_requirement=evidence)
        self.assertEqual(stale["verdict"], "NOT_READY")
        evidence["checks:security-scan:future"] = {"status": "PASS", "evidence_refs": ["future.json"]}
        with self.assertRaises(DeliveryGateError):
            compose_delivery_gate(route_result=result, identity=identity,
                                  review_plan=plan, evidence_by_requirement=evidence)

    def test_trusted_no_applicable_change_adds_no_safety_requirement(self):
        policy = SecurityPolicy(required_scanners=("trivy",))
        for stage in ("design", "execution", "closeout"):
            with self.subTest(stage=stage):
                requirements = governance_service.security_requirements(policy, stage=stage)
                self.assertEqual(requirements.checks, ())
                self.assertEqual(requirements.provenance, ())
        required = governance_service.security_requirements(
            policy, stage="closeout", dependency_required=True)
        self.assertEqual(required.checks,
                         ("dependency-delivery-check", "security-scan:trivy"))


if __name__ == "__main__":
    unittest.main()
