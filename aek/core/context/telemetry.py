"""Pure, metadata-only context event and report contracts.

The ledger records delivered sizes and digests, never prompt bodies, queries or
secrets. A complete report is valid only for an instrumented channel catalog.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ROUTES = {"direct", "bounded", "standard", "initiative"}
_STAGES = {"routing", "design", "execution", "closeout"}
_SOURCES = {"skill", "reference", "code", "test", "memory", "document", "tool",
            "capsule"}
_PURPOSES = {"route_required", "target_evidence", "risk_required", "review_required",
             "user_requested", "reroute_evidence", "benchmark_fixture"}
_CHANNEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NOTES = {"", "fallback", "cache_reuse", "reroute", "user_requested"}
_ZERO = "0" * 64


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class ContextEventInput:
    route: str
    stage: str
    source_kind: str
    channel: str
    source_digest: str
    delivered_bytes: int
    delivered_chars: int
    logical_bytes: int
    purpose: str
    cache_hit: bool
    session_id: str
    subject_digest: str
    logical_path: str
    occurred_at: str
    note: str
    duplicate_delivery: bool

    def __post_init__(self) -> None:
        if self.route not in _ROUTES or self.stage not in _STAGES:
            raise ValueError("unknown route or stage")
        if self.source_kind not in _SOURCES or self.purpose not in _PURPOSES:
            raise ValueError("unknown source kind or purpose")
        if not isinstance(self.channel, str) or not _CHANNEL.fullmatch(self.channel):
            raise ValueError("invalid channel")
        if not isinstance(self.source_digest, str) or not _DIGEST.fullmatch(self.source_digest):
            raise ValueError("source digest must be SHA-256")
        for name in ("delivered_bytes", "delivered_chars", "logical_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.logical_bytes < self.delivered_bytes:
            raise ValueError("logical bytes cannot be smaller than delivered bytes")
        if type(self.cache_hit) is not bool:
            raise ValueError("cache_hit must be boolean")
        if not isinstance(self.session_id, str) or not _SESSION.fullmatch(self.session_id):
            raise ValueError("session ID is invalid")
        if not isinstance(self.subject_digest, str) or not _DIGEST.fullmatch(
                self.subject_digest):
            raise ValueError("subject digest must be SHA-256")
        path = self.logical_path
        if (not isinstance(path, str) or len(path) > 300 or not path
                or path.startswith(("/", "\\")) or "\\" in path
                or any(ord(char) < 32 for char in path)
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise ValueError("logical source path is unsafe")
        if not isinstance(self.occurred_at, str) or not _TIMESTAMP.fullmatch(
                self.occurred_at):
            raise ValueError("event time must be UTC to the second")
        if not isinstance(self.note, str) or self.note not in _NOTES:
            raise ValueError("note must be a controlled, non-sensitive code")
        if type(self.duplicate_delivery) is not bool:
            raise ValueError("duplicate delivery flag must be boolean")


@dataclass(frozen=True)
class ContextEvent:
    schema_version: int
    sequence: int
    previous_hash: str
    route: str
    stage: str
    source_kind: str
    channel: str
    source_digest: str
    delivered_bytes: int
    delivered_chars: int
    logical_bytes: int
    purpose: str
    cache_hit: bool
    session_id: str
    subject_digest: str
    logical_path: str
    occurred_at: str
    note: str
    duplicate_delivery: bool
    event_id: str
    event_hash: str

    def as_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def next_event(item: ContextEventInput, previous: ContextEvent | None) -> ContextEvent:
    if previous and (previous.session_id != item.session_id
                     or previous.subject_digest != item.subject_digest):
        raise ValueError("event session or subject changed within one ledger")
    payload = {
        "schema_version": 2,
        "sequence": previous.sequence + 1 if previous else 1,
        "previous_hash": previous.event_hash if previous else _ZERO,
        **{name: getattr(item, name) for name in item.__dataclass_fields__},
    }
    payload["event_id"] = hashlib.sha256(_canonical({
        "session_id": item.session_id, "sequence": payload["sequence"],
        "logical_path": item.logical_path, "source_digest": item.source_digest,
    })).hexdigest()
    return ContextEvent(**payload,
                        event_hash=hashlib.sha256(_canonical(payload)).hexdigest())


def validate_events(events: tuple[ContextEvent, ...]) -> None:
    previous: ContextEvent | None = None
    for row in events:
        if not isinstance(row, ContextEvent) or row.schema_version != 2:
            raise ValueError("invalid event schema")
        item = ContextEventInput(**{
            name: getattr(row, name) for name in ContextEventInput.__dataclass_fields__
        })
        if row != next_event(item, previous):
            raise ValueError("event sequence or hash chain is invalid")
        previous = row


@dataclass(frozen=True)
class ContextReport:
    schema_version: int
    coverage: str
    delivered_bytes: int
    delivered_chars: int
    logical_bytes: int
    stream_digest: str
    observed_channels: tuple[str, ...]
    unobserved_channels: tuple[str, ...]
    by_source_kind: tuple[tuple[str, int, int, int], ...]
    findings: tuple[str, ...]


def build_context_report(
    events: tuple[ContextEvent, ...], *, coverage: str,
    expected_channels: tuple[str, ...], observed_channels: tuple[str, ...],
    bypass_detected: bool, delivered_byte_limit: int | None = None,
) -> ContextReport:
    if not isinstance(events, tuple):
        raise ValueError("events must be an immutable tuple")
    validate_events(events)
    if coverage not in {"complete", "partial"}:
        raise ValueError("unknown coverage mode")
    for name, values in (("expected", expected_channels), ("observed", observed_channels)):
        if (not isinstance(values, tuple) or len(values) != len(set(values))
                or any(not isinstance(value, str) or not _CHANNEL.fullmatch(value)
                       for value in values)):
            raise ValueError(f"invalid {name} channel catalog")
    if type(bypass_detected) is not bool:
        raise ValueError("bypass_detected must be boolean")
    if (delivered_byte_limit is not None and
            (type(delivered_byte_limit) is not int or delivered_byte_limit < 0)):
        raise ValueError("delivered byte limit must be non-negative")
    expected, observed = set(expected_channels), set(observed_channels)
    if events and (len({event.session_id for event in events}) != 1
                   or len({event.subject_digest for event in events}) != 1):
        raise ValueError("report contains mixed sessions or subjects")
    if any(event.channel not in observed for event in events):
        raise ValueError("event claims an unobserved channel")
    unobserved = tuple(sorted(expected - observed))
    if coverage == "complete" and (observed != expected or bypass_detected or not expected):
        raise ValueError("complete coverage needs the exact channel catalog and no bypass")
    delivered = sum(event.delivered_bytes for event in events)
    findings: list[str] = []
    if delivered_byte_limit is not None and delivered > delivered_byte_limit:
        findings.append("CONTEXT_OVER_BUDGET" if coverage == "complete"
                        else "OBSERVED_CONTEXT_OVER_BUDGET")
    if bypass_detected:
        findings.append("BYPASS_DETECTED")
    kinds = tuple(sorted({event.source_kind for event in events}))
    return ContextReport(
        schema_version=2, coverage=coverage, delivered_bytes=delivered,
        delivered_chars=sum(event.delivered_chars for event in events),
        logical_bytes=sum(event.logical_bytes for event in events),
        stream_digest=events[-1].event_hash if events else _ZERO,
        observed_channels=tuple(sorted(observed)),
        unobserved_channels=unobserved,
        by_source_kind=tuple((kind,
                              sum(event.delivered_bytes for event in events
                                  if event.source_kind == kind),
                              sum(event.delivered_chars for event in events
                                  if event.source_kind == kind),
                              sum(event.source_kind == kind for event in events))
                             for kind in kinds),
        findings=tuple(findings),
    )
