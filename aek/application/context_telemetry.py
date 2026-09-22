"""Bind a routed ContextBudget to the recorded AEK-owned material stream."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aek.core.context.budget import (
    ContextBudget, ExpansionDecision, decide_context_expansion,
)
from aek.core.context.telemetry import ContextEvent, ContextReport, build_context_report


_ROUTES = ("direct", "bounded", "standard", "initiative")


class LedgerReader(Protocol):
    def read(self) -> tuple[ContextEvent, ...]: ...


class LedgerWriter(Protocol):
    def total_bytes(self) -> int: ...
    def append_text(self, content: str, **kwargs: object) -> ContextEvent: ...


@dataclass(frozen=True)
class RecordedDelivery:
    event: ContextEvent
    decision: ExpansionDecision


def record_context_delivery(
    ledger: LedgerWriter, budget: ContextBudget, content: str, *,
    source: str, source_kind: str, channel: str, event_purpose: str,
    session_id: str, subject_digest: str, logical_path: str,
    expansion_purpose: str | None = None, cache_hit: bool = False,
    occurred_at: str | None = None,
) -> RecordedDelivery:
    """Gate one AEK-owned delivery before persisting its metadata event.

    This is a soft budget. A concurrent writer can cross the limit after the
    check; the final Ledger report is the authoritative accounting record.
    """
    if (not isinstance(budget, ContextBudget) or not isinstance(content, str)
            or not callable(getattr(ledger, "total_bytes", None))
            or not callable(getattr(ledger, "append_text", None))):
        raise ValueError("budgeted text delivery requires a ledger and text")
    if expansion_purpose is not None and expansion_purpose != event_purpose:
        raise ValueError("expansion purpose must match the recorded event")
    decision = decide_context_expansion(
        budget, used_bytes=ledger.total_bytes(),
        requested_bytes=len(content.encode("utf-8", errors="strict")),
        source=source, purpose=expansion_purpose)
    if decision.action == "REQUIRE_PURPOSE_OR_REROUTE":
        raise ValueError("context expansion needs a controlled purpose or reroute")
    event = ledger.append_text(
        content, route=budget.route, stage=budget.stage,
        source_kind=source_kind, channel=channel, purpose=event_purpose,
        cache_hit=cache_hit, session_id=session_id,
        subject_digest=subject_digest, logical_path=logical_path,
        occurred_at=occurred_at)
    return RecordedDelivery(event, decision)


def report_for_budget(
    ledger: LedgerReader, budget: ContextBudget, *, coverage: str,
    expected_channels: tuple[str, ...], observed_channels: tuple[str, ...],
    bypass_detected: bool,
) -> ContextReport:
    """Account for prior lower-route events after a monotonic reroute.

    This service is stage-local. A host using one ledger across stages must
    split/rebuild stage reports without breaking the global hash chain.
    """
    if not isinstance(budget, ContextBudget) or not callable(getattr(ledger, "read", None)):
        raise ValueError("ledger and budget are required")
    events = ledger.read()
    previous_rank = -1
    current_rank = _ROUTES.index(budget.route)
    for row in events:
        rank = _ROUTES.index(row.route)
        if row.stage != budget.stage or rank < previous_rank or rank > current_rank:
            raise ValueError("ledger stage or route transition conflicts with budget")
        previous_rank = rank
    return build_context_report(
        events, coverage=coverage, expected_channels=expected_channels,
        observed_channels=observed_channels, bypass_detected=bypass_detected,
        delivered_byte_limit=budget.stage_byte_limit,
    )
