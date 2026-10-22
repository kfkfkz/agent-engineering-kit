#!/usr/bin/env python3
"""Dependency decisions consume real registered sources, not author assertions."""
from __future__ import annotations
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "tests" / (name + ".test.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


model = load_fixture("security-model")
workflow = load_fixture("security-workflow")
NOW = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
REQUIRED = ("subject.scope_digest", "artifact.design_digest", "policy.content_digest",
            "context.source_digest", "evidence.verification_digest")


def dependency_context():
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.core.context.identity import decode_identity
    payload = model.request_payload()
    proposal = {"id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
                "version": "1.2.3", "purpose": "HTTP client", "scope": "production",
                "design_refs": ["D2"], "evidence_refs": ["advisory", "compatibility"]}
    payload["proposals"] = [proposal]
    host = workflow.LiveHost()
    identity = decode_identity(payload["identity"])
    authority = BuiltinWorkflowAuthority(repository_ref="fixture-repo", task_id="dep-task",
        scope_ref=payload["scope"]["subject_digest"], current_identity=identity,
        author="author", host_source=host, clock=lambda: NOW)
    return payload, host, authority, BuiltinSourceEvidencePort(authority)


def register_source(host, authority, port, ref, kind, facts, **overrides):
    from aek.adapters.security_evidence import source_binding
    metadata = workflow.source_metadata(authority, ref, kind)
    metadata["required_components"] = list(REQUIRED)
    metadata.update(overrides)
    binding = source_binding(metadata, facts)
    token = host.issue(authority, "tool-completed", "source", binding, actor="tool")
    handle = authority.on_source_completed(token, binding=binding)
    port.register(metadata, facts, handle)


def advisory_payload(match="AFFECTED"):
    return {"canonical_id": "GHSA-fixture-0001", "aliases": [],
            "source_url": "https://example.org/advisories/GHSA-fixture-0001",
            "affected_range": "[1.0.0,1.3.0)", "fixed_versions": ["1.3.0"],
            "checked_at": NOW.isoformat(), "source_digest": "f" * 64, "match_status": match}


class DependencyDecisionTests(unittest.TestCase):
    def test_dynamic_declaration_cannot_be_adopted_by_repeating_it_in_clean_sources(self):
        from aek.core.security.dependencies import evaluate_design_dependency
        payload, host, authority, port = dependency_context()
        for version in ("[1.0,2.0)", "LATEST", "RELEASE", "${client.version}"):
            with self.subTest(version=version):
                proposal = {**payload["proposals"][0], "version": version}
                package = {key: proposal[key] for key in ("ecosystem", "package", "version")}
                ref = "advisory-" + str(len(host.history))
                register_source(host, authority, port, ref, "advisory", {**package, "advisories": []})
                register_source(host, authority, port, ref + "-compat", "compatibility", {**package, "compatible": True})
                observations = tuple({"metadata": port.lookup(key).metadata, "facts": port.lookup(key).facts,
                                      "provenance_ref": port.lookup(key).provenance_ref}
                                     for key in (ref, ref + "-compat"))
                result = evaluate_design_dependency(proposal, observations, current_identity=authority.current_identity,
                    required_components=REQUIRED, scope_ref=authority.scope_ref, now=NOW)
                self.assertEqual(result["status"], "PENDING")
                self.assertIn("DETERMINISTIC_RESOLUTION_REQUIRED", result["reason_codes"])
                self.assertEqual(result["proposal"]["version"], version)

    def test_cve_ghsa_osv_and_vendor_aliases_require_current_attested_mapping(self):
        from aek.application.security_review import SecurityReviewService
        identifiers = ("CVE-2026-FIXTURE", "GHSA-fixture-0001", "OSV-FIXTURE", "VENDOR-2026-FIXTURE")
        for identifier in identifiers:
            with self.subTest(canonical_id=identifier):
                payload, host, authority, port = dependency_context()
                package = {key: payload["proposals"][0][key] for key in ("ecosystem", "package", "version")}
                alias = "ALIAS-" + identifier
                advisory = {**advisory_payload(), "canonical_id": identifier, "aliases": [alias]}
                register_source(host, authority, port, "advisory", "advisory",
                    {**package, "advisories": [advisory]})
                service = SecurityReviewService(source_port=port, clock=lambda: NOW)
                kwargs = dict(current_identity=authority.current_identity, required_components=REQUIRED)
                finding = {**model.finding_payload(), "category": "DEPENDENCY", "dependency_ref": "DEP-1",
                    "evidence_refs": ["advisory"]}
                for reference in (identifier, alias):
                    payload["findings"] = [{**finding, "advisory_refs": [reference]}]
                    result = service.evaluate(json.dumps(payload).encode(), **kwargs)
                    self.assertEqual(result["findings"][0]["verification_status"], "STATIC_VERIFIED")
                    self.assertEqual(result["findings"][0]["advisory_refs"], [reference])
                observed = port.lookup("advisory")
                self.assertEqual(observed.metadata["status"], "VERIFIED")
                self.assertEqual(observed.facts["advisories"], [advisory])
                payload["findings"] = [{**finding, "advisory_refs": ["UNMAPPED-" + identifier]}]
                self.assertEqual(service.evaluate(json.dumps(payload).encode(), **kwargs)
                                 ["findings"][0]["verification_status"], "SUSPECTED")
                # A valid checksum and the stored alias list do not replace a revoked producer.
                payload["findings"] = [{**finding, "advisory_refs": [alias]}]
                host.history.clear()
                self.assertEqual(service.evaluate(json.dumps(payload).encode(), **kwargs)
                                 ["findings"][0]["verification_status"], "SUSPECTED")

    def test_independent_advisories_without_alias_evidence_keep_distinct_finding_identity(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import finding_fingerprint
        payload, host, authority, port = dependency_context()
        package = {key: payload["proposals"][0][key] for key in ("ecosystem", "package", "version")}
        identifiers = ("GHSA-independent-0001", "VENDOR-independent-0002")
        rows = [{**advisory_payload(), "canonical_id": identifier} for identifier in identifiers]
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": rows})
        payload["findings"] = [{**model.finding_payload(), "id": "FIND-" + str(index),
            "category": "DEPENDENCY", "dependency_ref": "DEP-1", "advisory_refs": [identifier],
            "evidence_refs": ["advisory"]} for index, identifier in enumerate(identifiers)]
        response = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(
            json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
        self.assertEqual([finding["verification_status"] for finding in response["findings"]],
                         ["STATIC_VERIFIED", "STATIC_VERIFIED"])
        self.assertEqual([finding["advisory_refs"] for finding in response["findings"]],
                         [[identifier] for identifier in identifiers])
        self.assertNotEqual(finding_fingerprint(response["findings"][0]),
                            finding_fingerprint(response["findings"][1]))
        self.assertEqual(port.lookup("advisory").facts["advisories"], rows)

    def test_current_matching_dependency_advisory_verifies_without_import_or_dynamic_poc(self):
        from aek.application.security_review import SecurityReviewService
        payload, host, authority, port = dependency_context()
        package = {key: payload["proposals"][0][key] for key in ("ecosystem", "package", "version")}
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": [advisory_payload()]})
        payload["findings"] = [{**model.finding_payload(), "category": "DEPENDENCY", "dependency_ref": "DEP-1",
            "advisory_refs": ["GHSA-fixture-0001"], "evidence_refs": ["advisory"], "reachability": "UNKNOWN"}]
        service = SecurityReviewService(source_port=port, clock=lambda: NOW)
        kwargs = dict(current_identity=authority.current_identity, required_components=REQUIRED)
        result = service.evaluate(json.dumps(payload).encode(), **kwargs)
        self.assertEqual(result["findings"][0]["verification_status"], "STATIC_VERIFIED")
        self.assertEqual(result["findings"][0]["reachability"], "UNKNOWN")
        self.assertEqual(result["dependency_decisions"][0]["status"], "PENDING")
        self.assertNotIn("lifecycle", result["findings"][0])
        payload["proposals"][0]["version"] = "1.3.0"
        stale_version = service.evaluate(json.dumps(payload).encode(), **kwargs)
        self.assertNotEqual(stale_version["findings"][0]["verification_status"], "STATIC_VERIFIED")

    def test_current_independent_static_path_evidence_can_verify_a_finding_but_author_claim_cannot(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import finding_fingerprint
        payload, host, authority, port = dependency_context()
        payload["proposals"] = []
        finding = {**model.finding_payload(), "location_refs": ["pkg.Handler.download"],
                   "surface_refs": ["SURFACE-1"], "evidence_refs": ["static-path"],
                   "verification_status": "STATIC_VERIFIED"}
        payload["findings"] = [finding]
        surface = {"id": "SURFACE-1", "entry_ref": "pkg.Handler.download", "actors": ["user"],
            "controllable_inputs": ["request.path"], "boundaries": ["file-root"], "sink_refs": ["read_file"],
            "controls": [{"kind": "path-validation", "status": "ABSENT_WITH_EVIDENCE", "evidence_refs": ["static-path"]}],
            "evidence_refs": ["static-path"], "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}
        payload["surfaces"] = [surface]
        facts = {"finding_fingerprint": finding_fingerprint(finding), "verification_status": "STATIC_VERIFIED",
                 "preconditions": finding["preconditions"], "surfaces": [surface],
                 "path": ["request.path", "pkg.Handler.download", "read_file"], "complete_path": True}
        register_source(host, authority, port, "static-path", "static-path", facts)
        service = SecurityReviewService(source_port=port, clock=lambda: NOW)
        response = service.evaluate(json.dumps(payload).encode(), current_identity=authority.current_identity,
                                    required_components=REQUIRED)
        self.assertEqual(response["findings"][0]["verification_status"], "STATIC_VERIFIED")
        self.assertNotIn("SOURCE_EVIDENCE_UNAVAILABLE", response["findings"][0]["coverage_gaps"])
        host.history.clear()
        rejected = service.evaluate(json.dumps(payload).encode(), current_identity=authority.current_identity,
                                    required_components=REQUIRED)
        self.assertEqual(rejected["findings"][0]["verification_status"], "INCONCLUSIVE")
        self.assertNotIn("lifecycle", rejected["findings"][0])

    def test_observed_parameter_binding_is_not_promoted_to_confirmed_injection(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import finding_fingerprint
        payload, host, authority, port = dependency_context()
        payload["proposals"] = []
        finding = {**model.finding_payload(), "category": "SQL_INJECTION", "surface_refs": ["SQL-1"],
                   "evidence_refs": ["bound-query"], "verification_status": "STATIC_VERIFIED"}
        surface = {"id": "SQL-1", "entry_ref": "Order.list", "actors": ["user"],
            "controllable_inputs": ["request.filter"], "boundaries": ["database"], "sink_refs": ["execute_parameterized"],
            "controls": [{"kind": "parameter-binding", "status": "OBSERVED", "evidence_refs": ["bound-query"]}],
            "evidence_refs": ["bound-query"], "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}
        payload.update(findings=[finding], surfaces=[surface])
        register_source(host, authority, port, "bound-query", "static-path", {
            "finding_fingerprint": finding_fingerprint(finding), "verification_status": "STATIC_VERIFIED",
            "preconditions": finding["preconditions"], "surfaces": [surface],
            "path": ["request.filter", "Order.list", "execute_parameterized"], "complete_path": True})
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(json.dumps(payload).encode(),
            current_identity=authority.current_identity, required_components=REQUIRED)
        self.assertEqual(result["findings"][0]["verification_status"], "INCONCLUSIVE")

    def test_unrelated_absent_control_cannot_override_observed_or_unknown_protection(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import finding_fingerprint
        for protection in ("OBSERVED", "UNKNOWN"):
            with self.subTest(protection=protection):
                payload, host, authority, port = dependency_context()
                payload["proposals"] = []
                finding = {**model.finding_payload(), "category": "SQL_INJECTION", "surface_refs": ["SQL-1"],
                           "evidence_refs": ["bound-query"], "verification_status": "STATIC_VERIFIED"}
                surface = {"id": "SQL-1", "entry_ref": "Order.list", "actors": ["user"],
                    "controllable_inputs": ["request.filter"], "boundaries": ["database"],
                    "sink_refs": ["execute_parameterized"], "controls": [
                        {"kind": "parameter-binding", "status": protection, "evidence_refs": ["bound-query"]},
                        {"kind": "query-audit-log", "status": "ABSENT_WITH_EVIDENCE", "evidence_refs": ["bound-query"]}],
                    "evidence_refs": ["bound-query"], "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}
                payload.update(findings=[finding], surfaces=[surface])
                register_source(host, authority, port, "bound-query", "static-path", {
                    "finding_fingerprint": finding_fingerprint(finding), "verification_status": "STATIC_VERIFIED",
                    "preconditions": finding["preconditions"], "surfaces": [surface],
                    "path": ["request.filter", "Order.list", "execute_parameterized"], "complete_path": True})
                result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(json.dumps(payload).encode(),
                    current_identity=authority.current_identity, required_components=REQUIRED)
                self.assertEqual(result["findings"][0]["verification_status"], "INCONCLUSIVE")

    def test_fresh_no_dependency_scope_does_not_replace_stale_record_identity(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.context.identity import build_identity, identity_as_dict
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        payload, host, authority, port = dependency_context()
        payload["proposals"] = []
        body = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        components = identity_as_dict(authority.current_identity)["components"]
        components["policy"]["content_digest"] = "f" * 64
        authority.current_identity = build_identity(components)
        register_source(host, authority, port, "current-scope", "scope", {"scope": payload["scope"], "dependency_change": False})
        body["sources"] = [port.lookup("current-scope").metadata]
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).inspect_evidence(seal_security_record(body),
            current_identity=authority.current_identity, required_components=REQUIRED,
            requirements=("checks:dependency-design-check", "checks:dependency-delivery-check"))
        for row in result.values():
            self.assertEqual(row["status"], "PENDING")
            self.assertIn("EVIDENCE_STALE", row["details"]["reason_codes"])

    def test_design_cannot_omit_a_current_affected_source_recorded_outside_proposal_refs(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        payload, host, authority, port = dependency_context()
        package = {key: payload["proposals"][0][key] for key in ("ecosystem", "package", "version")}
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        register_source(host, authority, port, "new-affected", "advisory", {**package, "advisories": [advisory_payload()]})
        body = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        body["sources"] = [port.lookup(ref).metadata for ref in ("advisory", "compatibility", "new-affected")]
        key = "checks:dependency-design-check"
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).inspect_evidence(seal_security_record(body),
            current_identity=authority.current_identity, required_components=REQUIRED, requirements=(key,))
        self.assertEqual(result[key]["status"], "PENDING")
        self.assertIn("KNOWN_AFFECTED_DEPENDENCY", result[key]["details"]["reason_codes"])
        self.assertIn("new-affected", result[key]["evidence_refs"])

    def test_no_dependency_change_needs_a_current_complete_source_not_empty_proposals(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        payload, host, authority, port = dependency_context()
        payload["proposals"] = []
        record = new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A")
        service = SecurityReviewService(source_port=port, clock=lambda: NOW)
        keys = ("checks:dependency-design-check", "checks:dependency-delivery-check")
        args = dict(current_identity=authority.current_identity, required_components=REQUIRED, requirements=keys)
        self.assertEqual(service.compose_evidence(record, **args)[keys[0]]["status"], "PENDING")
        register_source(host, authority, port, "scope-proof", "scope",
                        {"scope": payload["scope"], "dependency_change": False})
        body = record_as_dict(record)
        body["sources"] = [port.lookup("scope-proof").metadata]
        proven = seal_security_record(body)
        result = service.compose_evidence(proven, **args)
        for key in keys:
            self.assertEqual(result[key]["status"], "PASS")
            self.assertEqual(result[key]["evidence_refs"], ["scope-proof"])
        # Reusing the same subject label for a different path is not a coverage proof.
        body["scope"]["paths"].append("other.py")
        self.assertEqual(service.compose_evidence(seal_security_record(body), **args)[keys[0]]["status"], "PENDING")
        # Revoked source callbacks cannot be replaced with stored VERIFIED metadata.
        host.history.clear()
        self.assertEqual(service.compose_evidence(proven, **args)[keys[1]]["status"], "PENDING")

    def test_no_dependency_change_does_not_resolve_an_existing_dependency_finding(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        payload, host, authority, port = dependency_context()
        payload["proposals"] = []
        payload["findings"] = [{**model.finding_payload(), "category": "DEPENDENCY", "dependency_ref": "DEP-1"}]
        register_source(host, authority, port, "scope-proof", "scope", {"scope": payload["scope"], "dependency_change": False})
        body = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        body["sources"] = [port.lookup("scope-proof").metadata]
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).inspect_evidence(seal_security_record(body),
            current_identity=authority.current_identity, required_components=REQUIRED,
            requirements=("checks:dependency-delivery-check",))
        row = result["checks:dependency-delivery-check"]
        self.assertEqual(row["status"], "PENDING")
        self.assertIn("DEPENDENCY_PROPOSAL_REQUIRED", row["details"]["reason_codes"])

    def test_service_delivery_rechecks_actual_sources_inventory_and_regression(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        delivery = load_fixture("security-delivery")
        payload, host, authority, port = dependency_context()
        package = {key: payload["proposals"][0][key] for key in ("ecosystem", "package", "version")}
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        item = delivery.inventory(authority)
        delivery.register_inventory(host, authority, port, item)
        register_source(host, authority, port, "delivery-scope", "delivery-scope", {"shipped_required": False})
        register_source(host, authority, port, "regression", "regression",
                        {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
        payload["inventories"] = [item]
        body = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        refs = ("advisory", "compatibility", "resolved", "delivery-scope", "regression")
        body["sources"] = [port.lookup(ref).metadata for ref in refs]
        record = seal_security_record(body)
        service = SecurityReviewService(source_port=port, clock=lambda: NOW)
        key = "checks:dependency-delivery-check"
        result = service.compose_evidence(record, current_identity=authority.current_identity,
            required_components=REQUIRED, requirements=(key,), stage="closeout")
        self.assertEqual(result[key]["status"], "PASS")
        self.assertEqual(set(result[key]["evidence_refs"]), set(refs))
        # A correctly sealed author-controlled inventory does not replace actual bytes.
        body["inventories"][0]["packages"][0]["version"] = "9.9.9"
        denied = service.inspect_evidence(seal_security_record(body), current_identity=authority.current_identity,
            required_components=REQUIRED, requirements=(key,), stage="closeout")
        self.assertEqual(denied[key]["status"], "PENDING")
        self.assertIn("INVENTORY_SOURCE_BINDING_MISMATCH", denied[key]["details"]["reason_codes"])

    def test_advisory_urls_are_bounded_https_data_without_credentials_or_ambiguous_authorities(self):
        from aek.core.security.dependencies import decode_advisories
        for url in ("https://github.com/advisories/GHSA-example?view=1#details",
                    "https://[2001:db8::1]:443/advisory", "https://[::ffff:192.0.2.1]/advisory",
                    "https://example.org:8443/advisory"):
            with self.subTest(url=url):
                self.assertEqual(decode_advisories([{**advisory_payload(), "source_url": url}])[0]["source_url"], url)
        for url in ("http://example.org/advisory", "https:///advisory", "https://user:secret@example.org/",
                    "https://example.org\\@evil.org/", "https://example.org:99999/",
                    "https://[not-an-ip]/", "https://example.org /", "https://example.org:/", "https://example.org../"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                decode_advisories([{**advisory_payload(), "source_url": url}])

    def test_design_gate_evidence_recomputes_sources_not_a_cached_adoptable_flag(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        record = new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A")
        key = "checks:dependency-design-check"
        service = SecurityReviewService(source_port=port, clock=lambda: NOW)
        result = service.compose_evidence(record, current_identity=authority.current_identity,
            required_components=REQUIRED, requirements=(key,), stage="design")
        self.assertEqual(result[key]["status"], "PASS")
        self.assertEqual(set(result[key]["evidence_refs"]), {"advisory", "compatibility"})
        body = record_as_dict(record)
        body["dependency_decisions"][0].update(status="ADOPTABLE", source_refs=["fake-a", "fake-b"], reason_codes=[])
        fake = seal_security_record(body)
        denied = SecurityReviewService(clock=lambda: NOW).compose_evidence(fake,
            current_identity=authority.current_identity, required_components=REQUIRED, requirements=(key,), stage="design")
        self.assertEqual(denied[key]["status"], "PENDING")
        self.assertNotIn("verdict", denied)

    def test_advisory_freshness_matching_and_schema_are_not_guessed(self):
        from aek.application.security_review import SecurityReviewService
        for change, expected in (({"match_status": "UNAFFECTED"}, "ADOPTABLE"),
                                 ({"checked_at": "2026-10-01T08:00:00Z"}, "PENDING"),
                                 ({"checked_at": "2026-10-11T08:00:00Z"}, "PENDING"),
                                 ({"match_status": "UNKNOWN"}, "PENDING"),
                                 ({"source_url": "https://credential@example.org/advisory"}, "PENDING"),
                                 ({"match_status": "SAFE"}, "PENDING"),
                                 ({"source_digest": "bad"}, "PENDING")):
            payload, host, authority, port = dependency_context()
            package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
            register_source(host, authority, port, "advisory", "advisory",
                            {**package, "advisories": [{**advisory_payload(), **change}]})
            register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
            result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(
                json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
            with self.subTest(change=change):
                self.assertEqual(result["dependency_decisions"][0]["status"], expected)

    def test_affected_design_adoption_needs_current_opaque_user_disposition(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.dependencies import dependency_evidence_digest
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        register_source(host, authority, port, "advisory", "advisory",
                        {**package, "advisories": [advisory_payload()]})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        observations = [{"metadata": port.lookup(ref).metadata, "facts": port.lookup(ref).facts,
                         "provenance_ref": port.lookup(ref).provenance_ref} for ref in ("advisory", "compatibility")]
        binding = {"operation": "accept-risk", "dependency_ref": "DEP-1", "version": "1.2.3",
                   "scope_ref": authority.scope_ref, "identity_digest": authority.current_identity.envelope_digest,
                   "expires_at": authority.expires_at, "evidence_digest": dependency_evidence_digest(tuple(observations))}
        token = host.issue(authority, "user-confirmation", "accept-risk", binding, actor="k")
        handle = authority.on_user_confirmation(token, binding=binding)
        ref = authority.validate(handle, operation="accept-risk", binding=binding)["provenance_ref"]
        payload["proposals"][0]["evidence_refs"].append(ref)
        result = SecurityReviewService(source_port=port, approval_port=authority, clock=lambda: NOW).evaluate(
            json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
        decision = result["dependency_decisions"][0]
        self.assertEqual(decision["status"], "ADOPTABLE")
        self.assertIn("KNOWN_AFFECTED_DEPENDENCY", decision["reason_codes"])
        self.assertIn("ACCEPTED_RISK", decision["reason_codes"])
        self.assertIn(ref, decision["source_refs"])
        self.assertIn("RISK_DISPOSITION_REVALIDATION_REQUIRED", decision["pending_delivery_requirements"])

    def test_a_referenced_partial_source_cannot_be_silently_ignored(self):
        from aek.application.security_review import SecurityReviewService
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        payload["proposals"][0]["evidence_refs"].append("partial")
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        register_source(host, authority, port, "partial", "advisory",
                        {**package, "advisories": []}, coverage="PARTIAL", coverage_gaps=["private library unknown"])
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(
            json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
        self.assertEqual(result["dependency_decisions"][0]["status"], "PENDING")
        self.assertIn("EVIDENCE_INCOMPLETE", result["dependency_decisions"][0]["reason_codes"])

    def test_required_unknown_advisory_or_missing_reference_is_not_replaced_by_a_clean_source(self):
        from aek.application.security_review import SecurityReviewService
        for variant in ("unknown", "missing"):
            payload, host, authority, port = dependency_context()
            package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
            payload["proposals"][0]["evidence_refs"].append("additional")
            register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
            register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
            if variant == "unknown":
                register_source(host, authority, port, "additional", "advisory",
                                {**package, "advisories": [advisory_payload("UNKNOWN")]})
            result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(
                json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
            with self.subTest(variant=variant):
                decision = result["dependency_decisions"][0]
                self.assertEqual(decision["status"], "PENDING")
                self.assertIn("ADVISORY_MATCH_UNKNOWN" if variant == "unknown" else "UNRESOLVED_REFERENCE",
                              decision["reason_codes"])

    def test_one_clean_source_cannot_hide_a_second_current_affected_advisory(self):
        from aek.application.security_review import SecurityReviewService
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        payload["proposals"][0]["evidence_refs"].append("second-advisory")
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "second-advisory", "advisory",
                        {**package, "advisories": [advisory_payload()]})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(
            json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
        decision = result["dependency_decisions"][0]
        self.assertEqual(decision["status"], "PENDING")
        self.assertIn("KNOWN_AFFECTED_DEPENDENCY", decision["reason_codes"])
        self.assertIn("second-advisory", decision["source_refs"])

    def test_affected_ghsa_without_cve_or_poc_requires_a_disposition_not_silent_adoption(self):
        from aek.application.security_review import SecurityReviewService
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        register_source(host, authority, port, "advisory", "advisory",
                        {**package, "advisories": [advisory_payload()]})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        result = SecurityReviewService(source_port=port, approval_port=authority, clock=lambda: NOW).evaluate(
            json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
        decision = result["dependency_decisions"][0]
        self.assertEqual(decision["status"], "PENDING")
        self.assertIn("KNOWN_AFFECTED_DEPENDENCY", decision["reason_codes"])
        self.assertIn("ADOPTION_PENDING", decision["reason_codes"])
        self.assertIn("advisory", decision["source_refs"])
        self.assertIn("SHIPPED_INVENTORY_IF_APPLICABLE", decision["pending_delivery_requirements"])
        self.assertEqual(port.lookup("advisory").facts["advisories"][0]["canonical_id"], "GHSA-fixture-0001")

    def test_known_incompatible_dependency_is_rejected_even_with_a_clean_advisory(self):
        from aek.application.security_review import SecurityReviewService
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": False})
        result = SecurityReviewService(source_port=port, clock=lambda: NOW).evaluate(
            json.dumps(payload).encode(), current_identity=authority.current_identity, required_components=REQUIRED)
        self.assertEqual(result["dependency_decisions"][0]["status"], "REJECTED")
        self.assertIn("DEPENDENCY_INCOMPATIBLE", result["dependency_decisions"][0]["reason_codes"])

    def test_fresh_bound_advisory_and_compatibility_allow_only_design_adoption(self):
        from aek.application.security_review import SecurityReviewService
        payload, host, authority, port = dependency_context()
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        service = SecurityReviewService(source_port=port, approval_port=authority, clock=lambda: NOW)
        result = service.evaluate(json.dumps(payload).encode(), current_identity=authority.current_identity,
                                  required_components=REQUIRED)
        decision = result["dependency_decisions"][0]
        self.assertEqual(decision["status"], "ADOPTABLE")
        self.assertEqual(decision["proposal"], payload["proposals"][0])
        self.assertEqual(set(decision["source_refs"]), {"advisory", "compatibility"})
        self.assertIn("RESOLVED_INVENTORY_REQUIRED", decision["pending_delivery_requirements"])
        self.assertIn("SHIPPED_INVENTORY_IF_APPLICABLE", decision["pending_delivery_requirements"])
        self.assertNotIn("verdict", result)
        self.assertIsNone(result["record_digest"])


if __name__ == "__main__":
    unittest.main()
