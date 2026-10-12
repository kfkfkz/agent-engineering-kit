"""One immutable security record revision; integrity is not provenance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from aek.core.context.identity import IdentityState, compare_identity, decode_identity
from aek.core.review.lifecycle import (
    IssueState, initialize_lifecycle, lifecycle_bytes, load_lifecycle, project_pass_gate,
)
from aek.core.security.model import (
    SecurityInputError, _digest, _exact_object, _findings, _inventories,
    _coverage, _proposals, _rows, _scope, _scope_identity_binding, _strings, _surfaces, _text, decode_security_json, decode_security_request,
)

_RECORD_KEYS = {
    "schema_version", "review_id", "revision", "revision_digest", "stage", "scope", "identity",
    "surfaces", "findings", "dependency_decisions", "inventories", "sources", "resolutions",
    "runs", "invariants", "events", "lifecycle", "gate_binding", "coverage_gaps",
    "required_evidence", "residual_risk",
}


@dataclass(frozen=True)
class SecurityRecord:
    review_id: str
    revision: int
    revision_digest: str
    payload_json: str


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _revision_digest(value: dict[str, object]) -> str:
    body = {key: item for key, item in value.items() if key != "revision_digest"}
    return hashlib.sha256(b"aek-security-record-v1\0" + canonical_bytes(body)).hexdigest()


def finding_fingerprint(finding: dict[str, object], repository_ref: str | None = None) -> str:
    """Stable semantic proxy; supplied references must be resolved by the host.

    Repository provenance is independently bound by the controlled workflow.
    Neither line numbers in titles nor the current vulnerable version are IDs.
    """
    _findings([finding])
    return hashlib.sha256(canonical_bytes({
        "repository_ref": repository_ref, "category": finding["category"],
        "location_refs": sorted(finding["location_refs"]),
        "surface_refs": sorted(finding["surface_refs"]),
        "dependency_ref": finding["dependency_ref"], "advisory_refs": sorted(finding["advisory_refs"]),
    })).hexdigest()


def _timestamp(value: object) -> datetime:
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise SecurityInputError() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecurityInputError()
    return parsed


def _events(value: object) -> None:
    if type(value) is not list:
        raise SecurityInputError()
    ids = set()
    for row in value:
        row = _exact_object(row, {"event_id", "operation", "input_digest", "current_identity_digest",
                                  "trusted_actor_ref", "evidence_refs", "occurred_at"})
        event_id = _text(row["event_id"])
        if event_id in ids:
            raise SecurityInputError()
        ids.add(event_id)
        _text(row["operation"])
        _text(row["trusted_actor_ref"])
        _digest(row["input_digest"])
        _digest(row["current_identity_digest"])
        _strings(row["evidence_refs"])
        _timestamp(row["occurred_at"])


def _components(value: object, identity: object) -> None:
    names = _strings(value)
    try:
        envelope = decode_identity(identity)
    except (TypeError, ValueError):
        raise SecurityInputError() from None
    if compare_identity(envelope, envelope, names).state != IdentityState.VALID:
        raise SecurityInputError()


def _sources(value: object) -> None:
    for row in _rows(value, {"id", "ref", "run_id", "producer_ref", "identity", "required_components",
                             "kind", "status", "checked_at", "scope_ref", "tool_version", "rules_digest",
                             "database_digest", "coverage", "coverage_gaps", "summary"}):
        for key in ("ref", "producer_ref", "kind", "scope_ref", "summary"):
            _text(row[key])
        for key in ("run_id", "tool_version"):
            if row[key] is not None:
                _text(row[key])
        for key in ("rules_digest", "database_digest"):
            if row[key] is not None:
                _digest(row[key])
        if _text(row["status"]) not in {"DECLARED", "VERIFIED", "STALE", "INVALID"}:
            raise SecurityInputError()
        _timestamp(row["checked_at"])
        _coverage(row)
        _components(row["required_components"], row["identity"])


def _runs(value: object) -> None:
    for row in _rows(value, {"id", "plan_digest", "identity_digest", "producer_ref", "tool_version",
                             "configuration_digest", "scope_ref", "status", "evidence_refs", "coverage_gaps",
                             "started_at", "finished_at", "reason_codes"}):
        for key in ("plan_digest", "identity_digest", "configuration_digest"):
            _digest(row[key])
        for key in ("producer_ref", "scope_ref"):
            _text(row[key])
        if row["tool_version"] is not None:
            _text(row["tool_version"])
        status = _text(row["status"])
        if status not in {"NOT_RUN", "PASSED", "FAILED", "BLOCKED", "ERROR", "INCONCLUSIVE"}:
            raise SecurityInputError()
        for key in ("evidence_refs", "coverage_gaps", "reason_codes"):
            _strings(row[key])
        started = _timestamp(row["started_at"]) if row["started_at"] is not None else None
        finished = _timestamp(row["finished_at"]) if row["finished_at"] is not None else None
        if ((status == "NOT_RUN" and (started is not None or finished is not None))
                or (finished is not None and (started is None or finished < started))
                or (status == "PASSED" and (started is None or finished is None or not row["evidence_refs"]))):
            raise SecurityInputError()


def _invariants(value: object) -> None:
    for row in _rows(value, {"id", "statement", "source_refs", "entry_refs", "test_refs", "status", "evidence_refs"}):
        _text(row["statement"])
        for key in ("source_refs", "entry_refs", "test_refs", "evidence_refs"):
            _strings(row[key])
        status = _text(row["status"])
        if (status not in {"CANDIDATE", "CONFIRMED", "INVALIDATED"}
                or (status == "CONFIRMED" and (not row["source_refs"] or not row["evidence_refs"]))):
            raise SecurityInputError()


def _resolutions(value: object) -> None:
    if type(value) is not list:
        raise SecurityInputError()
    fingerprints = set()
    for row in value:
        row = _exact_object(row, {"finding_fingerprint", "kind", "proof_ref", "decision_ref", "reason",
                                  "scope_ref", "expires_at", "required_components", "identity_digest",
                                  "trusted_actor_ref", "event_id"})
        fingerprint = _digest(row["finding_fingerprint"])
        if fingerprint in fingerprints:
            raise SecurityInputError()
        fingerprints.add(fingerprint)
        for key in ("reason", "scope_ref", "trusted_actor_ref", "event_id"):
            _text(row[key])
        _digest(row["identity_digest"])
        components = _strings(row["required_components"])
        for key in ("proof_ref", "decision_ref"):
            if row[key] is not None:
                _text(row[key])
        if row["expires_at"] is not None:
            _timestamp(row["expires_at"])
        kind = _text(row["kind"])
        if (kind not in {"NONE", "FALSE_POSITIVE", "ACCEPTED_RISK"}
                or (kind == "NONE" and (row["proof_ref"] is not None or row["decision_ref"] is not None))
                or (kind == "FALSE_POSITIVE" and (row["proof_ref"] is None or not components))
                or (kind == "ACCEPTED_RISK" and (row["decision_ref"] is None
                                                or row["expires_at"] is None or not components))):
            raise SecurityInputError()


def closure_proof_digest(value: dict[str, object]) -> str:
    body = {key: item for key, item in value.items() if key != "proof_digest"}
    return hashlib.sha256(b"aek-security-closure-v1\0" + canonical_bytes(body)).hexdigest()


def _gate_binding(value: object, current_lifecycle: object, identity: object) -> None:
    current = load_lifecycle(canonical_bytes(current_lifecycle))
    if value is None:
        if any(issue.state == IssueState.CLOSED for issue in current.issues):
            raise SecurityInputError()
        return
    row = _exact_object(value, {"gate_ref", "gate_digest", "identity_digest", "input_record_digest",
                                "before_lifecycle", "before_lifecycle_digest", "after_lifecycle_digest",
                                "judge_event_ref", "closure_proof"})
    for key in ("gate_ref", "judge_event_ref"):
        _text(row[key])
    for key in ("gate_digest", "identity_digest", "input_record_digest",
                "before_lifecycle_digest", "after_lifecycle_digest"):
        _digest(row[key])
    proof = _exact_object(row["closure_proof"], {
        "schema_version", "stage", "source_kind", "source_verdict", "source_gate_digest",
        "lifecycle_snapshot_digest", "identity_digest", "input_record_digest", "provenance_digest", "proof_digest"})
    if (type(proof["schema_version"]) is not int or proof["schema_version"] != 1
            or proof["source_kind"] != "delivery_composer" or proof["source_verdict"] != "READY"):
        raise SecurityInputError()
    _text(proof["stage"])
    for key in ("source_gate_digest", "lifecycle_snapshot_digest", "identity_digest",
                "input_record_digest", "provenance_digest", "proof_digest"):
        _digest(proof[key])
    try:
        before = load_lifecycle(canonical_bytes(row["before_lifecycle"]))
        envelope = decode_identity(identity)
        after = project_pass_gate(before, stage=proof["stage"], status="PASS",
                                  lifecycle_snapshot_digest=row["before_lifecycle_digest"],
                                  gate_digest=proof["proof_digest"])
    except (TypeError, ValueError, KeyError):
        raise SecurityInputError() from None
    if (proof["proof_digest"] != closure_proof_digest(proof)
            or row["gate_digest"] != proof["proof_digest"]
            or proof["lifecycle_snapshot_digest"] != before.snapshot_digest
            or row["identity_digest"] != proof["identity_digest"]
            or row["identity_digest"] != envelope.envelope_digest
            or row["input_record_digest"] != proof["input_record_digest"]
            or row["after_lifecycle_digest"] != after.snapshot_digest or after != current):
        raise SecurityInputError()


def _dependency_decisions(value: object) -> None:
    if type(value) is not list:
        raise SecurityInputError()
    refs = set()
    for row in value:
        row = _exact_object(row, {"dependency_ref", "proposal", "status", "reason_codes",
                                  "source_refs", "pending_delivery_requirements"})
        ref = _text(row["dependency_ref"])
        _proposals([row["proposal"]])
        if ref in refs or ref != row["proposal"]["id"]:
            raise SecurityInputError()
        refs.add(ref)
        if _text(row["status"]) not in {"PENDING", "ADOPTABLE", "REJECTED", "NOT_APPLICABLE"}:
            raise SecurityInputError()
        for key in ("reason_codes", "source_refs", "pending_delivery_requirements"):
            _strings(row[key])
        if ((row["status"] == "ADOPTABLE" and (row["proposal"]["version"] is None or not row["source_refs"]))
                or (row["status"] in {"PENDING", "REJECTED"} and not row["reason_codes"])):
            raise SecurityInputError()


def load_security_record(raw: bytes) -> SecurityRecord:
    """Decode stored data only; this does not confer writer/reviewer authority."""
    value = _exact_object(decode_security_json(raw), _RECORD_KEYS)
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["revision"]) is not int or value["revision"] < 1
            or _text(value["stage"]) not in {"design", "execution", "closeout"}):
        raise SecurityInputError()
    _text(value["review_id"])
    _digest(value["revision_digest"])
    _scope(value["scope"])
    try:
        envelope = decode_identity(value["identity"])
        _scope_identity_binding(value["scope"], envelope)
        snapshot = load_lifecycle(canonical_bytes(value["lifecycle"]))
        if snapshot.subject_digest != value["scope"]["subject_digest"]:
            raise SecurityInputError()
    except (TypeError, ValueError, KeyError):
        raise SecurityInputError() from None
    _surfaces(value["surfaces"])
    _findings(value["findings"])
    _inventories(value["inventories"])
    for key in ("dependency_decisions", "sources", "resolutions", "runs", "invariants", "events"):
        if type(value[key]) is not list:
            raise SecurityInputError()
    _events(value["events"])
    _sources(value["sources"])
    _runs(value["runs"])
    _invariants(value["invariants"])
    _resolutions(value["resolutions"])
    _dependency_decisions(value["dependency_decisions"])
    _gate_binding(value["gate_binding"], value["lifecycle"], value["identity"])
    for key in ("coverage_gaps", "required_evidence", "residual_risk"):
        _strings(value[key])
    if value["revision_digest"] != _revision_digest(value):
        raise SecurityInputError()
    return SecurityRecord(value["review_id"], value["revision"], value["revision_digest"],
                          canonical_bytes(value).decode("utf-8"))


def seal_security_record(value: dict[str, object]) -> SecurityRecord:
    """Seal controlled application data; never used to import authority from CLI."""
    payload = {**value, "revision_digest": _revision_digest(value)}
    return load_security_record(canonical_bytes(payload))


def record_bytes(record: SecurityRecord) -> bytes:
    if not isinstance(record, SecurityRecord):
        raise SecurityInputError()
    raw = record.payload_json.encode("utf-8")
    if load_security_record(raw) != record:
        raise SecurityInputError()
    return raw


def record_as_dict(record: SecurityRecord) -> dict[str, object]:
    return json.loads(record_bytes(record))


def new_security_record(raw: bytes, *, review_id: str) -> SecurityRecord:
    """Create a candidate snapshot with no grants, verified sources or Gate."""
    request = decode_security_request(raw)
    if request.operation != "evaluate":
        raise SecurityInputError()
    payload = json.loads(request.payload_json)
    request_digest = hashlib.sha256(raw).hexdigest()
    repository_ref = payload["identity"]["components"]["subject"].get("repo_digest")
    proxies = []
    for finding in payload["findings"]:
        if finding["verification_status"] != "SUSPECTED":
            finding["verification_status"] = "INCONCLUSIVE"
        fingerprint = finding_fingerprint(finding, repository_ref)
        proxies.append({"id": finding["id"], "location": fingerprint,
                        "problem": fingerprint, "evidence": "Declared candidate; independent proof required",
                        "required_change": fingerprint, "affected_ids": finding["affected_refs"],
                        "severity": "blocker" if finding["severity"] in {"CRITICAL", "HIGH", "UNKNOWN"}
                                    else "major" if finding["severity"] == "MEDIUM" else "minor"})
    lifecycle = initialize_lifecycle(
        stage="security", subject_digest=payload["scope"]["subject_digest"],
        request_digest=request_digest, doc_hashes={"security-input.json": request_digest},
        issues=tuple(proxies), iteration=1, max_iterations=3,
    )
    return seal_security_record({
        "schema_version": 1, "review_id": _text(review_id), "revision": 1,
        "stage": request.stage, "scope": payload["scope"], "identity": payload["identity"],
        "surfaces": payload["surfaces"], "findings": payload["findings"],
        "dependency_decisions": [{"dependency_ref": proposal["id"], "proposal": proposal,
                                  "status": "PENDING", "source_refs": [],
                                  "reason_codes": ["SOURCE_EVIDENCE_UNAVAILABLE"]
                                                  + (["VERSION_UNKNOWN"] if proposal["version"] is None else []),
                                  "pending_delivery_requirements": ["RESOLVED_INVENTORY_REQUIRED",
                                                                    "SHIPPED_INVENTORY_IF_APPLICABLE"]}
                                 for proposal in payload["proposals"]],
        "inventories": payload["inventories"], "sources": [],
        "resolutions": [], "runs": [], "invariants": [], "events": [],
        "lifecycle": json.loads(lifecycle_bytes(lifecycle)), "gate_binding": None,
        "coverage_gaps": ["SOURCE_EVIDENCE_UNAVAILABLE"],
        "required_evidence": ["SOURCE_EVIDENCE_REQUIRED"], "residual_risk": [],
    })
