"""Fixed host protocol tests; no public runtime adoption or live scan."""
import importlib.util
from contextlib import redirect_stdout
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('scanner_fixture', ROOT / 'tests/security-scanners.test.py')
scanner_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner_fixture)
spec = importlib.util.spec_from_file_location('host_fixture', ROOT / 'tests/security-host.test.py')
host_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_fixture)


def profile_fixture(base, *, clock_state=None):
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.adapters.security_runtime import BoundRuntimeAdoption
    from aek.adapters.security_scanner import ScannerAdapter
    from aek.adapters.security_trivy_host import (FixedTrivyHostProfile, LinuxTrivyProcessPort,
        TrivyHostRuntime, trivy_runtime_fingerprint)
    from aek.application.local_host import LocalToolHost
    from aek.core.security.authority import digest, runtime_binding
    from aek.core.security.scanners import ScanEvidencePolicy
    from aek.core.context.identity import identity_as_dict
    host, authority, _, _ = host_fixture.LocalHostTests().context(base)
    if clock_state is not None:
        authority._clock = lambda: clock_state[0]
        host._clock = authority._clock
    repo = Path(authority.repository_ref)
    (base / 'sources').mkdir()
    port = BuiltinSourceEvidencePort(authority, root=base / 'sources')
    def register(ref, kind, facts):
        values = {'fixture_source': ref}
        binding = runtime_binding(plan_digest=digest(values), repository_ref=authority.repository_ref,
            scope_ref=authority.scope_ref, identity_digest=authority.current_identity.envelope_digest,
            tool_id='tool', executable_digest='b' * 64, action='fixture-source', network_mode='deny',
            write_scope=(), timeout_seconds=1, max_output_bytes=10000, max_items=100)
        with patch.object(sys, 'stdin', host_fixture.Terminal()), redirect_stdout(host_fixture.Terminal()), \
                patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]):
            grant = host.approve_execution(authority, plan=values, binding=binding, reviewer='protocol', revalidate=lambda: True)
        attempt = host.begin_attempt(authority, plan=values, binding=binding, grant=grant)
        metadata = {'id': ref, 'ref': ref, 'run_id': ref, 'producer_ref': 'tool',
            'identity': identity_as_dict(authority.current_identity), 'required_components': ['subject.scope_digest'],
            'kind': kind, 'status': 'DECLARED', 'checked_at': authority._clock().isoformat(),
            'scope_ref': authority.scope_ref, 'tool_version': None, 'rules_digest': None,
            'database_digest': None, 'coverage': 'COMPLETE_FOR_DECLARED_SCOPE', 'coverage_gaps': [], 'summary': 'Protocol fixture'}
        handle = host.complete_attempt(authority, attempt, metadata=metadata, facts=facts, revalidate=lambda: True)
        port.register(metadata, facts, handle)
    runtime_paths = []
    for name in ('trivy', 'bwrap', 'prlimit'):
        path = base / name
        path.write_bytes(Path('/usr/bin/true').read_bytes())
        path.chmod(0o700)
        runtime_paths.extend((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    runtime = TrivyHostRuntime(*runtime_paths)
    proposals, refs = [], []
    for name in ('trivy', 'bubblewrap', 'prlimit'):
        package = {'ecosystem': 'maven', 'package': 'fixture:' + name, 'version': '1.0.0'}
        evidence = [name + '-advisory', name + '-compatibility']
        register(evidence[0], 'advisory', {**package, 'advisories': []})
        register(evidence[1], 'compatibility', {**package, 'compatible': True})
        proposals.append({'id': name, **package, 'purpose': 'Protocol runtime', 'scope': 'production',
                          'design_refs': ['D13'], 'evidence_refs': evidence})
        refs.extend(evidence)
    adoption = BoundRuntimeAdoption(authority, port, proposals=tuple(proposals), source_refs=tuple(refs),
        required_components=('subject.scope_digest',), runtime_sha256=digest(asdict(runtime)),
        runtime_fingerprint=trivy_runtime_fingerprint, clock=authority._clock)
    native_port = LinuxTrivyProcessPort(runtime, adoption)
    adapter = ScannerAdapter(repo, 'trivy', (runtime.trivy,), process_port=native_port)
    probe = adapter.plan(action='version_probe', identity_digest=authority.current_identity.envelope_digest,
        canonical_scope_ref=authority.scope_ref)
    values = asdict(probe)
    binding = runtime_binding(**{key: values[key] for key in ('plan_digest', 'repository_ref', 'scope_ref',
        'identity_digest', 'tool_id', 'executable_digest', 'action', 'network_mode', 'write_scope',
        'timeout_seconds', 'max_output_bytes', 'max_items')})
    with patch.object(sys, 'stdin', host_fixture.Terminal()), redirect_stdout(host_fixture.Terminal()), \
            patch('builtins.input', return_value='RUN ' + probe.plan_digest[:12]):
        grant = host.approve_execution(authority, plan=values, binding=binding, reviewer='protocol', revalidate=lambda: True)
    if not adapter.version_probe(probe, grant=grant, authority=authority).version_compatible:
        raise AssertionError('authorized protocol version probe failed')
    for name, raw in [('package-lock.json', b'{}'), ('config.json', b'{}'), ('db/trivy.db', b'fixture DB')]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    dbmeta = {'Version': 2, 'UpdatedAt': authority._clock().isoformat()}
    (repo / 'db/metadata.json').write_text(json.dumps(dbmeta))
    plan = adapter.plan(('package-lock.json',), identity_digest=authority.current_identity.envelope_digest,
        canonical_scope_ref=authority.scope_ref, configuration_path='config.json',
        cache_files=('db/trivy.db', 'db/metadata.json'), timeout_seconds=2, max_items=10)
    register('resolved', 'inventory', {'schema_version': 2, 'inventory': {'kind': 'RESOLVED',
        'scope_ref': authority.scope_ref, 'identity_digest': plan.identity_digest,
        'coverage': 'COMPLETE_FOR_DECLARED_SCOPE', 'coverage_gaps': []}, 'occurrences': []})
    register('database', 'database', {'schema_version': 2, 'database': {'kind': 'vulnerability', 'source': 'fixture',
        'schema': 2, 'updated_at': dbmeta['UpdatedAt'], 'path': 'db/trivy.db', 'metadata_path': 'db/metadata.json',
        'sha256': hashlib.sha256((repo / 'db/trivy.db').read_bytes()).hexdigest(),
        'metadata_sha256': hashlib.sha256((repo / 'db/metadata.json').read_bytes()).hexdigest()}})
    profile = FixedTrivyHostProfile(adapter, plan, runtime, adoption, port,
        resolved_inventory_ref='resolved', database_evidence_refs=('database',),
        policy=ScanEvidencePolicy('fixed', 7, (('vulnerability', 'fixture', 2),)),
        clock=authority._clock, required_components=('subject.scope_digest',))
    service = LocalToolHost(host_source=host, authority=authority, source_port=port, providers={'trivy-fixed': profile})
    return service, profile, host, authority, port, plan


@unittest.skipUnless(sys.platform == 'linux', 'fixed Linux Trivy host profile')
class FixedProfileTests(unittest.TestCase):
    @staticmethod
    def native(raw=b'{"SchemaVersion":2,"Results":[]}', *, exit_code=0, reason='PROCESS_COMPLETED', cleanup=True):
        from aek.adapters.security_process import ProcessResult
        def run(argv, cwd, **kwargs):
            value = b'0.75.0' if '--version' in argv else raw
            return ProcessResult(0 if '--version' in argv else exit_code,
                'PROCESS_COMPLETED' if '--version' in argv else reason, value, b'', '', '', 0, cleanup)
        return run

    @staticmethod
    def execute(service, plan):
        with patch.object(sys, 'stdin', host_fixture.Terminal()), redirect_stdout(host_fixture.Terminal()), \
                patch('builtins.input', return_value='RUN ' + plan.plan_digest[:12]):
            return service.execute({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}, reviewer='protocol')

    def test_original_host_registers_and_reuses_one_native_execution(self):
        from aek.adapters.security_process import ProcessResult
        from aek.adapters.security_trivy_host import ProcessPort
        calls = []
        def native(argv, cwd, **kwargs):
            calls.append((argv, kwargs))
            raw = b'0.75.0' if '--version' in argv else b'{"SchemaVersion":2,"Results":[]}'
            return ProcessResult(0, 'PROCESS_COMPLETED', raw, b'', '', '', 0, True)
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=native):
            service, profile, _, _, port, plan = profile_fixture(Path(directory))
            data = {'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}
            self.assertEqual(service.prepare(data)['binding']['plan_digest'], plan.plan_digest)
            self.assertEqual(len(calls), 1)
            with patch.object(sys, 'stdin', host_fixture.Terminal()), redirect_stdout(host_fixture.Terminal()), \
                    patch('builtins.input', return_value='RUN ' + plan.plan_digest[:12]):
                result = service.execute(data, reviewer='protocol')
            self.assertEqual((result['status'], result['action']), ('COMPLETED', 'executed'), result)
            facts = port.lookup(result['source_ref']).facts
            self.assertEqual(facts['schema_version'], 2)
            self.assertEqual(facts['exit_code'], 0)
            self.assertEqual(facts['plan_digest'], plan.plan_digest)
            self.assertEqual(facts['resolved_inventory_ref'], 'resolved')
            self.assertEqual(facts['database_evidence_refs'], ['database'])
            self.assertFalse(Path(facts['scope_root']).exists())
            self.assertEqual(service.execute(data, reviewer='protocol')['action'], 'reuse')
            self.assertEqual(len(calls), 2)

    def test_completed_registration_failure_recovers_authenticated_journal_without_reexecution(self):
        from aek.adapters.security_trivy_host import ProcessPort
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort
        from aek.adapters.security_host import TerminalHostEventSource
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
            base = Path(directory)
            service, profile, host, authority, port, plan = profile_fixture(base)
            with patch.object(port, 'register', side_effect=OSError('controlled registration interruption')):
                result = self.execute(service, plan)
            self.assertEqual(result['reason_codes'], ['SOURCE_REGISTRATION_INCOMPLETE'])
            # Real source/host/authority objects are restored from independently
            # authenticated records; the fixed controller adapter remains live.
            restored_host = TerminalHostEventSource(state_root=base / 'host', key_file=base / 'private/host.key', clock=authority._clock)
            self.assertTrue(restored_host.capabilities()['available'])
            restored_authority = BuiltinWorkflowAuthority(repository_ref=authority.repository_ref, task_id=authority.task_id,
                scope_ref=authority.scope_ref, current_identity=authority.current_identity, author=authority.author,
                host_source=restored_host, provenance_root=base / 'provenance', clock=authority._clock)
            restored_port = BuiltinSourceEvidencePort(restored_authority, root=base / 'sources')
            self.assertEqual(restored_port.lookup('resolved').metadata['status'], 'VERIFIED')
            # Original same controller profile recovery path does not execute
            # version or scan a second time after journal registration failed.
            data = {'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}
            self.assertEqual(service.execute(data, reviewer='protocol')['action'], 'recovered')
            self.assertEqual(service.execute(data, reviewer='protocol')['action'], 'reuse')
            self.assertEqual(native.call_count, 2)

    def test_material_changes_and_control_reconfiguration_invalidate_prepared_execution(self):
        from aek.adapters.security_trivy_host import ProcessPort
        for changed in ('input', 'database', 'runtime', 'port', 'version', 'source'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory, \
                    patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
                base = Path(directory)
                service, profile, _, authority, port, plan = profile_fixture(base)
                data = {'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}
                prepared = profile.prepare(data)
                if changed in {'input', 'database'}:
                    (Path(authority.repository_ref) / ('package-lock.json' if changed == 'input' else 'db/trivy.db')).write_bytes(b'changed')
                elif changed == 'runtime':
                    Path(profile._runtime.trivy).write_bytes(b'changed')
                elif changed == 'port':
                    profile._adapter.process_port = ProcessPort()
                elif changed == 'version':
                    profile._adapter._version_evidence = None
                else:
                    (base / 'sources' / next(path.name for path in (base / 'sources').glob('source-*.json')
                        if b'"ref":"resolved"' in path.read_bytes())).write_bytes(b'{}')
                    port._observations.clear()
                self.assertFalse(profile.revalidate(prepared))
                self.assertEqual(native.call_count, 1)

    def test_expired_or_revoked_original_sources_block_reuse_without_native_execution(self):
        from datetime import datetime, timedelta, timezone
        from aek.adapters.security_trivy_host import ProcessPort
        for change in ('expiry', 'revocation'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, \
                    patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
                clock_state = [datetime(2026, 10, 20, tzinfo=timezone.utc)]
                base = Path(directory)
                service, profile, host, authority, port, plan = profile_fixture(base, clock_state=clock_state)
                self.assertEqual(self.execute(service, plan)['status'], 'COMPLETED')
                prepared = profile.prepare({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']})
                if change == 'expiry':
                    clock_state[0] += timedelta(hours=2)
                else:
                    observation = port.lookup('resolved')
                    event = authority.read_provenance(observation.provenance_ref)['event']
                    (base / 'host' / ('revoked-' + event['event_id'] + '.json')).write_bytes(b'')
                self.assertFalse(profile.revalidate(prepared))
                self.assertEqual(native.call_count, 2)

    def test_cold_adapter_needs_authorized_probe_but_existing_source_read_does_not(self):
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.adapters.security_trivy_host import FixedTrivyHostProfile, ProcessPort
        raw = b'{"SchemaVersion":2,"Results":[{"Target":"package-lock.json","Type":"npm","Class":"lang-pkgs","Packages":[{"ID":"demo@1","Name":"demo","Version":"1"}]}]}'
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
            service, profile, _, authority, port, plan = profile_fixture(Path(directory))
            native.side_effect = self.native(raw)
            result = self.execute(service, plan)
            self.assertEqual(result['status'], 'COMPLETED')
            existing = port.lookup(result['source_ref'])
            cold = ScannerAdapter(Path(authority.repository_ref), 'trivy', plan.executable_prefix,
                process_port=profile._port)
            with self.assertRaisesRegex(ValueError, 'INVALID_FIXED_TRIVY_PROFILE'):
                FixedTrivyHostProfile(cold, plan, profile._runtime, profile._adoption, port,
                    resolved_inventory_ref='resolved', database_evidence_refs=('database',), policy=profile._policy,
                    clock=authority._clock, required_components=profile._required)
            cold.validate_plan_material(plan)
            view = cold.normalize_v2(raw, exit_code=0, version=plan.tool_version,
                scope_paths=tuple(path for path, _ in plan.input_files), source_port=port,
                resolved_inventory_ref='resolved', scan_evidence_ref=result['source_ref'],
                database_evidence_refs=('database',), identity_digest=plan.identity_digest, scope_ref=plan.scope_ref,
                policy=profile._policy, clock=authority._clock, plan=plan, scope_root=existing.facts['scope_root'])
            self.assertEqual(view.execution['status'], 'VERIFIED')
            self.assertEqual(native.call_count, 2)

    def test_signed_source_retains_exact_report_for_cold_v2_details_and_rejects_changes(self):
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort
        from aek.adapters.security_host import TerminalHostEventSource
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.adapters.security_trivy_host import ProcessPort
        raw = b'{ "SchemaVersion":2,"Results":[{"Target":"package-lock.json","Type":"npm","Class":"lang-pkgs","Packages":[{"ID":"demo@1","Name":"demo","Version":"1"}],"Vulnerabilities":[{"VulnerabilityID":"CVE-2026-FIXTURE","PkgName":"demo","InstalledVersion":"1","Severity":"HIGH","FixedVersion":"2.0, 2.1","PrimaryURL":"https://example.org/advisory","VendorIDs":["GHSA-fixture"]}]}]}\n'
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
            base = Path(directory)
            service, profile, _, authority, _, plan = profile_fixture(base)
            native.side_effect = self.native(raw, exit_code=10)
            result = self.execute(service, plan)
            self.assertEqual(result['status'], 'COMPLETED')
            restored_host = TerminalHostEventSource(state_root=base / 'host', key_file=base / 'private/host.key', clock=authority._clock)
            restored_authority = BuiltinWorkflowAuthority(repository_ref=authority.repository_ref, task_id=authority.task_id,
                scope_ref=authority.scope_ref, current_identity=authority.current_identity, author=authority.author,
                host_source=restored_host, provenance_root=base / 'provenance', clock=authority._clock)
            restored_port = BuiltinSourceEvidencePort(restored_authority, root=base / 'sources')
            observation = restored_port.lookup(result['source_ref'])
            self.assertEqual(observation.metadata['status'], 'VERIFIED')
            # The sole report input after restart comes from the response ref;
            # no test-side RAW value is supplied to the v2 consumer.
            restored_raw = observation.facts['report_utf8'].encode('utf-8')
            self.assertEqual(hashlib.sha256(restored_raw).hexdigest(), observation.facts['output_digest'])
            self.assertTrue(restored_raw.startswith(b'{ '))
            self.assertTrue(restored_raw.endswith(b'\n'))
            cold = ScannerAdapter(Path(restored_authority.repository_ref), 'trivy', plan.executable_prefix)
            def inspect(report):
                return cold.normalize_v2(report, exit_code=observation.facts['exit_code'], version=plan.tool_version,
                    scope_paths=tuple(path for path, _ in plan.input_files), source_port=restored_port,
                    resolved_inventory_ref=observation.facts['resolved_inventory_ref'], scan_evidence_ref=result['source_ref'],
                    database_evidence_refs=tuple(observation.facts['database_evidence_refs']), identity_digest=plan.identity_digest,
                    scope_ref=plan.scope_ref, policy=profile._policy, clock=restored_authority._clock,
                    plan=plan, scope_root=observation.facts['scope_root'])
            view = inspect(restored_raw)
            self.assertEqual(view.execution['status'], 'VERIFIED')
            finding = view.scan.findings[0]
            self.assertEqual(finding['fixed_versions'], ['2.0', '2.1'])
            self.assertEqual(finding['source_url'], 'https://example.org/advisory')
            self.assertEqual(finding['aliases'], ['GHSA-fixture'])
            self.assertEqual(inspect(restored_raw.replace(b'2.1', b'2.2')).execution['status'], 'UNVERIFIED')
            event = restored_authority.read_provenance(observation.provenance_ref)['event']
            (base / 'host' / ('revoked-' + event['event_id'] + '.json')).write_bytes(b'')
            self.assertEqual(inspect(restored_raw).execution['status'], 'UNVERIFIED')
            with self.assertRaisesRegex(ValueError, 'HOST_COMPLETION_STALE'):
                service.execute({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}, reviewer='protocol')
            self.assertEqual(native.call_count, 2)

    def test_exact_report_over_original_facts_byte_budget_never_registers_or_replays(self):
        from aek.adapters.security_trivy_host import ProcessPort
        from aek.core.security.scanners import normalize_scan
        raw = json.dumps({'SchemaVersion': 2, 'Results': [], 'padding': ['\n' * 900] * 1000},
            separators=(',', ':')).encode('utf-8')
        self.assertLess(len(raw), 2_000_000)
        # Report strings/items individually meet original parser limits, but
        # exact embedding needs escaping and exceeds the original facts budget.
        self.assertEqual(normalize_scan('trivy', raw, exit_code=0, version='0.75.0',
            scope_paths=('package-lock.json',), database_digest='a' * 64).status, 'INCONCLUSIVE')
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
            service, profile, _, _, port, plan = profile_fixture(Path(directory))
            native.side_effect = self.native(raw)
            result = self.execute(service, plan)
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertIsNone(port.lookup(profile._source_ref()))
            replay = service.execute({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}, reviewer='protocol')
            self.assertEqual(replay['reason_codes'], ['NATIVE_EXECUTION_RESULT_UNKNOWN'])
            self.assertEqual(native.call_count, 2)

    def test_untrusted_execution_fields_and_extra_targets_never_reach_native_boundary(self):
        from aek.adapters.security_trivy_host import ProcessPort
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
            service, _, _, _, _, plan = profile_fixture(Path(directory))
            for extra in ('facts', 'metadata', 'argv', 'completion_sink', 'grant', 'runtime', 'adopted'):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    service.execute({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json'], extra: {}}, reviewer='protocol')
            with self.assertRaises(ValueError):
                service.prepare({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json', 'other.json']})
            self.assertEqual(native.call_count, 1)

    def test_failed_or_incomplete_native_results_never_register_and_never_replay(self):
        from aek.adapters.security_trivy_host import ProcessPort
        cases = [(b'{}', 0, 'PROCESS_COMPLETED', True),
            (b'{"SchemaVersion":2,"Results":[]}', 0, 'PROCESS_TIMEOUT', True),
            (b'{"SchemaVersion":2,"Results":[]}', 0, 'PROCESS_COMPLETED', False),
            (b'{"SchemaVersion":2,"Results":[]}', 2, 'PROCESS_COMPLETED', True),
            (b'{"SchemaVersion":2,"Results":[]}', 0, 'OUTPUT_LIMIT_EXCEEDED', True),
            (b'{"SchemaVersion":2,"Results":[{"Target":"package-lock.json","Type":"jar","Class":"lang-pkgs","Packages":[]}]}', 0, 'PROCESS_COMPLETED', True)]
        for raw, exit_code, reason, cleanup in cases:
            with self.subTest(reason=reason, raw=raw), tempfile.TemporaryDirectory() as directory, \
                    patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
                service, profile, _, _, port, plan = profile_fixture(Path(directory))
                native.side_effect = self.native(raw, exit_code=exit_code, reason=reason, cleanup=cleanup)
                result = self.execute(service, plan)
                self.assertEqual(result['status'], 'INCOMPLETE', result)
                self.assertIsNone(port.lookup(profile._source_ref()))
                result = service.execute({'profile_id': 'trivy-fixed', 'targets': ['package-lock.json']}, reviewer='protocol')
                self.assertEqual(result['reason_codes'], ['NATIVE_EXECUTION_RESULT_UNKNOWN'])
                self.assertEqual(native.call_count, 2)

    def test_finding_exit_ten_keeps_candidates_and_inconclusive_keeps_coverage_gaps(self):
        from aek.adapters.security_trivy_host import ProcessPort
        raw = b'{"SchemaVersion":2,"Results":[{"Target":"package-lock.json","Type":"npm","Class":"lang-pkgs","Packages":[{"ID":"demo@1","Name":"demo","Version":"1"}],"Vulnerabilities":[{"VulnerabilityID":"CVE-2026-FIXTURE","PkgName":"demo","InstalledVersion":"1","Severity":"HIGH"}]}]}'
        for report, code, expected in ((raw, 10, 'FAILED'), (b'{"SchemaVersion":2,"Results":[]}', 0, 'INCONCLUSIVE')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory, \
                    patch.object(ProcessPort, 'run', side_effect=self.native()) as native:
                service, _, _, _, port, plan = profile_fixture(Path(directory))
                native.side_effect = self.native(report, exit_code=code)
                result = self.execute(service, plan)
                self.assertEqual(result['status'], 'COMPLETED', result)
                observation = port.lookup(result['source_ref'])
                self.assertEqual(observation.facts['status'], expected)
                self.assertEqual(observation.facts['exit_code'], code)
                if expected == 'FAILED':
                    self.assertEqual(observation.facts['findings'][0]['verification_status'], 'SUSPECTED')
                else:
                    self.assertTrue(observation.metadata['coverage_gaps'])
                    self.assertEqual(observation.metadata['coverage'], 'PARTIAL')

    def test_native_boundary_gets_sealed_executables_pinned_mounts_and_original_limits(self):
        import fcntl
        from aek.adapters.security_trivy_host import ProcessPort
        calls = []
        original = self.native()
        def inspect(argv, cwd, **kwargs):
            calls.append(argv)
            self.assertTrue(argv[0].startswith('/proc/self/fd/'))
            for flag in ('--unshare-net', '--unshare-user', '--unshare-pid', '--clearenv'):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index('--tmpfs') + 1], '/')
            self.assertIn('--as=2147483648', argv)
            self.assertIn('--nproc=64', argv)
            self.assertIn('--ro-bind-fd', argv)
            self.assertEqual(argv.count('--file'), 2)
            self.assertIn('--remount-ro', argv)
            self.assertEqual(argv.count('--size'), 3)
            self.assertNotIn('--dev', argv)
            self.assertEqual(argv.count('--dev-bind'), 2)
            descriptors = kwargs['pass_fds']
            self.assertLessEqual(len(descriptors), 32)
            for fd in descriptors[:3]:
                self.assertTrue(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & fcntl.F_SEAL_WRITE)
                with self.assertRaises(OSError):
                    os.write(fd, b'changed')
            if '--version' not in argv:
                self.assertEqual(kwargs['timeout_seconds'], 2)
                self.assertEqual(kwargs['max_output_bytes'], 2_000_000)
                self.assertEqual(argv.count('--ro-bind-fd'), 3)
            return original(argv, cwd, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(ProcessPort, 'run', side_effect=inspect):
            service, _, _, _, _, plan = profile_fixture(Path(directory))
            self.assertEqual(self.execute(service, plan)['status'], 'COMPLETED')
            self.assertEqual(len(calls), 2)


class CompletionObserverTests(unittest.TestCase):
    def test_non_linux_fingerprint_rejects_fixed_linux_runtime(self):
        from aek.adapters.security_trivy_host import TrivyHostRuntime, trivy_runtime_fingerprint
        runtime = TrivyHostRuntime('/not-opened/trivy', 'a' * 64, '/not-opened/bwrap', 'b' * 64,
            '/not-opened/prlimit', 'c' * 64)
        with patch.object(sys, 'platform', 'win32'):
            self.assertIsNone(trivy_runtime_fingerprint(runtime))

    def test_fixed_native_port_refuses_unqualified_runtime(self):
        from aek.adapters.security_trivy_host import LinuxTrivyProcessPort
        with self.assertRaises(ValueError):
            LinuxTrivyProcessPort(object(), object())

    def test_capture_is_after_cleanup_and_contains_original_normalized_execution(self):
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.adapters.security_process import ProcessResult
        class Native:
            supported_boundaries = frozenset()
            calls = 0
            after_scan = None
            cleanup = True
            scan_output = b'{"SchemaVersion":2,"Results":[]}'
            def run(self, argv, cwd, **kwargs):
                self.calls += 1
                raw = b'0.75.0' if '--version' in argv else self.scan_output
                if '--version' not in argv and self.after_scan is not None:
                    self.after_scan()
                return ProcessResult(0, 'PROCESS_COMPLETED', raw, b'', '', '', 0, self.cleanup)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, raw in [('app.py', b'x=1'), ('config.json', b'{}'), ('db/trivy.db', b'fixture')]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            native = Native()
            adapter = ScannerAdapter(root, 'trivy', (str(Path(sys.executable).resolve()),), process_port=native)
            probe = adapter.plan(identity_digest=scanner_fixture.test_identity_digest(), canonical_scope_ref='a' * 64,
                action='version_probe', required_boundaries=())
            authority, grant = scanner_fixture.grant_for(probe)
            self.assertTrue(adapter.version_probe(probe, grant=grant, authority=authority).version_compatible)
            plan = adapter.plan(('app.py',), identity_digest=probe.identity_digest, canonical_scope_ref=probe.scope_ref, configuration_path='config.json',
                cache_files=('db/trivy.db',), required_boundaries=())
            authority, grant = scanner_fixture.grant_for(plan)
            captured = []
            def observe(value):
                self.assertFalse(Path(value.scope_root).exists())
                captured.append(value)
            result = adapter.execute(plan, grant=grant, authority=authority, completion_sink=observe)
            self.assertEqual(result.status, 'INCONCLUSIVE')
            self.assertEqual(native.calls, 2)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].raw, b'{"SchemaVersion":2,"Results":[]}')
            self.assertEqual(captured[0].plan_digest, plan.plan_digest)
            with self.assertRaises(Exception):
                captured[0].exit_code = 10
            for failure in ('cleanup', 'malformed', 'material'):
                with self.subTest(failure=failure):
                    captured.clear()
                    native.cleanup = failure != 'cleanup'
                    native.scan_output = b'{}' if failure == 'malformed' else b'{"SchemaVersion":2,"Results":[]}'
                    native.after_scan = (lambda: (root / 'app.py').write_bytes(b'changed')) if failure == 'material' else None
                    result = adapter.execute(plan, grant=grant, authority=authority, completion_sink=observe)
                    self.assertIn(result.status, {'ERROR', 'BLOCKED'})
                    self.assertFalse(captured)


if __name__ == '__main__':
    unittest.main()
