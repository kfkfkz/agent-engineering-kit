"""Bounded, data-only UI observations; no filesystem, model calls or approval.

The caller must obtain the actual source and contract through its authorized
adapter. These pure checks bind data to that snapshot; they do not authenticate
the caller, establish applicability or manufacture a maintainer decision.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re


MAX_OBSERVATION_BYTES = 64 * 1024
MAX_REGIONS = 32
MAX_CONTROLS = 64
SEMANTIC_VERSION = "ui-observation-v1"
_ID = re.compile(r"[A-Z][A-Z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ObservationError(ValueError):
    """Invalid untrusted observation; messages intentionally omit its body."""


def _object(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ObservationError("invalid observation fields")
    return value


def _text(value: object, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum or "\0" in value:
        raise ObservationError("invalid observation text")
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ObservationError("invalid region or evidence identity")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ObservationError("invalid observation source digest")
    return value


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ObservationError("observation is not bounded UTF-8 JSON") from exc


def decode_candidate(raw: bytes) -> dict:
    """Parse bounded data only; build_observation still validates its meaning."""
    if type(raw) is not bytes or len(raw) > MAX_OBSERVATION_BYTES:
        raise ObservationError("observation input exceeds byte limit")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ObservationError("duplicate observation field")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ObservationError("non-finite observation JSON")

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ObservationError("invalid observation JSON") from None
    return _object(value, {"schema_version", "regions", "uncertainties"})


def build_observation(source: dict, contract: dict, required_regions: tuple[str, ...],
                      candidate: object, *, provenance: dict) -> dict:
    """Derive coverage and source binding; model output cannot supply either."""
    _object(source, {"scope_digest", "evidence_id", "image_sha256"})
    _digest(source["scope_digest"])
    _identifier(source["evidence_id"])
    _digest(source["image_sha256"])
    _object(contract, {"schema_version", "semantic_version", "prompt_sha256"})
    if (type(contract["schema_version"]) is not int or contract["schema_version"] != 2
            or contract["semantic_version"] != SEMANTIC_VERSION):
        raise ObservationError("unsupported extraction contract")
    _digest(contract["prompt_sha256"])
    if (type(required_regions) is not tuple or not 1 <= len(required_regions) <= MAX_REGIONS
            or len(set(_identifier(item) for item in required_regions)) != len(required_regions)):
        raise ObservationError("invalid required regions")
    value = _object(candidate, {"schema_version", "regions", "uncertainties"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ObservationError("unsupported observation schema")
    if type(value["regions"]) is not list or not 1 <= len(value["regions"]) <= MAX_REGIONS:
        raise ObservationError("invalid observation region count")
    observed, seen = set(), set()
    for region in value["regions"]:
        region = _object(region, {"region_id", "observation_kind", "layout", "controls", "style_clues"})
        key = _identifier(region["region_id"])
        if key not in required_regions:
            raise ObservationError("observation region is outside the requested scope")
        if key in seen:
            raise ObservationError("duplicate observation region")
        seen.add(key)
        if region["observation_kind"] not in ("observed", "inferred", "unknown"):
            raise ObservationError("invalid observation certainty")
        if region["observation_kind"] == "observed":
            observed.add(key)
        _text(region["layout"], 1024)
        if type(region["controls"]) is not list or len(region["controls"]) > MAX_CONTROLS:
            raise ObservationError("invalid observation control count")
        for control in region["controls"]:
            control = _object(control, {"kind", "label", "state"})
            _text(control["kind"], 80)
            _text(control["label"], 160)
            _text(control["state"], 80)
        if type(region["style_clues"]) is not list or len(region["style_clues"]) > 16:
            raise ObservationError("invalid style clue count")
        for clue in region["style_clues"]:
            _text(clue, 160)
    if type(value["uncertainties"]) is not list or len(value["uncertainties"]) > 32:
        raise ObservationError("invalid observation uncertainty count")
    for item in value["uncertainties"]:
        _text(item, 512)
    provenance_fields = {"channel", "model", "observed_at"}
    if type(provenance) is dict and "prepare_sha256" in provenance:
        provenance_fields.add("prepare_sha256")
        _digest(provenance["prepare_sha256"])
    _object(provenance, provenance_fields)
    if provenance["channel"] not in ("agent_visual", "maintainer_visual"):
        raise ObservationError("unknown observation channel")
    if provenance["model"] is not None:
        _text(provenance["model"], 120)
    timestamp = _text(provenance["observed_at"], 80)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationError("invalid observation timestamp") from exc
    if parsed.tzinfo is None:
        raise ObservationError("observation timestamp requires timezone")
    record = {"schema_version": 2, "evidence_scope": source, "extraction_contract": contract,
              "regions": value["regions"], "uncertainties": value["uncertainties"],
              "coverage": {"required_regions": sorted(required_regions),
                           "observed_regions": sorted(observed),
                           "missing_regions": sorted(set(required_regions) - observed)},
              "provenance": provenance}
    raw = canonical_bytes(record)
    if len(raw) > MAX_OBSERVATION_BYTES:
        raise ObservationError("observation exceeds byte limit")
    # Detach all mutable candidate/source inputs from the returned snapshot.
    return json.loads(raw.decode("utf-8"))


def validate_observation(value: object) -> dict:
    """Recompute derived coverage; a saved `complete` flag is not evidence."""
    record = _object(value, {"schema_version", "evidence_scope", "extraction_contract",
                             "regions", "uncertainties", "coverage", "provenance"})
    if type(record["schema_version"]) is not int or record["schema_version"] != 2:
        raise ObservationError("unsupported saved observation schema")
    coverage = _object(record["coverage"], {"required_regions", "observed_regions", "missing_regions"})
    for values in coverage.values():
        if type(values) is not list or len(values) > MAX_REGIONS:
            raise ObservationError("invalid saved observation coverage")
        for item in values:
            _identifier(item)
    rebuilt = build_observation(record["evidence_scope"], record["extraction_contract"],
        tuple(coverage["required_regions"]), {"schema_version": 2, "regions": record["regions"],
            "uncertainties": record["uncertainties"]}, provenance=record["provenance"])
    if canonical_bytes(record) != canonical_bytes(rebuilt):
        raise ObservationError("saved observation coverage does not match its content")
    return rebuilt


def can_reuse(value: object, source: dict, contract: dict, required_regions: tuple[str, ...], *,
              expected_observation_sha256: str) -> bool:
    """Content sufficiency only; caller must separately recheck authorization.

    The expected payload digest must come from the current validated binding,
    not from the candidate being checked. Permissions and applicability are not cached in
    an image interpretation. A cache hit never bypasses those current checks.
    """
    record = validate_observation(value)
    _digest(expected_observation_sha256)
    if hashlib.sha256(canonical_bytes(record)).hexdigest() != expected_observation_sha256:
        return False
    if (type(required_regions) is not tuple or not 1 <= len(required_regions) <= MAX_REGIONS
            or len(set(_identifier(item) for item in required_regions)) != len(required_regions)):
        raise ObservationError("invalid required regions")
    return (record["evidence_scope"] == source and record["extraction_contract"] == contract
            and set(required_regions).issubset(record["coverage"]["observed_regions"]))
