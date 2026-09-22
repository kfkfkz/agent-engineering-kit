"""Repository-bound EvidenceRef byte verification.

This adapter establishes source identity and freshness. It does not decide
whether a scanner's negative claim is complete or true; only trusted producer
implementations may construct ImpactFact observations from verified bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from aek.core.planning.facts import FACT_CATALOG


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_CODES = {"SOURCE_INSPECTED", "DIFF_SCOPE", "RULE_MATCH", "HUMAN_REVIEW"}
_PRODUCERS = {producer for spec in FACT_CATALOG.values()
              for producer in spec.required_producers}
_MAX_BYTES = 2_000_000


class EvidenceInvalid(ValueError):
    """The source cannot support an ImpactFact claim."""


@dataclass(frozen=True)
class EvidenceRef:
    logical_path: str
    source_digest: str
    subject_digest: str
    producer_id: str
    source_kind: str
    summary_code: str


@dataclass(frozen=True)
class VerifiedEvidenceRef:
    ref_id: str
    logical_path: str
    source_digest: str
    subject_digest: str
    producer_id: str
    source_kind: str
    summary_code: str
    content_bytes: int


def _components(ref: EvidenceRef) -> tuple[str, ...]:
    if not isinstance(ref, EvidenceRef):
        raise EvidenceInvalid("evidence reference must be typed")
    path = ref.logical_path
    if (not isinstance(path, str) or not path or len(path) > 300
            or path.startswith(("/", "\\")) or "\\" in path or ":" in path
            or any(ord(char) < 32 for char in path)):
        raise EvidenceInvalid("evidence path is unsafe")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise EvidenceInvalid("evidence path is unsafe")
    for name in ("source_digest", "subject_digest"):
        value = getattr(ref, name)
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise EvidenceInvalid(f"invalid {name}")
    if (not isinstance(ref.producer_id, str)
            or ref.producer_id not in _PRODUCERS
            or ref.source_kind != "repo_file"
            or not isinstance(ref.summary_code, str)
            or ref.summary_code not in _SUMMARY_CODES):
        raise EvidenceInvalid("unknown evidence producer, kind or summary code")
    return parts


def _open_posix(root: Path, parts: tuple[str, ...]) -> int:
    # O_NONBLOCK prevents an untrusted FIFO at the final component from
    # hanging the scanner before fstat can reject the non-regular file.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    parent = os.open(root, flags | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], flags, dir_fd=parent)
    finally:
        os.close(parent)


def _open_windows(root: Path, parts: tuple[str, ...], *, require_exact: bool = False) -> int:
    # The handle's resolved final path, not a pre-open Path.resolve(), is the
    # authority. Intermediate reparse points cannot take the read outside root.
    import ctypes
    import msvcrt
    import ntpath

    path = root.joinpath(*parts)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        api = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        api.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                        ctypes.c_uint32, ctypes.c_uint32]
        api.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        size = api(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise EvidenceInvalid("cannot verify Windows evidence handle path")
        final = buffer.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[8:]
        elif final.startswith("\\\\?\\"):
            final = final[4:]
        canonical_root = ntpath.normcase(ntpath.normpath(str(root.resolve())))
        canonical_final = ntpath.normcase(ntpath.normpath(final))
        try:
            inside = ntpath.commonpath((canonical_root, canonical_final)) == canonical_root
        except ValueError:
            inside = False
        if not inside:
            raise EvidenceInvalid("evidence handle escaped the repository")
        if require_exact and canonical_final != ntpath.normcase(ntpath.normpath(str(path))):
            raise EvidenceInvalid("evidence handle traversed a reparse point")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_verified_evidence(
    repository_root: Path, ref: EvidenceRef, *, expected_subject_digest: str,
    max_bytes: int = _MAX_BYTES,
) -> tuple[VerifiedEvidenceRef, bytes]:
    parts = _components(ref)
    if (not isinstance(expected_subject_digest, str)
            or not _DIGEST.fullmatch(expected_subject_digest)
            or ref.subject_digest != expected_subject_digest):
        raise EvidenceInvalid("evidence subject is stale")
    if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES:
        raise EvidenceInvalid("evidence size limit is invalid")
    root = Path(repository_root)
    if root.is_symlink() or not root.is_dir():
        raise EvidenceInvalid("repository root is not a real directory")
    try:
        fd = (_open_windows(root, parts) if os.name == "nt"
              else _open_posix(root, parts))
    except (OSError, ValueError) as exc:
        raise EvidenceInvalid("evidence source cannot be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise EvidenceInvalid("evidence source is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(fd, remaining)
            if not block:
                raise EvidenceInvalid("evidence source changed during read")
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(fd)
        if (after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns):
            raise EvidenceInvalid("evidence source changed during read")
        content = b"".join(chunks)
    finally:
        os.close(fd)
    if hashlib.sha256(content).hexdigest() != ref.source_digest:
        raise EvidenceInvalid("evidence source digest is stale")
    identity = {name: getattr(ref, name) for name in ref.__dataclass_fields__}
    ref_id = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return VerifiedEvidenceRef(ref_id, ref.logical_path, ref.source_digest,
                               ref.subject_digest, ref.producer_id,
                               ref.source_kind, ref.summary_code,
                               len(content)), content
