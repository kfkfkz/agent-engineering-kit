"""Atomic local carrier for codebase dirty state and freshness receipts."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re

from aek.adapters.atomic_file import AtomicFileStore
from aek.core.codebase_context import DirtyState


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class CodebaseStateStore:
    def __init__(self, root: Path, worktree_id: str, work_unit_id: str) -> None:
        if any(not isinstance(value, str) or not _ID.fullmatch(value)
               for value in (worktree_id, work_unit_id)):
            raise ValueError("codebase state scope id is invalid")
        root = Path(root)
        if root.is_symlink():
            raise ValueError("codebase freshness root is a symlink")
        root.mkdir(parents=True, exist_ok=True)
        worktree = root / worktree_id
        worktree.mkdir(exist_ok=True)
        directory = worktree / work_unit_id
        directory.mkdir(exist_ok=True)
        if worktree.is_symlink() or directory.is_symlink():
            raise ValueError("codebase freshness scope is a symlink")
        self.files = AtomicFileStore(directory)

    def load_state(self) -> DirtyState | None:
        raw = self.files.read("state.json")
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
            return DirtyState(
                value["code_identity_digest"], value["dirty_epoch"],
                value["active_batch_id"], tuple(value["changed_paths"]),
                tuple(value["reasons"]), value["refresh_started_epoch"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError,
                json.JSONDecodeError) as exc:
            raise ValueError("codebase dirty state is malformed") from exc

    def write_state(self, state: DirtyState) -> None:
        self.files.write("state.json", json.dumps(
            asdict(state), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))

    def write_receipt(self, receipt: dict[str, object]) -> None:
        self.files.write("receipt.json", json.dumps(
            receipt, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))

    def load_receipt(self) -> dict[str, object] | None:
        raw = self.files.read("receipt.json")
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("codebase freshness receipt is malformed") from exc
        if not isinstance(value, dict):
            raise ValueError("codebase freshness receipt must be an object")
        return value
