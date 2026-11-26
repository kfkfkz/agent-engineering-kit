"""Bind memory visibility and derived evidence to the actual checkout."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def _git(root: Path, *args: str) -> str | None:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, timeout=5, check=False, env=env,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git memory scope lookup timed out") from exc
    return os.fsdecode(result.stdout.rstrip(b"\r\n")) if result.returncode == 0 else None


def git_worktree_root(path: Path) -> Path | None:
    root = _git(path, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else None


def multiple_worktrees(path: Path) -> bool:
    listing = _git(path, "worktree", "list", "--porcelain", "-z")
    return listing is None or sum(part.startswith("worktree ") for part in listing.split("\0")) != 1


@dataclass(frozen=True)
class MemoryScope:
    root: Path
    worktree_id: str
    ref: str
    git: bool
    legacy_memory: bool
    parents: tuple[str, ...] = ()
    parent_worktrees: tuple[tuple[str, Path], ...] = ()

    @property
    def digest(self) -> str:
        sources = tuple(f"{ref}\0{path}" for ref, path in self.parent_worktrees)
        return _digest(self.worktree_id, self.ref, str(self.legacy_memory), *self.parents, *sources)

    def entry_fields(self) -> dict[str, str]:
        return {"memory_scope": "worktree", "memory_worktree": self.worktree_id,
                "memory_ref": self.ref}

    def allows(self, rel: str, fields: dict, origin_ref: str | None = None) -> bool:
        mode = fields.get("memory_scope")
        ref = fields.get("memory_ref")
        worktree = fields.get("memory_worktree")
        if origin_ref is not None:
            return mode == "inherit" and ref == origin_ref and worktree is None
        if mode is None and ref is None and worktree is None:
            return not rel.startswith("docs/memory/") or self.legacy_memory
        if mode == "project":
            return ref is None and worktree is None
        if mode == "branch":
            return (self.ref.startswith("refs/heads/") and ref == self.ref
                    and worktree is None)
        if mode == "inherit":
            return (self.ref.startswith("refs/heads/") and worktree is None
                    and (ref == self.ref or (ref in self.parents and ref not in dict(self.parent_worktrees))))
        return mode == "worktree" and ref == self.ref and worktree == self.worktree_id


def _parent_refs(root: Path, ref: str, primary: str | None) -> tuple[str, ...]:
    parents: list[str] = []
    current = ref
    prefix = "refs/heads/"
    while current.startswith(prefix):
        branch = current[len(prefix):]
        configured = _git(root, "config", "--get", f"branch.{branch}.aekMemoryParent")
        if configured is None:
            parent = primary if current != primary else None
        else:
            parent = configured if configured.startswith(prefix) else prefix + configured
            if _git(root, "show-ref", "--verify", "--quiet", parent) is None:
                raise ValueError("configured Memory parent is not a local Git branch")
        if parent is None:
            break
        if parent == ref or parent in parents or len(parents) >= 16:
            raise ValueError("Memory branch parents form a cycle or exceed the depth limit")
        parents.append(parent)
        current = parent
    return tuple(parents)


def _parent_worktrees(root: Path, parents: tuple[str, ...]) -> tuple[tuple[str, Path], ...]:
    if not parents:
        return ()
    listing = _git(root, "worktree", "list", "--porcelain", "-z")
    if listing is None:
        raise ValueError("cannot verify Memory parent worktrees")
    by_ref: dict[str, list[Path]] = {}
    for record in listing.split("\0\0"):
        fields = record.split("\0")
        if not fields[0].startswith("worktree ") or any(field.startswith("prunable") for field in fields):
            continue
        ref = next((field[7:] for field in fields if field.startswith("branch ")), None)
        if ref in parents:
            path = Path(fields[0][9:]).resolve()
            if path.is_dir() and path != root:
                by_ref.setdefault(ref, []).append(path)
    # A forced duplicate checkout does not provide a unique live source.
    return tuple((ref, by_ref[ref][0]) for ref in parents if len(by_ref.get(ref, ())) == 1)


def resolve_memory_scope(repo: Path) -> MemoryScope:
    root = repo.resolve()
    identity = _git(root, "rev-parse", "--show-toplevel", "--absolute-git-dir",
                    "--path-format=absolute", "--git-common-dir")
    if identity is None:
        if (root / ".git").exists() or (root / ".git").is_symlink():
            raise ValueError("cannot verify Git worktree for Memory")
        return MemoryScope(root, _digest(str(root)), "local", False, True)
    top, git_dir, common_dir = identity.split("\n")
    git_path = Path(git_dir.rstrip("\r")).resolve()
    common_path = Path(common_dir).resolve()
    # Preserve explicit scopes inside monorepos while binding their identity to
    # the actual Git worktree. MCP separately checks its checkout root.
    worktree_id = _digest(str(root), str(Path(top.rstrip("\r")).resolve()), str(git_path))
    ref = _git(root, "symbolic-ref", "--quiet", "HEAD")
    if ref is None:
        commit = _git(root, "rev-parse", "--verify", "HEAD")
        if not commit:
            raise ValueError("cannot verify detached HEAD for Memory")
        return MemoryScope(root, worktree_id, "detached:" + commit, True, False)
    # Legacy notes had no origin. Only the primary default branch retains the
    # old local behavior. Linked worktrees or switched branches need a scope.
    default = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    default_prefix = "refs/remotes/origin/"
    if default and default.startswith(default_prefix):
        primary_ref = "refs/heads/" + default[len(default_prefix):]
    else:
        main = "refs/heads/main"
        has_main = ref == main or _git(root, "show-ref", "--verify", "--quiet", main) is not None
        master = "refs/heads/master"
        has_master = ref == master or _git(root, "show-ref", "--verify", "--quiet", master) is not None
        primary_ref = main if has_main else master if has_master else None
    parents = _parent_refs(root, ref, primary_ref)
    return MemoryScope(root, worktree_id, ref, True,
                       git_path == common_path and ref == primary_ref,
                       parents, _parent_worktrees(root, parents))
