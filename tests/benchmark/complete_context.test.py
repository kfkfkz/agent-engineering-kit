#!/usr/bin/env python3
"""Pin the controlled full-channel fixture stream without claiming CLI parity."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from complete_context import CHANNELS, replay_case
from context import (BaselineManifest, aggregate_route, compare_case,
                     load_baseline, release_verdict)
from equivalence import complete_equivalence


ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("complete-baseline-v1.0.0.json")
THRESHOLDS = {"direct": 0.50, "bounded": 0.30, "standard": 0.15,
              "initiative": 0.0}


class CompleteReplayTests(unittest.TestCase):
    def test_pinned_stream_and_provisional_material_reduction(self) -> None:
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
                reduction = (1 - current.report.delivered_bytes /
                             baseline.report.delivered_bytes)
                self.assertGreaterEqual(reduction, THRESHOLDS[case.route])
                self.assertLessEqual(current.report.delivered_chars,
                                     baseline.report.delivered_chars)

    def test_complete_equivalence_and_release_verdict(self) -> None:
        selected = load_baseline(
            BASELINE.with_name("context-baseline-v1.0.0.json"))
        frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
        receipt_digests = json.loads(BASELINE.with_name(
            "complete-equivalence-v1.0.1.json").read_text(encoding="utf-8"))
        records = {row["case_id"]: row for row in frozen["cases"]}
        comparisons = []
        complete_cases = []
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
            self.assertEqual(comparison.verdict, "THRESHOLD_MET", comparison)
            comparisons.append(comparison)
            complete_cases.append(case)
        routes = tuple(aggregate_route(
            route, [row for row in comparisons if row.route == route])
            for route in THRESHOLDS)
        manifest = BaselineManifest(
            1, selected.baseline_commit, selected.metric,
            "complete_delivered_context", tuple(complete_cases))
        self.assertEqual(release_verdict(manifest, routes).verdict,
                         "THRESHOLD_MET")


if __name__ == "__main__":
    unittest.main()
