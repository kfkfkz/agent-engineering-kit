"""Fail-closed Markdown snapshots for ReviewService.

This parser intentionally recognizes a small deterministic subset. Ambiguous
structure does not become a guessed dependency graph; it marks the snapshot
incomplete so Core selects a full review.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping

from aek.core.review.incremental import ReviewSnapshot, SectionSnapshot


_SHA = re.compile(r"^[0-9a-f]{64}$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_ID = re.compile(r"(?<![A-Z0-9-])(?:TC-[0-9]+|[AFDT][0-9]+)(?![A-Z0-9-])")
_HEADING_DEF = re.compile(r"^#{1,6}[ \t]+((?:TC-)?[AFDT]?[0-9]+)[：:]", re.M)
_ACCEPT_DEF = re.compile(r"^[ \t]*[-*][ \t]+\[[ xX]\][ \t]+(A[0-9]+)[：:]", re.M)
_TABLE_DEF = re.compile(r"^[ \t]*\|[ \t]*(TC-[0-9]+)[ \t]*\|", re.M)


@dataclass(frozen=True)
class ReviewMaterial:
    snapshot: ReviewSnapshot
    documents: tuple[tuple[str, str], ...]
    section_texts: tuple[tuple[str, str, str], ...]

    def document_map(self) -> dict[str, str]:
        return dict(self.documents)

    def section_map(self) -> dict[tuple[str, str], str]:
        return {(doc, heading): text for doc, heading, text in self.section_texts}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _definitions(text: str) -> tuple[str, ...]:
    values = set(_HEADING_DEF.findall(text))
    values.update(_ACCEPT_DEF.findall(text))
    values.update(_TABLE_DEF.findall(text))
    return tuple(sorted(values))


def extract_trace_ids(text: str) -> tuple[str, ...]:
    """Return normalized trace IDs for declaring trusted external references."""
    if not isinstance(text, str):
        raise ValueError("trace source must be text")
    return tuple(sorted(set(_ID.findall(text))))


def parse_review_documents(
    stage: str, documents: Mapping[str, str], dependency_fingerprint: str,
) -> ReviewMaterial:
    if (not isinstance(stage, str) or not stage
            or not isinstance(documents, Mapping) or not documents
            or not isinstance(dependency_fingerprint, str)
            or not _SHA.fullmatch(dependency_fingerprint)
            or any(not isinstance(name, str) or not name
                   or not isinstance(text, str)
                   for name, text in documents.items())):
        raise ValueError("invalid Markdown review source")

    parsed_sections: list[SectionSnapshot] = []
    section_texts: list[tuple[str, str, str]] = []
    parse_complete = True
    unknown_edges = False
    defined_globally: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()

    for document in sorted(documents):
        text = documents[document]
        offsets: list[tuple[int, int, str]] = []
        stack: list[tuple[int, str]] = []
        position = 0
        fence_char = ""
        fence_size = 0
        for line in text.splitlines(keepends=True):
            fence = _FENCE.match(line)
            if fence:
                token = fence.group(1)
                if not fence_char:
                    fence_char, fence_size = token[0], len(token)
                elif token[0] == fence_char and len(token) >= fence_size:
                    fence_char, fence_size = "", 0
                position += len(line)
                continue
            if not fence_char:
                heading = _HEADING.match(line)
                if heading:
                    level = len(heading.group(1))
                    title = heading.group(2).strip()
                    if not title:
                        parse_complete = False
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, title))
                    logical = " / ".join(item[1] for item in stack)
                    offsets.append((position, level, logical))
            position += len(line)
        if fence_char:
            parse_complete = False
        if not offsets:
            offsets.append((0, 1, "<whole-document>"))
            parse_complete = False
        elif text[:offsets[0][0]].strip():
            offsets.insert(0, (0, 0, "<preamble>"))

        for index, (start, _level, logical) in enumerate(offsets):
            end = offsets[index + 1][0] if index + 1 < len(offsets) else len(text)
            body = text[start:end]
            key = (document, logical)
            if key in seen_keys:
                parse_complete = False
                logical = f"{logical} <duplicate-{index + 1}>"
                key = (document, logical)
            seen_keys.add(key)
            raw_definitions = _definitions(body)
            duplicates = defined_globally.intersection(raw_definitions)
            if duplicates:
                parse_complete = False
                unknown_edges = True
            definitions = tuple(item for item in raw_definitions
                                if item not in defined_globally)
            defined_globally.update(definitions)
            references = tuple(sorted(set(_ID.findall(body)) - set(definitions)))
            parsed_sections.append(SectionSnapshot(
                document, logical, _sha(body), definitions, references))
            section_texts.append((document, logical, body))

    snapshot = ReviewSnapshot(
        stage,
        tuple((name, _sha(documents[name])) for name in sorted(documents)),
        tuple(parsed_sections), dependency_fingerprint,
        parse_complete, unknown_edges,
    )
    return ReviewMaterial(
        snapshot,
        tuple((name, documents[name]) for name in sorted(documents)),
        tuple(section_texts),
    )
