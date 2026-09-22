"""Recoverable, bounded sidecar transaction for ArtifactPlan files.

Only Registry-derived plan/credential/binding names may be changed. A journal
contains exact preimages and intended bytes; recovery refuses any unexpected
target or unrelated consumer change. The document gate has not yet activated
this store for production decisions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from aek.core.artifact.registry import ARTIFACT_REGISTRY


class PlanConflict(RuntimeError):
    """The caller's consumer-set CAS no longer matches."""


class PlanInvalid(RuntimeError):
    """A journal or externally changed file prevents safe recovery."""


_PLAN = "artifact-plan.json"
_CREDENTIAL = "artifact-plan.gate.json"
_JOURNAL = "artifact-plan.tx.json"
_LOCK = ".artifact-plan.lock"
_MUTABLE = frozenset({_PLAN, _CREDENTIAL} | {
    f"{group_id}.plan-binding.json" for group_id in ARTIFACT_REGISTRY.groups
})
_MAX_FILE = 2_000_000
_BINARY = getattr(os, "O_BINARY", 0)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _fsync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _lock_file(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise PlanInvalid("plan lock is a symlink")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _BINARY
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PlanInvalid("plan lock is not a regular file")
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class PlanStore:
    def __init__(self, reviews: Path) -> None:
        self.reviews = Path(reviews)
        if self.reviews.is_symlink() or not self.reviews.is_dir():
            raise PlanInvalid("reviews must be an existing, real directory")

    @contextmanager
    def legacy_gate_lock(self) -> Iterator[None]:
        """Serialize legacy gate consumers with dynamic sidecar transactions.

        Callers must recheck dynamic sidecars *inside* this lock before writing.
        An interrupted plan journal is deliberately not recovered here: the
        legacy path must refuse it rather than infer a static plan.
        """
        with _lock_file(self.reviews / _LOCK):
            yield

    @contextmanager
    def gate_snapshot(self) -> Iterator[dict[str, bytes]]:
        """Serialize a gate write with plan transactions and expose its state.

        Gate callers must evaluate dynamic sidecars from the same lock epoch in
        which they write the legacy-compatible gate record.  An interrupted
        plan transaction is not recovered here: a gate may not guess whether
        the preimage or intended plan was authoritative.
        """
        with _lock_file(self.reviews / _LOCK):
            if self._journal() is not None:
                raise PlanInvalid("interrupted plan transaction blocks gate write")
            if any(path.name.startswith(".artifact-plan-write-")
                   for path in self.reviews.iterdir()):
                raise PlanInvalid("orphan plan publish temporary file")
            result: dict[str, bytes] = {}
            for name in self._consumer_names():
                if name not in _MUTABLE:
                    continue
                content = self._read(name)
                if content is not None:
                    result[name] = content
            yield result

    def _read(self, name: str) -> bytes | None:
        path = self.reviews / name
        if path.is_symlink():
            raise PlanInvalid(f"sidecar is a symlink: {name}")
        try:
            fd = os.open(path, os.O_RDONLY | _BINARY
                         | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
                raise PlanInvalid(f"sidecar is not a bounded regular file: {name}")
            chunks: list[bytes] = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    raise PlanInvalid(f"sidecar changed during read: {name}")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _atomic_write(self, name: str, content: bytes) -> None:
        if len(content) > _MAX_FILE:
            raise ValueError("plan sidecar exceeds size limit")
        fd, temporary = tempfile.mkstemp(prefix=".artifact-plan-write-", dir=self.reviews)
        try:
            remaining = content
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short sidecar write")
                remaining = remaining[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, self.reviews / name)
            _fsync_dir(self.reviews)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _consumer_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for path in self.reviews.iterdir():
            name = path.name
            if name in {_JOURNAL, _LOCK} or name.startswith(".artifact-plan-write-"):
                continue
            if name in {_PLAN, _CREDENTIAL} or name.endswith(
                    (".gate.json", ".plan-binding.json")):
                if name.endswith(".plan-binding.json") and name not in _MUTABLE:
                    raise PlanInvalid(f"unknown plan binding consumer: {name}")
                names.append(name)
        return tuple(sorted(names))

    def _consumer_digest(self, exclude: frozenset[str] = frozenset()) -> str:
        values = []
        for name in self._consumer_names():
            if name in exclude:
                continue
            content = self._read(name)
            if content is not None:
                values.append((name, _sha(content)))
        return _sha(_json_bytes(values))

    def consumer_digest(self) -> str:
        with _lock_file(self.reviews / _LOCK):
            self._recover_locked()
            return self._consumer_digest()

    def snapshot(self) -> dict[str, bytes]:
        """Recover and read all dynamic consumers under one plan lock.

        A stranded temporary file is not a valid plan state. Callers may
        inspect it manually, but must not silently fall back to legacy gates.
        """
        return self.snapshot_with_digest()[0]

    def snapshot_with_digest(self) -> tuple[dict[str, bytes], str]:
        """Return sidecars and their CAS consumer digest from one locked view."""
        with _lock_file(self.reviews / _LOCK):
            self._recover_locked()
            if any(path.name.startswith(".artifact-plan-write-")
                   for path in self.reviews.iterdir()):
                raise PlanInvalid("orphan plan publish temporary file")
            names = self._consumer_names()
            result: dict[str, bytes] = {}
            for name in names:
                if name not in _MUTABLE:
                    continue
                content = self._read(name)
                if content is not None:
                    result[name] = content
            return result, self._consumer_digest()

    def _journal(self) -> dict[str, object] | None:
        content = self._read(_JOURNAL)
        if content is None:
            return None
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanInvalid("plan journal is malformed") from exc
        if content != _json_bytes(raw):
            raise PlanInvalid("plan journal is not canonical JSON")
        if (not isinstance(raw, dict) or set(raw) != {
                "schema_version", "tx_id", "op", "phase", "other_consumers_digest",
                "entries"} or raw["schema_version"] != 1
                or raw["op"] not in {"publish", "rollback-all"}
                or raw["phase"] not in {"PREPARED", "COMMITTED"}
                or not isinstance(raw["tx_id"], str)
                or not isinstance(raw["entries"], list) or not raw["entries"]):
            raise PlanInvalid("plan journal has unknown schema or fields")
        try:
            uuid.UUID(raw["tx_id"])
        except (ValueError, AttributeError) as exc:
            raise PlanInvalid("plan journal transaction ID is invalid") from exc
        if not isinstance(raw["other_consumers_digest"], str) or len(
                raw["other_consumers_digest"]) != 64:
            raise PlanInvalid("plan journal consumer digest is invalid")
        names: set[str] = set()
        for row in raw["entries"]:
            if (not isinstance(row, dict) or set(row) != {
                    "name", "preimage", "preimage_digest", "intended", "intended_digest"}
                    or row["name"] not in _MUTABLE or row["name"] in names):
                raise PlanInvalid("plan journal entry is unsafe or duplicate")
            names.add(row["name"])
            for field in ("preimage", "intended"):
                encoded = row[field]
                digest = row[field + "_digest"]
                if encoded is None:
                    if digest is not None:
                        raise PlanInvalid("plan journal null digest mismatch")
                else:
                    if not isinstance(encoded, str) or not isinstance(digest, str):
                        raise PlanInvalid("plan journal image is invalid")
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                    except (ValueError, base64.binascii.Error) as exc:
                        raise PlanInvalid("plan journal base64 is invalid") from exc
                    if len(decoded) > _MAX_FILE or _sha(decoded) != digest:
                        raise PlanInvalid("plan journal image digest is invalid")
        return raw

    @staticmethod
    def _decode(value: str | None) -> bytes | None:
        return base64.b64decode(value) if value is not None else None

    def _remove_journal(self) -> None:
        os.unlink(self.reviews / _JOURNAL)
        _fsync_dir(self.reviews)

    def _apply(self, name: str, intended: bytes | None, preimage_digest: str | None) -> None:
        current = self._read(name)
        current_digest = _sha(current) if current is not None else None
        if current_digest != preimage_digest:
            raise PlanInvalid(f"sidecar changed before publish: {name}")
        if intended is None:
            if current is not None:
                os.unlink(self.reviews / name)
                _fsync_dir(self.reviews)
        else:
            self._atomic_write(name, intended)

    def _recover_locked(self) -> str:
        journal = self._journal()
        if journal is None:
            return "NONE"
        entries = journal["entries"]
        names = frozenset(row["name"] for row in entries)
        if self._consumer_digest(names) != journal["other_consumers_digest"]:
            raise PlanInvalid("plan consumers changed during interrupted transaction")
        states = []
        for row in entries:
            current = self._read(row["name"])
            digest = _sha(current) if current is not None else None
            if digest not in {row["preimage_digest"], row["intended_digest"]}:
                raise PlanInvalid(f"sidecar is neither preimage nor intended: {row['name']}")
            states.append(digest == row["intended_digest"])
        if journal["phase"] == "COMMITTED":
            if not all(states):
                raise PlanInvalid("committed plan journal has an incomplete target")
            self._remove_journal()
            return "CLEANED"
        if not any(states):
            self._remove_journal()
            return "ROLLED_BACK"
        for row, is_intended in zip(entries, states):
            if not is_intended:
                self._apply(row["name"], self._decode(row["intended"]),
                            row["preimage_digest"])
        journal["phase"] = "COMMITTED"
        self._atomic_write(_JOURNAL, _json_bytes(journal))
        self._remove_journal()
        return "ROLLED_FORWARD"

    def recover(self) -> str:
        with _lock_file(self.reviews / _LOCK):
            return self._recover_locked()

    def _commit(
        self, changes: Mapping[str, bytes | None], *, expected_consumers: str,
        operation: str, fault_after: int | None,
    ) -> None:
        if (not isinstance(changes, Mapping) or not changes
                or any(name not in _MUTABLE or (value is not None and
                        (not isinstance(value, bytes) or len(value) > _MAX_FILE))
                       for name, value in changes.items())):
            raise ValueError("plan changes contain unsafe name or bytes")
        if not isinstance(expected_consumers, str) or len(expected_consumers) != 64:
            raise ValueError("expected consumer digest is invalid")
        if fault_after is not None and (type(fault_after) is not int or fault_after < 0):
            raise ValueError("invalid fault injection step")
        with _lock_file(self.reviews / _LOCK):
            self._recover_locked()
            if self._consumer_digest() != expected_consumers:
                raise PlanConflict("plan consumer set changed")
            effective = {name: value for name, value in changes.items()
                         if self._read(name) != value}
            if not effective:
                return
            if operation == "publish":
                names = sorted(effective, key=lambda name: (
                    0 if name == _PLAN else 1 if name == _CREDENTIAL else 2,
                    name))
            else:
                names = sorted(effective, key=lambda name: (
                    0 if name.endswith(".plan-binding.json") else
                    1 if name == _CREDENTIAL else 2, name))
            entries = []
            for name in names:
                prior = self._read(name)
                intended = effective[name]
                entries.append({
                    "name": name,
                    "preimage": base64.b64encode(prior).decode("ascii") if prior is not None else None,
                    "preimage_digest": _sha(prior) if prior is not None else None,
                    "intended": base64.b64encode(intended).decode("ascii")
                    if intended is not None else None,
                    "intended_digest": _sha(intended) if intended is not None else None,
                })
            journal: dict[str, object] = {
                "schema_version": 1, "tx_id": str(uuid.uuid4()), "op": operation,
                "phase": "PREPARED", "other_consumers_digest":
                self._consumer_digest(frozenset(names)), "entries": entries,
            }
            self._atomic_write(_JOURNAL, _json_bytes(journal))
            if fault_after == 0:
                raise RuntimeError("injected crash before first plan publish")
            for index, row in enumerate(entries, 1):
                self._apply(row["name"], self._decode(row["intended"]),
                            row["preimage_digest"])
                if fault_after == index:
                    raise RuntimeError(f"injected crash after plan publish {index}")
            journal["phase"] = "COMMITTED"
            self._atomic_write(_JOURNAL, _json_bytes(journal))
            self._remove_journal()

    def publish(
        self, changes: Mapping[str, bytes | None], *, expected_consumers: str,
        fault_after: int | None = None,
    ) -> None:
        self._commit(changes, expected_consumers=expected_consumers,
                     operation="publish", fault_after=fault_after)

    def rollback_all(
        self, *, expected_consumers: str, fault_after: int | None = None,
    ) -> None:
        # Recovery must precede the no-op decision. A crash after the last
        # deletion leaves no target files but still leaves a PREPARED journal.
        with _lock_file(self.reviews / _LOCK):
            self._recover_locked()
            present = {name: None for name in _MUTABLE
                       if self._read(name) is not None}
        if not present:
            return
        self._commit(present, expected_consumers=expected_consumers,
                     operation="rollback-all", fault_after=fault_after)
