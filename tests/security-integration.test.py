#!/usr/bin/env python3
"""Current security material participates in the existing design reviewer input."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("security_model_fixture", REPO / "tests/security-model.test.py")
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


class DesignSecurityIntegrationTests(unittest.TestCase):
    def test_service_user_invariant_confirmation_is_not_a_risk_acceptance_receipt(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import record_as_dict, seal_security_record
        from aek.core.security.knowledge import invariant_binding
        fixture_spec = importlib.util.spec_from_file_location("approval_knowledge_fixture", REPO / "tests/security-knowledge.test.py")
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        for operation in ("accept-risk", "confirm-invariant"):
            with self.subTest(operation=operation):
                record = fixture.record([])
                authority, host, port = fixture.context(record)
                body = record_as_dict(record)
                row = fixture.invariant()
                binding = {"operation": operation, "invariants": [invariant_binding(row)],
                    "scope_ref": authority.scope_ref, "identity_digest": authority.current_identity.envelope_digest,
                    "expires_at": authority.expires_at}
                token = host.issue(authority, "user-confirmation", operation, binding, actor="k")
                handle = authority.on_user_confirmation(token, binding=binding)
                approval = authority.validate(handle, operation=operation, binding=binding)["provenance_ref"]
                source = fixture.register(authority, host, port, "requirements", "approved-invariant",
                    {"invariants": [invariant_binding(row)], "approval_ref": approval})
                body.update(sources=[source["metadata"]], invariants=[row])
                candidate = seal_security_record(body)
                result = SecurityReviewService(source_port=port, approval_port=authority, clock=authority._clock).knowledge(
                    candidate, current_identity=authority.current_identity,
                    required_components=("subject.scope_digest", "artifact.design_digest"), repository_ref=authority.repository_ref)
                self.assertEqual(result["invariants"][0]["status"], "CONFIRMED" if operation == "confirm-invariant" else "CANDIDATE")
                self.assertNotIn("verdict", result)

    def test_service_projects_frozen_invariant_sources_without_trusting_stored_status(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import record_as_dict, seal_security_record
        from aek.core.security.knowledge import invariant_binding
        fixture_spec = importlib.util.spec_from_file_location("knowledge_fixture", REPO / "tests/security-knowledge.test.py")
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        record = fixture.record([])
        authority, host, port = fixture.context(record)
        body = record_as_dict(record)
        authority.scope_ref = body["scope"]["subject_digest"]
        row = fixture.invariant()
        source = fixture.register(authority, host, port, "requirements", "frozen-requirements",
                                  {"frozen": True, "invariants": [invariant_binding(row)]})
        body["sources"] = [source["metadata"]]
        body["invariants"] = [row]
        candidate = seal_security_record(body)
        service = SecurityReviewService(source_port=port, clock=authority._clock)
        args = dict(current_identity=authority.current_identity,
                    required_components=("subject.scope_digest", "artifact.design_digest"),
                    repository_ref=authority.repository_ref)
        projected = service.knowledge(candidate, **args)
        self.assertEqual(projected["invariants"][0]["status"], "CONFIRMED")
        self.assertEqual(record_as_dict(candidate)["invariants"][0]["status"], "CANDIDATE")
        host.history.clear()
        revoked = service.knowledge(candidate, **args)
        self.assertEqual(revoked["invariants"][0]["status"], "INVALIDATED")
        self.assertIn("INVARIANT_SOURCE_INVALIDATED", revoked["coverage_gaps"])
        self.assertNotIn("verdict", revoked)

    def test_design_adoption_delivery_reverification_report_and_original_composer_share_one_record(self):
        from aek.core.security.record import record_as_dict, seal_security_record
        fixture_spec = importlib.util.spec_from_file_location("security_workflow_fixture", REPO / "tests/security-workflow.test.py")
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory() as directory:
            def design_only(workflow, host, record, payload):
                proposal = payload["proposals"][0]
                package = {key: proposal[key] for key in ("ecosystem", "package", "version")}
                fixture.add_source(workflow, host, "advisory", "advisory", {**package, "advisories": []})
                fixture.add_source(workflow, host, "compatibility", "compatibility", {**package, "compatible": True})
                body = record_as_dict(record)
                body["sources"] = [workflow.source_port.lookup(ref).metadata for ref in ("advisory", "compatibility")]
                return seal_security_record(body)
            workflow, host, fingerprint = fixture.workflow_fixture(directory, scope_ref="a" * 64,
                dependency=True, record_prepare=design_only)
            service = workflow._service()
            record = workflow.store.load()
            body = record_as_dict(record)
            payload = {"schema_version": 1, "operation": "evaluate", "stage": "design",
                "scope": body["scope"], "identity": body["identity"], "surfaces": [], "findings": [],
                "inventories": [], "run_refs": [], "invariant_refs": [],
                "proposals": [row["proposal"] for row in body["dependency_decisions"]]}
            designed = workflow.run_workflow((json.dumps(payload).encode(),),
                required_components=("subject.scope_digest",))[0]
            self.assertEqual(designed["dependency_decisions"][0]["status"], "ADOPTABLE")
            key = "checks:dependency-delivery-check"
            args = dict(current_identity=workflow.authority.current_identity,
                        required_components=("subject.scope_digest",), requirements=(key,))
            self.assertEqual(service.compose_evidence(record, **args)[key]["status"], "PENDING")
            # The control writer appends current tool-completion observations,
            # while each actual source still requires its genuine host event.
            for kind in ("RESOLVED", "SHIPPED"):
                ref = kind.lower()
                item = {"id": "INV-" + ref, "kind": kind, "scope_ref": workflow.authority.scope_ref,
                    "identity_digest": workflow.authority.current_identity.envelope_digest, "producer_ref": "tool",
                    "packages": [{"id": "PKG-" + ref, "ecosystem": "maven", "package": "org.example:client",
                        "version": "1.2.3", "scope": "production", "relationship": "direct",
                        "introduced_by": [], "dependency_path": [], "source_ref": ref}],
                    "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}
                fixture.add_source(workflow, host, ref, "inventory", {"inventory": item})
                body["inventories"].append(item)
            fixture.add_source(workflow, host, "delivery-scope", "delivery-scope", {"shipped_required": True})
            fixture.add_source(workflow, host, "normal", "regression",
                {"passed": True, "dependency_ref": "DEP-1", "version": "1.2.3"})
            refs = ("advisory", "compatibility", "resolved", "shipped", "delivery-scope", "normal")
            body["sources"] = [workflow.source_port.lookup(ref).metadata for ref in refs]
            body["revision"] += 1
            body["events"].append({"event_id": "current-delivery-input", "operation": "capture",
                "input_digest": "1" * 64, "current_identity_digest": workflow.authority.current_identity.envelope_digest,
                "trusted_actor_ref": "author:author", "evidence_refs": list(refs),
                "occurred_at": workflow.authority._clock().isoformat()})
            candidate = seal_security_record(body)
            workflow.store.write(candidate, expected_revision_digest=record.revision_digest)
            self.assertEqual(service.compose_evidence(candidate, **args)[key]["status"], "PASS")
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=candidate.revision_digest)
            fixture.add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            verification_refs = ("negative", *refs)
            handle = fixture.reviewer_handle(workflow, host, fingerprint, verification_refs)
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=verification_refs,
                event_id="verified", expected_revision_digest=fixed.revision_digest)
            inputs = fixture.gate_inputs(workflow)
            route = inputs["route_result"]
            route["requirements"]["required_checks"].append("dependency-delivery-check")
            route["governance"]["risk_requirements"]["checks"].append("dependency-delivery-check")
            route["governance"]["provenance"].append({"kind": "checks", "requirement": "dependency-delivery-check",
                                                    "source": "risk_overlay:security"})
            inputs["evidence_by_requirement"][key] = service.compose_evidence(verified, **args)[key]
            inputs["evidence_by_requirement"]["checks:security-review"]["evidence_refs"] = list(verification_refs)
            result = workflow.on_gate_composed(**inputs, event_id="closed", expected_revision_digest=verified.revision_digest)
            self.assertEqual(result["result"]["verdict"], "READY")
            closed = result["record"]
            self.assertEqual(record_as_dict(closed)["lifecycle"]["issues"][0]["state"], "CLOSED")
            request = model.request_payload("report")
            request.update(record_ref=workflow.store.record_ref, expected_revision_digest=closed.revision_digest)
            reported = workflow.run_workflow((json.dumps(request).encode(),),
                required_components=("subject.scope_digest",))[0]
            self.assertEqual(reported["record_digest"], closed.revision_digest)
            self.assertEqual(reported["report"]["gate_binding"]["before_lifecycle"]["issues"][0]["state"], "VERIFIED")
            self.assertTrue(workflow.recover()["closed_valid"])

    def test_design_review_receives_pending_dependencies_and_invalidates_prior_input_on_record_change(self):
        from aek.adapters.security_store import SecurityStore
        from aek.core.security.record import new_security_record
        gate = runpy.run_path(str(REPO / "doc-gate"), run_name="security_doc_gate_fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            req = root / "docs/03-SDD/001-Test"
            shutil.copytree(REPO / "templates/requirements", req)
            (req / "security").mkdir()
            payload = model.request_payload()
            payload["proposals"] = [{"id": "DEP-1", "ecosystem": "maven", "package": "example:client",
                "version": "1.2.3", "purpose": "HTTP client", "scope": "production",
                "design_refs": ["D2"], "evidence_refs": ["unavailable"]}]
            record = new_security_record(json.dumps(payload).encode(), review_id="security")
            store = SecurityStore(req, "security/record.json")
            store.create(record)
            before, _ = gate["_build_review_request"](req, "概要设计")
            context = dict(before.supporting_context)
            material = json.loads(context["security/design-preconditions.json"])
            self.assertEqual(material["record_digest"], record.revision_digest)
            self.assertEqual(material["evaluation"]["dependency_decisions"][0]["status"], "PENDING")
            self.assertIn("CURRENT_IDENTITY_UNAVAILABLE", material["evaluation"]["reason_codes"])
            self.assertFalse(material["authority_verified"])
            self.assertNotIn("verdict", material)
            payload["proposals"][0]["version"] = "1.3.0"
            replacement = new_security_record(json.dumps(payload).encode(), review_id="security")
            # Simulate a different saved candidate revision, not an authorized transition.
            from aek.core.security.record import record_bytes
            (req / "security/record.json").write_bytes(record_bytes(replacement))
            after, _ = gate["_build_review_request"](req, "概要设计")
            self.assertEqual(before.doc_hashes, after.doc_hashes)
            self.assertNotEqual(before.request_digest, after.request_digest)
            self.assertEqual(store.load(), replacement)  # review-input did not write the record


if __name__ == "__main__":
    unittest.main()
