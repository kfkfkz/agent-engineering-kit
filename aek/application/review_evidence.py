"""Compose Memory-first and codebase-first evidence for code review.

The host executes MCP tools.  This module only produces an ordered, auditable
plan so review entry points cannot silently replace structural evidence with
grep or execute two Memory lookup channels for the same request.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json

from aek.application.identity import IdentityBoundReceipt, verify_bound_receipt
from aek.core.codebase_context import CodebaseBarrierPlan
from aek.core.context.identity import IdentityEnvelope, IdentityState
from aek.core.context.lookup import KnowledgeLookupPlan


@dataclass(frozen=True)
class ReviewEvidenceStep:
    channel: str
    reason_code: str
    required_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not self.channel or not self.reason_code or not self.required_tools
                or any(not isinstance(tool, str) or not tool
                       for tool in self.required_tools)):
            raise ValueError("review evidence step is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "reason_code": self.reason_code,
            "required_tools": list(self.required_tools),
        }


@dataclass(frozen=True)
class ReviewEvidencePlan:
    steps: tuple[ReviewEvidenceStep, ...]
    blocking_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    confidence: str
    may_claim_complete_structure: bool
    reused_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.confidence not in {"high", "degraded", "unknown"}
                or any(not isinstance(item, ReviewEvidenceStep)
                       for item in self.steps)
                or any(not isinstance(item, str) or not item
                       for item in self.blocking_reasons + self.limitations + self.reused_refs)):
            raise ValueError("review evidence plan is invalid")

    @property
    def ready_for_review(self) -> bool:
        return not self.blocking_reasons

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            for tool in step.required_tools:
                if tool not in seen:
                    seen.append(tool)
        return tuple(seen)

    def as_dict(self) -> dict[str, object]:
        result = {
            "steps": [step.as_dict() for step in self.steps],
            "blocking_reasons": list(self.blocking_reasons),
            "limitations": list(self.limitations),
            "confidence": self.confidence,
            "ready_for_review": self.ready_for_review,
            "may_claim_complete_structure": self.may_claim_complete_structure,
        }
        if self.reused_refs:
            result["reused_refs"] = list(self.reused_refs)
        return result


_MEMORY_CHANNELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "mcp": ("memory_recall_mcp", ("memory_recall",)),
    "cli": ("memory_recall_cli", ("memory-recall",)),
    "bounded_text": ("bounded_memory_text", ("bounded_memory_text",)),
}

_REUSE_IDENTITY = (
    "subject.worktree", "subject.dependencies", "context.query", "policy.rules",
    "evidence.provenance",
)


@dataclass(frozen=True)
class ReviewEvidenceReuse:
    """Host supplies current identities and resolves refs from its trusted observations.

    A JSON envelope or digest from an untrusted caller does not establish origin.
    The lookup belongs to the host that owns the Memory and graph observations.
    """

    identities: Mapping[str, IdentityEnvelope]
    refs: Mapping[str, str]
    lookup: Callable[[str], IdentityBoundReceipt | None]

    def current_ref(self, kind: str) -> str | None:
        ref = self.refs.get(kind)
        identity = self.identities.get(kind)
        if not ref or identity is None:
            return None
        receipt = self.lookup(ref)
        if not isinstance(receipt, IdentityBoundReceipt):
            return None
        components = _REUSE_IDENTITY + (("context.generation",) if kind == "graph" else ())
        if verify_bound_receipt(receipt, identity, components).state != IdentityState.VALID:
            return None
        payload = json.loads(receipt.payload_json)
        if (payload.get("kind") != kind or payload.get("ref") != ref
                or payload.get("complete") is not True):
            return None
        return ref


def plan_code_review_evidence(
    *, memory: KnowledgeLookupPlan, codebase: CodebaseBarrierPlan,
    include_textual_checks: bool,
    reuse: ReviewEvidenceReuse | None = None,
) -> ReviewEvidencePlan:
    """Return the only permitted evidence order for a code-review pass.

    Memory is resolved first.  A structural freshness barrier must resolve
    before graph/source review or optional textual checks.  Text search is a
    bounded final supplement, never a substitute for graph evidence.
    """
    if (not isinstance(memory, KnowledgeLookupPlan)
            or not isinstance(codebase, CodebaseBarrierPlan)
            or type(include_textual_checks) is not bool):
        raise ValueError("review evidence inputs are invalid")

    if memory.selected_path == "stop":
        return ReviewEvidencePlan(
            (), (memory.reason_code,), (), "unknown", False)

    memory_ref = reuse.current_ref("memory") if reuse else None
    reused_refs = [memory_ref] if memory_ref else []
    steps = []
    if not memory_ref:
        memory_channel, memory_tools = _MEMORY_CHANNELS[memory.selected_path]
        steps.append(ReviewEvidenceStep(memory_channel, memory.reason_code, memory_tools))
    limitations: list[str] = []
    confidence = "high"
    if not memory_ref and memory.uncertainty != "normal":
        confidence = "degraded"
        limitations.append(memory.reason_code)

    if codebase.required_action in {"CHECK_STATUS", "REFRESH_ONCE"}:
        steps.append(ReviewEvidenceStep(
            "codebase_freshness_barrier", codebase.reason_code,
            codebase.required_tools))
        return ReviewEvidencePlan(
            tuple(steps), (codebase.reason_code,), tuple(limitations),
            "unknown", False, tuple(reused_refs))

    graph_complete = codebase.required_action == "USE_GRAPH"
    if graph_complete:
        graph_ref = reuse.current_ref("graph") if reuse else None
        if graph_ref:
            reused_refs.append(graph_ref)
        else:
            steps.append(ReviewEvidenceStep(
                "codebase_graph", codebase.reason_code, codebase.required_tools))
        steps.append(ReviewEvidenceStep(
            "targeted_source", "VERIFY_GRAPH_FINDINGS_IN_SOURCE",
            ("targeted_source_read",)))
    elif codebase.required_action == "BOUNDED_SOURCE":
        confidence = "degraded"
        limitations.append(codebase.reason_code)
        steps.append(ReviewEvidenceStep(
            "bounded_source", codebase.reason_code,
            codebase.required_tools))
    else:
        raise ValueError("unknown codebase barrier action")

    if include_textual_checks:
        steps.append(ReviewEvidenceStep(
            "bounded_text", "TEXTUAL_REVIEW_CHECKS", ("rg",)))

    return ReviewEvidencePlan(
        tuple(steps), (), tuple(dict.fromkeys(limitations)), confidence,
        graph_complete, tuple(reused_refs))
