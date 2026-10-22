#!/usr/bin/env python3
"""Runtime adoption protocol tests, not adoption of real public packages.

Only the host-event boundary is simulated. Sources and provenance use the
original adapters; runtime fingerprints read actual temporary file bytes.
"""
from __future__ import annotations

import hashlib
import importlib.util
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location(
    "runtime_dependency_fixtures", Path(__file__).with_name("security-dependencies.test.py"))
dependencies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dependencies)


def fingerprint(runtime):
    return hashlib.sha256(Path(runtime).read_bytes()).hexdigest()


def fixture(runtime, *, clock=None):
    from aek.adapters.security_runtime import BoundRuntimeAdoption
    payload, host, authority, port = dependencies.dependency_context()
    if clock is not None:
        authority._clock = clock
        authority.expires_at = (clock() + timedelta(days=30)).isoformat()
    proposal = payload["proposals"][0]
    package = {key: proposal[key] for key in ("ecosystem", "package", "version")}
    dependencies.register_source(host, authority, port, "advisory", "advisory",
                                 {**package, "advisories": []})
    dependencies.register_source(host, authority, port, "compatibility", "compatibility",
                                 {**package, "compatible": True})
    bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
        source_refs=("advisory", "compatibility"), required_components=dependencies.REQUIRED,
        runtime_sha256=fingerprint(runtime), runtime_fingerprint=fingerprint,
        clock=authority._clock)
    return bridge, host, authority, port, proposal


class RuntimeAdoptionTests(unittest.TestCase):
    def test_current_original_sources_and_actual_runtime_bytes_allow_protocol_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "protocol-runtime"
            runtime.write_bytes(b"fixture runtime, not a public package")
            bridge, host, authority, port, _ = fixture(runtime)
            before_sources = dict(port._observations)
            before_receipts = dict(authority._receipts)
            before_events = dict(host.history)
            before_handles = dict(authority._handles)
            self.assertIs(bridge(runtime), True)
            self.assertIs(bridge(runtime), True)
            self.assertEqual(port._observations, before_sources)
            self.assertEqual(authority._receipts, before_receipts)
            self.assertEqual(host.history, before_events)
            self.assertEqual(authority._handles, before_handles)

    def test_constructor_rejects_unbound_or_request_selected_configuration(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "protocol-runtime"
            runtime.write_bytes(b"fixture runtime")
            _, _, authority, port, proposal = fixture(runtime)
            options = dict(proposals=(proposal,), source_refs=("advisory", "compatibility"),
                required_components=dependencies.REQUIRED, runtime_sha256=fingerprint(runtime),
                runtime_fingerprint=fingerprint, clock=authority._clock)
            for changes in ({'proposals': ()}, {'proposals': ({**proposal, 'version': 'LATEST'},)},
                    {'proposals': ({**proposal, 'version': None},)}, {'proposals': (proposal, proposal)},
                    {'source_refs': ()}, {'source_refs': ('advisory', 'advisory')},
                    {'source_refs': ('',)}, {'source_refs': ['advisory']},
                    {'required_components': ()}, {'required_components': ('missing.digest',)},
                    {'runtime_sha256': True}, {'runtime_sha256': 'fake'},
                    {'runtime_fingerprint': None}, {'clock': None},
                    {'clock': lambda: dependencies.NOW}, {'max_age_days': True},
                    {'max_age_days': -1}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    BoundRuntimeAdoption(authority, port, **{**options, **changes})
            with self.assertRaises(ValueError):
                BoundRuntimeAdoption(authority, object(), **options)

    def test_changes_during_the_final_proof_read_cannot_publish_adoption(self):
        for field in ('repository_ref', 'task_id', 'scope_ref', 'author', 'expires_at',
                      'host_source', '_clock'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory) / 'protocol-runtime'
                runtime.write_bytes(b'fixture runtime')
                bridge, _, authority, _, _ = fixture(runtime)
                read = authority.read_provenance
                with patch.object(authority, 'read_provenance', wraps=read) as counted:
                    self.assertTrue(bridge(runtime))
                    final_read = counted.call_count
                calls = 0
                def changed_after_proof(*args, **kwargs):
                    nonlocal calls
                    result = read(*args, **kwargs)
                    calls += 1
                    if calls == final_read:
                        replacement = ('2000-01-01T00:00:00+00:00' if field == 'expires_at'
                            else dependencies.workflow.LiveHost() if field == 'host_source'
                            else (lambda: dependencies.NOW) if field == '_clock' else 'changed')
                        setattr(authority, field, replacement)
                    return result
                with patch.object(authority, 'read_provenance', side_effect=changed_after_proof):
                    self.assertIs(bridge(runtime), False)
                self.assertEqual(calls, final_read)

    def test_an_earlier_source_revoked_while_reading_another_source_is_not_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            bridge, host, authority, port, _ = fixture(runtime)
            advisory = port.lookup('advisory')
            event_id = authority.read_provenance(advisory.provenance_ref)['event']['event_id']
            lookup = port.lookup
            revoked = False
            def revoke_earlier(ref):
                nonlocal revoked
                result = lookup(ref)
                if ref == 'compatibility' and not revoked:
                    revoked = True
                    host.history.pop(event_id)
                return result
            with patch.object(port, 'lookup', side_effect=revoke_earlier):
                self.assertIs(bridge(runtime), False)
            self.assertTrue(revoked)

    def test_time_advanced_during_final_proof_cannot_reuse_the_old_freshness_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            now = [dependencies.NOW]
            bridge, _, authority, _, _ = fixture(runtime, clock=lambda: now[0])
            read = authority.read_provenance
            with patch.object(authority, 'read_provenance', wraps=read) as counted:
                self.assertTrue(bridge(runtime))
                final_read = counted.call_count
            calls = 0
            def advance_after_proof(*args, **kwargs):
                nonlocal calls
                result = read(*args, **kwargs)
                calls += 1
                if calls == final_read:
                    now[0] += timedelta(days=8)
                return result
            with patch.object(authority, 'read_provenance', side_effect=advance_after_proof):
                self.assertIs(bridge(runtime), False)
            self.assertEqual(calls, final_read)

    def test_runtime_descriptor_changes_during_final_proof_are_fenced(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'protocol-runtime'
            path.write_bytes(b'fixture runtime')
            _, _, authority, port, proposal = fixture(path)
            runtime = {'path': str(path), 'profile': 'fixed'}
            bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
                source_refs=('advisory', 'compatibility'), required_components=dependencies.REQUIRED,
                runtime_sha256=fingerprint(path), runtime_fingerprint=lambda item: fingerprint(item['path']),
                clock=authority._clock)
            read = authority.read_provenance
            with patch.object(authority, 'read_provenance', wraps=read) as counted:
                self.assertTrue(bridge(runtime))
                final_read = counted.call_count
            calls = 0
            def change_descriptor(*args, **kwargs):
                nonlocal calls
                result = read(*args, **kwargs)
                calls += 1
                if calls == final_read:
                    runtime['profile'] = 'other-context'
                return result
            with patch.object(authority, 'read_provenance', side_effect=change_descriptor):
                self.assertIs(bridge(runtime), False)
            self.assertEqual(calls, final_read)

    def test_fixed_configuration_cannot_be_swapped_during_the_final_proof(self):
        for field in ('_max_age_days', '_source_refs', '_required_components', '_clock', '_fingerprint'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory) / 'protocol-runtime'
                runtime.write_bytes(b'fixture runtime')
                bridge, _, authority, _, _ = fixture(runtime)
                read = authority.read_provenance
                with patch.object(authority, 'read_provenance', wraps=read) as counted:
                    self.assertTrue(bridge(runtime))
                    final_read = counted.call_count
                calls = 0
                def change_configuration(*args, **kwargs):
                    nonlocal calls
                    result = read(*args, **kwargs)
                    calls += 1
                    if calls == final_read:
                        replacement = {'_max_age_days': 365,
                            '_source_refs': ('compatibility', 'advisory'),
                            '_required_components': ('subject.scope_digest',),
                            '_clock': lambda: dependencies.NOW,
                            '_fingerprint': lambda value: fingerprint(value)}[field]
                        setattr(bridge, field, replacement)
                    return result
                with patch.object(authority, 'read_provenance', side_effect=change_configuration):
                    self.assertIs(bridge(runtime), False)
                self.assertEqual(calls, final_read)

    def test_runtime_bytes_changed_during_source_io_cannot_publish_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            bridge, _, _, port, _ = fixture(runtime)
            lookup = port.lookup
            changed = False
            def changed_after_read(ref):
                nonlocal changed
                result = lookup(ref)
                if not changed:
                    changed = True
                    runtime.write_bytes(b'replaced runtime')
                return result
            with patch.object(port, 'lookup', side_effect=changed_after_read):
                self.assertIs(bridge(runtime), False)
            self.assertTrue(changed)

    def test_failed_callbacks_fail_closed_without_mutating_sources_or_authority(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            _, _, authority, port, proposal = fixture(runtime)
            def unavailable(_runtime):
                raise RuntimeError('controlled measurement failure')
            bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
                source_refs=('advisory', 'compatibility'), required_components=dependencies.REQUIRED,
                runtime_sha256=fingerprint(runtime), runtime_fingerprint=unavailable,
                clock=authority._clock)
            receipts = dict(authority._receipts)
            self.assertIs(bridge(runtime), False)
            self.assertEqual(authority._receipts, receipts)

    def test_total_source_bytes_are_bounded_before_loading_later_sources(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            _, host, authority, port, proposal = fixture(runtime)
            for ref in ('large-one', 'large-two'):
                dependencies.register_source(host, authority, port, ref, 'runtime-extra',
                                             {'payload': 'x' * 1_100_000})
            dependencies.register_source(host, authority, port, 'sentinel', 'runtime-extra', {'unused': True})
            bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
                source_refs=('advisory', 'compatibility', 'large-one', 'large-two', 'sentinel'),
                required_components=dependencies.REQUIRED, runtime_sha256=fingerprint(runtime),
                runtime_fingerprint=fingerprint, clock=authority._clock)
            with patch.object(port, 'lookup', wraps=port.lookup) as looked_up:
                self.assertIs(bridge(runtime), False)
            self.assertNotIn('sentinel', [call.args[0] for call in looked_up.call_args_list])

    def test_every_fixed_package_requires_current_adoption_and_success_is_not_cached(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            _, host, authority, port, proposal = fixture(runtime)
            other = {**proposal, 'id': 'DEP-2', 'package': 'org.example:other', 'version': '2.0.0',
                     'evidence_refs': ['other-advisory', 'other-compatibility']}
            refs = ('advisory', 'compatibility', 'other-advisory', 'other-compatibility')
            bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal, other), source_refs=refs,
                required_components=dependencies.REQUIRED, runtime_sha256=fingerprint(runtime),
                runtime_fingerprint=fingerprint, clock=authority._clock)
            self.assertIs(bridge(runtime), False)
            package = {key: other[key] for key in ('ecosystem', 'package', 'version')}
            dependencies.register_source(host, authority, port, 'other-advisory', 'advisory',
                                         {**package, 'advisories': []})
            dependencies.register_source(host, authority, port, 'other-compatibility', 'compatibility',
                                         {**package, 'compatible': True})
            self.assertIs(bridge(runtime), True)
            host.history.clear()
            self.assertIs(bridge(runtime), False)

    def test_original_dependency_rules_reject_risks_unknown_matches_and_incompatible_runtime(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        for case in ('affected', 'unknown', 'stale-advisory', 'incompatible', 'partial'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory) / 'protocol-runtime'
                runtime.write_bytes(b'fixture runtime')
                _, host, authority, port, proposal = fixture(runtime)
                package = {key: proposal[key] for key in ('ecosystem', 'package', 'version')}
                kind, overrides = 'advisory', {}
                row = dependencies.advisory_payload('AFFECTED' if case == 'affected' else
                    'UNKNOWN' if case == 'unknown' else 'NOT_AFFECTED')
                if case == 'stale-advisory':
                    row['checked_at'] = (dependencies.NOW - timedelta(days=8)).isoformat()
                facts = {**package, 'advisories': [row]}
                if case == 'incompatible':
                    kind, facts = 'compatibility', {**package, 'compatible': False}
                if case == 'partial':
                    overrides = {'coverage': 'PARTIAL', 'coverage_gaps': ['fixture-missing-scope']}
                dependencies.register_source(host, authority, port, 'additional', kind, facts, **overrides)
                bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
                    source_refs=('advisory', 'compatibility', 'additional'),
                    required_components=dependencies.REQUIRED, runtime_sha256=fingerprint(runtime),
                    runtime_fingerprint=fingerprint, clock=authority._clock)
                self.assertIs(bridge(runtime), False)

    def test_json_verdicts_and_unattested_source_claims_cannot_replace_original_provenance(self):
        from aek.adapters.security_evidence import SourceObservation
        from aek.core.security.authority import canonical
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            bridge, _, _, port, _ = fixture(runtime)
            original = port.lookup('advisory')
            forged = SourceObservation(canonical(original.metadata).decode(),
                canonical(original.facts).decode(), 'provenance-' + '0' * 64)
            lookup = port.lookup
            for claim in (None, {'status': 'ADOPTABLE', 'adopted': True}, forged):
                with self.subTest(claim=type(claim).__name__), patch.object(port, 'lookup',
                        side_effect=lambda ref: claim if ref == 'advisory' else lookup(ref)):
                    self.assertIs(bridge(runtime), False)
            changed = {**original.facts, 'advisories': [] , 'fabricated': True}
            fabricated = SourceObservation(canonical(original.metadata).decode(),
                canonical(changed).decode(), original.provenance_ref)
            with patch.object(port, 'lookup', side_effect=lambda ref:
                    fabricated if ref == 'advisory' else lookup(ref)):
                self.assertIs(bridge(runtime), False)

    def test_actual_runtime_replacement_missing_file_and_source_expiry_never_reuse_success(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            now = [dependencies.NOW]
            bridge, _, _, _, _ = fixture(runtime, clock=lambda: now[0])
            self.assertTrue(bridge(runtime))
            runtime.write_bytes(b'changed bytes')
            self.assertFalse(bridge(runtime))
            runtime.write_bytes(b'fixture runtime')
            self.assertTrue(bridge(runtime))
            now[0] += timedelta(days=31)
            self.assertFalse(bridge(runtime))
            runtime.unlink()
            self.assertFalse(bridge(runtime))

    def test_controller_dataclass_descriptor_is_supported_but_boolean_measurement_is_not(self):
        from dataclasses import dataclass
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        @dataclass(frozen=True)
        class ProtocolRuntime:
            path: str
            profile: str = 'protocol-not-a-public-runtime'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'protocol-runtime'
            path.write_bytes(b'fixture runtime')
            _, _, authority, port, proposal = fixture(path)
            options = dict(proposals=(proposal,), source_refs=('advisory', 'compatibility'),
                required_components=dependencies.REQUIRED, runtime_sha256=fingerprint(path), clock=authority._clock)
            bridge = BoundRuntimeAdoption(authority, port,
                runtime_fingerprint=lambda runtime: fingerprint(runtime.path), **options)
            self.assertTrue(bridge(ProtocolRuntime(str(path))))
            boolean = BoundRuntimeAdoption(authority, port, runtime_fingerprint=lambda _: True, **options)
            self.assertFalse(boolean(ProtocolRuntime(str(path))))
            # External mutation cannot rewrite the bridge's captured proposal.
            proposal['version'] = '9.9.9'
            proposal['evidence_refs'].clear()
            self.assertTrue(bridge(ProtocolRuntime(str(path))))

    def test_runtime_measurement_io_cannot_hide_source_revocation(self):
        from aek.adapters.security_runtime import BoundRuntimeAdoption
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'protocol-runtime'
            runtime.write_bytes(b'fixture runtime')
            _, host, authority, port, proposal = fixture(runtime)
            calls = 0
            def revoke_on_final_measurement(path):
                nonlocal calls
                calls += 1
                result = fingerprint(path)
                if calls == 2:
                    host.history.clear()
                return result
            bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
                source_refs=('advisory', 'compatibility'), required_components=dependencies.REQUIRED,
                runtime_sha256=fingerprint(runtime), runtime_fingerprint=revoke_on_final_measurement,
                clock=authority._clock)
            self.assertFalse(bridge(runtime))
            self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
