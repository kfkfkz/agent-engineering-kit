"""Prepare reviewer input and verify the proof of what was actually reviewed.

The reviewer remains an untrusted sensor. Incremental input is allowed only
when Core proves the dependency closure and this service can bind every sent
byte to the current full-document snapshot. A bad incremental receipt asks the
caller to resend a full request; it never authorizes a gate result.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping

from aek.core.review.incremental import (
    ReviewInputPlan, ReviewSnapshot, SectionSnapshot, plan_full_review,
    plan_incremental_review,
)
from aek.core.review.result import ReviewResult, validate_review_payload


SectionKey = tuple[str, str]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewRequest:
    schema_version: int
    stage: str
    mode: str
    doc_hashes: tuple[tuple[str, str], ...]
    selected_sections: tuple[SectionKey, ...]
    full_documents: tuple[tuple[str, str], ...]
    section_documents: tuple[tuple[str, str, str], ...]
    supporting_context: tuple[tuple[str, str], ...]
    dependency_fingerprint: str
    prior_doc_hashes: tuple[tuple[str, str], ...]
    unresolved_sections: tuple[SectionKey, ...]
    plan_receipt_digest: str
    input_digest: str
    request_digest: str


@dataclass(frozen=True)
class ReviewValidation:
    state: str
    reason: str
    result: ReviewResult | None = None


def _request_payload(request: ReviewRequest) -> dict[str, object]:
    return {name: getattr(request, name)
            for name in ReviewRequest.__dataclass_fields__
            if name != "request_digest"}


def _build_request(
    plan: ReviewInputPlan, current: ReviewSnapshot, *,
    documents: Mapping[str, str], section_texts: Mapping[SectionKey, str],
    supporting_context: Mapping[str, str],
    prior_doc_hashes: tuple[tuple[str, str], ...],
    unresolved_sections: tuple[SectionKey, ...],
) -> ReviewRequest:
    expected_docs = dict(current.doc_hashes)
    if (set(documents) != set(expected_docs)
            or any(not isinstance(name, str) or not isinstance(text, str)
                   for name, text in documents.items())
            or any(_sha(documents[name]) != digest
                   for name, digest in current.doc_hashes)):
        raise ValueError("current full documents do not match the review snapshot")
    section_by_key = {section.key: section for section in current.sections}
    section_bytes_valid = (
        set(section_texts) == set(section_by_key)
        and all(isinstance(text, str) for text in section_texts.values())
        and all(_sha(section_texts[key]) == section.digest
                for key, section in section_by_key.items())
    )
    if plan.mode == "incremental" and not section_bytes_valid:
        plan = plan_full_review(
            current, reviewed_doc_hashes=prior_doc_hashes,
            reason="SECTION_PAYLOAD_BINDING_FAILED")
    if plan.mode == "full":
        full_documents = tuple((name, documents[name])
                               for name, _digest_value in current.doc_hashes)
        section_documents: tuple[tuple[str, str, str], ...] = ()
    else:
        full_documents = ()
        section_documents = tuple((doc, heading, section_texts[(doc, heading)])
                                  for doc, heading in plan.selected_sections)
    if (not isinstance(supporting_context, Mapping)
            or any(not isinstance(name, str) or not name
                   or not isinstance(text, str)
                   for name, text in supporting_context.items())):
        raise ValueError("review supporting context is invalid")
    supporting = tuple((name, supporting_context[name])
                       for name in sorted(supporting_context))
    actual_input = {
        "mode": plan.mode,
        "full_documents": full_documents,
        "section_documents": section_documents,
        "supporting_context": supporting,
    }
    shell = ReviewRequest(
        1, current.stage, plan.mode, current.doc_hashes,
        plan.selected_sections, full_documents, section_documents, supporting,
        current.dependency_fingerprint, prior_doc_hashes,
        unresolved_sections, plan.receipt_digest, _digest(actual_input), "",
    )
    return ReviewRequest(**{**asdict(shell),
                            "request_digest": _digest(_request_payload(shell))})


def plan_review(
    previous: ReviewSnapshot, current: ReviewSnapshot, *,
    prior_review_was_full: bool,
    reviewed_doc_hashes: tuple[tuple[str, str], ...],
    documents: Mapping[str, str],
    section_texts: Mapping[SectionKey, str],
    supporting_context: Mapping[str, str] | None = None,
    unresolved_sections: tuple[SectionKey, ...] = (),
    external_ids: tuple[str, ...] = (),
) -> ReviewRequest:
    """Return the exact full or proven-incremental payload for a reviewer."""
    plan = plan_incremental_review(
        previous, current, prior_review_was_full=prior_review_was_full,
        reviewed_doc_hashes=reviewed_doc_hashes,
        unresolved_sections=unresolved_sections, external_ids=external_ids,
        same_group_documents=tuple(doc for doc, _digest_value in
                                   current.doc_hashes))
    return _build_request(
        plan, current, documents=documents, section_texts=section_texts,
        supporting_context=supporting_context or {},
        prior_doc_hashes=reviewed_doc_hashes,
        unresolved_sections=unresolved_sections)


def full_review_request(
    current: ReviewSnapshot, *, documents: Mapping[str, str],
    section_texts: Mapping[SectionKey, str], reason: str,
    supporting_context: Mapping[str, str] | None = None,
    prior_doc_hashes: tuple[tuple[str, str], ...] = (),
    unresolved_sections: tuple[SectionKey, ...] = (),
) -> ReviewRequest:
    """Build the fail-closed full payload used after incremental proof failure."""
    plan = plan_full_review(current, reviewed_doc_hashes=prior_doc_hashes,
                            reason=reason)
    return _build_request(
        plan, current, documents=documents, section_texts=section_texts,
        supporting_context=supporting_context or {},
        prior_doc_hashes=prior_doc_hashes,
        unresolved_sections=unresolved_sections)


def reviewer_receipt(request: ReviewRequest) -> str:
    """Canonical receipt envelope the reviewer must return with its issues."""
    value = {
        "schema_version": 1,
        "stage": request.stage,
        "mode": request.mode,
        "request_digest": request.request_digest,
        "plan_receipt_digest": request.plan_receipt_digest,
        "input_digest": request.input_digest,
        "doc_hashes": dict(request.doc_hashes),
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def review_request_bytes(request: ReviewRequest) -> bytes:
    """Serialize a request canonically after revalidating its digests."""
    if (not isinstance(request, ReviewRequest)
            or request.request_digest != _digest(_request_payload(request))):
        raise ValueError("review request digest is invalid")
    return json.dumps(asdict(request), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load_review_request(raw: bytes) -> ReviewRequest:
    """Strictly decode persisted reviewer input without ignored fields."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review request is malformed") from exc
    if (not isinstance(value, dict)
            or set(value) != set(ReviewRequest.__dataclass_fields__)):
        raise ValueError("review request has unknown or missing fields")
    try:
        request = ReviewRequest(
            value["schema_version"], value["stage"], value["mode"],
            tuple(tuple(item) for item in value["doc_hashes"]),
            tuple(tuple(item) for item in value["selected_sections"]),
            tuple(tuple(item) for item in value["full_documents"]),
            tuple(tuple(item) for item in value["section_documents"]),
            tuple(tuple(item) for item in value["supporting_context"]),
            value["dependency_fingerprint"],
            tuple(tuple(item) for item in value["prior_doc_hashes"]),
            tuple(tuple(item) for item in value["unresolved_sections"]),
            value["plan_receipt_digest"], value["input_digest"],
            value["request_digest"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("review request field types are invalid") from exc
    if (type(request.schema_version) is not int or request.schema_version != 1
            or request.mode not in {"full", "incremental"}
            or not request.stage
            or request.request_digest != _digest(_request_payload(request))
            or request.input_digest != _digest({
                "mode": request.mode,
                "full_documents": request.full_documents,
                "section_documents": request.section_documents,
                "supporting_context": request.supporting_context,
            })
            or request.doc_hashes != tuple(
                (name, _sha(text)) for name, text in request.full_documents)
            and request.mode == "full"):
        raise ValueError("review request integrity check failed")
    if raw != review_request_bytes(request):
        raise ValueError("review request is not canonical JSON")
    return request


def review_snapshot_bytes(snapshot: ReviewSnapshot) -> bytes:
    """Serialize a snapshot only when Core accepts it as a full-review input."""
    plan_full_review(snapshot)
    return json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load_review_snapshot(raw: bytes) -> ReviewSnapshot:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review snapshot is malformed") from exc
    if (not isinstance(value, dict)
            or set(value) != set(ReviewSnapshot.__dataclass_fields__)):
        raise ValueError("review snapshot has unknown or missing fields")
    try:
        snapshot = ReviewSnapshot(
            value["stage"], tuple(tuple(item) for item in value["doc_hashes"]),
            tuple(SectionSnapshot(
                item["document"], item["heading"], item["digest"],
                tuple(item["defines_ids"]), tuple(item["uses_ids"]))
                for item in value["sections"]),
            value["dependency_fingerprint"], value["parse_complete"],
            value["unknown_edges"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("review snapshot field types are invalid") from exc
    plan_full_review(snapshot)
    if raw != review_snapshot_bytes(snapshot):
        raise ValueError("review snapshot is not canonical JSON")
    return snapshot


def validate_review_result(
    request: ReviewRequest, result_text: str, receipt_text: str,
) -> ReviewValidation:
    """Validate receipt and issues; incremental failure requests full resend."""
    expected_receipt = reviewer_receipt(request)
    try:
        canonical_receipt = json.dumps(
            json.loads(receipt_text), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
    except (TypeError, json.JSONDecodeError):
        canonical_receipt = ""
    if canonical_receipt != expected_receipt:
        return ReviewValidation(
            "FULL_REQUIRED" if request.mode == "incremental" else "INVALID",
            "REVIEW_RECEIPT_MISMATCH")
    result, error = validate_review_payload(
        result_text, request.stage, dict(request.doc_hashes))
    if result is None:
        return ReviewValidation("INVALID", error)
    return ReviewValidation("VALID", "review result and receipt verified", result)
