from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aek.adapters import ui_filesystem as fs
from aek.core.artifact.ui_observation import canonical_bytes

FIXTURE = Path(__file__).with_name('ui-observation.test.py')
spec = importlib.util.spec_from_file_location('observation_fixtures', FIXTURE)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ObservationStoreTests(unittest.TestCase):
    def make_record(self):
        source, candidate, contract, provenance = fixtures.UiObservationTests().inputs()
        return fixtures.observation.build_observation(source, contract, ('QUERY',), candidate,
                                                     provenance=provenance)

    def test_current_reader_requires_real_binding_source_contract_and_observed_region(self):
        from aek.adapters.ui_observation_store import ObservationStore
        record = self.make_record()
        source, contract = record['evidence_scope'], record['extraction_contract']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handle = fs.open_absolute_dir(root)
            try:
                with ObservationStore(handle, create=False):
                    self.fail('missing read-only store must not be created')
            except FileNotFoundError:
                self.assertEqual(list(root.iterdir()), [])
            try:
                with ObservationStore(handle) as store:
                    digest = store.publish_observation(record)
                    with self.assertRaisesRegex(ValueError, 'current bound'):
                        store.verify_current(source, contract, ('QUERY',), digest)
                    row = {'evidence_id': 'P1', 'image_sha256': source['image_sha256'],
                        'contract_sha256': hashlib.sha256(canonical_bytes(contract)).hexdigest(),
                        'observation_sha256': digest}
                    store.bind(source['scope_digest'], None, row)
                with ObservationStore(handle, create=False) as store:
                    self.assertIsNone(store.verify_current(source, contract, ('QUERY',), digest))
                    for changed_source, changed_contract, regions in (
                            ({**source, 'image_sha256': '0' * 64}, contract, ('QUERY',)),
                            ({**source, 'scope_digest': '0' * 64}, contract, ('QUERY',)),
                            (source, {**contract, 'prompt_sha256': '0' * 64}, ('QUERY',)),
                            (source, contract, ('UNOBSERVED',))):
                        with self.subTest(source=changed_source, regions=regions), self.assertRaises(ValueError):
                            store.verify_current(changed_source, changed_contract, regions, digest)
            finally:
                fs.close_dir(handle)

    def test_publication_is_content_addressed_and_retries_preserve_bytes(self):
        from aek.adapters.ui_observation_store import ObservationStore
        record = self.make_record()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handle = fs.open_absolute_dir(root)
            try:
                with ObservationStore(handle) as store:
                    digest = store.publish_observation(record)
                    self.assertEqual(digest, hashlib.sha256(canonical_bytes(record)).hexdigest())
                    path = root / 'observations' / (digest + '.json')
                    before = path.stat().st_mtime_ns
                    self.assertEqual(store.publish_observation(record), digest)
                    self.assertEqual(path.stat().st_mtime_ns, before)
                    self.assertEqual(path.read_bytes(), canonical_bytes(record))
                    self.assertEqual(store.read_observation(digest), record)
            finally:
                fs.close_dir(handle)

    def test_image_snapshot_is_immutable_and_bound_to_actual_bytes(self):
        from aek.adapters.ui_observation_store import ObservationStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'source.png'
            image.write_bytes(b'validated image bytes')
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            handle = fs.open_absolute_dir(root)
            try:
                with ObservationStore(handle) as store, image.open('rb') as source:
                    self.assertEqual(store.snapshot_image(source.fileno(), digest, limit=100), digest)
                    snapshot = root / 'observations/snapshots' / (digest + '.png')
                    self.assertEqual(snapshot.read_bytes(), b'validated image bytes')
                    image.write_bytes(b'changed source image')
                    self.assertEqual(snapshot.read_bytes(), b'validated image bytes')
                    source.seek(0)
                    with self.assertRaisesRegex(ValueError, 'snapshot digest mismatch'):
                        store.snapshot_image(source.fileno(), digest, limit=100)
                    self.assertEqual(list(snapshot.parent.iterdir()), [snapshot])
            finally:
                fs.close_dir(handle)

    def test_prepare_and_binding_are_explicit_v2_without_rewriting_legacy_index(self):
        from aek.adapters.ui_observation_store import ObservationStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / '截图索引.json'
            legacy.write_bytes(b'legacy bytes must remain unchanged')
            handle = fs.open_absolute_dir(root)
            try:
                with ObservationStore(handle) as store:
                    payload = {'schema_version': 2, 'source': 'bounded preparation data'}
                    prepare_sha = store.publish_preparation(payload)
                    self.assertEqual(store.read_preparation(prepare_sha), payload)
                    bindings, revision = store.binding_snapshot('a' * 64)
                    self.assertIsNone(revision)
                    self.assertEqual(bindings['bindings'], [])
                    record = self.make_record()
                    record_sha = store.publish_observation(record)
                    contract_sha = hashlib.sha256(canonical_bytes(record['extraction_contract'])).hexdigest()
                    row = {'evidence_id': 'P1', 'image_sha256': 'b' * 64,
                           'contract_sha256': contract_sha, 'observation_sha256': record_sha}
                    revision2 = store.bind('a' * 64, revision, row)
                    saved, actual = store.binding_snapshot('a' * 64)
                    self.assertEqual(actual, revision2)
                    self.assertEqual(saved['bindings'], [row])
                    self.assertEqual(store.bind('a' * 64, revision, row), revision2)
                    with self.assertRaisesRegex(ValueError, 'CONFLICT'):
                        store.bind('a' * 64, revision, {**row, 'observation_sha256': 'e' * 64})
                    self.assertEqual(store.binding_snapshot('a' * 64)[1], revision2)
                    self.assertEqual(legacy.read_bytes(), b'legacy bytes must remain unchanged')
            finally:
                fs.close_dir(handle)

    def test_binding_cannot_promote_missing_or_different_source_observation(self):
        from aek.adapters.ui_observation_store import ObservationStore
        with tempfile.TemporaryDirectory() as directory:
            handle = fs.open_absolute_dir(Path(directory))
            try:
                with ObservationStore(handle) as store:
                    row = {'evidence_id': 'P1', 'image_sha256': 'b' * 64,
                           'contract_sha256': 'c' * 64, 'observation_sha256': 'd' * 64}
                    with self.assertRaises(FileNotFoundError):
                        store.bind('a' * 64, None, row)
                    self.assertIsNone(store.binding_snapshot('a' * 64)[1])
                    record = self.make_record()
                    row['observation_sha256'] = store.publish_observation(record)
                    with self.assertRaisesRegex(ValueError, 'binding does not match'):
                        store.bind('a' * 64, None, row)
                    self.assertIsNone(store.binding_snapshot('a' * 64)[1])
            finally:
                fs.close_dir(handle)

    def test_immutable_conflict_and_tampered_reads_never_overwrite_user_bytes(self):
        from aek.adapters.ui_observation_store import ObservationStore
        record = self.make_record()
        digest = hashlib.sha256(canonical_bytes(record)).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handle = fs.open_absolute_dir(root)
            try:
                with ObservationStore(handle) as store:
                    path = root / 'observations' / (digest + '.json')
                    path.write_bytes(b'user content')
                    with self.assertRaisesRegex(ValueError, 'immutable observation content conflict'):
                        store.publish_observation(record)
                    self.assertEqual(path.read_bytes(), b'user content')
                    with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                        store.read_observation(digest)
                    self.assertEqual(list(path.parent.iterdir()), [path])
                    for bad in ('../outside', '/absolute', 'a' * 63, 'A' * 64, True):
                        with self.subTest(reference=bad), self.assertRaises(ValueError):
                            store.read_observation(bad)
            finally:
                fs.close_dir(handle)

    def test_write_and_publish_failure_leave_no_partial_observation_or_binding(self):
        from aek.adapters.ui_observation_store import ObservationStore
        for boundary in ('write', 'rename_noreplace'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                handle = fs.open_absolute_dir(root)
                try:
                    with ObservationStore(handle) as store:
                        target = ('aek.adapters.ui_observation_store.os.write' if boundary == 'write'
                                  else 'aek.adapters.ui_observation_store.fs.rename_noreplace')
                        with patch(target, side_effect=OSError('synthetic failure')):
                            with self.assertRaisesRegex(OSError, 'synthetic failure'):
                                store.publish_observation(self.make_record())
                        self.assertEqual(list((root / 'observations').iterdir()), [])
                        self.assertIsNone(store.binding_snapshot('a' * 64)[1])
                finally:
                    fs.close_dir(handle)

    def test_observation_directory_and_leaf_symlinks_are_rejected(self):
        from aek.adapters.ui_observation_store import ObservationStore
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, outside = base / 'root', base / 'outside'
            root.mkdir()
            outside.mkdir()
            try:
                (root / 'observations').symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest('native symlink unavailable: ' + str(exc))
            handle = fs.open_absolute_dir(root)
            try:
                with self.assertRaises(OSError):
                    ObservationStore(handle)
                self.assertEqual(list(outside.iterdir()), [])
                (root / 'observations').unlink()
                with ObservationStore(handle) as store:
                    digest = hashlib.sha256(canonical_bytes(self.make_record())).hexdigest()
                    victim = outside / 'victim'
                    victim.write_bytes(b'keep')
                    (root / 'observations' / (digest + '.json')).symlink_to(victim)
                    with self.assertRaises(OSError):
                        store.publish_observation(self.make_record())
                    with self.assertRaises(OSError):
                        store.read_observation(digest)
                    self.assertEqual(victim.read_bytes(), b'keep')
            finally:
                fs.close_dir(handle)


if __name__ == '__main__':
    unittest.main()
