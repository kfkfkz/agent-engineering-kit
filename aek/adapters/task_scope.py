"""Git observations for cooperative task scope and local evidence reuse."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from aek.adapters.change_scope import _git, _names, _read_regular
from aek.adapters.evidence import _open_posix, _open_windows


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def validate_path(path: str) -> None:
    _names(path.encode("utf-8") + b"\0")
    if (not path or ":" in path or any(ord(char) < 32 for char in path)
            or any(part.casefold() == ".git" for part in path.split("/"))):
        raise ValueError("expected a repository-relative Git path")


def validate_proposal(root: Path, path: str) -> None:
    validate_path(path)
    candidate = root.joinpath(*path.split("/"))
    if candidate.resolve() != candidate:
        raise ValueError(f"planned path resolves through an alias: {path}")


def checkout_identity(root: Path) -> dict[str, str]:
    def git_text(*args: str, allowed_status: tuple[int, ...] = (0,)) -> str:
        return _git(root, *args, allowed_status=allowed_status).decode("utf-8").rstrip("\r\n")

    if Path(git_text("rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("task scope requires the Git worktree root")
    # The branch is significant even when two refs point at the same commit.
    ref = git_text("symbolic-ref", "--quiet", "HEAD", allowed_status=(0, 1))
    head = git_text("rev-parse", "--verify", "--quiet", "HEAD", allowed_status=(0, 1))
    if not ref and not head:
        raise ValueError("cannot resolve Git worktree HEAD")
    return {
        "root": str(root),
        "git_dir": git_text("rev-parse", "--absolute-git-dir"),
        "ref": ref or "HEAD",
        "head": head,
    }


_DIFF = ("diff", "--no-renames", "--no-ext-diff", "--no-textconv",
         "--ignore-submodules=none", "--src-prefix=a/", "--dst-prefix=b/")
_GENERATED_STATE = tuple(f".repo-memory-kit/{name}/"
                         for name in ("context", "tx", "validation", "zvec"))


def _dirty_sets(root: Path, head: str) -> tuple[tuple[str, ...], ...]:
    return (
        _names(_git(root, *_DIFF, "--name-only", "-z", *_staged_flags(head), "--")),
        _names(_git(root, *_DIFF, "--name-only", "-z", "--")),
        tuple(path for path in _names(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
              if not path.startswith(_GENERATED_STATE)),
    )


def _diff_for_path(root: Path, flags: tuple[str, ...], path: str, *, raw: bool = False) -> bytes:
    mode = ("--raw", "--no-abbrev", "-z") if raw else ()
    return _git(root, "--literal-pathspecs", *_DIFF, *mode, *flags, "--", path)


def _staged_flags(head: str) -> tuple[str, ...]:
    return ("--cached", head) if head else ("--cached",)


def untracked_content(root: Path, path: str) -> bytes:
    validate_path(path)
    return _read_regular(root, path, plain_document=False)


def _file_fingerprint(root: Path, path: str) -> str:
    """Hash a file in bounded memory without following checkout aliases."""
    validate_path(path)
    parts = tuple(path.split("/"))
    fd = (_open_windows(root, parts, require_exact=True) if os.name == "nt"
          else _open_posix(root, parts))
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("scope input must be a regular file")
        checksum = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = file.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("scope input shrank during inspection")
            checksum.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(file.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("scope input mutated during inspection")
    return digest([checksum.hexdigest(), stat.S_IMODE(before.st_mode)])


def observe_scope(
    root: Path, *, baseline: dict[str, str] | None = None,
    collect_diff: bool = False,
) -> tuple[dict[str, str], dict[str, str], tuple[str, ...], bytes]:
    """Observe both layers, including staged edits reverted in the worktree.

    At closeout, only baseline paths need full fingerprints. New paths are
    task candidates, regardless of whether their content can be fingerprinted.
    """
    identity = checkout_identity(root)
    staged, unstaged, untracked = _dirty_sets(root, identity["head"])
    staged_set, unstaged_set, untracked_set = map(set, (staged, unstaged, untracked))
    changes: dict[str, str] = {}
    patches = []
    for path in sorted(staged_set | unstaged_set | untracked_set):
        validate_path(path)
        if baseline is not None and path not in baseline:
            changes[path] = "new"
            if not collect_diff:
                continue
        stage_flags = _staged_flags(identity["head"])
        staged_here = path in staged_set
        unstaged_here = path in unstaged_set
        if baseline is None or path in baseline:
            # Object IDs capture the index without expanding binary patches.
            index_state = _diff_for_path(root, stage_flags, path, raw=True) if staged_here else b""
            worktree_state = _diff_for_path(root, (), path, raw=True) if unstaged_here else b""
            # Git emits a zero worktree mode for a deletion and no working
            # object ID for a live edit. Fingerprint the live bytes ourselves.
            worktree_mode = worktree_state.split(b" ", 2)[1] if worktree_state else b"000000"
            content_digest = None
            if path in untracked_set or worktree_mode != b"000000":
                try:
                    content_digest = _file_fingerprint(root, path)
                except (OSError, ValueError):
                    raise ValueError(f"cannot fingerprint pre-existing path: {path}") from None
            changes[path] = digest([index_state.hex(), worktree_state.hex(), content_digest])
        if collect_diff and path not in (baseline or {}):
            if staged_here:
                patches.append(_diff_for_path(root, stage_flags, path))
            if unstaged_here:
                patches.append(_diff_for_path(root, (), path))
    if ((staged, unstaged, untracked) != _dirty_sets(root, identity["head"])
            or checkout_identity(root) != identity):
        raise ValueError("Git scope changed during inspection")
    return identity, changes, untracked, b"".join(patches)


def evidence_inputs(root: Path, paths: tuple[str, ...]) -> str:
    """Fingerprint the checkout and explicit evidence dependencies.

    With no paths, bind to the Git-visible worktree, including both Git layers.
    With explicit paths, callers must include the full dependency set.
    """
    if not paths:
        identity, changes, _, _ = observe_scope(root)
        return digest([identity, changes])
    identity = checkout_identity(root)
    inputs = {}
    for path in sorted(set(paths)):
        inputs[path] = [
            _file_fingerprint(root, path),
            _diff_for_path(root, _staged_flags(identity["head"]), path, raw=True).hex(),
            _diff_for_path(root, (), path, raw=True).hex(),
        ]
    if checkout_identity(root) != identity:
        raise ValueError("checkout changed during evidence inspection")
    return digest([identity, inputs])


def load_scope(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 1
            or not all(isinstance(data.get(key), dict) for key in ("checkout", "approved", "prior"))
            or not isinstance(data.get("confirmation"), str) or not data["confirmation"]
            or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in data["prior"].values())):
        raise ValueError("invalid task scope snapshot")
    if data.get("digest") != digest({key: value for key, value in data.items() if key != "digest"}):
        raise ValueError("task scope snapshot digest mismatch")
    return data


def save_scope(path: Path, data: dict) -> None:
    """Write a new local journal entry, never overwrite the start snapshot."""
    payload = {key: value for key, value in data.items() if key != "digest"}
    payload["digest"] = digest(payload)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False, sort_keys=True)
        file.write("\n")
