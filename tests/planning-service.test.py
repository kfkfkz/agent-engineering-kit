#!/usr/bin/env python3
"""Read dynamic plan state only after the transaction has recovered."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from aek.adapters.change_scope import scan_negative_risks
from aek.adapters.plan_transaction import PlanStore
from aek.application.document_gate import (
    effective_stage_states, participating_documents,
    participating_prerequisites,
)
from aek.application.planning import (
    bind_current_stage, bind_frozen_stage, build_git_current_plan,
    evaluate_stage_snapshot,
    evaluate_stage_states,
    freeze_git_current_plan, git_plan_input_digest, prepare_git_current_plan,
    read_dynamic_state, read_frozen_dynamic_state,
    read_git_current_dynamic_state, rollback_dynamic_state,
)
from aek.core.artifact.registry import ARTIFACT_REGISTRY
from aek.core.artifact.plan import bind_stage, build_plan, freeze_plan
from aek.core.planning.facts import (
    FACT_CATALOG, FactObservation, merge_fact_observations,
)
from aek.core.planning.policy import preview_plan


SUBJECT = "a" * 64
INPUT = "b" * 64


def canonical(value: object) -> bytes:
    return json.dumps(asdict(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


class PlanningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reviews = Path(self.tmp.name) / "reviews"
        self.reviews.mkdir()
        self.store = PlanStore(self.reviews)
        self.facts = {key: merge_fact_observations(key, SUBJECT, ())
                      for key in FACT_CATALOG}
        self.plan = build_plan(preview_plan("standard", SUBJECT, self.facts), self.facts)
        self.credential = freeze_plan(
            self.plan, input_digest=INPUT, generation_mode="native",
            frozen_at="2026-09-20T00:00:00Z")

    def test_absent_prepared_frozen_and_input_stale(self) -> None:
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT,
                                            self.facts).state, "ABSENT")
        self.store.publish({"artifact-plan.json": canonical(self.plan)},
                           expected_consumers=self.store.consumer_digest())
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT,
                                            self.facts).state, "PREPARED")
        self.store.publish({"artifact-plan.gate.json": canonical(self.credential)},
                           expected_consumers=self.store.consumer_digest())
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT,
                                            self.facts).state, "FROZEN")
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT).state, "INVALID")
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, "c" * 64,
                                            self.facts).state, "INVALID")
        different = dict(self.facts)
        different["public_api_change"] = merge_fact_observations(
            "public_api_change", SUBJECT,
            tuple(FactObservation(
                "public_api_change", producer, "false", SUBJECT,
                "c" * 64, True)
                for producer in FACT_CATALOG["public_api_change"].required_producers))
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT,
                                            different).state, "INVALID")

    def test_orphan_credential_and_corrupt_plan_are_invalid(self) -> None:
        self.store.publish({"artifact-plan.gate.json": canonical(self.credential)},
                           expected_consumers=self.store.consumer_digest())
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT,
                                            self.facts).state, "INVALID")
        self.store.publish({"artifact-plan.json": b'{"schema_version":1}'},
                           expected_consumers=self.store.consumer_digest())
        self.assertEqual(read_dynamic_state(self.store, SUBJECT, INPUT,
                                            self.facts).state, "INVALID")

    def test_git_bound_plan_rechecks_current_facts_before_freeze_status(self) -> None:
        repo = Path(self.tmp.name) / "repository"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                       check=True)
        (repo / "docs" / "03-SDD").mkdir(parents=True)
        guide = repo / "docs" / "03-SDD" / "guide.md"
        guide.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        guide.write_text("after\n", encoding="utf-8")
        plan = build_git_current_plan(
            repo, base, "standard", scan_scope=scan_negative_risks)
        self.assertEqual(len(git_plan_input_digest(plan, base)), 64)
        self.assertNotEqual(git_plan_input_digest(plan, base),
                            git_plan_input_digest(plan, "f" * 40))
        credential = freeze_plan(plan, input_digest=INPUT, generation_mode="native",
                                 frozen_at="2026-09-20T00:00:00Z")
        self.store.publish({"artifact-plan.json": canonical(plan),
                            "artifact-plan.gate.json": canonical(credential)},
                           expected_consumers=self.store.consumer_digest())
        self.assertEqual(read_git_current_dynamic_state(
            self.store, repo, base, INPUT,
            scan_scope=scan_negative_risks).state, "FROZEN")
        forged_facts = {fact_id: merge_fact_observations(
            fact_id, plan.subject_digest, tuple(FactObservation(
                fact_id, producer, "false", plan.subject_digest, "c" * 64, True)
                for producer in spec.required_producers))
            for fact_id, spec in FACT_CATALOG.items()}
        forged = build_plan(preview_plan("standard", plan.subject_digest,
                                         forged_facts), forged_facts)
        forged_credential = freeze_plan(
            forged, input_digest=INPUT, generation_mode="native",
            frozen_at="2026-09-20T00:00:00Z")
        forged_reviews = Path(self.tmp.name) / "forged-reviews"
        forged_reviews.mkdir()
        forged_store = PlanStore(forged_reviews)
        forged_store.publish({"artifact-plan.json": canonical(forged),
                              "artifact-plan.gate.json": canonical(forged_credential)},
                             expected_consumers=forged_store.consumer_digest())
        self.assertEqual(read_git_current_dynamic_state(
            forged_store, repo, base, INPUT,
            scan_scope=scan_negative_risks).state, "INVALID")
        guide.write_text("after changed\n", encoding="utf-8")
        self.assertEqual(read_git_current_dynamic_state(
            self.store, repo, base, INPUT,
            scan_scope=scan_negative_risks).state, "INVALID")
        self.assertEqual(read_frozen_dynamic_state(
            self.store, INPUT).state, "FROZEN")

        direct = build_git_current_plan(
            repo, base, "direct", scan_scope=scan_negative_risks)
        self.assertEqual(direct.route, "direct")
        self.assertTrue(all(item.classification == "skipped"
                            for item in direct.items))
        self.assertEqual(participating_documents(direct, "详细设计"), ())
        self.assertTrue(all(value == "SKIPPED" for value in
                            effective_stage_states(
                                direct, {stage: "UNBOUND" for stage in
                                         ARTIFACT_REGISTRY.groups}).values()))
        self.assertEqual(participating_prerequisites(direct, "计划"), ())

    def test_stage_binding_requires_current_gate_docs_and_parent(self) -> None:
        docs = {name: "c" * 64 for name in (
            "详细设计.md", "API设计.md", "数据库设计.md")}
        parents = {"业务流程设计": "d" * 64}
        self.store.publish({"artifact-plan.json": canonical(self.plan),
                            "artifact-plan.gate.json": canonical(self.credential)},
                           expected_consumers=self.store.consumer_digest())
        unbound = read_dynamic_state(self.store, SUBJECT, INPUT, self.facts)
        self.assertEqual(evaluate_stage_snapshot(
            unbound, "详细设计", gate_digest="e" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents), "UNBOUND")
        binding = bind_stage(
            self.plan, self.credential, "详细设计", gate_digest="e" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents)
        self.store.publish({"详细设计.plan-binding.json": canonical(binding)},
                           expected_consumers=self.store.consumer_digest())
        frozen = read_dynamic_state(self.store, SUBJECT, INPUT, self.facts)
        self.assertEqual(evaluate_stage_snapshot(
            frozen, "详细设计", gate_digest="e" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents), "BOUND")
        changed_docs = dict(docs, **{"API设计.md": "f" * 64})
        self.assertEqual(evaluate_stage_snapshot(
            frozen, "详细设计", gate_digest="e" * 64,
            doc_hashes=changed_docs, prerequisite_fingerprints=parents), "STALE")
        self.assertEqual(evaluate_stage_snapshot(
            frozen, "详细设计", gate_digest="e" * 64,
            doc_hashes=docs,
            prerequisite_fingerprints={"业务流程设计": "f" * 64}), "STALE")

    def test_stage_state_map_propagates_upstream_staleness_transitively(self) -> None:
        bindings = []
        snapshots = {}
        for offset, (stage, group) in enumerate(
                ARTIFACT_REGISTRY.groups.items(), start=1):
            gate = f"{offset:x}" * 64
            docs = {
                ARTIFACT_REGISTRY.get_artifact(artifact_id).filename:
                    f"{offset + 6:x}" * 64
                for artifact_id in group.artifact_ids
                if self.plan.by_id(artifact_id).classification != "skipped"
            }
            parents = {parent: f"{offset + 9:x}" * 64
                       for parent in group.prerequisites}
            bindings.append(bind_stage(
                self.plan, self.credential, stage, gate_digest=gate,
                doc_hashes=docs, prerequisite_fingerprints=parents))
            snapshots[stage] = {
                "gate_digest": gate,
                "doc_hashes": docs,
                "prerequisite_fingerprints": parents,
            }
        frozen = type(read_dynamic_state(
            self.store, SUBJECT, INPUT, self.facts))(
                "FROZEN", "test", self.plan, self.credential, tuple(bindings))
        self.assertTrue(all(value == "BOUND" for value in
                            evaluate_stage_states(frozen, snapshots).values()))

        changed = {stage: dict(snapshot) for stage, snapshot in snapshots.items()}
        changed["需求分析"] = dict(changed["需求分析"], gate_digest="f" * 64)
        states = evaluate_stage_states(frozen, changed)
        self.assertEqual(states["需求分析"], "STALE")
        self.assertEqual(states["概要设计"], "STALE")
        self.assertEqual(states["业务流程设计"], "STALE")
        self.assertEqual(states["详细设计"], "STALE")
        self.assertEqual(states["计划"], "STALE")
        self.assertEqual(states["UI设计"], "STALE")

        del changed["业务流程设计"]
        states = evaluate_stage_states(frozen, changed)
        self.assertEqual(states["业务流程设计"], "UNBOUND")
        self.assertEqual(states["详细设计"], "STALE")
        self.assertEqual(states["计划"], "STALE")

    def test_git_plan_mutation_workflow_is_recoverable_and_current(self) -> None:
        repo = Path(self.tmp.name) / "workflow-repository"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                       check=True)
        (repo / "docs" / "03-SDD").mkdir(parents=True)
        guide = repo / "docs" / "03-SDD" / "guide.md"
        guide.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        guide.write_text("after\n", encoding="utf-8")

        prepared = prepare_git_current_plan(
            self.store, repo, base, "standard", scan_scope=scan_negative_risks)
        self.assertEqual(prepared.state, "PREPARED")
        first_prepared = self.store.snapshot()
        self.assertEqual(prepare_git_current_plan(
            self.store, repo, base, "standard",
            scan_scope=scan_negative_risks).state, "PREPARED")
        self.assertEqual(self.store.snapshot(), first_prepared)
        frozen = freeze_git_current_plan(
            self.store, repo, base, INPUT, scan_scope=scan_negative_risks,
            frozen_at="2026-09-20T00:00:00Z")
        self.assertEqual(frozen.state, "FROZEN")
        first_frozen = self.store.snapshot()
        self.assertEqual(freeze_git_current_plan(
            self.store, repo, base, INPUT, scan_scope=scan_negative_risks,
            frozen_at="2026-09-21T00:00:00Z").state, "FROZEN")
        self.assertEqual(self.store.snapshot(), first_frozen)
        self.assertEqual(prepare_git_current_plan(
            self.store, repo, base, "standard",
            scan_scope=scan_negative_risks).state, "PREPARED")
        self.assertEqual(self.store.snapshot(), first_frozen)
        docs = {"详细设计.md": "c" * 64}
        parents = {"概要设计": "d" * 64}
        bound = bind_current_stage(
            self.store, repo, base, INPUT, "详细设计",
            gate_digest="e" * 64, doc_hashes=docs,
            prerequisite_fingerprints=parents,
            scan_scope=scan_negative_risks)
        self.assertEqual(bound.state, "FROZEN", bound.reason)
        self.assertEqual(evaluate_stage_snapshot(
            bound, "详细设计", gate_digest="e" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents), "BOUND")
        rebound = bind_frozen_stage(
            self.store, INPUT, "详细设计", gate_digest="e" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents)
        self.assertEqual(rebound.bindings, bound.bindings)
        rollback_dynamic_state(self.store)
        self.assertEqual(self.store.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
