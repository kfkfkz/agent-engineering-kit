"""Pure D7 projections over fixed records and already checked observations.

The application must obtain observations from its trusted SourceEvidencePort,
not deserialize a claimed VERIFIED status. These projections cannot mutate an
Issue lifecycle, compose a Gate, or grant authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from aek.core.context.identity import IdentityEnvelope, IdentityState, compare_identity, decode_identity
from aek.core.security.model import SecurityInputError, _strings, _text
from aek.core.security.record import SecurityRecord, _invariants, _sources, _timestamp, finding_fingerprint, record_as_dict


def _valid_observation(observation: dict | None, *, current_identity: IdentityEnvelope,
                       required_components: tuple[str, ...], scope_ref: str,
                       now: datetime, max_age_days: int, complete: bool = True) -> bool:
    try:
        if type(observation) is not dict or set(observation) != {"metadata", "facts", "provenance_ref"}:
            return False
        metadata = observation["metadata"]
        _sources([metadata])
        checked = _timestamp(metadata["checked_at"])
        components = tuple(sorted(set(required_components) | set(metadata["required_components"])))
        return (metadata["status"] == "VERIFIED" and type(observation["facts"]) is dict
                and bool(_text(observation["provenance_ref"])) and metadata["scope_ref"] == scope_ref
                and checked <= now and now - checked <= timedelta(days=max_age_days)
                and compare_identity(decode_identity(metadata["identity"]), current_identity,
                                     components).state == IdentityState.VALID
                and (not complete or (metadata["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE"
                                      and not metadata["coverage_gaps"])))
    except (TypeError, ValueError, KeyError):
        return False


def _context(required_components: tuple[str, ...], scope_ref: str, now: datetime, max_age_days: int) -> None:
    if type(required_components) is not tuple or not required_components:
        raise SecurityInputError()
    _strings(list(required_components))
    _text(scope_ref)
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(max_age_days) is not int or not 0 < max_age_days <= 365):
        raise SecurityInputError()


def compare_security_baseline(baseline: SecurityRecord, current: SecurityRecord, *,
                              baseline_observation: dict | None, observations: tuple[dict, ...] = (),
                              current_identity: IdentityEnvelope, required_components: tuple[str, ...],
                              scope_ref: str, repository_ref: str, now: datetime,
                              max_age_days: int = 7) -> dict:
    _context(required_components, scope_ref, now, max_age_days)
    _text(repository_ref)
    old, new = record_as_dict(baseline), record_as_dict(current)
    # Controller repository_ref proves where the baseline belongs. Finding
    # identity must use the existing record/workflow proxy namespace instead
    # of introducing the controller's filesystem path into the fingerprint.
    fingerprint_namespace = new["identity"]["components"]["subject"].get("repo_digest")
    old_namespace = old["identity"]["components"]["subject"].get("repo_digest")
    repository_compatible = old_namespace is None or old_namespace == fingerprint_namespace
    context = dict(current_identity=current_identity, required_components=required_components,
                   scope_ref=scope_ref, now=now, max_age_days=max_age_days)
    historical_resolved = ()
    facts = baseline_observation.get("facts", {}) if type(baseline_observation) is dict else {}
    if type(facts) is dict and type(facts.get("resolved_fingerprints", [])) is list:
        historical_resolved = tuple(facts.get("resolved_fingerprints", []))
    old_fingerprints = {finding_fingerprint(row, fingerprint_namespace) for row in old["findings"]}
    baseline_valid = (_valid_observation(baseline_observation, **context)
                      and baseline_observation["metadata"]["kind"] == "baseline"
                      and set(facts) in ({"record_digest", "scope", "repository_ref"},
                                         {"record_digest", "scope", "repository_ref", "resolved_fingerprints"})
                      and facts["record_digest"] == baseline.revision_digest
                      and facts["scope"] == old["scope"] and facts["repository_ref"] == repository_ref
                      and type(facts.get("resolved_fingerprints", [])) is list
                      and all(type(item) is str for item in historical_resolved)
                      and len(set(historical_resolved)) == len(historical_resolved)
                      and set(historical_resolved) <= old_fingerprints)
    current_valid = compare_identity(decode_identity(new["identity"]), current_identity,
                                     required_components).state == IdentityState.VALID
    scope_covered = (set(old["scope"]["paths"]) <= set(new["scope"]["paths"])
                     and set(old["scope"]["focus"]) <= set(new["scope"]["focus"]))
    def indexed(findings):
        result = {}
        for finding in findings:
            result.setdefault(finding_fingerprint(finding, fingerprint_namespace), []).append(finding["id"])
        return result
    old_ids, new_ids = indexed(old["findings"]), indexed(new["findings"])
    absence_proofs = {}
    for observation in observations:
        if not _valid_observation(observation, **context):
            continue
        metadata, facts = observation["metadata"], observation["facts"]
        if (metadata["kind"] in {"negative-test", "revalidation"}
                and set(facts) == {"baseline_record_digest", "record_digest", "finding_fingerprint",
                                   "covered_paths", "passed", "original_condition_absent"}
                and facts["baseline_record_digest"] == baseline.revision_digest
                and facts["record_digest"] == current.revision_digest
                and type(facts["finding_fingerprint"]) is str
                and facts["finding_fingerprint"] in old_ids
                and type(facts["passed"]) is bool and type(facts["original_condition_absent"]) is bool
                and type(facts["covered_paths"]) is list
                and all(type(path) is str for path in facts["covered_paths"])
                and len(set(facts["covered_paths"])) == len(facts["covered_paths"])
                and set(old["scope"]["paths"]) <= set(facts["covered_paths"])
                and set(facts["covered_paths"]) <= set(new["scope"]["paths"])):
            absence_proofs.setdefault(facts["finding_fingerprint"], []).append(observation)
    rows, gaps = [], []
    if not baseline_valid:
        gaps.append("BASELINE_SOURCE_UNVERIFIED")
    if not current_valid:
        gaps.append("CURRENT_IDENTITY_INVALID")
    if not repository_compatible:
        gaps.append("BASELINE_REPOSITORY_IDENTITY_CHANGED")
    if not scope_covered:
        gaps.append("BASELINE_SCOPE_NOT_COVERED")
    for fingerprint in sorted(set(old_ids) | set(new_ids)):
        reasons, evidence = [], []
        if not baseline_valid or not current_valid or not scope_covered or not repository_compatible:
            classification = "UNVERIFIED"
            reasons = ["BASELINE_SOURCE_UNVERIFIED" if not baseline_valid else
                       "CURRENT_IDENTITY_INVALID" if not current_valid else
                       "BASELINE_REPOSITORY_IDENTITY_CHANGED" if not repository_compatible else "BASELINE_SCOPE_NOT_COVERED"]
        elif fingerprint in new_ids:
            classification = "REOPENED" if fingerprint in historical_resolved else "EXISTING" if fingerprint in old_ids else "INTRODUCED"
            evidence = [baseline_observation["metadata"]["ref"]]
        else:
            classification, reasons = "UNVERIFIED", ["ABSENCE_REVALIDATION_REQUIRED"]
            proofs = absence_proofs.get(fingerprint, [])
            negative = [item for item in proofs if item["facts"]["passed"] and item["facts"]["original_condition_absent"]]
            if len(negative) < len(proofs):
                reasons = ["ABSENCE_REVALIDATION_CONFLICT" if negative else "ABSENCE_REVALIDATION_FAILED"]
            elif negative:
                classification, reasons = "RESOLVED", []
                evidence = [baseline_observation["metadata"]["ref"],
                            *sorted({item["metadata"]["ref"] for item in negative})]
        rows.append({"fingerprint": fingerprint, "classification": classification,
                     "baseline_finding_refs": old_ids.get(fingerprint, []),
                     "current_finding_refs": new_ids.get(fingerprint, []), "evidence_refs": evidence,
                     "reason_codes": reasons})
        gaps.extend(reasons)
    return {"baseline_revision_digest": baseline.revision_digest,
            "current_revision_digest": current.revision_digest,
            "baseline_ref": baseline_observation["metadata"]["ref"] if baseline_valid else None,
            "classifications": rows, "coverage_gaps": sorted(set(gaps))}


def invariant_binding(row: dict) -> dict:
    """Bind semantic content; later proof/status additions do not change it."""
    _invariants([row])
    return {"id": row["id"], "statement": row["statement"],
            **{key: sorted(row[key]) for key in ("source_refs", "entry_refs", "test_refs")}}


def _complete_refs(row: dict) -> bool:
    entries = row["entry_refs"]
    patterns = (r"(?:REQ|A)-?[0-9]+", r"F-?[0-9]+", r"D-?[0-9]+")
    return (bool(row["source_refs"]) and all(any(re.fullmatch(pattern, ref) for ref in entries) for pattern in patterns)
            and any(re.fullmatch(r"TC-?[0-9]+", ref) for ref in row["test_refs"])
            and any(not re.fullmatch(r"(?:REQ|A|F|D|TC)-?[0-9]+", ref) for ref in entries))


def derive_security_invariants(rows: tuple[dict, ...], observations: tuple[dict, ...] = (), *,
                               current_identity: IdentityEnvelope, required_components: tuple[str, ...],
                               scope_ref: str, now: datetime, max_age_days: int = 7,
                               confirmed_user_refs: tuple[str, ...] = (),
                               previous_confirmed_ids: tuple[str, ...] = ()) -> dict:
    """Return existing DTO rows plus gaps, never an enforcement/Gate decision.

    Confirmation refs and previously confirmed IDs are control-side inputs,
    checked by the application against actual current/historical provenance.
    They must never be populated from author row.status or approved_by fields.
    """
    _context(required_components, scope_ref, now, max_age_days)
    if type(rows) is not tuple:
        raise SecurityInputError()
    _invariants(list(rows))
    if type(confirmed_user_refs) is not tuple or type(previous_confirmed_ids) is not tuple:
        raise SecurityInputError()
    _strings(list(confirmed_user_refs))
    _strings(list(previous_confirmed_ids))
    context = dict(current_identity=current_identity, required_components=required_components,
                   scope_ref=scope_ref, now=now, max_age_days=max_age_days)
    projected, gaps = [], []
    for row in rows:
        binding = invariant_binding(row)
        refs, approvals = [], []
        invalidated = row["id"] in previous_confirmed_ids
        for observation in observations:
            # An observation whose former exact source is revoked or stale can
            # invalidate a claim, but cannot confirm it. Absence of an observed
            # source is distinguished from authenticated historical knowledge.
            if type(observation) is dict:
                metadata, facts = observation.get("metadata"), observation.get("facts")
                if (type(metadata) is dict and type(facts) is dict
                        and metadata.get("ref") in row["source_refs"]
                        and metadata.get("kind") in {"frozen-requirements", "frozen-design", "approved-invariant"}
                        and type(facts.get("invariants")) is list and binding in facts["invariants"]
                        and metadata.get("status") in {"DECLARED", "STALE", "INVALID"}):
                    invalidated = True
            if not _valid_observation(observation, **context):
                continue
            metadata, facts = observation["metadata"], observation["facts"]
            frozen = (metadata["kind"] in {"frozen-requirements", "frozen-design"}
                      and any(component.startswith("artifact.") for component in required_components)
                      and set(facts) == {"frozen", "invariants"} and facts["frozen"] is True)
            approved = (metadata["kind"] == "approved-invariant"
                        and set(facts) == {"invariants", "approval_ref"}
                        and type(facts["approval_ref"]) is str
                        and facts["approval_ref"] in confirmed_user_refs)
            if (metadata["ref"] in row["source_refs"] and (frozen or approved)
                    and type(facts["invariants"]) is list and binding in facts["invariants"]):
                refs.append(metadata["ref"])
                if approved:
                    approvals.append(facts["approval_ref"])
        status = "CONFIRMED" if _complete_refs(row) and set(refs) == set(row["source_refs"]) else "INVALIDATED" if invalidated else "CANDIDATE"
        if status != "CONFIRMED":
            gaps.append("INVARIANT_SOURCE_INVALIDATED" if invalidated else "INVARIANT_SOURCE_UNAVAILABLE")
        projected.append({**row, "status": status,
                          "evidence_refs": sorted(set(row["evidence_refs"]) | set(refs) | set(approvals))})
    _invariants(projected)
    return {"invariants": projected, "coverage_gaps": sorted(set(gaps))}
