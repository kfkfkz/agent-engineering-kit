"""Local host protocol fixtures are not production approval or real tool runs."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Terminal(io.StringIO):
    def isatty(self):
        return True


class LocalHostTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'private terminal profile')
    def test_original_scanner_domain_plan_digest_is_preserved_not_replaced_by_generic_json_hash(self):
        from dataclasses import asdict
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.core.security.authority import runtime_binding, SecurityAuthorityError
        with tempfile.TemporaryDirectory() as temporary:
            host, authority, _, _ = self.context(Path(temporary))
            adapter = ScannerAdapter(Path(authority.repository_ref), 'trivy', (str(Path(sys.executable).resolve()),))
            plan = adapter.plan(identity_digest=authority.current_identity.envelope_digest,
                canonical_scope_ref=authority.scope_ref, action='version_probe', required_boundaries=())
            values = asdict(plan)
            binding = runtime_binding(**{key: values[key] for key in (
                'plan_digest', 'repository_ref', 'scope_ref', 'identity_digest', 'tool_id', 'executable_digest',
                'action', 'network_mode', 'write_scope', 'timeout_seconds', 'max_output_bytes', 'max_items')})
            with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                    patch('builtins.input', return_value='RUN ' + plan.plan_digest[:12]):
                grant = host.approve_execution(authority, plan=values, binding=binding,
                    reviewer='protocol-test', revalidate=lambda: True)
            authority.validate_runtime_grant(grant, **{k: v for k, v in binding.items() if k != 'operation'})
            # No scanner is started by this protocol test, including a Python executable fixture.
            self.assertEqual(host.restore_attempt(authority, plan=values, binding=binding,
                                                  revalidate=lambda: True), None)
            bad = {**binding, 'max_items': binding['max_items'] - 1}
            with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                    patch('builtins.input') as choose, self.assertRaises(SecurityAuthorityError):
                host.approve_execution(authority, plan=values, binding=bad,
                    reviewer='protocol-test', revalidate=lambda: True)
            choose.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'authenticated private host journal')
    def test_started_attempt_is_unknown_and_real_completion_can_register_after_restart_without_reexecution(self):
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort
        from aek.adapters.security_host import TerminalHostEventSource
        from aek.core.security.authority import SecurityAuthorityError
        spec = importlib.util.spec_from_file_location('source_fixture', ROOT / 'tests/security-workflow.test.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            host, authority, plan, binding = self.context(base)
            self.assertIsNone(host.restore_attempt(authority, plan=plan, binding=binding, revalidate=lambda: True))
            with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                    patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]):
                grant = host.approve_execution(authority, plan=plan, binding=binding,
                    reviewer='protocol-test', revalidate=lambda: True)
            attempt = host.begin_attempt(authority, plan=plan, binding=binding, grant=grant)
            self.assertEqual(host.restore_attempt(authority, plan=plan, binding=binding,
                revalidate=lambda: True), {'status': 'UNKNOWN'})
            with self.assertRaises(SecurityAuthorityError):
                host.begin_attempt(authority, plan=plan, binding=binding, grant=grant)
            metadata = fixture.source_metadata(authority, 'completed-fixture', 'scope')
            metadata['producer_ref'] = binding['tool_id']
            metadata['required_components'] = ['subject.scope_digest']
            facts = {'scope': {'subject_digest': authority.scope_ref}, 'dependency_change': False}
            host.complete_attempt(authority, attempt, metadata=metadata, facts=facts, revalidate=lambda: True)
            # Simulate an interruption before the original source registration.
            restarted = TerminalHostEventSource(state_root=base / 'host', key_file=base / 'private/host.key',
                                                 clock=authority._clock)
            other = BuiltinWorkflowAuthority(repository_ref=authority.repository_ref, task_id=authority.task_id,
                scope_ref=authority.scope_ref, current_identity=authority.current_identity, author=authority.author,
                host_source=restarted, provenance_root=base / 'provenance', clock=authority._clock)
            result = restarted.restore_attempt(other, plan=plan, binding=binding, revalidate=lambda: True)
            self.assertEqual(result['status'], 'COMPLETED')
            self.assertEqual(result['metadata'], metadata)
            self.assertEqual(result['facts'], facts)
            # Repeated status/recovery inspection of one job is bounded by
            # job count, not an accumulating queue of unused opaque tokens.
            for _ in range(257):
                result = restarted.restore_attempt(other, plan=plan, binding=binding, revalidate=lambda: True)
                self.assertEqual(result['status'], 'COMPLETED')
            handle = restarted.recover_source(other, result['completion_token'], revalidate=lambda: True)
            (base / 'sources').mkdir()
            port = BuiltinSourceEvidencePort(other, root=base / 'sources')
            port.register(metadata, facts, handle)
            self.assertEqual(port.lookup(metadata['ref']).metadata['status'], 'VERIFIED')
            with self.assertRaises(SecurityAuthorityError):
                restarted.recover_source(other, result['completion_token'], revalidate=lambda: True)
            # Neither serialized completion data nor a different task can mint a source handle.
            with self.assertRaises(SecurityAuthorityError):
                restarted.recover_source(other, result['metadata'], revalidate=lambda: True)
            other.task_id = 'other-task'
            self.assertIsNone(restarted.restore_attempt(other, plan=plan, binding=binding, revalidate=lambda: True))

    @unittest.skipUnless(os.name == 'posix', 'private POSIX host profile')
    def test_revocation_or_expiry_during_journal_io_cannot_restore_current_authority(self):
        from datetime import timedelta
        from aek.adapters import ui_filesystem as fs
        from aek.core.security.authority import HostEvent
        for change in ('revoke', 'expire'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                host, authority, plan, binding = self.context(base)
                with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                        patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]):
                    grant = host.approve_execution(authority, plan=plan, binding=binding,
                        reviewer='protocol-test', revalidate=lambda: True)
                caller = authority.validate(grant, operation='execute', binding=binding)
                event = HostEvent(**authority.read_provenance(caller['provenance_ref'])['event'])
                original_read = fs.read_at
                def change_after_read(root, name, limit):
                    raw = original_read(root, name, limit)
                    if name.startswith('event-'):
                        if change == 'revoke':
                            (base / 'host' / ('revoked-' + event.event_id + '.json')).write_bytes(b'')
                        else:
                            now = authority._clock()
                            host._clock = lambda: now + timedelta(hours=2)
                    return raw
                with patch.object(fs, 'read_at', side_effect=change_after_read):
                    self.assertFalse(host.restore(event))

    @unittest.skipUnless(os.name == 'posix', 'real POSIX PTY protocol, not production approval')
    def test_real_pty_requires_the_displayed_phrase_and_cancel_mints_nothing(self):
        import errno
        import pty
        import re
        import select
        import time
        script = '''
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('host_protocol_fixture', sys.argv[1])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
host, authority, plan, binding = fixture.LocalHostTests().context(Path(sys.argv[2]))
from aek.core.security.authority import SecurityAuthorityError
try:
    grant = host.approve_execution(authority, plan=plan, binding=binding,
        reviewer='protocol-test', revalidate=lambda: True)
    authority.validate_runtime_grant(grant, **{key: value for key, value in binding.items() if key != 'operation'})
    print(json.dumps({'status': 'GRANTED'}))
except SecurityAuthorityError:
    print(json.dumps({'status': 'DENIED'}))
'''
        for decision in ('accept', 'cancel'):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as temporary:
                master, slave = pty.openpty()
                process = None
                output, sent = bytearray(), 0
                try:
                    process = subprocess.Popen([sys.executable, '-c', script, str(Path(__file__).resolve()), temporary],
                        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                        env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
                    os.close(slave)
                    slave = None
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                block = os.read(master, 65536)
                            except OSError as exc:
                                if exc.errno == errno.EIO:
                                    break
                                raise
                            if not block:
                                break
                            output.extend(block)
                            self.assertLess(len(output), 256 * 1024)
                            if sent == 0 and '否则输入 CANCEL' in output.decode('utf-8'):
                                phrase = re.search(r'RUN [0-9a-f]{12}', output.decode('utf-8')).group()
                                os.write(master, ((phrase if decision == 'accept' else 'CANCEL') + '\n').encode())
                                sent += 1
                        elif process.poll() is not None:
                            break
                    self.assertEqual(process.wait(timeout=2), 0, output.decode('utf-8'))
                    result = json.loads(next(line for line in reversed(output.decode('utf-8').splitlines())
                                             if line.startswith('{"status"')))
                    self.assertEqual(result['status'], 'GRANTED' if decision == 'accept' else 'DENIED')
                    self.assertEqual(sent, 1)
                    self.assertEqual(len(list((Path(temporary) / 'host').glob('event-*.json'))),
                                     1 if decision == 'accept' else 0)
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if slave is not None:
                        os.close(slave)
                    os.close(master)

    def test_unqualified_windows_private_store_does_not_claim_host_capability(self):
        from aek.adapters.security_host import TerminalHostEventSource
        host = TerminalHostEventSource(state_root=Path(tempfile.gettempdir()) / 'unopened-host',
                                       key_file=Path(tempfile.gettempdir()) / 'unopened-key')
        with patch('aek.adapters.security_host.os.name', 'nt'), \
                patch('aek.adapters.security_host.fs.open_absolute_dir') as opened:
            self.assertEqual(host.capabilities(), {'available': False, 'mode': 'unavailable'})
            opened.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'private POSIX host profile')
    def test_failed_journal_write_cleans_private_temporary_and_mints_no_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            host, authority, plan, binding = self.context(base)
            with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                    patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]), \
                    patch('aek.adapters.security_host.os.write', side_effect=OSError('controlled disk failure')):
                with self.assertRaises(OSError):
                    host.approve_execution(authority, plan=plan, binding=binding,
                        reviewer='protocol-test', revalidate=lambda: True)
            self.assertEqual(list((base / 'host').glob('event-*.json')), [])
            self.assertEqual(list((base / 'host').glob('.host-*.tmp')), [])

    @unittest.skipUnless(os.name == 'posix', 'private POSIX host profile')
    def test_displayed_plan_or_budget_change_cannot_mint_an_unseen_grant(self):
        from aek.core.security.authority import SecurityAuthorityError
        for changed in ('plan', 'budget', 'scope'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                host, authority, plan, binding = self.context(base)
                def changed_during_input(_prompt):
                    if changed == 'plan':
                        plan['input_sha256'] = 'f' * 64
                    elif changed == 'budget':
                        binding['max_output_bytes'] = 8192
                    else:
                        authority.scope_ref = 'changed-scope'
                    return 'RUN ' + binding['plan_digest'][:12]
                with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                        patch('builtins.input', side_effect=changed_during_input):
                    with self.assertRaises(SecurityAuthorityError):
                        host.approve_execution(authority, plan=plan, binding=binding,
                            reviewer='protocol-test', revalidate=lambda: True)
                self.assertEqual(list((base / 'host').glob('event-*.json')), [])

    @unittest.skipUnless(os.name == 'posix', 'private POSIX host profile')
    def test_noninteractive_cancel_and_revalidation_failure_leave_no_confirmation(self):
        from aek.core.security.authority import SecurityAuthorityError
        for mode in ('pipe', 'cancel', 'revalidation'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                host, authority, plan, binding = self.context(base)
                input_stream = io.StringIO() if mode == 'pipe' else Terminal()
                with patch.object(sys, 'stdin', input_stream), redirect_stdout(Terminal()), \
                        patch('builtins.input', return_value='CANCEL') as choose:
                    with self.assertRaises(SecurityAuthorityError):
                        host.approve_execution(authority, plan=plan, binding=binding,
                            reviewer='protocol-test', revalidate=lambda: mode != 'revalidation')
                if mode != 'cancel':
                    choose.assert_not_called()
                self.assertEqual(list((base / 'host').glob('event-*.json')), [])

    @unittest.skipUnless(os.name == 'posix', 'private POSIX host profile')
    def test_private_journal_tampering_or_missing_key_cannot_restore_authority(self):
        from aek.core.security.authority import HostEvent
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            host, authority, plan, binding = self.context(base)
            with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                    patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]):
                grant = host.approve_execution(authority, plan=plan, binding=binding,
                    reviewer='protocol-test', revalidate=lambda: True)
            caller = authority.validate(grant, operation='execute', binding=binding)
            event = HostEvent(**authority.read_provenance(caller['provenance_ref'])['event'])
            path = next((base / 'host').glob('event-*.json'))
            original = path.read_bytes()
            body = json.loads(original)
            body['body']['event']['actor'] = 'forged-reviewer'
            path.write_text(json.dumps(body), encoding='utf-8')
            self.assertFalse(host.restore(event))
            # Matching attacker-supplied event fields still lack host authentication.
            forged = HostEvent(**body['body']['event'])
            self.assertFalse(host.restore(forged))
            self.assertIsNone(authority.read_provenance(caller['provenance_ref']))
            path.write_bytes(original)
            self.assertTrue(host.restore(event))
            (base / 'private/host.key').unlink()
            self.assertFalse(host.restore(event))

    @unittest.skipUnless(os.name == 'posix', 'private POSIX host profile')
    def test_weak_storage_or_repository_local_state_cannot_become_a_trusted_host(self):
        from aek.adapters.security_host import TerminalHostEventSource
        from aek.core.security.authority import SecurityAuthorityError
        for mode in ('key-permissions', 'state-permissions', 'key-hardlink', 'repository-state'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                host, authority, plan, binding = self.context(base)
                if mode == 'key-permissions':
                    (base / 'private/host.key').chmod(0o644)
                elif mode == 'state-permissions':
                    (base / 'host').chmod(0o755)
                elif mode == 'key-hardlink':
                    os.link(base / 'private/host.key', base / 'private/other-key')
                else:
                    inside = base / 'repo/state'
                    inside.mkdir(mode=0o700)
                    host = TerminalHostEventSource(state_root=inside, key_file=base / 'private/host.key',
                                                   clock=authority._clock)
                    authority.host_source = host
                with patch.object(sys, 'stdin', Terminal()), redirect_stdout(Terminal()), \
                        patch('builtins.input') as choose:
                    with self.assertRaises(SecurityAuthorityError):
                        host.approve_execution(authority, plan=plan, binding=binding,
                            reviewer='protocol-test', revalidate=lambda: True)
                    choose.assert_not_called()


    def context(self, base):
        from aek.adapters.security_host import TerminalHostEventSource
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.core.context.identity import decode_identity
        from aek.core.security.authority import digest, runtime_binding
        spec = importlib.util.spec_from_file_location('host_identity_fixture', ROOT / 'tests/security-model.test.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        identity = decode_identity(fixture.request_payload()['identity'])
        repo, state, secrets = (base / name for name in ('repo', 'host', 'private'))
        for directory in (repo, state, secrets):
            directory.mkdir(mode=0o700)
        (base / 'provenance').mkdir(mode=0o700)
        key = secrets / 'host.key'
        key.write_bytes(b'\x12' * 32)  # Public deterministic fixture, never a production key.
        key.chmod(0o600)
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        host = TerminalHostEventSource(state_root=state, key_file=key, clock=lambda: now)
        scope = fixture.request_payload()['scope']['subject_digest']
        authority = BuiltinWorkflowAuthority(repository_ref=str(repo), task_id='host-task', scope_ref=scope,
            current_identity=identity, author='author', host_source=host,
            provenance_root=base / 'provenance', clock=lambda: now)
        plan = {'profile': 'controlled-test-profile', 'input_sha256': 'a' * 64,
                'runtime_sha256': 'b' * 64, 'policy_sha256': 'c' * 64}
        binding = runtime_binding(plan_digest=digest(plan), repository_ref=str(repo), scope_ref=scope,
            identity_digest=identity.envelope_digest, tool_id='controlled-test-profile', executable_digest='b' * 64,
            action='read-only-test', network_mode='offline', write_scope=[], timeout_seconds=5,
            max_output_bytes=4096, max_items=32)
        return host, authority, plan, binding

    @unittest.skipUnless(os.name == 'posix', 'first private host store requires POSIX permissions')
    def test_terminal_approval_uses_original_grant_and_authenticated_restart_receipt(self):
        from aek.adapters.security_host import TerminalHostEventSource
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.core.security.authority import RuntimeGrant, SecurityAuthorityError
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            host, authority, plan, binding = self.context(base)
            output = Terminal()
            phrase = 'RUN ' + binding['plan_digest'][:12]
            with patch.object(sys, 'stdin', Terminal()), redirect_stdout(output), patch('builtins.input', return_value=phrase), \
                    patch.object(host, 'consume', wraps=host.consume) as consumed:
                grant = host.approve_execution(authority, plan=plan, binding=binding,
                    reviewer='protocol-test', revalidate=lambda: True)
            live_token = consumed.call_args.args[1]
            with self.assertRaises(SecurityAuthorityError):
                host.consume('user-confirmation', live_token)
            self.assertIs(type(grant), RuntimeGrant)
            authority.validate_runtime_grant(grant, **{key: value for key, value in binding.items() if key != 'operation'})
            caller = authority.validate(grant, operation='execute', binding=binding)
            body = authority.read_provenance(caller['provenance_ref'], live=True)
            self.assertIsNotNone(body)
            from aek.core.security.authority import HostEvent
            event = HostEvent(**body['event'])
            self.assertTrue(host.restore(event))
            restarted = TerminalHostEventSource(state_root=base / 'host', key_file=base / 'private/host.key',
                                                 clock=authority._clock)
            self.assertTrue(restarted.restore(event))
            with self.assertRaises(SecurityAuthorityError):
                restarted.consume('user-confirmation', body['event'])
            self.assertIn(plan['input_sha256'], output.getvalue())
            self.assertNotIn('fixture-key', output.getvalue())


if __name__ == '__main__':
    unittest.main()
