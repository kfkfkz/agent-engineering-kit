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
from aek.core.security.model import SecurityInputError, _digest, _exact_object, _inventories, _proposals, _strings, _text
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


def is_concrete_version(value: object) -> bool:
    """A concrete spelling is not proof that a resolver actually selected it."""
    return (type(value) is str and bool(value) and len(value) <= 2048
            and not any(char.isspace() or char in "[](),${}" for char in value)
            and value.upper() not in {"LATEST", "RELEASE"})


def resolve_dynamic_dependency(proposal: dict, observations: tuple[dict, ...], *,
                               current_identity: IdentityEnvelope, required_components: tuple[str, ...],
                               scope_ref: str, now: datetime, max_age_days: int = 7) -> dict:
    """Select exact producer facts, never evaluate Maven ranges or properties.

    The original declaration remains in the proposal. A resolved version is
    usable only for this declaration's attested module/scope occurrences; it
    still requires independent advisories and compatibility for that version.
    """
    _proposals([proposal])
    pending = {"resolved_version": None, "source_refs": [], "reason_codes": ["DETERMINISTIC_RESOLUTION_REQUIRED"]}
    if proposal["ecosystem"] != "maven" or proposal["version"] is None:
        return pending
    selected, refs, invalid = set(), set(), False
    for observation in observations:
        try:
            metadata, facts = observation["metadata"], observation["facts"]
            if type(facts) is not dict or facts.get("schema_version") != 2 or type(facts.get("occurrences")) is not list:
                continue
            occurrences = [row for row in facts["occurrences"] if type(row) is dict
                           and row.get("ecosystem") == proposal["ecosystem"] and row.get("package") == proposal["package"]
                           and row.get("classification") == proposal["scope"]
                           and row.get("declared_version") == proposal["version"]]
            if not occurrences:
                continue
            _sources([metadata])
            # Retain applicable evidence even when it prevents resolution.
            # Callers re-inspect these refs; returning only successful refs
            # would let them silently shrink a conflicting/incomplete set.
            refs.add(metadata["ref"])
            inventory = facts["inventory"]
            _inventories([inventory])
            checked = _timestamp(metadata["checked_at"])
            if not (metadata["kind"] == "inventory" and metadata["status"] == "VERIFIED"
                    and metadata["scope_ref"] == scope_ref and metadata["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE"
                    and not metadata["coverage_gaps"] and observation["provenance_ref"]
                    and checked <= now and now - checked <= timedelta(days=max_age_days)
                    and inventory["kind"] == "RESOLVED" and inventory["scope_ref"] == scope_ref
                    and inventory["identity_digest"] == current_identity.envelope_digest
                    and inventory["producer_ref"] == metadata["producer_ref"]
                    and inventory["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE" and not inventory["coverage_gaps"]
                    and compare_identity(decode_identity(metadata["identity"]), current_identity,
                                         required_components).state == IdentityState.VALID
                    and compare_identity(decode_identity(metadata["identity"]), current_identity,
                                         tuple(metadata["required_components"])).state == IdentityState.VALID):
                invalid = True
                continue
            _digest(facts["tree_sha256"])
            _digest(facts["sidecar_sha256"])
            packages = {row["id"]: row for row in inventory["packages"]}
            if len(packages) != len(inventory["packages"]):
                invalid = True
                continue
            for occurrence in occurrences:
                version = occurrence["resolved_version"]
                package = packages.get(occurrence["instance_id"])
                if (not is_concrete_version(version) or package is None
                        or any(package[key] != proposal[key] for key in ("ecosystem", "package", "scope"))
                        or package["version"] != version or package["source_ref"] != metadata["ref"]):
                    invalid = True
                    continue
                selected.add(version)
        except (KeyError, ValueError, TypeError):
            invalid = True
    if invalid or len(selected) != 1:
        pending["source_refs"] = sorted(refs)
        if len(selected) > 1:
            pending["reason_codes"].append("RESOLUTION_VERSION_AMBIGUOUS")
        return pending
    return {"resolved_version": next(iter(selected)), "source_refs": sorted(refs), "reason_codes": []}


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
    resolution_refs = []
    effective_proposal = proposal
    if not is_concrete_version(proposal["version"]):
        resolution = resolve_dynamic_dependency(proposal, observations, current_identity=current_identity,
            required_components=required_components, scope_ref=scope_ref, now=now, max_age_days=max_age_days)
        if resolution["resolved_version"] is None:
            decision["reason_codes"] = resolution["reason_codes"]
            decision["source_refs"] = resolution["source_refs"]
            return decision
        resolution_refs = resolution["source_refs"]
        effective_proposal = {**proposal, "version": resolution["resolved_version"]}
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
                and all(facts.get(key) == effective_proposal[key] for key in package_keys)):
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
                        source_refs=resolution_refs + [incompatible["metadata"]["ref"]])
        return decision
    decision["source_refs"] = resolution_refs + [item["metadata"]["ref"] for item in advisories]
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
