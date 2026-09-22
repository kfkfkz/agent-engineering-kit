"""Budgeted candidate-first Memory recall with actual-delivery telemetry."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from aek.application.context_capsule import get_or_rebuild_capsule
from aek.application.context_telemetry import (
    record_context_delivery, report_for_budget,
)
from aek.core.context.budget import ContextBudget
from aek.core.context.capsule import (
    CapsuleClaim, CapsuleSource, ContextCapsule, capsule_as_dict,
)
from aek.core.context.memory import CandidateSet, select_expansions
from aek.core.context.telemetry import ContextReport


@dataclass(frozen=True)
class ExpandedMemory:
    path: str
    content_digest: str
    content: str


@dataclass(frozen=True)
class MemoryRecallResult:
    candidates: CandidateSet
    expanded: tuple[ExpandedMemory, ...]
    needs_more_candidates: bool
    reason: str
    capsule: ContextCapsule | None
    capsule_reused: bool
    report: ContextReport


def recall_context(
    repo: Path, query: str, budget: ContextBudget, ledger, *,
    session_id: str, subject_digest: str, risk_required: bool,
    candidate_search: Callable[[Path, str], CandidateSet],
    candidate_read: Callable[[Path, object, str], str],
    capsule_store=None, profile_version: int = 1,
) -> MemoryRecallResult:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("memory query is required")
    batch = candidate_search(repo, query)
    if not isinstance(batch, CandidateSet):
        raise ValueError("candidate adapter returned an invalid batch")
    if len(batch.candidates) > budget.memory_candidate_limit:
        raise ValueError("candidate adapter exceeded the route budget")
    metadata = json.dumps({
        "generation": batch.generation, "source": batch.source,
        "has_more": batch.has_more,
        "candidates": [asdict(item) for item in batch.candidates],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record_context_delivery(
        ledger, budget, metadata, source="memory", source_kind="memory",
        channel="memory_candidate", event_purpose="target_evidence",
        expansion_purpose="target_evidence", session_id=session_id,
        subject_digest=subject_digest,
        logical_path="generated/memory-candidates.json")
    selection = select_expansions(
        batch, limit=budget.memory_expand_limit, risk_required=risk_required)
    capsule = None
    capsule_reused = False
    if budget.route in {"standard", "initiative"}:
        if capsule_store is None:
            raise ValueError("standard and initiative recall require a capsule store")
        sources = tuple(CapsuleSource(ref.path, ref.content_digest)
                        for ref in selection.selected)
        claims = tuple(CapsuleClaim(
            f"memory-{index + 1}", ref.anchor, (ref.path,))
            for index, ref in enumerate(selection.selected))
        capsule_result = get_or_rebuild_capsule(
            capsule_store, subject_digest=subject_digest, stage=budget.stage,
            profile_version=profile_version, sources=sources, claims=claims)
        capsule = capsule_result.capsule
        capsule_reused = capsule_result.reused
        capsule_text = json.dumps(
            capsule_as_dict(capsule), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
        record_context_delivery(
            ledger, budget, capsule_text, source="capsule",
            source_kind="capsule", channel="capsule_loader",
            event_purpose="target_evidence", expansion_purpose="target_evidence",
            session_id=session_id, subject_digest=subject_digest,
            logical_path=f"generated/context-capsules/{capsule.capsule_id}.json",
            cache_hit=capsule_reused)
    expanded = []
    for ref in selection.selected:
        content = candidate_read(repo, ref, batch.generation)
        record_context_delivery(
            ledger, budget, content, source="memory", source_kind="memory",
            channel="memory_open", event_purpose="target_evidence",
            expansion_purpose="target_evidence", session_id=session_id,
            subject_digest=subject_digest, logical_path=ref.path)
        expanded.append(ExpandedMemory(ref.path, ref.content_digest, content))
    observed = (("capsule_loader",) if capsule is not None else ()) + \
        ("memory_candidate",) + (("memory_open",) if expanded else ())
    expected = ("host_native", "memory_candidate", "memory_open") + (
        ("capsule_loader",) if capsule is not None else ())
    report = report_for_budget(
        ledger, budget, coverage="partial",
        expected_channels=expected,
        observed_channels=observed, bypass_detected=False)
    return MemoryRecallResult(
        batch, tuple(expanded), selection.needs_more_candidates,
        selection.reason, capsule, capsule_reused, report)
