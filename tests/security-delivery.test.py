#!/usr/bin/env python3
"""Delivery checks require exact current inventory observations and regression."""
from __future__ import annotations
from pathlib import Path
import sys
import importlib.util
import unittest
import copy
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location("dependency_fixtures", Path(__file__).with_name("security-dependencies.test.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def context():
    from aek.core.security.dependencies import evaluate_design_dependency
    payload, host, authority, port = fixtures.dependency_context()
    package = {key: payload["proposals"][0][key] for key in ("ecosystem", "package", "version")}
    fixtures.register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
    fixtures.register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
    design = observations(port, ("advisory", "compatibility"))
    decision = evaluate_design_dependency(payload["proposals"][0], design, current_identity=authority.current_identity,
        required_components=fixtures.REQUIRED, scope_ref=authority.scope_ref, now=fixtures.NOW)
    return decision, host, authority, port


def observations(port, refs):
    result = []
    for ref in refs:
        observation = port.lookup(ref)
        if observation is not None:
            result.append({"metadata": observation.metadata, "facts": observation.facts,
                           "provenance_ref": observation.provenance_ref})
    return tuple(result)


def inventory(authority, kind="RESOLVED", version="1.2.3", ref=None):
    ref = ref or kind.lower()
    return {"id": "INV-" + ref, "kind": kind, "scope_ref": authority.scope_ref,
        "identity_digest": authority.current_identity.envelope_digest, "producer_ref": "tool",
        "packages": [{"id": "PKG-" + ref, "ecosystem": "maven", "package": "org.example:client",
                      "version": version, "scope": "production", "relationship": "direct",
                      "introduced_by": [], "dependency_path": [], "source_ref": ref}],
        "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}


def register_inventory(host, authority, port, item, **overrides):
    ref = item["packages"][0]["source_ref"]
    fixtures.register_source(host, authority, port, ref, "inventory", {"inventory": item}, **overrides)
    return ref


def evaluate(decision, inventories, port, authority, refs, **kwargs):
    from aek.core.security.delivery import evaluate_dependency_delivery
    return evaluate_dependency_delivery(decision, tuple(inventories), observations(port, refs),
        current_identity=authority.current_identity, required_components=fixtures.REQUIRED,
        scope_ref=authority.scope_ref, now=fixtures.NOW, **kwargs)


class SecurityDeliveryTests(unittest.TestCase):
    def test_attested_bom_parent_transitive_path_is_preserved_for_each_dependency_scope(self):
        for scope in ("production", "test", "provided", "build"):
            with self.subTest(scope=scope):
                decision, host, authority, port = context()
                decision["proposal"]["scope"] = scope
                item = inventory(authority)
                expected_path = ["PARENT-POM", "BOM", "INTRODUCER", "PKG-resolved"]
                item["packages"][0].update(scope=scope, relationship="transitive",
                    introduced_by=["PARENT-POM"], dependency_path=expected_path)
                register_inventory(host, authority, port, item)
                fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
                fixtures.register_source(host, authority, port, "normal", "regression",
                    {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
                result = evaluate(decision, (item,), port, authority,
                    ["advisory", "compatibility", "resolved", "scope", "normal"])
                self.assertEqual(result["status"], "PASS")
                self.assertIn("resolved", result["evidence_refs"])
                observed = port.lookup("resolved")
                self.assertEqual(observed.metadata["status"], "VERIFIED")
                package = observed.facts["inventory"]["packages"][0]
                self.assertEqual(package["scope"], scope)
                self.assertEqual(package["relationship"], "transitive")
                self.assertEqual(package["introduced_by"], ["PARENT-POM"])
                self.assertEqual(package["dependency_path"], expected_path)

    def test_bom_parent_and_private_resolution_gaps_remain_visible_and_prevent_delivery_pass(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        gaps = ["BOM resolution unavailable", "parent POM unresolved", "private repository package unknown"]
        decision, host, authority, port = context()
        item = inventory(authority)
        item.update(coverage="PARTIAL", coverage_gaps=gaps)
        item["packages"][0].update(version=None, relationship="UNKNOWN")
        register_inventory(host, authority, port, item, coverage="PARTIAL", coverage_gaps=gaps)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        refs = ["advisory", "compatibility", "resolved", "scope", "normal"]
        payload = fixtures.model.request_payload()
        payload.update(stage="closeout", proposals=[decision["proposal"]], inventories=[item])
        body = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        body["sources"] = [port.lookup(ref).metadata for ref in refs]
        record = seal_security_record(body)
        key = "checks:dependency-delivery-check"
        result = SecurityReviewService(source_port=port, clock=lambda: fixtures.NOW).inspect_evidence(record,
            current_identity=authority.current_identity, required_components=fixtures.REQUIRED, requirements=(key,))
        self.assertEqual(result[key]["status"], "PENDING")
        self.assertIn("RESOLVED_INVENTORY_SOURCE_REQUIRED", result[key]["details"]["reason_codes"])
        saved = record_as_dict(record)
        self.assertEqual(saved["inventories"][0]["coverage_gaps"], gaps)
        self.assertIsNone(saved["inventories"][0]["packages"][0]["version"])
        self.assertEqual(saved["inventories"][0]["packages"][0]["relationship"], "UNKNOWN")
        source = next(row for row in saved["sources"] if row["ref"] == "resolved")
        self.assertEqual(source["coverage"], "PARTIAL")
        self.assertEqual(source["coverage_gaps"], gaps)

    def test_finite_attested_cycle_keeps_instance_paths_without_recursive_expansion(self):
        decision, host, authority, port = context()
        item = inventory(authority)
        target = item["packages"][0]
        target.update(relationship="transitive", introduced_by=["PKG-parent"],
                      dependency_path=["PKG-parent", "PKG-resolved"])
        parent = {**target, "id": "PKG-parent", "package": "org.example:parent",
                  "introduced_by": ["PKG-resolved"], "dependency_path": ["PKG-resolved", "PKG-parent"]}
        item["packages"].append(parent)
        register_inventory(host, authority, port, item)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (item,), port, authority,
                          ["advisory", "compatibility", "resolved", "scope", "normal"])
        self.assertEqual(result["status"], "PASS")
        observed = port.lookup("resolved").facts["inventory"]["packages"]
        self.assertEqual([package["id"] for package in observed], ["PKG-resolved", "PKG-parent"])
        self.assertEqual([package["introduced_by"] for package in observed], [["PKG-parent"], ["PKG-resolved"]])
        self.assertEqual([package["dependency_path"] for package in observed],
                         [["PKG-parent", "PKG-resolved"], ["PKG-resolved", "PKG-parent"]])
        self.assertIn("resolved", result["evidence_refs"])

    def test_dependency_path_over_item_budget_is_rejected_without_truncating_a_proof(self):
        from aek.core.security.model import decode_security_request, MAX_SECURITY_BYTES
        _decision, host, authority, port = context()
        item = inventory(authority)
        item["packages"][0].update(relationship="transitive", introduced_by=["NODE-0"],
                                  dependency_path=["NODE-" + str(index) for index in range(10001)])
        payload = fixtures.model.request_payload()
        payload["inventories"] = [item]
        raw = json.dumps(payload).encode()
        self.assertLess(len(raw), MAX_SECURITY_BYTES)  # The item budget, not byte size, rejects this path.
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            decode_security_request(raw)
        previous_events = dict(host.history)
        with self.assertRaises(ValueError):
            register_inventory(host, authority, port, item)
        self.assertIsNone(port.lookup("resolved"))
        self.assertEqual(host.history, previous_events)
        self.assertEqual(len(item["packages"][0]["dependency_path"]), 10001)

    def test_repeated_nodes_in_a_declared_linear_path_are_rejected_as_ambiguous_data(self):
        from aek.core.security.model import decode_security_request
        _decision, _host, authority, _port = context()
        item = inventory(authority)
        item["packages"][0].update(relationship="transitive", introduced_by=["A"], dependency_path=["A", "B", "A"])
        payload = fixtures.model.request_payload()
        payload["inventories"] = [item]
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            decode_security_request(json.dumps(payload).encode())

    def test_current_instance_mapping_cannot_hide_old_bytes_or_unknown_and_conflicting_owners(self):
        for case in ("old-version", "unknown-owner", "conflicting-owner"):
            with self.subTest(case=case):
                decision, host, authority, port = context()
                item = inventory(authority, version="1.0.0" if case == "old-version" else "1.2.3")
                owner = "UNKNOWN" if case == "unknown-owner" else "DEP-1"
                fixtures.register_source(host, authority, port, "resolved", "inventory",
                    {"inventory": item, "package_proposals": {"PKG-resolved": owner}})
                items, refs = [item], ["advisory", "compatibility", "resolved", "scope", "normal"]
                if case == "conflicting-owner":
                    second = copy.deepcopy(item)
                    second["id"] = "INV-second"
                    second["packages"][0]["source_ref"] = "second"
                    fixtures.register_source(host, authority, port, "second", "inventory",
                        {"inventory": second, "package_proposals": {"PKG-resolved": "DEP-2"}})
                    items.append(second)
                    refs.append("second")
                fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
                fixtures.register_source(host, authority, port, "normal", "regression",
                    {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
                result = evaluate(decision, items, port, authority, refs, proposal_ids=("DEP-1", "DEP-2"))
                self.assertEqual(result["status"], "FAIL" if case == "old-version" else "PENDING")
                expected = {"old-version": "DEPENDENCY_VERSION_MISMATCH", "unknown-owner": "INVENTORY_PROPOSAL_MAPPING_UNKNOWN",
                            "conflicting-owner": "INVENTORY_PROPOSAL_MAPPING_CONFLICT"}[case]
                self.assertIn(expected, result["reason_codes"])

    def test_unrelated_ecosystem_inventory_does_not_block_current_dependency(self):
        decision, host, authority, port = context()
        resolved = inventory(authority)
        unrelated = inventory(authority, ref="frontend-resolved")
        unrelated["packages"][0].update(ecosystem="npm", package="frontend-widget", version="5.0.0")
        register_inventory(host, authority, port, resolved)
        register_inventory(host, authority, port, unrelated)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        refs = ["advisory", "compatibility", "resolved", "frontend-resolved", "scope", "normal"]
        for rows in ((resolved, unrelated), (unrelated, resolved)):
            with self.subTest(order=[item["id"] for item in rows]):
                self.assertEqual(evaluate(decision, rows, port, authority, refs)["status"], "PASS")

    def test_unmapped_distinct_version_instances_need_association_not_guessing(self):
        decision, host, authority, port = context()
        resolved = inventory(authority)
        other = {**resolved["packages"][0], "id": "PKG-other-workspace", "version": "9.9.9"}
        resolved["packages"].append(other)
        register_inventory(host, authority, port, resolved)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (resolved,), port, authority,
                          ["advisory", "compatibility", "resolved", "scope", "normal"])
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("INVENTORY_INSTANCE_ASSOCIATION_REQUIRED", result["reason_codes"])

    def test_trusted_instance_map_selects_current_proposal_without_hiding_other_workspaces(self):
        decision, host, authority, port = context()
        resolved = inventory(authority)
        resolved["packages"].append({**resolved["packages"][0], "id": "PKG-other-workspace", "version": "9.9.9"})
        fixtures.register_source(host, authority, port, "resolved", "inventory", {
            "inventory": resolved,
            "package_proposals": {"PKG-resolved": "DEP-1", "PKG-other-workspace": "DEP-2"}})
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (resolved,), port, authority,
                          ["advisory", "compatibility", "resolved", "scope", "normal"],
                          proposal_ids=("DEP-1", "DEP-2"))
        self.assertEqual(result["status"], "PASS")

    def test_mapping_only_other_proposal_does_not_prove_current_instance_delivery(self):
        decision, host, authority, port = context()
        resolved = inventory(authority)
        fixtures.register_source(host, authority, port, "resolved", "inventory", {
            "inventory": resolved, "package_proposals": {"PKG-resolved": "DEP-2"}})
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (resolved,), port, authority,
                          ["advisory", "compatibility", "resolved", "scope", "normal"],
                          proposal_ids=("DEP-1", "DEP-2"))
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("DEPENDENCY_INSTANCE_NOT_RESOLVED", result["reason_codes"])

    def test_conflicting_current_shipped_scope_sources_cannot_select_a_convenient_result(self):
        decision, host, authority, port = context()
        resolved, shipped = inventory(authority), inventory(authority, "SHIPPED")
        refs = ["advisory", "compatibility", register_inventory(host, authority, port, resolved),
                register_inventory(host, authority, port, shipped)]
        fixtures.register_source(host, authority, port, "scope-required", "delivery-scope", {"shipped_required": True})
        fixtures.register_source(host, authority, port, "scope-unneeded", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (resolved, shipped), port, authority, refs + ["scope-required", "scope-unneeded", "normal"])
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("DELIVERY_SCOPE_CONFLICT", result["reason_codes"])

    def test_real_current_disposition_can_allow_known_risk_without_hiding_delivery_requirements(self):
        from aek.core.security.dependencies import dependency_evidence_digest
        decision, host, authority, port = context()
        package = {key: decision["proposal"][key] for key in ("ecosystem", "package", "version")}
        fixtures.register_source(host, authority, port, "affected", "advisory",
            {**package, "advisories": [fixtures.advisory_payload()]})
        decision["proposal"]["evidence_refs"].append("affected")
        design_refs = ["advisory", "compatibility", "affected"]
        evidence_digest = dependency_evidence_digest(observations(port, design_refs))
        binding = {"operation": "accept-risk", "dependency_ref": "DEP-1", "version": "1.2.3",
            "scope_ref": authority.scope_ref, "identity_digest": authority.current_identity.envelope_digest,
            "expires_at": authority.expires_at, "evidence_digest": evidence_digest}
        token = host.issue(authority, "user-confirmation", "accept-risk", binding, actor="k")
        handle = authority.on_user_confirmation(token, binding=binding)
        accepted_ref = authority.validate(handle, operation="accept-risk", binding=binding)["provenance_ref"]
        self.assertTrue(authority.validate_disposition(accepted_ref, scope_ref=authority.scope_ref,
            identity_digest=authority.current_identity.envelope_digest, dependency_ref="DEP-1", version="1.2.3",
            evidence_digest=evidence_digest))
        item = inventory(authority)
        register_inventory(host, authority, port, item)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        refs = design_refs + ["resolved", "scope"]
        without_regression = evaluate(decision, (item,), port, authority, refs, accepted_risk_ref=accepted_ref)
        self.assertEqual(without_regression["status"], "PENDING")
        self.assertIn("DEPENDENCY_REGRESSION_REQUIRED", without_regression["reason_codes"])
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (item,), port, authority, refs + ["normal"], accepted_risk_ref=accepted_ref)
        self.assertEqual(result["status"], "PASS")
        self.assertIn(accepted_ref, result["evidence_refs"])
        self.assertEqual(evaluate(decision, (item,), port, authority, refs + ["normal"])["status"], "PENDING")

    def test_inventory_unknown_private_partial_and_wrong_producer_cannot_pass(self):
        cases = ("unknown-version", "unknown-relationship", "private-partial", "wrong-producer", "wrong-source-ref", "wrong-scope")
        for case in cases:
            decision, host, authority, port = context()
            item = inventory(authority)
            overrides = {}
            if case == "unknown-version":
                item["packages"][0]["version"] = None
            elif case == "unknown-relationship":
                item["packages"][0]["relationship"] = "UNKNOWN"
            elif case == "private-partial":
                item["coverage"] = "PARTIAL"
                item["coverage_gaps"] = ["private package unresolved"]
                overrides = {"coverage": "PARTIAL", "coverage_gaps": ["private package unresolved"]}
            elif case == "wrong-producer":
                item["producer_ref"] = "author"
            elif case == "wrong-source-ref":
                item["packages"][0]["source_ref"] = "forged"
            else:
                item["packages"][0]["scope"] = "provided"
            fixtures.register_source(host, authority, port, "resolved", "inventory", {"inventory": item}, **overrides)
            fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
            fixtures.register_source(host, authority, port, "normal", "regression",
                {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
            result = evaluate(decision, (item,), port, authority, ("advisory", "compatibility", "resolved", "scope", "normal"))
            with self.subTest(case=case):
                self.assertEqual(result["status"], "PENDING")

    def test_current_advisory_revalidation_overrides_cached_adoptable_and_no_import_claim(self):
        decision, host, authority, port = context()
        item = inventory(authority)
        register_inventory(host, authority, port, item)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        refs = ["advisory", "compatibility", "resolved", "scope", "normal"]
        self.assertEqual(evaluate(decision, (item,), port, authority, refs)["status"], "PASS")
        package = {key: decision["proposal"][key] for key in ("ecosystem", "package", "version")}
        fixtures.register_source(host, authority, port, "new-advisory", "advisory",
            {**package, "advisories": [fixtures.advisory_payload()]})
        fixtures.register_source(host, authority, port, "unobserved", "reachability", {"status": "NOT_OBSERVED"})
        result = evaluate(decision, (item,), port, authority, refs + ["new-advisory", "unobserved"])
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("KNOWN_AFFECTED_DEPENDENCY", result["reason_codes"])
        self.assertIn("RISK_DISPOSITION_REVALIDATION_REQUIRED", result["reason_codes"])
        self.assertIn("new-advisory", result["evidence_refs"])

    def test_missing_and_stale_design_source_cannot_reuse_cached_adoptable(self):
        from aek.core.context.identity import identity_as_dict, build_identity
        for changed in (False, True):
            decision, host, authority, port = context()
            item = inventory(authority)
            register_inventory(host, authority, port, item)
            fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
            fixtures.register_source(host, authority, port, "normal", "regression",
                {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
            refs = ["advisory", "compatibility", "resolved", "scope", "normal"]
            if changed:
                components = identity_as_dict(authority.current_identity)["components"]
                components["subject"]["scope_digest"] = "f" * 64
                authority.current_identity = build_identity(components)
            else:
                refs.remove("advisory")
            result = evaluate(decision, (item,), port, authority, refs)
            with self.subTest(changed=changed):
                self.assertEqual(result["status"], "PENDING")

    def test_risk_accepted_cache_still_needs_fresh_upper_layer_disposition(self):
        decision, host, authority, port = context()
        decision["reason_codes"] = ["ACCEPTED_RISK"]
        item = inventory(authority)
        register_inventory(host, authority, port, item)
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        result = evaluate(decision, (item,), port, authority, ("advisory", "compatibility", "resolved", "scope", "normal"))
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("RISK_DISPOSITION_REVALIDATION_REQUIRED", result["reason_codes"])

    def test_declared_new_version_cannot_replace_actual_inventory_fact(self):
        decision, host, authority, port = context()
        actual = inventory(authority, version="1.0.0")
        register_inventory(host, authority, port, actual)
        declared = copy.deepcopy(actual)
        declared["packages"][0]["version"] = "1.2.3"
        result = evaluate(decision, (declared,), port, authority, ("advisory", "compatibility", "resolved"))
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("INVENTORY_SOURCE_BINDING_MISMATCH", result["reason_codes"])

    def test_complete_current_resolved_and_shipped_sources_with_regression_pass(self):
        decision, host, authority, port = context()
        resolved, shipped = inventory(authority), inventory(authority, "SHIPPED")
        refs = ["advisory", "compatibility", register_inventory(host, authority, port, resolved),
                register_inventory(host, authority, port, shipped)]
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": True})
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        refs.extend(("scope", "normal"))
        result = evaluate(decision, (resolved, shipped), port, authority, refs)
        self.assertEqual(result, {"status": "PASS", "reason_codes": [], "evidence_refs": sorted(refs)})

    def test_scoped_no_shipped_artifact_needs_real_complete_source(self):
        decision, host, authority, port = context()
        resolved = inventory(authority)
        refs = ["advisory", "compatibility", register_inventory(host, authority, port, resolved)]
        fixtures.register_source(host, authority, port, "normal", "regression",
            {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        refs.append("normal")
        missing = evaluate(decision, (resolved,), port, authority, refs)
        self.assertEqual(missing["status"], "PENDING")
        self.assertIn("DELIVERY_SCOPE_EVIDENCE_REQUIRED", missing["reason_codes"])
        fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
        self.assertEqual(evaluate(decision, (resolved,), port, authority, refs + ["scope"])["status"], "PASS")

    def test_failed_or_other_version_regression_cannot_prove_compatibility(self):
        for regression in ({"passed": False, "dependency_ref": "DEP-1", "version": "1.2.3"},
                           {"passed": True, "dependency_ref": "OTHER", "version": "1.2.3"},
                           {"passed": True, "dependency_ref": "DEP-1", "version": "2.0.0"}):
            decision, host, authority, port = context()
            resolved = inventory(authority)
            refs = ["advisory", "compatibility", register_inventory(host, authority, port, resolved)]
            fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": False})
            fixtures.register_source(host, authority, port, "normal", "regression", regression)
            result = evaluate(decision, (resolved,), port, authority, refs + ["scope", "normal"])
            with self.subTest(regression=regression):
                self.assertEqual(result["status"], "FAIL" if regression["passed"] is False else "PENDING")

    def test_old_resolved_or_old_shipped_version_fails_despite_clean_proposed_version(self):
        for old_kind in ("RESOLVED", "SHIPPED"):
            decision, host, authority, port = context()
            resolved = inventory(authority, "RESOLVED", "1.0.0" if old_kind == "RESOLVED" else "1.2.3")
            shipped = inventory(authority, "SHIPPED", "1.0.0" if old_kind == "SHIPPED" else "1.2.3")
            refs = ["advisory", "compatibility", register_inventory(host, authority, port, resolved),
                    register_inventory(host, authority, port, shipped)]
            fixtures.register_source(host, authority, port, "scope", "delivery-scope", {"shipped_required": True})
            fixtures.register_source(host, authority, port, "normal", "regression",
                {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
            refs.extend(("scope", "normal"))
            result = evaluate(decision, (resolved, shipped), port, authority, refs)
            with self.subTest(old_kind=old_kind):
                self.assertEqual(result["status"], "FAIL")
                self.assertIn("DEPENDENCY_VERSION_MISMATCH", result["reason_codes"])

    def test_cached_adoptable_and_declared_inventory_cannot_prove_delivery(self):
        decision, host, authority, port = context()
        result = evaluate(decision, (inventory(authority),), port, authority, ("advisory", "compatibility"))
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("RESOLVED_INVENTORY_SOURCE_REQUIRED", result["reason_codes"])
        self.assertIn("DELIVERY_SCOPE_EVIDENCE_REQUIRED", result["reason_codes"])
        self.assertIn("DEPENDENCY_REGRESSION_REQUIRED", result["reason_codes"])
        self.assertNotIn("verdict", result)


if __name__ == "__main__":
    unittest.main()
