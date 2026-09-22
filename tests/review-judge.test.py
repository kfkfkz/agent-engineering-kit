#!/usr/bin/env python3
"""The deterministic judge, not AI review text, decides a gate verdict."""
from __future__ import annotations

import unittest

from aek.core.review.judge import JudgeInput, judge_gate


class JudgeTests(unittest.TestCase):
    def test_old_threshold_and_iteration_semantics(self) -> None:
        base = dict(hard_fail_count=0, blocker=0, major=0, minor=0,
                    upstream_pending=0, consecutive_failures=0,
                    was_blocked=False, max_blocker=0, max_major=0,
                    max_minor=2, max_iterations=3)
        self.assertEqual(judge_gate(JudgeInput(**base)).status, "PASS")
        self.assertEqual(judge_gate(JudgeInput(**{**base, "major": 1})).status,
                         "NEEDS_REVISION")
        self.assertEqual(judge_gate(JudgeInput(**{
            **base, "major": 1, "consecutive_failures": 2})).status, "BLOCKED")
        self.assertEqual(judge_gate(JudgeInput(**{
            **base, "upstream_pending": 1})).status, "BLOCKED")
        self.assertEqual(judge_gate(JudgeInput(**{
            **base, "major": 1, "was_blocked": True})).status, "BLOCKED")
        self.assertEqual(judge_gate(JudgeInput(**{
            **base, "was_blocked": True})).status, "PASS")

    def test_malformed_policy_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            JudgeInput(0, 0, 0, 0, 0, 0, False, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            JudgeInput(0, 0, 0, 0, 0, 0, False, True, 0, 0, 3)


if __name__ == "__main__":
    unittest.main()
