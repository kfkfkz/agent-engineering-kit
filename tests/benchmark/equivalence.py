"""Independent equivalence evidence for complete context comparisons."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from context import BenchmarkCase, EquivalenceReceipt


def _load_source(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def _atomic_contract(repo: Path, commit: str, module_name: str) -> bool:
    source = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:installer/atomic.py"],
        capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix="aek-equivalence-") as temp:
        module_path = Path(temp) / "atomic.py"
        module_path.write_bytes(source)
        module = _load_source(module_name, module_path)
        work = Path(temp) / "work"
        work.mkdir()
        original = work / "original"
        occupied = work / "occupied"
        original.write_text("original", encoding="utf-8")
        occupied.write_text("user-owned", encoding="utf-8")
        directory = os.open(work, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                module.rename_noreplace(
                    directory, original.name, directory, occupied.name)
            except FileExistsError:
                pass
            else:
                return False
        finally:
            os.close(directory)
        return (original.read_text(encoding="utf-8") == "original"
                and occupied.read_text(encoding="utf-8") == "user-owned")


def _current_atomic_contract(repo: Path) -> bool:
    module = _load_source(
        "aek_current_atomic_equivalence", repo / "installer/atomic.py")
    with tempfile.TemporaryDirectory(prefix="aek-equivalence-current-") as temp:
        work = Path(temp)
        original = work / "original"
        occupied = work / "occupied"
        original.write_text("original", encoding="utf-8")
        occupied.write_text("user-owned", encoding="utf-8")
        directory = os.open(work, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                module.rename_noreplace(
                    directory, original.name, directory, occupied.name)
            except FileExistsError:
                pass
            else:
                return False
        finally:
            os.close(directory)
        return (original.read_text(encoding="utf-8") == "original"
                and occupied.read_text(encoding="utf-8") == "user-owned")


def _cli_parity(repo: Path, commit: str) -> tuple[bool, bool, dict[str, object]]:
    with tempfile.TemporaryDirectory(prefix="aek-equivalence-cli-") as temp:
        old = Path(temp) / "route-eval"
        old.write_bytes(subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:route-eval"],
            capture_output=True, check=True).stdout)

        def run(path: Path) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [sys.executable, str(path), "--init"], capture_output=True,
                check=False, cwd=temp)

        before, after = run(old), run(repo / "route-eval")
    exits = before.returncode == after.returncode
    diagnostics = before.stdout == after.stdout and before.stderr == after.stderr
    evidence = {
        "old_rc": before.returncode, "new_rc": after.returncode,
        "old_stdout": hashlib.sha256(before.stdout).hexdigest(),
        "new_stdout": hashlib.sha256(after.stdout).hexdigest(),
        "old_stderr": hashlib.sha256(before.stderr).hexdigest(),
        "new_stderr": hashlib.sha256(after.stderr).hexdigest(),
    }
    return exits, diagnostics, evidence


def complete_equivalence(repo: Path, case: BenchmarkCase, baseline, current,
                         expected_digest: str) -> EquivalenceReceipt:
    task = (_atomic_contract(repo, case.baseline_commit,
                             f"aek_old_atomic_{case.case_id.replace('-', '_')}")
            and _current_atomic_contract(repo)
            and baseline.fixture_digest == current.fixture_digest)
    coverage = (baseline.report.coverage == current.report.coverage == "complete"
                and not baseline.report.unobserved_channels
                and not current.report.unobserved_channels)
    security = (task and not baseline.report.findings
                and not current.report.findings)
    exit_codes, diagnostics, cli_evidence = _cli_parity(
        repo, case.baseline_commit)
    payload = {
        "schema_version": 1, "case_id": case.case_id,
        "fixture_digest": current.fixture_digest,
        "baseline_stream_digest": baseline.report.stream_digest,
        "current_stream_digest": current.report.stream_digest,
        "task": task, "security": security, "coverage": coverage,
        "exit_codes": exit_codes, "diagnostics": diagnostics,
        "cli_evidence": cli_evidence,
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    if digest != expected_digest:
        return EquivalenceReceipt(
            False, False, False, False, False, digest)
    return EquivalenceReceipt(
        task, security, coverage, exit_codes, diagnostics, digest)
