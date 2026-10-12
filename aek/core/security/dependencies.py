"""Pure dependency decisions over already provenance-checked observations.

The application obtains observations from its controlled SourceEvidencePort.
This module never reads files, resolves version ranges or grants authority.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import hashlib
import ipaddress
import re

from aek.core.context.identity import IdentityEnvelope, IdentityState, compare_identity, decode_identity
from aek.core.security.model import SecurityInputError, _digest, _exact_object, _proposals, _strings, _text
from aek.core.security.record import _sources, _timestamp, canonical_bytes


def dependency_evidence_digest(observations: tuple[dict, ...]) -> str:
    materials = [{"ref": item["metadata"]["ref"], "kind": item["metadata"]["kind"],
                  "identity_digest": item["metadata"]["identity"]["envelope_digest"],
                  "rules_digest": item["metadata"]["rules_digest"],
                  "database_digest": item["metadata"]["database_digest"], "facts": item["facts"]}
                 for item in observations]
    return hashlib.sha256(canonical_bytes(sorted(materials, key=lambda row: row["ref"]))).hexdigest()


def decode_advisories(value: object) -> tuple[dict, ...]:
    if type(value) is not list:
        raise SecurityInputError()
    rows, identifiers = [], set()
    for row in value:
        row = _exact_object(row, {"canonical_id", "aliases", "source_url", "affected_range", "fixed_versions",
                                  "checked_at", "source_digest", "match_status"})
        identifier = _text(row["canonical_id"])
        if identifier in identifiers:
            raise SecurityInputError()
        identifiers.add(identifier)
        _strings(row["aliases"])
        _strings(row["fixed_versions"])
        _text(row["affected_range"])
        _digest(row["source_digest"])
        _timestamp(row["checked_at"])
        if _text(row["match_status"]) not in {"AFFECTED", "UNAFFECTED", "UNKNOWN"}:
            raise SecurityInputError()
        if not _https_reference(_text(row["source_url"])):
            raise SecurityInputError()
        rows.append(row)
    return tuple(rows)


def _https_reference(value: str) -> bool:
    """Validate a conservative reference grammar, never resolve or fetch it.

    Authority credentials, backslashes and whitespace are not evidence URLs.
    IDNs must use their ASCII/punycode form; IPv6 literals use brackets.
    """
    if any(char.isspace() for char in value) or "\\" in value:
        return False
    match = re.fullmatch(r"https://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::([0-9]{1,5}))?([/?#].*)?", value)
    if match is None:
        return False
    host, port, _ = match.groups()
    if port is not None and not 1 <= int(port) <= 65535:
        return False
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return False
    elif len(host) > 253 or any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                              for label in (host[:-1] if host.endswith(".") else host).split(".")):
        return False
    return True


def evaluate_design_dependency(proposal: dict, observations: tuple[dict, ...], *,
                               current_identity: IdentityEnvelope | None,
                               required_components: tuple[str, ...], scope_ref: str,
                               now: datetime, max_age_days: int = 7,
                               unresolved_refs: tuple[str, ...] = (),
                               accepted_risk_ref: str | None = None) -> dict:
    _proposals([proposal])
    reasons = ["SOURCE_EVIDENCE_UNAVAILABLE"]
    if proposal["version"] is None:
        reasons.append("VERSION_UNKNOWN")
    decision = {"dependency_ref": proposal["id"], "proposal": proposal,
                "status": "PENDING", "reason_codes": reasons, "source_refs": [],
                "pending_delivery_requirements": ["RESOLVED_INVENTORY_REQUIRED", "SHIPPED_INVENTORY_IF_APPLICABLE"]}
    if current_identity is None or proposal["version"] is None:
        return decision
    candidates = {}
    observation_gaps = []
    package_keys = ("ecosystem", "package", "version")
    for observation in observations:
        metadata, facts = observation["metadata"], observation["facts"]
        try:
            _sources([metadata])
            checked = _timestamp(metadata["checked_at"])
            valid = (metadata["status"] == "VERIFIED" and metadata["scope_ref"] == scope_ref
                     and metadata["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE" and not metadata["coverage_gaps"]
                     and checked <= now and now - checked <= timedelta(days=max_age_days)
                     and compare_identity(decode_identity(metadata["identity"]), current_identity,
                                          required_components).state == IdentityState.VALID
                     and bool(observation["provenance_ref"]))
        except (TypeError, ValueError, KeyError):
            observation_gaps.append("EVIDENCE_INVALID")
            continue
        if not valid:
            observation_gaps.append("EVIDENCE_INCOMPLETE")
        if (valid and type(facts) is dict
                and all(facts.get(key) == proposal[key] for key in package_keys)):
            candidates.setdefault(metadata["kind"], []).append(observation)
    advisories = []
    affected = False
    advisory_gaps = []
    for item in candidates.get("advisory", []):
        if set(item["facts"]) != {*package_keys, "advisories"}:
            continue
        try:
            rows = decode_advisories(item["facts"]["advisories"])
        except ValueError:
            advisory_gaps.append("ADVISORY_INVALID")
            continue
        if any(_timestamp(row["checked_at"]) > now
               or now - _timestamp(row["checked_at"]) > timedelta(days=max_age_days) for row in rows):
            advisory_gaps.append("ADVISORY_STALE")
            continue
        if any(row["match_status"] == "UNKNOWN" for row in rows):
            advisory_gaps.append("ADVISORY_MATCH_UNKNOWN")
            continue
        advisories.append(item)
        affected = affected or any(row["match_status"] == "AFFECTED" for row in rows)
    compatibility = next((item for item in candidates.get("compatibility", [])
                          if set(item["facts"]) == {*package_keys, "compatible"}
                          and item["facts"]["compatible"] is True), None)
    incompatible = next((item for item in candidates.get("compatibility", [])
                         if set(item["facts"]) == {*package_keys, "compatible"}
                         and item["facts"]["compatible"] is False), None)
    if incompatible is not None:
        decision.update(status="REJECTED", reason_codes=["DEPENDENCY_INCOMPATIBLE"],
                        source_refs=[incompatible["metadata"]["ref"]])
        return decision
    decision["source_refs"] = [item["metadata"]["ref"] for item in advisories]
    if compatibility is not None:
        decision["source_refs"].append(compatibility["metadata"]["ref"])
    if affected:
        decision["reason_codes"] = ["KNOWN_AFFECTED_DEPENDENCY", "ADOPTION_PENDING"]
        if accepted_risk_ref is not None and compatibility is not None and not (
                unresolved_refs or advisory_gaps or observation_gaps):
            decision.update(status="ADOPTABLE", reason_codes=["KNOWN_AFFECTED_DEPENDENCY", "ACCEPTED_RISK"])
            decision["source_refs"].append(accepted_risk_ref)
            decision["pending_delivery_requirements"].append("RISK_DISPOSITION_REVALIDATION_REQUIRED")
        return decision
    if unresolved_refs or advisory_gaps or observation_gaps:
        decision["reason_codes"] = sorted(set(advisory_gaps + observation_gaps
                                              + (["UNRESOLVED_REFERENCE"] if unresolved_refs else [])))
        return decision
    if advisories and compatibility is not None:
        decision.update(status="ADOPTABLE", reason_codes=[])
    elif not advisories:
        decision["reason_codes"].append("ADVISORY_REQUIRED")
    return decision
