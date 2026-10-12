"""Control-side facade joining real host provenance to the existing lifecycle.

No imported DTO creates an authority. A host without live direct-event channels
can evaluate/import/plan, but cannot verify, approve, execute or close records.
"""
from __future__ import annotations
from dataclasses import asdict
import json

from aek.application.review import review_request_bytes
from aek.core.context.identity import IdentityState, compare_identity, decode_identity
from aek.core.review.lifecycle import (
    IssueAuthority, IssueState, lifecycle_bytes, load_lifecycle,
    project_pass_gate, transition_issue,
)
from aek.core.security.authority import (
    CallerHandle, SecurityAuthorityError, canonical, digest, timestamp, runtime_binding,
)
from aek.core.security.record import (
    closure_proof_digest, finding_fingerprint, record_as_dict, seal_security_record,
)


class LocalSecurityWorkflow:
    def __init__(self, *, authority, source_port, store, review_service=None) -> None:
        self.authority, self.source_port, self.store = authority, source_port, store
        self.review_service = review_service
        self._material_ports = dict(getattr(review_service, "scanner_ports", {}))
        self._review_bindings: dict[str, dict] = {}

    def capabilities(self) -> dict:
        return self.authority.capabilities()

    def _service(self):
        if self.review_service is not None:
            return self.review_service
        from aek.application.security_review import SecurityReviewService
        return SecurityReviewService(record_port=self.store, source_port=self.source_port,
                                     approval_port=self.authority, clock=self.authority._clock)

    def run_workflow(self, data_inputs: tuple[bytes, ...], *, required_components: tuple[str, ...],
                     review_service=None) -> tuple[dict, ...]:
        """Read-only data dispatch through the same facade and application service.

        Live callbacks and opaque handles are passed to the explicit control
        methods; no role or authorization can be decoded from these inputs.
        """
        from aek.core.security.model import decode_security_request, SecurityInputError
        if (type(data_inputs) is not tuple or len(data_inputs) > 10_000
                or any(type(raw) is not bytes for raw in data_inputs)
                or sum(len(raw) for raw in data_inputs) > 2_000_000):
            raise SecurityInputError()
        requests = tuple(decode_security_request(raw) for raw in data_inputs)
        if review_service is None:
            review_service = self._service()
        responses = []
        for raw, request in zip(data_inputs, requests):
            if request.operation == "evaluate":
                responses.append(review_service.evaluate(raw, current_identity=self.authority.current_identity,
                                                        required_components=required_components))
            elif request.operation == "report":
                responses.append(review_service.report(raw, current_identity=self.authority.current_identity,
                    required_components=required_components, repository_ref=self.authority.repository_ref))
            elif request.operation == "plan":
                responses.append(review_service.plan(raw, current_identity=self.authority.current_identity,
                                                     required_components=required_components))
            else:
                method = getattr(review_service, request.operation, None)
                if method is None:
                    responses.append({"schema_version": 1, "operation": request.operation, "status": "INCOMPLETE",
                        "reason_codes": ["OPERATION_UNAVAILABLE"], "capabilities": self.capabilities()})
                else:
                    responses.append(method(raw))
        return tuple(responses)

    def execute(self, plan, *, grant, scanner_port, work_unit_service, work_unit_id: str,
                step_id: str, event_id: str, expected_revision_digest: str, on_completion,
                checkpoint=None) -> dict:
        """One controlled native attempt, with existing WorkUnit/CAS recovery.

        All ports/callbacks are supplied by the composition root. A completion
        callback authenticates a native result through the actual host channel;
        neither its event token nor a grant can be decoded from a data request.
        Record and WorkUnit writes are separate commits. Started attempts never
        silently select another execution path or replay an unknown action.
        """
        from aek.core.work_unit import WorkState
        from aek.core.security.scanners import ScannerPlan, NormalizedScan, ScannerCapability, plan_digest
        work = work_unit_service.store.load(work_unit_id)
        if (work is None or step_id not in work.by_step()
                or work.repo_identity != digest(self.authority.repository_ref)
                or work.task_identity != digest(self.authority.task_id)
                or work.writer_id != self.authority.author):
            raise SecurityAuthorityError()
        step = work.by_step()[step_id]
        if type(plan) is not ScannerPlan or plan.plan_digest != plan_digest(plan):
            raise SecurityAuthorityError("INVALID_SCANNER_PLAN")
        def response(status, action, reasons, record=None, result=None):
            return {"status": status, "action": action, "reason_codes": list(reasons),
                    "record": record, "result": result, "work_unit_id": work_unit_id, "step_id": step_id}
        try:
            record, value = self._current()
        except ValueError:
            return response("INCOMPLETE", "human", ("EVIDENCE_STALE",))

        def proof(ref):
            observation = self.source_port.lookup(ref)
            if observation is None:
                return None
            metadata, facts = observation.metadata, observation.facts
            run = next((r for r in value["runs"] if r["id"] == metadata["run_id"]), None)
            if (metadata["status"] != "VERIFIED" or metadata["scope_ref"] != plan.scope_ref
                    or metadata["producer_ref"] != plan.tool_id or facts.get("plan_digest") != plan.plan_digest
                    or facts.get("identity_digest") != self.authority.current_identity.envelope_digest
                    or facts.get("work_unit_id") != work_unit_id or facts.get("step_id") != step_id
                    or run is None or run["plan_digest"] != plan.plan_digest
                    or run["identity_digest"] != self.authority.current_identity.envelope_digest
                    or run["status"] != facts.get("status") or run["scope_ref"] != plan.scope_ref
                    or run["configuration_digest"] != plan.configuration_digest
                    or metadata["kind"] != ("scanner-version" if plan.action == "version_probe" else "scanner")
                    or facts.get("action") != plan.action or facts.get("tool_id") != plan.tool_id
                    or not any(s["ref"] == ref for s in value["sources"])
                    or ref not in run["evidence_refs"]
                    or not any(e["operation"] == "execute" and e["trusted_actor_ref"] == observation.provenance_ref
                               and ref in e["evidence_refs"] for e in value["events"])):
                return None
            return facts
        def receipt_state(ref):
            if proof(ref) is None:
                return None
            return "COMMITTED" if step.state == WorkState.COMPLETED else "STARTED"

        if step.state != WorkState.NOT_STARTED or step.receipt_ref:
            try:
                scanner_port.validate_plan_material(plan)
            except (ValueError, OSError, AttributeError):
                return response("INCOMPLETE", "human", ("SCANNER_PLAN_STALE",), record)
            decision = work_unit_service.resume(work_unit_id, self.authority.current_identity, receipt_state).by_step()[step_id]
            completed = proof(step.receipt_ref) if step.receipt_ref else None
            if decision.action == "reuse" and completed is not None:
                return response("COMPLETE" if completed["status"] == "PASSED" else "INCOMPLETE", "reuse", (),
                                record, completed["result"])
            # Core may permit explicit recomputation of idempotent work, but this
            # facade never starts a second attempt implicitly after STARTED.
            return response("INCOMPLETE", decision.action if decision.action != "reuse" else "human",
                            (decision.reason_code if decision.action != "reuse" else "SECURITY_EXECUTION_PROOF_UNKNOWN",), record)
        if record.revision_digest != expected_revision_digest:
            raise ValueError("SECURITY_REVISION_CONFLICT")
        if (not callable(on_completion) or not callable(getattr(self.source_port, "register", None))):
            return response("REJECTED", "not_started", ("AUTHORIZATION_SOURCE_UNAVAILABLE",), record)
        if (plan.repository_ref != self.authority.repository_ref or plan.scope_ref != self.authority.scope_ref
                or plan.identity_digest != self.authority.current_identity.envelope_digest
                or not {p for p, _d in plan.input_files}.issubset(value["scope"]["paths"])):
            raise SecurityAuthorityError()
        self.authority._active("source")
        self.authority.validate_runtime_grant(grant, **{k: getattr(plan, k) for k in (
            "plan_digest", "repository_ref", "scope_ref", "identity_digest", "tool_id", "executable_digest",
            "action", "network_mode", "write_scope", "timeout_seconds", "max_output_bytes", "max_items")})
        self._material_ports[plan.tool_id] = scanner_port
        supported = getattr(getattr(scanner_port, "process_port", None), "supported_boundaries", frozenset())
        if not set(plan.required_boundaries).issubset(supported):
            return response("REJECTED", "not_started", ("BOUNDARY_UNAVAILABLE",), record)
        input_digest = digest({"operation": "execute", "plan_digest": plan.plan_digest,
                               "work_unit_id": work_unit_id, "step_id": step_id})
        self._replay(value, event_id, input_digest)
        run_id = "run-" + digest({"work_unit_id": work_unit_id, "step_id": step_id, "plan_digest": plan.plan_digest})
        source_ref = "source:" + run_id
        started_at = self.authority._clock().isoformat()
        work = work_unit_service.transition(work_unit_id, expected_snapshot_digest=work.snapshot_digest,
            step_id=step_id, state=WorkState.IN_PROGRESS, writer_id=work.writer_id, writer_epoch=work.writer_epoch,
            event_id="security-started-" + input_digest, receipt_ref=source_ref, reason_code="STARTED")
        if checkpoint is not None:
            checkpoint("after_started")
        try:
            native = scanner_port.execute(plan, grant=grant, authority=self.authority)
            if type(native) is NormalizedScan and plan.action == "scan":
                if native.tool_id != plan.tool_id or native.version != plan.tool_version:
                    raise SecurityAuthorityError()
                status, version = native.status, native.version
                source_kind = "scanner"
                gaps = list(native.coverage_gaps)
                reasons = list(native.reason_codes)
            elif type(native) is ScannerCapability and plan.action == "version_probe":
                status = "PASSED" if native.version_compatible is True and native.authorized is True else "BLOCKED"
                version, source_kind, gaps, reasons = native.version, "scanner-version", [], list(native.reason_codes)
            else:
                raise SecurityAuthorityError()
            if status not in {"PASSED", "FAILED", "BLOCKED", "ERROR", "INCONCLUSIVE"}:
                raise SecurityAuthorityError()
            result = asdict(native)
        except Exception:
            return response("INCOMPLETE", "human", ("NATIVE_EXECUTION_RESULT_UNKNOWN",), record)
        finished_at = self.authority._clock().isoformat()
        metadata = {"id": run_id, "ref": source_ref, "run_id": run_id, "producer_ref": plan.tool_id,
            "identity": value["identity"], "required_components": list(step.required_components),
            "kind": source_kind, "status": "DECLARED", "checked_at": finished_at, "scope_ref": plan.scope_ref,
            "tool_version": version, "rules_digest": plan.rules_digest, "database_digest": plan.database_digest,
            "coverage": "COMPLETE_FOR_DECLARED_SCOPE" if not gaps and status in {"PASSED", "FAILED"} else "PARTIAL",
            "coverage_gaps": gaps, "summary": "Controlled native scanner completion"}
        facts = {"tool_id": plan.tool_id, "action": plan.action, "plan_digest": plan.plan_digest,
            "identity_digest": plan.identity_digest, "work_unit_id": work_unit_id, "step_id": step_id,
            "status": status, "result": result, "plan_snapshot": asdict(plan)}
        binding = {"operation": "source", "metadata_digest": digest(metadata), "facts_digest": digest(facts)}
        try:
            token = on_completion(metadata, facts, binding)
            handle = self.authority.on_source_completed(token, binding=binding)
            observation = self.source_port.register(metadata, facts, handle)
            actor_ref, evidence_refs = observation.provenance_ref, (source_ref,)
        except Exception:
            # The process may have finished, but no authoritative completion
            # exists. Preserve STARTED and require reconciliation, not replay.
            return response("INCOMPLETE", "human", ("NATIVE_COMPLETION_SOURCE_UNAVAILABLE",), record)
        if checkpoint is not None:
            checkpoint("before_record")
        value["runs"].append({"id": run_id, "plan_digest": plan.plan_digest, "identity_digest": plan.identity_digest,
            "producer_ref": plan.tool_id, "tool_version": version, "configuration_digest": plan.configuration_digest,
            "scope_ref": plan.scope_ref, "status": status, "evidence_refs": list(evidence_refs), "coverage_gaps": gaps,
            "started_at": started_at, "finished_at": finished_at, "reason_codes": reasons})
        value["sources"].append(observation.metadata)
        try:
            updated = self._write(record, value, event_id=event_id, operation="execute", input_digest=input_digest,
                actor_ref=actor_ref, evidence_refs=evidence_refs, expected_revision_digest=expected_revision_digest)
        except Exception:
            return response("INCOMPLETE", "human", ("SECURITY_EXECUTION_RECORD_NOT_COMMITTED",), record)
        if checkpoint is not None:
            checkpoint("after_record")
        try:
            work_unit_service.transition(work_unit_id, expected_snapshot_digest=work.snapshot_digest, step_id=step_id,
                state=WorkState.COMPLETED, writer_id=work.writer_id, writer_epoch=work.writer_epoch,
                event_id="security-committed-" + input_digest, receipt_ref=source_ref, reason_code="COMMITTED")
        except Exception:
            return response("INCOMPLETE", "human", ("SECURITY_EXECUTION_WORKUNIT_NOT_COMMITTED",), updated, result)
        return response("COMPLETE" if status == "PASSED" else "INCOMPLETE", "executed", reasons, updated, result)

    def _current(self):
        record = self.store.load()
        if record is None:
            raise ValueError("SECURITY_RECORD_NOT_FOUND")
        value = record_as_dict(record)
        if value["scope"]["subject_digest"] != self.authority.scope_ref:
            raise SecurityAuthorityError("SCOPE_BINDING_MISMATCH")
        if decode_identity(value["identity"]) != self.authority.current_identity:
            raise SecurityAuthorityError("EVIDENCE_STALE")
        return record, value

    def _issue(self, value: dict, fingerprint: str):
        repo = value["identity"]["components"]["subject"].get("repo_digest")
        finding = next((f for f in value["findings"]
                        if finding_fingerprint(f, repo) == fingerprint), None)
        snapshot = load_lifecycle(canonical(value["lifecycle"]))
        issue = next((i for i in snapshot.issues if any(
            o.location == fingerprint for o in i.occurrences)), None)
        if finding is None or issue is None:
            raise SecurityAuthorityError()
        return finding, snapshot, issue

    def _replay(self, value, event_id, input_digest):
        prior = next((e for e in value["events"] if e["event_id"] == event_id), None)
        if prior is not None:
            if prior["input_digest"] != input_digest:
                raise ValueError("SECURITY_EVENT_CONFLICT")
            return self.store.load()
        return None

    def _write(self, record, value, *, event_id, operation, input_digest, actor_ref,
               evidence_refs, expected_revision_digest):
        if not isinstance(event_id, str) or not event_id:
            raise SecurityAuthorityError()
        value["revision"] += 1
        value["events"].append({"event_id": event_id, "operation": operation,
            "input_digest": input_digest, "current_identity_digest": self.authority.current_identity.envelope_digest,
            "trusted_actor_ref": actor_ref, "evidence_refs": list(evidence_refs),
            "occurred_at": self.authority._clock().isoformat()})
        updated = seal_security_record(value)
        self.store.write(updated, expected_revision_digest=expected_revision_digest)
        return updated

    def claim_fixed(self, fingerprint: str, *, event_id: str, expected_revision_digest: str):
        self.authority._active("claim-fixed")
        record, value = self._current()
        input_digest = digest({"operation": "claim-fixed", "fingerprint": fingerprint})
        replay = self._replay(value, event_id, input_digest)
        if replay is not None:
            return replay
        _finding, snapshot, issue = self._issue(value, fingerprint)
        after = transition_issue(snapshot, issue.fingerprint, IssueState.FIXED, IssueAuthority.AUTHOR,
                                 input_digest=input_digest)
        value["lifecycle"] = json.loads(lifecycle_bytes(after))
        return self._write(record, value, event_id=event_id, operation="claim-fixed", input_digest=input_digest,
            actor_ref="author:" + self.authority.author, evidence_refs=(),
            expected_revision_digest=expected_revision_digest)

    def register_review(self, worker_id: str, *, request, finding_fingerprints: tuple[str, ...],
                        evidence_refs: tuple[str, ...], operation: str = "reverify") -> dict:
        record, value = self._current()
        review_request_bytes(request)
        supporting = dict(request.supporting_context)
        if (operation not in {"reverify", "false-positive"}
                or supporting.get("security-record") != record.payload_json
                or supporting.get("security-identity") != canonical(value["identity"]).decode()):
            raise SecurityAuthorityError()
        for fingerprint in finding_fingerprints:
            self._issue(value, fingerprint)
        if any(self.source_port.lookup(ref) is None for ref in evidence_refs):
            raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
        binding = {"operation": operation, "request_digest": request.request_digest,
                   "record_digest": record.revision_digest,
                   "finding_fingerprints": list(finding_fingerprints), "evidence_refs": list(evidence_refs)}
        self.authority.register_reviewer(worker_id, request_digest=request.request_digest,
            record_digest=record.revision_digest, finding_fingerprints=finding_fingerprints)
        self._review_bindings[worker_id] = binding
        return json.loads(canonical(binding))

    def on_review_completed(self, event, *, worker_id, request, result_text, receipt_text, binding):
        record, _value = self._current()
        if (self._review_bindings.get(worker_id) != binding
                or binding.get("record_digest") != record.revision_digest):
            raise SecurityAuthorityError()
        return self.authority.on_review_completed(event, worker_id=worker_id, request=request,
            result_text=result_text, receipt_text=receipt_text, binding=binding)

    def on_user_confirmation(self, event, *, binding):
        return self.authority.on_user_confirmation(event, binding=binding)

    def request_runtime_grant(self, plan, event):
        binding = runtime_binding(**{key: getattr(plan, key) for key in (
            "plan_digest", "repository_ref", "scope_ref", "identity_digest", "tool_id", "executable_digest",
            "action", "network_mode", "write_scope", "timeout_seconds", "max_output_bytes", "max_items")})
        return self.on_user_confirmation(event, binding=binding)

    def _review_caller(self, handle, operation, record, fingerprint, evidence_refs):
        # The request digest comes from the provider's registered live completion,
        # not an author-supplied signer field.
        capability = self.authority._handles.get(handle) if type(handle) is CallerHandle else None
        if capability is None:
            raise SecurityAuthorityError()
        binding = json.loads(capability.binding_json)
        if (binding.get("record_digest") != record.revision_digest
                or fingerprint not in binding.get("finding_fingerprints", ())
                or list(evidence_refs) != binding.get("evidence_refs")):
            raise SecurityAuthorityError()
        caller = self.authority.validate(handle, operation=operation, binding=binding)
        if caller["actor"] == self.authority.author:
            raise SecurityAuthorityError()
        return caller

    def _evidence(self, refs, *, fingerprint=None):
        rows = []
        for ref in refs:
            observation = self.source_port.lookup(ref)
            if observation is None:
                raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
            metadata = observation.metadata
            if (metadata["status"] != "VERIFIED" or metadata["scope_ref"] != self.authority.scope_ref
                    or metadata["coverage"] != "COMPLETE_FOR_DECLARED_SCOPE"
                    or metadata["coverage_gaps"] or timestamp(metadata["checked_at"]) > self.authority._clock()
                    or compare_identity(decode_identity(metadata["identity"]), self.authority.current_identity,
                                        tuple(metadata["required_components"])).state != IdentityState.VALID
                    or metadata["producer_ref"] == self.authority.author):
                raise SecurityAuthorityError("EVIDENCE_STALE")
            rows.append((metadata, observation.facts, observation.provenance_ref))
        return rows

    def reverify(self, fingerprint: str, *, handle, evidence_refs: tuple[str, ...],
                 event_id: str, expected_revision_digest: str):
        record, value = self._current()
        input_digest = digest({"operation": "reverify", "fingerprint": fingerprint, "evidence_refs": list(evidence_refs)})
        replay = self._replay(value, event_id, input_digest)
        if replay is not None:
            return replay
        caller = self._review_caller(handle, "reverify", record, fingerprint, evidence_refs)
        finding, snapshot, issue = self._issue(value, fingerprint)
        rows = self._evidence(evidence_refs, fingerprint=fingerprint)
        if not self._repair_checks_pass(rows, fingerprint):
            raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
        if finding["dependency_ref"] is not None:
            self._dependency_repair_evidence(record, value, finding, evidence_refs)
        after = transition_issue(snapshot, issue.fingerprint, IssueState.VERIFIED, IssueAuthority.REVIEWER,
                                 input_digest=input_digest)
        value["lifecycle"] = json.loads(lifecycle_bytes(after))
        for metadata, _facts, _provenance in rows:
            if not any(m["ref"] == metadata["ref"] for m in value["sources"]):
                value["sources"].append(metadata)
        if all(i.state == IssueState.VERIFIED for i in after.issues):
            value["coverage_gaps"] = [g for g in value["coverage_gaps"] if g != "SOURCE_EVIDENCE_UNAVAILABLE"]
            value["required_evidence"] = [r for r in value["required_evidence"] if r != "SOURCE_EVIDENCE_REQUIRED"]
        return self._write(record, value, event_id=event_id, operation="reverify", input_digest=input_digest,
            actor_ref=caller["provenance_ref"], evidence_refs=evidence_refs,
            expected_revision_digest=expected_revision_digest)

    @staticmethod
    def _repair_checks_pass(rows, fingerprint):
        negatives = [f for m, f, _p in rows if m["kind"] == "negative-test"
                     and f.get("finding_fingerprint") == fingerprint]
        regressions = [f for m, f, _p in rows if m["kind"] == "regression"]
        return (bool(negatives) and bool(regressions)
                and all(f.get("passed") is True and f.get("original_condition_absent") is True for f in negatives)
                and all(f.get("passed") is True for f in regressions))

    def _dependency_repair_evidence(self, record, value, finding, evidence_refs):
        if not any(d["dependency_ref"] == finding["dependency_ref"] for d in value["dependency_decisions"]):
            raise SecurityAuthorityError("DEPENDENCY_STILL_AFFECTED")
        required = tuple(f"{namespace}.{key}" for namespace, values in self.authority.current_identity.components
                         for key, _component_digest in values)
        try:
            expected = self._service().compose_evidence(record, current_identity=self.authority.current_identity,
                required_components=required, requirements=("checks:dependency-delivery-check",), stage=value["stage"])
            row = expected["checks:dependency-delivery-check"]
            if (row["status"] != "PASS" or not row["evidence_refs"]
                    or not set(row["evidence_refs"]).issubset(evidence_refs)):
                raise SecurityAuthorityError("DEPENDENCY_STILL_AFFECTED")
            for ref in row["evidence_refs"]:
                if self.source_port.lookup(ref) is None:
                    receipt = self.authority.read_provenance(ref, live=True)
                    if receipt is not None and receipt["event"]["operation"] == "accept-risk":
                        # A valid risk disposition may satisfy a delivery check,
                        # but an affected dependency is still not a repair.
                        raise SecurityAuthorityError("DEPENDENCY_STILL_AFFECTED")
            return row
        except (ValueError, KeyError, TypeError):
            raise SecurityAuthorityError("DEPENDENCY_STILL_AFFECTED") from None

    def resolution_binding(self, fingerprint: str, *, kind: str, reason: str, evidence_refs: tuple[str, ...],
                           expires_at: str | None, required_components: tuple[str, ...]) -> dict:
        record, value = self._current()
        self._issue(value, fingerprint)
        if (kind not in {"ACCEPTED_RISK", "FALSE_POSITIVE"} or not reason
                or compare_identity(self.authority.current_identity, self.authority.current_identity,
                                    required_components).state != IdentityState.VALID):
            raise SecurityAuthorityError()
        if expires_at is not None and timestamp(expires_at) <= self.authority._clock():
            raise SecurityAuthorityError()
        return {"operation": "accept-risk" if kind == "ACCEPTED_RISK" else "false-positive",
                "finding_fingerprint": fingerprint, "record_digest": record.revision_digest,
                "kind": kind, "reason": reason, "evidence_refs": list(evidence_refs),
                "scope_ref": self.authority.scope_ref, "expires_at": expires_at,
                "required_components": list(required_components),
                "identity_digest": self.authority.current_identity.envelope_digest}

    def resolve(self, fingerprint: str, *, kind: str, reason: str, evidence_refs: tuple[str, ...],
                expires_at: str | None, required_components: tuple[str, ...], handle,
                event_id: str, expected_revision_digest: str):
        record, value = self._current()
        event_input = {"operation": "resolve", "fingerprint": fingerprint, "kind": kind,
                       "reason": reason, "evidence_refs": list(evidence_refs), "expires_at": expires_at,
                       "required_components": list(required_components)}
        input_digest = digest(event_input)
        replay = self._replay(value, event_id, input_digest)
        if replay is not None:
            return replay
        binding = self.resolution_binding(fingerprint, kind=kind, reason=reason, evidence_refs=evidence_refs,
                                          expires_at=expires_at, required_components=required_components)
        rows = []
        if kind == "ACCEPTED_RISK":
            caller = self.authority.validate(handle, operation="accept-risk", binding=binding)
            proof_ref, decision_ref = None, caller["provenance_ref"]
        else:
            caller = self._review_caller(handle, "false-positive", record, fingerprint, evidence_refs)
            rows = self._evidence(evidence_refs)
            proofs = [m["ref"] for m, f, _p in rows if m["kind"] == "counterevidence"
                      and f.get("finding_fingerprint") == fingerprint and f.get("false_positive") is True]
            if not proofs:
                raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
            proof_ref, decision_ref = proofs[0], None
        resolution = {"finding_fingerprint": fingerprint, "kind": kind, "proof_ref": proof_ref,
            "decision_ref": decision_ref, "reason": reason, "scope_ref": self.authority.scope_ref,
            "expires_at": expires_at, "required_components": list(required_components),
            "identity_digest": self.authority.current_identity.envelope_digest,
            "trusted_actor_ref": caller["provenance_ref"], "event_id": event_id}
        value["resolutions"] = [r for r in value["resolutions"] if r["finding_fingerprint"] != fingerprint] + [resolution]
        for metadata, _facts, _p in rows:
            if not any(m["ref"] == metadata["ref"] for m in value["sources"]):
                value["sources"].append(metadata)
        return self._write(record, value, event_id=event_id, operation="resolve", input_digest=input_digest,
            actor_ref=caller["provenance_ref"], evidence_refs=evidence_refs,
            expected_revision_digest=expected_revision_digest)

    def valid_resolution(self, resolution: dict) -> bool:
        try:
            if (resolution["scope_ref"] != self.authority.scope_ref
                    or resolution["identity_digest"] != self.authority.current_identity.envelope_digest
                    or (resolution["expires_at"] is not None
                        and timestamp(resolution["expires_at"]) <= self.authority._clock())):
                return False
            if resolution["kind"] == "ACCEPTED_RISK":
                receipt = self.authority.read_provenance(resolution["decision_ref"], live=True)
                return (receipt is not None and all(receipt["binding"].get(k) == resolution[k] for k in (
                    "kind", "reason", "scope_ref", "expires_at", "required_components", "identity_digest",
                    "finding_fingerprint")) and self.authority.validate_disposition(resolution["decision_ref"],
                    scope_ref=resolution["scope_ref"], identity_digest=resolution["identity_digest"],
                    finding_fingerprint=resolution["finding_fingerprint"]))
            if resolution["kind"] == "FALSE_POSITIVE":
                receipt = self.authority.read_provenance(resolution["trusted_actor_ref"], live=True)
                rows = self._evidence((resolution["proof_ref"],))
                return (receipt is not None and receipt["event"]["kind"] == "review-completed"
                        and receipt["event"]["actor"] != self.authority.author
                        and receipt["event"]["operation"] == "false-positive"
                        and resolution["finding_fingerprint"] in receipt["binding"]["finding_fingerprints"]
                        and any(m["kind"] == "counterevidence" and f.get("false_positive") is True
                                and f.get("finding_fingerprint") == resolution["finding_fingerprint"] for m, f, _p in rows))
            return False
        except (ValueError, KeyError, TypeError):
            return False

    def _check_gate_evidence(self, evidence_by_requirement, value=None):
        provenance = []
        for requirement_id, row in evidence_by_requirement.items():
            if row.get("status") != "PASS":
                continue
            refs = row.get("evidence_refs", ())
            observations = [self.source_port.lookup(ref) for ref in refs]
            validated_decision_refs = set()
            if requirement_id in {"checks:dependency-design-check", "checks:dependency-delivery-check"}:
                record, _current_value = self._current()
                required = tuple(f"{namespace}.{key}" for namespace, values in self.authority.current_identity.components
                                 for key, _component_digest in values)
                compose = getattr(self._service(), "compose_evidence", None)
                if compose is None:
                    raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                expected = compose(record,
                    current_identity=self.authority.current_identity, required_components=required,
                    requirements=(requirement_id,), stage=value["stage"] if value is not None else None)
                expected_row = expected.get(requirement_id)
                if (expected_row is None or expected_row.get("status") != "PASS"
                        or not expected_row.get("evidence_refs")
                        or not set(expected_row["evidence_refs"]).issubset(refs)):
                    raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                # Only refs included by the freshly recomputed dependency
                # requirement may represent a direct-user risk disposition.
                # That service validates exact advisory digest/scope/expiry.
                for ref in expected_row["evidence_refs"]:
                    if self.source_port.lookup(ref) is None:
                        receipt = self.authority.read_provenance(ref, live=True)
                        if (receipt is not None and receipt["event"]["kind"] == "user-confirmation"
                                and receipt["event"]["operation"] == "accept-risk"):
                            validated_decision_refs.add(ref)
            if requirement_id in {"checks:security-review", "checks:security-review-check"}:
                if value is None:
                    _record, value = self._current()
                if not self._verify_historical_reverification(value):
                    raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                kinds = {o.metadata["kind"] for o in observations if o is not None}
                snapshot = load_lifecycle(canonical(value["lifecycle"]))
                if any(i.state in {IssueState.VERIFIED, IssueState.CLOSED} for i in snapshot.issues):
                    # Every verified finding's actual independent regression
                    # and negative-condition evidence must be included here.
                    for event in value["events"]:
                        if event["operation"] == "reverify" and not set(event["evidence_refs"]).issubset(refs):
                            raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                    if not {"negative-test", "regression"}.issubset(kinds):
                        raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                elif not any(o is not None and o.metadata["kind"] == "security-review"
                             and o.facts.get("status") == "PASSED" for o in observations):
                    raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
            if requirement_id.startswith("checks:security-scan:"):
                from aek.core.security.scanners import VERSIONS
                tool = requirement_id[len("checks:security-scan:"):]
                if value is None:
                    _record, value = self._current()
                if tool not in VERSIONS or not any(o is not None and o.metadata["kind"] == "scanner"
                    and o.metadata["tool_version"] == VERSIONS[tool] and o.metadata["run_id"] is not None
                    and o.metadata["producer_ref"] == tool
                    and o.facts.get("tool_id") == tool and o.facts.get("status") == "PASSED"
                    and isinstance(o.facts.get("plan_digest"), str) and len(o.facts["plan_digest"]) == 64
                    and self._scanner_material_current(o)
                    and any(run["id"] == o.metadata["run_id"] and run["status"] == "PASSED"
                            and run["producer_ref"] == tool and run["tool_version"] == VERSIONS[tool]
                            and run["scope_ref"] == o.metadata["scope_ref"]
                            and run["identity_digest"] == self.authority.current_identity.envelope_digest
                            and run["plan_digest"] == o.facts["plan_digest"] and not run["coverage_gaps"]
                            and o.metadata["ref"] in run["evidence_refs"] for run in value["runs"])
                    and any(event["operation"] == "execute" and event["trusted_actor_ref"] == o.provenance_ref
                            and o.metadata["ref"] in event["evidence_refs"] for event in value["events"])
                    for o in observations):
                    raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
            for ref in refs:
                observation = self.source_port.lookup(ref)
                if observation is not None:
                    rows = self._evidence((ref,))
                    if any(f.get("passed") is False or f.get("status") in {"ERROR", "FAILED", "PENDING", "UNKNOWN"}
                           for _m, f, _p in rows):
                        raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                    provenance.extend(p for _m, _f, p in rows)
                else:
                    receipt = self.authority.read_provenance(ref, live=True)
                    review = (receipt is not None and receipt["event"]["kind"] == "review-completed"
                              and receipt["event"]["actor"] != self.authority.author)
                    disposition = (receipt is not None and ref in validated_decision_refs
                                   and receipt["event"]["kind"] == "user-confirmation"
                                   and receipt["event"]["operation"] == "accept-risk")
                    if not (review or disposition):
                        raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
                    provenance.append(ref)
        return digest(sorted(set(provenance)))

    def _scanner_material_current(self, observation):
        """A host supplies the read-only material port; data cannot create it."""
        from aek.core.security.scanners import load_scanner_plan
        try:
            facts = observation.facts
            port = self._material_ports.get(facts["tool_id"])
            if port is None or observation.metadata["status"] != "VERIFIED":
                return False
            plan = load_scanner_plan(facts.get("plan_snapshot"))
            if (plan.plan_digest != facts["plan_digest"] or plan.tool_id != facts["tool_id"]
                    or plan.repository_ref != self.authority.repository_ref or plan.scope_ref != self.authority.scope_ref
                    or plan.identity_digest != self.authority.current_identity.envelope_digest):
                return False
            port.validate_plan_material(plan)
            return True
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            return False

    def on_gate_composed(self, *, route_result, identity, review_plan, evidence_by_requirement,
                         event_id: str, expected_revision_digest: str):
        """Invoke the original Composer here; never accept an imported verdict."""
        from aek.application.delivery_gate import compose_delivery_gate
        record, value = self._current()
        snapshot = load_lifecycle(canonical(value["lifecycle"]))
        inputs = {"route_result": route_result, "identity": asdict(identity),
                  "review_plan": review_plan.as_dict(), "evidence_by_requirement": evidence_by_requirement}
        input_digest = digest({"operation": "close", "inputs": inputs})
        replay = self._replay(value, event_id, input_digest)
        if replay is not None:
            if not self.recover()["closed_valid"]:
                raise SecurityAuthorityError("EVIDENCE_STALE")
            receipt = self.authority.read_provenance(value["gate_binding"]["gate_ref"], live=True)
            if receipt is None:
                raise SecurityAuthorityError()
            return {"result": receipt["binding"]["result"], "record": replay}
        result = compose_delivery_gate(route_result=route_result, identity=identity,
                                      review_plan=review_plan, evidence_by_requirement=evidence_by_requirement)
        if result["verdict"] != "READY":
            return {"result": result, "record": record}
        if (value["coverage_gaps"] or value["required_evidence"]
                or not snapshot.issues
                or any(i.state != IssueState.VERIFIED and not any(
                    r["finding_fingerprint"] == o.location and self.valid_resolution(r)
                    for r in value["resolutions"] for o in i.occurrences) for i in snapshot.issues)):
            raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
        provenance_digest = self._check_gate_evidence(evidence_by_requirement, value)
        binding = {"operation": "close", "result": result, "inputs": inputs,
                   "record_digest": record.revision_digest,
                   "lifecycle_digest": snapshot.snapshot_digest, "provenance_digest": provenance_digest}
        handle = self.authority._on_gate_composed(binding=binding)
        caller = self.authority.validate(handle, operation="close", binding=binding)
        proof = {"schema_version": 1, "stage": snapshot.stage, "source_kind": "delivery_composer",
                 "source_verdict": "READY", "source_gate_digest": digest(result),
                 "lifecycle_snapshot_digest": snapshot.snapshot_digest,
                 "identity_digest": self.authority.current_identity.envelope_digest,
                 "input_record_digest": record.revision_digest, "provenance_digest": provenance_digest}
        proof["proof_digest"] = closure_proof_digest(proof)
        after = project_pass_gate(snapshot, stage=snapshot.stage, status="PASS",
                                  lifecycle_snapshot_digest=snapshot.snapshot_digest, gate_digest=proof["proof_digest"])
        value["gate_binding"] = {"gate_ref": caller["provenance_ref"], "gate_digest": proof["proof_digest"],
            "identity_digest": self.authority.current_identity.envelope_digest,
            "input_record_digest": record.revision_digest, "before_lifecycle": value["lifecycle"],
            "before_lifecycle_digest": snapshot.snapshot_digest, "after_lifecycle_digest": after.snapshot_digest,
            "judge_event_ref": caller["provenance_ref"], "closure_proof": proof}
        value["lifecycle"] = json.loads(lifecycle_bytes(after))
        updated = self._write(record, value, event_id=event_id, operation="close", input_digest=input_digest,
            actor_ref=caller["provenance_ref"], evidence_refs=(caller["provenance_ref"],),
            expected_revision_digest=expected_revision_digest)
        return {"result": result, "record": updated}

    def recover(self) -> dict:
        """Recheck origin and current evidence; never deserialize capabilities."""
        from aek.application.delivery_gate import compose_delivery_gate
        from aek.application.review_evidence import ReviewEvidencePlan, ReviewEvidenceStep
        from aek.core.context.identity import IdentityComparison
        try:
            record, value = self._current()
        except ValueError:
            return {"closed_valid": False, "verified_valid": False, "valid_resolutions": [], "reason_codes": ["EVIDENCE_STALE"]}
        valid_resolutions = [r for r in value["resolutions"] if self.valid_resolution(r)]
        verified_valid = self._verify_historical_reverification(value)
        binding = value["gate_binding"]
        closed_valid = False
        if binding is not None:
            try:
                receipt = self.authority.read_provenance(binding["gate_ref"], live=True)
                judge = self.authority.read_provenance(binding["judge_event_ref"], live=True)
                if (receipt is None or judge is None or receipt != judge
                        or judge["event"]["kind"] != "gate-composed"
                        or judge["event"]["actor"] != "delivery-composer"
                        or judge["event"]["operation"] != "close"):
                    raise SecurityAuthorityError()
                saved = receipt["binding"]
                inputs = saved["inputs"]
                identity_data = inputs["identity"]
                plan_data = inputs["review_plan"]
                identity = IdentityComparison(IdentityState(identity_data["state"]),
                    tuple(identity_data["changed"]), tuple(identity_data["missing"]), tuple(identity_data["reason_codes"]))
                plan = ReviewEvidencePlan(tuple(ReviewEvidenceStep(s["channel"], s["reason_code"], tuple(s["required_tools"]))
                    for s in plan_data["steps"]), tuple(plan_data["blocking_reasons"]), tuple(plan_data["limitations"]),
                    plan_data["confidence"], plan_data["may_claim_complete_structure"])
                result = compose_delivery_gate(route_result=inputs["route_result"], identity=identity,
                    review_plan=plan, evidence_by_requirement=inputs["evidence_by_requirement"])
                proof = binding["closure_proof"]
                before = load_lifecycle(canonical(binding["before_lifecycle"]))
                after = project_pass_gate(before, stage=proof["stage"], status="PASS",
                    lifecycle_snapshot_digest=before.snapshot_digest, gate_digest=closure_proof_digest(proof))
                if (result != saved["result"] or result["verdict"] != "READY"
                        or saved["record_digest"] != binding["input_record_digest"]
                        or saved["lifecycle_digest"] != before.snapshot_digest
                        or digest(result) != proof["source_gate_digest"]
                        or self._check_gate_evidence(inputs["evidence_by_requirement"], value) != proof["provenance_digest"]
                        or saved["provenance_digest"] != proof["provenance_digest"]
                        or after != load_lifecycle(canonical(value["lifecycle"]))):
                    raise SecurityAuthorityError()
                closed_valid = verified_valid
            except (ValueError, KeyError, TypeError):
                closed_valid = False
        return {"record": record, "closed_valid": closed_valid, "verified_valid": verified_valid,
                "valid_resolutions": valid_resolutions,
                "reason_codes": [] if verified_valid and (binding is None or closed_valid) and len(valid_resolutions) == len(value["resolutions"])
                else ["SECURITY_PROVENANCE_REVALIDATION_REQUIRED"]}

    def _verify_historical_reverification(self, value: dict) -> bool:
        try:
            snapshot = load_lifecycle(canonical(value["lifecycle"]))
            for issue in snapshot.issues:
                if issue.state not in {IssueState.VERIFIED, IssueState.CLOSED}:
                    continue
                fingerprint = issue.occurrences[-1].location
                matched = []
                for event in value["events"]:
                    if event["operation"] != "reverify":
                        continue
                    receipt = self.authority.read_provenance(event["trusted_actor_ref"], live=True)
                    if (receipt is not None and receipt["event"]["kind"] == "review-completed"
                            and receipt["event"]["operation"] == "reverify"
                            and receipt["event"]["actor"] != self.authority.author
                            and fingerprint in receipt["binding"]["finding_fingerprints"]
                            and event["evidence_refs"] == receipt["binding"]["evidence_refs"]):
                        matched.append(event)
                if not matched:
                    return False
                rows = self._evidence(tuple(matched[-1]["evidence_refs"]))
                if not self._repair_checks_pass(rows, fingerprint):
                    return False
                finding, _snapshot, _issue = self._issue(value, fingerprint)
                if finding["dependency_ref"] is not None:
                    self._dependency_repair_evidence(self.store.load(), value, finding,
                                                     tuple(matched[-1]["evidence_refs"]))
            return True
        except (ValueError, KeyError, TypeError):
            return False
