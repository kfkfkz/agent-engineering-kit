"""Public prepare preview regressions; synthetic fixtures are not approvals."""
from __future__ import annotations

import hashlib
import importlib.util
import copy
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preview_v2_fixture', ROOT / 'tests/ui-prototype-v2.test.py')
DATA = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DATA)


def png(color=2, depth=8, scanline_filter=0):
    def chunk(kind, raw):
        return struct.pack('>I', len(raw)) + kind + raw + struct.pack('>I', zlib.crc32(kind + raw))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, depth, color, 0, 0, 0))
        + (chunk(b'PLTE', b'\xff\0\0') if color == 3 else b'')
        + chunk(b'IDAT', zlib.compress((bytes([scanline_filter]) + b'\0' * ((64 * channels * depth + 7) // 8)) * 64))
        + chunk(b'IEND', b''))


class PrototypePreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DATA.PrototypeV2Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.raw = png()
        self.set_image(self.raw)
        self.fixture.save()
        self.target = ('PAGE-1', 'AREA-1', 'default', self.f.context_id)

    def set_image(self, raw):
        row = self.f.index['screenshots'][0]
        (self.f.material / row['path']).write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        row['validation'].update(content_sha256=digest, bytes=len(raw))
        for context in row['applies_to']:
            context['content_review']['reviewed_sha256'] = digest
            context['freshness']['confirmed_sha256'] = digest

    def call(self, *flags):
        return subprocess.run([sys.executable, str(ROOT / 'doc-gate'), 'prototype-prepare',
            str(self.f.req), '--target', '/'.join(self.target), *flags], cwd=ROOT,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
            capture_output=True, timeout=30)

    def snapshot(self):
        return {path.relative_to(self.f.repo).as_posix(): path.read_bytes()
                for path in self.f.repo.rglob('*') if path.is_file()}

    def inprocess(self, **options):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            rc = DATA.data.GATE['cmd_prototype_prepare'](self.f.req, None, None, None, None,
                False, targets=[self.target], preview_html=True, **options)
        return rc, output.getvalue(), error.getvalue()

    def rendered_profile(self):
        """Real original host/source protocol; only native browser creation is replaced."""
        from aek.adapters.ui_render_host import LinuxRenderHostProfile, PROFILE_ID
        from aek.core.artifact.ui_preview import RenderConditions
        spec = importlib.util.spec_from_file_location('preview_real_render_host_fixture',
            ROOT / 'tests/ui-render-host.test.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = module.Fixture(Path(temporary.name))
        repo = Path(fixture.authority.repository_ref)
        shutil.copytree(self.f.repo / 'docs', repo / 'docs')
        self.f.repo = repo
        self.f.req = repo / 'docs/03-SDD/CHG-001'
        self.f.material = repo / 'docs/01-需求/CHG-001/原始材料/ui-reference'
        gate = DATA.data.GATE
        fixed_target = self.target
        def prepare(target):
            self.assertEqual(target, fixed_target)
            return gate['_prepare_prototype_selection'](self.f.req, [fixed_target])
        def read_html(relative):
            handle = gate['_open_dir_nofollow'](self.f.req)
            try:
                return gate['_read_beneath'](handle, relative, 5 * 1024 * 1024)
            finally:
                gate['_ui_fs'].close_dir(handle)
        profile = LinuxRenderHostProfile(fixture.authority, fixture.port, runtime=fixture.runtime,
            target=self.target, conditions=RenderConditions(64, 64), prepare_selection=prepare,
            read_html=read_html, required_components=module.REQUIRED,
            adoption_proposals=(fixture.proposal,), adoption_source_refs=('advisory', 'compatibility'),
            clock=fixture.authority._clock)
        request = {'profile_id': PROFILE_ID, 'targets': ['/'.join(self.target)]}
        prepared = profile.prepare(request)
        peer, boundary = module.native_peer()
        with boundary, patch.object(sys, 'stdin', module.host_fixture.Terminal()), \
                redirect_stdout(module.host_fixture.Terminal()), patch('builtins.input',
                return_value='RUN ' + prepared.binding['plan_digest'][:12]):
            executed = fixture.service(profile).execute(request, reviewer='synthetic-protocol-test')
        self.assertEqual(executed['status'], 'COMPLETED', executed)
        self.assertEqual(peer.opens, 1)
        fixture.completed_source_ref = executed['source_ref']
        return module, fixture, profile, peer

    @unittest.skipUnless(sys.platform == 'linux', 'first trusted render profile is Linux only')
    def test_control_side_profile_publishes_real_current_source_png_without_grant_or_rerun(self):
        module, fixture, profile, peer = self.rendered_profile()
        before = {path.relative_to(fixture.base): path.read_bytes()
                  for path in fixture.base.rglob('*') if path.is_file()}
        with patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('must not render')), \
                patch.object(fixture.host, 'approve_execution', side_effect=AssertionError('must not approve')), \
                patch.object(fixture.port, 'register', side_effect=AssertionError('must not register')):
            rc, output, error = self.inprocess(render_profiles={self.target: profile})
        self.assertEqual(rc, 0, error)
        self.assertIn('RENDERED', output)
        self.assertIn(hashlib.sha256(module.preview.png()).hexdigest(), output)
        self.assertIn('可比性：未知/未核实', output)
        self.assertNotIn('ACCEPTED', output)
        self.assertEqual(output.count('<img loading="lazy"'), 2)
        self.assertEqual(peer.opens, 1)
        self.assertEqual(before, {path.relative_to(fixture.base): path.read_bytes()
                  for path in fixture.base.rglob('*') if path.is_file()})

    @unittest.skipUnless(sys.platform == 'linux', 'first trusted render profile is Linux only')
    def test_fixed_profile_cannot_be_assigned_to_a_different_selected_context(self):
        _module, fixture, profile, _peer = self.rendered_profile()
        contexts = self.fixture.add_context()
        self.fixture.save()
        self.target = contexts[1]
        before = {path.relative_to(fixture.base): path.read_bytes()
                  for path in fixture.base.rglob('*') if path.is_file()}
        rc, output, error = self.inprocess(render_profiles={self.target: profile})
        self.assertEqual(rc, 3)
        self.assertEqual(output, '')
        self.assertTrue(error)
        self.assertEqual(before, {path.relative_to(fixture.base): path.read_bytes()
                  for path in fixture.base.rglob('*') if path.is_file()})

    def test_render_data_and_duck_profiles_cannot_inject_trusted_images(self):
        from aek.core.artifact.ui_preview import RenderResult
        result = RenderResult('RENDERED', 'STATIC_CAPTURE_COMPLETE')
        class FakeProfile:
            def read_result(self, **_kwargs):
                raise AssertionError('a duck profile must not be called')
        namespace = DATA.data.GATE['_prepare_prototype_preview'].__globals__
        for profiles in (result, {self.target: result}, {self.target: FakeProfile()},
                         {'https://example.invalid/result.png': result},
                         {self.target: {'status': 'RENDERED', 'png': 'untrusted'}}):
            with self.subTest(profiles=type(profiles)), patch.dict(namespace, {
                    '_prototype_preparation_maps': lambda *_args, **_kwargs: self.fail('must reject before source IO')}):
                rc, output, error = self.inprocess(render_profiles=profiles)
            self.assertEqual(rc, 3)
            self.assertEqual(output, '')
            self.assertTrue(error)

    @unittest.skipUnless(sys.platform == 'linux', 'first trusted render profile is Linux only')
    def test_revoked_current_source_is_unavailable_without_any_execution_or_registration(self):
        _module, fixture, profile, peer = self.rendered_profile()
        observed = fixture.port.lookup(fixture.completed_source_ref)
        event = fixture.authority.read_provenance(observed.provenance_ref)['event']
        (fixture.base / 'host' / ('revoked-' + event['event_id'] + '.json')).write_bytes(b'')
        before = {path.relative_to(fixture.base): path.read_bytes()
                  for path in fixture.base.rglob('*') if path.is_file()}
        with patch.object(fixture.host, 'approve_execution', side_effect=AssertionError('no approval')), \
                patch.object(fixture.port, 'register', side_effect=AssertionError('no registration')), \
                patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('no renderer')):
            rc, output, error = self.inprocess(render_profiles={self.target: profile})
        self.assertEqual(rc, 0, error)
        self.assertIn('UNAVAILABLE', output)
        self.assertNotIn('渲染：RENDERED', output)
        self.assertEqual(output.count('<img loading="lazy"'), 1)
        self.assertEqual(peer.opens, 1)
        self.assertEqual(before, {path.relative_to(fixture.base): path.read_bytes()
                  for path in fixture.base.rglob('*') if path.is_file()})

    @unittest.skipUnless(sys.platform == 'linux', 'first trusted render profile is Linux only')
    def test_source_revoked_during_material_recheck_discards_the_previously_read_png(self):
        _module, fixture, profile, peer = self.rendered_profile()
        observed = fixture.port.lookup(fixture.completed_source_ref)
        event = fixture.authority.read_provenance(observed.provenance_ref)['event']
        marker = fixture.base / 'host' / ('revoked-' + event['event_id'] + '.json')
        actual_result = type(profile).read_result
        reads, changed = [], []
        def read_result(current, **options):
            value = actual_result(current, **options)
            reads.append(value)
            return value
        namespace = DATA.data.GATE['_prepare_prototype_preview'].__globals__
        actual_read = namespace['_read_beneath']
        def read_then_revoke(root, relative, limit):
            raw = actual_read(root, relative, limit)
            if len(reads) == 1 and relative == 'UI设计.md' and not changed:
                marker.write_bytes(b'')
                changed.append(True)
            return raw
        with patch.object(type(profile), 'read_result', read_result), patch.dict(namespace, {
                '_read_beneath': read_then_revoke}):
            rc, output, error = self.inprocess(render_profiles={self.target: profile})
        self.assertEqual(changed, [True])
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0].status, 'RENDERED')
        self.assertIsNone(reads[1])
        self.assertEqual(rc, 3)
        self.assertEqual(output, '')
        self.assertTrue(error)
        self.assertEqual(peer.opens, 1)

    @unittest.skipUnless(sys.platform == 'linux', 'first trusted render profile is Linux only')
    def test_final_material_io_rechecks_profile_scope_author_identity_and_descriptor_without_new_source_io(self):
        from aek.core.artifact.ui_preview import RenderConditions
        for mutation in ('scope', 'author', 'identity', 'conditions'):
            _module, fixture, profile, peer = self.rendered_profile()
            actual_result = type(profile).read_result
            reads, changed = [], []
            def read_result(current, **options):
                value = actual_result(current, **options)
                reads.append(value)
                return value
            namespace = DATA.data.GATE['_prepare_prototype_preview'].__globals__
            actual_read = namespace['_read_beneath']
            def read_then_change(root, relative, limit):
                raw = actual_read(root, relative, limit)
                if len(reads) == 2 and relative == 'UI设计.md' and not changed:
                    if mutation == 'scope':
                        fixture.authority.scope_ref = 'e' * 64
                    elif mutation == 'author':
                        fixture.authority.author = 'another-author'
                    elif mutation == 'identity':
                        fixture.authority.current_identity = None
                    else:
                        profile._conditions = RenderConditions(128, 64)
                    changed.append(True)
                return raw
            with self.subTest(mutation=mutation), patch.object(type(profile), 'read_result', read_result), \
                    patch.dict(namespace, {'_read_beneath': read_then_change}):
                rc, output, error = self.inprocess(render_profiles={self.target: profile})
            self.assertEqual(changed, [True])
            self.assertEqual(len(reads), 2, 'final control fence must not reread source/proof')
            self.assertTrue(all(value is not None and value.status == 'RENDERED' for value in reads))
            self.assertEqual(rc, 3)
            self.assertEqual(output, '')
            self.assertTrue(error)
            self.assertEqual(peer.opens, 1)

    @unittest.skipUnless(sys.platform == 'linux', 'first trusted render profile is Linux only')
    def test_final_gate_rejects_requirement_changes_after_the_second_source_observation(self):
        _module, _fixture, profile, peer = self.rendered_profile()
        actual_result = type(profile).read_result
        reads = []
        def read_then_change(current, **options):
            value = actual_result(current, **options)
            reads.append(value)
            if len(reads) == 2:
                (self.f.req / '需求分析.md').write_text('- [x] A1：后续需求已变化\n', encoding='utf-8')
            return value
        with patch.object(type(profile), 'read_result', read_then_change):
            rc, output, error = self.inprocess(render_profiles={self.target: profile})
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(value is not None for value in reads))
        self.assertEqual(rc, 3)
        self.assertEqual(output, '')
        self.assertTrue(error)
        self.assertEqual(peer.opens, 1)

    def test_public_preview_is_complete_utf8_html_readonly_and_binds_actual_selection(self):
        original = self.call('--json')
        self.assertEqual(original.returncode, 0, original.stderr)
        prepared = json.loads(original.stdout.decode('utf-8'))
        before = self.snapshot()
        result = self.call('--preview-html')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.decode('utf-8')
        self.assertTrue(text.startswith('<!doctype html>'))
        self.assertTrue(text.endswith('</html>'))
        self.assertEqual(result.stderr, b'')
        self.assertIn(prepared['prepare_sha256'], text)
        self.assertIn(hashlib.sha256(self.raw).hexdigest(), text)
        self.assertIn('data:image/png;base64,', text)
        self.assertIn('UNAVAILABLE', text)
        self.assertNotIn('ACCEPTED', text)
        self.assertNotIn('<iframe', text)
        self.assertNotIn('<script', text)
        self.assertEqual(self.snapshot(), before)

    def test_missing_requirement_directory_has_only_bounded_stderr(self):
        self.f.req = self.f.req / '不存在的目录'
        result = self.call('--preview-html')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, b'')
        self.assertTrue(result.stderr)
        self.assertLess(len(result.stderr), 1024)

    def test_legacy_formats_remain_identical_and_preview_json_conflict_is_readonly(self):
        before = self.snapshot()
        prepared = json.loads(self.call('--json').stdout.decode('utf-8'))
        self.assertEqual(set(prepared), {'schema_version', 'status', 'per_item', 'prepare_sha256'})
        body = {key: prepared[key] for key in ('schema_version', 'status', 'per_item')}
        self.assertEqual(prepared['prepare_sha256'], hashlib.sha256(json.dumps(body,
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest())
        human = self.call()
        self.assertEqual(human.returncode, 0)
        self.assertEqual(human.stdout.decode('utf-8'),
            '✓ 原型已准备（尚未确认）：1 项\n准备 SHA-256: ' + prepared['prepare_sha256'] + '\n')
        for flags in (('--json', '--preview-html'), ('--preview-html', '--json')):
            result = self.call(*flags)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, b'')
            self.assertTrue(result.stderr)
            self.assertLess(len(result.stderr), 1024)
        self.assertEqual(self.snapshot(), before)

    def test_reference_full_decoder_keeps_palette_grayscale_and_16bit_support(self):
        for color, depth in ((3, 1), (3, 8), (0, 1), (0, 16), (2, 16), (4, 16), (6, 16)):
            with self.subTest(color=color, depth=depth):
                raw = png(color, depth)
                self.set_image(raw)
                self.fixture.save()
                result = self.call('--preview-html')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(hashlib.sha256(raw).hexdigest().encode(), result.stdout)
                self.assertIn(b'data:image/png;base64,', result.stdout)

    def test_decoder_failure_never_publishes_partial_html_or_changes_project_files(self):
        damaged_crc = self.raw[:-1] + bytes([self.raw[-1] ^ 1])
        for raw in (self.raw[:33], damaged_crc, self.raw + b'<script>untrusted</script>',
                    png(scanline_filter=5)):
            with self.subTest(bytes=len(raw)):
                # A self-reported valid index and matching hash are insufficient:
                # the shared decoder must actually inspect the selected bytes.
                self.set_image(raw)
                self.fixture.save()
                self.assertEqual(self.call('--json').returncode, 0)
                before = self.snapshot()
                result = self.call('--preview-html')
                self.assertEqual(result.returncode, 3)
                self.assertEqual(result.stdout, b'')
                self.assertTrue(result.stderr)
                self.assertLess(len(result.stderr), 1024)
                self.assertEqual(self.snapshot(), before)

    def test_chinese_directory_and_machine_change_id_keep_the_same_preview_identity(self):
        name = '019-中文集中预览'
        requirement = self.f.repo / 'docs/01-需求/CHG-001'
        requirement.rename(requirement.with_name(name))
        self.f.req.rename(self.f.req.with_name(name))
        self.f.req = self.f.req.with_name(name)
        self.f.material = requirement.with_name(name) / '原始材料/ui-reference'
        self.f.index['requirement_dir'] = name
        self.f.index['change_id'] = 'CHG-019'
        self.fixture.items[0]['approved_root'] = 'docs/03-SDD/' + name
        self.fixture.save()
        original = self.call('--json')
        self.assertEqual(original.returncode, 0, original.stdout + original.stderr)
        before = self.snapshot()
        result = self.call('--preview-html')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(hashlib.sha256(self.raw).hexdigest().encode(), result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_same_page_area_keeps_two_states_and_two_permission_contexts_separate(self):
        contexts = self.fixture.add_context()
        second = copy.deepcopy(self.f.index['screenshots'][0])
        second.update(id='P2', state='loading', path='function/FP01/data/DP01/P2-loading.png')
        second['prototype_refs'] = []
        self.f.index['screenshots'].append(second)
        (self.f.material / second['path']).write_bytes(self.raw)
        loading_items = []
        for item in self.fixture.items:
            loading_items.append({**copy.deepcopy(item), 'state_id': 'loading', 'depends_on': ['P2'],
                'evidence_contexts': [{'evidence_id': 'P2', 'context_id': item['context_id']}]})
        self.fixture.items.extend(loading_items)
        self.fixture.save()
        second['prototype_refs'] = [copy.deepcopy(ref) for ref in
            self.f.index['screenshots'][0]['prototype_refs'] if ref['state_id'] == 'loading']
        (self.f.material / '截图索引.json').write_text(json.dumps(self.f.index, ensure_ascii=False), encoding='utf-8')
        targets = contexts + [('PAGE-1', 'AREA-1', 'loading', target[3]) for target in contexts]
        arguments = [sys.executable, str(ROOT / 'doc-gate'), 'prototype-prepare', str(self.f.req), '--preview-html']
        for target in targets:
            arguments += ['--target', '/'.join(target)]
        before = self.snapshot()
        result = subprocess.run(arguments, capture_output=True, timeout=30,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.decode('utf-8')
        for target in targets:
            self.assertEqual(text.count('<h2>' + ' / '.join(target) + '</h2>'), 1)
        self.assertIn('DP01', text)
        self.assertIn('DP02', text)
        self.assertEqual(text.count('<img loading="lazy"'), 4)
        self.assertEqual(self.snapshot(), before)

    def test_image_requirement_and_context_changes_after_image_read_discard_the_whole_report(self):
        initial = self.snapshot()
        namespace = DATA.data.GATE['_prepare_prototype_preview'].__globals__
        original_read = namespace['_read_beneath']
        def change_image():
            (self.f.material / self.f.index['screenshots'][0]['path']).write_bytes(png(0, 16))
        def change_requirement():
            (self.f.req / '需求分析.md').write_text('- [x] A1：新的业务含义\n', encoding='utf-8')
        def change_context():
            value = json.loads((self.f.material / '截图索引.json').read_text(encoding='utf-8'))
            value['screenshots'][0]['applies_to'][0]['freshness']['status'] = 'needs_confirmation'
            (self.f.material / '截图索引.json').write_text(json.dumps(value), encoding='utf-8')
        for mutation in (change_image, change_requirement, change_context):
            for path, raw in initial.items():
                (self.f.repo / path).write_bytes(raw)
            calls = []
            def read_then_change(root, relative, limit):
                raw = original_read(root, relative, limit)
                if relative.endswith('/P1.png'):
                    calls.append(relative)
                    mutation()
                return raw
            with self.subTest(mutation=mutation.__name__), patch.dict(namespace, {'_read_beneath': read_then_change}):
                rc, output, error = self.inprocess()
            self.assertEqual(len(calls), 1)
            self.assertEqual(rc, 3)
            self.assertEqual(output, '')
            self.assertTrue(error)
            self.assertLess(len(error.encode('utf-8')), 1024)

    def test_unselected_image_body_is_not_read_or_decoded(self):
        second = copy.deepcopy(self.f.index['screenshots'][0])
        bad = b'not a PNG: deliberately unselected'
        digest = hashlib.sha256(bad).hexdigest()
        second.update(id='P2', state='loading', path='function/FP01/data/DP01/P2-loading.png')
        second['validation'].update(content_sha256=digest, bytes=len(bad))
        for context in second['applies_to']:
            context['content_review']['reviewed_sha256'] = digest
            context['freshness']['confirmed_sha256'] = digest
        self.f.index['screenshots'].append(second)
        (self.f.material / second['path']).write_bytes(bad)
        self.fixture.items.append({**copy.deepcopy(self.fixture.items[0]), 'state_id': 'loading',
            'depends_on': ['P2'], 'evidence_contexts': [{'evidence_id': 'P2', 'context_id': self.f.context_id}]})
        self.fixture.save()
        second['prototype_refs'] = [copy.deepcopy(ref) for ref in
            self.f.index['screenshots'][0]['prototype_refs'] if ref['state_id'] == 'loading']
        (self.f.material / '截图索引.json').write_text(json.dumps(self.f.index), encoding='utf-8')
        namespace = DATA.data.GATE['_prepare_prototype_preview'].__globals__
        original = namespace['_read_beneath']
        reads = []
        def record(root, relative, limit):
            if relative.endswith('.png'):
                reads.append(relative)
            return original(root, relative, limit)
        before = self.snapshot()
        with patch.dict(namespace, {'_read_beneath': record}):
            rc, output, error = self.inprocess()
        self.assertEqual(rc, 0, error)
        self.assertIn('UNAVAILABLE', output)
        self.assertEqual(reads, ['function/FP01/data/DP01/P1.png'])
        self.assertNotIn('P2-loading.png', output)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
