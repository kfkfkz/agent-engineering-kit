#!/usr/bin/env python3
"""Real process/WorkUnit/record execution with host events simulated at boundary."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("workflow_execution_fixtures", Path(__file__).with_name("security-workflow.test.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def execution_context(directory, *, boundaries=()):
    from aek.adapters.security_scanner import ScannerAdapter
    from aek.adapters.work_unit_store import WorkUnitStore
    from aek.application.work_unit import WorkUnitService
    from aek.core.work_unit import StepSpec
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.core.security.authority import runtime_binding
    root = Path(directory)
    # Files created only within this test's managed TemporaryDirectory.
    (root / "src").mkdir()
    (root / "src/app.py").write_text("print('synthetic source')\n", encoding="utf-8")
    (root / "rules.yml").write_text("rules: []\n", encoding="utf-8")
    (root / "stub.py").write_text(
        "import json, pathlib, sys\n"
        "if '--version' in sys.argv: print('1.180.0')\n"
        "else:\n"
        f" p = pathlib.Path({str(root / 'invocations')!r})\n"
        " p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        " print(json.dumps({'version':'1.180.0','results':[],'errors':[],"
        "'paths':{'scanned':['src/app.py'],'skipped':[]},'skipped_rules':[]}))\n", encoding="utf-8")
    workflow, host, _fingerprint = fixture.workflow_fixture(directory)
    scanner = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()), str(root / "stub.py")))
    identity = workflow.authority.current_identity
    probe = scanner.plan(identity_digest=identity.envelope_digest, canonical_scope_ref=workflow.authority.scope_ref,
                         action="version_probe", required_boundaries=())
    probe_authority = BuiltinWorkflowAuthority(repository_ref=str(root), task_id="version-test", scope_ref=probe.scope_ref,
        current_identity=identity, author="author", host_source=host)
    binding = runtime_binding(**{k: getattr(probe, k) for k in (
        "plan_digest", "repository_ref", "scope_ref", "identity_digest", "tool_id", "executable_digest",
        "action", "network_mode", "write_scope", "timeout_seconds", "max_output_bytes", "max_items")})
    token = host.issue(probe_authority, "user-confirmation", "execute", binding)
    probe_grant = probe_authority.on_user_confirmation(token, binding=binding)
    capability = scanner.version_probe(probe, grant=probe_grant, authority=probe_authority)
    assert capability.version_compatible is True
    plan = scanner.plan(("src/app.py",), identity_digest=identity.envelope_digest,
        configuration_path="rules.yml", required_boundaries=boundaries, canonical_scope_ref=workflow.authority.scope_ref)
    # The control-side authority is scoped to these exact authorized input bytes.
    authority = BuiltinWorkflowAuthority(repository_ref=str(root), task_id="task", scope_ref=plan.scope_ref,
        current_identity=identity, author="author", host_source=host, provenance_root=root / "provenance")
    workflow.authority = authority
    workflow.source_port = BuiltinSourceEvidencePort(authority, root=root / "sources")
    binding = runtime_binding(**{k: getattr(plan, k) for k in (
        "plan_digest", "repository_ref", "scope_ref", "identity_digest", "tool_id", "executable_digest",
        "action", "network_mode", "write_scope", "timeout_seconds", "max_output_bytes", "max_items")})
    token = host.issue(authority, "user-confirmation", "execute", binding)
    grant = authority.on_user_confirmation(token, binding=binding)
    (root / "work-units").mkdir()
    work = WorkUnitService(WorkUnitStore(root / "work-units"))
    from aek.core.security.authority import digest
    required = tuple(f"{n}.{k}" for n, fields in identity.components for k, _v in fields)
    opened = work.open_or_create(repo_identity=digest(str(root)), task_identity=digest(authority.task_id),
        writer_id=authority.author, identity=identity, steps=(StepSpec("scan", False, required),))
    def completion(metadata, facts, source_binding):
        return host.issue(authority, "tool-completed", "source", source_binding, actor=metadata["producer_ref"])
    arguments = {"plan": plan, "grant": grant, "scanner_port": scanner, "work_unit_service": work,
        "work_unit_id": opened.snapshot.work_unit_id, "step_id": "scan", "event_id": "scan-event",
        "expected_revision_digest": workflow.store.load().revision_digest, "on_completion": completion}
    return workflow, host, work, arguments


class SecurityExecutionTests(unittest.TestCase):
    def test_completed_reuse_rechecks_input_configuration_and_executable_bytes_without_replaying(self):
        for relative in ("src/app.py", "rules.yml", "stub.py"):
            with self.subTest(material=relative), tempfile.TemporaryDirectory() as directory:
                workflow, _host, work, arguments = execution_context(directory)
                completed = workflow.execute(**arguments)
                record = workflow.store.load()
                snapshot = work.store.load(arguments["work_unit_id"])
                material = Path(directory) / relative
                material.write_bytes(material.read_bytes() + b"\n# changed input\n")
                result = workflow.execute(**arguments)
                self.assertEqual(result["status"], "INCOMPLETE")
                self.assertEqual(result["action"], "human")
                self.assertIn("SCANNER_PLAN_STALE", result["reason_codes"])
                self.assertEqual((Path(directory) / "invocations").read_text(), "1")
                self.assertEqual(workflow.store.load(), record)
                self.assertEqual(work.store.load(arguments["work_unit_id"]), snapshot)
                self.assertEqual(completed["action"], "executed")

    def test_plan_scope_mismatch_is_rejected_before_workunit_or_record_changes(self):
        from aek.core.work_unit import WorkState
        with tempfile.TemporaryDirectory() as directory:
            workflow, _host, work, arguments = execution_context(directory)
            original = workflow.store.load()
            scanner = arguments["scanner_port"]
            other = scanner.plan(("src/app.py",), identity_digest=workflow.authority.current_identity.envelope_digest,
                canonical_scope_ref="b" * 64, configuration_path="rules.yml", required_boundaries=())
            arguments["plan"] = other
            with self.assertRaises(ValueError):
                workflow.execute(**arguments)
            self.assertEqual(work.store.load(arguments["work_unit_id"]).by_step()["scan"].state, WorkState.NOT_STARTED)
            self.assertEqual(workflow.store.load(), original)
            self.assertFalse((Path(directory) / "invocations").exists())

    def test_actual_committed_scanner_run_can_satisfy_the_original_composer_requirement(self):
        from aek.core.security.record import record_as_dict, finding_fingerprint
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, work, arguments = execution_context(directory)
            executed = workflow.execute(**arguments)
            record = executed["record"]
            body = record_as_dict(record)
            source_ref = body["runs"][0]["evidence_refs"][0]
            fingerprint = finding_fingerprint(body["findings"][0], body["identity"]["components"]["subject"].get("repo_digest"))
            fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=record.revision_digest)
            fixture.add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                "passed": True, "original_condition_absent": True})
            fixture.add_source(workflow, host, "normal", "regression", {"passed": True})
            handle = fixture.reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
            verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                event_id="verified", expected_revision_digest=fixed.revision_digest)
            inputs = fixture.gate_inputs(workflow)
            route = inputs["route_result"]
            route["requirements"]["required_checks"].append("security-scan:semgrep")
            route["governance"]["risk_requirements"]["checks"].append("security-scan:semgrep")
            route["governance"]["provenance"].append({"kind": "checks", "requirement": "security-scan:semgrep",
                                                    "source": "risk_overlay:security"})
            inputs["evidence_by_requirement"]["checks:security-scan:semgrep"] = {"status": "PASS", "evidence_refs": [source_ref]}
            closed = workflow.on_gate_composed(**inputs, event_id="closed", expected_revision_digest=verified.revision_digest)
            self.assertEqual(closed["result"]["verdict"], "READY")
            self.assertTrue(workflow.recover()["closed_valid"])
            self.assertEqual((Path(directory) / "invocations").read_text(), "1")
            # Recover with new provider instances and no ephemeral version
            # probe cache. Missing current material capability is not success.
            from aek.adapters.security_authority import BuiltinWorkflowAuthority
            from aek.adapters.security_evidence import BuiltinSourceEvidencePort
            from aek.adapters.security_scanner import ScannerAdapter
            from aek.application.security_review import SecurityReviewService
            from aek.application.security_workflow import LocalSecurityWorkflow
            root = Path(directory)
            prior = workflow.authority
            authority = BuiltinWorkflowAuthority(repository_ref=prior.repository_ref, task_id=prior.task_id,
                scope_ref=prior.scope_ref, current_identity=prior.current_identity, author=prior.author,
                host_source=host, provenance_root=root / "provenance")
            sources = BuiltinSourceEvidencePort(authority, root=root / "sources")
            missing = LocalSecurityWorkflow(authority=authority, source_port=sources, store=workflow.store)
            self.assertFalse(missing.recover()["closed_valid"])
            fresh_adapter = ScannerAdapter(root, "semgrep", arguments["scanner_port"].executable_prefix)
            self.assertIsNone(fresh_adapter._version_evidence)
            service = SecurityReviewService(record_port=workflow.store, source_port=sources,
                approval_port=authority, scanner_ports={"semgrep": fresh_adapter})
            recovered = LocalSecurityWorkflow(authority=authority, source_port=sources, store=workflow.store,
                review_service=service)
            self.assertTrue(recovered.recover()["closed_valid"])
            self.assertEqual((root / "invocations").read_text(), "1")

    def test_closed_scanner_proof_is_not_current_after_any_material_changes(self):
        from aek.core.security.record import record_as_dict, finding_fingerprint
        for relative, phase in ((relative, phase) for relative in ("src/app.py", "rules.yml", "stub.py")
                                for phase in ("before-gate", "after-gate")):
            with self.subTest(material=relative, phase=phase), tempfile.TemporaryDirectory() as directory:
                workflow, host, _work, arguments = execution_context(directory)
                executed = workflow.execute(**arguments)
                record = executed["record"]
                body = record_as_dict(record)
                source_ref = body["runs"][0]["evidence_refs"][0]
                fingerprint = finding_fingerprint(body["findings"][0])
                fixed = workflow.claim_fixed(fingerprint, event_id="fixed", expected_revision_digest=record.revision_digest)
                fixture.add_source(workflow, host, "negative", "negative-test", {"finding_fingerprint": fingerprint,
                    "passed": True, "original_condition_absent": True})
                fixture.add_source(workflow, host, "normal", "regression", {"passed": True})
                handle = fixture.reviewer_handle(workflow, host, fingerprint, ("negative", "normal"))
                verified = workflow.reverify(fingerprint, handle=handle, evidence_refs=("negative", "normal"),
                    event_id="verified", expected_revision_digest=fixed.revision_digest)
                inputs = fixture.gate_inputs(workflow)
                route = inputs["route_result"]
                route["requirements"]["required_checks"].append("security-scan:semgrep")
                route["governance"]["risk_requirements"]["checks"].append("security-scan:semgrep")
                route["governance"]["provenance"].append({"kind": "checks", "requirement": "security-scan:semgrep",
                                                        "source": "risk_overlay:security"})
                inputs["evidence_by_requirement"]["checks:security-scan:semgrep"] = {"status": "PASS", "evidence_refs": [source_ref]}
                material = Path(directory) / relative
                if phase == "before-gate":
                    material.write_bytes(material.read_bytes() + b"\n# changed\n")
                    with self.assertRaisesRegex(ValueError, "EVIDENCE_INCOMPLETE"):
                        workflow.on_gate_composed(**inputs, event_id="closed", expected_revision_digest=verified.revision_digest)
                    self.assertEqual(workflow.store.load(), verified)
                    self.assertEqual((Path(directory) / "invocations").read_text(), "1")
                    continue
                closed = workflow.on_gate_composed(**inputs, event_id="closed", expected_revision_digest=verified.revision_digest)["record"]
                self.assertTrue(workflow.recover()["closed_valid"])
                material.write_bytes(material.read_bytes() + b"\n# changed\n")
                self.assertFalse(workflow.recover()["closed_valid"])
                with self.assertRaisesRegex(ValueError, "EVIDENCE_STALE"):
                    workflow.on_gate_composed(**inputs, event_id="closed", expected_revision_digest=verified.revision_digest)
                self.assertEqual(workflow.store.load(), closed)
                self.assertEqual((Path(directory) / "invocations").read_text(), "1")

    def test_each_started_interruption_requires_reconciliation_without_process_replay(self):
        from aek.core.work_unit import WorkState
        for boundary in ("after_started", "before_record", "after_record"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                workflow, host, work, arguments = execution_context(directory)
                original = workflow.store.load()
                def crash(stage):
                    if stage == boundary:
                        raise RuntimeError("INJECTED_CRASH")
                with self.assertRaisesRegex(RuntimeError, "INJECTED_CRASH"):
                    workflow.execute(**arguments, checkpoint=crash)
                self.assertEqual(work.store.load(arguments["work_unit_id"]).by_step()["scan"].state, WorkState.IN_PROGRESS)
                outcome = workflow.execute(**arguments)
                self.assertEqual(outcome["action"], "human")
                self.assertEqual(outcome["status"], "INCOMPLETE")
                counter = Path(directory) / "invocations"
                self.assertEqual(counter.read_text() if counter.exists() else "0", "0" if boundary == "after_started" else "1")
                record = workflow.store.load()
                self.assertEqual(record == original, boundary != "after_record")
                self.assertEqual(len(json.loads(record.payload_json)["runs"]), 1 if boundary == "after_record" else 0)

    def test_missing_or_broken_completion_source_never_commits_or_claims_passed(self):
        for callback_kind in ("missing", "broken", "forged-json"):
            with self.subTest(callback_kind=callback_kind), tempfile.TemporaryDirectory() as directory:
                workflow, host, work, arguments = execution_context(directory)
                original = workflow.store.load()
                if callback_kind == "missing":
                    arguments["on_completion"] = None
                elif callback_kind == "forged-json":
                    arguments["on_completion"] = lambda *_args: {"producer": "semgrep", "status": "PASSED"}
                else:
                    def broken(*_args):
                        raise RuntimeError("synthetic callback failure")
                    arguments["on_completion"] = broken
                result = workflow.execute(**arguments)
                self.assertNotEqual(result["status"], "COMPLETE")
                self.assertEqual(result["action"], "not_started" if callback_kind == "missing" else "human")
                self.assertEqual(workflow.store.load(), original)
                if callback_kind != "missing":
                    self.assertEqual(workflow.execute(**arguments)["action"], "human")
                counter = Path(directory) / "invocations"
                self.assertEqual(counter.read_text() if counter.exists() else "0", "0" if callback_kind == "missing" else "1")

    def test_default_boundaries_unavailable_cannot_start(self):
        from aek.core.work_unit import WorkState
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, work, arguments = execution_context(directory, boundaries=("network", "filesystem"))
            original = workflow.store.load()
            result = workflow.execute(**arguments)
            self.assertEqual(result["action"], "not_started")
            self.assertEqual(result["reason_codes"], ["BOUNDARY_UNAVAILABLE"])
            self.assertEqual(work.store.load(arguments["work_unit_id"]).by_step()["scan"].state, WorkState.NOT_STARTED)
            self.assertEqual(workflow.store.load(), original)
            self.assertFalse((Path(directory) / "invocations").exists())

    def test_current_identity_change_or_completion_provenance_loss_prevents_committed_reuse(self):
        from aek.core.context.identity import identity_as_dict, build_identity
        for changed_identity in (False, True):
            with self.subTest(changed_identity=changed_identity), tempfile.TemporaryDirectory() as directory:
                workflow, host, work, arguments = execution_context(directory)
                first = workflow.execute(**arguments)
                self.assertEqual(first["status"], "COMPLETE")
                if changed_identity:
                    components = identity_as_dict(workflow.authority.current_identity)["components"]
                    components["subject"]["scope_digest"] = "f" * 64
                    workflow.authority.current_identity = build_identity(components)
                else:
                    host.history.clear()
                self.assertEqual(workflow.execute(**arguments)["action"], "human")
                self.assertEqual((Path(directory) / "invocations").read_text(), "1")

    def test_native_completion_is_saved_before_workunit_commit_and_valid_committed_run_reuses(self):
        from aek.core.work_unit import WorkState
        with tempfile.TemporaryDirectory() as directory:
            workflow, host, work, arguments = execution_context(directory)
            outcome = workflow.execute(**arguments)
            self.assertEqual(outcome["status"], "COMPLETE")
            self.assertEqual(outcome["action"], "executed")
            current = work.store.load(arguments["work_unit_id"])
            self.assertEqual(current.by_step()["scan"].state, WorkState.COMPLETED)
            value = json.loads(workflow.store.load().payload_json)
            self.assertEqual(value["runs"][0]["status"], "PASSED")
            self.assertEqual(value["runs"][0]["plan_digest"], arguments["plan"].plan_digest)
            reused = workflow.execute(**arguments)
            self.assertEqual(reused["action"], "reuse")
            self.assertEqual((Path(directory) / "invocations").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
