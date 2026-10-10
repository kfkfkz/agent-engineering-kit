"""Reports bind independently checked results; Markdown renders the same JSON."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from core.pair import CONTROL_KEYS, RunControls, RunManifest  # noqa: E402
from core.result import CheckResult, aggregate_checks  # noqa: E402
from core.scenario import parse_scenario  # noqa: E402
from reports.pair_report import (  # noqa: E402
    build_pair_report,
    outcome_digest,
    render_markdown,
    verifier_digest,
)


class PairReportTests(unittest.TestCase):
    def artifact_receipt(self, scenario, manifest, verdict="PASS"):
        from aek.application.identity import bind_receipt, bound_receipt_as_dict
        from aek.core.context.identity import build_identity
        identity = build_identity({"subject": {"code": "a" * 64, "scenario": scenario.digest},
                                   "artifact": {"plan": "b" * 64, "documents": "c" * 64},
                                   "policy": {"governance": "d" * 64},
                                   "context": {"manifest": manifest.digest},
                                   "evidence": {"verification": "e" * 64}})
        receipt = bind_receipt(identity, {"status": verdict, "private": "artifact-canary"})
        return identity, bound_receipt_as_dict(receipt), receipt.receipt_digest

    def fixture(self):
        scenario = parse_scenario({
            "schema_version": 1, "id": "SC-001", "name": "case", "category": "bug",
            "task": {"prompt": "fix"}, "fixture": {"path": "fixture", "commit": "a" * 40, "sha256": "b" * 64},
            "execution": {"agents": ["claude"], "variants": ["vanilla", "aek"], "timeout_seconds": 60, "repeat": 1},
            "evaluation": {"checks": [{"id": "behavior", "entrypoint": "checks/check.py", "sha256": "c" * 64,
                                      "argv": ["{python}", "{check}", "{workspace}"], "timeout_seconds": 5, "kind": "outcome"}],
                           "allowed_paths": ["app.py"], "forbidden_paths": []},
        })
        values = {key: "d" * 64 for key in CONTROL_KEYS}
        values.update(scenario=scenario.digest, fixture=scenario.fixture_sha256, verifier=verifier_digest(scenario))
        controls = RunControls("claude", "model-fixture", "anthropic_messages", "a" * 40, tuple(values.items()), "e" * 64)
        left = RunManifest("EXP-1", "PAIR-1", "RUN-v", "SC-001", 1, "vanilla", 0, controls,
                           tuple(str(index) * 64 for index in range(1, 5)), "8" * 64, "f" * 64)
        right = RunManifest("EXP-1", "PAIR-1", "RUN-a", "SC-001", 1, "aek", 1, controls,
                            tuple(str(index) * 64 for index in range(5, 9)), "9" * 64, "f" * 64)
        return scenario, left, right

    def outcome(self, scenario, status):
        check = CheckResult("behavior", "outcome", status, "CHECK_RESULT", 0 if status == "PASS" else 1,
                            "c" * 64, "1" * 64, "2" * 64, 0.1)
        return aggregate_checks((check,), scenario.digest)

    def report(self, scenario, left, right, outcomes, **overrides):
        kwargs = {
            "trusted_manifest_digests": {left.run_id: left.digest, right.run_id: right.digest},
            "trusted_isolation_digests": frozenset({"f" * 64}),
            "expected_treatments": {"vanilla": "8" * 64, "aek": "9" * 64},
            "trusted_outcome_digests": {key: outcome_digest(value) for key, value in outcomes.items()},
        }
        kwargs.update(overrides)
        return build_pair_report(scenario, left, right, outcomes, **kwargs)

    def test_json_and_markdown_keep_outcome_process_and_cost_separate(self):
        scenario, left, right = self.fixture()
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "FAIL"),
                                                   right.run_id: self.outcome(scenario, "PASS")})
        self.assertEqual(report["validity"]["status"], "VALID")
        self.assertEqual(report["outcome"]["vanilla"]["status"], "FAIL")
        self.assertEqual(report["outcome"]["aek"]["status"], "PASS")
        self.assertEqual(report["process"]["status"], "NOT_EVALUATED")
        self.assertEqual(report["cost"]["status"], "NOT_EVALUATED")
        self.assertTrue(report["exploratory"])
        self.assertNotIn("overall_score", report)
        markdown = render_markdown(report)
        self.assertIn("vanilla | FAIL", markdown)
        self.assertIn("aek | PASS", markdown)
        self.assertIn("NOT_EVALUATED", markdown)

    def test_missing_stale_or_resealed_outcomes_are_not_credited(self):
        scenario, left, right = self.fixture()
        good = self.outcome(scenario, "PASS")
        missing = self.report(scenario, left, right, {left.run_id: good}, trusted_outcome_digests={})
        self.assertEqual(missing["outcome"]["vanilla"]["status"], "NOT_EVALUATED")
        stale = replace(good, scenario_digest="9" * 64)
        report = self.report(scenario, left, right, {left.run_id: stale})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "STALE")
        report = self.report(scenario, left, right, {left.run_id: good},
                             trusted_outcome_digests={left.run_id: "0" * 64})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "INVALID")

    def test_pass_claim_does_not_replace_complete_check_set_or_actual_failures(self):
        scenario, left, right = self.fixture()
        empty = aggregate_checks((), scenario.digest)
        report = self.report(scenario, left, right, {left.run_id: replace(empty, status="PASS")})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "NOT_EVALUATED")
        claimed = replace(self.outcome(scenario, "FAIL"), status="PASS")
        report = self.report(scenario, left, right, {left.run_id: claimed})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "FAIL")

    def test_mismatched_controls_or_infrastructure_failure_are_retained(self):
        scenario, left, right = self.fixture()
        right = replace(right, controls=replace(right.controls, model="different-model"))
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "INFRA_ERROR"),
                                                   right.run_id: self.outcome(scenario, "FAIL")})
        self.assertEqual(report["validity"]["status"], "INVALID")
        self.assertEqual(report["outcome"]["vanilla"]["status"], "INFRA_ERROR")
        self.assertIn("INVALID", render_markdown(report))

    def test_duplicate_variant_members_do_not_overwrite_or_disappear(self):
        scenario, left, right = self.fixture()
        right = replace(right, variant="vanilla")
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "FAIL"),
                                                   right.run_id: self.outcome(scenario, "PASS")})
        self.assertEqual(report["validity"]["status"], "INVALID")
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual({row["run_id"] for row in report["runs"]}, {left.run_id, right.run_id})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "NOT_EVALUATED")
        self.assertIn("NOT_EVALUATED", render_markdown(report))

    def test_success_with_failure_exit_or_malformed_check_is_not_correctness_evidence(self):
        scenario, left, right = self.fixture()
        good = self.outcome(scenario, "PASS")
        conflicting = replace(good, checks=(replace(good.checks[0], exit_code=1),))
        report = self.report(scenario, left, right, {left.run_id: conflicting})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "INVALID")
        malformed = replace(good, checks=({"id": "behavior"},))
        report = self.report(scenario, left, right, {left.run_id: malformed})
        self.assertEqual(report["outcome"]["vanilla"]["status"], "INVALID")

    def test_experiment_retains_invalid_unknown_and_infrastructure_samples(self):
        from reports import pair_report
        reports = []
        for index, (validity, vanilla, aek) in enumerate((
            ("VALID", "FAIL", "PASS"), ("VALID", "INFRA_ERROR", "PASS"),
            ("INVALID", "PASS", "PASS"), ("NOT_EVALUATED", "PASS", "PASS"),
        )):
            scenario, left, right = self.fixture()
            left = replace(left, pair_id=f"PAIR-{index}", run_id=f"RUN-v{index}")
            right = replace(right, pair_id=f"PAIR-{index}", run_id=f"RUN-a{index}")
            kwargs = {} if validity == "VALID" else {"trusted_isolation_digests": frozenset()}
            if validity == "INVALID":
                right = replace(right, controls=replace(right.controls, model="different-model"))
            reports.append(self.report(scenario, left, right, {
                left.run_id: self.outcome(scenario, vanilla), right.run_id: self.outcome(scenario, aek)}, **kwargs))
        anchors = frozenset(pair_report.report_digest(report) for report in reports)
        summary = pair_report.build_experiment_report(tuple(reports), trusted_report_digests=anchors)
        self.assertEqual(len(summary["pairs"]), 4)
        self.assertEqual(summary["validity_counts"], {"VALID": 2, "INVALID": 1, "NOT_EVALUATED": 1})
        self.assertIn("CONTROL_CONDITIONS_MISMATCH", summary["pairs"][2]["reason_codes"])
        group = summary["groups"][0]
        self.assertEqual(group["outcome_counts"]["vanilla"]["INFRA_ERROR"], 1)
        self.assertEqual(group["comparison"], {"eligible_pairs": 1, "both_pass": 0, "aek_only_pass": 1,
                                             "vanilla_only_pass": 0, "both_fail": 0})
        self.assertEqual(summary["cost"]["status"], "NOT_EVALUATED")
        self.assertFalse(summary["benefit_claim_supported"])

    def test_experiment_does_not_pool_different_controlled_configurations(self):
        from reports import pair_report
        reports = []
        for index in range(2):
            scenario, left, right = self.fixture()
            values = dict(left.controls.fingerprints)
            values["configuration"] = str(index) * 64
            controls = replace(left.controls, fingerprints=tuple(values.items()))
            left = replace(left, pair_id=f"PAIR-{index}", run_id=f"RUN-v{index}", controls=controls)
            right = replace(right, pair_id=f"PAIR-{index}", run_id=f"RUN-a{index}", controls=controls)
            reports.append(self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "PASS"),
                                                             right.run_id: self.outcome(scenario, "PASS")}))
        anchors = frozenset(pair_report.report_digest(report) for report in reports)
        summary = pair_report.build_experiment_report(tuple(reports), trusted_report_digests=anchors)
        self.assertEqual(len(summary["groups"]), 2)
        self.assertTrue(all(group["valid_pairs"] == 1 for group in summary["groups"]))

    def test_report_cli_reads_explicit_files_and_never_creates_trust_from_contents(self):
        from reports.pair_report import report_digest
        scenario, left, right = self.fixture()
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "FAIL"),
                                                   right.run_id: self.outcome(scenario, "PASS")})
        entry = Path(__file__).resolve().parent / "benchmark" / "run.py"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pair.json"
            raw = json.dumps(report)
            path.write_text(raw, encoding="utf-8")
            command = [sys.executable, str(entry), "report", "--input", str(path)]
            unknown = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            self.assertEqual(unknown.returncode, 0, unknown.stderr)
            summary = json.loads(unknown.stdout)
            self.assertEqual(summary["validity_counts"]["NOT_EVALUATED"], 1)
            self.assertEqual(summary["groups"], [])
            result = subprocess.run(command + ["--sha256", report_digest(report), "--format", "markdown"],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("aek_only_pass", result.stdout)
            mismatch = subprocess.run(command + ["--sha256", "0" * 64], cwd=directory,
                                      capture_output=True, text=True)
            self.assertEqual(mismatch.returncode, 2)
            self.assertEqual(json.loads(mismatch.stdout)["reason_code"], "REPORT_INPUT_INVALID")
            self.assertEqual(path.read_text(encoding="utf-8"), raw)
            self.assertNotIn(str(path), mismatch.stdout)

    def test_reused_runs_under_new_pair_names_cannot_inflate_sample_count(self):
        from reports import pair_report
        scenario, left, right = self.fixture()
        outcomes = {left.run_id: self.outcome(scenario, "FAIL"), right.run_id: self.outcome(scenario, "PASS")}
        reports = (self.report(scenario, left, right, outcomes),
                   self.report(scenario, replace(left, pair_id="PAIR-2"), replace(right, pair_id="PAIR-2"), outcomes))
        anchors = frozenset(pair_report.report_digest(report) for report in reports)
        summary = pair_report.build_experiment_report(reports, trusted_report_digests=anchors)
        self.assertEqual(summary["validity_counts"]["INVALID"], 2)
        self.assertEqual(summary["groups"], [])
        self.assertTrue(all("DUPLICATE_RUN_SAMPLE" in pair["reason_codes"] for pair in summary["pairs"]))

    def test_bound_workflow_advice_is_reported_without_changing_correctness(self):
        from analyzers.workflow import WorkflowEvent
        scenario, left, right = self.fixture()
        first = WorkflowEvent("EVENT-1", left.run_id, left.pair_id, left.task_id, "memory.retrieve",
                              "review", "review_required", "a" * 64, "b" * 64, 1.0, "COMPLETE",
                              manifest_sha256=left.digest)
        second = replace(first, event_id="EVENT-2", timestamp_seconds=2.0)
        events = (first, second)
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "FAIL"),
                                                   right.run_id: self.outcome(scenario, "PASS")},
                             workflow_events={left.run_id: events},
                             trusted_workflow_digests={left.run_id: frozenset(event.digest for event in events)},
                             complete_workflow_capture={left.run_id: True})
        self.assertEqual(report["process"]["status"], "PARTIAL")
        rows = {row["run_id"]: row for row in report["process"]["runs"]}
        self.assertEqual(rows[left.run_id]["rules"]["WA-001"]["status"], "FINDINGS")
        self.assertEqual(rows[left.run_id]["findings_count"], 1)
        self.assertRegex(rows[left.run_id]["analysis_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(rows[right.run_id]["status"], "NOT_EVALUATED")
        self.assertEqual(report["outcome"]["vanilla"]["status"], "FAIL")
        self.assertEqual(report["outcome"]["aek"]["status"], "PASS")
        self.assertEqual(report["cost"]["status"], "NOT_EVALUATED")

    def test_experiment_keeps_bound_process_evidence_separate_from_outcome(self):
        from analyzers.workflow import WorkflowEvent
        from reports.pair_report import build_experiment_report, report_digest
        scenario, left, right = self.fixture()
        event = WorkflowEvent("EVENT-1", left.run_id, left.pair_id, left.task_id, "memory.retrieve",
                              "review", "review_required", "a" * 64, "b" * 64, 1.0, "COMPLETE",
                              manifest_sha256=left.digest)
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "FAIL"),
                                                   right.run_id: self.outcome(scenario, "PASS")},
                             workflow_events={left.run_id: (event,)},
                             trusted_workflow_digests={left.run_id: frozenset({event.digest})},
                             complete_workflow_capture={left.run_id: True})
        summary = build_experiment_report((report,), trusted_report_digests=frozenset({report_digest(report)}))
        self.assertEqual(summary["process"]["status"], "PARTIAL")
        self.assertEqual(summary["process"]["pair_status_counts"]["PARTIAL"], 1)
        self.assertEqual(summary["pairs"][0]["process"]["runs"][0]["analysis_sha256"],
                         report["process"]["runs"][0]["analysis_sha256"])
        self.assertEqual(summary["groups"][0]["comparison"]["aek_only_pass"], 1)

    def test_workflow_report_rejects_cross_run_stale_and_untrusted_events(self):
        from analyzers.workflow import WorkflowEvent
        scenario, left, right = self.fixture()
        event = WorkflowEvent("EVENT-1", left.run_id, left.pair_id, left.task_id, "memory.retrieve",
                              "review", "review_required", "a" * 64, "b" * 64, 1.0, "COMPLETE",
                              manifest_sha256=left.digest)
        for changed, trusted, complete, expected in (
            (replace(event, run_id=right.run_id), True, True, "INVALID"),
            (replace(event, manifest_sha256="0" * 64), True, True, "STALE"),
            (replace(event, manifest_sha256=None), True, True, "NOT_EVALUATED"),
            (event, False, True, "NOT_EVALUATED"),
            (event, True, False, "NOT_EVALUATED"),
            (event, True, 1, "INVALID"),
        ):
            with self.subTest(expected=expected, trusted=trusted, complete=complete):
                report = self.report(scenario, left, right, {}, workflow_events={left.run_id: (changed,)},
                                     trusted_workflow_digests={left.run_id: frozenset({changed.digest}) if trusted else frozenset()},
                                     complete_workflow_capture={left.run_id: complete})
                row = report["process"]["runs"][0]
                self.assertEqual(row["status"], expected)
                self.assertEqual(row.get("findings_count", 0), 0)
                self.assertEqual(report["outcome"]["vanilla"]["status"], "NOT_EVALUATED")

    def test_large_workflow_is_summarized_without_truncating_the_finding_count(self):
        from analyzers.workflow import WorkflowEvent
        from reports.pair_report import report_digest
        scenario, left, right = self.fixture()
        events = tuple(WorkflowEvent(f"EVENT-{index}", left.run_id, left.pair_id, left.task_id, "memory.retrieve",
                                     "review", "review_required", "a" * 64, "b" * 64, float(index), "COMPLETE",
                                     data=(("irrelevant", "private-canary"),), manifest_sha256=left.digest)
                       for index in range(1001))
        report = self.report(scenario, left, right, {}, workflow_events={left.run_id: events},
                             trusted_workflow_digests={left.run_id: frozenset(event.digest for event in events)},
                             complete_workflow_capture={left.run_id: True})
        row = report["process"]["runs"][0]
        self.assertEqual(row["findings_count"], 1000)
        self.assertEqual(row["omitted_findings_count"], 997)
        self.assertEqual(len(row["sample_findings"]), 3)
        self.assertRegex(report_digest(report), r"^[0-9a-f]{64}$")
        self.assertNotIn("private-canary", json.dumps(report))

    def test_claimed_process_completion_without_analysis_cannot_be_aggregated(self):
        from reports.pair_report import build_experiment_report, report_digest
        scenario, left, right = self.fixture()
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "PASS"),
                                                   right.run_id: self.outcome(scenario, "PASS")})
        report["process"] = {"status": "EVALUATED"}
        summary = build_experiment_report((report,), trusted_report_digests=frozenset({report_digest(report)}))
        self.assertEqual(summary["process"]["status"], "INVALID")
        self.assertEqual(summary["pairs"][0]["process"]["reason_codes"], ["PROCESS_REPORT_CONTENT_INVALID"])
        self.assertEqual(summary["groups"][0]["comparison"]["both_pass"], 1)

    def test_artifact_receipts_need_current_run_and_version_identity(self):
        scenario, left, right = self.fixture()
        captures = {manifest.run_id: self.artifact_receipt(scenario, manifest) for manifest in (left, right)}
        report = self.report(scenario, left, right, {left.run_id: self.outcome(scenario, "FAIL"),
                                                   right.run_id: self.outcome(scenario, "PASS")},
                             artifact_receipts={key: capture[1] for key, capture in captures.items()},
                             artifact_identities={key: capture[0] for key, capture in captures.items()},
                             trusted_artifact_receipt_digests={key: capture[2] for key, capture in captures.items()})
        self.assertEqual(report["artifacts"]["status"], "PASS")
        self.assertTrue(all(row["identity_state"] == "VALID" for row in report["artifacts"]["runs"]))
        self.assertEqual(report["outcome"]["vanilla"]["status"], "FAIL")
        self.assertNotIn("artifact-canary", json.dumps(report))

    def test_artifact_missing_stale_failed_and_resealed_evidence_stays_distinct(self):
        from aek.core.context.identity import build_identity, identity_as_dict
        scenario, left, right = self.fixture()
        identity, payload, anchor = self.artifact_receipt(scenario, left)
        changed = identity_as_dict(identity)["components"]
        changed["artifact"]["documents"] = "f" * 64
        _, failed, failed_anchor = self.artifact_receipt(scenario, left, "FAIL")
        for current, raw, trust, expected in (
            (identity, payload, None, "NOT_EVALUATED"),
            (build_identity(changed), payload, anchor, "STALE"),
            (identity, failed, failed_anchor, "FAIL"),
            (identity, failed, anchor, "INVALID"),
            (self.artifact_receipt(scenario, right)[0], payload, anchor, "STALE"),
        ):
            with self.subTest(expected=expected):
                report = self.report(scenario, left, right, {}, artifact_receipts={left.run_id: raw},
                                     artifact_identities={left.run_id: current},
                                     trusted_artifact_receipt_digests={} if trust is None else {left.run_id: trust})
                self.assertEqual(report["artifacts"]["runs"][0]["status"], expected)
                self.assertEqual(report["outcome"]["vanilla"]["status"], "NOT_EVALUATED")

    def test_artifact_boolean_schema_is_not_valid_receipt_evidence(self):
        scenario, left, right = self.fixture()
        identity, payload, anchor = self.artifact_receipt(scenario, left)
        payload["schema_version"] = True
        report = self.report(scenario, left, right, {}, artifact_receipts={left.run_id: payload},
                             artifact_identities={left.run_id: identity},
                             trusted_artifact_receipt_digests={left.run_id: anchor})
        self.assertEqual(report["artifacts"]["runs"][0]["status"], "INVALID")

    def test_experiment_retains_verified_artifact_receipt_references(self):
        from reports.pair_report import build_experiment_report, report_digest
        scenario, left, right = self.fixture()
        captures = {manifest.run_id: self.artifact_receipt(scenario, manifest) for manifest in (left, right)}
        report = self.report(scenario, left, right, {}, artifact_receipts={key: data[1] for key, data in captures.items()},
                             artifact_identities={key: data[0] for key, data in captures.items()},
                             trusted_artifact_receipt_digests={key: data[2] for key, data in captures.items()})
        summary = build_experiment_report((report,), trusted_report_digests=frozenset({report_digest(report)}))
        self.assertEqual(summary["artifacts"]["status"], "PASS")
        self.assertEqual(summary["artifacts"]["pair_status_counts"]["PASS"], 1)
        self.assertEqual(summary["pairs"][0]["artifacts"]["runs"][0]["receipt_sha256"], captures[left.run_id][2])
        self.assertEqual(summary["groups"][0]["comparison"]["eligible_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
