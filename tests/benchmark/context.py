#!/usr/bin/env python3
"""Deterministic Context benchmark baseline and comparison contracts.

This module is intentionally test-owned in T1.  Production telemetry will consume
the same observable contract in a later vertical slice; keeping the baseline
evaluator independent prevents the implementation from grading itself.
"""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROUTE_THRESHOLDS = {
    "direct": 0.50,
    "bounded": 0.30,
    "standard": 0.15,
    "initiative": 0.0,
}


class BaselineError(ValueError):
    """The frozen benchmark baseline is malformed or ambiguous."""


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise BaselineError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BaselineError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class MaterialRecord:
    path: str
    sha256: str
    bytes: int
    chars: int


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    route: str
    baseline_commit: str
    fixture_digest: str
    diff_digest: str
    environment_digest: str
    cold_cache: bool
    baseline_bytes: int
    baseline_chars: int
    material_set_digest: str
    materials: tuple[MaterialRecord, ...]


@dataclass(frozen=True)
class BaselineManifest:
    schema_version: int
    baseline_commit: str
    metric: str
    scope: str
    cases: tuple[BenchmarkCase, ...]


@dataclass(frozen=True)
class EquivalenceReceipt:
    task: bool
    security: bool
    coverage: bool
    exit_codes: bool
    diagnostics: bool
    receipt_digest: str

    @property
    def complete(self) -> bool:
        return all((self.task, self.security, self.coverage,
                    self.exit_codes, self.diagnostics))


@dataclass(frozen=True)
class CaseComparison:
    case_id: str
    route: str
    verdict: str
    reduction_bytes: float
    reduction_chars: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteComparison:
    route: str
    verdict: str
    worst_reduction_bytes: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseVerdict:
    verdict: str
    reasons: tuple[str, ...]


def _case_from_dict(raw: Any, baseline_commit: str) -> BenchmarkCase:
    if not isinstance(raw, dict):
        raise BaselineError("case must be an object")
    required = {
        "case_id", "route", "fixture_digest", "diff_digest",
        "environment_digest", "cold_cache", "baseline_bytes",
        "baseline_chars", "material_set_digest", "materials",
    }
    unknown = set(raw) - required
    missing = required - set(raw)
    if missing or unknown:
        raise BaselineError(
            f"case fields mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}")
    route = raw["route"]
    if route not in _ROUTE_THRESHOLDS:
        raise BaselineError(f"unknown route: {route!r}")
    if not isinstance(raw["case_id"], str) or not raw["case_id"]:
        raise BaselineError("case_id must be a non-empty string")
    if raw["cold_cache"] is not True:
        raise BaselineError("baseline cases must use cold_cache=true")
    materials = raw["materials"]
    if not isinstance(materials, list) or not materials:
        raise BaselineError("materials must be a non-empty list")
    paths: list[str] = []
    records: list[MaterialRecord] = []
    summed_bytes = 0
    summed_chars = 0
    for index, material in enumerate(materials):
        if not isinstance(material, dict) or set(material) != {
                "path", "sha256", "bytes", "chars"}:
            raise BaselineError(f"materials[{index}] has invalid fields")
        path = material["path"]
        if (not isinstance(path, str) or not path or path.startswith("/")
                or "\\" in path or "\0" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or path in paths):
            raise BaselineError(f"materials[{index}].path is unsafe or duplicate")
        paths.append(path)
        material_hash = _require_digest(
            material["sha256"], f"materials[{index}].sha256")
        byte_count = _require_non_negative_int(
            material["bytes"], f"materials[{index}].bytes")
        char_count = _require_non_negative_int(
            material["chars"], f"materials[{index}].chars")
        summed_bytes += byte_count
        summed_chars += char_count
        records.append(MaterialRecord(path, material_hash, byte_count, char_count))
    baseline_bytes = _require_non_negative_int(
        raw["baseline_bytes"], "baseline_bytes")
    baseline_chars = _require_non_negative_int(
        raw["baseline_chars"], "baseline_chars")
    if baseline_bytes <= 0 or baseline_chars <= 0:
        raise BaselineError("baseline totals must be positive")
    if (baseline_bytes, baseline_chars) != (summed_bytes, summed_chars):
        raise BaselineError("baseline totals do not match materials")
    canonical_materials = json.dumps(
        materials, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    set_digest = hashlib.sha256(canonical_materials).hexdigest()
    if raw["material_set_digest"] != set_digest:
        raise BaselineError("material_set_digest does not match materials")
    return BenchmarkCase(
        case_id=raw["case_id"],
        route=route,
        baseline_commit=baseline_commit,
        fixture_digest=_require_digest(raw["fixture_digest"], "fixture_digest"),
        diff_digest=_require_digest(raw["diff_digest"], "diff_digest"),
        environment_digest=_require_digest(
            raw["environment_digest"], "environment_digest"),
        cold_cache=True,
        baseline_bytes=baseline_bytes,
        baseline_chars=baseline_chars,
        material_set_digest=_require_digest(raw["material_set_digest"], "material_set_digest"),
        materials=tuple(records),
    )


def load_baseline(path: Path) -> BaselineManifest:
    """Load a frozen 1.0.0 baseline with strict, fail-closed validation."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline: {exc}") from exc
    if not isinstance(raw, dict):
        raise BaselineError("baseline must be an object")
    required = {"schema_version", "baseline_commit", "metric", "scope", "cases"}
    if set(raw) != required:
        raise BaselineError("baseline contains missing or unknown top-level fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise BaselineError("unsupported baseline schema_version")
    baseline_commit = raw["baseline_commit"]
    if not isinstance(baseline_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}", baseline_commit):
        raise BaselineError("baseline_commit must be a full Git SHA-1")
    if raw["metric"] != "delivered_utf8_bytes":
        raise BaselineError("metric must be delivered_utf8_bytes")
    if (not isinstance(raw["scope"], str) or raw["scope"] not in {
            "selected_skill_material_only", "complete_delivered_context"}):
        raise BaselineError("unknown baseline scope")
    if not isinstance(raw["cases"], list):
        raise BaselineError("cases must be a list")
    cases = tuple(_case_from_dict(item, baseline_commit) for item in raw["cases"])
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise BaselineError("case_id values must be unique")
    routes = [case.route for case in cases]
    if set(routes) != set(_ROUTE_THRESHOLDS):
        raise BaselineError("baseline must cover all four routes")
    return BaselineManifest(
        schema_version=1,
        baseline_commit=baseline_commit,
        metric=raw["metric"],
        scope=raw["scope"],
        cases=cases,
    )


def verify_baseline_materials(manifest: BaselineManifest, repo: Path) -> None:
    """Re-read every frozen material at the pinned commit, not the worktree."""
    for case in manifest.cases:
        if case.baseline_commit != manifest.baseline_commit:
            raise BaselineError(f"case {case.case_id} has another baseline commit")
        for material in case.materials:
            result = subprocess.run(
                ["git", "-C", str(repo), "show",
                 f"{manifest.baseline_commit}:{material.path}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if result.returncode != 0:
                raise BaselineError(
                    f"cannot read historical material: {case.case_id}/{material.path}")
            content = result.stdout
            try:
                chars = len(content.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise BaselineError(
                    f"historical material is not UTF-8: {material.path}") from exc
            if (hashlib.sha256(content).hexdigest() != material.sha256
                    or len(content) != material.bytes or chars != material.chars):
                raise BaselineError(
                    f"historical material mismatch: {case.case_id}/{material.path}")


def compare_case(
        baseline: BenchmarkCase,
        report: dict[str, Any],
        equivalence: EquivalenceReceipt) -> CaseComparison:
    """Compare one complete, identity-bound report against its frozen case."""
    reasons: list[str] = []
    if report.get("coverage") != "complete":
        reasons.append("coverage")
    identity = {
        "case_id": baseline.case_id,
        "route": baseline.route,
        "fixture_digest": baseline.fixture_digest,
        "diff_digest": baseline.diff_digest,
        "environment_digest": baseline.environment_digest,
        "cold_cache": True,
    }
    for field, expected in identity.items():
        if report.get(field) != expected:
            reasons.append(f"identity:{field}")
    baseline_commit = report.get("baseline_commit")
    if baseline_commit != baseline.baseline_commit:
        reasons.append("identity:baseline_commit")
    try:
        delivered_bytes = _require_non_negative_int(
            report.get("delivered_bytes"), "delivered_bytes")
        delivered_chars = _require_non_negative_int(
            report.get("delivered_chars"), "delivered_chars")
    except BaselineError:
        delivered_bytes = baseline.baseline_bytes
        delivered_chars = baseline.baseline_chars
        reasons.append("metrics")
    if (not equivalence.complete
            or not _SHA256.fullmatch(equivalence.receipt_digest)):
        reasons.append("equivalence")
    reduction_bytes = 1 - delivered_bytes / baseline.baseline_bytes
    reduction_chars = 1 - delivered_chars / baseline.baseline_chars
    if reasons:
        return CaseComparison(
            baseline.case_id, baseline.route, "INVALID",
            reduction_bytes, reduction_chars, tuple(sorted(set(reasons))))
    threshold = _ROUTE_THRESHOLDS[baseline.route]
    met = reduction_bytes >= threshold and delivered_chars <= baseline.baseline_chars
    return CaseComparison(
        baseline.case_id, baseline.route,
        "THRESHOLD_MET" if met else "REGRESSION",
        reduction_bytes, reduction_chars,
        () if met else ("threshold",),
    )


def aggregate_route(
        route: str, comparisons: Iterable[CaseComparison]) -> RouteComparison:
    """Use the worst case; an average can never hide a route regression."""
    rows = tuple(comparisons)
    if route not in _ROUTE_THRESHOLDS or not rows:
        raise BaselineError("aggregate_route needs a known route and comparisons")
    if any(row.route != route for row in rows):
        raise BaselineError("cannot aggregate comparisons from another route")
    worst = min(row.reduction_bytes for row in rows)
    if any(row.verdict == "INVALID" for row in rows):
        verdict = "INVALID"
    elif any(row.verdict == "REGRESSION" for row in rows):
        verdict = "REGRESSION"
    else:
        verdict = "THRESHOLD_MET"
    reasons = tuple(sorted({reason for row in rows for reason in row.reasons}))
    return RouteComparison(route, verdict, worst, reasons)


def release_verdict(
        manifest: BaselineManifest,
        routes: Iterable[RouteComparison]) -> ReleaseVerdict:
    """Never promote a component-material fixture to an A23 release claim."""
    rows = tuple(routes)
    reasons: list[str] = []
    if manifest.scope != "complete_delivered_context":
        reasons.append("baseline_scope")
    if len(rows) != len(_ROUTE_THRESHOLDS) or {
            row.route for row in rows} != set(_ROUTE_THRESHOLDS):
        reasons.append("route_coverage")
    if any(row.verdict == "INVALID" for row in rows):
        reasons.append("invalid_case")
    if reasons:
        return ReleaseVerdict("INVALID", tuple(reasons))
    if any(row.verdict == "REGRESSION" for row in rows):
        return ReleaseVerdict("REGRESSION", ("route_threshold",))
    if any(row.verdict != "THRESHOLD_MET" for row in rows):
        return ReleaseVerdict("INVALID", ("unknown_route_verdict",))
    return ReleaseVerdict("THRESHOLD_MET", ())
