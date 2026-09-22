#!/usr/bin/env python3
"""Dynamic plan contracts, independent of doc-gate's legacy schema."""
from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import asdict, replace

from aek.core.artifact.plan import (
    bind_stage, build_plan, evaluate_binding, freeze_plan,
    load_binding_bytes, load_credential_bytes, load_plan_bytes,
    resolved_stage_prerequisites,
)
from aek.core.planning.facts import FACT_CATALOG, FactObservation, merge_fact_observations
from aek.core.planning.policy import preview_plan


SUBJECT = "a" * 64


def known_false():
    result = {}
    for fact_id, spec in FACT_CATALOG.items():
        observations = tuple(FactObservation(
            fact_id, producer, "false", SUBJECT,
            hashlib.sha256((fact_id + producer).encode()).hexdigest(), True)
            for producer in spec.required_producers)
        result[fact_id] = merge_fact_observations(fact_id, SUBJECT, observations)
    return result


class PlanTests(unittest.TestCase):
    def test_deterministic_plan_and_skip_evidence(self) -> None:
        facts = known_false()
        preview = preview_plan("standard", SUBJECT, facts)
        plan = build_plan(preview, facts)
        self.assertEqual(plan, build_plan(preview, facts))
        api = plan.by_id("api-design")
        self.assertEqual(api.classification, "skipped")
        self.assertEqual(api.reason_code, "FACT_FALSE")
        self.assertTrue(api.evidence_ref_ids)
        self.assertEqual(plan.by_id("detail-design").resolved_prerequisites,
                         ("需求分析", "概要设计"))
        self.assertEqual(resolved_stage_prerequisites(plan, "详细设计"),
                         ("概要设计",))
        self.assertEqual(len(plan.items), 9)
        self.assertEqual(len(plan.digest), 64)
        overlay = preview_plan("standard", SUBJECT, facts,
                               required_overlays=("ui-design",))
        self.assertEqual(build_plan(overlay, facts,
                                    required_overlays=("ui-design",)).by_id(
                                        "ui-design").classification, "required")
        with self.assertRaises(ValueError):
            build_plan(overlay, facts)

    def test_unknown_never_skips_and_under_route_is_rejected(self) -> None:
        facts = known_false()
        facts["public_api_change"] = merge_fact_observations(
            "public_api_change", SUBJECT, ())
        preview = preview_plan("bounded", SUBJECT, facts)
        self.assertEqual(preview.minimum_route, "standard")
        with self.assertRaises(ValueError):
            build_plan(preview, facts)
        upgraded = preview_plan("standard", SUBJECT, facts)
        self.assertEqual(build_plan(upgraded, facts).by_id("api-design").classification,
                         "required")

    def test_credential_and_stage_binding_staleness(self) -> None:
        facts = known_false()
        plan = build_plan(preview_plan("standard", SUBJECT, facts), facts)
        credential = freeze_plan(plan, input_digest="b" * 64,
                                 generation_mode="native", frozen_at="2026-09-20T00:00:00Z")
        self.assertEqual(credential.plan_digest, plan.digest)
        docs = {"详细设计.md": "c" * 64}
        parents = {"概要设计": "f" * 64}
        binding = bind_stage(
            plan, credential, "详细设计", gate_digest="1" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents)
        with self.assertRaises(ValueError):
            bind_stage(plan, replace(credential, credential_digest="0" * 64),
                       "详细设计", gate_digest="1" * 64, doc_hashes=docs,
                       prerequisite_fingerprints=parents)
        timestamp_changed = replace(credential, frozen_at="2026-09-21T00:00:00Z")
        self.assertEqual(timestamp_changed.credential_digest,
                         credential.credential_digest)
        bind_stage(plan, timestamp_changed, "详细设计", gate_digest="1" * 64,
                   doc_hashes=docs, prerequisite_fingerprints=parents)
        self.assertEqual(evaluate_binding(
            binding, plan, credential, gate_digest="1" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents), "BOUND")
        self.assertEqual(evaluate_binding(
            binding, plan, credential, gate_digest="1" * 64,
            doc_hashes=docs,
            prerequisite_fingerprints={"概要设计": "2" * 64}), "STALE")
        self.assertEqual(evaluate_binding(
            binding, plan, credential, gate_digest="1" * 64,
            doc_hashes={"详细设计.md": "2" * 64},
            prerequisite_fingerprints=parents), "STALE")
        self.assertEqual(evaluate_binding(
            binding, plan, timestamp_changed,
            gate_digest="1" * 64, doc_hashes=docs,
            prerequisite_fingerprints=parents), "BOUND")

        forged = replace(binding, skipped_ids=())
        forged_payload = {name: getattr(forged, name) for name in forged.__dataclass_fields__
                          if name != "binding_digest"}
        forged = replace(forged, binding_digest=hashlib.sha256(json.dumps(
            forged_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(evaluate_binding(
            forged, plan, credential, gate_digest="1" * 64,
            doc_hashes=docs, prerequisite_fingerprints=parents), "INVALID")

    def test_persisted_plan_credentials_are_strictly_decoded(self) -> None:
        facts = known_false()
        plan = build_plan(preview_plan("standard", SUBJECT, facts), facts)
        credential = freeze_plan(plan, input_digest="b" * 64,
                                 generation_mode="native", frozen_at="2026-09-20T00:00:00Z")
        binding = bind_stage(
            plan, credential, "详细设计", gate_digest="1" * 64,
            doc_hashes={"详细设计.md": "c" * 64},
            prerequisite_fingerprints={"概要设计": "f" * 64})

        with self.assertRaises(ValueError):
            bind_stage(plan, credential, "业务流程设计", gate_digest="1" * 64,
                       doc_hashes={}, prerequisite_fingerprints={"概要设计": "f" * 64})

        def encoded(value: object) -> bytes:
            return json.dumps(asdict(value), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode()

        self.assertEqual(load_plan_bytes(encoded(plan)), plan)
        self.assertEqual(load_credential_bytes(encoded(credential)), credential)
        self.assertEqual(load_binding_bytes(encoded(binding)), binding)
        damaged = asdict(plan)
        damaged["unrecognized_critical_field"] = "ignored?"
        with self.assertRaises(ValueError):
            load_plan_bytes(json.dumps(damaged).encode())
        damaged = asdict(plan)
        damaged["items"][0]["classification"] = "skipped"
        with self.assertRaises(ValueError):
            load_plan_bytes(json.dumps(damaged).encode())
        changed_time = replace(credential, frozen_at="2026-09-21T00:00:00Z")
        self.assertEqual(load_credential_bytes(encoded(changed_time)), changed_time)
        damaged = asdict(plan)
        damaged["items"][0]["classification"] = []
        with self.assertRaises(ValueError):
            load_plan_bytes(json.dumps(damaged, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode())
        damaged = asdict(binding)
        damaged["stage"] = []
        with self.assertRaises(ValueError):
            load_binding_bytes(json.dumps(damaged, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode())


if __name__ == "__main__":
    unittest.main()
