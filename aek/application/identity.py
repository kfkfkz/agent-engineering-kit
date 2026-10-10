"""Bind machine receipts to composable identity envelopes."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from aek.core.context.identity import (
    IdentityComparison,
    IdentityEnvelope,
    IdentityState,
    compare_identity,
    decode_identity,
    identity_as_dict,
)


@dataclass(frozen=True)
class IdentityBoundReceipt:
    schema_version: int
    identity: IdentityEnvelope
    payload_json: str
    payload_digest: str
    receipt_digest: str


def _canonical_payload(payload: Mapping[str, object]) -> str:
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("receipt payload must be a non-empty mapping")
    try:
        encoded = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt payload must be canonical JSON") from exc
    if not isinstance(decoded, dict) or any(
            not isinstance(key, str) or not key for key in decoded):
        raise ValueError("receipt payload keys must be non-empty strings")
    return encoded


def _receipt_digest(identity_digest: str, payload_digest: str) -> str:
    return hashlib.sha256(
        b"aek-identity-bound-receipt-v1\0"
        + identity_digest.encode("ascii")
        + b"\0"
        + payload_digest.encode("ascii")
    ).hexdigest()


def bind_receipt(
    identity: IdentityEnvelope,
    payload: Mapping[str, object],
) -> IdentityBoundReceipt:
    identity_as_dict(identity)
    payload_json = _canonical_payload(payload)
    payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return IdentityBoundReceipt(
        1,
        identity,
        payload_json,
        payload_digest,
        _receipt_digest(identity.envelope_digest, payload_digest),
    )


def bound_receipt_as_dict(receipt: IdentityBoundReceipt) -> dict[str, object]:
    if not _receipt_is_authentic(receipt):
        raise ValueError("identity-bound receipt is invalid")
    return {
        "schema_version": receipt.schema_version,
        "identity_digest": receipt.identity.envelope_digest,
        "identity": identity_as_dict(receipt.identity),
        "payload": json.loads(receipt.payload_json),
        "payload_digest": receipt.payload_digest,
        "receipt_digest": receipt.receipt_digest,
    }


def decode_bound_receipt(payload: Mapping[str, object]) -> IdentityBoundReceipt:
    required = {
        "schema_version", "identity_digest", "identity", "payload",
        "payload_digest", "receipt_digest",
    }
    if (not isinstance(payload, Mapping) or set(payload) != required
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("identity"), Mapping)
            or not isinstance(payload.get("payload"), Mapping)
            or not all(isinstance(payload.get(key), str) for key in (
                "identity_digest", "payload_digest", "receipt_digest"))):
        raise ValueError("identity-bound receipt schema is invalid")
    try:
        identity = decode_identity(payload["identity"])  # type: ignore[arg-type]
        receipt = bind_receipt(
            identity, payload["payload"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("identity-bound receipt content is invalid") from exc
    if (payload["identity_digest"] != identity.envelope_digest
            or payload["payload_digest"] != receipt.payload_digest
            or payload["receipt_digest"] != receipt.receipt_digest):
        raise ValueError("identity-bound receipt digest mismatch")
    return receipt


def _receipt_is_authentic(receipt: object) -> bool:
    if (not isinstance(receipt, IdentityBoundReceipt) or type(receipt.schema_version) is not int
            or receipt.schema_version != 1):
        return False
    try:
        identity_as_dict(receipt.identity)
        decoded = json.loads(receipt.payload_json)
        canonical = _canonical_payload(decoded)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    payload_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (
        canonical == receipt.payload_json
        and payload_digest == receipt.payload_digest
        and receipt.receipt_digest == _receipt_digest(
            receipt.identity.envelope_digest, payload_digest)
    )


def verify_bound_receipt(
    receipt: IdentityBoundReceipt,
    current_identity: IdentityEnvelope,
    required_components: tuple[str, ...],
) -> IdentityComparison:
    if not _receipt_is_authentic(receipt):
        return IdentityComparison(
            IdentityState.INVALID,
            reason_codes=("RECEIPT_CONTENT_INVALID",),
        )
    return compare_identity(
        receipt.identity, current_identity, required_components)
