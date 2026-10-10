"""Immutable, scoped CLI observations; never an independent correctness verdict."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


@dataclass(frozen=True)
class CaptureContext:
    run_id: str
    pair_id: str
    task_id: str
    manifest_sha256: str
    capture_complete: bool = False
    session_mode: str = "unknown"

    def __post_init__(self):
        if (any(not isinstance(value, str) or not _ID.fullmatch(value)
                for value in (self.run_id, self.pair_id, self.task_id))
                or not isinstance(self.manifest_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", self.manifest_sha256)
                or type(self.capture_complete) is not bool
                or not isinstance(self.session_mode, str)
                or self.session_mode not in {"fresh", "resumed", "unknown"}):
            raise ValueError("INVALID_CAPTURE_CONTEXT")


@dataclass(frozen=True)
class Measurement:
    value: int | float | None
    source: str
    is_estimate: bool
    coverage: str
    scope: str


@dataclass(frozen=True)
class NormalizedEvent:
    sequence: int
    event_id: str
    run_id: str
    pair_id: str
    task_id: str
    source: str
    kind: str
    raw_sha256: str
    tool: str | None = None
    stage: str | None = None
    timestamp: str | None = None
    coverage: str = "CLI_ENVELOPE_ONLY"


@dataclass(frozen=True)
class StreamSummary:
    agent: str
    status: str
    reason_codes: tuple[str, ...]
    stream_sha256: str
    native_session_sha256: str | None
    manifest_sha256: str
    events: tuple[NormalizedEvent, ...]
    reported_usage: tuple[tuple[str, Measurement], ...]
    run_usage: tuple[tuple[str, Measurement], ...]
    session_mode: str
    capture_complete: bool
    execution_scope: str = "posthoc_cli_transcript"
    outcome: str = "NOT_EVALUATED"

    def to_dict(self) -> dict:
        value = asdict(self)
        value["reported_usage"] = {key: asdict(metric) for key, metric in self.reported_usage}
        value["run_usage"] = {key: asdict(metric) for key, metric in self.run_usage}
        return value
