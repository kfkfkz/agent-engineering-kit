#!/usr/bin/env python3
"""Legacy document writes share the ArtifactPlan consumer lock."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aek.adapters.plan_transaction import PlanInvalid, PlanStore
from aek.application.document_gate import run_stage_transaction
from aek.application.planning import DynamicPlanState


KIT = Path(__file__).resolve().parent.parent
# The 0.4 s assertion proves that the process is blocked while the lock is held.
# After release, allow for cold Python imports and loaded CI hosts; this timeout
# is only a deadlock guard, not a latency requirement.
RELEASE_TIMEOUT = 15


class DocumentGateLockTests(unittest.TestCase):
    def test_application_recovers_then_binds_in_same_live_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp)
            reviews = req / "reviews"
            reviews.mkdir()
            store = PlanStore(reviews)
            events = []
            state = DynamicPlanState("FROZEN", "")

            def recover(view, loaded):
                self.assertIs(loaded, state)
                view.recover_closeout(lambda _: {})
                events.append(("recover", view))

            def operate(view, loaded, snapshot):
                self.assertIs(loaded, state)
                self.assertEqual(snapshot, view.snapshot())
                events.append(("operate", view))
                return 0, True

            def bind(view, loaded, material_mode):
                self.assertIs(loaded, state)
                self.assertTrue(material_mode)
                self.assertTrue(view.source_identity)
                events.append(("bind", view))
                return 3

            code = run_stage_transaction(store.locked_view(req), lambda _: state,
                                         operate, bind, recover=recover)
            self.assertEqual(code, 3, "binding failure must propagate")
            self.assertEqual([name for name, _ in events], ["recover", "operate", "bind"])
            self.assertTrue(all(view is events[0][1] for _, view in events))
            with self.assertRaises(PlanInvalid):
                events[0][1].snapshot()

    def test_nonpassing_operation_does_not_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp)
            reviews = req / "reviews"
            reviews.mkdir()
            store = PlanStore(reviews)
            state = DynamicPlanState("FROZEN", "")
            self.assertEqual(run_stage_transaction(
                store.locked_view(req), lambda _: state, lambda *_: (2, True),
                lambda *_: self.fail("blocked stage must not bind")), 2)

    def test_status_and_check_wait_for_consistent_plan_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp) / "docs" / "03-SDD" / "001-read"
            reviews = req / "reviews"
            reviews.mkdir(parents=True)
            store = PlanStore(reviews)
            for args, expected_rc in (
                (("status", str(req)), 0),
                (("check", str(req), "--stage", "需求分析"), 1),
                (("review-input", str(req), "--stage", "需求分析",
                  "--precheck-summary"), 1),
            ):
                with self.subTest(command=args[0]):
                    with store.legacy_gate_lock():
                        proc = subprocess.Popen(
                            [sys.executable, str(KIT / "doc-gate"), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        with self.assertRaises(subprocess.TimeoutExpired):
                            proc.wait(timeout=0.4)
                    try:
                        out, err = proc.communicate(timeout=RELEASE_TIMEOUT)
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
                        out, err = proc.communicate(timeout=RELEASE_TIMEOUT)
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
                out, err = proc.communicate(timeout=RELEASE_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("document gate stayed blocked after lock release")
            self.assertEqual(proc.returncode, 3, (out, err))
            self.assertIn("sidecar", out.decode("utf-8"))
            self.assertFalse((reviews / "概要设计.gate.json").exists())


if __name__ == "__main__":
    unittest.main()
