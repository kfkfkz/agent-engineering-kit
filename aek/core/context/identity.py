"""Composable identity envelopes for selective cache and evidence invalidation.

An envelope combines digests that remain authoritative in their own domains.
Consumers name the exact components they require; the envelope does not turn
those independent lifecycles into a new global source of truth.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAMESPACES = frozenset({"subject", "artifact", "policy", "context", "evidence"})


class IdentityState(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class IdentityEnvelope:
    schema_version: int
    components: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    envelope_digest: str


@dataclass(frozen=True)
class IdentityComparison:
    state: IdentityState
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


def _normalize_components(
    components: Mapping[str, Mapping[str, str]],
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if not isinstance(components, Mapping) or set(components) != _NAMESPACES:
        raise ValueError("identity needs exactly the supported namespaces")
    normalized: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for namespace in sorted(components):
        values = components[namespace]
        if not isinstance(values, Mapping) or not values:
            raise ValueError("identity namespace must be a non-empty mapping")
        rows: list[tuple[str, str]] = []
        for key, digest in values.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise ValueError("identity component key is invalid")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("identity component digest must be SHA-256")
            rows.append((key, digest))
        normalized.append((namespace, tuple(sorted(rows))))
    return tuple(normalized)


def _payload(
    components: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "components": {
            namespace: {key: digest for key, digest in values}
            for namespace, values in components
        },
    }


def _digest(
    components: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> str:
    canonical = json.dumps(
        _payload(components), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_identity(
    components: Mapping[str, Mapping[str, str]],
) -> IdentityEnvelope:
    normalized = _normalize_components(components)
    return IdentityEnvelope(1, normalized, _digest(normalized))


def identity_as_dict(envelope: IdentityEnvelope) -> dict[str, object]:
    if not _is_authentic(envelope):
        raise ValueError("identity envelope is invalid")
    payload = _payload(envelope.components)
    payload["envelope_digest"] = envelope.envelope_digest
    return payload


def decode_identity(payload: Mapping[str, object]) -> IdentityEnvelope:
    if (not isinstance(payload, Mapping)
            or set(payload) != {
                "schema_version", "components", "envelope_digest"}
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("components"), Mapping)
            or not isinstance(payload.get("envelope_digest"), str)):
        raise ValueError("identity envelope schema is invalid")
    try:
        envelope = build_identity(payload["components"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("identity envelope components are invalid") from exc
    if envelope.envelope_digest != payload["envelope_digest"]:
        raise ValueError("identity envelope digest mismatch")
    return envelope


def _is_authentic(envelope: object) -> bool:
    if (not isinstance(envelope, IdentityEnvelope)
            or envelope.schema_version != 1
            or not isinstance(envelope.components, tuple)
            or not isinstance(envelope.envelope_digest, str)
            or not _DIGEST.fullmatch(envelope.envelope_digest)):
        return False
    try:
        rebuilt = build_identity({
            namespace: dict(values)
            for namespace, values in envelope.components
        })
    except (TypeError, ValueError):
        return False
    return rebuilt == envelope


def _component_map(envelope: IdentityEnvelope) -> dict[str, str]:
    return {
        f"{namespace}.{key}": digest
        for namespace, values in envelope.components
        for key, digest in values
    }


def compare_identity(
    expected: IdentityEnvelope,
    current: IdentityEnvelope,
    required_components: tuple[str, ...],
) -> IdentityComparison:
    if not _is_authentic(expected) or not _is_authentic(current):
        return IdentityComparison(
            IdentityState.INVALID,
            reason_codes=("IDENTITY_DIGEST_INVALID",),
        )
    if (not isinstance(required_components, tuple) or not required_components
            or len(set(required_components)) != len(required_components)
            or any(not isinstance(item, str)
                   or item.count(".") != 1
                   or item.split(".", 1)[0] not in _NAMESPACES
                   or not _KEY.fullmatch(item.split(".", 1)[1])
                   for item in required_components)):
        return IdentityComparison(
            IdentityState.INVALID,
            reason_codes=("IDENTITY_REQUIRED_COMPONENT_INVALID",),
        )
    expected_map = _component_map(expected)
    current_map = _component_map(current)
    if any(item not in expected_map for item in required_components):
        return IdentityComparison(
            IdentityState.INVALID,
            reason_codes=("IDENTITY_EXPECTED_COMPONENT_MISSING",),
        )
    missing = tuple(sorted(
        item for item in required_components if item not in current_map))
    changed = tuple(sorted(
        item for item in required_components
        if item in current_map and expected_map[item] != current_map[item]))
    if not missing and not changed:
        return IdentityComparison(IdentityState.VALID)
    reasons: list[str] = []
    if changed:
        reasons.append("IDENTITY_COMPONENT_CHANGED")
    if missing:
        reasons.append("IDENTITY_COMPONENT_MISSING")
    return IdentityComparison(
        IdentityState.STALE, changed, missing, tuple(reasons))
