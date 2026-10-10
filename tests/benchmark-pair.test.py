"""Paired runs require equal controls, distinct state and trusted external anchors."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from core.pair import (  # noqa: E402
    CONTROL_KEYS,
    RunControls,
    RunManifest,
    validate_pair,
)


class PairValidityTests(unittest.TestCase):
    def manifests(self):
        controls = RunControls("claude", "model-fixture", "anthropic_messages", "a" * 40,
                               tuple((key, "b" * 64) for key in CONTROL_KEYS), "c" * 64)
        left = RunManifest("EXP-1", "PAIR-1", "RUN-v", "TASK-1", 1, "vanilla", 0, controls,
                           ("1" * 64, "2" * 64, "3" * 64, "4" * 64), "d" * 64, "e" * 64)
        right = RunManifest("EXP-1", "PAIR-1", "RUN-a", "TASK-1", 1, "aek", 1, controls,
                            ("5" * 64, "6" * 64, "7" * 64, "8" * 64), "f" * 64, "e" * 64)
        return left, right

    def validate(self, left, right, **overrides):
        kwargs = {
            "trusted_manifest_digests": {left.run_id: left.digest, right.run_id: right.digest},
            "trusted_isolation_digests": frozenset({"e" * 64}),
            "expected_treatments": {"vanilla": "d" * 64, "aek": "f" * 64},
        }
        kwargs.update(overrides)
        return validate_pair(left, right, **kwargs)

    def test_equal_controls_with_distinct_storage_and_external_anchors_are_valid(self):
        left, right = self.manifests()
        result = self.validate(left, right)
        self.assertEqual(result.status, "VALID")
        self.assertEqual(result.reason_codes, ())
        self.assertEqual(result.scope, "manifest_identity_and_provenance")
        self.assertFalse(result.outcome_evaluated)

    def test_no_external_anchors_is_not_a_self_certified_valid_pair(self):
        left, right = self.manifests()
        result = validate_pair(left, right)
        self.assertEqual(result.status, "NOT_EVALUATED")
        self.assertIn("MANIFEST_ANCHOR_MISSING", result.reason_codes)
        self.assertIn("ISOLATION_UNPROVEN", result.reason_codes)

    def test_every_control_fingerprint_mismatch_invalidates_pair(self):
        left, right = self.manifests()
        for key in CONTROL_KEYS:
            with self.subTest(key=key):
                values = dict(right.controls.fingerprints)
                values[key] = "9" * 64
                changed = replace(right, controls=replace(right.controls, fingerprints=tuple(values.items())))
                self.assertEqual(self.validate(left, changed).status, "INVALID")
        for controls in (replace(right.controls, model="different-model"),
                         replace(right.controls, agent="codex", protocol="openai_responses"),
                         replace(right.controls, aek_source_commit="9" * 40)):
            self.assertEqual(self.validate(left, replace(right, controls=controls)).status, "INVALID")

    def test_missing_resolved_model_is_unknown_not_equal_null_proof(self):
        left, right = self.manifests()
        left = replace(left, controls=replace(left.controls, observed_model_sha256=None))
        right = replace(right, controls=replace(right.controls, observed_model_sha256=None))
        result = self.validate(left, right)
        self.assertEqual(result.status, "NOT_EVALUATED")
        self.assertIn("RESOLVED_MODEL_UNOBSERVED", result.reason_codes)

    def test_cross_variant_shared_cache_or_repeated_run_identity_is_invalid(self):
        left, right = self.manifests()
        for index in range(4):
            with self.subTest(slot=index):
                identities = list(right.storage_identities)
                identities[index] = left.storage_identities[index]
                shared = replace(right, storage_identities=tuple(identities))
                self.assertIn("SHARED_RUNTIME_STORAGE", self.validate(left, shared).reason_codes)
        for changed in (replace(right, run_id=left.run_id), replace(right, variant="vanilla"),
                        replace(right, order=0), replace(right, repetition=2),
                        replace(right, task_id="different-task")):
            self.assertEqual(self.validate(left, changed).status, "INVALID")

    def test_resealed_mutation_cannot_override_trusted_original_anchor(self):
        left, right = self.manifests()
        originals = {left.run_id: left.digest, right.run_id: right.digest}
        modified = replace(right, treatment_sha256="9" * 64)
        result = self.validate(left, modified, trusted_manifest_digests=originals)
        self.assertEqual(result.status, "INVALID")
        self.assertIn("MANIFEST_ANCHOR_MISMATCH", result.reason_codes)
        self.assertIn("UNDECLARED_TREATMENT", result.reason_codes)

    def test_fingerprint_order_is_canonical_not_an_experiment_difference(self):
        left, right = self.manifests()
        reordered = replace(right, controls=replace(right.controls, fingerprints=tuple(reversed(right.controls.fingerprints))))
        self.assertEqual(right.digest, reordered.digest)
        self.assertEqual(self.validate(left, reordered).status, "VALID")

    def test_equal_treatments_are_not_a_real_intervention(self):
        left, right = self.manifests()
        right = replace(right, treatment_sha256=left.treatment_sha256)
        result = self.validate(left, right, expected_treatments={"vanilla": "d" * 64, "aek": "d" * 64})
        self.assertEqual(result.status, "INVALID")
        self.assertIn("TREATMENT_NOT_DISTINCT", result.reason_codes)

    def test_schedule_rotates_order_with_unique_stable_run_ids(self):
        from core.pair import schedule_pairs
        plan = schedule_pairs("EXP-1", ("SC-001", "SC-002"), repeat=3)
        self.assertEqual(len(plan), 6)
        self.assertEqual([item.variants for item in plan[:3]],
                         [("vanilla", "aek"), ("aek", "vanilla"), ("vanilla", "aek")])
        self.assertEqual([item.variants for item in plan[3:]],
                         [("aek", "vanilla"), ("vanilla", "aek"), ("aek", "vanilla")])
        self.assertEqual([item.repetition for item in plan], [1, 2, 3, 1, 2, 3])
        self.assertEqual(len({item.pair_id for item in plan}), 6)
        self.assertEqual(len({run for item in plan for run in item.run_ids}), 12)
        self.assertEqual(plan, schedule_pairs("EXP-1", ("SC-001", "SC-002"), repeat=3))
        self.assertNotEqual(plan[0].pair_id, schedule_pairs("EXP-2", ("SC-001",), repeat=1)[0].pair_id)

    def test_schedule_rejects_duplicates_wrong_types_and_capacity_before_work(self):
        from core.pair import schedule_pairs
        for tasks, repeat in ((("SC-001", "SC-001"), 1), (("SC-001",), True),
                              (("SC-001",), 0), (("SC-001", "SC-002"), 1000), ([], 1)):
            with self.subTest(tasks=tasks, repeat=repeat), self.assertRaisesRegex(ValueError, "PAIR_SCHEDULE_INVALID"):
                schedule_pairs("EXP-1", tasks, repeat=repeat)

    def test_manifest_and_controls_refuse_mutable_or_malformed_identity(self):
        left, _ = self.manifests()
        for changes in ({"repetition": True}, {"order": False}, {"variant": "both"},
                        {"storage_identities": ["1" * 64] * 4}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(left, **changes)
        for changes in ({"fingerprints": left.controls.fingerprints[:-1]},
                        {"fingerprints": tuple((name, "bad") for name in CONTROL_KEYS)},
                        {"observed_model_sha256": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(left.controls, **changes)

    def test_codec_roundtrip_binds_exact_schema_and_rejects_unknown_fields(self):
        import json

        from core.pair import manifest_from_json, manifest_to_json
        left, _ = self.manifests()
        raw = manifest_to_json(left)
        self.assertEqual(manifest_from_json(raw), left)
        payload = json.loads(raw)
        payload["untrusted_extra"] = "secret-canary"
        with self.assertRaisesRegex(ValueError, "MANIFEST_JSON_INVALID"):
            manifest_from_json(json.dumps(payload).encode())
        with self.assertRaisesRegex(ValueError, "MANIFEST_JSON_INVALID"):
            manifest_from_json(b'{"variant":"vanilla","variant":"aek"}')

    def test_agent_protocol_pairing_does_not_assume_gateway_compatibility(self):
        left, _ = self.manifests()
        for changes in ({"agent": "codex", "protocol": "anthropic_messages"},
                        {"agent": "claude", "protocol": "openai_responses"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "AGENT_PROTOCOL_INVALID"):
                replace(left.controls, **changes)

    def test_schedule_cli_prints_plan_without_starting_models(self):
        entry = Path(__file__).resolve().parent / "benchmark" / "run.py"
        result = subprocess.run(
            [sys.executable, str(entry), "schedule", "--experiment-id", "EXP-1",
             "--tasks", "SC-001", "SC-002", "--repeat", "2"],
            cwd=entry.parent, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["pairs"]), 4)
        self.assertEqual(report["execution"], "NOT_STARTED")
        self.assertFalse(report["live_ready"])


if __name__ == "__main__":
    unittest.main()
