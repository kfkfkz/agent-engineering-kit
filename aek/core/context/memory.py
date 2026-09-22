"""Metadata-first memory selection; no source body is carried in CandidateRef."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@dataclass(frozen=True)
class CandidateRef:
    path: str
    score: float
    generation: str
    content_digest: str
    anchor: str

    def __post_init__(self) -> None:
        if (not isinstance(self.path, str) or not self.path.startswith("docs/memory/")
                or "\\" in self.path or "\0" in self.path
                or any(part in {"", ".", ".."} for part in self.path.split("/"))
                or not self.path.endswith(".md")):
            raise ValueError("candidate path must be a safe memory Markdown path")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) \
                or not math.isfinite(self.score) or self.score < 0:
            raise ValueError("candidate score must be finite and non-negative")
        if not isinstance(self.generation, str) or not _GENERATION.fullmatch(self.generation):
            raise ValueError("candidate generation is invalid")
        if not isinstance(self.content_digest, str) or not _DIGEST.fullmatch(self.content_digest):
            raise ValueError("candidate content digest must be SHA-256")
        if (not isinstance(self.anchor, str) or not self.anchor.strip()
                or len(self.anchor) > 200 or any(ord(c) < 32 for c in self.anchor)):
            raise ValueError("candidate anchor must be short metadata")


@dataclass(frozen=True)
class CandidateSet:
    generation: str
    candidates: tuple[CandidateRef, ...]
    has_more: bool
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.generation, str) or not _GENERATION.fullmatch(self.generation):
            raise ValueError("invalid candidate generation")
        if not isinstance(self.candidates, tuple) or any(
                not isinstance(item, CandidateRef) or item.generation != self.generation
                for item in self.candidates):
            raise ValueError("candidate generation mismatch")
        if len({item.path for item in self.candidates}) != len(self.candidates):
            raise ValueError("duplicate candidate path")
        if type(self.has_more) is not bool or (self.has_more and not self.candidates):
            raise ValueError("invalid candidate pagination")
        if self.source not in {"zvec", "fallback"}:
            raise ValueError("unknown candidate source")


@dataclass(frozen=True)
class ExpansionSelection:
    selected: tuple[CandidateRef, ...]
    needs_more_candidates: bool
    reason: str


def select_expansions(
    batch: CandidateSet, *, limit: int, risk_required: bool,
) -> ExpansionSelection:
    if not isinstance(batch, CandidateSet) or type(limit) is not int or limit < 0:
        raise ValueError("invalid candidate batch or expansion limit")
    if type(risk_required) is not bool:
        raise ValueError("risk_required must be boolean")
    ranked = sorted(batch.candidates, key=lambda item: (-item.score, item.path))
    selected = tuple(ranked[:limit])
    # Truncated candidates with a mandatory risk cannot justify a negative
    # conclusion merely because the budget stopped expansion.
    needs_more = risk_required and (batch.has_more or len(ranked) > limit)
    reason = "RISK_EXPANSION_REQUIRED" if needs_more else (
        "NO_MATCH" if not ranked else "BUDGETED_EXPANSION")
    return ExpansionSelection(selected, needs_more, reason)
