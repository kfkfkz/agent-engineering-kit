"""Small cross-platform atomic store for bounded sidecar files.

The caller owns higher-level locking and transaction ordering.  This adapter
only guarantees that readers observe the old or new complete byte sequence.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile


_BINARY = getattr(os, "O_BINARY", 0)


class AtomicFileInvalid(RuntimeError):
    """The store root or a managed sidecar is unsafe to access."""


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AtomicFileStore:
    """Atomically publish bounded files directly under one trusted directory."""

    def __init__(self, root: Path, *, max_bytes: int = 2_000_000) -> None:
        self.root = Path(root)
        if (type(max_bytes) is not int or max_bytes < 1
                or self.root.is_symlink() or not self.root.is_dir()):
            raise AtomicFileInvalid("atomic store root must be a real directory")
        self.max_bytes = max_bytes

    def _path(self, name: str) -> Path:
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name or Path(name).name != name):
            raise AtomicFileInvalid("atomic sidecar name must be one basename")
        return self.root / name

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise AtomicFileInvalid(f"atomic sidecar is a symlink: {path.name}")

    def read(self, name: str) -> bytes | None:
        path = self._path(name)
        self._reject_symlink(path)
        flags = os.O_RDONLY | _BINARY | getattr(os, "O_NOFOLLOW", 0)
        try:
            before = path.lstat()
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode)
                    or not stat.S_ISREG(opened.st_mode)
                    or (before.st_dev, before.st_ino) !=
                    (opened.st_dev, opened.st_ino)
                    or opened.st_size > self.max_bytes):
                raise AtomicFileInvalid(
                    f"atomic sidecar is not a stable bounded file: {name}")
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    raise AtomicFileInvalid(
                        f"atomic sidecar changed while reading: {name}")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def write(self, name: str, content: bytes) -> None:
        path = self._path(name)
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise ValueError("atomic sidecar content is invalid or oversized")
        self._reject_symlink(path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".aek-atomic-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            remaining = content
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short atomic sidecar write")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._reject_symlink(path)
            os.replace(temporary, path)
            _sync_directory(self.root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def delete(self, name: str) -> None:
        path = self._path(name)
        self._reject_symlink(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _sync_directory(self.root)
