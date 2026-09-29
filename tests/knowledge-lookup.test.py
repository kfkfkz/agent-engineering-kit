#!/usr/bin/env python3
"""Memory lookup selects exactly one observable channel and degrades safely."""
from __future__ import annotations

import unittest

from aek.core.context.lookup import (
    CapabilitySnapshot,
    LookupAttempt,
    plan_knowledge_lookup,
)


class KnowledgeLookupTests(unittest.TestCase):
    def capabilities(self, *, mcp_visible=True, mcp_compatible=True,
                     cli_available=True, text_available=True):
        return CapabilitySnapshot(
            mcp_configured=True, mcp_visible=mcp_visible,
            mcp_compatible=mcp_compatible, cli_available=cli_available,
            bounded_text_available=text_available,
            evidence=("host:tools/list",))

    def test_visible_compatible_mcp_is_the_only_primary_channel(self) -> None:
        plan = plan_knowledge_lookup(self.capabilities())
        self.assertEqual(plan.selected_path, "mcp")
        self.assertEqual(plan.reason_code, "MCP_AVAILABLE")
        self.assertEqual(plan.attempted_paths, ())

    def test_mcp_preflight_failure_degrades_once_to_cli(self) -> None:
        plan = plan_knowledge_lookup(
            self.capabilities(),
            attempts=(LookupAttempt("mcp", "before_start", "UNAVAILABLE"),))
        self.assertEqual(plan.selected_path, "cli")
        self.assertEqual(plan.reason_code, "MCP_PRESTART_FAILED")
        self.assertEqual(plan.attempted_paths, ("mcp",))

    def test_started_mcp_failure_stops_instead_of_double_execution(self) -> None:
        plan = plan_knowledge_lookup(
            self.capabilities(),
            attempts=(LookupAttempt("mcp", "after_start", "TIMEOUT"),))
        self.assertEqual(plan.selected_path, "stop")
        self.assertEqual(plan.reason_code, "MCP_OUTCOME_UNKNOWN")

    def test_cli_failure_degrades_to_bounded_text_with_uncertainty(self) -> None:
        plan = plan_knowledge_lookup(
            self.capabilities(mcp_visible=False),
            attempts=(LookupAttempt("cli", "before_start", "MISSING"),))
        self.assertEqual(plan.selected_path, "bounded_text")
        self.assertEqual(plan.reason_code, "CLI_PRESTART_FAILED")
        self.assertEqual(plan.uncertainty, "degraded")

    def test_no_channel_fails_closed(self) -> None:
        plan = plan_knowledge_lookup(self.capabilities(
            mcp_visible=False, cli_available=False, text_available=False))
        self.assertEqual(plan.selected_path, "stop")
        self.assertEqual(plan.reason_code, "NO_LOOKUP_CHANNEL")


if __name__ == "__main__":
    unittest.main()
