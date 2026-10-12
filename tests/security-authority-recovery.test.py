#!/usr/bin/env python3
"""Restart validates source receipts and closure derivation, not saved roles."""
from __future__ import annotations
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location("workflow_fixtures", Path(__file__).with_name("security-workflow.test.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def restart(old, host):
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.application.security_workflow import LocalSecurityWorkflow
    authority = BuiltinWorkflowAuthority(repository_ref=old.authority.repository_ref,
        task_id=old.authority.task_id, scope_ref=old.authority.scope_ref,
        current_identity=old.authority.current_identity, author=old.authority.author,
        host_source=host, provenance_root=Path(old.authority.repository_ref) / "provenance")
    return LocalSecurityWorkflow(authority=authority, store=old.store,
        source_port=BuiltinSourceEvidencePort(authority, root=Path(old.authority.repository_ref) / "sources"))


def close_fixture(directory):
    workflow, host, fingerprint = fixture.workflow_fixture(directory)
    fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=workflow.store.load().revision_digest)
    fixture.add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
        "passed": True, "original_condition_absent": True})
    fixture.add_source(workflow, host, "normal", "regression", {"passed": True})
    handle = fixture.reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
    verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
        event_id="verify", expected_revision_digest=fixed.revision_digest)
    closed = workflow.on_gate_composed(**fixture.gate_inputs(workflow), event_id="close",
        expected_revision_digest=verified.revision_digest)["record"]
    return workflow, host, fingerprint, closed


class SecurityAuthorityRecoveryTests(unittest.TestCase):
    def test_restart_revalidates_original_ready_and_sources_without_reviving_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint, closed = close_fixture(directory)
            reopened = restart(workflow, host)
            state = reopened.recover()
            self.assertTrue(state["closed_valid"])
            self.assertTrue(state["verified_valid"])
            self.assertEqual(state["record"], closed)
            self.assertEqual(reopened.authority._handles, {})
            self.assertEqual(reopened.store.load(), closed)

    def test_missing_or_expired_host_provenance_invalidates_closed_without_overwriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint, closed = close_fixture(directory)
            unavailable = restart(workflow, fixture.LiveHost())
            self.assertFalse(unavailable.recover()["closed_valid"])
            self.assertFalse(unavailable.recover()["verified_valid"])
            self.assertEqual(unavailable.source_port.lookup("normal").metadata["status"], "DECLARED")
            self.assertEqual(unavailable.store.load(), closed)
            reopened = restart(workflow, host)
            now = reopened.authority._clock()
            reopened.authority._clock = lambda: now + timedelta(hours=2)
            self.assertFalse(reopened.recover()["closed_valid"])

    def test_resolution_recovery_uses_direct_user_receipt_and_invalidates_expiry_or_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint = fixture.workflow_fixture(directory)
            binding = workflow.resolution_binding(fingerprint, kind="ACCEPTED_RISK", reason="Temporary exception",
                evidence_refs=(), expires_at=workflow.authority.expires_at,
                required_components=("subject.scope_digest",))
            token = host.issue(workflow.authority, "user-confirmation", "accept-risk", binding)
            handle = workflow.on_user_confirmation(token, binding=binding)
            record = workflow.resolve(fingerprint, kind="ACCEPTED_RISK", reason="Temporary exception", evidence_refs=(),
                expires_at=workflow.authority.expires_at, required_components=("subject.scope_digest",),
                handle=handle, event_id="risk", expected_revision_digest=workflow.store.load().revision_digest)
            reopened = restart(workflow, host)
            self.assertEqual(len(reopened.recover()["valid_resolutions"]), 1)
            self.assertEqual(json.loads(record.payload_json)["lifecycle"]["issues"][0]["state"], "OPEN")
            reopened.authority.scope_ref = "different-scope"
            self.assertEqual(reopened.recover()["valid_resolutions"], [])
            unavailable = restart(workflow, fixture.LiveHost())
            self.assertEqual(unavailable.recover()["valid_resolutions"], [])

    def test_current_code_change_and_corrupt_judge_source_prevent_reuse(self):
        from aek.core.context.identity import identity_as_dict, build_identity
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, fingerprint, closed = close_fixture(directory)
            reopened = restart(workflow, host)
            components = identity_as_dict(reopened.authority.current_identity)["components"]
            components["subject"]["scope_digest"] = "f" * 64
            reopened.authority.current_identity = build_identity(components)
            self.assertFalse(reopened.recover()["closed_valid"])
            reopened = restart(workflow, host)
            judge_ref = json.loads(closed.payload_json)["gate_binding"]["judge_event_ref"]
            # Remove only the independently authenticated host callback.
            event_id = workflow.authority.read_provenance(judge_ref, live=True)["event"]["event_id"]
            del host.history[event_id]
            self.assertFalse(reopened.recover()["closed_valid"])
            self.assertTrue(reopened.recover()["verified_valid"])
            self.assertEqual(reopened.store.load(), closed)


if __name__ == "__main__":
    unittest.main()
