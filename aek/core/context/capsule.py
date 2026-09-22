"""Discardable, claim-source-bound context summaries.

Claims are navigation aids, not evidence or gate credentials. Rebuild a capsule
when any source digest changes; never reuse it to prove a negative fact.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = {"routing", "design", "execution", "closeout"}


@dataclass(frozen=True)
class CapsuleSource:
    path: str
    digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.path, str) or not self.path
                or self.path.startswith("/") or "\\" in self.path or "\0" in self.path
                or any(part in {"", ".", ".."} for part in self.path.split("/"))):
            raise ValueError("capsule source path must be repository-relative")
        if not isinstance(self.digest, str) or not _DIGEST.fullmatch(self.digest):
            raise ValueError("capsule source digest must be SHA-256")


@dataclass(frozen=True)
class CapsuleClaim:
    claim_id: str
    summary: str
    source_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.claim_id):
            raise ValueError("invalid capsule claim ID")
        if not isinstance(self.summary, str) or not self.summary.strip() or len(self.summary) > 1000:
            raise ValueError("capsule claim summary must be bounded")
        if (not isinstance(self.source_paths, tuple) or not self.source_paths
                or len(set(self.source_paths)) != len(self.source_paths)):
            raise ValueError("capsule claim needs unique source paths")


@dataclass(frozen=True)
class ContextCapsule:
    schema_version: int
    capsule_id: str
    subject_digest: str
    stage: str
    profile_version: int
    sources: tuple[CapsuleSource, ...]
    claims: tuple[CapsuleClaim, ...]


def capsule_as_dict(capsule: ContextCapsule) -> dict[str, object]:
    if not isinstance(capsule, ContextCapsule):
        raise ValueError("capsule serialization needs a ContextCapsule")
    return {
        "schema_version": capsule.schema_version,
        "capsule_id": capsule.capsule_id,
        "subject_digest": capsule.subject_digest,
        "stage": capsule.stage,
        "profile_version": capsule.profile_version,
        "sources": [{"path": row.path, "digest": row.digest}
                    for row in capsule.sources],
        "claims": [{"claim_id": row.claim_id, "summary": row.summary,
                    "source_paths": list(row.source_paths)}
                   for row in capsule.claims],
    }


def build_capsule(
    subject_digest: str, stage: str, profile_version: int,
    sources: tuple[CapsuleSource, ...], claims: tuple[CapsuleClaim, ...],
) -> ContextCapsule:
    if not isinstance(subject_digest, str) or not _DIGEST.fullmatch(subject_digest):
        raise ValueError("capsule subject digest must be SHA-256")
    if stage not in _STAGES or type(profile_version) is not int or profile_version < 1:
        raise ValueError("invalid capsule stage or profile version")
    if (not isinstance(sources, tuple) or any(not isinstance(s, CapsuleSource) for s in sources)
            or len({s.path for s in sources}) != len(sources)):
        raise ValueError("capsule sources must be unique")
    if (not isinstance(claims, tuple) or any(not isinstance(c, CapsuleClaim) for c in claims)
            or len({c.claim_id for c in claims}) != len(claims)):
        raise ValueError("capsule claims must be unique")
    source_paths = {s.path for s in sources}
    if any(not set(c.source_paths) <= source_paths for c in claims):
        raise ValueError("capsule claim cites an absent source")
    ordered_sources = tuple(sorted(sources, key=lambda s: s.path))
    ordered_claims = tuple(sorted(claims, key=lambda c: c.claim_id))
    payload = {
        "schema_version": 1, "subject_digest": subject_digest,
        "stage": stage, "profile_version": profile_version,
        "sources": [{"path": s.path, "digest": s.digest} for s in ordered_sources],
        "claims": [{"claim_id": c.claim_id,
                    "summary": unicodedata.normalize("NFC", c.summary),
                    "source_paths": sorted(c.source_paths)} for c in ordered_claims],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return ContextCapsule(1, hashlib.sha256(canonical).hexdigest(),
                          subject_digest, stage, profile_version,
                          ordered_sources, ordered_claims)


def capsule_is_current(
    capsule: ContextCapsule, current_sources: tuple[CapsuleSource, ...],
) -> bool:
    if not isinstance(capsule, ContextCapsule):
        raise ValueError("invalid capsule")
    if (not isinstance(current_sources, tuple)
            or any(not isinstance(s, CapsuleSource) for s in current_sources)):
        raise ValueError("invalid current source set")
    try:
        expected = build_capsule(
            capsule.subject_digest, capsule.stage, capsule.profile_version,
            capsule.sources, capsule.claims)
    except ValueError:
        return False
    return (capsule.schema_version == 1 and capsule.capsule_id == expected.capsule_id
            and capsule.sources == tuple(sorted(current_sources, key=lambda s: s.path)))
