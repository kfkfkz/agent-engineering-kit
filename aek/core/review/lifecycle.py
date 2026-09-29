"""Trusted lifecycle for issues emitted by an untrusted review sensor."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
import hashlib
import json
import re
import unicodedata


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SEVERITIES = {"blocker", "major", "minor"}


class IssueState(str, Enum):
    OPEN = "OPEN"
    FIXED = "FIXED"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"
    BLOCKED = "BLOCKED"


class IssueAuthority(str, Enum):
    AUTHOR = "AUTHOR"
    REVIEWER = "REVIEWER"
    JUDGE = "JUDGE"


@dataclass(frozen=True)
class IssueOccurrence:
    issue_id: str
    request_digest: str
    doc_hashes_digest: str
    location: str
    problem: str
    evidence: str
    required_change: str
    severity: str
    affected_ids: tuple[str, ...]


@dataclass(frozen=True)
class IssueLifecycle:
    fingerprint: str
    state: IssueState
    occurrences: tuple[IssueOccurrence, ...]
    gate_digest: str | None = None


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    precondition_snapshot_digest: str
    fingerprint: str
    from_state: IssueState
    to_state: IssueState
    authority: IssueAuthority
    input_digest: str
    gate_digest: str | None


@dataclass(frozen=True)
class LifecycleSnapshot:
    schema_version: int
    stage: str
    subject_digest: str
    iteration: int
    max_iterations: int
    issues: tuple[IssueLifecycle, ...]
    events: tuple[LifecycleEvent, ...]
    snapshot_digest: str


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"issue {field} must be text")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not normalized or len(normalized) > 4000:
        raise ValueError(f"issue {field} is empty or exceeds the bound")
    return normalized


def _occurrence(
    stage: str,
    request_digest: str,
    doc_hashes_digest: str,
    issue: dict[str, object],
) -> tuple[str, IssueOccurrence]:
    if not isinstance(issue, dict):
        raise ValueError("review issue must be an object")
    issue_id = _text(issue.get("id"), "id")
    location = _text(issue.get("location"), "location")
    problem = _text(issue.get("problem"), "problem")
    evidence = _text(issue.get("evidence"), "evidence")
    required_change = _text(issue.get("required_change"), "required_change")
    severity = _text(issue.get("severity"), "severity").lower()
    affected = issue.get("affected_ids")
    if (severity not in _SEVERITIES or not isinstance(affected, list)
            or not affected or any(not isinstance(item, str) or not item.strip()
                                   for item in affected)):
        raise ValueError("issue severity or affected IDs are invalid")
    affected_ids = tuple(sorted(set(_text(item, "affected_id") for item in affected)))
    fingerprint = _digest({
        "schema_version": 1,
        "stage": stage,
        "location": location,
        "problem": problem,
        "required_change": required_change,
        "affected_ids": affected_ids,
    })
    return fingerprint, IssueOccurrence(
        issue_id, request_digest, doc_hashes_digest, location, problem,
        evidence, required_change, severity, affected_ids,
    )


def _snapshot_payload(
    stage: str,
    subject_digest: str,
    iteration: int,
    max_iterations: int,
    issues: tuple[IssueLifecycle, ...],
    events: tuple[LifecycleEvent, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "stage": stage,
        "subject_digest": subject_digest,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "issues": [{
            "fingerprint": item.fingerprint,
            "state": item.state.value,
            "gate_digest": item.gate_digest,
            "occurrences": [{
                "issue_id": row.issue_id,
                "request_digest": row.request_digest,
                "doc_hashes_digest": row.doc_hashes_digest,
                "location": row.location,
                "problem": row.problem,
                "evidence": row.evidence,
                "required_change": row.required_change,
                "severity": row.severity,
                "affected_ids": row.affected_ids,
            } for row in item.occurrences],
        } for item in issues],
        "events": [{
            "event_id": event.event_id,
            "precondition_snapshot_digest": event.precondition_snapshot_digest,
            "fingerprint": event.fingerprint,
            "from_state": event.from_state.value,
            "to_state": event.to_state.value,
            "authority": event.authority.value,
            "input_digest": event.input_digest,
            "gate_digest": event.gate_digest,
        } for event in events],
    }


def _make_snapshot(
    stage: str,
    subject_digest: str,
    iteration: int,
    max_iterations: int,
    issues: tuple[IssueLifecycle, ...],
    events: tuple[LifecycleEvent, ...],
) -> LifecycleSnapshot:
    payload = _snapshot_payload(
        stage, subject_digest, iteration, max_iterations, issues, events)
    return LifecycleSnapshot(
        1, stage, subject_digest, iteration, max_iterations,
        issues, events, _digest(payload),
    )


def _snapshot_is_valid(snapshot: LifecycleSnapshot) -> bool:
    if (not isinstance(snapshot, LifecycleSnapshot)
            or type(snapshot.schema_version) is not int
            or snapshot.schema_version != 1
            or not isinstance(snapshot.stage, str) or not snapshot.stage.strip()
            or len(snapshot.stage) > 64
            or not _DIGEST.fullmatch(snapshot.subject_digest)
            or type(snapshot.iteration) is not int
            or type(snapshot.max_iterations) is not int
            or snapshot.iteration < 1
            or snapshot.iteration > snapshot.max_iterations
            or not isinstance(snapshot.issues, tuple)
            or not isinstance(snapshot.events, tuple)
            or not _DIGEST.fullmatch(snapshot.snapshot_digest)):
        return False
    fingerprints: set[str] = set()
    for item in snapshot.issues:
        if (not isinstance(item, IssueLifecycle)
                or not _DIGEST.fullmatch(item.fingerprint)
                or item.fingerprint in fingerprints
                or not isinstance(item.state, IssueState)
                or not isinstance(item.occurrences, tuple)
                or not item.occurrences
                or (item.state == IssueState.CLOSED) !=
                (isinstance(item.gate_digest, str)
                 and bool(_DIGEST.fullmatch(item.gate_digest)))):
            return False
        fingerprints.add(item.fingerprint)
        for row in item.occurrences:
            try:
                valid_text = all(_text(value, field) == value for field, value in (
                    ("id", row.issue_id), ("location", row.location),
                    ("problem", row.problem), ("evidence", row.evidence),
                    ("required_change", row.required_change)))
            except (AttributeError, ValueError):
                return False
            if (not isinstance(row, IssueOccurrence) or not valid_text
                    or not _DIGEST.fullmatch(row.request_digest)
                    or not _DIGEST.fullmatch(row.doc_hashes_digest)
                    or row.severity not in _SEVERITIES
                    or not isinstance(row.affected_ids, tuple)
                    or not row.affected_ids
                    or tuple(sorted(set(row.affected_ids))) != row.affected_ids):
                return False
    event_ids: set[str] = set()
    for event in snapshot.events:
        if (not isinstance(event, LifecycleEvent)
                or not _DIGEST.fullmatch(event.event_id)
                or event.event_id in event_ids
                or not _DIGEST.fullmatch(event.precondition_snapshot_digest)
                or event.fingerprint not in fingerprints
                or not isinstance(event.from_state, IssueState)
                or not isinstance(event.to_state, IssueState)
                or not isinstance(event.authority, IssueAuthority)
                or not _DIGEST.fullmatch(event.input_digest)
                or (event.to_state == IssueState.CLOSED) !=
                (isinstance(event.gate_digest, str)
                 and bool(_DIGEST.fullmatch(event.gate_digest)))):
            return False
        payload = {
            "schema_version": 1,
            "snapshot_digest": event.precondition_snapshot_digest,
            "fingerprint": event.fingerprint,
            "from_state": event.from_state.value,
            "to_state": event.to_state.value,
            "authority": event.authority.value,
            "input_digest": event.input_digest,
            "gate_digest": event.gate_digest,
        }
        if event.event_id != _digest(payload):
            return False
        event_ids.add(event.event_id)
    return _make_snapshot(
        snapshot.stage, snapshot.subject_digest, snapshot.iteration,
        snapshot.max_iterations, snapshot.issues,
        snapshot.events).snapshot_digest == snapshot.snapshot_digest


def lifecycle_bytes(snapshot: LifecycleSnapshot) -> bytes:
    """Serialize a verified snapshot as canonical, digest-bound JSON."""
    if not _snapshot_is_valid(snapshot):
        raise ValueError("lifecycle snapshot integrity check failed")
    return _canonical(asdict(snapshot))


def load_lifecycle(raw: bytes) -> LifecycleSnapshot:
    """Strictly decode lifecycle state; unknown fields and non-canonical input fail."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("lifecycle snapshot is malformed") from exc
    expected = set(LifecycleSnapshot.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("lifecycle snapshot has unknown or missing fields")
    try:
        issues = tuple(IssueLifecycle(
            row["fingerprint"], IssueState(row["state"]),
            tuple(IssueOccurrence(
                occurrence["issue_id"], occurrence["request_digest"],
                occurrence["doc_hashes_digest"], occurrence["location"],
                occurrence["problem"], occurrence["evidence"],
                occurrence["required_change"], occurrence["severity"],
                tuple(occurrence["affected_ids"]),
            ) for occurrence in row["occurrences"]),
            row["gate_digest"],
        ) for row in value["issues"])
        events = tuple(LifecycleEvent(
            row["event_id"], row["precondition_snapshot_digest"],
            row["fingerprint"],
            IssueState(row["from_state"]), IssueState(row["to_state"]),
            IssueAuthority(row["authority"]), row["input_digest"],
            row["gate_digest"],
        ) for row in value["events"])
        snapshot = LifecycleSnapshot(
            value["schema_version"], value["stage"],
            value["subject_digest"], value["iteration"],
            value["max_iterations"], issues, events,
            value["snapshot_digest"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("lifecycle snapshot fields are invalid") from exc
    if (type(snapshot.schema_version) is not int or snapshot.schema_version != 1
            or raw != lifecycle_bytes(snapshot)):
        raise ValueError("lifecycle snapshot integrity check failed")
    return snapshot


def initialize_lifecycle(
    *,
    stage: str,
    subject_digest: str,
    request_digest: str,
    doc_hashes: dict[str, str],
    issues: tuple[dict[str, object], ...],
    iteration: int,
    max_iterations: int,
) -> LifecycleSnapshot:
    if (not isinstance(stage, str) or not stage.strip() or len(stage) > 64
            or not _DIGEST.fullmatch(subject_digest)
            or not _DIGEST.fullmatch(request_digest)
            or type(iteration) is not int or type(max_iterations) is not int
            or iteration < 1 or max_iterations < 1 or iteration > max_iterations
            or not isinstance(doc_hashes, dict) or not doc_hashes
            or any(not isinstance(name, str) or not name
                   or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                   for name, digest in doc_hashes.items())
            or not isinstance(issues, tuple)):
        raise ValueError("lifecycle initialization input is invalid")
    doc_hashes_digest = _digest(doc_hashes)
    records: list[IssueLifecycle] = []
    fingerprints: set[str] = set()
    for raw_issue in issues:
        fingerprint, occurrence = _occurrence(
            stage, request_digest, doc_hashes_digest, raw_issue)
        if fingerprint in fingerprints:
            raise ValueError("review issues contain duplicate semantics")
        fingerprints.add(fingerprint)
        records.append(IssueLifecycle(
            fingerprint, IssueState.OPEN, (occurrence,)))
    return _make_snapshot(
        stage, subject_digest, iteration, max_iterations,
        tuple(sorted(records, key=lambda item: item.fingerprint)), ())


def ingest_review_occurrences(
    snapshot: LifecycleSnapshot,
    *,
    subject_digest: str,
    request_digest: str,
    doc_hashes: dict[str, str],
    issues: tuple[dict[str, object], ...],
    iteration: int,
) -> LifecycleSnapshot:
    """Append sensor occurrences without allowing the sensor to close issues."""
    if (not isinstance(snapshot, LifecycleSnapshot)
            or load_lifecycle(lifecycle_bytes(snapshot)) != snapshot
            or not _DIGEST.fullmatch(subject_digest)
            or not _DIGEST.fullmatch(request_digest)
            or type(iteration) is not int
            or iteration < snapshot.iteration
            or iteration > snapshot.max_iterations
            or not isinstance(doc_hashes, dict) or not doc_hashes
            or any(not isinstance(name, str) or not name
                   or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                   for name, digest in doc_hashes.items())
            or not isinstance(issues, tuple)):
        raise ValueError("review occurrence input is invalid")
    doc_hashes_digest = _digest(doc_hashes)
    records = {item.fingerprint: item for item in snapshot.issues}
    events = snapshot.events
    seen: set[str] = set()
    for raw_issue in issues:
        fingerprint, occurrence = _occurrence(
            snapshot.stage, request_digest, doc_hashes_digest, raw_issue)
        if fingerprint in seen:
            raise ValueError("review issues contain duplicate semantics")
        seen.add(fingerprint)
        current = records.get(fingerprint)
        if current is None:
            records[fingerprint] = IssueLifecycle(
                fingerprint, IssueState.OPEN, (occurrence,))
            continue
        if any(row.request_digest == request_digest for row in current.occurrences):
            continue
        next_state = current.state
        gate_digest = current.gate_digest
        if current.state in {IssueState.FIXED, IssueState.VERIFIED,
                             IssueState.CLOSED}:
            next_state = IssueState.REOPENED
            gate_digest = None
            payload = {
                "schema_version": 1,
                "snapshot_digest": snapshot.snapshot_digest,
                "fingerprint": fingerprint,
                "from_state": current.state.value,
                "to_state": IssueState.REOPENED.value,
                "authority": IssueAuthority.REVIEWER.value,
                "input_digest": request_digest,
                "gate_digest": None,
            }
            events += (LifecycleEvent(
                _digest(payload), snapshot.snapshot_digest, fingerprint,
                current.state,
                IssueState.REOPENED, IssueAuthority.REVIEWER,
                request_digest, None,
            ),)
        records[fingerprint] = replace(
            current, state=next_state,
            occurrences=current.occurrences + (occurrence,),
            gate_digest=gate_digest,
        )
    return _make_snapshot(
        snapshot.stage, subject_digest, iteration, snapshot.max_iterations,
        tuple(sorted(records.values(), key=lambda item: item.fingerprint)), events)


_TRANSITIONS = {
    (IssueState.OPEN, IssueState.FIXED): IssueAuthority.AUTHOR,
    (IssueState.REOPENED, IssueState.FIXED): IssueAuthority.AUTHOR,
    (IssueState.FIXED, IssueState.VERIFIED): IssueAuthority.REVIEWER,
    (IssueState.VERIFIED, IssueState.CLOSED): IssueAuthority.JUDGE,
    (IssueState.FIXED, IssueState.REOPENED): IssueAuthority.REVIEWER,
    (IssueState.VERIFIED, IssueState.REOPENED): IssueAuthority.REVIEWER,
    (IssueState.CLOSED, IssueState.REOPENED): IssueAuthority.REVIEWER,
    (IssueState.OPEN, IssueState.BLOCKED): IssueAuthority.JUDGE,
    (IssueState.REOPENED, IssueState.BLOCKED): IssueAuthority.JUDGE,
}


def _transition_issue(
    snapshot: LifecycleSnapshot,
    fingerprint: str,
    to_state: IssueState,
    authority: IssueAuthority,
    *,
    input_digest: str,
    gate_digest: str | None = None,
) -> LifecycleSnapshot:
    if (not isinstance(snapshot, LifecycleSnapshot)
            or not _DIGEST.fullmatch(snapshot.snapshot_digest)
            or _make_snapshot(
                snapshot.stage, snapshot.subject_digest, snapshot.iteration,
                snapshot.max_iterations, snapshot.issues,
                snapshot.events).snapshot_digest != snapshot.snapshot_digest
            or not isinstance(fingerprint, str)
            or not _DIGEST.fullmatch(fingerprint)
            or not isinstance(to_state, IssueState)
            or not isinstance(authority, IssueAuthority)
            or not isinstance(input_digest, str)
            or not _DIGEST.fullmatch(input_digest)):
        raise ValueError("lifecycle transition input is invalid")
    index = next(
        (i for i, item in enumerate(snapshot.issues)
         if item.fingerprint == fingerprint),
        None,
    )
    if index is None:
        raise ValueError("lifecycle issue does not exist")
    current = snapshot.issues[index]
    required_authority = _TRANSITIONS.get((current.state, to_state))
    if required_authority != authority:
        raise ValueError("lifecycle transition authority is denied")
    if (to_state == IssueState.CLOSED
            and (gate_digest is None or not _DIGEST.fullmatch(gate_digest))):
        raise ValueError("CLOSED requires a matching PASS gate digest")
    if to_state != IssueState.CLOSED and gate_digest is not None:
        raise ValueError("gate digest is only valid for CLOSED")
    if (to_state == IssueState.BLOCKED
            and snapshot.iteration < snapshot.max_iterations):
        raise ValueError("BLOCKED requires the iteration limit")
    event_payload = {
        "schema_version": 1,
        "snapshot_digest": snapshot.snapshot_digest,
        "fingerprint": fingerprint,
        "from_state": current.state.value,
        "to_state": to_state.value,
        "authority": authority.value,
        "input_digest": input_digest,
        "gate_digest": gate_digest,
    }
    event = LifecycleEvent(
        _digest(event_payload), snapshot.snapshot_digest, fingerprint,
        current.state, to_state,
        authority, input_digest, gate_digest,
    )
    updated_issue = replace(
        current,
        state=to_state,
        gate_digest=gate_digest if to_state == IssueState.CLOSED else None,
    )
    updated = list(snapshot.issues)
    updated[index] = updated_issue
    return _make_snapshot(
        snapshot.stage, snapshot.subject_digest, snapshot.iteration,
        snapshot.max_iterations, tuple(updated), snapshot.events + (event,))


def transition_issue(
    snapshot: LifecycleSnapshot,
    fingerprint: str,
    to_state: IssueState,
    authority: IssueAuthority,
    *,
    input_digest: str,
    gate_digest: str | None = None,
) -> LifecycleSnapshot:
    """Apply a claim/review transition; CLOSED has no direct public writer."""
    if to_state == IssueState.CLOSED:
        raise ValueError("CLOSED is derived only from a matching PASS gate")
    return _transition_issue(
        snapshot, fingerprint, to_state, authority,
        input_digest=input_digest, gate_digest=gate_digest)


def project_pass_gate(
    snapshot: LifecycleSnapshot,
    *,
    stage: str,
    status: str,
    lifecycle_snapshot_digest: str,
    gate_digest: str,
) -> LifecycleSnapshot:
    """Derive CLOSED only from a PASS gate bound to the VERIFIED snapshot.

    Replaying the same gate after a crash is idempotent.  The gate remains the
    authority; the returned CLOSED state is merely a materialized projection.
    """
    if (not isinstance(snapshot, LifecycleSnapshot)
            or load_lifecycle(lifecycle_bytes(snapshot)) != snapshot
            or stage != snapshot.stage or status != "PASS"
            or not _DIGEST.fullmatch(lifecycle_snapshot_digest)
            or not _DIGEST.fullmatch(gate_digest)):
        raise ValueError("PASS gate binding is invalid")
    if snapshot.snapshot_digest == lifecycle_snapshot_digest:
        result = snapshot
        for item in tuple(result.issues):
            if item.state == IssueState.VERIFIED:
                result = _transition_issue(
                    result, item.fingerprint, IssueState.CLOSED,
                    IssueAuthority.JUDGE,
                    input_digest=lifecycle_snapshot_digest,
                    gate_digest=gate_digest,
                )
            elif item.state == IssueState.BLOCKED:
                raise ValueError("PASS gate cannot override a BLOCKED issue")
        return result
    close_events = [event for event in snapshot.events
                    if event.to_state == IssueState.CLOSED]
    if (close_events
            and all(event.input_digest == lifecycle_snapshot_digest
                    and event.gate_digest == gate_digest
                    for event in close_events)
            and not any(item.state in {IssueState.VERIFIED, IssueState.BLOCKED}
                        for item in snapshot.issues)):
        return snapshot
    raise ValueError("PASS gate does not match the lifecycle snapshot")
