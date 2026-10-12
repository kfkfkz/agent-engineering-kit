#!/usr/bin/env python3
"""Real builtin authorization, with only the host event boundary simulated."""
from __future__ import annotations
import unittest
from pathlib import Path
import sys
import json
import tempfile
import importlib.util
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aek.core.security.authority import HostEvent, digest


class LiveHost:
    """Fake only the trusted host boundary: exact issued-event membership."""
    def __init__(self):
        self.live, self.history = {}, {}

    def capabilities(self):
        return {"available": True, "mode": "test-host-direct-events"}

    def issue(self, authority, event_kind, operation, binding, actor="user", **overrides):
        event = HostEvent("event-" + str(len(self.history)), event_kind, actor, authority.task_id,
            authority.repository_ref, authority.scope_ref, authority.current_identity.envelope_digest,
            operation, digest(binding), authority._clock().isoformat(), authority.expires_at)
        if overrides:
            from dataclasses import replace
            event = replace(event, **overrides)
        token = object()
        self.live[token] = event
        self.history[event.event_id] = event
        return token

    def consume(self, kind, token):
        if token not in self.live:
            raise ValueError("SECURITY_AUTHORITY_INVALID")
        return self.live.pop(token)

    def restore(self, event):
        return self.history.get(event.event_id) == event

    def on_gate_composed(self, event, binding):
        self.history[event.event_id] = event


def make_authority(host=None, **kwargs):
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.core.context.identity import build_identity
    identity = build_identity({n: {"digest": "a" * 64} for n in
                               ("subject", "artifact", "policy", "context", "evidence")})
    return BuiltinWorkflowAuthority(repository_ref="/repo", task_id="task", scope_ref="scope",
        current_identity=identity, author="author", host_source=host, **kwargs)


def source_metadata(authority, ref="EV-1", kind="scope"):
    from aek.core.context.identity import identity_as_dict
    return {"id": ref, "ref": ref, "run_id": None, "producer_ref": "tool",
        "identity": identity_as_dict(authority.current_identity), "required_components": ["subject.digest"],
        "kind": kind, "status": "DECLARED", "checked_at": authority._clock().isoformat(),
        "scope_ref": authority.scope_ref, "tool_version": None, "rules_digest": None,
        "database_digest": None, "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": [],
        "summary": "Controlled independent observation"}


def workflow_fixture(directory, scope_ref=None, dependency=False, record_prepare=None):
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.adapters.security_store import SecurityStore
    from aek.application.security_workflow import LocalSecurityWorkflow
    from aek.core.security.record import new_security_record, finding_fingerprint
    module_spec = importlib.util.spec_from_file_location("model_fixtures", Path(__file__).with_name("security-model.test.py"))
    fixtures = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(fixtures)
    payload = fixtures.request_payload()
    scope_ref = payload["scope"]["subject_digest"] if scope_ref is None else scope_ref
    payload["findings"] = [fixtures.finding_payload()]
    if dependency:
        payload["findings"][0].update(category="DEPENDENCY", dependency_ref="DEP-1")
        payload["proposals"] = [{"id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
            "version": "1.2.3", "purpose": "HTTP client", "scope": "production", "design_refs": ["D2"],
            "evidence_refs": ["advisory", "compatibility"]}]
        payload["stage"] = "closeout"
    record = new_security_record(json.dumps(payload).encode(), review_id="review-a")
    root = Path(directory)
    (root / "evidence" / "review-a").mkdir(parents=True)
    (root / "provenance").mkdir()
    (root / "sources").mkdir()
    host = LiveHost()
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.core.context.identity import decode_identity
    authority = BuiltinWorkflowAuthority(repository_ref=str(root), task_id="task", scope_ref=scope_ref,
        current_identity=decode_identity(payload["identity"]), author="author", host_source=host,
        provenance_root=root / "provenance")
    source = BuiltinSourceEvidencePort(authority, root=root / "sources")
    store = SecurityStore(root, "evidence/review-a/record.json")
    workflow = LocalSecurityWorkflow(authority=authority, source_port=source, store=store)
    if record_prepare is not None:
        record = record_prepare(workflow, host, record, payload)
    store.create(record)
    fingerprint = finding_fingerprint(payload["findings"][0], payload["identity"]["components"]["subject"].get("repo_digest"))
    return workflow, host, fingerprint


def add_source(workflow, host, ref, kind, facts, producer="tool"):
    from aek.adapters.security_evidence import source_binding
    metadata = source_metadata(workflow.authority, ref, kind)
    metadata["producer_ref"] = producer
    metadata["required_components"] = ["subject.scope_digest"]
    binding = source_binding(metadata, facts)
    token = host.issue(workflow.authority, "tool-completed", "source", binding, actor=producer)
    handle = workflow.authority.on_source_completed(token, binding=binding)
    workflow.source_port.register(metadata, facts, handle)


def prepare_dependency_record(workflow, host, record, payload, shipped_version="1.2.3"):
    from aek.core.security.record import record_as_dict, seal_security_record
    value = record_as_dict(record)
    proposal = payload["proposals"][0]
    package = {k: proposal[k] for k in ("ecosystem", "package", "version")}
    add_source(workflow, host, "advisory", "advisory", {**package, "advisories": []})
    add_source(workflow, host, "compatibility", "compatibility", {**package, "compatible": True})
    for kind in ("RESOLVED", "SHIPPED"):
        ref = kind.lower()
        item = {"id": "INV-" + ref, "kind": kind, "scope_ref": workflow.authority.scope_ref,
            "identity_digest": workflow.authority.current_identity.envelope_digest, "producer_ref": "tool",
            "packages": [{"id": "PKG-" + ref, "ecosystem": package["ecosystem"], "package": package["package"],
                "version": shipped_version if kind == "SHIPPED" else package["version"], "scope": proposal["scope"],
                "relationship": "direct", "introduced_by": [], "dependency_path": [], "source_ref": ref}],
            "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}
        add_source(workflow, host, ref, "inventory", {"inventory": item})
        value["inventories"].append(item)
    add_source(workflow, host, "delivery-scope", "delivery-scope", {"shipped_required": True})
    add_source(workflow, host, "normal", "regression", {"passed": True, "dependency_ref": proposal["id"], "version": proposal["version"]})
    value["sources"] = [workflow.source_port.lookup(ref).metadata for ref in
                        ("advisory", "compatibility", "resolved", "shipped", "delivery-scope", "normal")]
    value["dependency_decisions"] = [{"dependency_ref": proposal["id"], "proposal": proposal, "status": "ADOPTABLE",
        "reason_codes": [], "source_refs": ["advisory", "compatibility"],
        "pending_delivery_requirements": ["RESOLVED_INVENTORY_REQUIRED", "SHIPPED_INVENTORY_IF_APPLICABLE"]}]
    return seal_security_record(value)


def prepare_accepted_dependency_record(workflow, host, record, payload):
    from aek.core.security.record import record_as_dict, seal_security_record
    from aek.core.security.dependencies import dependency_evidence_digest
    if not payload["proposals"]:
        payload["proposals"] = [{"id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
            "version": "1.2.3", "purpose": "HTTP client", "scope": "production", "design_refs": ["D2"],
            "evidence_refs": ["advisory", "compatibility"]}]
    record = prepare_dependency_record(workflow, host, record, payload)
    value = record_as_dict(record)
    proposal = value["dependency_decisions"][0]["proposal"]
    package = {k: proposal[k] for k in ("ecosystem", "package", "version")}
    add_source(workflow, host, "affected", "advisory", {**package, "advisories": [{"canonical_id": "GHSA-fixture",
        "aliases": [], "source_url": "https://example.org/advisory", "affected_range": "<1.3.0",
        "fixed_versions": ["1.3.0"], "checked_at": workflow.authority._clock().isoformat(),
        "source_digest": "f" * 64, "match_status": "AFFECTED"}]})
    proposal["evidence_refs"].append("affected")
    material = tuple({"metadata": workflow.source_port.lookup(ref).metadata,
                      "facts": workflow.source_port.lookup(ref).facts,
                      "provenance_ref": workflow.source_port.lookup(ref).provenance_ref}
                     for ref in proposal["evidence_refs"])
    binding = {"operation": "accept-risk", "dependency_ref": proposal["id"], "version": proposal["version"],
        "scope_ref": workflow.authority.scope_ref, "identity_digest": workflow.authority.current_identity.envelope_digest,
        "expires_at": workflow.authority.expires_at, "evidence_digest": dependency_evidence_digest(material)}
    token = host.issue(workflow.authority, "user-confirmation", "accept-risk", binding, actor="k")
    handle = workflow.authority.on_user_confirmation(token, binding=binding)
    accepted_ref = workflow.authority.validate(handle, operation="accept-risk", binding=binding)["provenance_ref"]
    proposal["evidence_refs"].append(accepted_ref)
    value["sources"].append(workflow.source_port.lookup("affected").metadata)
    value["dependency_decisions"][0]["reason_codes"] = ["KNOWN_AFFECTED_DEPENDENCY", "ACCEPTED_RISK"]
    return seal_security_record(value)


def reviewer_handle(workflow, host, fingerprint, evidence_refs, operation="reverify", worker="worker"):
    from aek.application.review import full_review_request, reviewer_receipt
    from aek.core.review.incremental import ReviewSnapshot
    import hashlib
    documents = {"review.txt": "Independent security review"}
    hashes = tuple((k, hashlib.sha256(v.encode()).hexdigest()) for k, v in documents.items())
    snapshot = ReviewSnapshot("security", hashes, (), "a" * 64, True, False)
    record = workflow.store.load()
    request = full_review_request(snapshot, documents=documents, section_texts={}, reason="SECURITY",
        supporting_context={"security-record": record.payload_json,
                            "security-identity": json.dumps(json.loads(record.payload_json)["identity"], sort_keys=True, separators=(",", ":"))})
    binding = workflow.register_review(worker, request=request, finding_fingerprints=(fingerprint,),
        evidence_refs=evidence_refs, operation=operation)
    token = host.issue(workflow.authority, "review-completed", operation, binding, actor=worker)
    handle = workflow.on_review_completed(token, worker_id=worker, request=request,
        result_text=json.dumps({"stage": "security", "doc_hashes": dict(hashes), "issues": []}),
        receipt_text=reviewer_receipt(request), binding=binding)
    return handle


def gate_inputs(workflow, evidence_status="PASS"):
    from aek.core.context.identity import compare_identity
    from aek.application.review_evidence import ReviewEvidencePlan
    return {"route_result": {"schema_version": 1, "ok": True, "effective_route": "lightweight",
        "route_verdict": {"status": "DECIDED"},
        "requirements": {"required_checks": ["security-review"]},
        "governance": {"verdict": "DECIDED", "profile": "test", "matched_rules": [],
            "risk_requirements": {"checks": ["security-review"], "reviews": [], "receipts": [], "artifacts": []},
            "provenance": [{"kind": "checks", "requirement": "security-review", "source": "risk_overlay:security"}]}},
        "identity": compare_identity(workflow.authority.current_identity, workflow.authority.current_identity,
                                     ("subject.scope_digest",)),
        "review_plan": ReviewEvidencePlan((), (), (), "high", True),
        "evidence_by_requirement": {"checks:security-review": {"status": evidence_status,
                                                     "evidence_refs": ["negative", "normal"]}}}


class SecurityWorkflowTests(unittest.TestCase):
    def test_scope_context_drift_cannot_claim_or_restore_current_record_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, _host, fingerprint = workflow_fixture(directory, scope_ref="a" * 64)
            original = workflow.store.load()
            workflow.authority.scope_ref = "b" * 64
            with self.assertRaisesRegex(ValueError, "SCOPE_BINDING_MISMATCH"):
                workflow.claim_fixed(fingerprint, event_id="wrong-scope", expected_revision_digest=original.revision_digest)
            self.assertEqual(workflow.store.load(), original)
            self.assertFalse(workflow.recover()["closed_valid"])

    def test_generic_tool_report_cannot_impersonate_an_executed_scanner_at_delivery(self):
        from aek.adapters.security_evidence import source_binding
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            add_source(workflow, host, "normal", "regression", {"passed": True})
            handle = reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verify", expected_revision_digest=fixed.revision_digest)
            metadata = source_metadata(workflow.authority, "imported-scan", "scanner")
            metadata.update(run_id="invented-run", tool_version="8.30.1", required_components=["subject.scope_digest"])
            facts = {"tool_id": "gitleaks", "status": "PASSED", "plan_digest": "1" * 64}
            binding = source_binding(metadata, facts)
            token = host.issue(workflow.authority, "tool-completed", "source", binding, actor="tool")
            source_handle = workflow.authority.on_source_completed(token, binding=binding)
            workflow.source_port.register(metadata, facts, source_handle)
            inputs = gate_inputs(workflow)
            route = inputs["route_result"]
            route["requirements"]["required_checks"].append("security-scan:gitleaks")
            route["governance"]["risk_requirements"]["checks"].append("security-scan:gitleaks")
            route["governance"]["provenance"].append({"kind": "checks", "requirement": "security-scan:gitleaks",
                                                    "source": "risk_overlay:security"})
            inputs["evidence_by_requirement"]["checks:security-scan:gitleaks"] = {"status": "PASS", "evidence_refs": ["imported-scan"]}
            with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                workflow.on_gate_composed(**inputs, event_id="scan-close", expected_revision_digest=verified.revision_digest)
            self.assertEqual(workflow.store.load(), verified)

    def test_conflicting_current_negative_or_normal_regression_cannot_be_selected_as_success(self):
        for failed_kind in ("negative-test", "regression"):
            with tempfile.TemporaryDirectory() as directory:
                workflow, host, fingerprint = workflow_fixture(directory)
                fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
                add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                    "passed": True, "original_condition_absent": True})
                add_source(workflow, host, "normal", "regression", {"passed": True})
                failed = {"passed": False}
                if failed_kind == "negative-test":
                    failed.update(finding_fingerprint=fingerprint, original_condition_absent=False)
                add_source(workflow, host, "failed", failed_kind, failed)
                refs = ("negative", "normal", "failed")
                handle = reviewer_handle(workflow, host, fingerprint, refs)
                with self.subTest(failed_kind=failed_kind):
                    with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                        workflow.reverify(fingerprint, handle=handle, evidence_refs=refs, event_id="verify",
                            expected_revision_digest=fixed.revision_digest)
                    self.assertEqual(workflow.store.load(), fixed)

    def test_current_risk_disposition_does_not_turn_an_affected_dependency_into_verified_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory, scope_ref="a" * 64, dependency=True,
                                                           record_prepare=prepare_accepted_dependency_record)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            refs = ("negative", "normal", "advisory", "compatibility", "affected", "resolved", "shipped", "delivery-scope")
            handle = reviewer_handle(workflow, host, fingerprint, refs)
            with self.assertRaisesRegex(ValueError, "DEPENDENCY_STILL_AFFECTED"):
                workflow.reverify(fingerprint, handle=handle, evidence_refs=refs, event_id="verify",
                    expected_revision_digest=fixed.revision_digest)
            self.assertEqual(workflow.store.load(), fixed)

    def test_accepted_dependency_receipt_only_satisfies_the_recomputed_dependency_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory, scope_ref="a" * 64,
                                                           record_prepare=prepare_accepted_dependency_record)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            handle = reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verify", expected_revision_digest=fixed.revision_digest)
            value = json.loads(verified.payload_json)
            approved_ref = value["dependency_decisions"][0]["proposal"]["evidence_refs"][-1]
            required = tuple(f"{n}.{k}" for n, fields in workflow.authority.current_identity.components for k, _v in fields)
            dependency_row = workflow._service().compose_evidence(verified,
                current_identity=workflow.authority.current_identity, required_components=required,
                requirements=("checks:dependency-delivery-check",))["checks:dependency-delivery-check"]
            self.assertEqual(dependency_row["status"], "PASS")
            self.assertIn(approved_ref, dependency_row["evidence_refs"])
            inputs = gate_inputs(workflow)
            inputs["route_result"]["requirements"]["required_checks"].append("dependency-delivery-check")
            governance = inputs["route_result"]["governance"]
            governance["risk_requirements"]["checks"].append("dependency-delivery-check")
            governance["provenance"].append({"kind": "checks", "requirement": "dependency-delivery-check",
                                             "source": "risk_overlay:security"})
            inputs["evidence_by_requirement"]["checks:dependency-delivery-check"] = {
                "status": "PASS", "evidence_refs": dependency_row["evidence_refs"]}
            other = json.loads(json.dumps(inputs["route_result"]))
            other["requirements"]["required_checks"].append("other-check")
            other["governance"]["risk_requirements"]["checks"].append("other-check")
            other["governance"]["provenance"].append({"kind": "checks", "requirement": "other-check",
                                                      "source": "risk_overlay:security"})
            with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                workflow.on_gate_composed(**{**inputs, "route_result": other,
                    "evidence_by_requirement": {**inputs["evidence_by_requirement"],
                        "checks:other-check": {"status": "PASS", "evidence_refs": [approved_ref]}}},
                    event_id="wrong-purpose", expected_revision_digest=verified.revision_digest)
            closed = workflow.on_gate_composed(**inputs, event_id="close", expected_revision_digest=verified.revision_digest)["record"]
            self.assertTrue(workflow.recover()["closed_valid"])
            self.assertEqual(json.loads(closed.payload_json)["dependency_decisions"][0]["reason_codes"],
                             ["KNOWN_AFFECTED_DEPENDENCY", "ACCEPTED_RISK"])

    def test_real_dto_inventory_and_current_advisory_allow_dependency_verification_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory, scope_ref="a" * 64, dependency=True,
                                                           record_prepare=prepare_dependency_record)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            refs = ("negative", "normal", "advisory", "compatibility", "resolved", "shipped", "delivery-scope")
            handle = reviewer_handle(workflow, host, fingerprint, refs)
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=refs, event_id="verify",
                expected_revision_digest=fixed.revision_digest)
            self.assertEqual(json.loads(verified.payload_json)["lifecycle"]["issues"][0]["state"], "VERIFIED")
            self.assertTrue(workflow.recover()["verified_valid"])
            observation = workflow.source_port.lookup("shipped")
            event = workflow.authority.read_provenance(observation.provenance_ref, live=True)["event"]
            del host.history[event["event_id"]]
            self.assertFalse(workflow.recover()["verified_valid"])
            self.assertEqual(workflow.store.load(), verified)

    def test_old_shipped_dto_cannot_verify_even_if_cached_decision_says_adoptable(self):
        with tempfile.TemporaryDirectory() as directory:
            def old_shipped(workflow, host, record, payload):
                return prepare_dependency_record(workflow, host, record, payload, shipped_version="1.0.0")
            workflow, host, fingerprint = workflow_fixture(directory, scope_ref="a" * 64, dependency=True,
                                                           record_prepare=old_shipped)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            refs = ("negative", "normal", "advisory", "compatibility", "resolved", "shipped", "delivery-scope")
            handle = reviewer_handle(workflow, host, fingerprint, refs)
            with self.assertRaisesRegex(ValueError, "DEPENDENCY_STILL_AFFECTED"):
                workflow.reverify(fingerprint, handle=handle, evidence_refs=refs, event_id="verify",
                    expected_revision_digest=fixed.revision_digest)
            self.assertEqual(workflow.store.load(), fixed)

    def test_legacy_affected_false_inventory_booleans_never_verify_dependency_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory, dependency=True)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            add_source(workflow, host, "normal", "regression", {"passed": True})
            for kind in ("RESOLVED", "SHIPPED"):
                add_source(workflow, host, kind.lower(), "inventory", {"kind": kind,
                    "finding_fingerprint": fingerprint, "affected": False})
            refs = ("negative", "normal", "resolved", "shipped")
            handle = reviewer_handle(workflow, host, fingerprint, refs)
            with self.assertRaisesRegex(ValueError, "DEPENDENCY_STILL_AFFECTED"):
                workflow.reverify(fingerprint, handle=handle, evidence_refs=refs, event_id="verify",
                                  expected_revision_digest=fixed.revision_digest)
            self.assertEqual(workflow.store.load(), fixed)

    def test_dependency_approval_is_bound_to_current_advisory_evidence_digest(self):
        host = LiveHost()
        authority = make_authority(host)
        binding = {"operation": "accept-risk", "dependency_ref": "DEP-1", "version": "1.2.3",
            "scope_ref": authority.scope_ref, "identity_digest": authority.current_identity.envelope_digest,
            "expires_at": authority.expires_at, "evidence_digest": "a" * 64}
        token = host.issue(authority, "user-confirmation", "accept-risk", binding)
        handle = authority.on_user_confirmation(token, binding=binding)
        decision_ref = authority.validate(handle, operation="accept-risk", binding=binding)["provenance_ref"]
        context = {"scope_ref": authority.scope_ref, "identity_digest": authority.current_identity.envelope_digest,
                   "dependency_ref": "DEP-1", "version": "1.2.3"}
        self.assertTrue(authority.validate_disposition(decision_ref, **context, evidence_digest="a" * 64))
        self.assertFalse(authority.validate_disposition(decision_ref, **context, evidence_digest="b" * 64))
        self.assertFalse(authority.validate_disposition(decision_ref, **context, evidence_digest="approved"))
        self.assertTrue(authority.validate_disposition(decision_ref, **context))
        with self.assertRaises(ValueError):
            authority.on_user_confirmation({"approved": True, "evidence_digest": "b" * 64}, binding=binding)

    def test_default_workflow_service_consumes_real_source_ports_for_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory, scope_ref="a" * 64)
            record = workflow.store.load()
            body = json.loads(record.payload_json)
            package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
            add_source(workflow, host, "advisory", "advisory", {**package, "advisories": []})
            add_source(workflow, host, "compatibility", "compatibility", {**package, "compatible": True})
            payload = {"schema_version": 1, "operation": "evaluate", "stage": "design",
                "scope": body["scope"], "identity": body["identity"], "surfaces": [], "findings": [],
                "inventories": [], "run_refs": [], "invariant_refs": [],
                "proposals": [{"id": "DEP-1", **package, "purpose": "HTTP client", "scope": "production",
                    "design_refs": ["D2"], "evidence_refs": ["advisory", "compatibility"]}]}
            before_handles = dict(workflow.authority._handles)
            result = workflow.run_workflow((json.dumps(payload).encode(),),
                required_components=("subject.scope_digest",))[0]
            self.assertEqual(result["dependency_decisions"][0]["status"], "ADOPTABLE")
            self.assertEqual(set(result["dependency_decisions"][0]["source_refs"]), {"advisory", "compatibility"})
            self.assertEqual(workflow.authority._handles, before_handles)
            self.assertEqual(workflow.store.load(), record)
    def test_runtime_grant_must_cover_the_entire_execution_budget(self):
        from aek.adapters.security_authority import runtime_binding
        now = datetime.now(timezone.utc)
        host = LiveHost()
        authority = make_authority(host, clock=lambda: now)
        arguments = {"plan_digest": "a" * 64, "repository_ref": "/repo", "scope_ref": "scope",
            "identity_digest": authority.current_identity.envelope_digest, "tool_id": "trivy",
            "executable_digest": "b" * 64, "action": "scan", "network_mode": "deny", "write_scope": (),
            "timeout_seconds": 2, "max_output_bytes": 1000, "max_items": 10}
        binding = runtime_binding(**arguments)
        token = host.issue(authority, "user-confirmation", "execute", binding,
            expires_at=(now + timedelta(seconds=1)).isoformat())
        grant = authority.on_user_confirmation(token, binding=binding)
        with self.assertRaisesRegex(ValueError, "SECURITY_AUTHORITY_INVALID"):
            authority.validate_runtime_grant(grant, **arguments)

    def test_scope_fact_cannot_satisfy_security_review_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            add_source(workflow, host, "normal", "regression", {"passed": True})
            handle = reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verify", expected_revision_digest=fixed.revision_digest)
            add_source(workflow, host, "scope-only", "scope", {"dependency_change": False, "passed": True})
            inputs = gate_inputs(workflow)
            inputs["evidence_by_requirement"]["checks:security-review"]["evidence_refs"] = ["scope-only"]
            with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                workflow.on_gate_composed(**inputs, event_id="close", expected_revision_digest=verified.revision_digest)
            self.assertEqual(workflow.store.load(), verified)
            inputs = gate_inputs(workflow)
            inputs["route_result"]["requirements"]["required_checks"].append("security-scan:gitleaks")
            governance = inputs["route_result"]["governance"]
            governance["risk_requirements"]["checks"].append("security-scan:gitleaks")
            governance["provenance"].append({"kind": "checks", "requirement": "security-scan:gitleaks",
                                             "source": "risk_overlay:security"})
            inputs["evidence_by_requirement"]["checks:security-scan:gitleaks"] = {
                "status": "PASS", "evidence_refs": ["scope-only"]}
            with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                workflow.on_gate_composed(**inputs, event_id="scanner-close", expected_revision_digest=verified.revision_digest)
            self.assertEqual(workflow.store.load(), verified)

    def test_workflow_readonly_dispatch_uses_service_and_never_creates_source_handles(self):
        from aek.application.security_review import SecurityReviewService
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            record = workflow.store.load()
            value = json.loads(record.payload_json)
            payload = {"schema_version": 1, "operation": "report", "stage": value["stage"], "scope": value["scope"],
                "identity": value["identity"], "record_ref": workflow.store.record_ref,
                "expected_revision_digest": record.revision_digest, "format": "json"}
            responses = workflow.run_workflow((json.dumps(payload).encode(),), required_components=("subject.scope_digest",),
                review_service=SecurityReviewService(record_port=workflow.store))
            self.assertEqual(responses[0]["record_digest"], record.revision_digest)
            self.assertEqual(workflow.store.load(), record)
            self.assertEqual(workflow.authority._handles, {})
            payload["role"] = "JUDGE"
            with self.assertRaisesRegex(ValueError, "INVALID_SECURITY_INPUT"):
                workflow.run_workflow((json.dumps(payload).encode(),), required_components=("subject.scope_digest",))

    def test_host_identity_scope_task_kind_and_time_are_checked_before_minting(self):
        from aek.adapters.security_authority import runtime_binding
        host = LiveHost()
        authority = make_authority(host)
        binding = runtime_binding(plan_digest="a" * 64, repository_ref="/repo", scope_ref="scope",
            identity_digest=authority.current_identity.envelope_digest, tool_id="trivy", executable_digest="b" * 64,
            action="scan", network_mode="deny", write_scope=(), timeout_seconds=1, max_output_bytes=1000, max_items=10)
        for change in ({"scope_ref": "other"}, {"task_id": "other"}, {"identity_digest": "f" * 64},
                       {"repository_ref": "/other"}, {"kind": "user-rejected"},
                       {"occurred_at": (authority._clock() + timedelta(hours=2)).isoformat()},
                       {"expires_at": (authority._clock() - timedelta(seconds=1)).isoformat()}):
            token = host.issue(authority, "user-confirmation", "execute", binding, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                authority.on_user_confirmation(token, binding=binding)
        with self.assertRaises(ValueError):
            authority.on_user_confirmation({"sender": "user", "approved_by": "k"}, binding=binding)
        self.assertEqual(authority._handles, {})

    def test_live_source_receipt_revocation_and_ancestor_component_changes_are_observed(self):
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort, source_binding
        from aek.core.context.identity import identity_as_dict, build_identity
        host = LiveHost()
        authority = make_authority(host)
        port = BuiltinSourceEvidencePort(authority)
        metadata, facts = source_metadata(authority), {"dependency_change": False}
        binding = source_binding(metadata, facts)
        token = host.issue(authority, "tool-completed", "source", binding, actor="tool")
        handle = authority.on_source_completed(token, binding=binding)
        port.register(metadata, facts, handle)
        components = identity_as_dict(authority.current_identity)["components"]
        components["artifact"]["digest"] = "b" * 64
        authority.current_identity = build_identity(components)
        self.assertEqual(port.lookup("EV-1").metadata["status"], "VERIFIED")
        components["subject"]["digest"] = "b" * 64
        authority.current_identity = build_identity(components)
        self.assertEqual(port.lookup("EV-1").metadata["status"], "STALE")
        components["subject"]["digest"] = "a" * 64
        authority.current_identity = build_identity(components)
        host.history.clear()
        self.assertEqual(port.lookup("EV-1").metadata["status"], "DECLARED")

    def test_runtime_grant_cannot_be_serialized_forged_replayed_or_cross_bound(self):
        from aek.adapters.security_authority import runtime_binding
        from aek.core.security.authority import RuntimeGrant
        now = datetime.now(timezone.utc)
        host = LiveHost()
        authority = make_authority(host, clock=lambda: now)
        arguments = {"plan_digest": "a" * 64, "repository_ref": "/repo", "scope_ref": "scope",
            "identity_digest": authority.current_identity.envelope_digest, "tool_id": "trivy",
            "executable_digest": "b" * 64, "action": "scan", "network_mode": "deny", "write_scope": ("cache",),
            "timeout_seconds": .1, "max_output_bytes": 1000, "max_items": 10}
        binding = runtime_binding(**arguments)
        token = host.issue(authority, "user-confirmation", "execute", binding)
        grant = authority.on_user_confirmation(token, binding=binding)
        authority.validate_runtime_grant(grant, **arguments)
        with self.assertRaises(TypeError):
            json.dumps(grant)
        for forged in ({"approved_by": "user", "plan_digest": "a" * 64}, object.__new__(RuntimeGrant)):
            with self.assertRaises(ValueError):
                authority.validate_runtime_grant(forged, **arguments)
        with self.assertRaises(ValueError):
            authority.on_user_confirmation(token, binding=binding)
        for changed in ({"plan_digest": "c" * 64}, {"executable_digest": "c" * 64},
                        {"scope_ref": "other"}, {"network_mode": "offline"}, {"max_items": 11},
                        {"timeout_seconds": True}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                authority.validate_runtime_grant(grant, **{**arguments, **changed})
        authority._clock = lambda: now + timedelta(hours=2)
        with self.assertRaises(ValueError):
            authority.validate_runtime_grant(grant, **arguments)

    def test_author_or_other_request_completion_never_becomes_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            with self.assertRaises(ValueError):
                reviewer_handle(workflow, host, fingerprint, ("negative",), worker="author")
            handle = reviewer_handle(workflow, host, fingerprint, ("negative",))
            original = workflow.store.load()
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=original.revision_digest)
            with self.assertRaises(ValueError):
                workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative",), event_id="verify",
                    expected_revision_digest=fixed.revision_digest)
            self.assertEqual(workflow.store.load(), fixed)

    def test_false_positive_independent_counterevidence_keeps_open_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            add_source(workflow, host, "counter", "counterevidence", {"finding_fingerprint": fingerprint,
                "false_positive": True})
            handle = reviewer_handle(workflow, host, fingerprint, ("counter",), operation="false-positive")
            resolved = workflow.resolve(fingerprint, kind="FALSE_POSITIVE", reason="Observed control rejects attack",
                evidence_refs=("counter",), expires_at=workflow.authority.expires_at,
                required_components=("subject.scope_digest",), handle=handle, event_id="counter",
                expected_revision_digest=workflow.store.load().revision_digest)
            value = json.loads(resolved.payload_json)
            self.assertEqual(value["lifecycle"]["issues"][0]["state"], "OPEN")
            self.assertTrue(workflow.valid_resolution(value["resolutions"][0]))
            host.history.clear()
            self.assertFalse(workflow.valid_resolution(value["resolutions"][0]))

    def test_incomplete_or_failed_normal_regression_cannot_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            add_source(workflow, host, "normal", "regression", {"passed": False})
            handle = reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                    event_id="verify", expected_revision_digest=fixed.revision_digest)
            self.assertEqual(workflow.store.load(), fixed)

    def test_only_real_ready_composer_closes_same_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            add_source(workflow, host, "normal", "regression", {"passed": True})
            handle = reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verify", expected_revision_digest=fixed.revision_digest)
            blocked = workflow.on_gate_composed(**gate_inputs(workflow, "PENDING"), event_id="blocked",
                expected_revision_digest=verified.revision_digest)
            self.assertEqual(blocked["result"]["verdict"], "NOT_READY")
            self.assertEqual(workflow.store.load(), verified)
            closed = workflow.on_gate_composed(**gate_inputs(workflow), event_id="close",
                expected_revision_digest=verified.revision_digest)["record"]
            value = json.loads(closed.payload_json)
            self.assertEqual(value["lifecycle"]["issues"][0]["state"], "CLOSED")
            self.assertEqual(value["gate_binding"]["before_lifecycle"]["issues"][0]["state"], "VERIFIED")
            self.assertTrue(workflow.recover()["closed_valid"])

    def test_risk_acceptance_is_a_disposition_and_never_fixes_or_passes_technical_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            record = workflow.store.load()
            binding = workflow.resolution_binding(fingerprint, kind="ACCEPTED_RISK", reason="Temporary exception",
                evidence_refs=(), expires_at=workflow.authority.expires_at,
                required_components=("subject.scope_digest",))
            token = host.issue(workflow.authority, "user-confirmation", "accept-risk", binding)
            handle = workflow.on_user_confirmation(token, binding=binding)
            resolved = workflow.resolve(fingerprint, kind="ACCEPTED_RISK", reason="Temporary exception",
                evidence_refs=(), expires_at=workflow.authority.expires_at,
                required_components=("subject.scope_digest",), handle=handle, event_id="risk",
                expected_revision_digest=record.revision_digest)
            value = json.loads(resolved.payload_json)
            self.assertEqual(value["lifecycle"]["issues"][0]["state"], "OPEN")
            self.assertEqual(value["resolutions"][0]["kind"], "ACCEPTED_RISK")
            self.assertIn("SOURCE_EVIDENCE_REQUIRED", value["required_evidence"])
            self.assertTrue(workflow.valid_resolution(value["resolutions"][0]))

    def test_only_current_independent_negative_and_regression_evidence_verify_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = workflow_fixture(directory)
            original = workflow.store.load()
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=original.revision_digest)
            self.assertEqual(json.loads(fixed.payload_json)["lifecycle"]["issues"][0]["state"], "FIXED")
            with self.assertRaises(ValueError):
                workflow.reverify(fingerprint, handle={"role": "REVIEWER"}, evidence_refs=("negative", "normal"),
                    event_id="verify", expected_revision_digest=fixed.revision_digest)
            add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            add_source(workflow, host, "normal", "regression", {"passed": True})
            handle = reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verify", expected_revision_digest=fixed.revision_digest)
            self.assertEqual(json.loads(verified.payload_json)["lifecycle"]["issues"][0]["state"], "VERIFIED")
            self.assertEqual(workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verify", expected_revision_digest=fixed.revision_digest), verified)

    def test_source_registration_checks_real_live_event_and_cannot_upgrade_import(self):
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort, source_binding
        host = LiveHost()
        authority = make_authority(host)
        port = BuiltinSourceEvidencePort(authority)
        metadata, facts = source_metadata(authority), {"dependency_change": False}
        binding = source_binding(metadata, facts)
        with self.assertRaises(ValueError):
            port.register(metadata, facts, {"role": "REVIEWER", "verified": True})
        token = host.issue(authority, "tool-completed", "source", binding, actor="tool")
        handle = authority.on_source_completed(token, binding=binding)
        port.register(metadata, facts, handle)
        observation = port.lookup("EV-1")
        self.assertEqual(observation.metadata["status"], "VERIFIED")
        self.assertEqual(observation.facts, facts)
        observation.facts["dependency_change"] = True
        self.assertFalse(port.lookup("EV-1").facts["dependency_change"])
        with self.assertRaises(ValueError):
            port.register(metadata, {"dependency_change": True}, handle)

    def test_missing_host_source_is_explicitly_readonly(self):
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.core.context.identity import build_identity
        identity = build_identity({n: {"digest": "a" * 64} for n in
                                   ("subject", "artifact", "policy", "context", "evidence")})
        authority = BuiltinWorkflowAuthority(repository_ref="/repo", task_id="task", scope_ref="scope",
            current_identity=identity, author="author")
        self.assertIn("AUTHORIZATION_SOURCE_UNAVAILABLE", authority.capabilities()["reason_codes"])
        with self.assertRaisesRegex(ValueError, "AUTHORIZATION_SOURCE_UNAVAILABLE"):
            authority.on_user_confirmation(object(), binding={"operation": "accept-risk"})


if __name__ == "__main__":
    unittest.main()
