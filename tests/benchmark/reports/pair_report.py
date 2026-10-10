"""Build a scoped report from trusted run/check identities; render that JSON."""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections import Counter
from dataclasses import asdict
from typing import Mapping

from analyzers.workflow import WorkflowEvent, analyze_workflow
from core.evidence import evaluate_receipt
from core.pair import RunManifest, validate_pair
from core.result import CheckResult, OutcomeResult, aggregate_checks
from core.scenario import ScenarioSpec

from aek.core.context.identity import IdentityEnvelope, identity_as_dict

_ARTIFACT_COMPONENTS = ("subject.code", "subject.scenario", "artifact.plan", "artifact.documents",
                        "policy.governance", "context.manifest", "evidence.verification")


def verifier_digest(scenario: ScenarioSpec) -> str:
    return hashlib.sha256(json.dumps([asdict(check) for check in scenario.checks], sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def outcome_digest(outcome: OutcomeResult) -> str:
    return hashlib.sha256(json.dumps(asdict(outcome), sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _outcome(scenario: ScenarioSpec, outcome: OutcomeResult | None, anchor: str | None) -> dict:
    missing = {"status": "NOT_EVALUATED", "reason_codes": ["OUTCOME_EVIDENCE_MISSING"], "evidence_sha256": None}
    if outcome is None or anchor is None:
        return missing
    if (not isinstance(outcome, OutcomeResult) or type(outcome.checks) is not tuple
            or len(outcome.checks) > 128):
        return {"status": "INVALID", "reason_codes": ["OUTCOME_SHAPE_INVALID"], "evidence_sha256": None}
    for check in outcome.checks:
        if (not isinstance(check, CheckResult) or type(check.id) is not str
                or type(check.kind) is not str or type(check.status) is not str
                or check.status not in {"PASS", "FAIL", "INFRA_ERROR", "NOT_EVALUATED"}
                or type(check.reason_code) is not str
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", check.reason_code)
                or (check.exit_code is not None and type(check.exit_code) is not int)
                or (check.status == "PASS" and check.exit_code != 0)
                or (check.status == "FAIL" and check.exit_code in {None, 0})
                or type(check.duration_seconds) not in {int, float}
                or not 0 <= check.duration_seconds <= 2**53
                or not math.isfinite(check.duration_seconds) or check.duration_seconds < 0
                or any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in (check.script_sha256, check.stdout_sha256, check.stderr_sha256))):
            return {"status": "INVALID", "reason_codes": ["OUTCOME_CHECK_INVALID"], "evidence_sha256": None}
    try:
        digest = outcome_digest(outcome)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {"status": "INVALID", "reason_codes": ["OUTCOME_SHAPE_INVALID"], "evidence_sha256": None}
    if digest != anchor:
        return {"status": "INVALID", "reason_codes": ["OUTCOME_ANCHOR_MISMATCH"], "evidence_sha256": digest}
    if outcome.scenario_digest != scenario.digest:
        return {"status": "STALE", "reason_codes": ["OUTCOME_SCENARIO_MISMATCH"], "evidence_sha256": digest}
    expected = {check.id: check for check in scenario.checks}
    if (len(outcome.checks) != len(expected) or {check.id for check in outcome.checks} != set(expected)):
        return {"status": "NOT_EVALUATED", "reason_codes": ["CHECK_SET_INCOMPLETE"], "evidence_sha256": digest}
    if any(check.kind != expected[check.id].kind or check.script_sha256 != expected[check.id].sha256
           for check in outcome.checks):
        return {"status": "STALE", "reason_codes": ["VERIFIER_IDENTITY_MISMATCH"], "evidence_sha256": digest}
    status = aggregate_checks(outcome.checks, scenario.digest).status
    return {"status": status, "reason_codes": [], "evidence_sha256": digest}


def build_pair_report(scenario: ScenarioSpec, left: RunManifest, right: RunManifest,
                      outcomes: Mapping[str, OutcomeResult], *,
                      trusted_manifest_digests: Mapping[str, str] | None = None,
                      trusted_isolation_digests: frozenset[str] | None = None,
                      expected_treatments: Mapping[str, str] | None = None,
                      trusted_outcome_digests: Mapping[str, str] | None = None,
                      workflow_events: Mapping[str, tuple[WorkflowEvent, ...]] | None = None,
                      trusted_workflow_digests: Mapping[str, frozenset[str]] | None = None,
                      complete_workflow_capture: Mapping[str, bool] | None = None,
                      trusted_hidden_isolation_digests: frozenset[str] | None = None,
                      artifact_receipts: Mapping[str, Mapping[str, object]] | None = None,
                      artifact_identities: Mapping[str, IdentityEnvelope] | None = None,
                      trusted_artifact_receipt_digests: Mapping[str, str] | None = None) -> dict:
    validity = validate_pair(left, right, trusted_manifest_digests=trusted_manifest_digests,
                             trusted_isolation_digests=trusted_isolation_digests, expected_treatments=expected_treatments)
    mismatched = any(dict(manifest.controls.fingerprints)["scenario"] != scenario.digest
                     or dict(manifest.controls.fingerprints)["fixture"] != scenario.fixture_sha256
                     or dict(manifest.controls.fingerprints)["verifier"] != verifier_digest(scenario)
                     for manifest in (left, right))
    status = "INVALID" if mismatched else validity.status
    reasons = list(validity.reason_codes) + (["TRUSTED_SCENARIO_MISMATCH"] if mismatched else [])
    runs = [{"run_id": manifest.run_id, "variant": manifest.variant,
             "outcome": _outcome(scenario, outcomes.get(manifest.run_id),
                                  None if trusted_outcome_digests is None else trusted_outcome_digests.get(manifest.run_id))}
            for manifest in (left, right)]
    results = {}
    for variant in ("vanilla", "aek"):
        members = [run for run in runs if run["variant"] == variant]
        results[variant] = members[0]["outcome"] if len(members) == 1 else {
            "status": "NOT_EVALUATED", "reason_codes": ["PAIR_MEMBER_AMBIGUOUS"], "evidence_sha256": None}
    process = {"status": "NOT_EVALUATED", "reason_codes": ["SEMANTIC_EVENTS_NOT_BOUND"]}
    if workflow_events is not None:
        rows = [_process(manifest, workflow_events.get(manifest.run_id),
                         None if trusted_workflow_digests is None else trusted_workflow_digests.get(manifest.run_id),
                         False if complete_workflow_capture is None else complete_workflow_capture.get(manifest.run_id, False),
                         trusted_hidden_isolation_digests) for manifest in (left, right)]
        process = {"status": _process_state({row["status"] for row in rows}), "runs": rows,
                   "scope": "anchored_semantic_analysis", "modifies_source": False}
    artifacts = {"status": "NOT_EVALUATED", "reason_codes": ["ARTIFACT_REQUIREMENTS_NOT_BOUND"]}
    if artifact_receipts is not None:
        rows = [_artifacts(scenario, manifest, artifact_receipts.get(manifest.run_id),
                           None if artifact_identities is None else artifact_identities.get(manifest.run_id),
                           None if trusted_artifact_receipt_digests is None else trusted_artifact_receipt_digests.get(manifest.run_id))
                for manifest in (left, right)]
        artifacts = {"status": _artifact_state({row["status"] for row in rows}), "runs": rows,
                     "scope": "current_identity_bound_receipts"}
    return {
        "schema_version": 1, "experiment_id": left.experiment_id, "pair_id": left.pair_id,
        "task_id": left.task_id, "repetition": left.repetition, "agent": left.controls.agent,
        "requested_model": left.controls.model,
        "controls_sha256": hashlib.sha256(json.dumps(asdict(left.controls), sort_keys=True,
                                                     separators=(",", ":")).encode("utf-8")).hexdigest(),
        "validity": {"status": status, "reason_codes": reasons, "scope": validity.scope}, "outcome": results,
        "runs": runs,
        "process": process,
        "cost": {"status": "NOT_EVALUATED", "reason_codes": ["USAGE_NOT_BOUND"]},
        "artifacts": artifacts,
        "exploratory": True, "benefit_claim_supported": False,
        "run_refs": [{"run_id": manifest.run_id, "variant": manifest.variant,
                      "order": manifest.order, "manifest_sha256": manifest.digest} for manifest in (left, right)],
    }


def _artifacts(scenario: ScenarioSpec, manifest: RunManifest, payload, identity, anchor) -> dict:
    row = {"run_id": manifest.run_id, "status": "NOT_EVALUATED", "identity_state": "UNKNOWN",
           "reason_codes": ["ARTIFACT_RECEIPT_MISSING"], "receipt_sha256": None}
    if payload is None or identity is None:
        return row
    try:
        parts = identity_as_dict(identity)["components"]
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("capacity")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {**row, "status": "INVALID", "identity_state": "INVALID", "reason_codes": ["ARTIFACT_INPUT_INVALID"]}
    if "manifest" not in parts["context"] or "scenario" not in parts["subject"]:
        return {**row, "reason_codes": ["ARTIFACT_RUN_IDENTITY_MISSING"]}
    if parts["context"]["manifest"] != manifest.digest or parts["subject"]["scenario"] != scenario.digest:
        return {**row, "status": "STALE", "identity_state": "STALE", "reason_codes": ["ARTIFACT_RUN_IDENTITY_MISMATCH"]}
    result = evaluate_receipt(payload, identity, _ARTIFACT_COMPONENTS, trusted_receipt_digest=anchor)
    row.update(status=result.status, identity_state=result.identity_state, reason_codes=list(result.reason_codes))
    if isinstance(anchor, str) and re.fullmatch(r"[a-f0-9]{64}", anchor):
        row["receipt_sha256"] = anchor
    return row


def _artifact_state(states: set[str]) -> str:
    for state in ("INVALID", "STALE", "FAIL", "NOT_EVALUATED"):
        if state in states:
            return state
    return "PASS" if states == {"PASS"} else "NOT_EVALUATED"


def _artifact_summary(payload, run_ids: set[str]) -> dict:
    invalid = {"status": "INVALID", "reason_codes": ["ARTIFACT_REPORT_CONTENT_INVALID"], "runs": []}
    statuses = {"PASS", "FAIL", "STALE", "INVALID", "NOT_EVALUATED"}
    if type(payload) is not dict or type(payload.get("status")) is not str or payload["status"] not in statuses:
        return invalid
    if payload["status"] == "NOT_EVALUATED" and "runs" not in payload:
        return {"status": "NOT_EVALUATED", "runs": []}
    rows = payload.get("runs")
    if (type(rows) is not list or len(rows) != 2 or any(type(row) is not dict for row in rows)
            or any(type(row.get("run_id")) is not str for row in rows)
            or {row["run_id"] for row in rows} != run_ids or len(run_ids) != 2):
        return invalid
    result = []
    for row in rows:
        state, identity, digest = row.get("status"), row.get("identity_state"), row.get("receipt_sha256")
        reasons = row.get("reason_codes")
        if (type(state) is not str or state not in statuses or type(identity) is not str
                or identity not in {"VALID", "INVALID", "STALE", "UNKNOWN"}
                or digest is not None and (type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest))
                or state in {"PASS", "FAIL"} and (identity != "VALID" or digest is None)
                or type(reasons) is not list or len(reasons) > 128
                or any(type(reason) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", reason) for reason in reasons)):
            return invalid
        result.append({"run_id": row["run_id"], "status": state, "identity_state": identity,
                       "receipt_sha256": digest, "reason_codes": list(reasons)})
    state = _artifact_state({row["status"] for row in result})
    return {"status": state, "runs": result} if state == payload["status"] else invalid


def _process(manifest: RunManifest, events: tuple[WorkflowEvent, ...] | None,
             anchors: frozenset[str] | None, complete: bool, hidden_isolation: frozenset[str] | None) -> dict:
    row = {"run_id": manifest.run_id, "status": "NOT_EVALUATED", "reason_codes": ["SEMANTIC_EVENTS_NOT_BOUND"]}
    if events is None:
        return row
    if (type(events) is not tuple or len(events) > 10_000
            or any(not isinstance(event, WorkflowEvent) for event in events)):
        return {**row, "status": "INVALID", "reason_codes": ["WORKFLOW_SHAPE_INVALID"]}
    if any((event.run_id, event.pair_id, event.task_id) != (manifest.run_id, manifest.pair_id, manifest.task_id)
           for event in events):
        return {**row, "status": "INVALID", "reason_codes": ["WORKFLOW_SUBJECT_MISMATCH"]}
    if any(event.manifest_sha256 is None for event in events):
        return {**row, "reason_codes": ["WORKFLOW_MANIFEST_MISSING"]}
    manifest_digest = manifest.digest
    if any(event.manifest_sha256 != manifest_digest for event in events):
        return {**row, "status": "STALE", "reason_codes": ["WORKFLOW_MANIFEST_MISMATCH"]}
    try:
        analysis = analyze_workflow(events, trusted_event_digests=anchors, complete_capture=complete,
                                    trusted_hidden_isolation_digests=hidden_isolation)
    except ValueError:
        return {**row, "status": "INVALID", "reason_codes": ["WORKFLOW_INPUT_INVALID"]}
    states = {rule["status"] for rule in analysis["rules"].values()}
    evaluated = states != {"NOT_EVALUATED"}
    row.update(status="PARTIAL" if evaluated and "NOT_EVALUATED" in states else
               "EVALUATED" if evaluated else "NOT_EVALUATED",
               reason_codes=[] if evaluated else ["SEMANTIC_EVIDENCE_INCOMPLETE"],
               rules=analysis["rules"], findings_count=len(analysis["findings"]),
               sample_findings=analysis["findings"][:3], omitted_findings_count=max(0, len(analysis["findings"]) - 3),
               analysis_sha256=hashlib.sha256(json.dumps(analysis, sort_keys=True, separators=(",", ":"),
                                                       allow_nan=False).encode("utf-8")).hexdigest())
    return row


def _process_state(states: set[str]) -> str:
    return ("INVALID" if "INVALID" in states else "STALE" if "STALE" in states else
            "EVALUATED" if states == {"EVALUATED"} else "PARTIAL" if states & {"EVALUATED", "PARTIAL"} else
            "NOT_EVALUATED")


def _process_summary(payload, run_ids: set[str]) -> dict:
    invalid = {"status": "INVALID", "reason_codes": ["PROCESS_REPORT_CONTENT_INVALID"], "runs": []}
    statuses = {"EVALUATED", "PARTIAL", "NOT_EVALUATED", "INVALID", "STALE"}
    if type(payload) is not dict or type(payload.get("status")) is not str or payload["status"] not in statuses:
        return invalid
    if payload["status"] == "NOT_EVALUATED" and "runs" not in payload:
        return {"status": "NOT_EVALUATED", "runs": []}
    rows = payload.get("runs")
    if (type(rows) is not list or len(rows) != 2 or any(type(row) is not dict for row in rows)
            or any(type(row.get("run_id")) is not str for row in rows)
            or {row["run_id"] for row in rows} != run_ids or len(run_ids) != 2):
        return invalid
    summaries = []
    for row in rows:
        state = row.get("status")
        if type(state) is not str or state not in statuses:
            return invalid
        summary = {"run_id": row["run_id"], "status": state, "findings_count": None, "analysis_sha256": None}
        if state in {"EVALUATED", "PARTIAL"}:
            rules, count, digest = row.get("rules"), row.get("findings_count"), row.get("analysis_sha256")
            if (type(rules) is not dict or set(rules) != {f"WA-00{index}" for index in range(1, 8)}
                    or any(type(rule) is not dict or type(rule.get("status")) is not str
                           or rule["status"] not in {"FINDINGS", "NO_FINDING", "NOT_EVALUATED"} for rule in rules.values())
                    or type(count) is not int or not 0 <= count <= 10_000
                    or type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                return invalid
            rule_states = {rule["status"] for rule in rules.values()}
            if (rule_states == {"NOT_EVALUATED"} or (state == "EVALUATED") != ("NOT_EVALUATED" not in rule_states)
                    or (count > 0) != ("FINDINGS" in rule_states)):
                return invalid
            summary.update(findings_count=count, analysis_sha256=digest,
                           rules={key: rule["status"] for key, rule in rules.items()})
        summaries.append(summary)
    state = _process_state({row["status"] for row in summaries})
    if state != payload["status"]:
        return invalid
    return {"status": state, "runs": summaries}


def _cell(value) -> str:
    return html.escape(str(value)).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def report_digest(report: dict) -> str:
    try:
        raw = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if type(report) is not dict or len(raw) > 64 * 1024:
            raise ValueError("size")
        return hashlib.sha256(raw).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("PAIR_REPORT_INVALID") from exc


def report_from_json(raw: bytes) -> dict:
    def unique(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError("nonfinite")

    try:
        if type(raw) is not bytes or len(raw) > 64 * 1024:
            raise ValueError("size")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
        report_digest(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("PAIR_REPORT_JSON_INVALID") from exc


def build_experiment_report(reports: tuple[dict, ...], *,
                            trusted_report_digests: frozenset[str] | None = None) -> dict:
    """Describe all samples, but compare only anchored, valid, conclusive pairs."""
    statuses = ("PASS", "FAIL", "INFRA_ERROR", "NOT_EVALUATED", "INVALID", "STALE")
    if type(reports) is not tuple or len(reports) > 1024:
        raise ValueError("EXPERIMENT_REPORT_INVALID")
    pairs, groups = [], {}
    subjects = []
    run_ids = []
    for report in reports:
        try:
            if (type(report) is not dict or type(report.get("schema_version")) is not int
                    or report["schema_version"] != 1
                    or any(type(report.get(key)) is not str
                           or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", report[key])
                           for key in ("experiment_id", "pair_id", "task_id"))
                    or report["agent"] not in {"codex", "claude"}
                    or type(report["requested_model"]) is not str
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+\[\]-]{0,127}", report["requested_model"])
                    or type(report["controls_sha256"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", report["controls_sha256"])
                    or type(report["repetition"]) is not int or not 0 < report["repetition"] <= 1000
                    or report["validity"]["status"] not in {"VALID", "INVALID", "NOT_EVALUATED"}
                    or type(report["validity"]["reason_codes"]) is not list
                    or len(report["validity"]["reason_codes"]) > 128
                    or any(type(reason) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", reason)
                           for reason in report["validity"]["reason_codes"])
                    or type(report["run_refs"]) is not list or len(report["run_refs"]) != 2
                    or any(type(run) is not dict or type(run.get("run_id")) is not str
                           or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", run["run_id"])
                           or type(run.get("manifest_sha256")) is not str
                           or not re.fullmatch(r"[a-f0-9]{64}", run["manifest_sha256"]) for run in report["run_refs"])
                    or any(report["outcome"][variant]["status"] not in statuses for variant in ("vanilla", "aek"))):
                raise ValueError("shape")
            subjects.append((report["experiment_id"], report["pair_id"]))
            run_ids.extend(run["run_id"] for run in report["run_refs"])
            report_digest(report)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("EXPERIMENT_REPORT_INVALID") from exc
    if len({subject[0] for subject in subjects}) > 1:
        raise ValueError("EXPERIMENT_SUBJECT_MISMATCH")
    duplicate_subjects = {subject for subject, count in Counter(subjects).items() if count > 1}
    duplicate_runs = {run_id for run_id, count in Counter(run_ids).items() if count > 1}
    counts = {"VALID": 0, "INVALID": 0, "NOT_EVALUATED": 0}
    process_counts = {state: 0 for state in ("EVALUATED", "PARTIAL", "NOT_EVALUATED", "INVALID", "STALE")}
    artifact_counts = {state: 0 for state in ("PASS", "FAIL", "NOT_EVALUATED", "INVALID", "STALE")}
    for report, subject in zip(reports, subjects):
        digest = report_digest(report)
        status = report["validity"]["status"]
        reasons = list(report["validity"]["reason_codes"])
        if trusted_report_digests is None or digest not in trusted_report_digests:
            status = "NOT_EVALUATED" if status != "INVALID" else status
            reasons.append("PAIR_REPORT_ANCHOR_MISSING")
        if subject in duplicate_subjects:
            status = "INVALID"
            reasons.append("DUPLICATE_PAIR_SAMPLE")
        if any(run["run_id"] in duplicate_runs for run in report["run_refs"]):
            status = "INVALID"
            reasons.append("DUPLICATE_RUN_SAMPLE")
        counts[status] += 1
        process = (_process_summary(report.get("process"), {run["run_id"] for run in report["run_refs"]})
                   if status == "VALID" else {"status": "NOT_EVALUATED", "runs": [], "reason_codes": ["PAIR_NOT_VALID"]})
        artifacts = (_artifact_summary(report.get("artifacts"), {run["run_id"] for run in report["run_refs"]})
                     if status == "VALID" else {"status": "NOT_EVALUATED", "runs": [], "reason_codes": ["PAIR_NOT_VALID"]})
        outcomes = {variant: report["outcome"][variant]["status"] for variant in ("vanilla", "aek")}
        pairs.append({"pair_id": report["pair_id"], "task_id": report["task_id"],
                      "repetition": report["repetition"], "agent": report["agent"],
                      "requested_model": report["requested_model"], "validity": status,
                      "controls_sha256": report["controls_sha256"],
                      "reason_codes": reasons, "outcome": outcomes, "process": process,
                      "artifacts": artifacts, "report_sha256": digest})
        if status != "VALID":
            continue
        process_counts[process["status"]] += 1
        artifact_counts[artifacts["status"]] += 1
        key = (report["agent"], report["requested_model"], report["task_id"], report["controls_sha256"])
        if key not in groups:
            groups[key] = {"agent": key[0], "requested_model": key[1], "task_id": key[2],
                           "controls_sha256": key[3],
                           "valid_pairs": 0,
                           "outcome_counts": {variant: {state: 0 for state in statuses} for variant in ("vanilla", "aek")},
                           "comparison": {"eligible_pairs": 0, "both_pass": 0, "aek_only_pass": 0,
                                          "vanilla_only_pass": 0, "both_fail": 0}}
        group = groups[key]
        group["valid_pairs"] += 1
        for variant, outcome in outcomes.items():
            group["outcome_counts"][variant][outcome] += 1
        if all(outcome in {"PASS", "FAIL"} for outcome in outcomes.values()):
            comparison = group["comparison"]
            comparison["eligible_pairs"] += 1
            category = ("both_pass" if outcomes["vanilla"] == outcomes["aek"] == "PASS" else
                        "both_fail" if outcomes["vanilla"] == outcomes["aek"] == "FAIL" else
                        "aek_only_pass" if outcomes["aek"] == "PASS" else "vanilla_only_pass")
            comparison[category] += 1
    return {"schema_version": 1, "experiment_id": subjects[0][0] if subjects else None,
            "pairs": pairs, "validity_counts": counts, "groups": list(groups.values()),
            "process": {"status": _process_state({state for state, count in process_counts.items() if count}),
                        "pair_status_counts": process_counts, "scope": "valid_anchored_pairs"},
            "cost": {"status": "NOT_EVALUATED"},
            "artifacts": {"status": _artifact_state({state for state, count in artifact_counts.items() if count}),
                          "pair_status_counts": artifact_counts, "scope": "valid_anchored_pairs"},
            "exploratory": True, "benefit_claim_supported": False}


def render_markdown(report: dict) -> str:
    if report.get("schema_version") != 1:
        raise ValueError("REPORT_SCHEMA_INVALID")
    lines = [f"# Pair {_cell(report['pair_id'])}", "",
             f"Validity: {_cell(report['validity']['status'])}", "",
             "| Variant | Outcome |", "| --- | --- |"]
    for variant in ("vanilla", "aek"):
        lines.append(f"| {variant} | {_cell(report['outcome'][variant]['status'])} |")
    lines += ["", f"Process: {_cell(report['process']['status'])}",
              f"Cost: {_cell(report['cost']['status'])}", f"Artifacts: {_cell(report['artifacts']['status'])}",
              "", "Exploratory sample; no combined score or supported benefit claim."]
    if report["validity"]["reason_codes"]:
        lines += ["", "Reasons: " + ", ".join(_cell(reason) for reason in report["validity"]["reason_codes"])]
    return "\n".join(lines) + "\n"


def render_experiment_markdown(report: dict) -> str:
    if report.get("schema_version") != 1:
        raise ValueError("REPORT_SCHEMA_INVALID")
    lines = [f"# Experiment {_cell(report['experiment_id'])}", "",
             "| Pair | Agent | Model | Validity | Vanilla | AEK |", "| --- | --- | --- | --- | --- | --- |"]
    for pair in report["pairs"]:
        lines.append("| " + " | ".join(_cell(value) for value in (
            pair["pair_id"], pair["agent"], pair["requested_model"], pair["validity"],
            pair["outcome"]["vanilla"], pair["outcome"]["aek"])) + " |")
    for group in report["groups"]:
        lines += ["", f"## {_cell(group['agent'])} / {_cell(group['requested_model'])} / {_cell(group['task_id'])}", "",
                  f"Controls: {_cell(group['controls_sha256'])}", "",
                  "| Paired outcome | Count |", "| --- | --- |"]
        lines.extend(f"| {_cell(key)} | {_cell(value)} |" for key, value in group["comparison"].items())
    lines += ["", f"Process: {_cell(report['process']['status'])}", f"Cost: {_cell(report['cost']['status'])}",
              f"Artifacts: {_cell(report['artifacts']['status'])}",
              "", "Exploratory samples; no combined score or supported benefit claim."]
    return "\n".join(lines) + "\n"
