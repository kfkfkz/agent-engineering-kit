#!/usr/bin/env python3
"""Context reports bind actual ledger events to the routed budget."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aek.adapters.telemetry import ContextLedger
from aek.application.context_telemetry import (
    record_context_delivery, report_for_budget,
)
from aek.core.context.budget import resolve_context_budget


class ContextServiceTests(unittest.TestCase):
    def test_budgeted_delivery_rejects_unreasoned_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = ContextLedger(Path(temp) / "events.jsonl")
            budget = resolve_context_budget("bounded", "execution", "quick")
            ledger.append_text(
                "x" * budget.stage_byte_limit,
                route="bounded", stage="execution", source_kind="code",
                channel="code_loader", purpose="route_required", cache_hit=False,
                session_id="test-session", subject_digest="a" * 64,
                logical_path="src/code.py")
            with self.assertRaises(ValueError):
                record_context_delivery(
                    ledger, budget, "y", source="target_code",
                    source_kind="code", channel="code_loader",
                    event_purpose="route_required", session_id="test-session",
                    subject_digest="a" * 64, logical_path="src/code.py")
            self.assertEqual(len(ledger.read()), 1)
            result = record_context_delivery(
                ledger, budget, "y", source="target_code",
                source_kind="code", channel="code_loader",
                event_purpose="target_evidence", expansion_purpose="target_evidence",
                session_id="test-session", subject_digest="a" * 64,
                logical_path="src/code.py")
            self.assertEqual(result.decision.action, "ALLOW_WITH_FINDING")
            self.assertEqual(result.event.sequence, 2)
            self.assertEqual(ledger.total_bytes(), budget.stage_byte_limit + 1)

    def test_mandatory_material_is_never_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = ContextLedger(Path(temp) / "events.jsonl")
            budget = resolve_context_budget("direct", "routing", "quick")
            result = record_context_delivery(
                ledger, budget, "x" * (budget.stage_byte_limit + 1),
                source="root_instructions", source_kind="document",
                channel="root_loader", event_purpose="risk_required",
                session_id="test-session", subject_digest="a" * 64,
                logical_path="AGENTS.md")
            self.assertEqual(result.decision.action, "ALLOW_MANDATORY")
            self.assertTrue(result.decision.over_budget)

    def test_partial_report_discloses_unobserved_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = ContextLedger(Path(temp) / "events.jsonl")
            ledger.append_text(
                "材料", route="bounded", stage="routing", source_kind="reference",
                channel="aek_skill", purpose="route_required", cache_hit=False,
                session_id="test-session", subject_digest="a" * 64,
                logical_path="skills/repo-delivery/SKILL.md")
            report = report_for_budget(
                ledger, resolve_context_budget("bounded", "routing", "quick"),
                coverage="partial", expected_channels=("aek_skill", "host_shell"),
                observed_channels=("aek_skill",), bypass_detected=False)
            self.assertEqual(report.unobserved_channels, ("host_shell",))

    def test_mixed_route_ledger_cannot_be_reported_under_one_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = ContextLedger(Path(temp) / "events.jsonl")
            ledger.append_text(
                "材料", route="standard", stage="routing", source_kind="reference",
                channel="aek_skill", purpose="route_required", cache_hit=False,
                session_id="test-session", subject_digest="a" * 64,
                logical_path="skills/repo-delivery/SKILL.md")
            with self.assertRaises(ValueError):
                report_for_budget(
                    ledger, resolve_context_budget("bounded", "routing", "quick"),
                    coverage="partial", expected_channels=("aek_skill",),
                    observed_channels=("aek_skill",), bypass_detected=False)

    def test_reroute_retains_prior_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = ContextLedger(Path(temp) / "events.jsonl")
            for route in ("direct", "bounded"):
                ledger.append_text(
                    "材料", route=route, stage="routing", source_kind="reference",
                    channel="aek_skill", purpose="reroute_evidence", cache_hit=False,
                    session_id="test-session", subject_digest="a" * 64,
                    logical_path="skills/repo-delivery/SKILL.md")
            report = report_for_budget(
                ledger, resolve_context_budget("bounded", "routing", "quick"),
                coverage="complete", expected_channels=("aek_skill",),
                observed_channels=("aek_skill",), bypass_detected=False)
            self.assertEqual(report.delivered_bytes, 12)


if __name__ == "__main__":
    unittest.main()
