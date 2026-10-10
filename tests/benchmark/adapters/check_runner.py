"""Trusted independent script checks in development mode, never Agent self-evaluation."""
from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

from core.result import CheckResult, OutcomeResult, aggregate_checks
from core.scenario import MAX_SCENARIO_BYTES, ScenarioError

from installer import platform as fs

from .process import run_process
from .scenario_file import LoadedScenario, load_scenario


def _read_script(root: Path, rel: str) -> bytes:
    fd = fs.secure_open(root, rel, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCENARIO_BYTES:
            raise ValueError("check must be a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_SCENARIO_BYTES + 1)
        if len(data) > MAX_SCENARIO_BYTES:
            raise ValueError("check exceeds the input budget")
        return data
    finally:
        os.close(fd)


def run_checks(loaded: LoadedScenario, workspace: Path) -> OutcomeResult:
    """Only validates development checks; no claim of live hidden-test isolation."""
    results = []
    workspace = Path(workspace).resolve()
    empty = hashlib.sha256(b"").hexdigest()
    if (loaded.source.is_relative_to(workspace)
            or any((loaded.scenario_root / check.entrypoint).is_relative_to(workspace)
                   for check in loaded.scenario.checks)):
        invalid = tuple(CheckResult(check.id, check.kind, "INFRA_ERROR",
                                    "CHECK_TRUST_BOUNDARY_INVALID", None, check.sha256,
                                    empty, empty, 0.0) for check in loaded.scenario.checks)
        return aggregate_checks(invalid, loaded.scenario.digest)
    try:
        current = load_scenario(loaded.source)
        if (current.source_sha256 != loaded.source_sha256
                or current.scenario != loaded.scenario
                or current.scenario_root != loaded.scenario_root):
            raise ScenarioError("selected scenario changed")
    except ScenarioError:
        invalid = tuple(CheckResult(check.id, check.kind, "INFRA_ERROR",
                                    "SCENARIO_SOURCE_CHANGED", None, check.sha256,
                                    empty, empty, 0.0) for check in loaded.scenario.checks)
        return aggregate_checks(invalid, loaded.scenario.digest)
    for check in loaded.scenario.checks:
        try:
            source = _read_script(loaded.scenario_root, check.entrypoint)
            script_digest = hashlib.sha256(source).hexdigest()
            if script_digest != check.sha256:
                raise ValueError("independent check digest changed")
        except (OSError, ValueError):
            results.append(CheckResult(check.id, check.kind, "INFRA_ERROR", "CHECK_SOURCE_INVALID",
                                       None, check.sha256, empty, empty, 0.0))
            continue
        with tempfile.TemporaryDirectory(prefix="aek-independent-check-") as temp:
            snapshot = Path(temp) / "check.py"
            snapshot.write_bytes(source)
            substitutions = {"{python}": sys.executable, "{check}": str(snapshot),
                             "{workspace}": str(workspace),
                             "{scenario_root}": str(loaded.scenario_root)}
            args = tuple(substitutions.get(arg, arg) for arg in check.argv)
            # Isolate Python's import startup from candidate-controlled paths.
            args = (args[0], "-I", *args[1:])
            outcome = run_process(args, workspace, timeout_seconds=check.timeout_seconds)
        if outcome.reason_code != "PROCESS_COMPLETED":
            status, reason = "INFRA_ERROR", outcome.reason_code
        elif outcome.exit_code == 0:
            status, reason = "PASS", "CHECK_PASSED"
        elif outcome.exit_code == 1:
            status, reason = "FAIL", "CHECK_ASSERTION_FAILED"
        else:
            status, reason = "INFRA_ERROR", "CHECK_EXECUTION_ERROR"
        results.append(CheckResult(check.id, check.kind, status, reason, outcome.exit_code,
                                   script_digest, outcome.stdout_sha256, outcome.stderr_sha256,
                                   outcome.duration_seconds))
        if reason == "PROCESS_CLEANUP_INCOMPLETE":
            break
    return aggregate_checks(tuple(results), loaded.scenario.digest)
