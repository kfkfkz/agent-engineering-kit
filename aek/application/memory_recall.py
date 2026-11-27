"""Budgeted candidate-first Memory recall with actual-delivery telemetry."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from aek.application.context_capsule import get_or_rebuild_capsule
from aek.application.context_telemetry import (
    record_context_delivery,
    report_for_budget,
    validate_route_history,
)
from aek.core.context.budget import CONTEXT_PURPOSES, ContextBudget
from aek.core.context.capsule import (
    CapsuleClaim,
    CapsuleSource,
    ContextCapsule,
    capsule_as_dict,
)
from aek.core.context.lookup import (
    CapabilitySnapshot,
    KnowledgeLookupPlan,
    plan_knowledge_lookup,
)
from aek.core.context.memory import CandidateSet, is_memory_markdown, select_expansions
from aek.core.context.telemetry import (
    ContextChannelLimit,
    ContextReport,
    validate_context_session_id,
    validate_context_subject_digest,
)


@dataclass(frozen=True)
class ExpandedMemory:
    path: str
    content_digest: str
    content: str
    origin_ref: str | None = None


@dataclass(frozen=True)
class ReusedMemory:
    path: str
    content_digest: str
    origin_ref: str | None = None


def validate_known_digests(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError("known_digests must contain at most 16 SHA-256 values")
    return tuple(dict.fromkeys(validate_context_subject_digest(item) for item in value))


def validate_expand_paths(value: object) -> tuple[str, ...]:
    if (not isinstance(value, (list, tuple)) or len(value) > 16
            or not all(is_memory_markdown(item) for item in value)):
        raise ValueError("expand_paths must contain at most 16 repository-relative Memory Markdown paths")
    return tuple(dict.fromkeys(value))


@dataclass(frozen=True)
class MemoryRecallResult:
    candidates: CandidateSet
    expanded: tuple[ExpandedMemory, ...]
    needs_more_candidates: bool
    reason: str
    capsule: ContextCapsule | None
    capsule_reused: bool
    report: ContextReport
    selected_path: str
    reason_code: str
    capability_snapshot: dict[str, object]
    uncertainty: str
    delivered_bytes: int
    tool_calls: dict[str, int]
    reused: tuple[ReusedMemory, ...] = ()


def recall_context(
    repo: Path, query: str, budget: ContextBudget, ledger, *,
    session_id: str, subject_digest: str, risk_required: bool,
    candidate_search: Callable[[Path, str], CandidateSet],
    candidate_read: Callable[[Path, object, str], str],
    capsule_store=None, profile_version: int = 1,
    lookup_plan: KnowledgeLookupPlan | None = None,
    purpose: str = "target_evidence",
    known_digests: tuple[str, ...] = (),
    expand_paths: tuple[str, ...] | None = None,
) -> MemoryRecallResult:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("memory query is required")
    if not isinstance(purpose, str) or purpose not in CONTEXT_PURPOSES:
        raise ValueError("memory recall purpose must be one of: " + ", ".join(sorted(CONTEXT_PURPOSES)))
    validate_context_session_id(session_id)
    validate_context_subject_digest(subject_digest)
    known = set(validate_known_digests(known_digests))
    requested = None if expand_paths is None else frozenset(validate_expand_paths(expand_paths))
    history = ledger.read()
    validate_route_history(history, budget.route)
    previously_delivered = {
        (event.logical_path, event.source_digest) for event in history
        if event.channel == "memory_open" and event.delivered_bytes > 0
        and event.session_id == session_id and event.subject_digest == subject_digest
        and event.stage == budget.stage and event.route == budget.route
        and event.purpose == purpose and event.source_digest in known
    } if known else set()
    if lookup_plan is None:
        lookup_plan = plan_knowledge_lookup(CapabilitySnapshot(
            mcp_configured=False, mcp_visible=False, mcp_compatible=False,
            cli_available=True, bounded_text_available=True,
            evidence=("application:legacy-cli",)))
    if not isinstance(lookup_plan, KnowledgeLookupPlan):
        raise ValueError("lookup plan is invalid")
    if lookup_plan.selected_path == "stop":
        raise ValueError(f"lookup is stopped: {lookup_plan.reason_code}")
    batch = candidate_search(repo, query)
    if not isinstance(batch, CandidateSet):
        raise ValueError("candidate adapter returned an invalid batch")
    if len(batch.candidates) > budget.memory_candidate_limit:
        raise ValueError("candidate adapter exceeded the route budget")
    selection_batch = batch
    missing = False
    if requested is not None:
        matches = tuple(ref for ref in batch.candidates if ref.path in requested)
        missing = len(matches) < len(requested)
        selection_batch = replace(
            batch, candidates=matches,
            has_more=batch.has_more or missing or (risk_required and len(matches) < len(batch.candidates)))
    selection = select_expansions(
        selection_batch, limit=budget.memory_expand_limit, risk_required=risk_required)
    use_capsule = (budget.route in {"standard", "initiative"}
                   and (requested is None or bool(selection.selected)))
    if use_capsule and capsule_store is None:
        raise ValueError("standard and initiative recall require a capsule store")
    # Validate the candidate generation and sources before reporting delivery.
    bodies = tuple((ref, candidate_read(repo, ref, batch.generation))
                   for ref in selection.selected)
    metadata = json.dumps({
        "generation": batch.generation, "source": batch.source,
        "has_more": batch.has_more,
        "candidates": [{key: value for key, value in asdict(item).items() if value is not None}
                       for item in batch.candidates],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record_context_delivery(
        ledger, budget, metadata, source="memory", source_kind="memory",
        channel="memory_candidate", event_purpose=purpose,
        expansion_purpose=purpose, session_id=session_id,
        subject_digest=subject_digest,
        logical_path="generated/memory-candidates.json")
    expanded = []
    reused = []
    admitted = []
    deferred = False
    for ref, content in bodies:
        if (ref.path, ref.content_digest) in previously_delivered:
            reused.append(ReusedMemory(ref.path, ref.content_digest, ref.origin_ref))
        else:
            try:
                record_context_delivery(
                    ledger, budget, content, source="memory", source_kind="memory",
                    channel="memory_open", event_purpose=purpose,
                    expansion_purpose=purpose, session_id=session_id,
                    subject_digest=subject_digest, logical_path=ref.path,
                    stage_channel_byte_limit=budget.memory_byte_limit)
            except ContextChannelLimit:
                deferred = True
                continue
            expanded.append(ExpandedMemory(ref.path, ref.content_digest, content, ref.origin_ref))
        admitted.append(ref)
    capsule = None
    capsule_reused = False
    if use_capsule:
        sources = tuple(CapsuleSource(ref.path, ref.content_digest)
                        for ref in admitted)
        claims = tuple(CapsuleClaim(
            f"memory-{index + 1}", ref.anchor, (ref.path,))
            for index, ref in enumerate(admitted))
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
            event_purpose=purpose, expansion_purpose=purpose,
            session_id=session_id, subject_digest=subject_digest,
            logical_path=f"generated/context-capsules/{capsule.capsule_id}.json",
            cache_hit=capsule_reused)
    # The report covers the complete stage ledger, including earlier lookups.
    expected = ("host_native", "memory_candidate", "memory_open") + (
        ("capsule_loader",) if capsule is not None else ())
    report = report_for_budget(
        ledger, budget, coverage="partial",
        expected_channels=expected,
        observed_channels=None, bypass_detected=False)
    if deferred or missing or selection.needs_more_candidates or batch.has_more:
        uncertainty = "truncated_unknown"
    elif not batch.candidates:
        uncertainty = "no_match_unknown"
    elif batch.source == "fallback":
        uncertainty = "index_fallback"
    else:
        uncertainty = lookup_plan.uncertainty
    reason = selection.reason
    if requested is not None and not requested and batch.candidates:
        reason = "CANDIDATES_ONLY"
    elif missing:
        reason = "REQUESTED_CANDIDATE_NOT_FOUND"
    return MemoryRecallResult(
        batch, tuple(expanded), selection.needs_more_candidates or (deferred and risk_required),
        "MEMORY_BYTE_LIMIT" if deferred else reason,
        capsule, capsule_reused, report,
        lookup_plan.selected_path, lookup_plan.reason_code,
        lookup_plan.capability_snapshot.as_dict(), uncertainty,
        report.delivered_bytes,
        {"candidate_search": 1, "memory_open": len(selection.selected),
         "capsule": 1 if capsule is not None else 0}, tuple(reused))
