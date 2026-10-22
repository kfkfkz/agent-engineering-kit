"""Prepare reviewer input and verify the proof of what was actually reviewed.

The reviewer remains an untrusted sensor. Incremental input is allowed only
when Core proves the dependency closure and this service can bind every sent
byte to the current full-document snapshot. A bad incremental receipt asks the
caller to resend a full request; it never authorizes a gate result.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Mapping

from aek.core.review.incremental import (
    ReviewInputPlan, ReviewSnapshot, SectionSnapshot, plan_full_review,
    plan_incremental_review,
)
from aek.core.review.result import ReviewResult, validate_review_payload
from aek.core.review.scope import marker_count, scoped_sections


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
class ReviewRequestV2(ReviewRequest):
    reference_sections: tuple[tuple[str, str, str], ...]
    scope_proof: dict[str, object]


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


def _full_boundary_evidence(
    current: ReviewSnapshot, documents: Mapping[str, str],
    supporting_context: Mapping[str, str],
) -> dict[str, object]:
    """Describe actual source structure; this is not incremental admission."""
    roles = ({"概要设计.md": "F"} if current.stage == "概要设计" else
             {"详细设计.md": "D", "API设计.md": "D", "数据库设计.md": "D"}
             if current.stage == "详细设计" else {})
    markers = []
    for document in sorted(documents):
        markers.append((document, marker_count(documents[document])))
    owners = []
    globals_ = []
    providers = {item: section.key for section in current.sections
                 for item in section.defines_ids}
    # Display headings are lossy: literal " / " in a title is not a level
    # boundary. Use the actual supported Markdown heading/fence stack, not
    # split() or prefix matching (which can assign a global title to an owner).
    sections_by_key = {section.key: section for section in current.sections}
    actual_owners = {}
    for document, text in documents.items():
        family = roles.get(document)
        stack = []
        fence_char, fence_size = '', 0
        for line in text.splitlines(keepends=True):
            fence = re.match(r'^[ \t]{0,3}(`{3,}|~{3,})', line)
            if fence:
                token = fence.group(1)
                tab_indented = '\t' in line[:fence.start(1)]
                if not fence_char and (tab_indented or (token[0] == '`' and '`' in line[fence.end(1):])):
                    # The source parser marks this unsupported opener unknown.
                    # Do not let it invent an owner boundary in the proof.
                    continue
                if not fence_char:
                    fence_char, fence_size = token[0], len(token)
                elif (not tab_indented and token[0] == fence_char and len(token) >= fence_size
                      and not line[fence.end(1):].strip(' \t\r\n')):
                    fence_char, fence_size = '', 0
                continue
            if fence_char:
                continue
            heading = re.match(r'^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$', line)
            if not heading:
                continue
            level, title = len(heading.group(1)), heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            logical = ' / '.join([item[1] for item in stack] + [title])
            section = sections_by_key.get((document, logical))
            owner = stack[-1][2] if stack else None
            definition = re.fullmatch(r'([FD][1-9][0-9]*)[：:]\s*\S.*', title)
            if (definition and family and definition.group(1).startswith(family)
                    and section is not None and section.defines_ids == (definition.group(1),)):
                owner = definition.group(1)
            stack.append((level, title, owner))
            actual_owners[(document, logical)] = owner
    for section in current.sections:
        owner = actual_owners.get(section.key)
        owners.append((section.document, section.heading,
                       owner))
        if owner is None:
            globals_.append(section.key)
    edges = sorted({(providers[ref], section.key) for section in current.sections
                    for ref in section.uses_ids if ref in providers})
    return {
        "model_version": 1,
        "participating_document_roles": tuple(
            (document, roles.get(document, "UNKNOWN")) for document in sorted(documents)),
        "document_markers": tuple(markers),
        "section_owners": tuple(owners),
        "global_sections": tuple(globals_),
        "resolved_reference_edges": tuple(edges),
        # Bind actual supplied bodies. The CLI verifies freezing separately;
        # a digest produced by this pure factory is never an authorization.
        "frozen_upstream_source_hashes": tuple(
            (source, _sha(text)) for source, text in sorted(supporting_context.items())
            if source.startswith("upstream/")),
    }


def _v2_request_payload(request: ReviewRequestV2) -> dict[str, object]:
    return {name: getattr(request, name)
            for name in ReviewRequestV2.__dataclass_fields__ if name != "request_digest"}


def _v2_input_payload(request: ReviewRequestV2) -> dict[str, object]:
    return {name: getattr(request, name) for name in (
        "mode", "full_documents", "section_documents", "reference_sections",
        "supporting_context", "scope_proof")}


def full_review_request_v2(
    current: ReviewSnapshot, *, documents: Mapping[str, str],
    section_texts: Mapping[SectionKey, str],
    supporting_context: Mapping[str, str] | None = None,
) -> ReviewRequestV2:
    """Bind the initial full v2 input without claiming a baseline or approval."""
    base = full_review_request(
        current, documents=documents, section_texts=section_texts,
        supporting_context=supporting_context, reason="FULL_BASELINE_REQUIRED")
    if (set(section_texts) != {section.key for section in current.sections}
            or any(not isinstance(section_texts[section.key], str)
                   or _sha(section_texts[section.key]) != section.digest
                   for section in current.sections)
            or any("".join(section_texts[section.key]
                           for section in current.sections if section.document == doc) != text
                   for doc, text in base.full_documents)):
        raise ValueError("v2 full section payload does not match the current documents")
    proof = {
        "schema_version": 1,
        "algorithm": "directed-id-impact-v1",
        "boundary_kind": "declared-id-boundary",
        "boundary_evidence_digest": _digest(_full_boundary_evidence(
            current, dict(base.full_documents), dict(base.supporting_context))),
        "baseline_snapshot_digest": None,
        "current_snapshot_digest": hashlib.sha256(review_snapshot_bytes(current)).hexdigest(),
        "target_reasons": [{"document": doc, "heading": heading,
                            "reason_codes": ["FULL_BASELINE_REQUIRED"]}
                           for doc, heading in base.selected_sections],
        "reference_reasons": [],
        "issue_bindings": [],
        "reason_codes": ["FULL_BASELINE_REQUIRED"],
    }
    value = {**asdict(base), "schema_version": 2, "reference_sections": (),
             "scope_proof": proof, "input_digest": "", "request_digest": ""}
    # asdict recursively copies tuples and leaves the original v1 value intact.
    shell = ReviewRequestV2(**value)
    value["input_digest"] = _digest(_v2_input_payload(shell))
    shell = ReviewRequestV2(**value)
    value["request_digest"] = _digest(_v2_request_payload(shell))
    return ReviewRequestV2(**value)


def plan_review_v2(
    current: ReviewSnapshot, *, baseline: bytes | None,
    documents: Mapping[str, str], section_texts: Mapping[SectionKey, str],
    supporting_context: Mapping[str, str] | None = None,
    unresolved_sections: tuple[SectionKey, ...] = (),
    external_ids: tuple[str, ...] = (),
    issue_bindings: tuple[dict[str, object], ...] = (),
) -> ReviewRequestV2:
    """Use accepted-material metadata to prepare actual scoped bytes, not PASS."""
    full = full_review_request_v2(current, documents=documents,
        section_texts=section_texts, supporting_context=supporting_context)
    if baseline is None and not issue_bindings:
        return full
    previous = None
    evidence = _full_boundary_evidence(current, dict(full.full_documents),
                                       dict(full.supporting_context))
    try:
        data = json.loads(baseline.decode("utf-8")) if baseline is not None else None
        expected = {"schema_version", "snapshot", "accepted_full_request_digest",
                    "accepted_receipt_digest", "accepted_sensor_digest",
                    "dependency_fingerprint", "boundary_evidence_digest"}
        if (not isinstance(data, dict) or set(data) != expected
                or type(data["schema_version"]) is not int or data["schema_version"] != 2
                or any(not _sha_value(data[name]) for name in expected - {"schema_version", "snapshot"})
                or baseline != json.dumps(data, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")):
            raise ValueError("accepted baseline format is invalid")
        previous = load_review_snapshot(json.dumps(data["snapshot"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if data["dependency_fingerprint"] != previous.dependency_fingerprint:
            raise ValueError("baseline dependency identity is inconsistent")
        targets, references, reason = scoped_sections(previous, current,
            evidence=evidence, section_texts=dict(section_texts),
            unresolved_sections=unresolved_sections, external_ids=external_ids)
        if reason is None and data["boundary_evidence_digest"] != _digest(evidence):
            reason = "BOUNDARY_CHANGED"
        if any(row.get("section_keys") is None for row in issue_bindings):
            targets, references, reason = (), (), "UNKNOWN_ISSUE_LOCATION"
    except (AttributeError, TypeError, ValueError):
        targets, references, reason = (), (), "BASELINE_MISMATCH"
    proof = json.loads(json.dumps(full.scope_proof))
    proof["issue_bindings"] = json.loads(json.dumps(issue_bindings))
    value = asdict(full)
    if reason is not None:
        proof["reason_codes"] = [reason]
        for row in proof["target_reasons"]:
            row["reason_codes"] = [reason]
    else:
        if not targets:
            raise ValueError("no scoped review work; use the existing convergence result")
        assert previous is not None
        before = {section.key: section for section in previous.sections}
        changed = {section.key for section in current.sections
                   if before[section.key].digest != section.digest}
        unresolved = set(unresolved_sections)
        global_keys = set(evidence["global_sections"])
        target_rows = []
        for doc, heading in targets:
            key = (doc, heading)
            codes = (["CHANGED_SECTION"] if key in changed else [])
            if key in unresolved:
                codes.append("UNRESOLVED_ISSUE")
            target_rows.append({"document": doc, "heading": heading,
                                "reason_codes": sorted(codes or ["DEPENDENT_CONTRACT"])})
        value.update(mode="incremental", selected_sections=targets,
                     full_documents=(),
                     section_documents=tuple((doc, heading, section_texts[(doc, heading)])
                                             for doc, heading in targets),
                     reference_sections=tuple((doc, heading, section_texts[(doc, heading)])
                                              for doc, heading in references),
                     prior_doc_hashes=previous.doc_hashes,
                     unresolved_sections=unresolved_sections)
        proof.update(baseline_snapshot_digest=hashlib.sha256(review_snapshot_bytes(previous)).hexdigest(),
                     target_reasons=target_rows,
                     reference_reasons=[{"document": doc, "heading": heading,
                                         "reason_codes": ["GLOBAL_CONSTRAINT" if (doc, heading)
                                                          in global_keys else "REFERENCED_PROVIDER"]}
                                        for doc, heading in references],
                     reason_codes=sorted((["CHANGED_SECTION"] if changed else [])
                                         + (["UNRESOLVED_ISSUE"] if unresolved else [])))
    value["scope_proof"] = proof
    value["plan_receipt_digest"] = _digest({"mode": value["mode"], "proof": proof,
                                           "doc_hashes": current.doc_hashes})
    shell = ReviewRequestV2(**value)
    value["input_digest"] = _digest(_v2_input_payload(shell))
    shell = ReviewRequestV2(**value)
    value["request_digest"] = _digest(_v2_request_payload(shell))
    request = ReviewRequestV2(**value)
    _validate_v2_request(request)
    return request


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
    if isinstance(request, ReviewRequestV2):
        _validate_v2_request(request)
        value.update(schema_version=2, scope_proof_digest=_digest(request.scope_proof),
                     issue_locations=[])
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def review_request_bytes(request: ReviewRequest) -> bytes:
    """Serialize a request canonically after revalidating its digests."""
    if isinstance(request, ReviewRequestV2):
        _validate_v2_request(request)
        return json.dumps(asdict(request), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
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


_V2_REASONS = frozenset({
    "CHANGED_SECTION", "UNRESOLVED_ISSUE", "DEPENDENT_CONTRACT",
    "REFERENCED_PROVIDER", "GLOBAL_CONSTRAINT", "FULL_BASELINE_REQUIRED",
    "BASELINE_MISMATCH", "DEPENDENCY_CHANGED", "PARSER_OR_EDGE_GAP",
    "BOUNDARY_CHANGED", "UNKNOWN_SCOPE", "UNKNOWN_ISSUE_LOCATION",
    "OUT_OF_SCOPE_FINDING", "PAYLOAD_BINDING_FAILED", "OWNERSHIP_AMBIGUOUS",
    "UNTRUSTED_REFERENCE", "GLOBAL_CONSTRAINT_CHANGED",
})
_V2_PROOF_FIELDS = frozenset({
    "schema_version", "algorithm", "boundary_kind", "boundary_evidence_digest",
    "baseline_snapshot_digest", "current_snapshot_digest", "target_reasons",
    "reference_reasons", "issue_bindings", "reason_codes",
})


def _text_rows(value, width: int, *, unique_key_width: int) -> bool:
    return (isinstance(value, tuple)
            and all(isinstance(row, tuple) and len(row) == width
                    and all(isinstance(cell, str) and cell for cell in row)
                    for row in value)
            and len({row[:unique_key_width] for row in value}) == len(value))


def _sha_value(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _reason_codes(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item in _V2_REASONS for item in value)
            and value == sorted(set(value)))


def _validate_v2_request(request: ReviewRequestV2) -> None:
    """Check typed structure and byte binding, not approval or semantic coverage."""
    if (type(request) is not ReviewRequestV2
            or type(request.schema_version) is not int or request.schema_version != 2
            or not isinstance(request.stage, str) or not request.stage
            or not isinstance(request.mode, str)
            or request.mode not in {"full", "incremental"}
            or any(not _sha_value(getattr(request, name)) for name in (
                "dependency_fingerprint", "plan_receipt_digest", "input_digest",
                "request_digest"))):
        raise ValueError("v2 review request identity is invalid")
    for name, width, key_width in (
        ("doc_hashes", 2, 1), ("prior_doc_hashes", 2, 1),
        ("selected_sections", 2, 2), ("unresolved_sections", 2, 2),
        ("full_documents", 2, 1), ("section_documents", 3, 2),
        ("reference_sections", 3, 2), ("supporting_context", 2, 1),
    ):
        if not _text_rows(getattr(request, name), width, unique_key_width=key_width):
            raise ValueError(f"v2 review request {name} is invalid")
    if (not request.doc_hashes
            or any(not _sha_value(digest) for _name, digest in
                   request.doc_hashes + request.prior_doc_hashes)):
        raise ValueError("v2 document identity is invalid")
    documents = {name for name, _digest_value in request.doc_hashes}
    if (any(doc not in documents for doc, _heading in request.selected_sections)
            or any(doc not in documents for doc, _heading in request.unresolved_sections)):
        raise ValueError("v2 selected section has an unknown document")
    if request.mode == "full":
        if (not request.full_documents or request.section_documents
                or request.reference_sections
                or request.doc_hashes != tuple(
                    (doc, _sha(text)) for doc, text in request.full_documents)):
            raise ValueError("v2 full document payload does not match its identity")
    elif (request.full_documents or not request.section_documents
          or tuple(row[:2] for row in request.section_documents) != request.selected_sections):
        raise ValueError("v2 incremental target payload is invalid")
    targets = set(request.selected_sections)
    references = {row[:2] for row in request.reference_sections}
    if (targets.intersection(references)
            or any(doc not in documents for doc, _heading in references)):
        raise ValueError("v2 targets and references are not disjoint current sections")
    proof = request.scope_proof
    if (not isinstance(proof, dict) or set(proof) != _V2_PROOF_FIELDS
            or type(proof["schema_version"]) is not int or proof["schema_version"] != 1
            or proof["algorithm"] != "directed-id-impact-v1"
            or proof["boundary_kind"] != "declared-id-boundary"
            or not _sha_value(proof["boundary_evidence_digest"])
            or not _sha_value(proof["current_snapshot_digest"])
            or not _reason_codes(proof["reason_codes"])
            or (proof["baseline_snapshot_digest"] is not None
                and not _sha_value(proof["baseline_snapshot_digest"]))
            or (request.mode == "incremental" and proof["baseline_snapshot_digest"] is None)):
        raise ValueError("v2 scope proof identity is invalid")
    for name, expected in (("target_reasons", targets), ("reference_reasons", references)):
        rows = proof[name]
        if (not isinstance(rows, list)
                or any(not isinstance(row, dict)
                       or set(row) != {"document", "heading", "reason_codes"}
                       or not isinstance(row["document"], str)
                       or not isinstance(row["heading"], str)
                       or not _reason_codes(row["reason_codes"]) for row in rows)
                or len(rows) != len(expected)
                or {(row["document"], row["heading"]) for row in rows} != expected):
            raise ValueError("v2 scope proof reasons do not cover the actual input")
    bindings = proof["issue_bindings"]
    if not isinstance(bindings, list):
        raise ValueError("v2 issue bindings are invalid")
    seen = set()
    for row in bindings:
        if (not isinstance(row, dict) or set(row) != {"fingerprint", "state", "section_keys"}
                or not _sha_value(row["fingerprint"]) or row["fingerprint"] in seen
                or not isinstance(row["state"], str)
                or row["state"] not in {"OPEN", "FIXED", "VERIFIED", "REOPENED", "BLOCKED"}):
            raise ValueError("v2 unresolved issue binding is invalid")
        seen.add(row["fingerprint"])
        keys = row["section_keys"]
        if keys is None:
            if request.mode != "full":
                raise ValueError("unlocated issues require full input")
        elif (not isinstance(keys, list) or not keys
              or any(not isinstance(key, list) or len(key) != 2
                     or any(not isinstance(cell, str) or not cell for cell in key)
                     or tuple(key) not in targets for key in keys)
              or len({tuple(key) for key in keys}) != len(keys)):
            raise ValueError("v2 issue location is outside the target input")
    if (request.input_digest != _digest(_v2_input_payload(request))
            or request.request_digest != _digest(_v2_request_payload(request))):
        raise ValueError("v2 review request integrity check failed")


def load_versioned_review_request(raw: bytes) -> ReviewRequest | ReviewRequestV2:
    """Dispatch explicit versions; the original v1 decoder stays strict."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review request is malformed") from exc
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise ValueError("review request has no supported explicit version")
    if value["schema_version"] == 1:
        return load_review_request(raw)
    if (value["schema_version"] != 2
            or set(value) != set(ReviewRequestV2.__dataclass_fields__)):
        raise ValueError("review request has unsupported version or fields")
    for name, width in (("doc_hashes", 2), ("selected_sections", 2),
                        ("full_documents", 2), ("section_documents", 3),
                        ("supporting_context", 2), ("prior_doc_hashes", 2),
                        ("unresolved_sections", 2), ("reference_sections", 3)):
        rows = value[name]
        if (not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != width for row in rows)):
            raise ValueError(f"v2 review request {name} rows are invalid")
        value[name] = tuple(tuple(row) for row in rows)
    request = ReviewRequestV2(**value)
    _validate_v2_request(request)
    if raw != review_request_bytes(request):
        raise ValueError("v2 review request is not canonical JSON")
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


def review_progress_digest(request: ReviewRequestV2, current: ReviewSnapshot,
                           bindings: tuple[dict, ...], external_ids: tuple[str, ...],
                           documents: Mapping[str, str], section_texts: Mapping[SectionKey, str],
                           claims: tuple = (), locations: Mapping | None = None) -> str:
    """Bind related evidence, not revision labels or unrelated body edits."""
    evidence = _full_boundary_evidence(current, documents,
                                       dict(request.supporting_context))
    keys = tuple(sorted({tuple(key) for row in bindings
                         for key in (row["section_keys"] or [])}))
    targets, references, reason = scoped_sections(current, current, evidence=evidence,
        section_texts=dict(section_texts), unresolved_sections=keys, external_ids=external_ids)
    unknown = any(row["section_keys"] is None for row in bindings)
    selected = set(targets) | set(references)
    rows = tuple((item.document, item.heading, item.digest) for item in current.sections
                 if unknown or reason is not None or item.key in selected)
    return _digest({"stage": current.stage, "dependency": current.dependency_fingerprint,
        "related_sections": rows, "bindings": bindings, "claims": claims,
        "locations": locations or {}})


def review_issue_locations(
    request: ReviewRequestV2, receipt_text: str, current: ReviewSnapshot,
    known_issue_sections: Mapping[str, tuple[SectionKey, ...] | None],
) -> dict[str, tuple[SectionKey, ...]]:
    """Validate Reviewer location observations, never create Author claims."""
    receipt = json.loads(receipt_text)
    expected = json.loads(reviewer_receipt(request))
    if not isinstance(receipt, dict) or set(receipt) != set(expected):
        raise ValueError("v2 receipt fields do not match the current input")
    rows = receipt["issue_locations"]
    if {**receipt, "issue_locations": []} != expected or not isinstance(rows, list):
        raise ValueError("v2 receipt identity is invalid")
    if not rows:
        return {}
    if (request.mode != "full" or current.stage != request.stage
            or current.doc_hashes != request.doc_hashes
            or _sha(review_snapshot_bytes(current).decode("utf-8")) !=
                request.scope_proof["current_snapshot_digest"]):
        raise ValueError("Reviewer location needs the current full snapshot")
    evidence = _full_boundary_evidence(current, dict(request.full_documents),
                                       dict(request.supporting_context))
    owners = {(doc, heading): owner for doc, heading, owner in evidence["section_owners"]}
    sections = {item.key: item for item in current.sections}
    located: dict[str, list[SectionKey]] = {}
    order = []
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) != {"fingerprint", "document", "heading", "section_digest"}
                or not _sha_value(row["fingerprint"])
                or row["fingerprint"] not in known_issue_sections
                or not isinstance(row["document"], str) or not isinstance(row["heading"], str)):
            raise ValueError("Reviewer location is not bound to an existing issue")
        key = (row["document"], row["heading"])
        # Plans currently have no F/D owner frontier and are always reviewed
        # in full. Their exact task/table/global section bytes are legitimate
        # locations, not permission to admit a scoped or partial plan review.
        if (key not in sections or (request.stage != "计划" and owners.get(key) is None)
                or row["section_digest"] != sections[key].digest):
            raise ValueError("Reviewer location is not a current owned section")
        order.append((row["fingerprint"], *key))
        located.setdefault(row["fingerprint"], []).append(key)
    if order != sorted(set(order)):
        raise ValueError("Reviewer locations must be unique and sorted")
    for fingerprint, keys in located.items():
        known = known_issue_sections[fingerprint]
        if known is not None and not set(known).issubset(keys):
            raise ValueError("Reviewer location omitted a known affected section")
    return {fingerprint: tuple(keys) for fingerprint, keys in located.items()}


def validate_review_result(
    request: ReviewRequest, result_text: str, receipt_text: str, *,
    current_snapshot: ReviewSnapshot | None = None,
    known_issue_sections: Mapping[str, tuple[SectionKey, ...] | None] | None = None,
) -> ReviewValidation:
    """Validate receipt and issues; incremental failure requests full resend."""
    expected_receipt = reviewer_receipt(request)
    try:
        canonical_receipt = json.dumps(
            json.loads(receipt_text), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
    except (TypeError, json.JSONDecodeError):
        canonical_receipt = ""
    if isinstance(request, ReviewRequestV2) and current_snapshot is not None:
        try:
            review_issue_locations(request, receipt_text, current_snapshot,
                                   known_issue_sections or {})
            canonical_receipt = expected_receipt
        except (KeyError, TypeError, ValueError):
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
