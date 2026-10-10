"""Offline synthetic-agent attempts, guarded by the existing WorkUnit CAS store.

This is not a live adapter or an untrusted-code sandbox. The caller selects a
pinned, trusted Python fixture; execution completion is never Outcome PASS.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

from aek.adapters.atomic_file import AtomicFileStore
from aek.adapters.work_unit_store import WorkUnitConflict, WorkUnitStore
from aek.application.work_unit import WorkUnitService
from aek.core.context.identity import build_identity
from aek.core.work_unit import StepSpec, WorkState

from .check_runner import _read_script
from .process import run_process
from .workspace import PreparedPair, _git


def _human(request_digest: str, reason: str) -> dict:
    return {"status": "NEEDS_HUMAN", "reason_code": reason, "request_digest": request_digest,
            "reused": False, "outcome": "NOT_EVALUATED", "execution_scope": "development_fake",
            "usage": {"tokens": None, "coverage": "UNKNOWN"},
            "next_step": "Inspect the recorded attempt; do not relaunch this run."}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_RESULT_KEY")
        value[key] = item
    return value


def _cached_result(raw_receipt: bytes, raw_result: bytes, digest: str) -> dict | None:
    receipt = json.loads(raw_receipt, object_pairs_hook=_unique_object)
    result = json.loads(raw_result, object_pairs_hook=_unique_object)
    if (not isinstance(receipt, dict) or set(receipt) != {"state", "request_digest", "result_sha256"}
            or receipt.get("state") != "COMMITTED" or receipt.get("request_digest") != digest
            or receipt.get("result_sha256") != hashlib.sha256(raw_result).hexdigest()
            or not isinstance(result, dict) or set(result) != {
                "status", "reason_code", "request_digest", "exit_code", "stdout_sha256",
                "stderr_sha256", "duration_seconds", "reused", "outcome", "execution_scope", "usage"}
            or result.get("request_digest") != digest
            or result.get("execution_scope") != "development_fake"
            or result.get("outcome") != "NOT_EVALUATED" or result.get("reused") is not False
            or result.get("usage") != {"tokens": None, "coverage": "UNKNOWN"}
            or result.get("status") not in {"COMPLETED", "FAILED", "TIMED_OUT"}
            or result.get("reason_code") not in {"PROCESS_COMPLETED", "PROCESS_START_FAILED",
                                                 "PROCESS_TIMEOUT", "OUTPUT_LIMIT_EXCEEDED",
                                                 "PROCESS_DESCENDANT_OPEN_PIPE"}):
        return None
    if any(not isinstance(result[key], str) or not re.fullmatch(r"[a-f0-9]{64}", result[key])
           for key in ("stdout_sha256", "stderr_sha256")):
        return None
    if result["exit_code"] is not None and type(result["exit_code"]) is not int:
        return None
    duration = result["duration_seconds"]
    if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
        return None
    expected = ("TIMED_OUT" if result["reason_code"] == "PROCESS_TIMEOUT" else
                "COMPLETED" if result["reason_code"] == "PROCESS_COMPLETED" and result["exit_code"] == 0
                else "FAILED")
    return result if result["status"] == expected else None


def run_fake(prepared: PreparedPair, variant: str, script: Path, *,
             script_sha256: str, timeout_seconds: int) -> dict:
    workspace = prepared.workspace(variant)
    if type(timeout_seconds) is not int or not 0 < timeout_seconds <= 3600:
        raise ValueError("INVALID_FAKE_TIMEOUT")
    if timeout_seconds > prepared.manifest["execution_budget"]["timeout_seconds"]:
        raise ValueError("FAKE_TIMEOUT_EXCEEDS_SCENARIO")
    files = AtomicFileStore(prepared.root)
    if files.read("manifest.json") != prepared.manifest_bytes:
        raise ValueError("PREPARED_MANIFEST_CHANGED")
    script = Path(script).absolute()
    script = script.parent.resolve() / script.name
    if any(script.is_relative_to(item.path) for item in prepared.workspaces):
        raise ValueError("FAKE_SOURCE_INSIDE_CANDIDATE")
    source = _read_script(script.parent.resolve(), script.name)
    if hashlib.sha256(source).hexdigest() != script_sha256:
        raise ValueError("FAKE_SCRIPT_DIGEST_MISMATCH")
    request = {"schema_version": 1, "variant": variant, "script_sha256": script_sha256,
               "timeout_seconds": timeout_seconds, "manifest_sha256": prepared.manifest_sha256,
               "interpreter": sys.executable, "environment_profile": "private-home-v1"}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    identity = build_identity({
        "subject": {"scenario": prepared.manifest["scenario_digest"]},
        "artifact": {"manifest": prepared.manifest_sha256},
        "policy": {"mode": hashlib.sha256(b"development-fake-only-v1").hexdigest()},
        "context": {"request": digest},
        "evidence": {"capability": hashlib.sha256(b"isolation-not-evaluated").hexdigest()},
    })
    state_root = prepared.root / "work-units"
    state_root.mkdir(mode=0o700, exist_ok=True)
    service = WorkUnitService(WorkUnitStore(state_root))
    try:
        opened = service.open_or_create(
            repo_identity=prepared.manifest_sha256, task_identity=variant,
            writer_id="benchmark-runner", identity=identity,
            steps=(StepSpec("execute", False, ("subject.scenario", "artifact.manifest",
                                               "policy.mode", "context.request", "evidence.capability")),))
    except WorkUnitConflict:
        return _human(digest, "RUN_ALREADY_CLAIMED")
    except ValueError:
        return _human(digest, "WORK_UNIT_STATE_INVALID")
    current = opened.snapshot
    receipt_name = f"{variant}.execution-receipt.json"
    result_name = f"{variant}.execution-result.json"
    cached = None
    evidence_present = False
    try:
        raw_receipt, raw_result = files.read(receipt_name), files.read(result_name)
        evidence_present = raw_receipt is not None or raw_result is not None
        if raw_receipt is not None and raw_result is not None:
            cached = _cached_result(raw_receipt, raw_result, digest)
    except (OSError, ValueError, RecursionError, TypeError, OverflowError):
        evidence_present = True
    decision = service.resume(current.work_unit_id, identity,
                              lambda ref: "COMMITTED" if cached is not None else None).by_step()["execute"]
    if decision.action == "reuse" and cached is not None:
        if current.by_step()["execute"].state == WorkState.IN_PROGRESS:
            try:
                service.transition(
                    current.work_unit_id, expected_snapshot_digest=current.snapshot_digest,
                    step_id="execute", state=WorkState.COMPLETED, writer_id=current.writer_id,
                    writer_epoch=current.writer_epoch, event_id="execute-recovered", receipt_ref=receipt_name)
            except (ValueError, WorkUnitConflict):
                # Another controller may have repaired the same state; never rerun the process.
                pass
        return {**cached, "reused": True}
    if current.by_step()["execute"].state != WorkState.NOT_STARTED:
        return _human(digest, "EXISTING_ATTEMPT_REQUIRES_INSPECTION")
    if evidence_present:
        return _human(digest, "UNATTACHED_EXECUTION_EVIDENCE")
    try:
        current = service.transition(
            current.work_unit_id, expected_snapshot_digest=current.snapshot_digest,
            step_id="execute", state=WorkState.IN_PROGRESS, writer_id=current.writer_id,
            writer_epoch=current.writer_epoch, event_id="execute-start", receipt_ref=receipt_name)
    except (ValueError, WorkUnitConflict):
        return _human(digest, "RUN_ALREADY_CLAIMED")
    # Persist STARTED before launch. Errors preserve evidence and propagate, never trigger a retry.
    files.write(receipt_name, json.dumps({"state": "STARTED", "request_digest": digest},
                                         sort_keys=True).encode("utf-8"))
    if (_git(workspace.path, "rev-parse", "HEAD").decode("ascii").strip() != workspace.baseline_commit
            or _git(workspace.path, "status", "--porcelain")):
        raise ValueError("WORKSPACE_BASELINE_CHANGED")
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL"}}
    env.update(HOME=str(workspace.home), USERPROFILE=str(workspace.home),
               XDG_CACHE_HOME=str(workspace.cache), CODEX_HOME=str(workspace.session),
               CLAUDE_CONFIG_DIR=str(workspace.session))
    with tempfile.TemporaryDirectory(prefix="aek-fake-agent-") as snapshot_dir:
        snapshot = Path(snapshot_dir) / "agent.py"
        snapshot.write_bytes(source)
        process = run_process((sys.executable, "-I", str(snapshot), str(workspace.path)),
                              workspace.path, timeout_seconds=timeout_seconds, environment=env)
    if process.reason_code == "PROCESS_CLEANUP_INCOMPLETE":
        raise ValueError("PROCESS_CLEANUP_INCOMPLETE")
    status = ("TIMED_OUT" if process.reason_code == "PROCESS_TIMEOUT" else
              "COMPLETED" if process.reason_code == "PROCESS_COMPLETED" and process.exit_code == 0
              else "FAILED")
    result = {"status": status, "reason_code": process.reason_code, "request_digest": digest,
              "exit_code": process.exit_code, "stdout_sha256": process.stdout_sha256,
              "stderr_sha256": process.stderr_sha256, "duration_seconds": process.duration_seconds,
              "reused": False, "outcome": "NOT_EVALUATED", "execution_scope": "development_fake",
              "usage": {"tokens": None, "coverage": "UNKNOWN"}}
    raw = json.dumps(result, sort_keys=True).encode("utf-8")
    files.write(result_name, raw)
    files.write(receipt_name, json.dumps({"state": "COMMITTED", "request_digest": digest,
                                          "result_sha256": hashlib.sha256(raw).hexdigest()},
                                         sort_keys=True).encode("utf-8"))
    service.transition(
        current.work_unit_id, expected_snapshot_digest=current.snapshot_digest,
        step_id="execute", state=WorkState.COMPLETED, writer_id=current.writer_id,
        writer_epoch=current.writer_epoch, event_id="execute-sealed", receipt_ref=receipt_name)
    return result
