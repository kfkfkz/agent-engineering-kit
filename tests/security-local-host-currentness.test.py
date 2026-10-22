"""Real signed host/source IO races; fixed provider does not run a native tool."""
from contextlib import redirect_stdout
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tests' / (name + '.test.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host_fixture = load_fixture('security-host')
workflow = load_fixture('security-workflow')


class FixedProvider:
    def __init__(self, authority):
        self.authority, self.runs = authority, 0

    def prepare(self, data):
        from aek.application.local_host import PreparedLocalTool
        from aek.core.security.authority import digest, runtime_binding
        plan = {'profile': 'currentness-fixture', 'targets': data['targets']}
        binding = runtime_binding(plan_digest=digest(plan), repository_ref=self.authority.repository_ref,
            scope_ref=self.authority.scope_ref, identity_digest=self.authority.current_identity.envelope_digest,
            tool_id='currentness-fixture', executable_digest='b' * 64, action='read-only-fixture',
            network_mode='offline', write_scope=[], timeout_seconds=1, max_output_bytes=4096, max_items=32)
        return PreparedLocalTool(plan, binding, 'source-' + digest(binding))

    def revalidate(self, _prepared):
        return True

    def execute(self, prepared, grant, authority):
        from aek.application.local_host import LocalToolCompletion
        authority.validate_runtime_grant(grant, **{key: value for key, value in prepared.binding.items() if key != 'operation'})
        self.runs += 1
        metadata = workflow.source_metadata(authority, prepared.source_ref, 'scope')
        metadata['producer_ref'] = 'currentness-fixture'
        metadata['required_components'] = ['subject.scope_digest']
        return LocalToolCompletion(metadata, {'fixture': 'same-current-snapshot'})


DATA = {'profile_id': 'currentness-fixture', 'targets': ['fixture.py']}


@unittest.skipUnless(os.name == 'posix', 'requires the qualified private POSIX terminal-host profile')
class LocalHostCurrentnessTests(unittest.TestCase):
    def setUp(self):
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort
        from aek.application.local_host import LocalToolHost
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.host, self.authority, _, _ = host_fixture.LocalHostTests().context(self.base)
        (self.base / 'sources').mkdir(mode=0o700)
        self.port = BuiltinSourceEvidencePort(self.authority, root=self.base / 'sources')
        self.provider = FixedProvider(self.authority)
        self.service = LocalToolHost(host_source=self.host, authority=self.authority,
            source_port=self.port, providers={'currentness-fixture': self.provider})
        self.prepared = self.service.prepare(DATA)

    def execute(self):
        with patch.object(sys, 'stdin', host_fixture.Terminal()), redirect_stdout(host_fixture.Terminal()), \
                patch('builtins.input', return_value='RUN ' + self.prepared['binding']['plan_digest'][:12]):
            return self.service.execute(DATA, reviewer='protocol-fixture-not-production-approval')

    def revoke_on_actual_source_write(self):
        original = self.port._files.write
        def revoked(name, raw):
            original(name, raw)
            event = self.authority.read_provenance(json.loads(raw)['provenance_ref'])['event']
            (self.base / 'host' / ('revoked-' + event['event_id'] + '.json')).write_bytes(b'')
        return patch.object(self.port._files, 'write', side_effect=revoked)

    def test_fresh_registration_returning_declared_after_real_revocation_is_incomplete(self):
        observed = []
        original = self.port.register
        def register(*args):
            result = original(*args)
            observed.append(result.metadata['status'])
            return result
        with self.revoke_on_actual_source_write(), patch.object(self.port, 'register', side_effect=register):
            result = self.execute()
        self.assertEqual(observed, ['DECLARED'], 'the original SourceEvidencePort must actually see the revocation')
        self.assertEqual(result['status'], 'INCOMPLETE', result)
        self.assertEqual(result['action'], 'human')
        self.assertEqual(self.provider.runs, 1)
        self.assertEqual(self.port.lookup(self.prepared['source_ref']).metadata['status'], 'DECLARED')

    def test_recovered_registration_returning_declared_does_not_rerun_or_claim_completed(self):
        with patch.object(self.port, 'register', side_effect=OSError('controlled interruption before source write')):
            first = self.execute()
        self.assertEqual(first['reason_codes'], ['SOURCE_REGISTRATION_INCOMPLETE'])
        with self.revoke_on_actual_source_write():
            recovered = self.execute()
        self.assertEqual(recovered['status'], 'INCOMPLETE', recovered)
        self.assertEqual(recovered['action'], 'human')
        self.assertEqual(self.provider.runs, 1)
        self.assertEqual(self.port.lookup(self.prepared['source_ref']).metadata['status'], 'DECLARED')

    def assert_provider_io_revocation_is_incomplete(self, mode):
        if mode == 'reuse':
            self.assertEqual(self.execute()['status'], 'COMPLETED')
        elif mode == 'recover':
            with patch.object(self.port, 'register', side_effect=OSError('controlled registration interruption')):
                self.assertEqual(self.execute()['status'], 'INCOMPLETE')
        original_lookup = self.port.lookup
        original_revalidate = self.provider.revalidate
        pending = {}
        def read_then_arm(ref):
            value = original_lookup(ref)
            if value is not None and value.metadata['status'] == 'VERIFIED':
                receipt = self.authority.read_provenance(value.provenance_ref)
                pending['event_id'] = receipt['event']['event_id']
            return value
        def revoked_by_actual_provider_io(prepared):
            if 'event_id' in pending:
                event_id = pending.pop('event_id')
                (self.base / 'host' / ('revoked-' + event_id + '.json')).write_bytes(b'')
            return original_revalidate(prepared)
        with patch.object(self.port, 'lookup', side_effect=read_then_arm), \
                patch.object(self.provider, 'revalidate', side_effect=revoked_by_actual_provider_io):
            result = self.execute()
        self.assertEqual(result['status'], 'INCOMPLETE', result)
        self.assertEqual(result['action'], 'human')
        self.assertEqual(self.port.lookup(self.prepared['source_ref']).metadata['status'], 'DECLARED')
        self.assertEqual(self.provider.runs, 1)

    def test_fresh_source_is_rechecked_after_provider_revalidation_io(self):
        self.assert_provider_io_revocation_is_incomplete('fresh')

    def test_recovered_source_is_rechecked_after_provider_revalidation_io(self):
        self.assert_provider_io_revocation_is_incomplete('recover')

    def test_reuse_cannot_keep_the_snapshot_verified_before_provider_io_revokes_it(self):
        self.assert_provider_io_revocation_is_incomplete('reuse')

    def test_final_original_receipt_is_checked_after_the_last_source_lookup(self):
        self.assertEqual(self.execute()['status'], 'COMPLETED')
        original_lookup, original_revalidate = self.port.lookup, self.provider.revalidate
        state = {}
        def lookup(ref):
            value = original_lookup(ref)
            if value is not None and value.metadata['status'] == 'VERIFIED':
                event = self.authority.read_provenance(value.provenance_ref)['event']
                if state.get('provider_io_finished'):
                    (self.base / 'host' / ('revoked-' + event['event_id'] + '.json')).write_bytes(b'')
                    state['revoked_after_lookup'] = True
                else:
                    state['source_observed'] = True
            return value
        def revalidate(prepared):
            if state.get('source_observed'):
                state['provider_io_finished'] = True
            return original_revalidate(prepared)
        with patch.object(self.port, 'lookup', side_effect=lookup), \
                patch.object(self.provider, 'revalidate', side_effect=revalidate):
            result = self.execute()
        self.assertTrue(state.get('revoked_after_lookup'))
        self.assertEqual(result['status'], 'INCOMPLETE', result)
        self.assertEqual(self.provider.runs, 1)
        self.assertEqual(self.port.lookup(self.prepared['source_ref']).metadata['status'], 'DECLARED')

    def assert_last_proof_io_identity_change_is_incomplete(self, mutation):
        from aek.adapters import ui_filesystem as fs
        from aek.core.context.identity import build_identity, identity_as_dict
        self.assertEqual(self.execute()['status'], 'COMPLETED')
        persisted = {path.name: path.read_bytes() for path in (self.base / 'sources').glob('source-*.json')}
        observation = self.port.lookup(self.prepared['source_ref'])
        event = self.authority.read_provenance(observation.provenance_ref)['event']
        event_name = 'event-' + event['event_id'] + '.json'
        original_lookup, original_read = self.port.lookup, fs.read_at
        original_revalidate = self.provider.revalidate
        state = {}
        def lookup(ref):
            value = original_lookup(ref)
            if value is not None and value.metadata['status'] == 'VERIFIED':
                if state.get('provider_io_finished'):
                    state['arm_final_receipt'] = True
                else:
                    state['source_observed'] = True
            return value
        def revalidate(prepared):
            if state.get('source_observed'):
                state['provider_io_finished'] = True
            return original_revalidate(prepared)
        def changed_after_real_journal_read(root, name, limit):
            raw = original_read(root, name, limit)
            if name == event_name and state.get('arm_final_receipt') and not state.get('changed'):
                state['changed'] = True
                if mutation == 'scope':
                    self.authority.scope_ref = 'e' * 64
                else:
                    components = identity_as_dict(self.authority.current_identity)['components']
                    components['artifact']['design_digest'] = 'e' * 64
                    self.authority.current_identity = build_identity(components)
            return raw
        with patch.object(self.port, 'lookup', side_effect=lookup), \
                patch.object(self.provider, 'revalidate', side_effect=revalidate), \
                patch.object(fs, 'read_at', side_effect=changed_after_real_journal_read):
            result = self.execute()
        self.assertTrue(state.get('changed'), 'must exercise real final signed-journal IO after a verified lookup')
        self.assertEqual(result['status'], 'INCOMPLETE', result)
        self.assertEqual(result['action'], 'human')
        self.assertEqual(self.provider.runs, 1)
        self.assertEqual({path.name: path.read_bytes() for path in (self.base / 'sources').glob('source-*.json')}, persisted)

    def test_final_signed_proof_io_cannot_keep_an_old_scope_snapshot_current(self):
        self.assert_last_proof_io_identity_change_is_incomplete('scope')

    def test_final_signed_proof_io_cannot_keep_an_old_identity_snapshot_current(self):
        self.assert_last_proof_io_identity_change_is_incomplete('identity')

    def test_real_current_journal_can_recover_registration_then_reuse_without_another_execution(self):
        with patch.object(self.port, 'register', side_effect=OSError('controlled registration interruption')):
            self.assertEqual(self.execute()['status'], 'INCOMPLETE')
        recovered = self.execute()
        self.assertEqual((recovered['status'], recovered['action']), ('COMPLETED', 'recovered'))
        reused = self.execute()
        self.assertEqual((reused['status'], reused['action']), ('COMPLETED', 'reuse'))
        self.assertEqual(self.port.lookup(self.prepared['source_ref']).metadata['status'], 'VERIFIED')
        self.assertEqual(self.provider.runs, 1)
        self.assertEqual(len(list((self.base / 'host').glob('event-*.json'))), 2)


if __name__ == '__main__':
    unittest.main()
