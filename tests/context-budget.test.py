#!/usr/bin/env python3
"""A budget overrun is explicit, reasoned, and never truncates mandatory input."""
from __future__ import annotations

import unittest

from aek.core.context.budget import decide_context_expansion, resolve_context_budget


class ExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = resolve_context_budget("bounded", "execution", "quick")

    def test_under_limit_and_reasoned_expansion(self) -> None:
        decision = decide_context_expansion(self.budget, used_bytes=0,
                                            requested_bytes=4096,
                                            source="target_code")
        self.assertEqual(decision.action, "ALLOW")
        self.assertFalse(decision.over_budget)
        denied = decide_context_expansion(
            self.budget, used_bytes=self.budget.stage_byte_limit,
            requested_bytes=1, source="target_code")
        self.assertEqual(denied.action, "REQUIRE_PURPOSE_OR_REROUTE")
        allowed = decide_context_expansion(
            self.budget, used_bytes=self.budget.stage_byte_limit,
            requested_bytes=1, source="target_code", purpose="target_evidence")
        self.assertEqual(allowed.action, "ALLOW_WITH_FINDING")
        self.assertEqual(allowed.finding, "REASONED_CONTEXT_EXPANSION")

    def test_mandatory_sources_are_not_truncated(self) -> None:
        result = decide_context_expansion(
            self.budget, used_bytes=self.budget.stage_byte_limit,
            requested_bytes=10, source="safety_required")
        self.assertEqual(result.action, "ALLOW_MANDATORY")
        self.assertTrue(result.over_budget)

    def test_invalid_purpose_or_sizes_are_rejected(self) -> None:
        for purpose in ("secret=abc", "", 7):
            with self.assertRaises(ValueError):
                decide_context_expansion(
                    self.budget, used_bytes=0, requested_bytes=1,
                    source="target_code", purpose=purpose)
        with self.assertRaises(ValueError):
            decide_context_expansion(self.budget, used_bytes=-1,
                                     requested_bytes=1, source="target_code")


if __name__ == "__main__":
    unittest.main()
