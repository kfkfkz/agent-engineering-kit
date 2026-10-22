"""Explicit v2 helper operations; fixture reviews are not human approvals."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ui_reference_v1_fixture', ROOT / 'tests/ui-reference.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class UiReferenceV2Tests(unittest.TestCase):
    setUp = fixture.UiReferenceTests.setUp
    tearDown = fixture.UiReferenceTests.tearDown
    invoke = fixture.UiReferenceTests.invoke
    invoke_raw = fixture.UiReferenceTests.invoke_raw

    @property
    def material_root(self):
        return self.repo / 'docs/01-需求/014-中文需求/原始材料/ui-reference'

    def request(self):
        value = fixture.request()
        value.update(schema_version=2, change_id='CHG-014', requirement_dir='014-中文需求',
                     targets=['P1', 'P2'], expected_index_sha256=None)
        value['screenshots'].append({**value['screenshots'][0], 'id': 'P2', 'page': '详情', 'state': 'error'})
        return value

    def call(self, operation, payload, **options):
        args = [operation, '--schema-version', '2', '--change-id', 'CHG-014', '--requirement-dir', '014-中文需求']
        if operation in {'record-review', 'bind-prototype'}:
            args += ['--expected-index-sha256', payload['expected_index_sha256']]
        if options.get('human'):
            args += ['--format', 'human']
        return self.invoke(*args, payload=payload)

    def index(self):
        path = self.material_root / '截图索引.json'
        return json.loads(path.read_bytes()), hashlib.sha256(path.read_bytes()).hexdigest()

    def test_v2_scaffold_creates_chinese_paths_without_promoting_placeholders(self):
        result = self.call('scaffold', self.request())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(set(value), {'schema_version', 'operation', 'change_id', 'status', 'index_sha256',
                                     'targets', 'gaps', 'coverage', 'truncated'})
        self.assertEqual((value['schema_version'], value['operation'], value['change_id']), (2, 'scaffold', 'CHG-014'))
        self.assertEqual(value['coverage'], 'PARTIAL')
        self.assertFalse(value['truncated'])
        index, digest = self.index()
        self.assertEqual(value['index_sha256'], digest)
        self.assertEqual(index['schema_version'], 1, 'the persisted legacy evidence contract must remain unchanged')
        self.assertEqual([row['id'] for row in value['targets']], ['P1', 'P2'])
        for target, row in zip(value['targets'], index['screenshots']):
            self.assertEqual(target['full_path'], str(self.material_root / row['path']))
            self.assertEqual(Path(target['full_path']).read_bytes(), b'')
            self.assertEqual((target['page'], target['state']), (row['page'], row['state']))
            self.assertEqual(target['file_status'], 'pending')
            self.assertEqual(target['contexts'][0]['applicability'], 'unverified')
            self.assertEqual(target['contexts'][0]['freshness'], 'unknown')

    def test_existing_scaffold_requires_cas_and_preserves_non_target_state_and_user_image(self):
        value = self.request()
        self.assertEqual(self.call('scaffold', value).returncode, 0)
        index, _ = self.index()
        image = self.material_root / index['screenshots'][0]['path']
        image.write_bytes(fixture.png_bytes())
        payload = {key: value[key] for key in ('schema_version', 'change_id', 'requirement_dir', 'targets')}
        payload.update(targets=['P1'], expected_index_sha256=self.index()[1])
        self.assertEqual(self.call('validate', payload).returncode, 4)
        before = (self.material_root / '截图索引.json').read_bytes()
        value.update(targets=['P1'], expected_index_sha256=hashlib.sha256(before).hexdigest())
        result = self.call('scaffold', value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.material_root / '截图索引.json').read_bytes(), before)
        self.assertEqual(image.read_bytes(), fixture.png_bytes())
        self.assertEqual(json.loads(result.stdout)['targets'][0]['file_status'], 'valid')
        for expected in (None, '0' * 64):
            value['expected_index_sha256'] = expected
            result = self.call('scaffold', value)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'CONFLICT')
            self.assertEqual((self.material_root / '截图索引.json').read_bytes(), before)

    def ready_image(self):
        self.assertEqual(self.call('scaffold', self.request()).returncode, 0)
        index, digest = self.index()
        (self.material_root / index['screenshots'][0]['path']).write_bytes(fixture.png_bytes())
        (self.material_root / index['screenshots'][1]['path']).unlink()
        payload = dict(schema_version=2, change_id='CHG-014', requirement_dir='014-中文需求',
                       targets=['P1'], expected_index_sha256=digest)
        self.assertEqual(self.call('validate', payload).returncode, 4)
        return payload

    def review(self):
        return {'screenshot_id': 'P1', 'context_id': 'P1-CAPTURED',
            'content_review': {'status': 'applicable', 'reviewer_type': 'agent_visual', 'reviewer': 'fixture-only',
                'reviewed_at': '2026-10-20T00:00:00Z', 'rationale': '合成夹具的文字确认，不是业务页面批准'},
            'freshness': {'status': 'confirmed_current', 'confirmer_type': 'agent_visual', 'confirmer': 'fixture-only',
                'confirmed_at': '2026-10-20T00:00:00Z'}}

    def test_v2_record_review_is_local_cas_bound_and_retry_preserves_sibling_missing_state(self):
        payload = self.ready_image()
        before, digest = self.index()
        payload.update(expected_index_sha256=digest, updates=[self.review()])
        result = self.call('record-review', payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual((output['operation'], output['coverage']), ('record-review', 'PARTIAL'))
        self.assertEqual(output['targets'][0]['contexts'][0]['applicability'], 'applicable')
        self.assertEqual(output['targets'][0]['contexts'][0]['freshness'], 'confirmed_current')
        self.assertEqual(output['targets'][0]['next_action'], 'CONTINUE_DESIGN')
        after, digest = self.index()
        self.assertEqual(after['screenshots'][1], before['screenshots'][1])
        self.assertFalse((self.material_root / after['screenshots'][1]['path']).exists())
        current = (self.material_root / '截图索引.json').read_bytes()
        payload['expected_index_sha256'] = digest
        repeated = self.call('record-review', payload)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual((self.material_root / '截图索引.json').read_bytes(), current)

    def test_full_projection_selector_resolves_only_an_existing_state_context_binding(self):
        payload = self.ready_image()
        payload.update(expected_index_sha256=self.index()[1], updates=[self.review()])
        self.assertEqual(self.call('record-review', payload).returncode, 0)
        index, digest = self.index()
        identity = dict(page_id='PAGE-1', area_id='AREA-1', state_id=index['screenshots'][0]['state'],
                        context_id='P1-CAPTURED')
        binding = {**{key: payload[key] for key in ('schema_version', 'change_id', 'requirement_dir', 'targets')},
            'expected_index_sha256': digest, 'artifact_plan_id': 'AP-fixture', 'gate_digest': 'a' * 64,
            'approved_root': 'docs/03-SDD/014-中文需求', 'prototype_refs': [dict(**identity,
                prototype_path='原型.html', prototype_sha256='b' * 64, feature_ids=['F1'], acceptance_ids=['A1'],
                change_intent='合成协议绑定，不是业务批准', depends_on=['P1'])]}
        result = self.call('bind-prototype', binding)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        selection = {**{key: payload[key] for key in ('schema_version', 'change_id', 'requirement_dir')},
                     'targets': [identity], 'expected_index_sha256': self.index()[1]}
        result = self.call('validate', selection)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual([row['id'] for row in output['targets']], ['P1'])
        self.assertEqual(output['coverage'], 'PARTIAL')
        self.assertEqual(output['targets'][0]['contexts'][0]['context_id'], 'P1-CAPTURED')
        before = (self.material_root / '截图索引.json').read_bytes()
        wrong_scope = copy.deepcopy(binding)
        scaffold = self.request()
        scaffold.update(targets=[identity], expected_index_sha256=hashlib.sha256(before).hexdigest())
        repeated = self.call('scaffold', scaffold)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual([row['id'] for row in json.loads(repeated.stdout)['targets']], ['P1'])
        self.assertEqual((self.material_root / '截图索引.json').read_bytes(), before)
        self.assertFalse((self.material_root / index['screenshots'][1]['path']).exists())
        wrong_scope.update(targets=[identity], expected_index_sha256=hashlib.sha256(before).hexdigest())
        wrong_scope['prototype_refs'][0].update(page_id='PAGE-2', area_id='AREA-2', prototype_path='other.html')
        escaped = self.call('bind-prototype', wrong_scope)
        self.assertEqual(escaped.returncode, 2, escaped.stdout + escaped.stderr)
        self.assertEqual((self.material_root / '截图索引.json').read_bytes(), before)
        selection.update(targets=[{**identity, 'context_id': 'CTX-OTHER'}],
                         expected_index_sha256=hashlib.sha256(before).hexdigest())
        self.assertEqual(self.call('validate', selection).returncode, 2)
        self.assertEqual((self.material_root / '截图索引.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
