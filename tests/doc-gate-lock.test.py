#!/usr/bin/env python3
"""Legacy document writes share the ArtifactPlan consumer lock."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aek.adapters.plan_transaction import PlanStore


KIT = Path(__file__).resolve().parent.parent


class DocumentGateLockTests(unittest.TestCase):
    def test_status_and_check_wait_for_consistent_plan_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp) / "docs" / "03-SDD" / "001-read"
            reviews = req / "reviews"
            reviews.mkdir(parents=True)
            store = PlanStore(reviews)
            for args, expected_rc in (
                (("status", str(req)), 0),
                (("check", str(req), "--stage", "需求分析"), 1),
            ):
                with self.subTest(command=args[0]):
                    with store.legacy_gate_lock():
                        proc = subprocess.Popen(
                            [sys.executable, str(KIT / "doc-gate"), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        with self.assertRaises(subprocess.TimeoutExpired):
                            proc.wait(timeout=0.4)
                    try:
                        out, err = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                        self.fail("document gate reader stayed blocked after release")
                    self.assertEqual(proc.returncode, expected_rc, (out, err))

    def test_legacy_writers_wait_for_plan_consumer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp) / "docs" / "03-SDD" / "001-lock"
            reviews = req / "reviews"
            reviews.mkdir(parents=True)
            store = PlanStore(reviews)
            for args, expected_rc in (
                (("freeze", str(req), "--stage", "需求分析", "--by", "tester"), 3),
                (("gate", str(req), "--stage", "概要设计"), 1),
                (("closeout", str(req)), 3),
            ):
                with self.subTest(command=args[0]):
                    with store.legacy_gate_lock():
                        proc = subprocess.Popen(
                            [sys.executable, str(KIT / "doc-gate"), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        try:
                            with self.assertRaises(subprocess.TimeoutExpired):
                                proc.wait(timeout=0.4)
                        finally:
                            if proc.poll() is not None:
                                proc.communicate()
                    try:
                        out, err = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                        self.fail("document gate stayed blocked after lock release")
                    self.assertEqual(proc.returncode, expected_rc, (out, err))

    def test_legacy_writer_rechecks_sidecar_after_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp) / "docs" / "03-SDD" / "001-race"
            reviews = req / "reviews"
            reviews.mkdir(parents=True)
            with PlanStore(reviews).legacy_gate_lock():
                proc = subprocess.Popen(
                    [sys.executable, str(KIT / "doc-gate"), "gate", str(req),
                     "--stage", "概要设计"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.wait(timeout=0.4)
                (reviews / "artifact-plan.json").write_text("{}")
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("document gate stayed blocked after lock release")
            self.assertEqual(proc.returncode, 3, (out, err))
            self.assertIn("sidecar", out.decode("utf-8"))
            self.assertFalse((reviews / "概要设计.gate.json").exists())


if __name__ == "__main__":
    unittest.main()
