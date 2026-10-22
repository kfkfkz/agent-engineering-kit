from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aek.adapters import ui_filesystem as fs

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'skills/design-pipeline/scripts/ui-reference.py'
spec = importlib.util.spec_from_file_location('ui_fixtures', Path(__file__).with_name('ui-reference.test.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
helper_spec = importlib.util.spec_from_file_location('observation_helper', HELPER)
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


class ObservationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.requirement = '003-中文需求'
        self.material = self.repo / 'docs/01-需求' / self.requirement / '原始材料/ui-reference'
        value = fixtures.request()
        value.update(change_id='CHG-003', requirement_dir=self.requirement)
        self.assertEqual(self.invoke('scaffold', value)[0], 0)
        self.index = self.material / '截图索引.json'
        row = json.loads(self.index.read_bytes())['screenshots'][0]
        self.image = self.material / row['path']
        self.image.write_bytes(fixtures.png_bytes())
        self.assertEqual(self.invoke('validate')[0], 1)  # unreviewed, not a visual baseline

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, command, payload=None, *, expected=None):
        args = [sys.executable, str(HELPER), command, '--repo-root', str(self.repo)]
        if command != 'scaffold':
            args += ['--change-id', 'CHG-003', '--requirement-dir', self.requirement]
        if expected is not None:
            args += ['--expected-index-sha256', expected]
        result = subprocess.run(args, input=None if payload is None else json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding='utf-8', timeout=15, check=False,
            env={**os.environ, 'PYTHONUTF8': '1'})
        try:
            value = json.loads(result.stdout)
        except ValueError:
            self.fail(result.stdout + result.stderr)
        return result.returncode, value

    def prepare(self, regions=None):
        return self.invoke('observation-prepare', {'schema_version': 2, 'evidence_id': 'P1',
            'context_ref': 'P1-CAPTURED', 'required_regions': regions or ['QUERY']},
            expected=hashlib.sha256(self.index.read_bytes()).hexdigest())

    def record_payload(self, prepared, *, kind='observed'):
        return {'schema_version': 2,
            'prepare_ref': prepared['prepare_ref'], 'prepare_sha256': prepared['prepare_sha256'],
            'candidate': {'schema_version': 2, 'regions': [{
                'region_id': 'QUERY', 'observation_kind': kind, 'layout': '上方单行查询区',
                'controls': [{'kind': 'button', 'label': '查询', 'state': 'enabled'}],
                'style_clues': ['蓝色主按钮']}], 'uncertainties': ['接口未知']},
            'provenance': {'channel': 'agent_visual', 'model': None,
                           'observed_at': '2026-10-16T08:00:00Z'}}

    def record(self, prepared, *, kind='observed'):
        return self.invoke('observation-record', self.record_payload(prepared, kind=kind),
            expected=hashlib.sha256(self.index.read_bytes()).hexdigest())

    def test_prepare_binds_chinese_material_image_snapshot_and_current_prompt_without_approval(self):
        before = self.index.read_bytes()
        rc, prepared = self.prepare()
        self.assertEqual(rc, 0, prepared)
        self.assertEqual(prepared['status'], 'NEEDS_EVIDENCE')
        self.assertEqual(prepared['schema_version'], 2)
        self.assertEqual(prepared['image_sha256'], hashlib.sha256(self.image.read_bytes()).hexdigest())
        snapshot = self.material / prepared['snapshot_ref']
        self.assertEqual(snapshot.read_bytes(), self.image.read_bytes())
        saved = (self.material / prepared['prepare_ref']).read_bytes()
        self.assertEqual(hashlib.sha256(saved).hexdigest(), prepared['prepare_sha256'])
        prompt = ROOT / 'skills/design-pipeline/references/screenshot-observation.md'
        self.assertEqual(json.loads(saved)['extraction_contract']['prompt_sha256'],
                         hashlib.sha256(prompt.read_bytes()).hexdigest())
        self.assertEqual(self.index.read_bytes(), before)
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())
        self.image.write_bytes(fixtures.png_bytes(2, 1))
        self.assertNotEqual(snapshot.read_bytes(), self.image.read_bytes())

    def test_unbound_published_observation_recovers_by_rechecking_without_rewriting_file(self):
        _, prepared = self.prepare()
        before = self.index.read_bytes()
        with patch('aek.adapters.ui_observation_store.fs.replace', side_effect=OSError('synthetic disk failure')):
            pending, rc = helper._observation_command(self.repo, 'CHG-003',
                hashlib.sha256(before).hexdigest(), self.record_payload(prepared),
                'observation-record', self.requirement)
        self.assertEqual(rc, 5, pending)
        self.assertEqual(pending['association_status'], 'PENDING')
        path = self.material / pending['observation_ref']
        raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())
        rc, saved = self.record(prepared)
        self.assertEqual(rc, 0, saved)
        self.assertEqual(saved['association_status'], 'BOUND')
        self.assertEqual(saved['observation_sha256'], pending['observation_sha256'])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.index.read_bytes(), before)

    def test_image_swap_after_publication_leaves_only_unbound_data(self):
        _, prepared = self.prepare()
        original = fs.rename_noreplace

        def rename_then_change_image(src_handle, src, dst_handle, dst):
            original(src_handle, src, dst_handle, dst)
            self.image.write_bytes(fixtures.png_bytes(2, 1))

        with patch('aek.adapters.ui_observation_store.fs.rename_noreplace', side_effect=rename_then_change_image):
            result, rc = helper._observation_command(self.repo, 'CHG-003',
                hashlib.sha256(self.index.read_bytes()).hexdigest(), self.record_payload(prepared),
                'observation-record', self.requirement)
        self.assertEqual(rc, 3, result)
        self.assertEqual(result['association_status'], 'PENDING')
        self.assertTrue((self.material / result['observation_ref']).is_file())
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())

    def test_reuse_rechecks_image_after_reading_cached_text(self):
        _, prepared = self.prepare()
        _, recorded = self.record(prepared)
        original = fs.read_at

        def read_then_change_image(handle, name, limit):
            result = original(handle, name, limit)
            if name == recorded['observation_sha256'] + '.json':
                self.image.write_bytes(fixtures.png_bytes(2, 1))
            return result

        request = {'schema_version': 2, 'evidence_id': 'P1', 'context_ref': 'P1-CAPTURED',
                   'required_regions': ['QUERY']}
        with patch('aek.adapters.ui_observation_store.fs.read_at', side_effect=read_then_change_image):
            with self.assertRaisesRegex(helper.UiReferenceError, 'changed'):
                helper._observation_command(self.repo, 'CHG-003',
                    hashlib.sha256(self.index.read_bytes()).hexdigest(), request,
                    'observation-prepare', self.requirement)

    def test_reuse_rechecks_current_contract_after_reading_cached_text(self):
        _, prepared = self.prepare()
        _, recorded = self.record(prepared)
        original = fs.read_at
        contract = helper._observation_contract()
        active = dict(contract)

        def read_then_change_contract(handle, name, limit):
            result = original(handle, name, limit)
            if name == recorded['observation_sha256'] + '.json':
                active['prompt_sha256'] = 'f' * 64
            return result

        request = {'schema_version': 2, 'evidence_id': 'P1', 'context_ref': 'P1-CAPTURED',
                   'required_regions': ['QUERY']}
        with patch.object(helper, '_observation_contract', side_effect=lambda: dict(active)), \
                patch('aek.adapters.ui_observation_store.fs.read_at', side_effect=read_then_change_contract):
            with self.assertRaisesRegex(helper.UiReferenceError, 'STALE:.*contract'):
                helper._observation_command(self.repo, 'CHG-003',
                    hashlib.sha256(self.index.read_bytes()).hexdigest(), request,
                    'observation-prepare', self.requirement)

    def test_reuse_rechecks_binding_revocation_after_reading_cached_text(self):
        _, prepared = self.prepare()
        _, recorded = self.record(prepared)
        original = fs.read_at
        path = self.material / 'observations/索引.v2.json'

        def read_then_revoke_binding(handle, name, limit):
            result = original(handle, name, limit)
            if name == recorded['observation_sha256'] + '.json':
                binding = json.loads(path.read_bytes())
                binding['bindings'] = []
                path.write_text(json.dumps(binding), encoding='utf-8')
            return result

        request = {'schema_version': 2, 'evidence_id': 'P1', 'context_ref': 'P1-CAPTURED',
                   'required_regions': ['QUERY']}
        with patch('aek.adapters.ui_observation_store.fs.read_at', side_effect=read_then_revoke_binding):
            with self.assertRaisesRegex(helper.UiReferenceError, 'CONFLICT:.*binding'):
                helper._observation_command(self.repo, 'CHG-003',
                    hashlib.sha256(self.index.read_bytes()).hexdigest(), request,
                    'observation-prepare', self.requirement)

    def test_parallel_exact_record_retries_publish_one_binding_and_one_observation(self):
        _, prepared = self.prepare()
        args = [sys.executable, str(HELPER), 'observation-record', '--repo-root', str(self.repo),
            '--change-id', 'CHG-003', '--requirement-dir', self.requirement,
            '--expected-index-sha256', hashlib.sha256(self.index.read_bytes()).hexdigest()]
        raw = json.dumps(self.record_payload(prepared), ensure_ascii=False)
        processes = []
        lock_handle = fs.open_absolute_dir(self.material)
        try:
            # Queue both real writers behind the actual material lock. Feed
            # both requests before releasing; don't serialize communicate(input).
            with fs.exclusive_lock(lock_handle):
                for _ in range(2):
                    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'})
                    processes.append(process)
                for process in processes:
                    process.stdin.write(raw)
                    process.stdin.close()
                    process.stdin = None
            results = [process.communicate(timeout=15) for process in processes]
            values = [json.loads(output) for output, _ in results]
            self.assertEqual([process.returncode for process in processes], [0, 0], results)
            self.assertEqual(values[0], values[1])
            bindings = json.loads((self.material / 'observations/索引.v2.json').read_bytes())
            self.assertEqual(len(bindings['bindings']), 1)
            # Only the observation and explicit v2 projection, no partial files.
            self.assertEqual(len(list((self.material / 'observations').glob('*.json'))), 2)
            self.assertFalse(list((self.material / 'observations').glob('.publish-*')))
        finally:
            fs.close_dir(lock_handle)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

    def test_record_and_current_cache_reuse_do_not_grant_applicability_or_touch_legacy(self):
        before = self.index.read_bytes()
        rc, prepared = self.prepare()
        self.assertEqual(rc, 0, prepared)
        rc, saved = self.record(prepared)
        self.assertEqual(rc, 0, saved)
        self.assertEqual(saved['status'], 'RECORDED')
        self.assertEqual(saved['association_status'], 'BOUND')
        payload = (self.material / saved['observation_ref']).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), saved['observation_sha256'])
        self.assertEqual(json.loads(payload)['regions'][0]['controls'][0]['label'], '查询')
        self.assertEqual(json.loads(payload)['provenance']['prepare_sha256'], prepared['prepare_sha256'])
        binding = self.material / 'observations/索引.v2.json'
        binding_bytes, mtime = binding.read_bytes(), binding.stat().st_mtime_ns
        self.assertEqual(self.record(prepared), (0, saved))
        self.assertEqual(binding.read_bytes(), binding_bytes)
        self.assertEqual(binding.stat().st_mtime_ns, mtime)
        rc, reused = self.prepare()
        self.assertEqual(rc, 0, reused)
        self.assertEqual(reused['status'], 'REUSED')
        self.assertEqual(reused['observation_sha256'], saved['observation_sha256'])
        self.assertNotIn('prepare_ref', reused)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(json.loads(before)['screenshots'][0]['applies_to'][0]['content_review'],
                         {'status': 'unverified'})

    def test_changed_image_cannot_bind_stale_preparation_and_uses_v2_error_code(self):
        _, prepared = self.prepare()
        self.image.write_bytes(fixtures.png_bytes(2, 1))
        rc, stale = self.record(prepared)
        self.assertEqual(rc, 3, stale)
        self.assertEqual(stale['reason_code'], 'STALE')
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())
        self.assertEqual(self.invoke('validate')[0], 1)
        rc, stale = self.record(prepared)
        self.assertEqual(rc, 3, stale)
        self.assertEqual(stale['reason_code'], 'STALE')
        rc, fresh = self.prepare()
        self.assertEqual(rc, 0, fresh)
        self.assertNotEqual(fresh['image_sha256'], prepared['image_sha256'])

    def test_changed_visual_snapshot_cannot_be_recorded_against_unchanged_original(self):
        _, prepared = self.prepare()
        (self.material / prepared['snapshot_ref']).write_bytes(fixtures.png_bytes(3, 1))
        rc, stale = self.record(prepared)
        self.assertEqual(rc, 3, stale)
        self.assertEqual(stale['reason_code'], 'STALE')
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())

    def test_partial_certainty_does_not_become_a_reusable_observation(self):
        _, prepared = self.prepare(['QUERY', 'RESULTS'])
        rc, saved = self.record(prepared, kind='inferred')
        self.assertEqual(rc, 0, saved)
        self.assertEqual(saved['status'], 'PARTIAL')
        self.assertEqual(saved['coverage']['observed_regions'], [])
        self.assertEqual(saved['coverage']['missing_regions'], ['QUERY', 'RESULTS'])
        self.assertEqual(self.prepare()[1]['status'], 'NEEDS_EVIDENCE')

    def test_invalid_version_context_regions_and_injected_fields_do_not_publish(self):
        base = {'schema_version': 2, 'evidence_id': 'P1', 'context_ref': 'P1-CAPTURED',
                'required_regions': ['QUERY']}
        invalid = [dict(base, schema_version=True), dict(base, schema_version=1),
            dict(base, context_ref='OTHER'), dict(base, evidence_id='P99'),
            dict(base, required_regions=[]), dict(base, required_regions=['QUERY', 'QUERY']),
            dict(base, required_regions=['../outside']), dict(base, applicable=True)]
        before = self.index.read_bytes()
        for request in invalid:
            with self.subTest(request=request):
                rc, result = self.invoke('observation-prepare', request,
                    expected=hashlib.sha256(before).hexdigest())
                self.assertEqual(rc, 2, result)
                self.assertEqual(result['reason_code'], 'INVALID_INPUT')
        self.assertFalse((self.material / 'observations').exists())
        self.assertEqual(self.index.read_bytes(), before)

    def test_preparation_reference_is_not_permission_to_read_an_arbitrary_file(self):
        _, prepared = self.prepare()
        request = self.record_payload(prepared)
        request['prepare_ref'] = '../../outside.json'
        rc, result = self.invoke('observation-record', request,
            expected=hashlib.sha256(self.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 2, result)
        self.assertEqual(result['reason_code'], 'INVALID_INPUT')
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())

    def test_self_hashed_malformed_preparation_is_rejected_without_traceback_or_binding(self):
        _, prepared = self.prepare()
        path = self.material / prepared['prepare_ref']
        value = json.loads(path.read_bytes())
        value['evidence_scope'] = {}
        raw = json.dumps(value).encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        malformed = path.with_name(digest + '.json')
        malformed.write_bytes(raw)
        request = self.record_payload({**prepared,
            'prepare_ref': 'observations/preparations/' + digest + '.json', 'prepare_sha256': digest})
        rc, result = self.invoke('observation-record', request,
            expected=hashlib.sha256(self.index.read_bytes()).hexdigest())
        self.assertEqual(rc, 2, result)
        self.assertEqual(result['reason_code'], 'INVALID_INPUT')
        self.assertFalse((self.material / 'observations/索引.v2.json').exists())

    def test_copying_materials_to_another_repository_does_not_rebind_preparation(self):
        import shutil
        _, prepared = self.prepare()
        previous = self.repo
        other = Path(self.temp.name) / 'other'
        shutil.copytree(previous, other)
        self.repo = other
        try:
            rc, result = self.record(prepared)
            self.assertEqual(rc, 3, result)
            self.assertEqual(result['reason_code'], 'STALE')
            self.assertFalse((other / self.material.relative_to(previous) / 'observations/索引.v2.json').exists())
        finally:
            self.repo = previous

    def test_material_lock_timeout_uses_new_code_and_keeps_old_index_unchanged(self):
        before = self.index.read_bytes()
        handle = fs.open_absolute_dir(self.material)
        try:
            with fs.exclusive_lock(handle):
                rc, result = self.prepare()
            self.assertEqual(rc, 5, result)
            self.assertEqual(result['reason_code'], 'TIMEOUT')
            self.assertEqual(self.index.read_bytes(), before)
            self.assertFalse((self.material / 'observations').exists())
        finally:
            fs.close_dir(handle)

    def test_tampered_observation_is_not_delivered_as_a_cache_hit(self):
        _, prepared = self.prepare()
        _, saved = self.record(prepared)
        path = self.material / saved['observation_ref']
        value = json.loads(path.read_bytes())
        value['regions'][0]['layout'] = 'tampered but structurally valid'
        path.write_text(json.dumps(value), encoding='utf-8')
        rc, result = self.prepare()
        self.assertEqual(rc, 3, result)
        self.assertEqual(result['reason_code'], 'STALE')
        self.assertNotIn('observation_ref', result)

    def test_required_region_growth_and_prompt_changes_invalidate_only_observation_reuse(self):
        _, prepared = self.prepare()
        self.assertEqual(self.record(prepared)[0], 0)
        self.assertEqual(self.prepare(['QUERY', 'RESULTS'])[1]['status'], 'NEEDS_EVIDENCE')
        self.assertEqual(self.prepare()[1]['status'], 'REUSED')
        before = self.index.read_bytes()
        # A separate installed kit snapshot tests a real resource-version change
        # without ever editing the live kit or its prompt.
        kit = Path(self.temp.name) / 'installed'
        import shutil
        shutil.copytree(ROOT / 'aek', kit / '.repo-memory-kit/lib/aek')
        surface = kit / '.agents/skills/design-pipeline'
        (surface / 'scripts').mkdir(parents=True)
        (surface / 'references').mkdir()
        shutil.copy2(HELPER, surface / 'scripts/ui-reference.py')
        prompt = ROOT / 'skills/design-pipeline/references/screenshot-observation.md'
        (surface / 'references/screenshot-observation.md').write_bytes(prompt.read_bytes() + b'\nchanged contract\n')
        result = subprocess.run([sys.executable, str(surface / 'scripts/ui-reference.py'),
            'observation-prepare', '--repo-root', str(self.repo), '--change-id', 'CHG-003',
            '--requirement-dir', self.requirement, '--expected-index-sha256', hashlib.sha256(before).hexdigest()],
            input=json.dumps({'schema_version': 2, 'evidence_id': 'P1',
                'context_ref': 'P1-CAPTURED', 'required_regions': ['QUERY']}),
            text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        changed = json.loads(result.stdout)
        self.assertEqual(changed['status'], 'NEEDS_EVIDENCE')
        self.assertNotEqual(changed['contract_digest'], prepared['contract_digest'])
        self.assertEqual(self.index.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
