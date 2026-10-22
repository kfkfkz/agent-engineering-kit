from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import copy
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('query_workflow_fixtures',
    ROOT / 'tests/ui-observation-workflow.test.py')
data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data)
helper = data.helper


class QueryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = data.ObservationWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def ready_observation(self):
        fixture = self.fixture
        rc, prepared = fixture.prepare()
        self.assertEqual(rc, 0, prepared)
        rc, recorded = fixture.record(prepared)
        self.assertEqual(rc, 0, recorded)
        review = {'schema_version': 1, 'updates': [{
            'screenshot_id': 'P1', 'context_id': 'P1-CAPTURED',
            'content_review': {'status': 'applicable', 'reviewer_type': 'maintainer',
                'reviewer': 'k', 'reviewed_at': '2026-10-19T00:00:00Z', 'rationale': '测试夹具当前查询区'},
            'freshness': {'status': 'confirmed_current', 'confirmer_type': 'maintainer',
                'confirmer': 'k', 'confirmed_at': '2026-10-19T00:00:00Z'}}]}
        rc, result = fixture.invoke('record-review', review,
            expected=hashlib.sha256(fixture.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 0, result)
        return recorded

    def request(self, **changes):
        return {'schema_version': 2, 'query': '查询', 'context_ref': 'P1-CAPTURED', **changes}

    def invoke(self, payload, *, context='P1-CAPTURED'):
        f = self.fixture
        result = subprocess.run([sys.executable, str(data.HELPER), 'observation-query',
            '--repo-root', str(f.repo), '--change-id', 'CHG-003', '--requirement-dir', f.requirement,
            '--context-ref', context], input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            capture_output=True, check=False, timeout=15)
        self.assertTrue(result.stdout, result.stderr.decode('utf-8', errors='replace'))
        return result.returncode, json.loads(result.stdout)

    def test_cli_reads_current_chinese_observation_as_explicit_file_fallback_without_writes(self):
        recorded = self.ready_observation()
        before = {path.relative_to(self.fixture.material): path.read_bytes()
                  for path in self.fixture.material.rglob('*') if path.is_file()}
        rc, result = self.invoke(self.request())
        self.assertEqual(rc, 0, result)
        self.assertEqual(result['backend'], 'bounded_files')
        self.assertEqual(result['status'], 'UNAVAILABLE', 'fallback is not adopted SQLite capability')
        self.assertEqual(len(result['hits']), 1)
        self.assertEqual(result['hits'][0]['observation_sha256'], recorded['observation_sha256'])
        self.assertIn('查询', result['hits'][0]['snippet'])
        self.assertEqual(result['hits'][0]['applicability_ref']['context_ref'], 'P1-CAPTURED')
        after = {path.relative_to(self.fixture.material): path.read_bytes()
                 for path in self.fixture.material.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_control_host_can_use_adopted_sqlite_with_actual_source_revalidation(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        self.ready_observation()
        result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
            self.fixture.requirement, 'P1-CAPTURED', index_factory=ScopedObservationIndex)
        self.assertEqual(rc, 0, result)
        self.assertEqual(result['backend'], 'sqlite_memory')
        self.assertEqual(result['status'], 'COMPLETE_FOR_SCOPE')
        self.assertEqual(len(result['hits']), 1)

    @unittest.skipUnless(sys.platform == 'linux', 'qualified loaded SQLite control profile')
    def test_final_material_io_cannot_publish_sqlite_text_after_control_scope_changes(self):
        runtime_spec = importlib.util.spec_from_file_location('query_runtime_protocol_fixture',
            ROOT / 'tests/ui-sqlite-runtime.test.py')
        runtime = importlib.util.module_from_spec(runtime_spec)
        runtime_spec.loader.exec_module(runtime)
        factory, _host, authority, _port = runtime.protocol_factory()
        self.ready_observation()
        class ControllerFactory:
            finished = False
            @contextmanager
            def __call__(self, *args, **kwargs):
                with factory(*args, **kwargs) as projection:
                    yield projection
                self.finished = True
            def control_fence(self):
                return factory.control_fence()
        controller = ControllerFactory()
        original = helper._observation_contract
        mutations = []
        def read_then_revoke_scope():
            value = original()
            if controller.finished:
                mutations.append(True)
                authority.scope_ref = 'e' * 64
            return value
        with patch.object(helper, '_observation_contract', side_effect=read_then_revoke_scope):
            result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
                self.fixture.requirement, 'P1-CAPTURED', index_factory=controller)
        self.assertTrue(mutations)
        self.assertEqual(rc, 0, result)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['hits'], [])
        self.assertIn({'reason_code': 'SQLITE_UNAVAILABLE'}, result['gaps'])

    @unittest.skipUnless(sys.platform == 'linux', 'qualified loaded SQLite control profile')
    def test_final_material_io_rechecks_author_identity_and_loaded_descriptor(self):
        from dataclasses import replace
        runtime_spec = importlib.util.spec_from_file_location('query_runtime_mutation_fixture',
            ROOT / 'tests/ui-sqlite-runtime.test.py')
        runtime = importlib.util.module_from_spec(runtime_spec)
        runtime_spec.loader.exec_module(runtime)
        self.ready_observation()
        for change in ('author', 'identity', 'descriptor'):
            factory, _host, authority, _port = runtime.protocol_factory()
            class ControllerFactory:
                finished = False
                @contextmanager
                def __call__(self, *args, **kwargs):
                    with factory(*args, **kwargs) as projection:
                        yield projection
                    self.finished = True
                def control_fence(self):
                    return factory.control_fence()
            controller = ControllerFactory()
            original = helper._observation_contract
            mutations = []
            def read_then_change():
                value = original()
                if controller.finished:
                    mutations.append(True)
                    if change == 'author':
                        authority.author = 'another-author'
                    elif change == 'identity':
                        authority.current_identity = None
                    else:
                        factory.runtime = replace(factory.runtime, sqlite_version='changed-after-query')
                return value
            with self.subTest(change=change), patch.object(helper, '_observation_contract', side_effect=read_then_change):
                result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
                    self.fixture.requirement, 'P1-CAPTURED', index_factory=controller)
            self.assertTrue(mutations)
            self.assertEqual(rc, 0)
            self.assertEqual(result['status'], 'PARTIAL')
            self.assertEqual(result['hits'], [])
            self.assertIn({'reason_code': 'SQLITE_UNAVAILABLE'}, result['gaps'])

    def test_optional_control_fence_requires_true_and_is_after_all_material_reads(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        self.ready_observation()
        original_read = data.fs.read_at
        for mode in (True, False, 1, 'raise'):
            checked = []
            class ControllerFactory:
                def __call__(self, *args, **kwargs):
                    return ScopedObservationIndex(*args, **kwargs)
                def control_fence(self):
                    checked.append(True)
                    if mode == 'raise':
                        raise RuntimeError('private controller diagnostic must not be echoed')
                    return mode
            def read_before_fence(handle, name, limit):
                self.assertFalse(checked, 'material IO happened after the final control fence')
                return original_read(handle, name, limit)
            with self.subTest(mode=mode), patch('aek.adapters.ui_filesystem.read_at', side_effect=read_before_fence):
                result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
                    self.fixture.requirement, 'P1-CAPTURED', index_factory=ControllerFactory())
            self.assertEqual(checked, [True])
            self.assertEqual(rc, 0)
            if mode is True:
                self.assertEqual(result['status'], 'COMPLETE_FOR_SCOPE')
                self.assertEqual(len(result['hits']), 1)
            else:
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertEqual(result['hits'], [])
                self.assertIn({'reason_code': 'SQLITE_UNAVAILABLE'}, result['gaps'])
            self.assertNotIn('private controller diagnostic', json.dumps(result))

    def test_request_cannot_choose_another_control_context_or_expand_capabilities(self):
        for change in ({'context_ref': 'OTHER'}, {'schema_version': True}, {'limit': True},
                       {'limit': 21}, {'query': '中' * 257}, {'database_path': '/tmp/private.db'},
                       {'backend': 'sqlite_memory'}):
            with self.subTest(change=change), patch.object(helper, '_open_absolute_dir') as opened:
                with self.assertRaises(helper.UiReferenceError):
                    helper._observation_query(self.fixture.repo, 'CHG-003', self.request(**change),
                        self.fixture.requirement, 'P1-CAPTURED')
                opened.assert_not_called()

    def test_unreviewed_or_stale_screenshot_never_reads_cached_observation_text(self):
        _, prepared = self.fixture.prepare()
        _, recorded = self.fixture.record(prepared)
        original = data.fs.read_at
        def forbid_observation(handle, name, limit):
            self.assertNotEqual(name, recorded['observation_sha256'] + '.json')
            return original(handle, name, limit)
        with patch('aek.adapters.ui_filesystem.read_at', side_effect=forbid_observation):
            result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
                self.fixture.requirement, 'P1-CAPTURED')
        self.assertEqual(rc, 0)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['hits'], [])

    def test_page_and_state_only_narrow_before_loading_text_and_empty_scope_is_not_global_absence(self):
        recorded = self.ready_observation()
        original = data.fs.read_at
        def forbid_observation(handle, name, limit):
            self.assertNotEqual(name, recorded['observation_sha256'] + '.json')
            return original(handle, name, limit)
        refs = []
        for filters in ({'page': '别的页面'}, {'state': 'loading'}):
            with self.subTest(filters=filters), patch('aek.adapters.ui_filesystem.read_at', side_effect=forbid_observation):
                result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(**filters),
                    self.fixture.requirement, 'P1-CAPTURED')
            self.assertEqual(rc, 0)
            self.assertEqual(result['hits'], [])
            refs.append(result['scope_ref'])
        self.assertNotEqual(*refs)

    def test_missing_or_ambiguous_context_and_unknown_area_require_evidence_not_a_guessed_scope(self):
        self.ready_observation()
        rc, result = self.invoke(self.request(context_ref='OTHER'), context='OTHER')
        self.assertEqual(rc, 4, result)
        self.assertEqual(result['reason_code'], 'NEEDS_EVIDENCE')
        self.assertNotIn('hits', result)
        rc, result = self.invoke(self.request(area='AREA-1'))
        self.assertEqual(rc, 4, result)
        self.assertEqual(result['reason_code'], 'NEEDS_EVIDENCE')

    def test_mutated_or_missing_observation_body_does_not_return_stale_text(self):
        recorded = self.ready_observation()
        path = self.fixture.material / recorded['observation_ref']
        for content in (b'{"sensitive":"must not return"}', None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                rc, result = self.invoke(self.request())
                self.assertEqual(rc, 0, result)
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertEqual(result['hits'], [])
                self.assertNotIn('sensitive', json.dumps(result))

    def test_actual_sqlite_failure_uses_explicit_validated_file_fallback(self):
        import sqlite3
        from aek.adapters.ui_query import ScopedObservationIndex
        self.ready_observation()
        with patch('aek.adapters.ui_query.sqlite3.connect', side_effect=sqlite3.OperationalError('failure')):
            result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
                self.fixture.requirement, 'P1-CAPTURED', index_factory=ScopedObservationIndex)
        self.assertEqual(rc, 0)
        self.assertEqual(result['backend'], 'bounded_files')
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertEqual(len(result['hits']), 1)
        self.assertEqual(result['gaps'], [{'reason_code': 'SQLITE_UNAVAILABLE'}])

    def test_late_image_context_binding_and_prompt_changes_remove_old_text_before_return(self):
        recorded = self.ready_observation()
        f = self.fixture
        index_raw, image_raw = f.index.read_bytes(), f.image.read_bytes()
        binding_path = f.material / 'observations/索引.v2.json'
        binding_raw = binding_path.read_bytes()
        original = data.fs.read_at
        contract = helper._observation_contract()
        for change in ('image', 'context', 'binding', 'prompt'):
            with self.subTest(change=change):
                f.index.write_bytes(index_raw)
                f.image.write_bytes(image_raw)
                binding_path.write_bytes(binding_raw)
                active = dict(contract)
                def read_then_change(handle, name, limit):
                    raw = original(handle, name, limit)
                    if name == recorded['observation_sha256'] + '.json':
                        if change == 'image':
                            f.image.write_bytes(data.fixtures.png_bytes(2, 1))
                        elif change == 'context':
                            value = json.loads(index_raw)
                            value['screenshots'][0]['applies_to'][0]['content_review']['status'] = 'not_applicable'
                            f.index.write_text(json.dumps(value), encoding='utf-8')
                        elif change == 'binding':
                            value = json.loads(binding_raw)
                            value['bindings'] = []
                            binding_path.write_text(json.dumps(value), encoding='utf-8')
                        else:
                            active['prompt_sha256'] = '0' * 64
                    return raw
                with patch.object(helper, '_observation_contract', side_effect=lambda: dict(active)), patch(
                        'aek.adapters.ui_filesystem.read_at', side_effect=read_then_change):
                    result, rc = helper._observation_query(f.repo, 'CHG-003', self.request(),
                        f.requirement, 'P1-CAPTURED')
                self.assertEqual(rc, 0, result)
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertEqual(result['hits'], [])

    def test_literal_queries_and_normalization_match_between_real_sqlite_and_file_path(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        self.ready_observation()
        for query in ('查询', 'ＥＮＡＢＬＥＤ', "' OR 1=1 --", '%', '*'):
            with self.subTest(query=query):
                file_result, _ = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(query=query),
                    self.fixture.requirement, 'P1-CAPTURED')
                sql_result, _ = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(query=query),
                    self.fixture.requirement, 'P1-CAPTURED', index_factory=ScopedObservationIndex)
                self.assertEqual(file_result['hits'], sql_result['hits'])
                self.assertEqual(file_result['source_generation'], sql_result['source_generation'])
                self.assertEqual(len(sql_result['hits']), 1 if query in ('查询', 'ＥＮＡＢＬＥＤ') else 0)

    def test_partial_extraction_keeps_kind_and_gap_instead_of_claiming_complete_scope(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        self.ready_observation()
        _, prepared = self.fixture.prepare(['QUERY', 'MISSING'])
        rc, result = self.fixture.record(prepared, kind='inferred')
        self.assertEqual(rc, 0, result)
        output, _ = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
            self.fixture.requirement, 'P1-CAPTURED', index_factory=ScopedObservationIndex)
        self.assertEqual(output['status'], 'PARTIAL')
        self.assertEqual(output['hits'][0]['observation_kind'], 'inferred')
        self.assertIn({'evidence_id': 'P1', 'reason_code': 'OBSERVATION_PARTIAL'}, output['gaps'])

    def test_query_lock_timeout_does_not_terminate_or_override_owner(self):
        self.ready_observation()
        handle = data.fs.open_absolute_dir(self.fixture.material)
        try:
            with data.fs.exclusive_lock(handle):
                rc, result = self.invoke(self.request())
                self.assertEqual(rc, 5, result)
                self.assertEqual(result['reason_code'], 'TIMEOUT')
                # A different process still cannot acquire the owner's lock.
                program = """import sys
sys.path.insert(0, sys.argv[1])
from aek.adapters import ui_filesystem as fs
from pathlib import Path
handle = fs.open_absolute_dir(Path(sys.argv[2]))
try:
    try:
        with fs.exclusive_lock(handle, timeout=0):
            code = 0
    except TimeoutError:
        code = 5
finally:
    fs.close_dir(handle)
raise SystemExit(code)
"""
                probe = subprocess.run([sys.executable, '-c', program, str(ROOT), str(self.fixture.material)],
                    capture_output=True, check=False, timeout=10)
                self.assertEqual(probe.returncode, 5, probe.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(self.invoke(self.request())[0], 0)
        finally:
            data.fs.close_dir(handle)

    def test_query_deadline_is_checked_even_when_loading_returns_no_hits(self):
        recorded = self.ready_observation()
        now = [0.0]
        original = data.fs.read_at
        def read_then_expire(handle, name, limit):
            raw = original(handle, name, limit)
            if name == recorded['observation_sha256'] + '.json':
                now[0] = 3.0
            return raw
        with patch.object(helper.time, 'monotonic', side_effect=lambda: now[0]), patch(
                'aek.adapters.ui_filesystem.read_at', side_effect=read_then_expire):
            with self.assertRaises(TimeoutError):
                helper._observation_query(self.fixture.repo, 'CHG-003', self.request(query='没有命中'),
                    self.fixture.requirement, 'P1-CAPTURED')

    def test_other_permission_text_is_not_loaded_or_returned_and_its_revision_does_not_invalidate_scope(self):
        request = data.fixtures.request()
        request['screenshots'].append({**copy.deepcopy(request['screenshots'][0]), 'id': 'P2',
            'permissions': {'function_id': 'FP01', 'data_id': 'DP02'}})
        other_fixture = data.ObservationWorkflowTests()
        with patch.object(data.fixtures, 'request', return_value=request):
            other_fixture.setUp()
        self.addCleanup(other_fixture.tearDown)
        self.fixture = other_fixture
        first = self.ready_observation()
        index = json.loads(other_fixture.index.read_bytes())
        second = next(row for row in index['screenshots'] if row['id'] == 'P2')
        (other_fixture.material / second['path']).write_bytes(data.fixtures.png_bytes(2, 1))
        self.assertEqual(other_fixture.invoke('validate')[0], 1)
        rc, prepared = other_fixture.invoke('observation-prepare', {'schema_version': 2, 'evidence_id': 'P2',
            'context_ref': 'P2-CAPTURED', 'required_regions': ['QUERY']},
            expected=hashlib.sha256(other_fixture.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 0, prepared)
        payload = other_fixture.record_payload(prepared)
        payload['candidate']['regions'][0]['layout'] = 'private unrelated permission data'
        rc, second_recorded = other_fixture.invoke('observation-record', payload,
            expected=hashlib.sha256(other_fixture.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 0, second_recorded)
        before, _ = helper._observation_query(other_fixture.repo, 'CHG-003', self.request(),
            other_fixture.requirement, 'P1-CAPTURED')
        original = data.fs.read_at
        binding_path = other_fixture.material / 'observations/索引.v2.json'
        def read_then_change_other_scope(handle, name, limit):
            self.assertNotEqual(name, second_recorded['observation_sha256'] + '.json')
            raw = original(handle, name, limit)
            if name == first['observation_sha256'] + '.json':
                value = json.loads(binding_path.read_bytes())
                value['bindings'] = [row for row in value['bindings'] if row['evidence_id'] != 'P2']
                binding_path.write_text(json.dumps(value), encoding='utf-8')
            return raw
        with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_change_other_scope):
            result, rc = helper._observation_query(other_fixture.repo, 'CHG-003', self.request(),
                other_fixture.requirement, 'P1-CAPTURED')
        self.assertEqual(rc, 0)
        self.assertEqual(result, before)
        self.assertNotIn('private', json.dumps(result))

    def test_real_sqlite_single_group_overflow_is_disclosed_like_file_matching(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        self.ready_observation()
        regions = [f'R{number}' for number in range(25)]
        _, prepared = self.fixture.prepare(regions)
        payload = self.fixture.record_payload(prepared)
        base = payload['candidate']['regions'][0]
        payload['candidate']['regions'] = [{**copy.deepcopy(base), 'region_id': ident} for ident in regions]
        rc, recorded = self.fixture.invoke('observation-record', payload,
            expected=hashlib.sha256(self.fixture.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 0, recorded)
        for factory in (None, ScopedObservationIndex):
            with self.subTest(backend=factory):
                result, _ = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(limit=20),
                    self.fixture.requirement, 'P1-CAPTURED', index_factory=factory)
                self.assertEqual(len(result['hits']), 20)
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertTrue(result['truncated'])

    def test_file_payload_removed_or_corrupted_after_loading_does_not_return_cached_body(self):
        recorded = self.ready_observation()
        path = self.fixture.material / recorded['observation_ref']
        original_bytes = path.read_bytes()
        original = data.fs.read_at
        for mode in ('delete', 'corrupt'):
            with self.subTest(mode=mode):
                path.write_bytes(original_bytes)
                changed = False
                def read_then_change(handle, name, limit):
                    nonlocal changed
                    raw = original(handle, name, limit)
                    if name == recorded['observation_sha256'] + '.json' and not changed:
                        changed = True
                        if mode == 'delete':
                            path.unlink()
                        else:
                            path.write_bytes(b'changed user observation')
                    return raw
                with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_change):
                    result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(),
                        self.fixture.requirement, 'P1-CAPTURED')
                self.assertEqual(rc, 0)
                self.assertEqual(result['hits'], [])
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertIn({'evidence_id': 'P1', 'reason_code': 'STALE'}, result['gaps'])

    def area_fixture(self):
        self.ready_observation()
        _, prepared = self.fixture.prepare(['AQUERY', 'ZQUERY'])
        payload = self.fixture.record_payload(prepared)
        base = payload['candidate']['regions'][0]
        payload['candidate']['regions'] = [{**copy.deepcopy(base), 'region_id': ident}
                                          for ident in ('AQUERY', 'ZQUERY')]
        rc, recorded = self.fixture.invoke('observation-record', payload,
            expected=hashlib.sha256(self.fixture.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 0, recorded)
        region = {'page_id': 'PAGE-1', 'area_id': 'AREA-1', 'region_id': 'ZQUERY',
            'intent': 'PRESERVE', 'feature_ids': ['F1'], 'acceptance_ids': ['A1'],
            'source_ids': [], 'screenshot_ids': ['P1'], 'rationale': '只查当前页的指定区域',
            'adjacent_context': None, 'observations': [
                {'evidence_id': 'P1', 'sha256': recorded['observation_sha256']}]}
        baseline = {'schema_version': 2, 'sources': [], 'regions': [region]}
        path = self.fixture.repo / 'docs/03-SDD' / self.fixture.requirement / 'UI设计.md'
        path.parent.mkdir(parents=True)
        path.write_text('## 设计基线\n\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n',
                        encoding='utf-8')
        return path, baseline, recorded

    def test_area_uses_actual_region_mapping_before_limit_in_both_backends(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        self.area_fixture()
        for factory in (None, ScopedObservationIndex):
            with self.subTest(backend=factory):
                result, rc = helper._observation_query(self.fixture.repo, 'CHG-003',
                    self.request(area='AREA-1', limit=1), self.fixture.requirement,
                    'P1-CAPTURED', index_factory=factory)
                self.assertEqual(rc, 0, result)
                self.assertEqual([hit['region_id'] for hit in result['hits']], ['ZQUERY'])
                self.assertFalse(result['truncated'])
                unfiltered, _ = helper._observation_query(self.fixture.repo, 'CHG-003',
                    self.request(limit=1), self.fixture.requirement, 'P1-CAPTURED', index_factory=factory)
                self.assertNotEqual(result['scope_ref'], unfiltered['scope_ref'])
                self.assertNotEqual(result['source_generation'], unfiltered['source_generation'])

    def test_typed_area_filters_current_context_and_actual_state_before_any_other_source_io(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        path, baseline, _ = self.area_fixture()
        current = baseline['regions'][0]
        current.update(state_id='default', context_id='P1-CAPTURED')
        other = dict(copy.deepcopy(current), context_id='CTX-OTHER', region_id='AQUERY', source_ids=['SRC-1'])
        loading = dict(copy.deepcopy(current), state_id='loading', region_id='AQUERY', source_ids=['SRC-1'])
        baseline['regions'] += [other, loading]
        (self.fixture.repo / 'frontend').mkdir()
        (self.fixture.repo / 'frontend/private.css').write_bytes(b'other permission source')
        baseline['sources'] = [{'id': 'SRC-1', 'kind': 'layout', 'path': 'frontend/private.css',
            'sha256': hashlib.sha256(b'other permission source').hexdigest(), 'certainty': 'source_backed'}]
        path.write_text('```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n', encoding='utf-8')
        original = data.fs.read_at
        def forbid_other_source(handle, name, limit):
            self.assertNotEqual(name, 'private.css', 'filter scope before reading any other context source')
            return original(handle, name, limit)
        for factory in (None, ScopedObservationIndex):
            with self.subTest(backend=factory):
                with patch('aek.adapters.ui_filesystem.read_at', side_effect=forbid_other_source):
                    result, rc = helper._observation_query(self.fixture.repo, 'CHG-003',
                        self.request(area='AREA-1', limit=1), self.fixture.requirement,
                        'P1-CAPTURED', index_factory=factory)
                self.assertEqual(rc, 0, result)
                self.assertEqual([hit['region_id'] for hit in result['hits']], ['ZQUERY'])
                self.assertFalse(result['truncated'])

    def test_typed_area_cannot_assign_a_default_image_to_loading_region_in_a_multistate_query(self):
        path, baseline, _ = self.area_fixture()
        current = baseline['regions'][0]
        current.update(state_id='default', context_id='P1-CAPTURED')
        baseline['regions'].append(dict(copy.deepcopy(current), state_id='loading', region_id='AQUERY'))
        path.write_text('```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n', encoding='utf-8')
        index = json.loads(self.fixture.index.read_bytes())
        loading = copy.deepcopy(index['screenshots'][0])
        loading.update(id='P2', state='loading')
        shared = copy.deepcopy(loading['applies_to'][0])
        shared['relation'] = 'shared_layout'
        loading['applies_to'][0]['context_id'] = 'P2-CAPTURED'
        loading['applies_to'].append(shared)
        loading['path'] = loading['path'].replace('P1.png', 'P2-loading.png')
        (self.fixture.material / loading['path']).write_bytes(self.fixture.image.read_bytes())
        index['screenshots'].append(loading)
        self.fixture.index.write_text(json.dumps(index), encoding='utf-8')
        rc, result = self.invoke(self.request(area='AREA-1', limit=1))
        self.assertEqual(rc, 4, result)
        self.assertEqual(result['reason_code'], 'NEEDS_EVIDENCE')
        self.assertNotIn('hits', result)

    def test_declared_current_context_cannot_read_sources_for_only_another_permissions_image(self):
        path, baseline, _ = self.area_fixture()
        index = json.loads(self.fixture.index.read_bytes())
        other = copy.deepcopy(index['screenshots'][0])
        other.update(id='P2', permissions={'function_id': 'FP01', 'data_id': 'DP02'},
                     path='function/FP01/data/DP02/P2.png')
        other['applies_to'][0].update(context_id='P2-CAPTURED', permissions=other['permissions'])
        image = self.fixture.material / other['path']
        image.parent.mkdir(parents=True)
        image.write_bytes(self.fixture.image.read_bytes())
        index['screenshots'].append(other)
        self.fixture.index.write_text(json.dumps(index), encoding='utf-8')
        (self.fixture.repo / 'frontend').mkdir()
        (self.fixture.repo / 'frontend/private.css').write_bytes(b'other permission source')
        baseline['sources'] = [{'id': 'SRC-1', 'kind': 'layout', 'path': 'frontend/private.css',
            'sha256': hashlib.sha256(b'other permission source').hexdigest(), 'certainty': 'source_backed'}]
        region = baseline['regions'][0]
        current = copy.deepcopy(region)
        current.update(state_id='default', context_id='P1-CAPTURED')
        region.update(state_id='default', context_id='P1-CAPTURED', region_id='AQUERY',
                      screenshot_ids=['P2'], source_ids=['SRC-1'])
        region.pop('observations')
        path.write_text('```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n', encoding='utf-8')
        original = data.fs.read_at
        def forbid_source(handle, name, limit):
            self.assertNotEqual(name, 'private.css', 'declared context cannot grant source IO authority')
            return original(handle, name, limit)
        with patch('aek.adapters.ui_filesystem.read_at', side_effect=forbid_source):
            with self.assertRaisesRegex(helper.UiReferenceError, 'NEEDS_EVIDENCE'):
                helper._observation_query(self.fixture.repo, 'CHG-003', self.request(area='AREA-1'),
                    self.fixture.requirement, 'P1-CAPTURED')
        baseline['regions'].append(current)
        path.write_text('```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n', encoding='utf-8')
        with patch('aek.adapters.ui_filesystem.read_at', side_effect=forbid_source):
            result, rc = helper._observation_query(self.fixture.repo, 'CHG-003', self.request(area='AREA-1'),
                self.fixture.requirement, 'P1-CAPTURED')
        self.assertEqual(rc, 0, result)
        self.assertEqual([hit['region_id'] for hit in result['hits']], ['ZQUERY'])

    def test_area_mapping_and_actual_token_changes_after_payload_loading_remove_old_hits(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        path, baseline, recorded = self.area_fixture()
        token = self.fixture.repo / 'frontend/theme.css'
        token.parent.mkdir()
        token.write_bytes(b'blue')
        baseline['sources'] = [{'id': 'SRC-1', 'kind': 'token', 'path': 'frontend/theme.css',
            'sha256': hashlib.sha256(b'blue').hexdigest(), 'certainty': 'source_backed'}]
        baseline['regions'][0]['source_ids'] = ['SRC-1']
        original = data.fs.read_at
        def save(value):
            path.write_text('## 设计基线\n\n```aek-ui-baseline\n' + json.dumps(value) + '\n```\n',
                            encoding='utf-8')
        for factory in (None, ScopedObservationIndex):
            for mutation in ('mapping', 'token', 'unrelated'):
                with self.subTest(backend=factory, mutation=mutation):
                    save(baseline)
                    token.write_bytes(b'blue')
                    changed = False
                    def read_then_mutate(handle, name, limit):
                        nonlocal changed
                        raw = original(handle, name, limit)
                        if name == recorded['observation_sha256'] + '.json' and not changed:
                            changed = True
                            if mutation == 'mapping':
                                modified = copy.deepcopy(baseline)
                                modified['regions'][0]['region_id'] = 'AQUERY'
                                save(modified)
                            elif mutation == 'token':
                                token.write_bytes(b'red')
                            else:
                                path.write_text(path.read_text(encoding='utf-8') + '\n无关设计说明\n', encoding='utf-8')
                        return raw
                    with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_mutate):
                        result, rc = helper._observation_query(self.fixture.repo, 'CHG-003',
                            self.request(area='AREA-1'), self.fixture.requirement,
                            'P1-CAPTURED', index_factory=factory)
                    self.assertEqual(rc, 0, result)
                    self.assertEqual([hit['region_id'] for hit in result['hits']],
                                     ['ZQUERY'] if mutation == 'unrelated' else [])
                    if mutation != 'unrelated':
                        self.assertEqual(result['status'], 'PARTIAL')
                        self.assertIn({'reason_code': 'SCOPE_CHANGED'}, result['gaps'])

    def test_unsafe_or_ambiguous_area_mapping_cannot_open_observation_body(self):
        path, baseline, recorded = self.area_fixture()
        original = data.fs.read_at
        for change in ('missing', 'duplicate', 'ambiguous', 'unsafe', 'stale_observation'):
            with self.subTest(change=change):
                value = copy.deepcopy(baseline)
                if change == 'missing':
                    text = '## 设计基线\n'
                else:
                    if change == 'ambiguous':
                        value['regions'].append({**copy.deepcopy(value['regions'][0]), 'page_id': 'PAGE-2'})
                    if change == 'unsafe':
                        value['sources'] = [{'id': 'SRC-1', 'kind': 'token', 'path': '../secret.css',
                            'sha256': 'a' * 64, 'certainty': 'source_backed'}]
                        value['regions'][0]['source_ids'] = ['SRC-1']
                    if change == 'stale_observation':
                        value['regions'][0]['observations'][0]['sha256'] = 'a' * 64
                    text = '```aek-ui-baseline\n' + json.dumps(value) + '\n```\n'
                    if change == 'duplicate':
                        text += text
                path.write_text(text, encoding='utf-8')
                def guarded(handle, name, limit):
                    if change != 'stale_observation':
                        self.assertNotEqual(name, recorded['observation_sha256'] + '.json')
                    return original(handle, name, limit)
                with patch('aek.adapters.ui_filesystem.read_at', side_effect=guarded):
                    if change == 'stale_observation':
                        result, _ = helper._observation_query(self.fixture.repo, 'CHG-003',
                            self.request(area='AREA-1'), self.fixture.requirement, 'P1-CAPTURED')
                        self.assertEqual(result['hits'], [])
                        self.assertEqual(result['status'], 'PARTIAL')
                    else:
                        with self.assertRaisesRegex(helper.UiReferenceError, 'NEEDS_EVIDENCE'):
                            helper._observation_query(self.fixture.repo, 'CHG-003', self.request(area='AREA-1'),
                                self.fixture.requirement, 'P1-CAPTURED')

    def test_area_mapping_changed_during_final_token_read_cannot_return_old_region(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        path, baseline, _ = self.area_fixture()
        token = self.fixture.repo / 'frontend/theme.css'
        token.parent.mkdir()
        token.write_bytes(b'blue')
        baseline['sources'] = [{'id': 'SRC-1', 'kind': 'token', 'path': 'frontend/theme.css',
            'sha256': hashlib.sha256(b'blue').hexdigest(), 'certainty': 'source_backed'}]
        baseline['regions'][0]['source_ids'] = ['SRC-1']
        original = data.fs.read_at
        for factory in (None, ScopedObservationIndex):
            with self.subTest(backend=factory):
                path.write_text('```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n', encoding='utf-8')
                token_reads = 0
                def read_then_change_mapping(handle, name, limit):
                    nonlocal token_reads
                    raw = original(handle, name, limit)
                    if name == 'theme.css':
                        token_reads += 1
                        if token_reads == 4:
                            changed = copy.deepcopy(baseline)
                            changed['regions'][0]['region_id'] = 'AQUERY'
                            path.write_text('```aek-ui-baseline\n' + json.dumps(changed) + '\n```\n', encoding='utf-8')
                    return raw
                with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_change_mapping):
                    result, rc = helper._observation_query(self.fixture.repo, 'CHG-003',
                        self.request(area='AREA-1'), self.fixture.requirement,
                        'P1-CAPTURED', index_factory=factory)
                self.assertEqual(token_reads, 4)
                self.assertEqual(rc, 0, result)
                self.assertEqual(result['hits'], [])
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertIn({'reason_code': 'SCOPE_CHANGED'}, result['gaps'])


if __name__ == '__main__':
    unittest.main()
