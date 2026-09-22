#!/usr/bin/env python3
"""Pinned old/new CLI outputs and seven-run same-host latency smoke."""
from __future__ import annotations

import subprocess
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aek.adapters.telemetry import ContextLedger

from performance import compare_latency, measure_seven


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "88944283eb8030b4c44363f21f5ef0110252e1fa"


class PerformanceTests(unittest.TestCase):
    def test_threshold_semantics(self) -> None:
        self.assertEqual(compare_latency((1,) * 7, (11,) * 7).verdict, "FINDING")
        self.assertEqual(compare_latency((2_000_000,) * 7,
                                         (20_000_000,) * 7).verdict, "BLOCK")
        self.assertEqual(compare_latency((2_000_000,) * 7,
                                         (3_000_000,) * 7).verdict, "PASS")
        with self.assertRaises(ValueError):
            compare_latency((1,) * 6, (2,) * 7)

    def test_old_new_cli_parity_and_latency(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aek-perf-") as temp:
            temp_path = Path(temp)
            for entry, args, stdin in (
                ("route-eval", ("--init",), None),
                ("governance-eval", (".", "--json"), b""),
                # Top-level help intentionally lists the new 1.0.1 dynamic-plan
                # commands.  Pin an existing public operation instead: its
                # JSON, exit code and stderr remain the compatibility contract.
                ("doc-gate", ("status", ".", "--json"), None),
            ):
                with self.subTest(entry=entry):
                    old_bytes = subprocess.run(
                        ["git", "-C", str(ROOT), "show", f"{COMMIT}:{entry}"],
                        capture_output=True, check=True).stdout
                    old = temp_path / entry
                    old.write_bytes(old_bytes)

                    def invoke(path: Path) -> subprocess.CompletedProcess[bytes]:
                        return subprocess.run(
                            [sys.executable, str(path), *args], cwd=temp,
                            input=stdin, capture_output=True, check=False)

                    old_result = invoke(old)
                    new_result = invoke(ROOT / entry)
                    self.assertEqual(new_result.returncode, old_result.returncode)
                    self.assertEqual(new_result.stdout, old_result.stdout)
                    self.assertEqual(new_result.stderr, old_result.stderr)
                    baseline = measure_seven(lambda: invoke(old))
                    current = measure_seven(lambda: invoke(ROOT / entry))
                    result = compare_latency(baseline, current)
                    self.assertNotEqual(result.verdict, "BLOCK", result)

    def test_pure_route_core_parity_and_latency(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aek-core-perf-") as temp:
            old_bytes = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{COMMIT}:route-eval"],
                capture_output=True, check=True).stdout
            old_path = Path(temp) / "route-eval"
            old_path.write_bytes(old_bytes)
            old_module = runpy.run_path(str(old_path))
            new_module = runpy.run_path(str(ROOT / "route-eval"))
            governance = {"profile": "strict", "all_actions": ["receipt"],
                          "changed_files": [], "matched_rules": [],
                          "required_actions": []}
            old_evaluate, new_evaluate = old_module["evaluate"], new_module["evaluate"]
            for route in ("direct", "bounded", "standard", "initiative"):
                card = old_module["_template"]()
                card["route"] = route
                with self.subTest(route=route):
                    old = old_evaluate(card, governance)
                    new = new_evaluate(card, governance)
                    new.pop("context_budget")
                    self.assertEqual(new, old)
            card = old_module["_template"]()
            with patch("subprocess.run", side_effect=AssertionError("Core subprocess")), \
                 patch("pathlib.Path.open", side_effect=AssertionError("Core file scan")):
                baseline = measure_seven(lambda: old_evaluate(card, governance))
                current = measure_seven(lambda: new_evaluate(card, governance))
            self.assertNotEqual(compare_latency(baseline, current).verdict, "BLOCK")

    def test_report_rebuild_capacity_is_linear_with_stream_size(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aek-ledger-capacity-") as temp:
            ledger = ContextLedger(Path(temp) / "events.jsonl")
            medians = []
            for size in (256, 512, 1024):
                for _ in range(size - len(ledger.read())):
                    ledger.append_text(
                        "x", route="bounded", stage="execution",
                        source_kind="code", channel="code_loader",
                        purpose="target_evidence", cache_hit=False,
                        session_id="capacity-test", subject_digest="a" * 64,
                        logical_path="aek/core/context/telemetry.py",
                        occurred_at="2026-09-20T00:00:00Z")

                def rebuild() -> None:
                    self.assertEqual(len(ledger.read()), size)

                samples = measure_seven(rebuild)
                medians.append(sorted(samples)[3])
            for previous, current in zip(medians, medians[1:]):
                if current >= 20_000_000:
                    self.assertLess(current / previous, 4.0,
                                    "report rebuild grew superlinearly")


if __name__ == "__main__":
    unittest.main()
