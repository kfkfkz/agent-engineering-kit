"""Handle-relative immutable UI observations inside an already approved root.

The caller owns authorization, image validation and the material lock. Files
published here are data, not applicability, approval or a trusted model event.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

from aek.adapters import ui_filesystem as fs
from aek.core.artifact.ui_observation import (
    MAX_OBSERVATION_BYTES, SEMANTIC_VERSION, ObservationError, canonical_bytes,
    can_reuse, validate_observation,
)

_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Z][A-Z0-9_-]{0,63}\Z')
_BINDINGS = '索引.v2.json'
_BINDING_FIELDS = {'evidence_id', 'image_sha256', 'contract_sha256', 'observation_sha256'}


def extraction_contract(skill_root: Path) -> dict:
    """Control-selected managed skill root, never a path from material JSON."""
    root = fs.open_absolute_dir(skill_root)
    try:
        parent = fs.open_child_dir(root, 'references')
        try:
            prompt = fs.read_at(parent, 'screenshot-observation.md', 8 * 1024)
        finally:
            fs.close_dir(parent)
    finally:
        fs.close_dir(root)
    return {'schema_version': 2, 'semantic_version': SEMANTIC_VERSION,
            'prompt_sha256': hashlib.sha256(prompt).hexdigest()}


def observation_scope(repo_root: Path, requirement_dir: str, change_id: str) -> str:
    return hashlib.sha256(canonical_bytes({'repo_root': str(repo_root.absolute()),
        'requirement_dir': requirement_dir, 'change_id': change_id})).hexdigest()


def _decode(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ObservationError('duplicate stored observation field')
            result[key] = value
        return result

    def invalid(_value):
        raise ObservationError('non-finite stored observation JSON')

    try:
        value = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=unique,
                           parse_constant=invalid)
    except (UnicodeError, ValueError, RecursionError):
        raise ObservationError('invalid stored observation JSON') from None
    if type(value) is not dict:
        raise ObservationError('invalid stored observation object')
    return value


def _digest(value) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ObservationError('invalid observation reference')
    return value


def _binding(value: object) -> dict:
    if type(value) is not dict or set(value) != _BINDING_FIELDS:
        raise ObservationError('invalid observation binding fields')
    if type(value['evidence_id']) is not str or _ID.fullmatch(value['evidence_id']) is None:
        raise ObservationError('invalid observation binding identity')
    for key in _BINDING_FIELDS - {'evidence_id'}:
        _digest(value[key])
    return value


class ObservationStore:
    def __init__(self, material_handle, *, create: bool = True):
        self.handle = fs.open_child_dir(material_handle, 'observations', create=create)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        fs.close_dir(self.handle)

    def _publish_blocks(self, handle, blocks, suffix: str, *, expected: str | None = None) -> str:
        temp = '.publish-' + uuid.uuid4().hex
        fd = fs.open_file(handle, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            digest = hashlib.sha256()
            total = 0
            try:
                for block in blocks:
                    digest.update(block)
                    total += len(block)
                    view = memoryview(block)
                    while view:
                        count = os.write(fd, view)
                        if count <= 0:
                            raise OSError('incomplete observation write')
                        view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            digest = digest.hexdigest()
            if expected is not None and digest != expected:
                raise ObservationError('snapshot digest mismatch')
            target = digest + suffix
            try:
                fs.rename_noreplace(handle, temp, handle, target)
                fs.fsync_dir(handle)
            except FileExistsError:
                if self._file_digest(handle, target, total) != digest:
                    raise ObservationError('immutable observation content conflict') from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                fs.unlink(handle, temp)
        return digest

    @staticmethod
    def _file_digest(handle, name: str, limit: int) -> str:
        fd = fs.open_file(handle, name, os.O_RDONLY)
        try:
            digest = hashlib.sha256()
            for block in ObservationStore._source_blocks(fd, limit):
                digest.update(block)
            return digest.hexdigest()
        finally:
            os.close(fd)

    @staticmethod
    def _source_blocks(fd: int, limit: int):
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ObservationError('image snapshot exceeds size limit')
        os.lseek(fd, 0, os.SEEK_SET)
        total = 0
        while True:
            block = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise ObservationError('image snapshot exceeds size limit')
            yield block
        after = os.fstat(fd)
        keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if total != before.st_size or any(getattr(before, k) != getattr(after, k) for k in keys):
            raise ObservationError('image changed while taking snapshot')

    def snapshot_image(self, source_fd: int, expected_sha256: str, *, limit: int) -> str:
        if type(expected_sha256) is not str or _SHA.fullmatch(expected_sha256) is None:
            raise ObservationError('invalid snapshot digest')
        if type(limit) is not int or limit < 1:
            raise ObservationError('invalid snapshot limit')
        handle = fs.open_child_dir(self.handle, 'snapshots', create=True)
        try:
            return self._publish_blocks(handle, self._source_blocks(source_fd, limit), '.png',
                                        expected=expected_sha256)
        finally:
            fs.close_dir(handle)

    def verify_snapshot(self, digest: str, *, limit: int) -> None:
        _digest(digest)
        handle = fs.open_child_dir(self.handle, 'snapshots')
        try:
            if self._file_digest(handle, digest + '.png', limit) != digest:
                raise ObservationError('visual snapshot digest mismatch')
        finally:
            fs.close_dir(handle)

    def publish_observation(self, record: object) -> str:
        raw = canonical_bytes(validate_observation(record))
        return self._publish_blocks(self.handle, (raw,), '.json')

    def read_observation(self, digest: str) -> dict:
        raw = fs.read_at(self.handle, _digest(digest) + '.json', MAX_OBSERVATION_BYTES)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ObservationError('observation content digest mismatch')
        return validate_observation(_decode(raw))

    def verify_current(self, source: dict, contract: dict, required_regions: tuple[str, ...],
                       expected_sha256: str) -> None:
        """Read-only current reference check; no applicability or approval here."""
        expected = {'evidence_id': source['evidence_id'], 'image_sha256': source['image_sha256'],
            'contract_sha256': hashlib.sha256(canonical_bytes(contract)).hexdigest(),
            'observation_sha256': _digest(expected_sha256)}
        def current_binding():
            value, _ = self.binding_snapshot(source['scope_digest'])
            return next((row for row in value['bindings']
                         if row['evidence_id'] == source['evidence_id']), None)
        if current_binding() != expected:
            raise ObservationError('observation is not the current bound reference')
        record = self.read_observation(expected_sha256)
        if not can_reuse(record, source, contract, required_regions,
                         expected_observation_sha256=expected_sha256):
            raise ObservationError('observation source, contract or observed region is stale')
        if current_binding() != expected:
            raise ObservationError('observation binding changed during read')

    def publish_preparation(self, value: dict) -> str:
        raw = canonical_bytes(value)
        if len(raw) > MAX_OBSERVATION_BYTES:
            raise ObservationError('preparation exceeds byte limit')
        handle = fs.open_child_dir(self.handle, 'preparations', create=True)
        try:
            return self._publish_blocks(handle, (raw,), '.json')
        finally:
            fs.close_dir(handle)

    def read_preparation(self, digest: str) -> dict:
        _digest(digest)
        handle = fs.open_child_dir(self.handle, 'preparations')
        try:
            raw = fs.read_at(handle, digest + '.json', MAX_OBSERVATION_BYTES)
        finally:
            fs.close_dir(handle)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ObservationError('preparation content digest mismatch')
        return _decode(raw)

    def binding_snapshot(self, scope_digest: str) -> tuple[dict, str | None]:
        _digest(scope_digest)
        try:
            raw = fs.read_at(self.handle, _BINDINGS, 256 * 1024)
        except FileNotFoundError:
            return {'schema_version': 2, 'scope_digest': scope_digest, 'bindings': []}, None
        value = _decode(raw)
        if (set(value) != {'schema_version', 'scope_digest', 'bindings'}
                or type(value['schema_version']) is not int or value['schema_version'] != 2
                or value['scope_digest'] != scope_digest or type(value['bindings']) is not list
                or len(value['bindings']) > 256):
            raise ObservationError('invalid v2 observation index or scope')
        seen = set()
        for row in value['bindings']:
            key = _binding(row)['evidence_id']
            if key in seen:
                raise ObservationError('duplicate observation binding identity')
            seen.add(key)
        return value, hashlib.sha256(raw).hexdigest()

    def bind(self, scope_digest: str, expected_revision: str | None, binding: dict) -> str:
        """CAS while caller holds the existing material lock; no approval here."""
        row = _binding(binding)
        if expected_revision is not None:
            _digest(expected_revision)
        value, revision = self.binding_snapshot(scope_digest)
        previous = next((item for item in value['bindings']
                         if item['evidence_id'] == row['evidence_id']), None)
        if previous != row and revision != expected_revision:
            raise ObservationError('CONFLICT: observation index changed')
        record = self.read_observation(row['observation_sha256'])
        expected_source = {'scope_digest': scope_digest, 'evidence_id': row['evidence_id'],
                           'image_sha256': row['image_sha256']}
        if (record['evidence_scope'] != expected_source
                or hashlib.sha256(canonical_bytes(record['extraction_contract'])).hexdigest()
                    != row['contract_sha256']):
            raise ObservationError('observation binding does not match its source')
        if previous == row:  # exact successful retry, even after the old CAS revision
            return revision
        value['bindings'] = sorted([item for item in value['bindings']
            if item['evidence_id'] != row['evidence_id']] + [row], key=lambda item: item['evidence_id'])
        if len(value['bindings']) > 256:
            raise ObservationError('observation binding count exceeds limit')
        raw = canonical_bytes(value)
        temp = '.binding-' + uuid.uuid4().hex
        fd = fs.open_file(self.handle, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            try:
                view = memoryview(raw)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError('incomplete observation binding write')
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            fs.replace(self.handle, temp, self.handle, _BINDINGS)
            fs.fsync_dir(self.handle)
        finally:
            with contextlib.suppress(FileNotFoundError):
                fs.unlink(self.handle, temp)
        return hashlib.sha256(raw).hexdigest()
