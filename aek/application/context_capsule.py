"""Build or reuse source-bound Context Capsules."""
from __future__ import annotations

from dataclasses import dataclass

from aek.core.context.capsule import (
    CapsuleClaim, CapsuleSource, ContextCapsule, build_capsule,
    capsule_is_current,
)


@dataclass(frozen=True)
class CapsuleResult:
    capsule: ContextCapsule
    reused: bool


def get_or_rebuild_capsule(
    store, *, subject_digest: str, stage: str, profile_version: int,
    sources: tuple[CapsuleSource, ...], claims: tuple[CapsuleClaim, ...],
) -> CapsuleResult:
    """Reuse only the exact canonical entry; corrupt entries are replaced."""
    desired = build_capsule(
        subject_digest, stage, profile_version, sources, claims)
    try:
        cached = store.load(desired.capsule_id)
    except ValueError:
        cached = None
    if cached is not None and capsule_is_current(cached, sources):
        return CapsuleResult(cached, True)
    store.publish(desired)
    return CapsuleResult(desired, False)
