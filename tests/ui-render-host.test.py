#!/usr/bin/env python3
"""Fixed render-host protocol, not real browser/package adoption or approval.

Runtime fingerprints read actual temporary bytes. The original private HMAC
host, authority and source port are used; fixture source events and terminal
choices are protocol inputs only. Browser execution is replaced explicitly.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import redirect_stdout
import base64
import hashlib
import importlib.util
import random
import struct
import zlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.test.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


host_fixture = load('security-host')
workflow = load('security-workflow')
preview = load('ui-preview-report')
renderer_fixture = load('ui-renderer')
TARGET = ('PAGE-1', 'AREA-1', 'default', 'CTX-1')
REQUIRED = ('subject.scope_digest', 'artifact.design_digest', 'policy.content_digest',
            'context.source_digest', 'evidence.verification_digest')


class Fixture:
    def __init__(self, base):
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort, source_binding
        from aek.adapters.ui_linux_renderer import LinuxRuntime
        from aek.core.security.authority import digest, runtime_binding
        self.base = base
        self.read_hook = None
        self.host, self.authority, _, _ = host_fixture.LocalHostTests().context(base)
        (base / 'sources').mkdir(mode=0o700)
        self.port = BuiltinSourceEvidencePort(self.authority, root=base / 'sources')
        browser = base / 'browser'
        browser.mkdir()
        binaries = {browser / 'chrome-headless-shell': b'protocol browser fixture',
                    base / 'bwrap': b'protocol sandbox fixture', base / 'prlimit': b'protocol limits fixture'}
        for path, raw in binaries.items():
            path.write_bytes(raw)
            path.chmod(0o700)
        self.runtime = LinuxRuntime(str(browser), str(base / 'bwrap'), str(base / 'prlimit'),
            (('chrome-headless-shell', hashlib.sha256(binaries[browser / 'chrome-headless-shell']).hexdigest()),),
            hashlib.sha256(binaries[base / 'bwrap']).hexdigest(), hashlib.sha256(binaries[base / 'prlimit']).hexdigest(),
            ('/usr/lib', '/usr/lib64', '/usr/share/fonts', '/usr/share/fontconfig', '/etc/fonts'))
        self.html = '<main>真实临时文件，协议夹具</main>'.encode('utf-8')
        self.path = Path(self.authority.repository_ref) / 'prototype.html'
        self.path.write_bytes(self.html)
        self.selection = preview.selection(TARGET)
        self.selection['per_item'][0].update(prototype_path='prototype.html',
            prototype_sha256=hashlib.sha256(self.html).hexdigest(), scope_digest=self.authority.scope_ref)
        self.selection['prepare_sha256'] = digest({key: self.selection[key]
            for key in ('schema_version', 'status', 'per_item')})
        self.proposal = {'id': 'DEP-PROTOCOL', 'ecosystem': 'fixture', 'package': 'protocol-runtime',
            'version': '1.0.0', 'purpose': 'protocol only, not adoption of a public runtime',
            'scope': 'build', 'design_refs': ['D13'], 'evidence_refs': ['advisory', 'compatibility']}
        for ref, kind, facts in (('advisory', 'advisory', {'advisories': []}),
                                 ('compatibility', 'compatibility', {'compatible': True})):
            metadata = workflow.source_metadata(self.authority, ref, kind)
            metadata['producer_ref'] = 'protocol-adoption-producer'
            metadata['required_components'] = list(REQUIRED)
            facts = {**{key: self.proposal[key] for key in ('ecosystem', 'package', 'version')}, **facts}
            plan = {'protocol_source_fixture': ref}
            binding = runtime_binding(plan_digest=digest(plan), repository_ref=self.authority.repository_ref,
                scope_ref=self.authority.scope_ref, identity_digest=self.authority.current_identity.envelope_digest,
                tool_id='protocol-adoption-producer', executable_digest='a' * 64, action='protocol-fixture',
                network_mode='offline', write_scope=[], timeout_seconds=1, max_output_bytes=4096, max_items=32)
            current_scope = self.authority.scope_ref
            def current():
                return self.authority.scope_ref == current_scope
            with patch.object(sys, 'stdin', host_fixture.Terminal()), redirect_stdout(host_fixture.Terminal()), \
                    patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]):
                grant = self.host.approve_execution(self.authority, plan=plan, binding=binding,
                    reviewer='protocol-test', revalidate=current)
            attempt = self.host.begin_attempt(self.authority, plan=plan, binding=binding, grant=grant)
            handle = self.host.complete_attempt(self.authority, attempt, metadata=metadata,
                                                facts=facts, revalidate=current)
            self.port.register(metadata, facts, handle)

    def profile(self, target=TARGET, conditions=None):
        from aek.adapters.ui_render_host import LinuxRenderHostProfile
        from aek.core.artifact.ui_preview import RenderConditions
        from aek.adapters import ui_filesystem as fs
        def read_html(relative):
            root = fs.open_absolute_dir(Path(self.authority.repository_ref))
            try:
                raw = fs.read_at(root, relative, 5 * 1024 * 1024)
                if self.read_hook is not None:
                    self.read_hook()
                return raw
            finally:
                fs.close_dir(root)
        return LinuxRenderHostProfile(self.authority, self.port, runtime=self.runtime,
            target=target, conditions=conditions or RenderConditions(64, 64),
            prepare_selection=lambda selected: self.selection if selected == target else None,
            read_html=read_html, required_components=REQUIRED, adoption_proposals=(self.proposal,),
            adoption_source_refs=('advisory', 'compatibility'), clock=self.authority._clock)

    def service(self, profile):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.application.local_host import LocalToolHost
        return LocalToolHost(host_source=self.host, authority=self.authority, source_port=self.port,
                             providers={PROFILE_ID: profile})

    def restart(self):
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort
        from aek.adapters.security_host import TerminalHostEventSource
        old = self.authority
        self.host = TerminalHostEventSource(state_root=self.base / 'host',
            key_file=self.base / 'private/host.key', clock=old._clock)
        self.authority = BuiltinWorkflowAuthority(repository_ref=old.repository_ref, task_id=old.task_id,
            scope_ref=old.scope_ref, current_identity=old.current_identity, author=old.author,
            host_source=self.host, provenance_root=self.base / 'provenance',
            expires_at=old.expires_at, clock=old._clock)
        self.port = BuiltinSourceEvidencePort(self.authority, root=self.base / 'sources')

    def run_protocol(self, profile, **options):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.adapters.ui_linux_renderer import _browser_inventory
        data = {'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]}
        prepared = profile.prepare(data)
        peer, boundary = native_peer(**options)
        with patch('aek.adapters.ui_linux_renderer._browser_inventory', wraps=_browser_inventory) as measured, \
                boundary, patch.object(sys, 'stdin', host_fixture.Terminal()), \
                redirect_stdout(host_fixture.Terminal()), patch('builtins.input',
                return_value='RUN ' + prepared.binding['plan_digest'][:12]):
            result = self.service(profile).execute(data, reviewer='protocol-test')
        self.measurements = {'browser_bytes': sum(len((Path(self.runtime.browser_root) / name).read_bytes())
            for name, _ in self.runtime.browser_files), 'execute_inventory_measurements': measured.call_count}
        return result, peer


def native_peer(**options):
    """Replace only native browser creation, retaining real pipes/CDP/cleanup."""
    peer = renderer_fixture.RendererTests().provider(image=options.pop('image', preview.png()), **options)
    class NativePeer:
        def __init__(self, *args, **kwargs):
            self.context = None
        def __enter__(self):
            self.context = peer.open(b'protocol boundary', {}, peer.plan)
            return self.context.__enter__().channel
        def __exit__(self, *args):
            return self.context.__exit__(*args)
    return peer, patch('aek.adapters.ui_linux_renderer._NativeProcess', NativePeer)


@unittest.skipUnless(sys.platform == 'linux', 'first fixed profile is Linux only')
class RenderHostTests(unittest.TestCase):
    def test_preparation_uses_the_original_native_plan_and_does_not_grant_or_run(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.application.local_host import LocalToolHost
        from aek.core.security.authority import digest
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            host = LocalToolHost(host_source=f.host, authority=f.authority, source_port=f.port,
                                 providers={PROFILE_ID: profile})
            before = set(f.authority._handles)
            prepared = host.prepare({'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]})
            self.assertEqual(prepared['status'], 'PREPARED')
            self.assertNotIn('plan_digest', prepared['plan'])
            self.assertEqual(digest(prepared['plan']), prepared['binding']['plan_digest'])
            self.assertEqual(prepared['binding']['tool_id'], 'aek-static-render-linux-v2')
            self.assertEqual(prepared['binding']['max_output_bytes'], 1_000_000)
            self.assertEqual(prepared['binding']['write_scope'], [])
            self.assertEqual(set(f.authority._handles), before)

    def test_original_host_grant_and_native_provider_persist_the_actual_png_in_original_source(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            data = {'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]}
            prepared = profile.prepare(data)
            peer, boundary = native_peer()
            with boundary, patch.object(sys, 'stdin', host_fixture.Terminal()), \
                    redirect_stdout(host_fixture.Terminal()), patch('builtins.input',
                    return_value='RUN ' + prepared.binding['plan_digest'][:12]):
                result = f.service(profile).execute(data, reviewer='protocol-test')
            self.assertEqual(result['status'], 'COMPLETED', result)
            self.assertEqual(peer.opens, 1)
            self.assertTrue(peer.cleaned)
            observation = f.port.lookup(result['source_ref'])
            self.assertEqual(observation.metadata['status'], 'VERIFIED')
            self.assertEqual(observation.metadata['producer_ref'], 'aek-static-render-linux-v2')
            rendered = observation.facts['render_result']
            self.assertEqual(base64.b64decode(rendered['png_base64'], validate=True), preview.png())
            self.assertEqual(rendered['output_sha256'], hashlib.sha256(preview.png()).hexdigest())
            self.assertFalse(rendered['comparable'])
            self.assertEqual(observation.facts['target'], list(TARGET))

    def test_restart_can_read_the_current_actual_png_without_a_new_grant_or_browser(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            data = {'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]}
            prepared = profile.prepare(data)
            peer, boundary = native_peer()
            with boundary, patch.object(sys, 'stdin', host_fixture.Terminal()), \
                    redirect_stdout(host_fixture.Terminal()), patch('builtins.input',
                    return_value='RUN ' + prepared.binding['plan_digest'][:12]):
                result = f.service(profile).execute(data, reviewer='protocol-test')
            self.assertEqual(result['status'], 'COMPLETED')
            before = {path.name: path.read_bytes() for path in (f.base / 'sources').glob('*.json')}
            f.restart()
            restarted = f.profile()
            with patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('no replay')), \
                    patch('builtins.input', side_effect=AssertionError('read is not approval')):
                restored = restarted.read_result()
                reused = f.service(restarted).execute(data, reviewer='protocol-test')
            self.assertEqual(restored.status, 'RENDERED')
            self.assertEqual(restored.png, preview.png())
            self.assertFalse(restored.comparable)
            self.assertEqual(reused['action'], 'reuse')
            self.assertEqual(peer.opens, 1)
            self.assertEqual(before, {path.name: path.read_bytes() for path in (f.base / 'sources').glob('*.json')})

    def test_prepared_selection_mutated_during_html_io_cannot_stay_current(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.core.security.authority import digest
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            prepared = profile.prepare({'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]})
            def changed_selection():
                f.selection['per_item'][0]['dependency_sha256'] = '9' * 64
                f.selection['prepare_sha256'] = digest({key: f.selection[key]
                    for key in ('schema_version', 'status', 'per_item')})
            f.read_hook = changed_selection
            self.assertIs(profile.revalidate(prepared), False)

    def test_imported_preparation_cannot_widen_the_fixed_native_budget(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.core.security.authority import digest
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            prepared = profile.prepare({'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]})
            wider = replace(prepared.provider_data.native_plan, max_output_bytes=2_000_000)
            ref = 'render-' + digest({'task_id': f.authority.task_id, 'target': TARGET,
                'prepare_sha256': f.selection['prepare_sha256'], 'plan_digest': wider.plan_digest})
            forged = replace(prepared, plan=asdict(wider), binding=wider.binding(), source_ref=ref,
                             provider_data=replace(prepared.provider_data, native_plan=wider))
            self.assertIs(profile.revalidate(forged), False)

    def test_registration_interruption_recovers_original_png_without_browser_reexecution(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            with patch.object(f.port, 'register', side_effect=OSError('controlled registration fault')):
                interrupted, peer = f.run_protocol(profile)
            self.assertEqual(interrupted['reason_codes'], ['SOURCE_REGISTRATION_INCOMPLETE'])
            self.assertIsNone(profile.read_result())
            f.restart()
            restarted = f.profile()
            with patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('no replay')), \
                    patch('builtins.input', side_effect=AssertionError('no reapproval')):
                recovered = f.service(restarted).execute(
                    {'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]}, reviewer='protocol-test')
            self.assertEqual(recovered['action'], 'recovered')
            self.assertEqual(restarted.read_result().png, preview.png())
            self.assertEqual(peer.opens, 1)

    def test_cleanup_failure_remains_unknown_and_does_not_retry_or_publish_a_png(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            result, peer = f.run_protocol(profile, cleanup_error=True)
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertIsNone(profile.read_result())
            with patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('no replay')), \
                    patch('builtins.input', side_effect=AssertionError('no reapproval')):
                again = f.service(profile).execute(
                    {'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]}, reviewer='protocol-test')
            self.assertEqual(again['reason_codes'], ['NATIVE_EXECUTION_RESULT_UNKNOWN'])
            self.assertEqual(peer.opens, 1)
            self.assertTrue(peer.cleaned)

    def test_identical_native_plan_cannot_reuse_a_completed_different_context(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.core.security.authority import digest, SecurityAuthorityError
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            first = profile.prepare({'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]})
            result, peer = f.run_protocol(profile)
            self.assertEqual(result['status'], 'COMPLETED')
            target = (*TARGET[:3], 'CTX-2')
            row = f.selection['per_item'][0]
            row['context_id'] = 'CTX-2'
            row['evidence_contexts'][0]['contexts'][0]['context_id'] = 'CTX-2'
            f.selection['prepare_sha256'] = digest({key: f.selection[key]
                for key in ('schema_version', 'status', 'per_item')})
            other = f.profile(target=target)
            second = other.prepare({'profile_id': PROFILE_ID, 'targets': ['/'.join(target)]})
            self.assertEqual(first.binding, second.binding, 'deliberate same-native-plan conflict')
            self.assertNotEqual(first.source_ref, second.source_ref)
            self.assertIsNone(other.read_result())
            with patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('no replay')), \
                    patch('builtins.input', side_effect=AssertionError('no reapproval')), \
                    self.assertRaises(SecurityAuthorityError):
                f.service(other).execute({'profile_id': PROFILE_ID, 'targets': ['/'.join(target)]},
                                         reviewer='protocol-test')
            self.assertEqual(peer.opens, 1)

    def test_data_cannot_select_another_target_or_inject_execution_results_or_grants(self):
        from aek.adapters.ui_render_host import PROFILE_ID
        from aek.core.security.authority import SecurityAuthorityError
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            valid = {'profile_id': PROFILE_ID, 'targets': ['/'.join(TARGET)]}
            with patch('aek.adapters.ui_linux_renderer._NativeProcess', side_effect=AssertionError('no start')):
                for bad in ({**valid, 'html': '<main>injected</main>'}, {**valid, 'argv': ['sh']},
                        {**valid, 'url': 'https://invalid.example'}, {**valid, 'facts': {'status': 'RENDERED'}},
                        {**valid, 'targets': ['PAGE-2/AREA-1/default/CTX-1']},
                        {**valid, 'targets': valid['targets'] * 2}):
                    with self.subTest(bad=bad), self.assertRaises(ValueError):
                        profile.prepare(bad)
                prepared = profile.prepare(valid)
                with self.assertRaises(SecurityAuthorityError):
                    profile.execute(prepared, {'approved': True}, f.authority)

    def test_png_above_the_fixed_inline_budget_is_not_registered_or_published(self):
        from aek.core.artifact.ui_preview import RenderConditions
        def chunk(kind, raw):
            return struct.pack('>I', len(raw)) + kind + raw + struct.pack('>I', zlib.crc32(kind + raw))
        rng = random.Random(1)
        image = (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 430, 430, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(b'\0' + rng.randbytes(430 * 3) for _ in range(430))))
            + chunk(b'IEND', b''))
        self.assertGreater(len(image), 512 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile(conditions=RenderConditions(430, 430))
            result, peer = f.run_protocol(profile, image=image)
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertEqual(peer.opens, 1)
            self.assertIsNone(profile.read_result())

    def test_replaced_runtime_revoked_source_and_tampered_source_json_cannot_supply_a_preview(self):
        from aek.core.security.authority import digest
        import json
        for failure in ('runtime', 'revoked', 'tampered-source'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                f = Fixture(Path(directory))
                profile = f.profile()
                result, _ = f.run_protocol(profile)
                self.assertEqual(result['status'], 'COMPLETED')
                if failure == 'runtime':
                    (f.base / 'bwrap').write_bytes(b'other runtime bytes')
                elif failure == 'revoked':
                    observed = f.port.lookup(result['source_ref'])
                    event = f.authority.read_provenance(observed.provenance_ref)['event']
                    (f.base / 'host' / ('revoked-' + event['event_id'] + '.json')).write_bytes(b'')
                else:
                    path = f.base / 'sources' / ('source-' + digest(result['source_ref']) + '.json')
                    value = json.loads(path.read_bytes())
                    value['facts']['render_result']['png_base64'] = base64.b64encode(preview.png()).decode() + 'AAAA'
                    path.write_text(json.dumps(value), encoding='utf-8')
                f.restart()
                self.assertIsNone(f.profile().read_result())

    def test_final_source_proof_io_cannot_publish_a_result_after_control_context_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            result, _ = f.run_protocol(profile)
            self.assertEqual(result['status'], 'COMPLETED')
            read = f.authority.read_provenance
            with patch.object(f.authority, 'read_provenance', wraps=read) as counted:
                self.assertIsNotNone(profile.read_result())
                final_read = counted.call_count
            calls = 0
            def changed_after_proof(*args, **kwargs):
                nonlocal calls
                value = read(*args, **kwargs)
                calls += 1
                if calls == final_read:
                    f.authority.author = 'changed-during-last-proof'
                return value
            with patch.object(f.authority, 'read_provenance', side_effect=changed_after_proof):
                self.assertIsNone(profile.read_result())
            self.assertEqual(calls, final_read)

    def test_result_reader_accepts_only_the_complete_current_expected_prepared_item(self):
        import copy
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            result, _ = f.run_protocol(profile)
            self.assertEqual(result['status'], 'COMPLETED')
            item = copy.deepcopy(f.selection['per_item'][0])
            narrowed = profile.read_result(expected_item=item)
            self.assertIsNotNone(narrowed)
            self.assertEqual(narrowed.png, preview.png())
            self.assertEqual(profile.read_result().png, preview.png(), 'old call keeps its original meaning')

    def test_expected_item_cannot_omit_or_change_target_dependencies_or_permission_context(self):
        import copy
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            result, _ = f.run_protocol(profile)
            self.assertEqual(result['status'], 'COMPLETED')
            item = copy.deepcopy(f.selection['per_item'][0])
            wrong_context = copy.deepcopy(item)
            wrong_context['evidence_contexts'][0]['contexts'][0]['permissions']['function_id'] = 'FP02'
            missing = {key: value for key, value in item.items() if key != 'evidence_contexts'}
            for other in (True, 'PAGE-1', [], {}, {'page_id': 'PAGE-1'}, missing, wrong_context,
                    {**item, 'context_id': 'CTX-2'}, {**item, 'dependency_sha256': 'f' * 64},
                    {**item, 'verified': True}, {'invalid': object()}):
                with self.subTest(other=type(other).__name__):
                    self.assertIsNone(profile.read_result(expected_item=other))
            self.assertIsNotNone(profile.read_result(expected_item=item))

    def test_caller_item_mutation_during_final_proof_cannot_publish_the_old_private_snapshot(self):
        import copy
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            result, _ = f.run_protocol(profile)
            self.assertEqual(result['status'], 'COMPLETED')
            item = copy.deepcopy(f.selection['per_item'][0])
            read = f.authority.read_provenance
            with patch.object(f.authority, 'read_provenance', wraps=read) as counted:
                self.assertIsNotNone(profile.read_result(expected_item=item))
                final_read = counted.call_count
            calls = 0
            def changed_after_proof(*args, **kwargs):
                nonlocal calls
                value = read(*args, **kwargs)
                calls += 1
                if calls == final_read:
                    item['evidence_contexts'][0]['contexts'][0]['permissions']['function_id'] = 'FP02'
                return value
            with patch.object(f.authority, 'read_provenance', side_effect=changed_after_proof):
                self.assertIsNone(profile.read_result(expected_item=item))
            self.assertEqual(calls, final_read)

    def test_control_fence_is_pure_and_checks_bound_adoption_configuration_not_only_object_id(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            adoption = profile._adoption
            with patch.object(f.port, 'lookup', side_effect=AssertionError('no source IO')), \
                    patch.object(f.authority, 'read_provenance', side_effect=AssertionError('no proof IO')), \
                    patch('aek.adapters.ui_filesystem.open_absolute_dir', side_effect=AssertionError('no FS IO')), \
                    patch('aek.adapters.ui_render_host.hashlib.sha256', side_effect=AssertionError('no hash')), \
                    patch('aek.adapters.security_runtime.BoundRuntimeAdoption.__call__',
                          side_effect=AssertionError('no adoption IO')):
                self.assertIs(profile.control_fence(), True)
                for name, changed in (('_max_age_days', 365), ('_source_refs', ('different-ref',)),
                        ('_proposals', b'[]'), ('_runtime_sha256', 'f' * 64),
                        ('_fingerprint', lambda runtime: runtime.sha256),
                        ('_clock', lambda: None), ('_bound', ())):
                    with self.subTest(name=name):
                        original = getattr(adoption, name)
                        setattr(adoption, name, changed)
                        self.assertIs(profile.control_fence(), False)
                        setattr(adoption, name, original)
                        self.assertIs(profile.control_fence(), True)

    def test_final_proof_cannot_publish_after_bound_adoption_policy_changes_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(Path(directory))
            profile = f.profile()
            result, _ = f.run_protocol(profile)
            self.assertEqual(result['status'], 'COMPLETED')
            read = f.authority.read_provenance
            with patch.object(f.authority, 'read_provenance', wraps=read) as counted:
                self.assertIsNotNone(profile.read_result())
                final_read = counted.call_count
            calls = 0
            def changed_after_proof(*args, **kwargs):
                nonlocal calls
                value = read(*args, **kwargs)
                calls += 1
                if calls == final_read:
                    profile._adoption._max_age_days = 365
                return value
            with patch.object(f.authority, 'read_provenance', side_effect=changed_after_proof):
                self.assertIsNone(profile.read_result())
            self.assertEqual(calls, final_read)


if __name__ == '__main__':
    unittest.main()
