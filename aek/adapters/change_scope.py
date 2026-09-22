"""Conservative Git-visible negative risk producer.

Only documentation-only deltas can currently prove absence of migration and
access-shape changes. Code deltas stay unknown; a keyword search is not proof.
The returned subject must be re-scanned and compared at the authorization edge.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aek.adapters.evidence import _open_windows
from aek.core.planning.facts import FactObservation


_MAX_GIT_OUTPUT = 32_000_000
_MAX_FILE = 2_000_000


@dataclass(frozen=True)
class NegativeRiskScan:
    subject_digest: str
    irreversible: FactObservation
    performance: FactObservation
    changed_paths: tuple[str, ...]
    scope_complete: bool
    observations: tuple[FactObservation, ...]


_DOC_NEGATIVE_PRODUCERS = {
    "public_api_change": ("diff_scanner", "api_surface_parser"),
    "database_change": ("diff_scanner", "sql_config_scanner"),
    "ui_behavior_change": ("diff_scanner", "ui_path_registry"),
    "business_flow_change": ("diff_scanner", "trace_analyzer"),
    "cross_module_change": ("diff_scanner", "trace_analyzer"),
    "security_sensitive": ("security_rules", "diff_scanner"),
    # Conditional negative paths intentionally do not impersonate a human
    # credential, capacity declaration or deep trace/SQL review.
    "irreversible_change": ("migration_operation_scanner",),
    "performance_capacity": ("access_change_scanner",),
}


def _git(root: Path, *args: str) -> bytes:
    try:
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        result = subprocess.run(
            ["git", "-C", str(root), "--no-optional-locks", *args],
            capture_output=True, timeout=20, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("cannot capture Git change scope") from exc
    if result.returncode or len(result.stdout) > _MAX_GIT_OUTPUT:
        raise ValueError("Git change scope is invalid or exceeds the bound")
    return result.stdout


def _names(payload: bytes) -> tuple[str, ...]:
    if payload and not payload.endswith(b"\0"):
        raise ValueError("Git path list is incomplete")
    try:
        paths = tuple(item.decode("utf-8") for item in payload.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise ValueError("Git path is not UTF-8") from exc
    for path in paths:
        parts = path.split("/")
        if (not path or path.startswith("/") or "\\" in path
                or any(part in {"", ".", ".."} for part in parts)):
            raise ValueError("Git path is unsafe")
    return paths


def _read_regular(root: Path, path: str, *, plain_document: bool = True) -> bytes:
    """Read a bounded regular file under no-follow handles."""
    parts = path.split("/")
    if os.name == "nt":
        fd = _open_windows(root, tuple(parts), require_exact=True)
    else:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        parent = os.open(root, flags | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], flags, dir_fd=parent)
        finally:
            os.close(parent)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode)
                or (plain_document and before.st_mode & 0o111)
                or before.st_size > _MAX_FILE):
            raise ValueError("changed file is not bounded plain text")
        content = bytearray()
        while len(content) < before.st_size:
            block = os.read(fd, before.st_size - len(content))
            if not block:
                raise ValueError("changed file became unreadable")
            content.extend(block)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("changed file mutated during scan")
        return bytes(content)
    finally:
        os.close(fd)


def _safe_document(path: str) -> bool:
    return ((path.startswith(("docs/01-需求/", "docs/03-SDD/",
                              "docs/delivery-receipts/"))
             and path.endswith((".md", ".rst")))
            or path in {"README.md", "CHANGELOG.md"})


def _control_state(path: str) -> bool:
    parts = path.split("/")
    return (len(parts) >= 5 and parts[0:2] == ["docs", "03-SDD"]
            and parts[3] == "reviews")


def resolve_base_commit(repository_root: Path, base_ref: str) -> str:
    """Resolve a caller ref to the exact commit identity used by a scan."""
    root = Path(repository_root)
    if (root.is_symlink() or not root.is_dir() or not isinstance(base_ref, str)
            or not base_ref or base_ref.startswith("-")):
        raise ValueError("repository or base ref is invalid")
    base = _git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    base_oid = base.strip().decode("ascii")
    if len(base_oid) not in (40, 64) or any(
            c not in "0123456789abcdef" for c in base_oid):
        raise ValueError("base ref must resolve to a commit")
    return base_oid


def scan_negative_risks(repository_root: Path, base_ref: str) -> NegativeRiskScan:
    """Prove false only for an entirely safe Git-visible documentation delta.

    This is a bounded producer, not a general code scanner. `scope_complete`
    means all changed paths were safely classified into this producer's domain.
    """
    root = Path(repository_root)
    if (root.is_symlink() or not root.is_dir() or not isinstance(base_ref, str)
            or not base_ref or base_ref.startswith("-")):
        raise ValueError("repository or base ref is invalid")
    base_oid = resolve_base_commit(root, base_ref)
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    if Path(os.fsdecode(top)).resolve() != root.resolve():
        raise ValueError("repository root must be Git top level")
    diff_args = ("diff", "--no-renames", "--no-ext-diff", "--no-textconv")
    all_tracked = _names(_git(root, *diff_args, "--name-only", "-z", base_oid, "--"))
    all_untracked = _names(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    tracked = tuple(path for path in all_tracked if not _control_state(path))
    untracked = tuple(path for path in all_untracked if not _control_state(path))
    patch = (_git(root, *diff_args, "--binary", base_oid, "--", *tracked)
             if tracked else b"")
    paths = tuple(sorted(set(tracked + untracked)))
    safe = all(_safe_document(path) for path in paths)
    # Binary, symlink and submodule changes cannot be justified as plain docs.
    if any(marker in patch for marker in (
            b"GIT binary patch", b"Binary files ", b"mode 120000",
            b"mode 160000", b"Subproject commit")):
        safe = False
    untracked_digests: list[tuple[str, str]] = []
    for path in untracked:
        try:
            content = _read_regular(root, path, plain_document=safe)
            if safe and b"\0" not in content:
                content.decode("utf-8")
            elif safe:
                raise ValueError("binary documentation cannot prove a negative")
        except (OSError, UnicodeDecodeError, ValueError):
            safe = False
            untracked_digests.append((path, "unreadable"))
        else:
            untracked_digests.append((path, hashlib.sha256(content).hexdigest()))
    for path, first_digest in untracked_digests:
        if first_digest == "unreadable":
            continue
        try:
            second_digest = hashlib.sha256(
                _read_regular(root, path, plain_document=False)).hexdigest()
        except (OSError, ValueError) as exc:
            raise ValueError("untracked source mutated during scan") from exc
        if first_digest != second_digest:
            raise ValueError("untracked source mutated during scan")
    # A tracked, still-present document can have become a symlink or executable
    # after Git emitted the patch. Reject that path as a negative proof.
    if safe:
        for path in tracked:
            try:
                content = _read_regular(root, path)
                if b"\0" in content:
                    raise ValueError("binary documentation cannot prove a negative")
                content.decode("utf-8")
            except FileNotFoundError:
                continue  # document deletion
            except (OSError, UnicodeDecodeError, ValueError):
                safe = False
                break
    current_tracked = tuple(path for path in _names(_git(
        root, *diff_args, "--name-only", "-z", base_oid, "--"))
        if not _control_state(path))
    current_untracked = tuple(path for path in _names(_git(
        root, "ls-files", "--others", "--exclude-standard", "-z"))
        if not _control_state(path))
    current_patch = (_git(root, *diff_args, "--binary", base_oid, "--",
                          *current_tracked) if current_tracked else b"")
    if (patch != current_patch or tracked != current_tracked
            or untracked != current_untracked):
        raise ValueError("Git change scope mutated during scan")
    canonical = json.dumps({
        "base": base_oid,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "paths": paths, "untracked": untracked_digests,
        "complete": safe,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    subject = hashlib.sha256(canonical).hexdigest()
    evidence = hashlib.sha256(b"aek-negative-risk-scan-v1\0" + canonical).hexdigest()
    state = "false" if safe else "unknown"
    if safe:
        observations = tuple(
            FactObservation(fact_id, producer, "false", subject, evidence, True)
            for fact_id, producers in _DOC_NEGATIVE_PRODUCERS.items()
            for producer in producers)
    else:
        observations = (
            FactObservation("irreversible_change", "migration_operation_scanner",
                            "unknown", subject, evidence, False),
            FactObservation("performance_capacity", "access_change_scanner",
                            "unknown", subject, evidence, False),
        )
    irreversible = next(item for item in observations
                        if item.fact_id == "irreversible_change")
    performance = next(item for item in observations
                       if item.fact_id == "performance_capacity")
    return NegativeRiskScan(subject, irreversible, performance, paths, safe,
                            observations)
