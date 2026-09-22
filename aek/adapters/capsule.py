"""Atomic, discardable Context Capsule cache."""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

from aek.core.context.capsule import (
    CapsuleClaim, CapsuleSource, ContextCapsule, build_capsule, capsule_as_dict,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 1024 * 1024


class CapsuleInvalid(ValueError):
    """A cache entry is malformed and must not be consumed."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise CapsuleInvalid("duplicate capsule field")
        out[key] = value
    return out


def decode_capsule(payload: bytes) -> ContextCapsule:
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
        expected = {"schema_version", "capsule_id", "subject_digest", "stage",
                    "profile_version", "sources", "claims"}
        if not isinstance(raw, dict) or set(raw) != expected or raw["schema_version"] != 1:
            raise ValueError("unexpected capsule schema")
        if not isinstance(raw["sources"], list) or not isinstance(raw["claims"], list):
            raise ValueError("capsule collections must be arrays")
        sources = tuple(CapsuleSource(**row) for row in raw["sources"])
        claims = tuple(CapsuleClaim(
            claim_id=row["claim_id"], summary=row["summary"],
            source_paths=tuple(row["source_paths"])) for row in raw["claims"])
        capsule = build_capsule(
            raw["subject_digest"], raw["stage"], raw["profile_version"],
            sources, claims)
        if capsule.capsule_id != raw["capsule_id"]:
            raise ValueError("capsule digest mismatch")
        canonical = json.dumps(capsule_as_dict(capsule), ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")).encode("utf-8")
        if payload != canonical:
            raise ValueError("capsule encoding is not canonical")
        return capsule
    except (CapsuleInvalid, UnicodeDecodeError, json.JSONDecodeError,
            KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, CapsuleInvalid):
            raise
        raise CapsuleInvalid("invalid capsule cache entry") from exc


class CapsuleStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, capsule_id: str) -> Path:
        if not isinstance(capsule_id, str) or not _DIGEST.fullmatch(capsule_id):
            raise ValueError("invalid capsule ID")
        return self.root / f"{capsule_id}.json"

    def _ensure_safe_root(self, *, create: bool) -> None:
        # The runtime cache always lives at <repo>/.repo-memory-kit/context/capsules.
        # Check every managed container before mkdir so a project-controlled
        # symlink cannot redirect cache publication outside the repository.
        managed = (self.root.parent.parent, self.root.parent, self.root)
        for path in managed:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise CapsuleInvalid("capsule cache container is unsafe")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            for path in managed:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise CapsuleInvalid("capsule cache container changed")

    def load(self, capsule_id: str) -> ContextCapsule | None:
        self._ensure_safe_root(create=False)
        path = self._path(capsule_id)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                raise CapsuleInvalid("capsule cache entry is unsafe")
            chunks = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        except OSError as exc:
            raise CapsuleInvalid("capsule cache entry is unreadable") from exc
        finally:
            os.close(fd)
        if len(payload) != info.st_size:
            raise CapsuleInvalid("capsule cache entry changed during read")
        capsule = decode_capsule(payload)
        if capsule.capsule_id != capsule_id:
            raise CapsuleInvalid("capsule filename does not match content")
        return capsule

    def publish(self, capsule: ContextCapsule) -> Path:
        if not isinstance(capsule, ContextCapsule):
            raise ValueError("publish needs a ContextCapsule")
        self._ensure_safe_root(create=True)
        target = self._path(capsule.capsule_id)
        payload = json.dumps(capsule_as_dict(capsule), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        temp = self.root / f".{capsule.capsule_id}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temp, target)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        return target
