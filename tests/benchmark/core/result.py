"""Independent verification results, separate from process compliance/cost."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    id: str
    kind: str
    status: str
    reason_code: str
    exit_code: int | None
    script_sha256: str
    stdout_sha256: str
    stderr_sha256: str
    duration_seconds: float


@dataclass(frozen=True)
class OutcomeResult:
    status: str
    checks: tuple[CheckResult, ...]
    scenario_digest: str
    scope: str = "development_independent_checks"


def aggregate_checks(checks: tuple[CheckResult, ...], scenario_digest: str) -> OutcomeResult:
    if not checks or not any(item.kind == "outcome" for item in checks):
        status = "NOT_EVALUATED"
    elif any(item.status == "FAIL" for item in checks):
        status = "FAIL"
    elif any(item.status == "INFRA_ERROR" for item in checks):
        status = "INFRA_ERROR"
    elif any(item.status != "PASS" for item in checks):
        status = "NOT_EVALUATED"
    else:
        status = "PASS"
    return OutcomeResult(status, checks, scenario_digest)
