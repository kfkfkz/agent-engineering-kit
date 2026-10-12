#!/usr/bin/env python3
"""D7 derived views, with real builtin source/provenance verification."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixtures(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".test.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODEL = fixtures("security-model")
WORKFLOW = fixtures("security-workflow")


def record(findings, *, review_id="knowledge", identity=None, paths=None):
    from aek.core.security.record import new_security_record
    request = MODEL.request_payload()
    request["findings"] = findings
    if identity is not None:
        request["identity"] = identity
        request["scope"]["subject_digest"] = identity["components"]["subject"].get("scope_digest", request["scope"]["subject_digest"])
    if paths is not None:
        request["scope"]["paths"] = paths
    return new_security_record(json.dumps(request).encode(), review_id=review_id)


def context(current):
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.core.context.identity import decode_identity
    from aek.core.security.record import record_as_dict
    host = WORKFLOW.LiveHost()
    authority = BuiltinWorkflowAuthority(repository_ref="/repo", task_id="knowledge", scope_ref=record_as_dict(current)["scope"]["subject_digest"],
        current_identity=decode_identity(record_as_dict(current)["identity"]), author="author", host_source=host)
    return authority, host, BuiltinSourceEvidencePort(authority)


def register(authority, host, port, ref, kind, facts, *, coverage="COMPLETE_FOR_DECLARED_SCOPE", gaps=(), components=None, checked_at=None):
    from aek.adapters.security_evidence import source_binding
    metadata = WORKFLOW.source_metadata(authority, ref, kind)
    metadata.update(required_components=list(components or ("subject.scope_digest", "artifact.design_digest")),
                    coverage=coverage, coverage_gaps=list(gaps))
    if checked_at is not None:
        metadata["checked_at"] = checked_at
    binding = source_binding(metadata, facts)
    token = host.issue(authority, "tool-completed", "source", binding, actor="tool")
    handle = authority.on_source_completed(token, binding=binding)
    port.register(metadata, facts, handle)
    return observed(port, ref)


def observed(port, ref):
    value = port.lookup(ref)
    return None if value is None else {"metadata": value.metadata, "facts": value.facts,
                                      "provenance_ref": value.provenance_ref}


def baseline_source(authority, host, port, baseline):
    from aek.core.security.record import record_as_dict
    return register(authority, host, port, "baseline", "baseline", {
        "record_digest": baseline.revision_digest, "scope": record_as_dict(baseline)["scope"],
        "repository_ref": authority.repository_ref})


def compare(baseline, current, authority, source, observations=()):
    from aek.core.security.knowledge import compare_security_baseline
    return compare_security_baseline(baseline, current, baseline_observation=source,
        observations=observations, current_identity=authority.current_identity,
        required_components=("subject.scope_digest", "artifact.design_digest"),
        scope_ref=authority.scope_ref, repository_ref=authority.repository_ref, now=authority._clock())


def invariant():
    return {"id": "INV-1", "statement": "Every document read enforces the approved tenant boundary",
            "source_refs": ["requirements"], "entry_refs": ["REQ-1", "F1", "D1", "CONTROL-1"],
            "test_refs": ["TC-1"], "status": "CANDIDATE", "evidence_refs": []}


def derive(rows, authority, observations=(), **kwargs):
    from aek.core.security.knowledge import derive_security_invariants
    return derive_security_invariants(tuple(rows), observations,
        current_identity=authority.current_identity, required_components=("artifact.design_digest",),
        scope_ref=authority.scope_ref, now=authority._clock(), **kwargs)


def proxy_fingerprint(finding, current):
    from aek.core.security.record import finding_fingerprint, record_as_dict
    namespace = record_as_dict(current)["identity"]["components"]["subject"].get("repo_digest")
    return finding_fingerprint(finding, namespace)


class SecurityKnowledgeTests(unittest.TestCase):
    def test_missing_old_finding_without_revalidation_is_unverified(self):
        finding = MODEL.finding_payload()
        baseline, current = record([finding], review_id="baseline"), record([])
        authority, host, port = context(current)
        source = baseline_source(authority, host, port, baseline)
        result = compare(baseline, current, authority, source)
        self.assertEqual(result["classifications"][0]["classification"], "UNVERIFIED")
        self.assertIn("ABSENCE_REVALIDATION_REQUIRED", result["classifications"][0]["reason_codes"])

    def test_fresh_attested_historical_baseline_identifies_existing_and_introduced(self):
        from aek.core.context.identity import build_identity, identity_as_dict
        old_identity = MODEL.request_payload()["identity"]
        components = old_identity["components"]
        components["subject"]["scope_digest"] = "b" * 64
        old_identity = identity_as_dict(build_identity(components))
        finding = MODEL.finding_payload()
        baseline = record([finding], review_id="baseline", identity=old_identity)
        existing = {**finding, "id": "CURRENT-1", "title": "Reworded candidate"}
        introduced = {**finding, "id": "CURRENT-2", "location_refs": ["LOC-2"]}
        current = record([existing, introduced])
        authority, host, port = context(current)
        result = compare(baseline, current, authority, baseline_source(authority, host, port, baseline))
        self.assertEqual(len(result["classifications"]), 2)
        by_id = {row["current_finding_refs"][0]: row["classification"] for row in result["classifications"]}
        self.assertEqual(by_id, {"CURRENT-1": "EXISTING", "CURRENT-2": "INTRODUCED"})
        self.assertEqual(result["coverage_gaps"], [])

    def test_resolution_requires_complete_current_scope_and_exact_live_revalidation(self):
        from aek.core.security.record import finding_fingerprint
        baseline, current = record([MODEL.finding_payload()], review_id="baseline"), record([])
        authority, host, port = context(current)
        source = baseline_source(authority, host, port, baseline)
        fingerprint = proxy_fingerprint(MODEL.finding_payload(), current)
        proof = register(authority, host, port, "absence", "negative-test", {
            "baseline_record_digest": baseline.revision_digest, "record_digest": current.revision_digest,
            "finding_fingerprint": fingerprint, "covered_paths": ["src/app.py"],
            "passed": True, "original_condition_absent": True})
        before = (baseline.payload_json, current.payload_json)
        result = compare(baseline, current, authority, source, (proof,))
        self.assertEqual(result["classifications"][0]["classification"], "RESOLVED")
        self.assertEqual(result["classifications"][0]["evidence_refs"], ["baseline", "absence"])
        self.assertEqual((baseline.payload_json, current.payload_json), before)

    def test_reappearing_resolved_fingerprint_needs_attested_history_not_finding_label(self):
        from aek.core.security.record import finding_fingerprint, record_as_dict
        finding = MODEL.finding_payload()
        baseline, current = record([finding], review_id="baseline"), record([finding])
        authority, host, port = context(current)
        fingerprint = proxy_fingerprint(finding, current)
        source = register(authority, host, port, "history", "baseline", {
            "record_digest": baseline.revision_digest, "scope": record_as_dict(baseline)["scope"],
            "repository_ref": authority.repository_ref, "resolved_fingerprints": [fingerprint]})
        result = compare(baseline, current, authority, source)
        self.assertEqual(result["classifications"][0]["classification"], "REOPENED")
        plain_source = baseline_source(authority, host, port, baseline)
        self.assertEqual(compare(baseline, current, authority, plain_source)["classifications"][0]["classification"], "EXISTING")

    def test_author_claimed_confirmed_invariant_is_only_a_candidate(self):
        authority, _host, _port = context(record([]))
        row = {**invariant(), "status": "CONFIRMED", "evidence_refs": ["author-proof"]}
        result = derive([row], authority)
        self.assertEqual(result["invariants"][0]["status"], "CANDIDATE")
        self.assertIn("INVARIANT_SOURCE_UNAVAILABLE", result["coverage_gaps"])
        self.assertEqual(row["status"], "CONFIRMED")

    def test_frozen_requirement_confirms_only_the_exact_fully_bound_invariant(self):
        from aek.core.security.knowledge import invariant_binding
        row = invariant()
        authority, host, port = context(record([]))
        source = register(authority, host, port, "requirements", "frozen-requirements", {
            "frozen": True, "invariants": [invariant_binding(row)]}, components=("artifact.design_digest",))
        result = derive([row], authority, (source,))
        self.assertEqual(result["invariants"][0]["status"], "CONFIRMED")
        self.assertEqual(result["invariants"][0]["evidence_refs"], ["requirements"])
        self.assertEqual(result["coverage_gaps"], [])
        # Appending a derived evidence reference must not self-invalidate the
        # semantic statement on the next read of the same fixed record.
        again = derive(result["invariants"], authority, (source,))
        self.assertEqual(again["invariants"][0]["status"], "CONFIRMED")

    def test_actual_user_confirmation_reference_is_required_for_approved_invariant(self):
        from aek.core.security.knowledge import invariant_binding
        row = invariant()
        authority, host, port = context(record([]))
        binding = {"operation": "confirm-invariant",
                   "scope_ref": authority.scope_ref, "identity_digest": authority.current_identity.envelope_digest,
                   "expires_at": authority.expires_at,
                   "invariants": [invariant_binding(row)]}
        token = host.issue(authority, "user-confirmation", "confirm-invariant", binding)
        handle = authority.on_user_confirmation(token, binding=binding)
        approval = authority.validate(handle, operation="confirm-invariant", binding=binding)["provenance_ref"]
        source = register(authority, host, port, "requirements", "approved-invariant", {
            "invariants": [invariant_binding(row)], "approval_ref": approval})
        self.assertEqual(derive([row], authority, (source,))["invariants"][0]["status"], "CANDIDATE")
        result = derive([row], authority, (source,), confirmed_user_refs=(approval,))
        self.assertEqual(result["invariants"][0]["status"], "CONFIRMED")
        self.assertIn(approval, result["invariants"][0]["evidence_refs"])

    def test_revoked_or_changed_document_source_invalidates_without_trusting_row_status(self):
        from aek.core.context.identity import build_identity, identity_as_dict
        from aek.core.security.knowledge import invariant_binding
        for change in ("revoked", "document_changed"):
            with self.subTest(change=change):
                row = invariant()
                authority, host, port = context(record([]))
                register(authority, host, port, "requirements", "frozen-design", {
                    "frozen": True, "invariants": [invariant_binding(row)]}, components=("artifact.design_digest",))
                if change == "revoked":
                    host.history.clear()
                else:
                    components = identity_as_dict(authority.current_identity)["components"]
                    components["artifact"]["design_digest"] = "f" * 64
                    authority.current_identity = build_identity(components)
                stale = observed(port, "requirements")
                self.assertIn(stale["metadata"]["status"], {"DECLARED", "STALE"})
                result = derive([row], authority, (stale,))
                self.assertEqual(result["invariants"][0]["status"], "INVALIDATED")
                self.assertIn("INVARIANT_SOURCE_INVALIDATED", result["coverage_gaps"])

    def test_empty_lists_do_not_prove_a_missing_baseline_source_is_valid(self):
        baseline, current = record([], review_id="baseline"), record([])
        authority, _host, _port = context(current)
        result = compare(baseline, current, authority, None)
        self.assertEqual(result["classifications"], [])
        self.assertIn("BASELINE_SOURCE_UNVERIFIED", result["coverage_gaps"])

    def test_conflicting_live_absence_observations_cannot_resolve_by_order(self):
        from aek.core.security.record import finding_fingerprint
        baseline, current = record([MODEL.finding_payload()], review_id="baseline"), record([])
        authority, host, port = context(current)
        source = baseline_source(authority, host, port, baseline)
        facts = {"baseline_record_digest": baseline.revision_digest, "record_digest": current.revision_digest,
                 "finding_fingerprint": proxy_fingerprint(MODEL.finding_payload(), current),
                 "covered_paths": ["src/app.py"], "passed": True, "original_condition_absent": True}
        clean = register(authority, host, port, "clean", "negative-test", facts)
        present = register(authority, host, port, "present", "negative-test", {**facts, "original_condition_absent": False})
        for observations in ((clean, present), (present, clean)):
            with self.subTest(order=[item["metadata"]["ref"] for item in observations]):
                result = compare(baseline, current, authority, source, observations)
                self.assertEqual(result["classifications"][0]["classification"], "UNVERIFIED")
                self.assertIn("ABSENCE_REVALIDATION_CONFLICT", result["coverage_gaps"])

    def test_partial_stale_or_wrongly_bound_absence_evidence_stays_unverified(self):
        from datetime import timedelta
        from aek.core.security.record import finding_fingerprint
        for defect in ("partial", "gap", "stale", "old-record", "new-record", "fingerprint", "scope"):
            with self.subTest(defect=defect):
                baseline, current = record([MODEL.finding_payload()], review_id="baseline"), record([])
                authority, host, port = context(current)
                source = baseline_source(authority, host, port, baseline)
                facts = {"baseline_record_digest": baseline.revision_digest, "record_digest": current.revision_digest,
                         "finding_fingerprint": proxy_fingerprint(MODEL.finding_payload(), current),
                         "covered_paths": ["src/app.py"], "passed": True, "original_condition_absent": True}
                options = {}
                if defect == "partial":
                    options["coverage"] = "PARTIAL"
                elif defect == "gap":
                    options["gaps"] = ("PRIVATE_TARGET_UNKNOWN",)
                elif defect == "stale":
                    options["checked_at"] = (authority._clock() - timedelta(days=8)).isoformat()
                elif defect == "old-record":
                    facts["baseline_record_digest"] = "f" * 64
                elif defect == "new-record":
                    facts["record_digest"] = "f" * 64
                elif defect == "fingerprint":
                    facts["finding_fingerprint"] = "f" * 64
                else:
                    facts["covered_paths"] = []
                proof = register(authority, host, port, "absence", "negative-test", facts, **options)
                result = compare(baseline, current, authority, source, (proof,))
                self.assertEqual(result["classifications"][0]["classification"], "UNVERIFIED")

    def test_revoked_mismatched_or_out_of_scope_baseline_is_not_reused(self):
        from aek.core.security.record import record_as_dict
        for defect in ("revoked", "digest", "repository", "scope"):
            with self.subTest(defect=defect):
                baseline = record([MODEL.finding_payload()], review_id="baseline")
                current = record([MODEL.finding_payload()], paths=["different.py"] if defect == "scope" else None)
                authority, host, port = context(current)
                facts = {"record_digest": baseline.revision_digest, "scope": record_as_dict(baseline)["scope"],
                         "repository_ref": authority.repository_ref}
                if defect == "digest":
                    facts["record_digest"] = "f" * 64
                if defect == "repository":
                    facts["repository_ref"] = "/other"
                source = register(authority, host, port, "baseline", "baseline", facts)
                if defect == "revoked":
                    host.history.clear()
                    source = observed(port, "baseline")
                result = compare(baseline, current, authority, source)
                self.assertEqual(result["classifications"][0]["classification"], "UNVERIFIED")
                self.assertTrue(result["coverage_gaps"])

    def test_changed_statement_or_missing_source_needs_checked_historical_context(self):
        from aek.core.security.knowledge import invariant_binding
        row = invariant()
        authority, host, port = context(record([]))
        source = register(authority, host, port, "requirements", "frozen-requirements", {
            "frozen": True, "invariants": [invariant_binding(row)]}, components=("artifact.design_digest",))
        confirmed = derive([row], authority, (source,))
        self.assertEqual(confirmed["invariants"][0]["status"], "CONFIRMED")
        # The controller obtained this ID from checked source derivation,
        # rather than trusting an author's claimed row status.
        checked_history = (confirmed["invariants"][0]["id"],)
        changed = {**row, "statement": "Author substituted a weaker tenant condition"}
        self.assertEqual(derive([changed], authority, (source,))["invariants"][0]["status"], "CANDIDATE")
        self.assertEqual(derive([changed], authority, (source,), previous_confirmed_ids=checked_history)["invariants"][0]["status"], "INVALIDATED")
        self.assertEqual(derive([row], authority)["invariants"][0]["status"], "CANDIDATE")
        self.assertEqual(derive([row], authority, previous_confirmed_ids=checked_history)["invariants"][0]["status"], "INVALIDATED")

    def test_frozen_confirmation_requires_consumer_artifact_identity(self):
        from aek.core.security.knowledge import derive_security_invariants, invariant_binding
        row = invariant()
        authority, host, port = context(record([]))
        source = register(authority, host, port, "requirements", "frozen-design", {
            "frozen": True, "invariants": [invariant_binding(row)]}, components=("artifact.design_digest",))
        result = derive_security_invariants((row,), (source,), current_identity=authority.current_identity,
            required_components=("subject.scope_digest",), scope_ref=authority.scope_ref, now=authority._clock())
        self.assertEqual(result["invariants"][0]["status"], "CANDIDATE")

    def test_existing_aek_acceptance_ids_are_supported_without_inventing_req_ids(self):
        from aek.core.security.knowledge import invariant_binding
        row = {**invariant(), "entry_refs": ["A1", "F1", "D1", "CONTROL-1"]}
        authority, host, port = context(record([]))
        source = register(authority, host, port, "requirements", "frozen-requirements", {
            "frozen": True, "invariants": [invariant_binding(row)]}, components=("artifact.design_digest",))
        result = derive([row], authority, (source,))
        self.assertEqual(result["invariants"][0]["status"], "CONFIRMED")
        without_control = {**row, "entry_refs": ["A1", "F1", "D1"]}
        source_without_control = register(authority, host, port, "incomplete", "frozen-requirements", {
            "frozen": True, "invariants": [invariant_binding({**without_control, "source_refs": ["incomplete"]})]},
            components=("artifact.design_digest",))
        self.assertEqual(derive([{**without_control, "source_refs": ["incomplete"]}], authority,
                               (source_without_control,))["invariants"][0]["status"], "CANDIDATE")

    def test_baseline_fingerprint_matches_existing_record_lifecycle_proxy(self):
        from aek.core.security.record import record_as_dict
        finding = MODEL.finding_payload()
        baseline, current = record([finding], review_id="baseline"), record([finding])
        authority, host, port = context(current)
        result = compare(baseline, current, authority, baseline_source(authority, host, port, baseline))
        existing_proxy = record_as_dict(current)["lifecycle"]["issues"][0]["occurrences"][0]["location"]
        self.assertEqual(result["classifications"][0]["fingerprint"], existing_proxy)


if __name__ == "__main__":
    unittest.main()
