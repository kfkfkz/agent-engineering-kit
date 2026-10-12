"""Declared-ID review boundaries; pure planning, never approval or persistence."""
from __future__ import annotations

import re
from collections import deque

from .incremental import ReviewSnapshot, plan_full_review

MARKER = "<!-- aek-review-boundary: declared-id-v1 -->"


def marker_count(text: str) -> int:
    count = 0
    fence, size = "", 0
    for line in text.splitlines():
        token = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if token:
            value = token.group(1)
            if not fence:
                fence, size = value[0], len(value)
            elif value[0] == fence and len(value) >= size:
                fence, size = "", 0
            continue
        if not fence and line.strip() == MARKER:
            count += 1
    return count


def scoped_sections(previous: ReviewSnapshot, current: ReviewSnapshot, *,
                    evidence: dict, section_texts: dict,
                    unresolved_sections: tuple = (), external_ids: tuple = ()):
    """Return target/reference keys or a fixed full-fallback reason.

    Only the explicitly declared ownership/reference model is checked. Global
    constraints remain actual context; this is not natural-language proof.
    """
    plan_full_review(previous)
    plan_full_review(current)
    if (not previous.parse_complete or not current.parse_complete
            or previous.unknown_edges or current.unknown_edges):
        return (), (), "PARSER_OR_EDGE_GAP"
    if previous.stage != current.stage or previous.dependency_fingerprint != current.dependency_fingerprint:
        return (), (), "DEPENDENCY_CHANGED"
    if ({doc for doc, _hash in previous.doc_hashes} != {doc for doc, _hash in current.doc_hashes}
            or any(role == "UNKNOWN" for _doc, role in evidence["participating_document_roles"])
            or any(count != 1 for _doc, count in evidence["document_markers"])):
        return (), (), "UNKNOWN_SCOPE"
    before = {section.key: section for section in previous.sections}
    after = {section.key: section for section in current.sections}
    if (before.keys() != after.keys()
            or any(before[key].defines_ids != after[key].defines_ids
                   or before[key].uses_ids != after[key].uses_ids for key in after)):
        return (), (), "BOUNDARY_CHANGED"
    owners = {(doc, heading): owner for doc, heading, owner in evidence["section_owners"]}
    globals_ = set(evidence["global_sections"])
    for document, _count in evidence["document_markers"]:
        if sum(marker_count(section_texts[key]) for key in globals_ if key[0] == document) != 1:
            return (), (), "UNKNOWN_SCOPE"
    if any(before[key].digest != after[key].digest for key in globals_):
        return (), (), "GLOBAL_CONSTRAINT_CHANGED"
    if (not isinstance(unresolved_sections, tuple)
            or any(key not in after for key in unresolved_sections)):
        return (), (), "UNKNOWN_ISSUE_LOCATION"
    providers = {item: section.key for section in current.sections for item in section.defines_ids}
    if any(ref not in providers and ref not in external_ids
           for section in current.sections for ref in section.uses_ids):
        return (), (), "UNTRUSTED_REFERENCE"
    changed = {key for key in after if before[key].digest != after[key].digest}
    changed_docs = {doc for doc, digest in current.doc_hashes
                    if digest != dict(previous.doc_hashes)[doc]}
    if changed_docs != {doc for doc, _heading in changed}:
        return (), (), "BOUNDARY_CHANGED"
    seeds = changed | set(unresolved_sections)
    if any(owners.get(key) is None for key in seeds):
        return (), (), "UNKNOWN_SCOPE"
    groups = {}
    for key, owner in owners.items():
        if owner is not None:
            groups.setdefault(owner, set()).add(key)
    def expand(keys):
        return set(keys).union(*(groups.get(owners.get(key), set()) for key in keys))
    targets = expand(seeds)
    outgoing = {key: set() for key in after}
    incoming = {key: set() for key in after}
    for provider, consumer in evidence["resolved_reference_edges"]:
        outgoing[provider].add(consumer)
        incoming[consumer].add(provider)
    queue = deque(targets)
    while queue:
        key = queue.popleft()
        related = expand(outgoing[key]) - targets
        if related.intersection(globals_):
            # A changed contract consumed by global prose has shared impact.
            return (), (), "UNKNOWN_SCOPE"
        targets.update(related)
        queue.extend(related)
    references = globals_ - targets
    queue = deque(targets)
    seen = set(targets)
    while queue:
        key = queue.popleft()
        related = expand(incoming[key]) - seen
        references.update(related - targets)
        seen.update(related)
        queue.extend(related)
    return tuple(sorted(targets)), tuple(sorted(references)), None
