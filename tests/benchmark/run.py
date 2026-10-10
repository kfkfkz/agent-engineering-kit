#!/usr/bin/env python3
"""Development-side diagnostics and explicit fake pairs. Live execution is disabled."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.cli import probe_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="probe CLI help/version only, without model calls")
    doctor.add_argument("--agent", choices=("codex", "claude"), required=True)
    doctor.add_argument("--cli", type=Path, help="explicit trusted CLI executable")
    doctor.add_argument("--timeout-seconds", type=int, default=5)
    paired = commands.add_parser("paired", help="development-only pair of pinned synthetic agents")
    selected = paired.add_mutually_exclusive_group(required=True)
    selected.add_argument("--scenario-file", type=Path)
    selected.add_argument("--resume-pair", type=Path, help="existing development pair")
    paired.add_argument("--manifest-sha256", help="external manifest anchor required for recovery")
    paired.add_argument("--output-parent", type=Path, help="existing trusted result directory for a new pair")
    paired.add_argument("--fake-agent", type=Path, help="explicit trusted Python fixture")
    paired.add_argument("--fake-sha256", help="expected SHA-256 of the synthetic agent")
    paired.add_argument("--live", action="store_true", help="not available; rejected without launching models")
    collect = commands.add_parser("collect", help="normalize an explicit JSONL capture; no model calls")
    collect.add_argument("--input", type=Path, required=True)
    collect.add_argument("--agent", choices=("codex", "claude"), required=True)
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--pair-id", required=True)
    collect.add_argument("--task-id", required=True)
    collect.add_argument("--manifest-sha256", required=True)
    collect.add_argument("--session-mode", choices=("fresh", "resumed", "unknown"), default="unknown")
    collect.add_argument("--capture-complete", action="store_true", help="caller declaration, not provenance proof")
    schedule = commands.add_parser("schedule", help="print deterministic pair order; no execution")
    schedule.add_argument("--experiment-id", required=True)
    schedule.add_argument("--tasks", nargs="+", required=True)
    schedule.add_argument("--repeat", type=int, required=True)
    schedule.add_argument("--first-variant", choices=("vanilla", "aek"), default="vanilla")
    report = commands.add_parser("report", help="summarize explicit pair reports; no execution or implicit trust")
    report.add_argument("--input", type=Path, nargs="+", required=True)
    report.add_argument("--sha256", nargs="+", help="external canonical report anchors, in input order")
    report.add_argument("--format", choices=("json", "markdown"), default="json")
    isolation = commands.add_parser("isolation-doctor", help="offline Linux file/network canary probe; no models")
    isolation.add_argument("--backend", type=Path, default=Path("/usr/bin/bwrap"), help="explicit trusted bubblewrap binary")
    isolation.add_argument("--timeout-seconds", type=int, default=5)
    args = parser.parse_args(argv)
    if args.command == "paired":
        return _paired(args)
    if args.command == "collect":
        return _collect(args)
    if args.command == "report":
        return _report(args)
    if args.command == "isolation-doctor":
        from adapters.isolation import probe_linux_isolation
        try:
            result = probe_linux_isolation(args.backend, timeout_seconds=args.timeout_seconds)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "PROBED" else 2
        except ValueError:
            print(json.dumps({"status": "INVALID", "reason_code": "ISOLATION_PROBE_INPUT_INVALID",
                              "live_ready": False, "model_calls": 0}, sort_keys=True))
            return 2
    if args.command == "schedule":
        from dataclasses import asdict

        from core.pair import schedule_pairs
        try:
            pairs = schedule_pairs(args.experiment_id, tuple(args.tasks), repeat=args.repeat,
                                   first_variant=args.first_variant)
            print(json.dumps({"schema_version": 1, "pairs": [asdict(pair) for pair in pairs],
                              "execution": "NOT_STARTED", "live_ready": False}, sort_keys=True))
            return 0
        except ValueError:
            print(json.dumps({"status": "INVALID", "reason_code": "PAIR_SCHEDULE_INVALID"}, sort_keys=True))
            return 2
    cli = str(args.cli.resolve()) if args.cli is not None else (shutil.which(args.agent) or args.agent)
    try:
        # Neither help nor version should run in a candidate-controlled checkout.
        with tempfile.TemporaryDirectory(prefix="aek-cli-probe-") as probe_dir:
            result = probe_cli(args.agent, (cli,), Path(probe_dir),
                               timeout_seconds=args.timeout_seconds)
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if result.status == "COMPATIBLE" else 2
    except ValueError:
        print(json.dumps({"status": "INVALID", "reason_code": "CLI_PROBE_INPUT_INVALID",
                          "live_ready": False}, sort_keys=True))
        return 2


def _collect(args: argparse.Namespace) -> int:
    from adapters.capture_file import read_cli_capture
    from collectors.cli_events import parse_cli_stream
    from core.telemetry import CaptureContext
    try:
        context = CaptureContext(args.run_id, args.pair_id, args.task_id, args.manifest_sha256,
                                 capture_complete=args.capture_complete, session_mode=args.session_mode)
        summary = parse_cli_stream(read_cli_capture(args.input), args.agent, context)
        print(json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2 if summary.status == "INVALID" else 0
    except (OSError, ValueError):
        print(json.dumps({"status": "INVALID", "reason_codes": ["CAPTURE_INPUT_INVALID"],
                          "outcome": "NOT_EVALUATED", "execution_scope": "posthoc_cli_transcript"}, sort_keys=True))
        return 2


def _report(args: argparse.Namespace) -> int:
    from adapters.capture_file import read_cli_capture
    from reports.pair_report import (
        build_experiment_report,
        render_experiment_markdown,
        report_digest,
        report_from_json,
    )
    try:
        if len(args.input) > 256 or args.sha256 is not None and len(args.sha256) != len(args.input):
            raise ValueError("capacity or anchor count")
        reports = tuple(report_from_json(read_cli_capture(path)) for path in args.input)
        if args.sha256 is not None and any(report_digest(report) != anchor
                                          for report, anchor in zip(reports, args.sha256)):
            raise ValueError("anchor mismatch")
        summary = build_experiment_report(reports, trusted_report_digests=(
            None if args.sha256 is None else frozenset(args.sha256)))
        summary["execution_scope"] = "posthoc_pair_reports"
        print(render_experiment_markdown(summary) if args.format == "markdown" else
              json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError):
        print(json.dumps({"status": "INVALID", "reason_code": "REPORT_INPUT_INVALID",
                          "execution_scope": "posthoc_pair_reports"}, sort_keys=True))
        return 2


def _paired(args: argparse.Namespace) -> int:
    if args.live:
        print(json.dumps({"status": "NOT_AVAILABLE", "reason_code": "LIVE_RUNNER_NOT_AVAILABLE",
                          "live_ready": False}, sort_keys=True))
        return 2
    if args.fake_agent is None or args.fake_sha256 is None:
        print(json.dumps({"status": "INVALID", "reason_code": "EXPLICIT_FAKE_AGENT_REQUIRED",
                          "live_ready": False}, sort_keys=True))
        return 2
    if ((args.resume_pair is not None and (args.manifest_sha256 is None or args.output_parent is not None))
            or (args.scenario_file is not None and (args.output_parent is None or args.manifest_sha256 is not None))):
        print(json.dumps({"status": "INVALID", "reason_code": "PAIR_SELECTION_INVALID",
                          "live_ready": False}, sort_keys=True))
        return 2
    from adapters.fake_run import run_fake
    from adapters.scenario_file import load_scenario
    from adapters.workspace import PreparationError, load_prepared_pair, prepare_pair

    from aek.adapters.atomic_file import AtomicFileStore
    try:
        if args.resume_pair is not None:
            prepared = load_prepared_pair(args.resume_pair, expected_manifest_sha256=args.manifest_sha256)
            timeout = prepared.manifest["execution_budget"]["timeout_seconds"]
        else:
            loaded = load_scenario(args.scenario_file)
            prepared = prepare_pair(loaded, args.output_parent)
            timeout = loaded.scenario.timeout_seconds
        runs = [{"variant": variant,
                 "attempt": run_fake(prepared, variant, args.fake_agent,
                                     script_sha256=args.fake_sha256,
                                     timeout_seconds=timeout)}
                for variant in ("vanilla", "aek")]
        report = {
            "schema_version": 1, "pair_id": prepared.root.name,
            "manifest_sha256": prepared.manifest_sha256, "execution_scope": "development_fake",
            "validity": {"status": "NOT_EVALUATED",
                         "reasons": ["FAKE_EXECUTION", "TREATMENT_NOT_APPLIED", "ISOLATION_UNVERIFIED"]},
            "runs": runs, "live_ready": False,
        }
        raw = json.dumps(report, sort_keys=True).encode("utf-8")
        AtomicFileStore(prepared.root).write("pair-result.json", raw)
        print(raw.decode("utf-8"))
        return 0
    except PreparationError as exc:
        print(json.dumps({"status": "INVALID", "reason_code": str(exc), "live_ready": False}, sort_keys=True))
        return 2
    except (OSError, ValueError):
        print(json.dumps({"status": "INVALID", "reason_code": "FAKE_PAIR_FAILED",
                          "live_ready": False}, sort_keys=True))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "NEEDS_HUMAN", "reason_code": "PAIR_INTERRUPTED",
                          "live_ready": False}, sort_keys=True))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
