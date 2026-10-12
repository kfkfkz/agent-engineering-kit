"""Opaque capabilities. Serialized facts and checksums never confer authority."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math


class SecurityAuthorityError(ValueError):
    def __init__(self, reason: str = "SECURITY_AUTHORITY_INVALID") -> None:
        super().__init__(reason)


class CallerHandle:
    __slots__ = ()

    def __new__(cls):
        raise SecurityAuthorityError()


class RuntimeGrant(CallerHandle):
    __slots__ = ()


@dataclass(frozen=True)
class HostEvent:
    """Only HostEventSource may attest this data through its live channel."""
    event_id: str
    kind: str
    actor: str
    task_id: str
    repository_ref: str
    scope_ref: str
    identity_digest: str
    operation: str
    binding_digest: str
    occurred_at: str
    expires_at: str


def canonical(value: object) -> bytes:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise SecurityAuthorityError() from None
    if len(raw) > 2_000_000:
        raise SecurityAuthorityError()
    return raw


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, AttributeError, ValueError):
        raise SecurityAuthorityError() from None
    if parsed.tzinfo is None:
        raise SecurityAuthorityError()
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def runtime_binding(**values) -> dict:
    """Pure canonical execution contract; capability validation remains external."""
    values = dict(values)
    values["write_scope"] = list(values.get("write_scope", ()))
    values["operation"] = "execute"
    duration = values.get("timeout_seconds")
    if type(duration) not in {int, float} or not math.isfinite(duration) or not 0 < duration <= 300:
        raise SecurityAuthorityError()
    for key, limit in (("max_output_bytes", 2_000_000), ("max_items", 10_000)):
        if type(values.get(key)) is not int or not 0 < values[key] <= limit:
            raise SecurityAuthorityError()
    return values
