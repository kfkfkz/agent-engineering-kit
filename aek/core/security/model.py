"""Strict security data boundary, independent of host and execution authority."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from aek.core.context.identity import IdentityEnvelope, decode_identity

MAX_SECURITY_BYTES = 2_000_000
MAX_SECURITY_ITEMS = 10_000
MAX_SECURITY_STRING = 2048
_COMMON_KEYS = {"schema_version", "operation", "stage", "scope", "identity"}
_VARIANT_KEYS = {
    "evaluate": {"proposals", "surfaces", "findings", "inventories",
                 "run_refs", "invariant_refs"},
    "report": {"record_ref", "expected_revision_digest", "format"},
    "doctor": {"tool_ids"},
    "plan": {"tool_id", "input_refs"},
}
_TOOLS = {"trivy", "semgrep", "gitleaks"}
_COVERAGE = {"COMPLETE_FOR_DECLARED_SCOPE", "PARTIAL", "UNKNOWN"}
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "UNKNOWN"}
_VERIFICATION = {"SUSPECTED", "STATIC_VERIFIED", "DYNAMIC_CONFIRMED",
                 "NOT_REPRODUCIBLE", "FALSE_POSITIVE", "INCONCLUSIVE"}
_REACHABILITY = {"UNKNOWN", "NOT_OBSERVED", "IMPORTED", "SYMBOL_USED", "VERIFIED_CALL_PATH"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SecurityRequest:
    """Immutable input snapshot; its identity is a declaration, not a proof."""

    operation: str
    stage: str
    identity: IdentityEnvelope
    payload_json: str


class SecurityInputError(ValueError):
    """Safe diagnostic: never include untrusted values or parser excerpts."""

    def __init__(self) -> None:
        super().__init__("INVALID_SECURITY_INPUT")


def _exact_object(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise SecurityInputError()
    return value


def _text(value: object) -> str:
    if (type(value) is not str or not value.strip()
            or len(value) > MAX_SECURITY_STRING
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise SecurityInputError()
    return value


def _digest(value: object) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise SecurityInputError()
    return value


def _path(value: object) -> str:
    value = _text(value)
    if ("\\" in value or ":" in value
            or any(part in {"", ".", ".."} or part.endswith((".", " "))
                   for part in value.split("/"))):
        raise SecurityInputError()
    return value


def _strings(value: object, *, paths: bool = False) -> tuple[str, ...]:
    if type(value) is not list:
        raise SecurityInputError()
    result = tuple((_path if paths else _text)(item) for item in value)
    if len(set(result)) != len(result):
        raise SecurityInputError()
    return result


def _scope(value: object) -> None:
    scope = _exact_object(value, {"mode", "subject_digest", "paths", "focus"})
    if _text(scope["mode"]) not in {"targeted", "focused", "full"}:
        raise SecurityInputError()
    _digest(scope["subject_digest"])
    _strings(scope["paths"], paths=True)
    _strings(scope["focus"])


def _scope_identity_binding(scope: dict, identity: IdentityEnvelope) -> None:
    subject = dict(dict(identity.components)["subject"])
    if "scope_digest" in subject and subject["scope_digest"] != scope["subject_digest"]:
        raise SecurityInputError()


def _rows(value: object, keys: set[str]) -> tuple[dict[str, object], ...]:
    if type(value) is not list:
        raise SecurityInputError()
    rows = tuple(_exact_object(row, keys) for row in value)
    ids = tuple(_text(row["id"]) for row in rows)
    if len(set(ids)) != len(ids):
        raise SecurityInputError()
    return rows


def _proposals(value: object) -> None:
    for row in _rows(value, {"id", "ecosystem", "package", "version", "purpose",
                             "scope", "design_refs", "evidence_refs"}):
        for key in ("ecosystem", "package", "purpose"):
            _text(row[key])
        if row["version"] is not None:
            _text(row["version"])
        if _text(row["scope"]) not in {"production", "test", "provided", "build"}:
            raise SecurityInputError()
        _strings(row["design_refs"])
        _strings(row["evidence_refs"])


def _coverage(row: dict[str, object]) -> None:
    if _text(row["coverage"]) not in _COVERAGE:
        raise SecurityInputError()
    _strings(row["coverage_gaps"])


def _surfaces(value: object) -> None:
    for row in _rows(value, {"id", "entry_ref", "actors", "controllable_inputs",
                             "boundaries", "sink_refs", "controls", "evidence_refs",
                             "coverage", "coverage_gaps"}):
        _text(row["entry_ref"])
        for key in ("actors", "controllable_inputs", "boundaries", "sink_refs", "evidence_refs"):
            _strings(row[key])
        _coverage(row)
        if type(row["controls"]) is not list:
            raise SecurityInputError()
        for value in row["controls"]:
            control = _exact_object(value, {"kind", "status", "evidence_refs"})
            _text(control["kind"])
            status = _text(control["status"])
            refs = _strings(control["evidence_refs"])
            if (status not in {"OBSERVED", "ABSENT_WITH_EVIDENCE", "UNKNOWN"}
                    or (status != "UNKNOWN" and not refs)):
                raise SecurityInputError()


def _inventories(value: object) -> None:
    for row in _rows(value, {"id", "kind", "scope_ref", "identity_digest",
                             "producer_ref", "packages", "coverage", "coverage_gaps"}):
        if _text(row["kind"]) not in {"PROPOSED", "RESOLVED", "SHIPPED"}:
            raise SecurityInputError()
        _digest(row["identity_digest"])
        _text(row["scope_ref"])
        _text(row["producer_ref"])
        _coverage(row)
        for package in _rows(row["packages"], {
                "id", "ecosystem", "package", "version", "scope", "relationship",
                "introduced_by", "dependency_path", "source_ref"}):
            for key in ("ecosystem", "package", "source_ref"):
                _text(package[key])
            if package["version"] is not None:
                _text(package["version"])
            if _text(package["scope"]) not in {"production", "test", "provided", "build"}:
                raise SecurityInputError()
            if _text(package["relationship"]) not in {"direct", "transitive", "UNKNOWN"}:
                raise SecurityInputError()
            _strings(package["introduced_by"])
            _strings(package["dependency_path"])


def _findings(value: object) -> None:
    for row in _rows(value, {"id", "category", "severity", "verification_status", "title",
                             "location_refs", "surface_refs", "dependency_ref", "advisory_refs",
                             "evidence_refs", "counterevidence_refs", "preconditions", "impact",
                             "required_change", "affected_refs", "reachability", "coverage_gaps"}):
        for key in ("category", "title", "impact", "required_change"):
            _text(row[key])
        if (_text(row["severity"]) not in _SEVERITIES
                or _text(row["verification_status"]) not in _VERIFICATION
                or _text(row["reachability"]) not in _REACHABILITY):
            raise SecurityInputError()
        if row["dependency_ref"] is not None:
            _text(row["dependency_ref"])
        for key in ("location_refs", "surface_refs", "advisory_refs", "evidence_refs",
                    "counterevidence_refs", "preconditions", "affected_refs", "coverage_gaps"):
            _strings(row[key])
        if row["reachability"] != "UNKNOWN" and not row["evidence_refs"]:
            raise SecurityInputError()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SecurityInputError()
        result[key] = value
    return result


def decode_security_json(raw: bytes) -> object:
    """Decode data without last-key-wins ambiguity, at every object depth."""
    if type(raw) is not bytes or len(raw) > MAX_SECURITY_BYTES:
        raise SecurityInputError()
    items = 0
    def reject_constant(_value: str) -> object:
        raise SecurityInputError()

    def check(value: object) -> None:
        nonlocal items
        if isinstance(value, str):
            if len(value) > MAX_SECURITY_STRING:
                raise SecurityInputError()
            value.encode("utf-8", errors="strict")
        elif isinstance(value, float) and not math.isfinite(value):
            raise SecurityInputError()
        elif isinstance(value, dict):
            for key, item in value.items():
                check(key)
                check(item)
        elif isinstance(value, list):
            items += len(value)
            if items > MAX_SECURITY_ITEMS:
                raise SecurityInputError()
            for item in value:
                check(item)

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=_unique_object,
                           parse_constant=reject_constant)
        check(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise SecurityInputError() from None


def decode_security_request(raw: bytes) -> SecurityRequest:
    payload = decode_security_json(raw)
    if type(payload) is not dict:
        raise SecurityInputError()
    operation = payload.get("operation")
    stage = payload.get("stage")
    if (type(operation) is not str or operation not in _VARIANT_KEYS
            or set(payload) != _COMMON_KEYS | _VARIANT_KEYS[operation]
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or type(stage) is not str
            or stage not in {"design", "execution", "closeout"}):
        raise SecurityInputError()
    try:
        identity = decode_identity(payload["identity"])
    except (TypeError, ValueError):
        raise SecurityInputError() from None
    _scope(payload["scope"])
    _scope_identity_binding(payload["scope"], identity)
    if operation == "evaluate":
        if any(type(payload[key]) is not list for key in _VARIANT_KEYS[operation]):
            raise SecurityInputError()
        _proposals(payload["proposals"])
        _surfaces(payload["surfaces"])
        _inventories(payload["inventories"])
        _findings(payload["findings"])
        _strings(payload["run_refs"])
        _strings(payload["invariant_refs"])
    elif operation == "report":
        _path(payload["record_ref"])
        _digest(payload["expected_revision_digest"])
        if type(payload["format"]) is not str or payload["format"] not in {"json", "md"}:
            raise SecurityInputError()
    elif operation == "doctor":
        tools = payload["tool_ids"]
        if (type(tools) is not list
                or any(type(tool) is not str or tool not in _TOOLS for tool in tools)
                or len(set(tools)) != len(tools)):
            raise SecurityInputError()
    else:
        if type(payload["tool_id"]) is not str or payload["tool_id"] not in _TOOLS:
            raise SecurityInputError()
        _strings(payload["input_refs"])
    return SecurityRequest(operation, stage, identity,
                           json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False))
