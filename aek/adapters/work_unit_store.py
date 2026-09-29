"""Cross-platform locked AtomicFile persistence for WorkUnit snapshots."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re

from aek.adapters.atomic_file import AtomicFileStore
from aek.core.work_unit import (
    WorkUnitSnapshot,
    decode_work_unit,
    work_unit_as_dict,
)


_WORK_UNIT_ID = re.compile(r"^[0-9a-f]{64}$")
_BINARY = getattr(os, "O_BINARY", 0)


class WorkUnitConflict(RuntimeError):
    """The persisted snapshot changed after the caller observed it."""


@contextmanager
def _exclusive_lock(path: Path):
    if path.is_symlink():
        raise ValueError("work unit lock must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT | _BINARY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class WorkUnitStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("work unit root must be a real directory")

    def _directory(self, work_unit_id: str, *, create: bool) -> Path:
        if not isinstance(work_unit_id, str) or not _WORK_UNIT_ID.fullmatch(work_unit_id):
            raise ValueError("work unit id must be SHA-256")
        directory = self.root / work_unit_id
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("work unit directory must be a real directory")
        return directory

    @staticmethod
    def _bytes(snapshot: WorkUnitSnapshot) -> bytes:
        return json.dumps(work_unit_as_dict(snapshot), ensure_ascii=False,
                          sort_keys=True, indent=2).encode("utf-8")

    @staticmethod
    def _load_from(files: AtomicFileStore) -> WorkUnitSnapshot | None:
        raw = files.read("state.json")
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("work unit state is malformed") from exc
        return decode_work_unit(payload)

    def load(self, work_unit_id: str) -> WorkUnitSnapshot | None:
        if not isinstance(work_unit_id, str) or not _WORK_UNIT_ID.fullmatch(work_unit_id):
            raise ValueError("work unit id must be SHA-256")
        candidate = self.root / work_unit_id
        if not candidate.exists() and not candidate.is_symlink():
            return None
        directory = self._directory(work_unit_id, create=False)
        return self._load_from(AtomicFileStore(directory))

    def create(self, snapshot: WorkUnitSnapshot) -> None:
        directory = self._directory(snapshot.work_unit_id, create=True)
        files = AtomicFileStore(directory)
        with _exclusive_lock(directory / "state.lock"):
            current = self._load_from(files)
            if current is not None:
                if current == snapshot:
                    return
                raise WorkUnitConflict("work unit already exists")
            files.write("state.json", self._bytes(snapshot))

    def write(
        self, snapshot: WorkUnitSnapshot, *,
        expected_snapshot_digest: str,
    ) -> None:
        directory = self._directory(snapshot.work_unit_id, create=False)
        files = AtomicFileStore(directory)
        with _exclusive_lock(directory / "state.lock"):
            current = self._load_from(files)
            if current is None or current.snapshot_digest != expected_snapshot_digest:
                raise WorkUnitConflict("work unit compare-and-swap failed")
            files.write("state.json", self._bytes(snapshot))
