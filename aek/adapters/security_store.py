"""Bounded security carrier using the existing single-file publish/CAS pattern.

The host supplies a trusted, stable repository root and an approved carrier.
This adapter verifies data integrity; write authority is checked by the workflow,
not inferred from a serialized actor label or a valid revision checksum.
"""
from __future__ import annotations

from pathlib import Path
import stat

from aek.adapters.atomic_file import AtomicFileStore
from aek.adapters.work_unit_store import _exclusive_lock
from aek.core.security.model import SecurityInputError, _path
from aek.core.security.record import SecurityRecord, load_security_record, record_as_dict, record_bytes


class SecurityRevisionConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SECURITY_REVISION_CONFLICT")


class SecurityStore:
    def __init__(self, root: Path, record_ref: str) -> None:
        self.root = Path(root)
        self.record_ref = _path(record_ref)
        if self.root.is_symlink() or not self.root.is_dir():
            raise SecurityInputError()
        if self.record_ref.split("/")[-1] != "record.json":
            raise SecurityInputError()
        if len(self.record_ref.split("/")) < 2:
            raise SecurityInputError()

    def _files(self) -> AtomicFileStore:
        directory = self.root
        for part in self.record_ref.split("/")[:-1]:
            directory /= part
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise SecurityInputError()
        return AtomicFileStore(directory)

    def _load(self, files: AtomicFileStore) -> SecurityRecord | None:
        raw = files.read("record.json")
        if raw is None:
            return None
        record = load_security_record(raw)
        if self.record_ref.split("/")[-2] != record.review_id:
            raise SecurityInputError()
        return record

    def load(self) -> SecurityRecord | None:
        try:
            return self._load(self._files())
        except FileNotFoundError:
            return None

    def read(self, record_ref: str) -> SecurityRecord | None:
        if record_ref != self.record_ref:
            raise SecurityInputError()
        return self.load()

    def create(self, record: SecurityRecord) -> None:
        record_bytes(record)
        if self.record_ref.split("/")[-2] != record.review_id or record.revision != 1:
            raise SecurityInputError()
        files = self._files()
        with _exclusive_lock(files.root / "record.lock"):
            current = self._load(files)
            if current is not None:
                if current == record:
                    return
                raise SecurityRevisionConflict()
            files.write("record.json", record_bytes(record))

    def write(self, record: SecurityRecord, *, expected_revision_digest: str) -> None:
        record_bytes(record)
        files = self._files()
        with _exclusive_lock(files.root / "record.lock"):
            current = self._load(files)
            if (current is None or current.revision_digest != expected_revision_digest
                    or record.review_id != current.review_id
                    or record.revision != current.revision + 1):
                raise SecurityRevisionConflict()
            before, after = record_as_dict(current), record_as_dict(record)
            if (after["events"][:len(before["events"])] != before["events"]
                    or len(after["events"]) <= len(before["events"])):
                raise SecurityRevisionConflict()
            files.write("record.json", record_bytes(record))
