"""Single-writer, crash-recoverable append adapter for context metadata."""
from __future__ import annotations

import json
import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from aek.core.context.telemetry import (
    ContextEvent, ContextEventInput, next_event, validate_events,
)


class LedgerInvalid(ValueError):
    """The committed part of the ledger is malformed; do not append."""


_BINARY = getattr(os, "O_BINARY", 0)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LedgerInvalid("lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | _BINARY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LedgerInvalid("lock is not a regular file")
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


class ContextLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._cached_identity: tuple[int, int, int, int, int] | None = None
        self._cached_last: ContextEvent | None = None
        self._cached_total: int | None = None

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def _open(self) -> int:
        if self.path.is_symlink():
            raise LedgerInvalid("ledger cannot be a symlink")
        flags = os.O_RDWR | os.O_CREAT | _BINARY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise LedgerInvalid("ledger is not a regular file")
        return fd

    @staticmethod
    def _read_rows(fd: int) -> tuple[tuple[ContextEvent, ...], int]:
        size = os.fstat(fd).st_size
        os.lseek(fd, 0, os.SEEK_SET)
        content = bytearray()
        while len(content) < size:
            chunk = os.read(fd, size - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) != size:
            raise LedgerInvalid("ledger changed during locked read")
        committed_end = content.rfind(b"\n") + 1
        lines = content[:committed_end].splitlines()
        rows: list[ContextEvent] = []
        expected = set(ContextEvent.__dataclass_fields__)
        for line in lines:
            try:
                raw = json.loads(line.decode("utf-8"))
                if not isinstance(raw, dict) or set(raw) != expected:
                    raise ValueError("unexpected event fields")
                if line != json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8"):
                    raise ValueError("event encoding is not canonical")
                rows.append(ContextEvent(**raw))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise LedgerInvalid("committed ledger line is invalid") from exc
        try:
            validate_events(tuple(rows))
        except ValueError as exc:
            raise LedgerInvalid("ledger hash chain is invalid") from exc
        return tuple(rows), committed_end

    def read(self) -> tuple[ContextEvent, ...]:
        with _exclusive_lock(self.lock_path):
            fd = self._open()
            try:
                rows, committed_end = self._read_rows(fd)
                if os.fstat(fd).st_size != committed_end:
                    os.ftruncate(fd, committed_end)
                    os.fsync(fd)
                self._cached_identity = self._identity(os.fstat(fd))
                self._cached_last = rows[-1] if rows else None
                self._cached_total = sum(row.delivered_bytes for row in rows)
                return rows
            finally:
                os.close(fd)

    def total_bytes(self) -> int:
        """Return validated delivered bytes, reusing an unchanged stream prefix."""
        with _exclusive_lock(self.lock_path):
            fd = self._open()
            try:
                identity = self._identity(os.fstat(fd))
                if identity == self._cached_identity and self._cached_total is not None:
                    return self._cached_total
                rows, committed_end = self._read_rows(fd)
                if os.fstat(fd).st_size != committed_end:
                    os.ftruncate(fd, committed_end)
                    os.fsync(fd)
                self._cached_identity = self._identity(os.fstat(fd))
                self._cached_last = rows[-1] if rows else None
                self._cached_total = sum(row.delivered_bytes for row in rows)
                return self._cached_total
            finally:
                os.close(fd)

    def append(self, item: ContextEventInput) -> ContextEvent:
        if not isinstance(item, ContextEventInput):
            raise ValueError("append needs a validated event input")
        with _exclusive_lock(self.lock_path):
            fd = self._open()
            try:
                identity = self._identity(os.fstat(fd))
                if identity == self._cached_identity and self._cached_total is not None:
                    committed_end = identity[2]
                    previous = self._cached_last
                    previous_total = self._cached_total
                else:
                    rows, committed_end = self._read_rows(fd)
                    previous = rows[-1] if rows else None
                    previous_total = sum(row.delivered_bytes for row in rows)
                if os.fstat(fd).st_size != committed_end:
                    os.ftruncate(fd, committed_end)
                    os.fsync(fd)
                row = next_event(item, previous)
                line = json.dumps(row.as_dict(), ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8") + b"\n"
                os.lseek(fd, 0, os.SEEK_END)
                while line:
                    line = line[os.write(fd, line):]
                os.fsync(fd)
                self._cached_identity = self._identity(os.fstat(fd))
                self._cached_last = row
                self._cached_total = previous_total + row.delivered_bytes
                return row
            finally:
                os.close(fd)

    def append_text(
        self, content: str, *, route: str, stage: str, source_kind: str,
        channel: str, purpose: str, cache_hit: bool, session_id: str,
        subject_digest: str, logical_path: str, occurred_at: str | None = None,
        note: str = "", duplicate_delivery: bool = False,
    ) -> ContextEvent:
        """Record actual delivered UTF-8 text without writing its body."""
        if not isinstance(content, str):
            raise ValueError("delivered content must be text")
        encoded = content.encode("utf-8", errors="strict")
        item = ContextEventInput(
            route=route, stage=stage, source_kind=source_kind,
            channel=channel, source_digest=hashlib.sha256(encoded).hexdigest(),
            delivered_bytes=len(encoded), delivered_chars=len(content),
            logical_bytes=len(encoded), purpose=purpose, cache_hit=cache_hit,
            session_id=session_id, subject_digest=subject_digest,
            logical_path=logical_path,
            occurred_at=occurred_at or datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            note=note, duplicate_delivery=duplicate_delivery,
        )
        return self.append(item)
