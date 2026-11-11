#!/usr/bin/env python3
"""External mappings describe real sources and one writer, never execution."""
import copy
import importlib.util
import json
import runpy
from pathlib import Path
from dataclasses import asdict
import unittest

from aek.core.artifact.source_map import load_source_map, source_map_digest
from aek.core.artifact.registry import ARTIFACT_REGISTRY


def mapping():
    path = 'docs/版本/1.0.9/详细设计/查询/01-功能设计.md'
    return {'schema_version': 2, 'package_ref': 'docs/需求/1.0.9/查询', 'artifacts': [{
        'artifact_id': 'outline-design', 'sources': [path],
        'sections': [{'section': section, 'path': path, 'heading': '设计 / ' + section}
                     for section in ARTIFACT_REGISTRY.artifacts['outline-design'].sections],
        'points': [{'id': 'F1', 'path': path, 'heading': '设计 / 查询功能',
                    'covers': ['A1'], 'uses': [], 'sha256': 'a' * 64}],
        'globals': [{'path': path, 'heading': '设计 / 公共约束'}]}],
        'capability_bindings': [{'artifact_id': 'outline-design', 'orchestrator': 'aek',
            'writer': 'product-design-author', 'provider': 'product-design', 'client': 'codex',
            'entry': '$product-design', 'contract_path': '.agents/skills/product-design/SKILL.md',
            'contract_sha256': 'c' * 64}]}


class CompositionTests(unittest.TestCase):
    def test_composed_plan_binds_exact_real_source_set_and_preserves_legacy(self):
        from aek.core.artifact.plan import (build_plan, compose_plan, load_plan_bytes,
            freeze_plan, bind_stage, evaluate_binding)
        from aek.core.planning.policy import preview_plan
        spec = importlib.util.spec_from_file_location('base_plan_tests', Path(__file__).with_name('artifact-plan.test.py'))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        facts = fixture.known_false()
        legacy = build_plan(preview_plan('standard', fixture.SUBJECT, facts), facts)
        encode = lambda item: json.dumps(asdict(item), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        old_bytes = encode(legacy)
        composed = compose_plan(legacy, load_source_map(mapping()))
        self.assertEqual(composed.schema_version, 2)
        self.assertEqual(load_plan_bytes(encode(composed)), composed)
        self.assertEqual(load_plan_bytes(old_bytes), legacy)
        self.assertEqual(encode(legacy), old_bytes)
        credential = freeze_plan(composed, input_digest='b' * 64, generation_mode='native', frozen_at='now')
        from aek.application.planning import read_dynamic_state
        class Store:
            def snapshot(self):
                return {'artifact-plan.json': encode(composed), 'artifact-plan.gate.json': encode(credential)}
        state = read_dynamic_state(Store(), fixture.SUBJECT, 'b' * 64, facts)
        # A decoded composition/fact set alone is not live source evidence.
        self.assertEqual(state.state, 'INVALID')
        self.assertIn('current source verifier', state.reason)
        sources = {mapping()['artifacts'][0]['sources'][0]: 'e' * 64}
        binding = bind_stage(composed, credential, '概要设计', gate_digest='f' * 64,
            doc_hashes=sources, prerequisite_fingerprints={'需求分析': '1' * 64})
        self.assertEqual(evaluate_binding(binding, composed, credential, gate_digest='f' * 64,
            doc_hashes=sources, prerequisite_fingerprints={'需求分析': '1' * 64}), 'BOUND')
        with self.assertRaises(ValueError):
            bind_stage(composed, credential, '概要设计', gate_digest='f' * 64,
                doc_hashes={'概要设计.md': 'e' * 64}, prerequisite_fingerprints={'需求分析': '1' * 64})
        changed = mapping()
        changed['capability_bindings'][0]['writer'] = 'other-author'
        second = compose_plan(legacy, load_source_map(changed))
        self.assertNotEqual(second.digest, composed.digest)
        self.assertEqual(evaluate_binding(binding, second, credential, gate_digest='f' * 64,
            doc_hashes=sources, prerequisite_fingerprints={'需求分析': '1' * 64}), 'INVALID')

    def test_composed_plan_content_changes_do_not_rewrite_the_mapping_approval(self):
        from aek.core.artifact.plan import build_plan, compose_plan
        from aek.core.planning.policy import preview_plan
        fixture = runpy.run_path(str(Path(__file__).with_name('artifact-plan.test.py')))
        facts = fixture['known_false']()
        plan = build_plan(preview_plan('standard', fixture['SUBJECT'], facts), facts)
        first, second = mapping(), mapping()
        second['artifacts'][0]['points'][0]['sha256'] = 'b' * 64
        before = compose_plan(plan, load_source_map(first))
        after = compose_plan(plan, load_source_map(second))
        self.assertEqual(before, after)
        self.assertNotIn('sha256', before.source_map['artifacts'][0]['points'][0])

    def test_plan_stage_source_union_cannot_exceed_the_actual_capture_limit(self):
        from aek.core.artifact.plan import build_plan, compose_plan
        from aek.core.planning.policy import preview_plan
        fixture = runpy.run_path(str(Path(__file__).with_name('artifact-plan.test.py')))
        facts = fixture['known_false']()
        overlays = ('api-design', 'database-design')
        plan = build_plan(preview_plan('standard', fixture['SUBJECT'], facts, required_overlays=overlays),
                          facts, required_overlays=overlays)
        value = mapping()
        value['artifacts'], value['capability_bindings'] = [], []
        for index, ident in enumerate(('detail-design', 'api-design', 'database-design'), 1):
            sources = [f'docs/外部设计/{ident}/part-{part}.md' for part in range(6)]
            value['artifacts'].append({'artifact_id': ident, 'sources': sources,
                'sections': [{'section': section, 'path': sources[0], 'heading': section}
                             for section in ARTIFACT_REGISTRY.artifacts[ident].sections],
                'points': [{'id': f'D{index}', 'path': sources[0], 'heading': '设计', 'covers': ['F1'], 'uses': []}],
                'globals': []})
            value['capability_bindings'].append({**mapping()['capability_bindings'][0], 'artifact_id': ident})
        with self.assertRaisesRegex(ValueError, '16 physical files'):
            compose_plan(plan, load_source_map(value))

    def test_actual_path_and_logical_ids_are_separate_and_no_use_is_inferred(self):
        value = load_source_map(mapping())
        self.assertEqual(value.package_ref, 'docs/需求/1.0.9/查询')
        self.assertEqual(value.artifacts[0]['points'][0]['id'], 'F1')
        self.assertTrue(value.artifacts[0]['sources'][0].endswith('01-功能设计.md'))
        self.assertFalse(hasattr(value, 'actually_used'))

    def test_current_content_hash_is_integrity_not_a_change_to_mapping_topology(self):
        before, after = mapping(), mapping()
        after['artifacts'][0]['points'][0]['sha256'] = 'b' * 64
        self.assertEqual(source_map_digest(load_source_map(before)), source_map_digest(load_source_map(after)))
        after['artifacts'][0]['points'][0]['heading'] = '设计 / 另一功能'
        self.assertNotEqual(source_map_digest(load_source_map(before)), source_map_digest(load_source_map(after)))

    def test_contract_change_changes_composition_identity(self):
        first, second = mapping(), mapping()
        second['capability_bindings'][0]['contract_sha256'] = 'd' * 64
        self.assertNotEqual(source_map_digest(load_source_map(first)), source_map_digest(load_source_map(second)))

    def test_one_artifact_cannot_have_two_writers(self):
        value = mapping()
        other = {**value['capability_bindings'][0], 'writer': 'another-author'}
        value['capability_bindings'].append(other)
        with self.assertRaisesRegex(ValueError, 'WRITER_CONFLICT'):
            load_source_map(value)

    def test_paths_roles_and_unsupported_authority_claims_are_rejected(self):
        for path in ('../outside.md', '/tmp/outside.md', 'docs/../outside.md', 'docs/NUL/file.md',
                     'docs/x:stream/file.md', 'docs/memory/secret.md', 'docs/Memory/secret.md',
                     'docs/Delivery-Receipts/private.md', 'docs/设计/run.py'):
            with self.subTest(path=path):
                value = mapping()
                value['artifacts'][0]['sources'] = [path]
                for category in ('sections', 'points', 'globals'):
                    for location in value['artifacts'][0][category]:
                        location['path'] = path
                with self.assertRaises(ValueError):
                    load_source_map(value)
        value = mapping()
        value['capability_bindings'][0]['actually_used'] = True
        with self.assertRaises(ValueError):
            load_source_map(value)
        value = mapping()
        value['capability_bindings'][0]['client'] = ['codex']
        with self.assertRaises(ValueError):
            load_source_map(value)
        value = mapping()
        value['artifacts'][0]['points'][0]['id'] = 'D1'
        with self.assertRaises(ValueError):
            load_source_map(value)

    def test_mapping_is_complete_for_required_roles_and_rejects_duplicate_or_missing_locations(self):
        value = mapping()
        value['artifacts'][0]['sections'].pop()
        with self.assertRaisesRegex(ValueError, 'MISSING_ARTIFACT_CONTENT'):
            load_source_map(value)
        value = mapping()
        value['artifacts'][0]['points'].append(copy.deepcopy(value['artifacts'][0]['points'][0]))
        with self.assertRaises(ValueError):
            load_source_map(value)
        value = mapping()
        value['artifacts'][0]['points'][0]['path'] = 'docs/另外.md'
        with self.assertRaises(ValueError):
            load_source_map(value)


if __name__ == '__main__':
    unittest.main()
