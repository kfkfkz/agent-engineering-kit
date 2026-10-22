#!/usr/bin/env python3
"""Original source/host protocol with only TTY and native-process substitutes.

Tiny runtime files are protocol fixtures, not a real Maven/JDK adoption claim.
The separate project-local offline smoke is not repeated by this suite.
"""
from dataclasses import replace
from contextlib import redirect_stdout
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.test.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fixture(root):
    from aek.adapters.security_maven_host import MavenLocalProvider, MavenRuntime, MavenTarget
    from aek.adapters.security_runtime import BoundRuntimeAdoption
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.application.local_host import LocalToolHost
    dependencies = load('security-dependencies')
    protocol = load('security-host')
    host, authority, _, _ = protocol.LocalHostTests().context(root)
    (root / 'sources').mkdir(mode=0o700)
    port = BuiltinSourceEvidencePort(authority, root=root / 'sources')
    proposal = {'id': 'DEP-FIXTURE', 'ecosystem': 'fixture', 'package': 'protocol-runtime',
        'version': '1.0.0', 'purpose': 'protocol only, not adoption of public Maven/JDK',
        'scope': 'build', 'design_refs': ['D13'], 'evidence_refs': ['advisory', 'compatibility']}
    package = {key: proposal[key] for key in ('ecosystem', 'package', 'version')}
    for ref, kind, facts in (('advisory', 'advisory', {**package, 'advisories': []}),
                            ('compatibility', 'compatibility', {**package, 'compatible': True})):
        from aek.core.security.authority import digest, runtime_binding
        metadata = load('security-workflow').source_metadata(authority, ref, kind)
        metadata.update(producer_ref='fixture-producer', required_components=list(dependencies.REQUIRED))
        plan = {'fixture_source': ref}
        binding = runtime_binding(plan_digest=digest(plan), repository_ref=authority.repository_ref,
            scope_ref=authority.scope_ref, identity_digest=authority.current_identity.envelope_digest,
            tool_id='fixture-producer', executable_digest='a' * 64, action='fixture', network_mode='offline',
            write_scope=[], timeout_seconds=1, max_output_bytes=4096, max_items=32)
        with patch.object(sys, 'stdin', protocol.Terminal()), redirect_stdout(protocol.Terminal()), \
                patch('builtins.input', return_value='RUN ' + binding['plan_digest'][:12]):
            grant = host.approve_execution(authority, plan=plan, binding=binding, reviewer='fixture', revalidate=lambda: True)
        attempt = host.begin_attempt(authority, plan=plan, binding=binding, grant=grant)
        handle = host.complete_attempt(authority, attempt, metadata=metadata, facts=facts, revalidate=lambda: True)
        port.register(metadata, facts, handle)
    def file(name, mount, executable=False):
        path = root / name
        content = ('fixture:' + name).encode()
        path.write_bytes(content)
        path.chmod(0o755 if executable else 0o644)
        return str(path), mount, hashlib.sha256(content).hexdigest(), executable
    wrap = file('bubblewrap', 'unused', True)
    limit = file('prlimit', 'unused', True)
    files = (file('java', 'jdk/bin/java', True), file('boot', 'maven/boot/plexus-classworlds-2.11.0.jar'),
             file('config', 'maven/bin/m2.conf'), file('plugin',
                'cache/org/apache/maven/plugins/maven-dependency-plugin/3.11.0/maven-dependency-plugin-3.11.0.jar'))
    runtime = MavenRuntime(wrap[0], wrap[2], limit[0], limit[2], files)
    pom = file('pom.xml', 'model/api/pom.xml')
    settings = file('settings.xml', 'model/settings.xml')
    target = MavenTarget('api', 'api', 'model/api/pom.xml', ('validation',), (pom, settings),
                        (('api/pom.xml', 'module', pom[1]), ('settings.xml', 'settings', settings[1])))
    # The mandatory callback is the production byte measurement, not True.
    from aek.adapters.security_maven_host import maven_runtime_fingerprint
    bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
        source_refs=('advisory', 'compatibility'), required_components=dependencies.REQUIRED,
        runtime_sha256=maven_runtime_fingerprint(runtime), runtime_fingerprint=maven_runtime_fingerprint,
        clock=authority._clock)
    provider = MavenLocalProvider(runtime, authority=authority, adoption=bridge,
                                 targets=(target,), required_components=dependencies.REQUIRED)
    service = LocalToolHost(host_source=host, authority=authority, source_port=port,
                           providers={'maven-offline-v1': provider})
    return service, host, authority, port, provider


DATA = {'profile_id': 'maven-offline-v1', 'targets': ['api']}


def execute(service):
    protocol = load('security-host')
    prepared = service.prepare(DATA)
    with patch.object(sys, 'stdin', protocol.Terminal()), redirect_stdout(protocol.Terminal()), \
            patch('builtins.input', return_value='RUN ' + prepared['binding']['plan_digest'][:12]):
        return service.execute(DATA, reviewer='fixture')


def native_tree(_port, _argv, cwd, **_options):
    from aek.adapters.security_process import ProcessResult
    import json
    tree = {'groupId': 'fixture', 'artifactId': 'api', 'version': '1.0', 'type': 'pom',
            'children': [{'groupId': 'fixture', 'artifactId': 'client', 'version': '1.2.3',
                          'type': 'jar', 'scope': 'compile'}]}
    (Path(cwd) / 'tree.json').write_text(json.dumps(tree))
    empty = hashlib.sha256(b'').hexdigest()
    return ProcessResult(0, 'PROCESS_COMPLETED', b'', b'', empty, empty, .01, True)


def restart(root, authority, provider):
    from aek.adapters.security_host import TerminalHostEventSource
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.adapters.security_runtime import BoundRuntimeAdoption
    from aek.adapters.security_maven_host import MavenLocalProvider, maven_runtime_fingerprint
    from aek.application.local_host import LocalToolHost
    import json
    host = TerminalHostEventSource(state_root=root / 'host', key_file=root / 'private/host.key', clock=authority._clock)
    other = BuiltinWorkflowAuthority(repository_ref=authority.repository_ref, task_id=authority.task_id,
        scope_ref=authority.scope_ref, current_identity=authority.current_identity, author=authority.author,
        expires_at=authority.expires_at, host_source=host, provenance_root=root / 'provenance', clock=authority._clock)
    port = BuiltinSourceEvidencePort(other, root=root / 'sources')
    adoption = BoundRuntimeAdoption(other, port, proposals=tuple(json.loads(provider.adoption._proposals)),
        source_refs=provider.adoption._source_refs, required_components=provider.required_components,
        runtime_sha256=maven_runtime_fingerprint(provider.runtime), runtime_fingerprint=maven_runtime_fingerprint,
        clock=other._clock)
    fixed = MavenLocalProvider(provider.runtime, authority=other, adoption=adoption,
        targets=provider.targets, required_components=provider.required_components)
    return LocalToolHost(host_source=host, authority=other, source_port=port, providers={'maven-offline-v1': fixed})


class DefaultMavenTests(unittest.TestCase):
    def test_default_provider_does_not_start_a_tool_or_claim_availability(self):
        from aek.adapters.security_maven_host import MavenLocalProvider
        from aek.core.security.authority import SecurityAuthorityError
        with self.assertRaisesRegex(SecurityAuthorityError, 'MAVEN_PROVIDER_UNAVAILABLE'):
            MavenLocalProvider().prepare({'profile_id': 'maven-offline-v1', 'targets': ['api']})


@unittest.skipUnless(sys.platform == 'linux', 'fixed Linux offline profile')
class MavenHostTests(unittest.TestCase):
    def test_prlimit_sealed_fd_is_rewound_then_copied_to_a_readonly_named_mount(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            service, _, _, _, _ = fixture(Path(directory))
            def native(_port, argv, cwd, **options):
                limit = options['pass_fds'][1]
                self.assertEqual(os.lseek(limit, 0, os.SEEK_CUR), 0)
                index = argv.index('--file')
                self.assertEqual(argv[index:index + 3], ('--file', str(limit), '/exec/prlimit'))
                self.assertEqual(argv[index + 3:index + 5], ('--remount-ro', '/exec'))
                self.assertNotIn(('--ro-bind', f'/proc/self/fd/{limit}', '/exec/prlimit'),
                    [argv[i:i + 3] for i in range(len(argv) - 2)])
                return native_tree(_port, argv, cwd, **options)
            with patch('aek.adapters.security_process.ProcessPort.run', autospec=True, side_effect=native):
                result = execute(service)
            self.assertEqual(result['status'], 'COMPLETED', result)

    def test_fixed_128_item_budget_completes_at_boundary_and_rejects_129_direct_dependencies(self):
        import json
        for count in (128, 129):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                service, host, _, port, _ = fixture(Path(directory))
                def native(_port, argv, cwd, **options):
                    result = native_tree(_port, argv, cwd, **options)
                    path = Path(cwd) / 'tree.json'
                    tree = json.loads(path.read_bytes())
                    tree['children'] = [{'groupId': 'fixture', 'artifactId': 'dep-' + str(index),
                        'version': '1.2.3', 'type': 'jar', 'scope': 'compile'} for index in range(count)]
                    path.write_text(json.dumps(tree))
                    return result
                prepared = service.prepare(DATA)
                self.assertEqual(prepared['binding']['max_items'], 128)
                with patch('aek.adapters.security_process.ProcessPort.run', autospec=True, side_effect=native) as run:
                    result = execute(service)
                self.assertEqual(run.call_count, 1)
                source = port.lookup(prepared['source_ref'])
                if count == 128:
                    self.assertEqual(result['status'], 'COMPLETED', result)
                    self.assertEqual(len(source.facts['occurrences']), 128)
                else:
                    self.assertEqual(result['status'], 'INCOMPLETE', result)
                    self.assertEqual(result['reason_codes'], ['NATIVE_COMPLETION_SOURCE_UNAVAILABLE'])
                    self.assertIsNone(source)
                    self.assertEqual(len(list(host._root.glob('completion-*.json'))), 2)

    def test_hmac_completion_recovers_registration_after_restart_and_then_reuses_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _, authority, port, provider = fixture(root)
            with patch('aek.adapters.security_process.ProcessPort.run', autospec=True, side_effect=native_tree) as run:
                with patch.object(port, 'register', side_effect=OSError('fixture registration interruption')):
                    first = execute(service)
                self.assertEqual(first['reason_codes'], ['SOURCE_REGISTRATION_INCOMPLETE'])
                resumed = restart(root, authority, provider)
                with patch('builtins.input', side_effect=AssertionError('must not reapprove')):
                    recovered = resumed.execute(DATA, reviewer='fixture')
                    reused = resumed.execute(DATA, reviewer='fixture')
                self.assertEqual((recovered['status'], recovered['action']), ('COMPLETED', 'recovered'), recovered)
                self.assertEqual((reused['status'], reused['action']), ('COMPLETED', 'reuse'), reused)
                self.assertEqual(run.call_count, 1)

    def test_unknown_native_result_stays_unknown_after_restart_without_a_second_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _, authority, _, provider = fixture(root)
            with patch('aek.adapters.security_process.ProcessPort.run', side_effect=OSError('fixture failure')) as run:
                first = execute(service)
                resumed = restart(root, authority, provider)
                with patch('builtins.input', side_effect=AssertionError('must not reapprove')):
                    second = resumed.execute(DATA, reviewer='fixture')
            self.assertEqual(first['status'], 'INCOMPLETE')
            self.assertEqual(second['reason_codes'], ['NATIVE_EXECUTION_RESULT_UNKNOWN'])
            self.assertEqual(run.call_count, 1)

    def test_changed_model_or_runtime_during_confirmation_is_rejected_before_native_start(self):
        from aek.core.security.authority import SecurityAuthorityError
        protocol = load('security-host')
        for name in ('pom.xml', 'java'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service, _, _, _, _ = fixture(root)
                prepared = service.prepare(DATA)
                def changed(_prompt):
                    (root / name).write_bytes(b'changed fixture')
                    return 'RUN ' + prepared['binding']['plan_digest'][:12]
                with patch.object(sys, 'stdin', protocol.Terminal()), redirect_stdout(protocol.Terminal()), \
                        patch('builtins.input', side_effect=changed), patch('aek.adapters.security_process.ProcessPort.run') as run:
                    with self.assertRaises(SecurityAuthorityError):
                        service.execute(DATA, reviewer='fixture')
                run.assert_not_called()

    def test_missing_adoption_or_untrusted_request_fields_cannot_start_native(self):
        from aek.core.security.authority import SecurityAuthorityError
        for mutation in ('adoption', 'facts', 'argv', 'actor', 'approved', 'targets'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                service, _, _, _, provider = fixture(Path(directory))
                data = dict(DATA)
                if mutation == 'adoption':
                    provider.adoption = None
                elif mutation == 'targets':
                    data['targets'] = ['../../pom.xml']
                else:
                    data[mutation] = True
                with patch('aek.adapters.security_process.ProcessPort.run') as run, self.assertRaises(SecurityAuthorityError):
                    service.execute(data, reviewer='fixture')
                run.assert_not_called()

    def test_failed_limits_cleanup_or_output_never_register_completion(self):
        for failure in ('exit', 'timeout', 'limit', 'cleanup', 'json', 'symlink'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service, host, _, port, _ = fixture(root)
                def failed(_port, argv, cwd, **options):
                    result = native_tree(_port, argv, cwd, **options)
                    if failure == 'json':
                        (Path(cwd) / 'tree.json').write_bytes(b'not-json')
                    elif failure == 'symlink':
                        (Path(cwd) / 'tree.json').unlink()
                        (Path(cwd) / 'tree.json').symlink_to(root / 'pom.xml')
                    return replace(result, exit_code=1 if failure == 'exit' else 0,
                        reason_code={'timeout': 'PROCESS_TIMEOUT', 'limit': 'OUTPUT_LIMIT_EXCEEDED'}.get(failure, 'PROCESS_COMPLETED'),
                        cleanup_complete=failure != 'cleanup')
                source_ref = service.prepare(DATA)['source_ref']
                with patch('aek.adapters.security_process.ProcessPort.run', autospec=True, side_effect=failed):
                    result = execute(service)
                self.assertEqual(result['status'], 'INCOMPLETE')
                self.assertIsNone(port.lookup(source_ref))
                self.assertEqual(len(list(host._root.glob('completion-*.json'))), 2)

    def test_large_fixed_manifest_uses_one_readonly_tree_and_eight_pinned_descriptors(self):
        from aek.adapters.security_maven_host import MavenLocalProvider, maven_runtime_fingerprint
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        from aek.application.local_host import LocalToolHost
        import fcntl
        import json
        import os
        import stat
        seen = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, host, authority, port, original = fixture(root)
            extra = []
            for index in range(80):
                path = root / ('cache-' + str(index))
                path.write_bytes(b'fixed cache fixture')
                extra.append((str(path), 'cache/fixture/' + str(index), hashlib.sha256(path.read_bytes()).hexdigest(), False))
            runtime = replace(original.runtime, files=original.runtime.files + tuple(extra))
            bridge = BoundRuntimeAdoption(authority, port, proposals=tuple(json.loads(original.adoption._proposals)),
                source_refs=('advisory', 'compatibility'), required_components=original.required_components,
                runtime_sha256=maven_runtime_fingerprint(runtime), runtime_fingerprint=maven_runtime_fingerprint, clock=authority._clock)
            provider = MavenLocalProvider(runtime, authority=authority, adoption=bridge,
                targets=original.targets, required_components=original.required_components)
            service = LocalToolHost(host_source=host, authority=authority, source_port=port, providers={'maven-offline-v1': provider})
            def native(_port, argv, cwd, **options):
                fds = options['pass_fds']
                seen['fds'], seen['cwd'] = fds, Path(cwd)
                self.assertEqual(len(fds), 8)
                seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
                for fd in fds[:2]:
                    self.assertEqual(fcntl.fcntl(fd, fcntl.F_GET_SEALS), seals)
                tree = Path('/proc/self/fd') / str(fds[2])
                self.assertEqual((tree / 'cache/fixture/79').read_bytes(), b'fixed cache fixture')
                self.assertEqual((tree / 'model/api/pom.xml').read_bytes(), (root / 'pom.xml').read_bytes())
                self.assertFalse((tree / 'cache/fixture/79').stat().st_mode & 0o222)
                self.assertTrue(stat.S_ISREG(os.fstat(fds[3]).st_mode))
                self.assertIn('--dev-bind', argv)
                self.assertNotIn('--dev', argv)
                self.assertEqual(argv.count('--size'), 4)
                self.assertIn('--unshare-all', argv)
                self.assertIn('-o', argv)
                self.assertIn('-N', argv)
                self.assertIn('-Pvalidation', argv)
                self.assertNotIn(str(root / 'java'), argv)
                self.assertNotIn(str(root / 'pom.xml'), argv)
                return native_tree(_port, argv, cwd, **options)
            with patch('aek.adapters.security_process.ProcessPort.run', autospec=True, side_effect=native):
                result = execute(service)
            self.assertEqual(result['status'], 'COMPLETED', result)
            self.assertFalse(seen['cwd'].exists())
            for fd in seen['fds']:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_source_symlinks_or_special_files_are_rejected_during_byte_measurement(self):
        from aek.adapters.security_maven_host import maven_runtime_fingerprint
        import os
        for kind in ('symlink', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, _, _, _, provider = fixture(root)
                java = root / 'java'
                java.rename(root / 'original-java')
                if kind == 'symlink':
                    java.symlink_to(root / 'original-java')
                else:
                    os.mkfifo(java)
                with self.assertRaises((OSError, ValueError)):
                    maven_runtime_fingerprint(provider.runtime)

    def test_native_completion_registers_partial_resolution_through_original_source(self):
        def native(_port, argv, cwd, **options):
            self.assertIn('--unshare-all', argv)
            self.assertIn('--clearenv', argv)
            self.assertLessEqual(len(options['pass_fds']), 8)
            self.assertEqual(options['environment'], {'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
            return native_tree(_port, argv, cwd, **options)
        with tempfile.TemporaryDirectory() as directory:
            service, host, _, port, _ = fixture(Path(directory))
            with patch('aek.adapters.security_process.ProcessPort.run', autospec=True, side_effect=native) as run:
                result = execute(service)
            self.assertEqual(result['status'], 'COMPLETED', result)
            observed = port.lookup(result['source_ref'])
            self.assertEqual(observed.metadata['status'], 'VERIFIED')
            self.assertEqual(observed.facts['inventory']['kind'], 'RESOLVED')
            self.assertEqual(observed.facts['inventory']['coverage'], 'PARTIAL')
            self.assertIn('MAVEN_MODEL_CLOSURE_UNKNOWN', observed.facts['inventory']['coverage_gaps'])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(list(host._root.glob('completion-*.json'))), 3)

    def test_prepare_is_fixed_offline_plan_data_not_native_execution_or_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            service, host, _, _, _ = fixture(Path(directory))
            before = set(host._root.iterdir())
            result = service.prepare({'profile_id': 'maven-offline-v1', 'targets': ['api']})
            self.assertEqual(result['status'], 'PREPARED')
            self.assertEqual(result['binding']['network_mode'], 'offline')
            self.assertEqual(set(host._root.iterdir()), before)


if __name__ == '__main__':
    unittest.main()
