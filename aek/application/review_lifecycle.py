"""Reconcile immutable review sensor output with trusted issue state."""
from __future__ import annotations

import hashlib
import json
import re

from aek.core.review.lifecycle import (
    IssueAuthority,
    IssueState,
    LifecycleSnapshot,
    ingest_review_occurrences,
    initialize_lifecycle,
    lifecycle_bytes,
    load_lifecycle,
    transition_issue,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TRACE_ID = re.compile(r"\b(?:TC-\d+|[AFDT]\d+)\b")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _normalize_issues(
    issues: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    if not isinstance(issues, tuple):
        raise ValueError("review issues must be a tuple")
    normalized: list[dict[str, object]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("review issue must be an object")
        row = dict(issue)
        problem = row.get("problem")
        if not isinstance(row.get("evidence"), str) or not str(
                row.get("evidence", "")).strip():
            row["evidence"] = problem
        affected = row.get("affected_ids")
        if (not isinstance(affected, list) or not affected
                or any(not isinstance(item, str) or not item.strip()
                       for item in affected)):
            source = " ".join(str(row.get(name, "")) for name in (
                "location", "problem", "required_change"))
            extracted = sorted(set(_TRACE_ID.findall(source)))
            row["affected_ids"] = extracted or ["UNSCOPED"]
        normalized.append(row)
    return tuple(normalized)


def _claim_rows(
    text: str,
    *,
    stage: str,
    doc_hashes: dict[str, str],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("issue claims are malformed") from exc
    required = {"schema_version", "stage", "base_snapshot_digest",
                "doc_hashes", "claims"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["stage"] != stage
            or not isinstance(value["base_snapshot_digest"], str)
            or not _DIGEST.fullmatch(value["base_snapshot_digest"])
            or value["doc_hashes"] != doc_hashes
            or not isinstance(value["claims"], list)
            or not value["claims"]):
        raise ValueError("issue claims are not bound to the current review")
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for claim in value["claims"]:
        if (not isinstance(claim, dict)
                or set(claim) != {"fingerprint", "state"}
                or claim.get("state") != IssueState.FIXED.value
                or not isinstance(claim.get("fingerprint"), str)
                or not _DIGEST.fullmatch(claim["fingerprint"])
                or claim["fingerprint"] in seen):
            raise ValueError("issue claim entry is invalid")
        seen.add(claim["fingerprint"])
        rows.append((claim["fingerprint"], _digest({
            "schema_version": 1,
            "stage": stage,
            "base_snapshot_digest": value["base_snapshot_digest"],
            "doc_hashes": doc_hashes,
            "claim": claim,
        })))
    return value["base_snapshot_digest"], tuple(rows)


def prepare_lifecycle(
    previous: LifecycleSnapshot | None,
    *,
    stage: str,
    subject_digest: str,
    request_digest: str,
    doc_hashes: dict[str, str],
    issues: tuple[dict[str, object], ...],
    iteration: int,
    max_iterations: int,
    claims_text: str | None = None,
) -> LifecycleSnapshot:
    """Apply Author claims, append Reviewer facts, then verify absent fixes.

    The caller invokes this only after the review request/receipt and current
    document hashes have been validated.  A clean review can verify a prior
    FIXED claim; it cannot create that claim or close the issue.
    """
    normalized = _normalize_issues(issues)
    if previous is None:
        if claims_text is not None:
            raise ValueError("issue claims require an existing lifecycle")
        return initialize_lifecycle(
            stage=stage, subject_digest=subject_digest,
            request_digest=request_digest, doc_hashes=doc_hashes,
            issues=normalized, iteration=iteration,
            max_iterations=max_iterations,
        )
    previous = load_lifecycle(lifecycle_bytes(previous))
    if previous.stage != stage or previous.max_iterations != max_iterations:
        raise ValueError("lifecycle stage or policy changed")
    working = previous
    if claims_text is not None:
        base_digest, claims = _claim_rows(
            claims_text, stage=stage, doc_hashes=doc_hashes)
        existing = {(event.fingerprint, event.input_digest)
                    for event in working.events
                    if event.authority == IssueAuthority.AUTHOR
                    and event.to_state == IssueState.FIXED}
        unapplied = [(fingerprint, digest) for fingerprint, digest in claims
                     if (fingerprint, digest) not in existing]
        if unapplied and base_digest != working.snapshot_digest:
            raise ValueError("issue claim precondition snapshot changed")
        known = {item.fingerprint for item in working.issues}
        if any(fingerprint not in known for fingerprint, _digest_value in claims):
            raise ValueError("issue claim references an unknown issue")
        for fingerprint, claim_digest in unapplied:
            working = transition_issue(
                working, fingerprint, IssueState.FIXED,
                IssueAuthority.AUTHOR, input_digest=claim_digest)
    working = ingest_review_occurrences(
        working, subject_digest=subject_digest,
        request_digest=request_digest, doc_hashes=doc_hashes,
        issues=normalized, iteration=iteration)
    reported = {
        item.fingerprint for item in working.issues
        if any(row.request_digest == request_digest
               for row in item.occurrences)
    }
    for item in tuple(working.issues):
        if item.state == IssueState.FIXED and item.fingerprint not in reported:
            working = transition_issue(
                working, item.fingerprint, IssueState.VERIFIED,
                IssueAuthority.REVIEWER, input_digest=request_digest)
    return working
