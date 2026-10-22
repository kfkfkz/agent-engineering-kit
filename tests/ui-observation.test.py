"""Data-only screenshot observations never constitute applicability or approval."""
from __future__ import annotations

import sys
import copy
import hashlib
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aek.core.artifact import ui_observation as observation


class UiObservationTests(unittest.TestCase):
    def reusable(self, record, source, contract, regions):
        digest = hashlib.sha256(observation.canonical_bytes(record)).hexdigest()
        return observation.can_reuse(record, source, contract, regions, expected_observation_sha256=digest)

    def test_prompt_resource_is_bound_and_managed_in_both_skill_surfaces(self):
        from installer.registry import REGISTRY
        resource = "skills/design-pipeline/references/screenshot-observation.md"
        destinations = {item.destination_path for item in REGISTRY if item.source_path == resource}
        self.assertEqual(destinations, {
            ".claude/skills/design-pipeline/references/screenshot-observation.md",
            ".agents/skills/design-pipeline/references/screenshot-observation.md"})
        prompt = (Path(__file__).resolve().parents[1] / resource).read_bytes()
        self.assertLess(len(prompt), 8 * 1024)
        source, candidate, contract, provenance = self.inputs()
        contract["prompt_sha256"] = hashlib.sha256(prompt).hexdigest()
        record = observation.build_observation(source, contract, ("QUERY",), candidate, provenance=provenance)
        self.assertEqual(record["extraction_contract"]["prompt_sha256"], hashlib.sha256(prompt).hexdigest())

    def test_observation_workflow_resource_is_installed_without_expanding_default_skill(self):
        from installer.registry import REGISTRY
        resource = "skills/design-pipeline/references/screenshot-observation-workflow.md"
        self.assertEqual({item.destination_path for item in REGISTRY if item.source_path == resource}, {
            ".agents/skills/design-pipeline/references/screenshot-observation-workflow.md",
            ".claude/skills/design-pipeline/references/screenshot-observation-workflow.md"})
        root = Path(__file__).resolve().parents[1]
        self.assertLess(len((root / resource).read_bytes()), 8 * 1024)
        self.assertIn("references/screenshot-observation-workflow.md",
                      (root / "skills/design-pipeline/SKILL.md").read_text(encoding="utf-8"))

    def inputs(self):
        source = {"scope_digest": "a" * 64, "evidence_id": "P1", "image_sha256": "b" * 64}
        candidate = {"schema_version": 2, "regions": [{
            "region_id": "QUERY", "observation_kind": "observed", "layout": "单行查询区",
            "controls": [{"kind": "button", "label": "查询", "state": "enabled"}],
            "style_clues": ["蓝色主按钮"],
        }], "uncertainties": ["没有看到接口或鉴权规则"]}
        contract = {"schema_version": 2, "semantic_version": "ui-observation-v1", "prompt_sha256": "c" * 64}
        provenance = {"channel": "agent_visual", "model": None, "observed_at": "2026-10-16T08:00:00Z"}
        return source, candidate, contract, provenance

    def test_observation_binds_control_side_source_not_model_claims(self):
        source, candidate, contract, provenance = self.inputs()
        record = observation.build_observation(source, contract, ("QUERY",), candidate,
            provenance=provenance)
        self.assertEqual(record["evidence_scope"], source)
        self.assertEqual(record["extraction_contract"], contract)
        self.assertEqual(record["coverage"], {
            "required_regions": ["QUERY"], "observed_regions": ["QUERY"], "missing_regions": []})
        self.assertNotIn("applicable", record)
        self.assertNotIn("confirmation", record)
        self.assertEqual(record["provenance"]["model"], None)

    def test_cache_reuse_requires_same_evidence_domain_image_contract_and_sufficient_regions(self):
        source, candidate, contract, provenance = self.inputs()
        record = observation.build_observation(source, contract, ("QUERY",), candidate, provenance=provenance)
        self.assertTrue(self.reusable(record, source, contract, ("QUERY",)))
        for changed_source in ({**source, "scope_digest": "d" * 64},
                               {**source, "image_sha256": "e" * 64},
                               {**source, "evidence_id": "P2"}):
            with self.subTest(source=changed_source):
                self.assertFalse(self.reusable(record, changed_source, contract, ("QUERY",)))
        self.assertFalse(self.reusable(record, source,
            {**contract, "prompt_sha256": "f" * 64}, ("QUERY",)))
        self.assertFalse(self.reusable(record, source, contract, ("QUERY", "RESULTS")))

    def test_cache_rejects_changed_text_even_when_source_and_coverage_still_match(self):
        source, candidate, contract, provenance = self.inputs()
        record = observation.build_observation(source, contract, ("QUERY",), candidate, provenance=provenance)
        expected = hashlib.sha256(observation.canonical_bytes(record)).hexdigest()
        record["regions"][0]["layout"] = "被改写但仍满足schema的观察"
        self.assertFalse(observation.can_reuse(record, source, contract, ("QUERY",),
                                             expected_observation_sha256=expected))

    def test_model_cannot_set_source_approval_or_executable_extra_fields(self):
        source, candidate, contract, provenance = self.inputs()
        for field, value in (("image_sha256", "d" * 64), ("applicable", True),
                             ("confirmation", "ACCEPTED"), ("command", "ignored data")):
            with self.subTest(field=field), self.assertRaises(observation.ObservationError):
                observation.build_observation(source, contract, ("QUERY",),
                    {**candidate, field: value}, provenance=provenance)
        candidate["regions"][0]["observation_kind"] = "inferred"
        record = observation.build_observation(source, contract, ("QUERY",), candidate, provenance=provenance)
        self.assertEqual(record["coverage"]["missing_regions"], ["QUERY"])
        self.assertFalse(self.reusable(record, source, contract, ("QUERY",)))

    def test_saved_coverage_is_recomputed_and_candidate_mutation_is_detached(self):
        source, candidate, contract, provenance = self.inputs()
        record = observation.build_observation(source, contract, ("QUERY",), candidate, provenance=provenance)
        original = copy.deepcopy(record)
        candidate["regions"][0]["layout"] = "模型随后改写"
        source["image_sha256"] = "d" * 64
        self.assertEqual(record, original)
        record["coverage"]["observed_regions"].append("ADMIN")
        with self.assertRaises(observation.ObservationError):
            observation.validate_observation(record)

    def test_observation_does_not_capture_unrequested_regions(self):
        source, candidate, contract, provenance = self.inputs()
        candidate["regions"].append({**copy.deepcopy(candidate["regions"][0]), "region_id": "ADMIN"})
        with self.assertRaises(observation.ObservationError):
            observation.build_observation(source, contract, ("QUERY",), candidate, provenance=provenance)

    def test_region_control_text_schema_and_byte_limits_are_enforced(self):
        reasons = {"bool_schema": "unsupported observation schema", "duplicate_region": "duplicate observation region",
                   "regions": "invalid observation region count", "controls": "invalid observation control count",
                   "text": "invalid observation text", "bytes": "exceeds byte limit", "surrogate": "bounded UTF-8 JSON"}
        for mode in ("bool_schema", "duplicate_region", "regions", "controls", "text", "bytes", "surrogate"):
            with self.subTest(mode=mode):
                source, candidate, contract, provenance = self.inputs()
                required = ("QUERY",)
                region = candidate["regions"][0]
                if mode == "bool_schema":
                    candidate["schema_version"] = True
                elif mode == "duplicate_region":
                    candidate["regions"].append(copy.deepcopy(region))
                elif mode == "regions":
                    candidate["regions"] = [{**copy.deepcopy(region), "region_id": f"R{i}"} for i in range(33)]
                elif mode == "controls":
                    region["controls"] *= 65
                elif mode == "text":
                    region["controls"][0]["label"] = "长" * 161
                elif mode == "bytes":
                    region["controls"] = [{"kind": "button", "label": "长" * 160, "state": "enabled"}] * 64
                    candidate["regions"] = [{**copy.deepcopy(region), "region_id": f"R{i}"} for i in range(3)]
                    required = ("R0", "R1", "R2")
                else:
                    region["layout"] = "\ud800"
                with self.assertRaisesRegex(observation.ObservationError, reasons[mode]):
                    observation.build_observation(source, contract, required, candidate, provenance=provenance)

    def test_decode_rejects_duplicate_keys_invalid_utf8_and_oversized_input(self):
        _source, candidate, _contract, _provenance = self.inputs()
        self.assertEqual(observation.decode_candidate(json.dumps(candidate, ensure_ascii=False).encode("utf-8")), candidate)
        for raw in (b'{"schema_version":2,"schema_version":2}', b'\xff',
                    b' ' * (64 * 1024 + 1), b'{"value":NaN}'):
            with self.subTest(raw=raw[:80]), self.assertRaises(observation.ObservationError):
                observation.decode_candidate(raw)


if __name__ == "__main__":
    unittest.main()
