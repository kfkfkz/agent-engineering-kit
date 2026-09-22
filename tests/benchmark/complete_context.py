"""Controlled, instrumented replay of four routed delivery material streams.

The 1.0.0 Kit is read from its pinned Git commit. Task code/doc/test fixtures
are identical on both sides. Every text handed to the simulated Agent passes
through one loader which records its actual UTF-8 bytes in ContextLedger.
This is a fixture benchmark, not a claim about unobserved host sessions.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aek.adapters.telemetry import ContextLedger
from aek.core.context.reference_catalog import REFERENCE_CATALOG
from aek.core.context.telemetry import ContextReport, build_context_report

from context import BenchmarkCase


CHANNELS = ("skill_loader", "reference_loader", "code_loader",
            "test_loader", "memory_loader", "document_loader", "review_loader")
ROOT_SKILL = "skills/repo-delivery/SKILL.md"
CODE_PATH = "installer/atomic.py"
TEST_PATH = "tests/benchmark/fixtures/task-test.py"
DOCUMENT_PATH = "vendor/archify/examples/checkout-platform.base.architecture.json"
REVIEW_PATH = "vendor/archify/examples/checkout-platform.head.architecture.json"


@dataclass(frozen=True)
class CompleteReplay:
    case_id: str
    mode: str
    fixture_digest: str
    report: ContextReport
    delivered_paths: tuple[str, ...]


def _pinned_text(repo: Path, commit: str, path: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True, check=False)
    if process.returncode:
        raise ValueError(f"pinned material is unavailable: {path}")
    return process.stdout.decode("utf-8", errors="strict")


def _fixture_digest(repo: Path, case: BenchmarkCase) -> str:
    fixture = (repo / TEST_PATH).read_bytes()
    payload = {
        "case_id": case.case_id, "route": case.route,
        "baseline_commit": case.baseline_commit,
        "code_digest": hashlib.sha256(_pinned_text(
            repo, case.baseline_commit, CODE_PATH).encode("utf-8")).hexdigest(),
        "test_digest": hashlib.sha256(fixture).hexdigest(),
        "document_digest": hashlib.sha256(_pinned_text(
            repo, case.baseline_commit, DOCUMENT_PATH).encode("utf-8")).hexdigest(),
        "review_digest": hashlib.sha256(_pinned_text(
            repo, case.baseline_commit, REVIEW_PATH).encode("utf-8")).hexdigest(),
        "channel_catalog": CHANNELS,
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def replay_case(repo: Path, case: BenchmarkCase, *, mode: str) -> CompleteReplay:
    if mode not in {"baseline", "current"}:
        raise ValueError("unknown replay mode")
    repo = Path(repo).resolve()
    fixture_digest = _fixture_digest(repo, case)
    with tempfile.TemporaryDirectory(prefix="aek-context-replay-") as tmp:
        ledger = ContextLedger(Path(tmp) / "events.jsonl")
        delivered: list[str] = []

        def deliver(text: str, path: str, kind: str, channel: str,
                    stage: str, purpose: str) -> None:
            ledger.append_text(
                text, route=case.route, stage=stage, source_kind=kind,
                channel=channel, purpose=purpose, cache_hit=False,
                session_id=f"{case.case_id}-{mode}",
                subject_digest=case.diff_digest, logical_path=path,
                occurred_at="2026-09-20T00:00:00Z")
            delivered.append(path)

        old_skills = tuple(item.path for item in case.materials)
        if ROOT_SKILL not in old_skills:
            raise ValueError("baseline case omits the root Skill")
        if mode == "baseline":
            for path in old_skills:
                deliver(_pinned_text(repo, case.baseline_commit, path), path,
                        "skill", "skill_loader", "routing", "route_required")
        else:
            for path in (ROOT_SKILL,) + tuple(
                    item for item in old_skills if item != ROOT_SKILL):
                deliver((repo / path).read_text(encoding="utf-8"), path,
                        "skill", "skill_loader", "routing", "route_required")
            refs = (REFERENCE_CATALOG.for_route(case.route, "route") +
                    REFERENCE_CATALOG.for_route(case.route, "closeout"))
            for ref in refs:
                stage = "closeout" if ref.id == "evidence-closeout" else "routing"
                deliver((repo / ref.source_path).read_text(encoding="utf-8"),
                        ref.source_path, "reference", "reference_loader",
                        stage, "route_required")

        # Task fixture inputs are fixed and identical on both Kit versions.
        deliver(_pinned_text(repo, case.baseline_commit, CODE_PATH), CODE_PATH,
                "code", "code_loader", "execution", "target_evidence")
        deliver((repo / TEST_PATH).read_text(encoding="utf-8"), TEST_PATH,
                "test", "test_loader", "execution", "target_evidence")
        if case.route != "direct":
            metadata = f"candidate:{DOCUMENT_PATH}:sha256=" + hashlib.sha256(
                _pinned_text(repo, case.baseline_commit, DOCUMENT_PATH).encode(
                    "utf-8")).hexdigest()
            deliver(metadata, "generated:memory-candidate", "memory",
                    "memory_loader", "routing", "target_evidence")
            deliver(_pinned_text(repo, case.baseline_commit, DOCUMENT_PATH),
                    DOCUMENT_PATH, "document", "document_loader", "design",
                    "target_evidence")
            deliver(_pinned_text(repo, case.baseline_commit, REVIEW_PATH),
                    REVIEW_PATH, "document", "review_loader", "design",
                    "review_required")
        events = ledger.read()
        expected_count = (len(old_skills) + (0 if mode == "baseline" else len(refs))
                          + 2 + (0 if case.route == "direct" else 3))
        if len(events) != expected_count or len(delivered) != expected_count:
            raise ValueError("controlled material stream is incomplete")
        report = build_context_report(
            events, coverage="complete", expected_channels=CHANNELS,
            observed_channels=CHANNELS, bypass_detected=False)
        return CompleteReplay(case.case_id, mode, fixture_digest,
                              report, tuple(delivered))
