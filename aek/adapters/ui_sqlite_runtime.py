"""Qualified Linux loaded objects, not symbol-binding or memory-page attestation.

The fixed ELF64 profile uses DT_NEEDED and a unique mapped SQLite library as
association evidence. It neither loads a candidate library nor trusts a caller
path. Same-process arbitrary native code is outside this control-side boundary.
"""
from dataclasses import asdict, dataclass
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import sqlite3
import stat
import struct
import sys
import time
import _sqlite3

from aek.adapters.ui_query import QueryUnavailable, ScopedObservationIndex
from aek.adapters.security_runtime import BoundRuntimeAdoption

_MAX_OBJECT = 64 * 1024 * 1024
_MAX_TOTAL = 128 * 1024 * 1024
_MEASURE_END = ContextVar('loaded_sqlite_measure_end', default=None)


@dataclass(frozen=True)
class LoadedSQLiteObject:
    role: str
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True)
class LoadedSQLiteRuntime:
    objects: tuple[LoadedSQLiteObject, ...]
    sqlite_version: str
    python_version: str
    mappings: tuple[tuple, ...]


def _unavailable():
    raise QueryUnavailable('qualified loaded SQLite runtime unavailable')


def _deadline(end):
    if time.monotonic() >= end:
        _unavailable()


def _maps():
    with open(f'/proc/{os.getpid()}/maps', 'rb') as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or len(raw.splitlines()) > 4096:
        _unavailable()
    result = []
    for line in raw.decode('utf-8', errors='strict').splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith('/'):
            start, end = (int(item, 16) for item in fields[0].split('-'))
            major, minor = (int(item, 16) for item in fields[3].split(':'))
            result.append((fields[5], os.makedev(major, minor), int(fields[4]),
                           start, end, fields[1], int(fields[2], 16)))
    return tuple(result)


def _safe_open(path):
    parts = PurePosixPath(path).parts
    if (not parts or parts[0] != '/' or any(part in ('.', '..') for part in parts)
            or '\\' in path or '\0' in path or path.endswith(' (deleted)')):
        _unavailable()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


def _read_at(fd, size, offset, file_size):
    if (type(size) is not int or type(offset) is not int or size < 0 or offset < 0
            or size > 65536 or offset + size > file_size):
        _unavailable()
    raw = os.pread(fd, size, offset)
    if len(raw) != size:
        _unavailable()
    return raw


def _needed(fd, file_size):
    """Only bounded native ELF64/little-endian DT_NEEDED, not a loader."""
    header = _read_at(fd, 64, 0, file_size)
    native_machine = {'x86_64': 62, 'aarch64': 183}.get(os.uname().machine)
    kind, machine, version = struct.unpack_from('<HHI', header, 16)
    if (header[:7] != b'\x7fELF\x02\x01\x01' or native_machine is None
            or kind != 3 or machine != native_machine or version != 1
            or struct.unpack_from('<H', header, 52)[0] != 64):
        _unavailable()
    phoff = struct.unpack_from('<Q', header, 32)[0]
    phsize, count = struct.unpack_from('<HH', header, 54)
    if phsize != 56 or not 1 <= count <= 128 or phoff < 64:
        _unavailable()
    raw = _read_at(fd, count * phsize, phoff, file_size)
    loads, dynamic = [], []
    for position in range(count):
        kind, _, offset, address, _, size, memory_size, _ = struct.unpack_from('<IIQQQQQQ', raw, position * 56)
        if kind in (1, 2):
            if offset + size > file_size or size > memory_size:
                _unavailable()
            (loads if kind == 1 else dynamic).append((offset, address, size))
    if len(dynamic) != 1 or not loads:
        _unavailable()
    offset, _, size = dynamic[0]
    if not 16 <= size <= 8192 or size % 16:
        _unavailable()
    entries = _read_at(fd, size, offset, file_size)
    needed, string_address, string_size, ended = [], [], [], False
    for position in range(0, size, 16):
        key, value = struct.unpack_from('<QQ', entries, position)
        if key == 0:
            ended = True
            break
        if key == 1:
            needed.append(value)
        elif key == 5:
            string_address.append(value)
        elif key == 10:
            string_size.append(value)
    if (not ended or not 1 <= len(needed) <= 32 or len(string_address) != 1
            or len(string_size) != 1 or not 1 <= string_size[0] <= 65536):
        _unavailable()
    candidates = [offset + string_address[0] - address for offset, address, size in loads
                  if address <= string_address[0] and string_address[0] + string_size[0] <= address + size]
    if len(candidates) != 1:
        _unavailable()
    strings = _read_at(fd, string_size[0], candidates[0], file_size)
    names = []
    for offset in needed:
        if offset >= len(strings) or (end := strings.find(b'\0', offset)) < 0:
            _unavailable()
        value = strings[offset:end].decode('ascii', errors='strict')
        if not value or '/' in value or len(value) > 256:
            _unavailable()
        names.append(value)
    return tuple(names)


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _measure(role, path, maps, end, *, max_bytes=_MAX_TOTAL):
    selected = tuple(row for row in maps if row[0] == path)
    if (not selected or len({(row[1], row[2]) for row in selected}) != 1
            or not any('x' in row[5] for row in selected)):
        _unavailable()
    fd = _safe_open(path)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= min(_MAX_OBJECT, max_bytes)
                or (before.st_dev, before.st_ino) != selected[0][1:3]):
            _unavailable()
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 256 * 1024):
            _deadline(end)
            total += len(chunk)
            if total > before.st_size:
                _unavailable()
            digest.update(chunk)
        needed = _needed(fd, before.st_size) if role == 'sqlite-extension' else ()
        if total != before.st_size or _signature(before) != _signature(os.fstat(fd)):
            _unavailable()
        value = LoadedSQLiteObject(role, path, *_signature(before), digest.hexdigest())
        return value, needed, selected
    finally:
        os.close(fd)


def capture_loaded_sqlite_runtime(*, timeout=2.0):
    """Measure this process only; no caller-selected binary or library path."""
    try:
        if (sys.platform != 'linux' or os.name != 'posix' or sys.byteorder != 'little'
                or struct.calcsize('P') != 8 or not hasattr(os, 'O_NOFOLLOW')
                or any(os.environ.get(key) for key in ('LD_PRELOAD', 'LD_AUDIT'))
                or type(timeout) not in (int, float) or not 0 < timeout <= 2):
            _unavailable()
        end = time.monotonic() + timeout
        maps = _maps()
        executable = os.stat(f'/proc/{os.getpid()}/exe')
        python = {row[0] for row in maps if row[1:3] == (executable.st_dev, executable.st_ino)}
        libraries = {row[0] for row in maps if PurePosixPath(row[0]).name.startswith('libsqlite3')}
        libpython = {row[0] for row in maps if PurePosixPath(row[0]).name.startswith('libpython')}
        extension = _sqlite3.__file__
        if len(python) != 1 or len(libraries) != 1 or len(libpython) > 1 or type(extension) is not str:
            _unavailable()
        paths = [('python', next(iter(python))), ('sqlite-extension', extension),
                 ('sqlite-library', next(iter(libraries)))]
        if libpython:
            paths.append(('python-library', next(iter(libpython))))
        objects, selected_maps, needed, total = [], [], (), 0
        for role, path in paths:
            _deadline(end)
            item, dependencies, rows = _measure(role, path, maps, end, max_bytes=_MAX_TOTAL - total)
            objects.append(item)
            selected_maps.extend(rows)
            total += item.size
            if total > _MAX_TOTAL:
                _unavailable()
            if role == 'sqlite-extension':
                needed = dependencies
        sqlite_names = [name for name in needed if re.fullmatch(r'libsqlite3\.so(?:\.[0-9]+)*', name)]
        actual_name = PurePosixPath(paths[2][1]).name
        if (len(sqlite_names) != 1 or not (actual_name == sqlite_names[0]
                or actual_name.startswith(sqlite_names[0] + '.'))):
            _unavailable()
        # Re-open only the mapped names after all hash IO. A replacement at
        # the old pathname must not inherit the inode's earlier measurement.
        for item in objects:
            fd = _safe_open(item.path)
            try:
                if _signature(os.fstat(fd)) != (item.device, item.inode, item.size,
                                                item.mtime_ns, item.ctime_ns):
                    _unavailable()
            finally:
                os.close(fd)
        final_maps = _maps()
        chosen_paths = {path for _, path in paths}
        relevant = tuple(row for row in final_maps if row[0] in chosen_paths
                         or PurePosixPath(row[0]).name.startswith(('libsqlite3', 'libpython')))
        if sorted(relevant) != sorted(selected_maps):
            _unavailable()
        _deadline(end)
        return LoadedSQLiteRuntime(tuple(objects), sqlite3.sqlite_version, sys.version, tuple(sorted(selected_maps)))
    except (OSError, UnicodeError, ValueError, AttributeError, struct.error) as exc:
        raise QueryUnavailable('qualified loaded SQLite runtime unavailable') from exc


def loaded_sqlite_fingerprint(runtime):
    end = _MEASURE_END.get()
    timeout = 2.0 if end is None else min(2.0, end - time.monotonic())
    if (type(runtime) is not LoadedSQLiteRuntime or timeout <= 0
            or capture_loaded_sqlite_runtime(timeout=timeout) != runtime):
        _unavailable()
    # Addresses and inode metadata are mutation fences, not content identity.
    content = {'objects': [(item.role, item.size, item.sha256) for item in runtime.objects],
               'sqlite_version': runtime.sqlite_version, 'python_version': runtime.python_version}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class AdoptedSQLiteFactory:
    """Only the controller constructs this internal original-helper factory.

    control_fence is a pure descriptor/control mutation fence, not another
    adoption observation or a claim of atomicity with unrelated file writers.
    The helper must call it after its own final material IO before publishing.
    """
    def __init__(self, runtime, adoption):
        if (type(runtime) is not LoadedSQLiteRuntime or type(adoption) is not BoundRuntimeAdoption
                or adoption._fingerprint is not loaded_sqlite_fingerprint):
            _unavailable()
        self.runtime, self.adoption = runtime, adoption
        self._descriptor = json.dumps(asdict(runtime), sort_keys=True)
        self._control = adoption._snapshot()
        self._objects = id(runtime), id(adoption)

    def control_fence(self):
        """No ports, provider callbacks, hashing, or filesystem IO here."""
        try:
            return (type(self.runtime) is LoadedSQLiteRuntime
                    and type(self.adoption) is BoundRuntimeAdoption
                    and (id(self.runtime), id(self.adoption)) == self._objects
                    and self.adoption._fingerprint is loaded_sqlite_fingerprint
                    and self.adoption._snapshot() == self._control
                    and json.dumps(asdict(self.runtime), sort_keys=True) == self._descriptor)
        except (AttributeError, TypeError, ValueError):
            return False

    def _check(self, end):
        _deadline(end)
        token = _MEASURE_END.set(end)
        try:
            if not self.control_fence() or self.adoption(self.runtime) is not True:
                _unavailable()
        finally:
            _MEASURE_END.reset(token)
        _deadline(end)
        if not self.control_fence():
            _unavailable()

    def __call__(self, scope, sources, *, revalidate, timeout=2.0):
        if type(timeout) not in (int, float) or not 0 < timeout <= 2:
            _unavailable()
        end = time.monotonic() + timeout
        self._check(end)
        index = ScopedObservationIndex(scope, sources, revalidate=revalidate,
                                       timeout=max(0.000001, end - time.monotonic()))
        try:
            self._check(end)
        except BaseException:
            index.close()
            raise
        return _AdoptedProjection(self, index, timeout, end)


class _AdoptedProjection:
    def __init__(self, factory, index, timeout, end):
        self._factory, self._index, self._timeout, self._end = factory, index, timeout, end

    def __enter__(self):
        return self

    def close(self):
        self._index.close()

    def __exit__(self, exc_type, *_args):
        self.close()
        if exc_type is None:
            self._factory._check(self._end)

    def query(self, text, *, scope, limit=20):
        self._end = time.monotonic() + self._timeout
        try:
            self._factory._check(self._end)
            result = self._index.query(text, scope=scope, limit=limit)
            self._factory._check(self._end)
            return result
        except BaseException:
            self.close()
            raise
