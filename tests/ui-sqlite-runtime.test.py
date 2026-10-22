#!/usr/bin/env python3
"""Real loaded-object measurements; fixture evidence is not public adoption."""
from pathlib import Path
from dataclasses import replace
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import importlib.util
import struct

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixture_module(filename):
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).with_name(filename + '.test.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def protocol_factory():
    """Original source/proof protocol fixture, not public package adoption."""
    from aek.adapters.ui_sqlite_runtime import (capture_loaded_sqlite_runtime,
        loaded_sqlite_fingerprint, AdoptedSQLiteFactory)
    from aek.adapters.security_runtime import BoundRuntimeAdoption
    dependencies = fixture_module('security-dependencies')
    payload, host, authority, port = dependencies.dependency_context()
    proposal = payload['proposals'][0]
    package = {key: proposal[key] for key in ('ecosystem', 'package', 'version')}
    dependencies.register_source(host, authority, port, 'advisory', 'advisory',
                                 {**package, 'advisories': []})
    dependencies.register_source(host, authority, port, 'compatibility', 'compatibility',
                                 {**package, 'compatible': True})
    runtime = capture_loaded_sqlite_runtime()
    bridge = BoundRuntimeAdoption(authority, port, proposals=(proposal,),
        source_refs=('advisory', 'compatibility'), required_components=dependencies.REQUIRED,
        runtime_sha256=loaded_sqlite_fingerprint(runtime), runtime_fingerprint=loaded_sqlite_fingerprint,
        clock=authority._clock)
    return AdoptedSQLiteFactory(runtime, bridge), host, authority, port


@unittest.skipUnless(sys.platform == 'linux', 'qualified Linux loaded-object profile')
class LoadedSQLiteTests(unittest.TestCase):
    def test_capture_measures_the_current_mapped_python_extension_and_sqlite_bytes(self):
        from aek.adapters.ui_sqlite_runtime import capture_loaded_sqlite_runtime
        runtime = capture_loaded_sqlite_runtime()
        self.assertTrue({'python', 'sqlite-extension', 'sqlite-library'} <=
                        {item.role for item in runtime.objects})
        self.assertTrue({item.role for item in runtime.objects} <=
                        {'python', 'python-library', 'sqlite-extension', 'sqlite-library'})
        self.assertTrue(all(len(item.sha256) == 64 and item.inode > 0
                            and item.size > 0 for item in runtime.objects))

    def test_fingerprint_remeasures_and_rejects_an_object_digest_not_currently_loaded(self):
        from aek.adapters.ui_sqlite_runtime import capture_loaded_sqlite_runtime, loaded_sqlite_fingerprint
        from aek.adapters.ui_query import QueryUnavailable
        runtime = capture_loaded_sqlite_runtime()
        changed = replace(runtime, objects=(replace(runtime.objects[0], sha256='0' * 64),
                                             *runtime.objects[1:]))
        with self.assertRaises(QueryUnavailable):
            loaded_sqlite_fingerprint(changed)

    def test_new_ambiguous_sqlite_mapping_during_measurement_is_unavailable(self):
        from aek.adapters import ui_sqlite_runtime as module
        original = module._maps
        calls = 0
        def changed_maps():
            nonlocal calls
            rows = original()
            calls += 1
            if calls > 1:
                return rows + (('/another/libsqlite3.so.0', 1, 1, 1, 2, 'r-xp', 0),)
            return rows
        with patch.object(module, '_maps', side_effect=changed_maps), self.assertRaises(module.QueryUnavailable):
            module.capture_loaded_sqlite_runtime()

    def test_factory_uses_original_sources_and_real_private_memory_chinese_query(self):
        factory, _, _, _ = protocol_factory()
        scope, source = fixture_module('ui-query').UiQueryTests().inputs()
        checked = []
        with factory(scope, (source,), revalidate=lambda value: checked.append(value.sha256) or True) as index:
            result = index.query('名称查询', scope=scope)
            self.assertEqual(len(result.hits), 1)
            self.assertIn('按名称查询任务', result.hits[0]['snippet'])
        self.assertGreaterEqual(len(checked), 3)

    def test_original_source_revoked_before_creation_never_opens_sqlite(self):
        from aek.adapters.ui_query import QueryUnavailable
        factory, host, _, _ = protocol_factory()
        scope, source = fixture_module('ui-query').UiQueryTests().inputs()
        host.history.clear()
        with patch('aek.adapters.ui_query.sqlite3.connect') as opened:
            with self.assertRaises(QueryUnavailable):
                factory(scope, (source,), revalidate=lambda _: True)
            opened.assert_not_called()

    def test_image_revalidation_can_revoke_adoption_and_no_old_sqlite_result_escapes(self):
        from aek.adapters.ui_query import QueryUnavailable
        factory, host, _, _ = protocol_factory()
        scope, source = fixture_module('ui-query').UiQueryTests().inputs()
        querying = False
        def revalidate(_):
            if querying:
                host.history.clear()
            return True
        with self.assertRaises(QueryUnavailable):
            with factory(scope, (source,), revalidate=revalidate) as index:
                querying = True
                index.query('名称查询', scope=scope)

    def test_original_image_currentness_semantics_are_preserved(self):
        factory, _, _, _ = protocol_factory()
        scope, source = fixture_module('ui-query').UiQueryTests().inputs()
        current = True
        with factory(scope, (source,), revalidate=lambda _: current) as index:
            current = False
            result = index.query('名称查询', scope=scope)
            self.assertEqual(result.hits, ())
            self.assertEqual(result.stale_count, 1)

    def test_boolean_or_forged_fingerprint_cannot_construct_an_adopted_factory(self):
        from aek.adapters.ui_sqlite_runtime import AdoptedSQLiteFactory, capture_loaded_sqlite_runtime
        from aek.adapters.ui_query import QueryUnavailable
        runtime = capture_loaded_sqlite_runtime()
        for adoption in (True, lambda _: True, {'adopted': True}):
            with self.subTest(adoption=type(adoption)), self.assertRaises(QueryUnavailable):
                AdoptedSQLiteFactory(runtime, adoption)
        factory, _, _, _ = protocol_factory()
        factory.adoption._fingerprint = lambda _: factory.adoption._runtime_sha256
        with self.assertRaises(QueryUnavailable):
            AdoptedSQLiteFactory(runtime, factory.adoption)

    def test_pure_control_fence_detects_changed_authority_without_source_or_runtime_io(self):
        factory, _, authority, port = protocol_factory()
        with patch.object(port, 'lookup', side_effect=AssertionError('unexpected source IO')), patch(
                'aek.adapters.ui_sqlite_runtime.capture_loaded_sqlite_runtime',
                side_effect=AssertionError('unexpected runtime IO')):
            self.assertTrue(factory.control_fence())
            authority.scope_ref = 'e' * 64
            self.assertFalse(factory.control_fence())

    def test_safe_open_rejects_symlinks_in_final_and_parent_components(self):
        from aek.adapters.ui_sqlite_runtime import _safe_open
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'real').mkdir()
            (root / 'real' / 'object').write_bytes(b'object')
            (root / 'linked-directory').symlink_to(root / 'real', target_is_directory=True)
            (root / 'linked-object').symlink_to(root / 'real' / 'object')
            for path in (root / 'linked-object', root / 'linked-directory' / 'object'):
                with self.subTest(path=path.name), self.assertRaises(OSError):
                    _safe_open(str(path))

    def test_elf_truncation_counts_and_offsets_are_bounded(self):
        from aek.adapters.ui_sqlite_runtime import _needed
        from aek.adapters.ui_query import QueryUnavailable
        import _sqlite3
        original = Path(_sqlite3.__file__).read_bytes()
        huge_offset, huge_count, missing_dynamic, huge_dynamic = (bytearray(original) for _ in range(4))
        struct.pack_into('<Q', huge_offset, 32, 2**64 - 1)
        struct.pack_into('<H', huge_count, 56, 65535)
        phoff = struct.unpack_from('<Q', original, 32)[0]
        count = struct.unpack_from('<H', original, 56)[0]
        for number in range(count):
            position = phoff + number * 56
            if struct.unpack_from('<I', original, position)[0] == 2:
                struct.pack_into('<I', missing_dynamic, position, 0)
                struct.pack_into('<Q', huge_dynamic, position + 32, 2**64 - 1)
        with tempfile.TemporaryFile() as source:
            for raw in (b'\x7fELF', original[:64], huge_offset, huge_count, missing_dynamic, huge_dynamic):
                with self.subTest(size=len(raw)):
                    source.seek(0)
                    source.truncate()
                    source.write(raw)
                    source.flush()
                    with self.assertRaises(QueryUnavailable):
                        _needed(source.fileno(), len(raw))

    def test_elf_native_abi_and_dynamic_string_offsets_are_not_guessed(self):
        from aek.adapters.ui_sqlite_runtime import _needed
        from aek.adapters.ui_query import QueryUnavailable
        import _sqlite3
        original = Path(_sqlite3.__file__).read_bytes()
        bad_abi = bytearray(original)
        struct.pack_into('<H', bad_abi, 18, 0)
        variants = [bad_abi]
        phoff = struct.unpack_from('<Q', original, 32)[0]
        count = struct.unpack_from('<H', original, 56)[0]
        for number in range(count):
            position = phoff + number * 56
            if struct.unpack_from('<I', original, position)[0] != 2:
                continue
            dynamic_offset = struct.unpack_from('<Q', original, position + 8)[0]
            dynamic_size = struct.unpack_from('<Q', original, position + 32)[0]
            for offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
                key = struct.unpack_from('<Q', original, offset)[0]
                if key in (1, 5, 10):
                    changed = bytearray(original)
                    struct.pack_into('<Q', changed, offset + 8, 2**64 - 1)
                    variants.append(changed)
        self.assertGreaterEqual(len(variants), 4)
        with tempfile.TemporaryFile() as source:
            for number, raw in enumerate(variants):
                with self.subTest(variant=number):
                    source.seek(0)
                    source.truncate()
                    source.write(raw)
                    source.flush()
                    with self.assertRaises(QueryUnavailable):
                        _needed(source.fileno(), len(raw))

    def test_runtime_measurement_is_not_multiplied_by_screenshot_count(self):
        from aek.adapters import ui_sqlite_runtime as module
        query = fixture_module('ui-query')
        from aek.adapters.ui_query import QuerySource
        from aek.core.artifact.ui_observation import canonical_bytes
        import hashlib
        scope, first = query.UiQueryTests().inputs()
        source, candidate, contract, provenance = query.fixtures.UiObservationTests().inputs()
        values = [first]
        for number in range(2, 13):
            selected = {**source, 'evidence_id': 'P' + str(number)}
            record = query.fixtures.observation.build_observation(selected, contract, ('QUERY',),
                candidate, provenance=provenance)
            values.append(QuerySource(scope, hashlib.sha256(canonical_bytes(record)).hexdigest(),
                                       'c' * 64, record))
        counts = []
        for sources in ((first,), tuple(values)):
            factory, _, _, _ = protocol_factory()
            with patch.object(module, 'capture_loaded_sqlite_runtime',
                              wraps=module.capture_loaded_sqlite_runtime) as measured:
                with factory(scope, sources, revalidate=lambda _: True) as index:
                    index.query('查询', scope=scope)
                counts.append(measured.call_count)
        self.assertEqual(counts, [10, 10])

    def test_mapped_object_inode_mismatch_is_unavailable_before_reading_candidate_bytes(self):
        from aek.adapters import ui_sqlite_runtime as module
        maps = module._maps()
        changed = tuple((row[0], row[1], row[2] + 1, *row[3:])
                        if row[0] == module._sqlite3.__file__ else row for row in maps)
        with patch.object(module, '_maps', return_value=changed), self.assertRaises(module.QueryUnavailable):
            module.capture_loaded_sqlite_runtime()

    def test_runtime_mutation_during_stream_hash_and_oversized_files_are_rejected(self):
        from aek.adapters import ui_sqlite_runtime as module
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'object'
            target.write_bytes(b'original-object')
            info = target.stat()
            maps = ((str(target), info.st_dev, info.st_ino, 1, 2, 'r-xp', 0),)
            original_read = module.os.read
            changed = False
            def change_during_read(fd, count):
                nonlocal changed
                value = original_read(fd, count)
                if not changed:
                    changed = True
                    target.write_bytes(b'replaced-object')
                return value
            with patch.object(module.os, 'read', side_effect=change_during_read), self.assertRaises(module.QueryUnavailable):
                module._measure('python', str(target), maps, module.time.monotonic() + 2)
            self.assertTrue(changed)
            with patch.object(module, '_MAX_OBJECT', 1), self.assertRaises(module.QueryUnavailable):
                module._measure('python', str(target), maps, module.time.monotonic() + 2)

    def test_failed_and_successful_measurements_do_not_leak_safe_descriptors(self):
        from aek.adapters import ui_sqlite_runtime as module
        before = len(tuple(Path('/proc/self/fd').iterdir()))
        for _ in range(3):
            module.capture_loaded_sqlite_runtime()
            with patch.object(module, '_needed', side_effect=module.QueryUnavailable('bad ELF')):
                with self.assertRaises(module.QueryUnavailable):
                    module.capture_loaded_sqlite_runtime()
        self.assertEqual(len(tuple(Path('/proc/self/fd').iterdir())), before)

    def test_expired_measurement_budget_and_preload_ambiguity_never_open_sqlite(self):
        from aek.adapters import ui_sqlite_runtime as module
        for options in ({'timeout': 0}, {'timeout': True}, {'timeout': 3}):
            with self.subTest(options=options), self.assertRaises(module.QueryUnavailable):
                module.capture_loaded_sqlite_runtime(**options)
        with patch.dict(module.os.environ, {'LD_PRELOAD': '/not-authorized.so'}):
            with self.assertRaises(module.QueryUnavailable):
                module.capture_loaded_sqlite_runtime()
        factory, _, _, _ = protocol_factory()
        scope, source = fixture_module('ui-query').UiQueryTests().inputs()
        with patch('aek.adapters.ui_query.sqlite3.connect') as opened:
            with self.assertRaises(module.QueryUnavailable):
                factory(scope, (source,), revalidate=lambda _: True, timeout=0.000001)
            opened.assert_not_called()

    def test_mutation_during_last_original_proof_read_cannot_return_sqlite_result(self):
        from aek.adapters.ui_query import QueryUnavailable
        factory, _, authority, _ = protocol_factory()
        scope, source = fixture_module('ui-query').UiQueryTests().inputs()
        calls = 0
        with self.assertRaises(QueryUnavailable):
            with factory(scope, (source,), revalidate=lambda _: True) as index:
                read = authority.read_provenance
                def change_after_read(*args, **kwargs):
                    nonlocal calls
                    result = read(*args, **kwargs)
                    calls += 1
                    # Two proof passes in each query boundary check.
                    if calls == 8:
                        authority.scope_ref = 'e' * 64
                    return result
                with patch.object(authority, 'read_provenance', side_effect=change_after_read):
                    index.query('名称查询', scope=scope)
        self.assertEqual(calls, 8)


class UnsupportedRuntimeTests(unittest.TestCase):
    def test_unqualified_windows_profile_is_explicitly_unavailable_before_file_access(self):
        from aek.adapters import ui_sqlite_runtime as module
        with patch.object(module.sys, 'platform', 'win32'), patch.object(module, '_maps') as maps:
            with self.assertRaises(module.QueryUnavailable):
                module.capture_loaded_sqlite_runtime()
            maps.assert_not_called()


if __name__ == '__main__':
    unittest.main()
