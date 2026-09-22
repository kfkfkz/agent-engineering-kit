"""Fail-closed dependency closure for an advisory incremental review input.

The caller must supply parsed, full-document snapshots and a dependency
fingerprint covering Registry, templates, rubric, policy, upstream gates and
plan. This module never edits gate credentials or accepts review verdicts.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


_SHA = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^(?:A|F|D|T|TC-)[0-9]+$")


@dataclass(frozen=True)
class SectionSnapshot:
    document: str
    heading: str
    digest: str
    defines_ids: tuple[str, ...]
    uses_ids: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return self.document, self.heading


@dataclass(frozen=True)
class ReviewSnapshot:
    stage: str
    doc_hashes: tuple[tuple[str, str], ...]
    sections: tuple[SectionSnapshot, ...]
    dependency_fingerprint: str
    parse_complete: bool
    unknown_edges: bool


@dataclass(frozen=True)
class ReviewInputPlan:
    mode: str
    selected_sections: tuple[tuple[str, str], ...]
    doc_hashes: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...]
    receipt_digest: str


def _valid(snapshot: ReviewSnapshot) -> bool:
    if (not isinstance(snapshot, ReviewSnapshot)
            or not isinstance(snapshot.stage, str) or not snapshot.stage
            or not isinstance(snapshot.dependency_fingerprint, str)
            or not _SHA.fullmatch(snapshot.dependency_fingerprint)
            or type(snapshot.parse_complete) is not bool
            or type(snapshot.unknown_edges) is not bool
            or not isinstance(snapshot.doc_hashes, tuple)
            or not snapshot.doc_hashes
            or not isinstance(snapshot.sections, tuple)):
        return False
    docs: set[str] = set()
    for pair in snapshot.doc_hashes:
        if (not isinstance(pair, tuple) or len(pair) != 2
                or not isinstance(pair[0], str) or not pair[0]
                or not isinstance(pair[1], str) or not _SHA.fullmatch(pair[1])
                or pair[0] in docs):
            return False
        docs.add(pair[0])
    keys: set[tuple[str, str]] = set()
    definitions: set[str] = set()
    for section in snapshot.sections:
        if (not isinstance(section, SectionSnapshot)
                or not isinstance(section.document, str)
                or section.document not in docs
                or not isinstance(section.heading, str) or not section.heading
                or not isinstance(section.digest, str)
                or not _SHA.fullmatch(section.digest)
                or section.key in keys):
            return False
        keys.add(section.key)
        for values in (section.defines_ids, section.uses_ids):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not _ID.fullmatch(item)
                           for item in values)
                    or len(values) != len(set(values))):
                return False
        if definitions.intersection(section.defines_ids):
            return False
        definitions.update(section.defines_ids)
    return True


def _receipt(
    mode: str, sections: tuple[tuple[str, str], ...], current: ReviewSnapshot,
    reasons: tuple[str, ...], reviewed: tuple[tuple[str, str], ...],
) -> ReviewInputPlan:
    payload = {"mode": mode, "selected_sections": sections,
               "doc_hashes": current.doc_hashes,
               "dependency_fingerprint": current.dependency_fingerprint,
               "reviewed_doc_hashes": reviewed, "reasons": reasons}
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return ReviewInputPlan(mode, sections, current.doc_hashes, reasons, digest)


def plan_full_review(
    current: ReviewSnapshot, *,
    reviewed_doc_hashes: tuple[tuple[str, str], ...] = (),
    reason: str = "FULL_REVIEW_REQUESTED",
) -> ReviewInputPlan:
    """Create a canonical full-review plan for a validated current snapshot."""
    if not _valid(current):
        raise ValueError("current review snapshot is not a complete typed snapshot")
    if not isinstance(reason, str) or not reason:
        raise ValueError("full review reason is required")
    return _receipt("full", tuple(section.key for section in current.sections),
                    current, (reason,), reviewed_doc_hashes)


def plan_incremental_review(
    previous: ReviewSnapshot, current: ReviewSnapshot, *,
    prior_review_was_full: bool,
    reviewed_doc_hashes: tuple[tuple[str, str], ...],
    unresolved_sections: tuple[tuple[str, str], ...] = (),
    external_ids: tuple[str, ...] = (),
    same_group_documents: tuple[str, ...] = (),
) -> ReviewInputPlan:
    if not _valid(current):
        raise ValueError("current review snapshot is not a complete typed snapshot")
    all_keys = tuple(section.key for section in current.sections)

    def full(reason: str) -> ReviewInputPlan:
        return plan_full_review(current, reviewed_doc_hashes=reviewed_doc_hashes,
                                reason=reason)

    if (not _valid(previous) or not previous.parse_complete
            or not current.parse_complete or previous.unknown_edges
            or current.unknown_edges):
        return full("PARSER_OR_EDGE_GAP")
    if type(prior_review_was_full) is not bool or not prior_review_was_full:
        return full("NO_FULL_REVIEW_BASELINE")
    if reviewed_doc_hashes != previous.doc_hashes:
        return full("PRIOR_REVIEW_SNAPSHOT_MISMATCH")
    if (previous.stage != current.stage
            or previous.dependency_fingerprint != current.dependency_fingerprint):
        return full("DEPENDENCY_FINGERPRINT_CHANGED")
    if set(previous.doc_hashes) == set(current.doc_hashes):
        changed_docs: set[str] = set()
    elif {doc for doc, _ in previous.doc_hashes} != {
            doc for doc, _ in current.doc_hashes}:
        return full("DOCUMENT_SET_CHANGED")
    else:
        before_docs = dict(previous.doc_hashes)
        changed_docs = {doc for doc, digest in current.doc_hashes
                        if digest != before_docs[doc]}
    before = {section.key: section for section in previous.sections}
    after = {section.key: section for section in current.sections}
    if set(before) != set(after):
        return full("SECTION_BOUNDARY_CHANGED")
    changed = {key for key in after if before[key] != after[key]}
    if any(doc not in {key[0] for key in changed} for doc in changed_docs):
        return full("UNEXPLAINED_DOCUMENT_CHANGE")
    if changed and not changed_docs:
        return full("SECTION_CHANGED_WITHOUT_DOCUMENT_HASH")
    if (not isinstance(external_ids, tuple) or any(
            not isinstance(item, str) or not _ID.fullmatch(item)
            for item in external_ids) or len(external_ids) != len(set(external_ids))):
        return full("EXTERNAL_ID_INDEX_INVALID")
    if (not isinstance(unresolved_sections, tuple)
            or any(key not in after for key in unresolved_sections)):
        return full("UNRESOLVED_ISSUE_LOCATION_UNKNOWN")
    current_documents = {doc for doc, _digest_value in current.doc_hashes}
    if (not isinstance(same_group_documents, tuple)
            or len(same_group_documents) != len(set(same_group_documents))
            or any(not isinstance(doc, str) or doc not in current_documents
                   for doc in same_group_documents)):
        return full("ARTIFACT_GROUP_INVALID")
    current_providers = {item: section.key for section in current.sections
                         for item in section.defines_ids}
    previous_providers = {item: section.key for section in previous.sections
                          for item in section.defines_ids}
    for sections, providers in ((current.sections, current_providers),
                                (previous.sections, previous_providers)):
        if any(ref not in providers and ref not in external_ids
               for section in sections for ref in section.uses_ids):
            return full("UNRESOLVED_REFERENCE")
    closure = set(changed) | set(unresolved_sections)
    changed_group_docs = {doc for doc, _heading in changed}
    if changed_group_docs.intersection(same_group_documents):
        closure.update(section.key for section in current.sections
                       if section.document in same_group_documents
                       and section.document not in changed_group_docs)
    neighbors: dict[tuple[str, str], set[tuple[str, str]]] = {
        key: set() for key in after}
    for sections, providers in ((current.sections, current_providers),
                                (previous.sections, previous_providers)):
        for section in sections:
            for ref in section.uses_ids:
                provider = providers.get(ref)
                if provider is not None:
                    neighbors[section.key].add(provider)
                    neighbors[provider].add(section.key)
    queue = list(closure)
    while queue:
        key = queue.pop()
        for related in neighbors[key] - closure:
            closure.add(related)
            queue.append(related)
    selected = tuple(key for key in all_keys if key in closure)
    return _receipt("incremental", selected, current,
                    ("PROVEN_DEPENDENCY_CLOSURE",), reviewed_doc_hashes)
