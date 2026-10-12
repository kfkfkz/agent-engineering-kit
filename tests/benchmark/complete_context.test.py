#!/usr/bin/env python3
"""Pin controls and reject stale 1.0.4 context-reduction evidence for 1.0.5."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from complete_context import CHANNELS, replay_case
from context import (
    BaselineManifest,
    aggregate_route,
    compare_case,
    load_baseline,
    release_verdict,
)
from equivalence import complete_equivalence

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("complete-baseline-v1.0.0.json")
class CompleteReplayTests(unittest.TestCase):
    def test_pinned_control_stream_and_current_full_channel_coverage(self) -> None:
        selected = load_baseline(
            BASELINE.with_name("context-baseline-v1.0.0.json"))
        frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(frozen["scope"], "complete_delivered_context")
        self.assertEqual(frozen["baseline_commit"], selected.baseline_commit)
        records = {row["case_id"]: row for row in frozen["cases"]}
        self.assertEqual(set(records), {case.case_id for case in selected.cases})
        for case in selected.cases:
            with self.subTest(case=case.case_id):
                baseline = replay_case(ROOT, case, mode="baseline")
                current = replay_case(ROOT, case, mode="current")
                record = records[case.case_id]
                self.assertEqual(baseline.fixture_digest, record["fixture_digest"])
                self.assertEqual(current.fixture_digest, record["fixture_digest"])
                self.assertEqual(baseline.report.stream_digest,
                                 record["baseline_stream_digest"])
                self.assertEqual(baseline.report.delivered_bytes,
                                 record["baseline_bytes"])
                self.assertEqual(baseline.report.delivered_chars,
                                 record["baseline_chars"])
                self.assertEqual(len(baseline.delivered_paths), record["baseline_events"])
                self.assertEqual((baseline.report.coverage, current.report.coverage),
                                 ("complete", "complete"))
                self.assertEqual(baseline.report.observed_channels,
                                 tuple(sorted(CHANNELS)))
                self.assertEqual(current.report.observed_channels,
                                 tuple(sorted(CHANNELS)))
                self.assertGreater(current.report.delivered_bytes, 0)
                self.assertGreater(current.report.delivered_chars, 0)

    def test_old_equivalence_receipts_cannot_authorize_changed_material(self) -> None:
        selected = load_baseline(
            BASELINE.with_name("context-baseline-v1.0.0.json"))
        frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
        receipt_digests = json.loads(BASELINE.with_name(
            "complete-equivalence-v1.0.4.json").read_text(encoding="utf-8"))
        records = {row["case_id"]: row for row in frozen["cases"]}
        comparisons = []
        for selected_case in selected.cases:
            record = records[selected_case.case_id]
            case = replace(
                selected_case, fixture_digest=record["fixture_digest"],
                baseline_bytes=record["baseline_bytes"],
                baseline_chars=record["baseline_chars"])
            baseline = replay_case(ROOT, case, mode="baseline")
            current = replay_case(ROOT, case, mode="current")
            receipt = complete_equivalence(
                ROOT, case, baseline, current,
                receipt_digests[case.case_id])
            report = {
                "coverage": current.report.coverage,
                "case_id": case.case_id, "route": case.route,
                "fixture_digest": case.fixture_digest,
                "diff_digest": case.diff_digest,
                "environment_digest": case.environment_digest,
                "cold_cache": True, "baseline_commit": case.baseline_commit,
                "delivered_bytes": current.report.delivered_bytes,
                "delivered_chars": current.report.delivered_chars,
            }
            comparison = compare_case(case, report, receipt)
            # The checked-in receipt pins 1.0.4 material. 1.0.5 intentionally
            # makes no Token/context-reduction claim; changed streams require a
            # new separately approved measurement, so the stale receipt cannot
            # establish equivalence or a release verdict.
            self.assertEqual(comparison.verdict, "INVALID", comparison)
            self.assertIn("equivalence", comparison.reasons)
            comparisons.append(comparison)
        routes = tuple(aggregate_route(
            route, [row for row in comparisons if row.route == route])
            for route in ("direct", "bounded", "standard", "initiative"))
        manifest = BaselineManifest(
            1, selected.baseline_commit, selected.metric,
            "complete_delivered_context", selected.cases)
        verdict = release_verdict(manifest, routes)
        self.assertEqual(verdict.verdict, "INVALID")
        self.assertIn("invalid_case", verdict.reasons)


if __name__ == "__main__":
    unittest.main()
