"""Builtin authority for a trusted control-side session.

The host adapter must expose actual direct-user/tool events. The default
adapter is deliberately unavailable. This boundary does not defend against
an attacker able to execute arbitrary Python as the controller or rewrite it.
Persisted provenance is an immutable event receipt index, not a finding DB.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import json
import uuid
from typing import Callable

from aek.adapters.atomic_file import AtomicFileStore
from aek.core.context.identity import IdentityEnvelope, identity_as_dict
from aek.core.security.authority import (
    CallerHandle, HostEvent, RuntimeGrant, SecurityAuthorityError,
    canonical, digest, timestamp, utc_now, runtime_binding,
)


class HostEventSource:
    """Implement only in the host control plane, never from imported JSON.

consume must authenticate the opaque live event and prevent reuse. restore
must independently confirm the exact historical event receipt. Neither method
may trust sender/role/approved_by fields in user content or scanner output.
"""
    def consume(self, kind: str, live_event: object) -> HostEvent:
        raise SecurityAuthorityError("AUTHORIZATION_SOURCE_UNAVAILABLE")

    def restore(self, event: HostEvent) -> bool:
        return False

    def capabilities(self) -> dict:
        return {"available": False, "mode": "unavailable"}

    def on_gate_composed(self, event: HostEvent, binding: dict) -> None:
        """Optionally retain the genuine local Composer callback for restart."""
        return None


@dataclass(frozen=True)
class _Capability:
    operation: str
    actor: str
    binding_json: str
    provenance_ref: str
    expires_at: str


class BuiltinWorkflowAuthority:
    def __init__(self, *, repository_ref: str, task_id: str, scope_ref: str,
                 current_identity: IdentityEnvelope, author: str,
                 host_source: HostEventSource | None = None,
                 provenance_root: Path | None = None,
                 allowed_operations: tuple[str, ...] = (
                     "claim-fixed", "reverify", "false-positive", "accept-risk",
                     "execute", "source", "close", "confirm-invariant"),
                 expires_at: str | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None:
        identity_data = identity_as_dict(current_identity)
        if any(not isinstance(v, str) or not v for v in
               (repository_ref, task_id, scope_ref, author)):
            raise SecurityAuthorityError()
        identity_scope = identity_data["components"]["subject"].get("scope_digest")
        if identity_scope is not None and identity_scope != scope_ref:
            raise SecurityAuthorityError("SCOPE_BINDING_MISMATCH")
        self.repository_ref, self.task_id, self.scope_ref = repository_ref, task_id, scope_ref
        self.current_identity, self.author = current_identity, author
        self.host_source = host_source or HostEventSource()
        self._clock = clock
        self.expires_at = expires_at or (clock() + timedelta(hours=1)).isoformat()
        timestamp(self.expires_at)
        self.allowed_operations = frozenset(allowed_operations)
        self._session_id, self._issuer = uuid.uuid4().hex, uuid.uuid4().hex
        self._handles: dict[CallerHandle, _Capability] = {}
        self._workers: dict[str, tuple[str, str, tuple[str, ...], str]] = {}
        self._receipts: dict[str, dict] = {}
        self._files = AtomicFileStore(provenance_root) if provenance_root is not None else None

    def capabilities(self) -> dict:
        host = self.host_source.capabilities()
        available = host.get("available") is True
        return {"available": available, "host_mode": host.get("mode", "unknown"),
                "operations": sorted(self.allowed_operations) if available else ["evaluate", "import", "plan"],
                "reason_codes": [] if available else ["AUTHORIZATION_SOURCE_UNAVAILABLE"],
                "threat_boundary": "trusted host control-side Python; imported content cannot mint handles; arbitrary controller-code execution is outside this boundary"}

    def _active(self, operation: str) -> None:
        if operation not in self.allowed_operations or self._clock() >= timestamp(self.expires_at):
            raise SecurityAuthorityError()

    def _record(self, event: HostEvent, binding: dict, review_proof: dict | None = None) -> str:
        body = {"schema_version": 1, "issuer": self._issuer,
                "session": self._session_id, "event": asdict(event), "binding": binding}
        if review_proof is not None:
            body["review_proof"] = review_proof
        ref = "provenance-" + digest(body)
        self._receipts[ref] = body
        if self._files is not None:
            name = ref + ".json"
            existing = self._files.read(name)
            raw = canonical(body)
            if existing is not None and existing != raw:
                raise SecurityAuthorityError()
            self._files.write(name, raw)
        return ref

    def _live(self, kind: str, event: object, operation: str, binding: dict,
              actor: str | None = None) -> HostEvent:
        self._active(operation)
        if self.host_source.capabilities().get("available") is not True:
            raise SecurityAuthorityError("AUTHORIZATION_SOURCE_UNAVAILABLE")
        try:
            received = self.host_source.consume(kind, event)
        except (TypeError, ValueError):
            raise SecurityAuthorityError() from None
        now = self._clock()
        if (not isinstance(received, HostEvent) or received.kind != kind
                or received.operation != operation
                or (actor is not None and received.actor != actor)
                or received.task_id != self.task_id
                or received.repository_ref != self.repository_ref
                or received.scope_ref != self.scope_ref
                or received.identity_digest != self.current_identity.envelope_digest
                or received.binding_digest != digest(binding)
                or timestamp(received.occurred_at) > now
                or timestamp(received.expires_at) <= now
                or timestamp(received.expires_at) > timestamp(self.expires_at)
                or not received.event_id or not received.actor):
            raise SecurityAuthorityError()
        return received

    def _mint(self, event: HostEvent, binding: dict, cls=CallerHandle,
              review_proof: dict | None = None) -> CallerHandle:
        handle = object.__new__(cls)
        ref = self._record(event, binding, review_proof)
        self._handles[handle] = _Capability(event.operation, event.actor,
                                         canonical(binding).decode(), ref, event.expires_at)
        return handle

    def on_user_confirmation(self, event: object, *, binding: dict) -> CallerHandle:
        """The host emits this event only for a direct positive confirmation.

        Rejection, quoted confirmation, and an imported approved_by field must
        never be authenticated by HostEventSource as user-confirmation.
        """
        operation = binding.get("operation")
        if operation not in {"execute", "accept-risk", "confirm-invariant"}:
            raise SecurityAuthorityError()
        received = self._live("user-confirmation", event, operation, binding)
        if operation == "accept-risk":
            if (binding.get("scope_ref") != self.scope_ref
                    or binding.get("identity_digest") != self.current_identity.envelope_digest
                    or timestamp(binding.get("expires_at")) <= self._clock()
                    or timestamp(binding.get("expires_at")) > timestamp(received.expires_at)):
                raise SecurityAuthorityError()
        return self._mint(received, binding, RuntimeGrant if operation == "execute" else CallerHandle)

    def register_reviewer(self, worker_id: str, *, request_digest: str,
                          record_digest: str, finding_fingerprints: tuple[str, ...]) -> None:
        self._active("reverify")
        if (not worker_id or worker_id == self.author or worker_id in self._workers
                or not finding_fingerprints or len(set(finding_fingerprints)) != len(finding_fingerprints)):
            raise SecurityAuthorityError()
        self._workers[worker_id] = (request_digest, record_digest,
                                  finding_fingerprints, self.current_identity.envelope_digest)

    def on_review_completed(self, event: object, *, worker_id: str, request,
                            result_text: str, receipt_text: str, binding: dict) -> CallerHandle:
        from aek.application.review import review_request_bytes, validate_review_result
        review_request_bytes(request)
        registered = self._workers.get(worker_id)
        checked = validate_review_result(request, result_text, receipt_text)
        if (registered is None or registered != (
                request.request_digest, binding.get("record_digest"),
                tuple(binding.get("finding_fingerprints", ())), self.current_identity.envelope_digest)
                or binding.get("request_digest") != request.request_digest
                or checked.state != "VALID" or checked.result is None or checked.result.issues):
            raise SecurityAuthorityError()
        received = self._live("review-completed", event, binding.get("operation"), binding, worker_id)
        return self._mint(received, binding, review_proof={
            "request": review_request_bytes(request).decode(), "result_text": result_text,
            "receipt_text": receipt_text})

    def on_source_completed(self, event: object, *, binding: dict) -> CallerHandle:
        received = self._live("tool-completed", event, "source", binding)
        if received.actor == self.author:
            raise SecurityAuthorityError()
        return self._mint(received, binding)

    def _on_gate_composed(self, *, binding: dict) -> CallerHandle:
        """Only the workflow calls this after invoking the original Composer."""
        self._active("close")
        if binding.get("result", {}).get("verdict") != "READY":
            raise SecurityAuthorityError()
        if self.host_source.capabilities().get("available") is not True:
            raise SecurityAuthorityError("AUTHORIZATION_SOURCE_UNAVAILABLE")
        event = HostEvent(uuid.uuid4().hex, "gate-composed", "delivery-composer", self.task_id,
                          self.repository_ref, self.scope_ref, self.current_identity.envelope_digest,
                          "close", digest(binding), self._clock().isoformat(), self.expires_at)
        self.host_source.on_gate_composed(event, binding)
        return self._mint(event, binding)

    def validate_disposition(self, decision_ref: str, *, scope_ref: str, identity_digest: str,
                             dependency_ref: str | None = None, version: str | None = None,
                             finding_fingerprint: str | None = None,
                             evidence_digest: str | None = None) -> bool:
        receipt = self.read_provenance(decision_ref, live=True)
        if receipt is None:
            return False
        event, binding = receipt["event"], receipt["binding"]
        try:
            if evidence_digest is not None and (
                    type(evidence_digest) is not str or len(evidence_digest) != 64
                    or any(character not in "0123456789abcdef" for character in evidence_digest)
                    or binding.get("evidence_digest") != evidence_digest):
                return False
            if (event["kind"] != "user-confirmation" or event["operation"] != "accept-risk"
                    or binding.get("operation") != "accept-risk" or binding.get("scope_ref") != scope_ref
                    or scope_ref != self.scope_ref or binding.get("identity_digest") != identity_digest
                    or identity_digest != self.current_identity.envelope_digest
                    or timestamp(binding.get("expires_at")) <= self._clock()):
                return False
            return all(binding.get(k) == v for k, v in (
                ("dependency_ref", dependency_ref), ("version", version),
                ("finding_fingerprint", finding_fingerprint)) if v is not None)
        except ValueError:
            return False

    def validate(self, handle: object, *, operation: str, binding: dict) -> dict:
        self._active(operation)
        capability = self._handles.get(handle) if type(handle) in {CallerHandle, RuntimeGrant} else None
        if (capability is None or capability.operation != operation
                or capability.binding_json != canonical(binding).decode()
                or timestamp(capability.expires_at) <= self._clock()):
            raise SecurityAuthorityError()
        receipt = self.read_provenance(capability.provenance_ref, live=True)
        if receipt is None:
            raise SecurityAuthorityError()
        return {"actor": capability.actor, "provenance_ref": capability.provenance_ref,
                "expires_at": capability.expires_at}

    def read_provenance(self, ref: str, *, live: bool = False,
                        expected_identity_digest: str | None = None) -> dict | None:
        if not isinstance(ref, str) or len(ref) != 75 or not ref.startswith("provenance-"):
            return None
        body = self._receipts.get(ref)
        if body is None and self._files is not None:
            raw = self._files.read(ref + ".json")
            try:
                body = json.loads(raw) if raw is not None else None
            except (ValueError, TypeError):
                return None
        try:
            if (body is None or set(body) not in (
                    {"schema_version", "issuer", "session", "event", "binding"},
                    {"schema_version", "issuer", "session", "event", "binding", "review_proof"})
                    or type(body["schema_version"]) is not int or body["schema_version"] != 1
                    or "provenance-" + digest(body) != ref):
                return None
            event = HostEvent(**body["event"])
            if (event.task_id != self.task_id or event.repository_ref != self.repository_ref
                    or event.scope_ref != self.scope_ref
                    or event.identity_digest != (expected_identity_digest or self.current_identity.envelope_digest)
                    or event.binding_digest != digest(body["binding"])
                    or timestamp(event.expires_at) <= self._clock()
                    or timestamp(event.occurred_at) > self._clock()):
                return None
            # A known live receipt still needs the host's current attestation:
            # revoking a producer receipt cannot be bypassed by a memory cache.
            # Gate receipts additionally need workflow recomposition.
            if self.host_source.restore(event) is not True:
                return None
            if event.kind == "review-completed":
                from aek.application.review import load_review_request, validate_review_result
                from aek.core.security.record import load_security_record
                proof = body["review_proof"]
                request = load_review_request(proof["request"].encode())
                checked = validate_review_result(request, proof["result_text"], proof["receipt_text"])
                prior = load_security_record(dict(request.supporting_context)["security-record"].encode())
                if (request.request_digest != body["binding"]["request_digest"]
                        or prior.revision_digest != body["binding"]["record_digest"]
                        or checked.state != "VALID" or checked.result is None or checked.result.issues):
                    return None
            return json.loads(canonical(body))
        except (TypeError, ValueError, KeyError):
            return None

    def validate_runtime_grant(self, grant: object, *, plan_digest: str,
                               repository_ref: str, scope_ref: str, identity_digest: str,
                               tool_id: str, executable_digest: str, action: str,
                               network_mode: str, write_scope, timeout_seconds: int,
                               max_output_bytes: int, max_items: int = 10000) -> None:
        if type(grant) is not RuntimeGrant:
            raise SecurityAuthorityError("AUTHORIZATION_REQUIRED")
        binding = runtime_binding(plan_digest=plan_digest, repository_ref=repository_ref,
            scope_ref=scope_ref, identity_digest=identity_digest, tool_id=tool_id,
            executable_digest=executable_digest, action=action, network_mode=network_mode,
            write_scope=write_scope, timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes, max_items=max_items)
        if (repository_ref != self.repository_ref or scope_ref != self.scope_ref
                or identity_digest != self.current_identity.envelope_digest):
            raise SecurityAuthorityError()
        caller = self.validate(grant, operation="execute", binding=binding)
        deadline = min(timestamp(caller["expires_at"]), timestamp(self.expires_at))
        if (deadline - self._clock()).total_seconds() < timeout_seconds:
            raise SecurityAuthorityError()
