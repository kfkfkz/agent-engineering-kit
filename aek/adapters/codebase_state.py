"""Atomic local carrier for codebase dirty state and freshness receipts."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from time import time
from uuid import uuid4

from aek.adapters.atomic_file import AtomicFileStore
from aek.adapters import ui_filesystem
from aek.core.codebase_context import DirtyState


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REFRESH_LEASE_SECONDS = 300


@dataclass(frozen=True)
class RefreshReservation:
    token: str
    expires_at: int
    identity_digest: str
    dirty_epoch: int

    def __post_init__(self) -> None:
        if (not isinstance(self.token, str) or not re.fullmatch(r"[0-9a-f]{32}", self.token)
                or type(self.expires_at) is not int or self.expires_at < 0
                or not isinstance(self.identity_digest, str) or not self.identity_digest
                or type(self.dirty_epoch) is not int or self.dirty_epoch < 0):
            raise ValueError("refresh reservation is malformed")

    def matches(self, state: DirtyState) -> bool:
        return (self.identity_digest == state.code_identity_digest
                and self.dirty_epoch == state.dirty_epoch)

    @property
    def active(self) -> bool:
        return self.expires_at > time()


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

    @contextmanager
    def locked(self):
        directory = ui_filesystem.open_absolute_dir(self.files.root.absolute())
        try:
            with ui_filesystem.exclusive_lock(directory, timeout=5):
                yield
        finally:
            ui_filesystem.close_dir(directory)

    def load_state(self) -> DirtyState | None:
        raw = self.files.read("state.json")
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
            recorded_epoch = value["refresh_started_epoch"]
            version = value.get("schema_version", 1)
            if type(version) is not int or version not in {1, 2}:
                raise ValueError("unsupported codebase state schema")
            state = DirtyState(
                value["code_identity_digest"], value["dirty_epoch"],
                value["active_batch_id"], tuple(value["changed_paths"]),
                tuple(value["reasons"]), recorded_epoch)
            # Version 1 marked the plan as executed before calling the provider.
            return replace(state, refresh_started_epoch=-1) if version == 1 else state
        except (KeyError, TypeError, ValueError, UnicodeDecodeError,
                json.JSONDecodeError) as exc:
            raise ValueError("codebase dirty state is malformed") from exc

    def write_state(self, state: DirtyState) -> None:
        self.files.write("state.json", json.dumps(
            {"schema_version": 2, **asdict(state)}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))

    def load_refresh_reservation(self, state: DirtyState) -> RefreshReservation | None:
        raw = self.files.read("refresh.json")
        if raw is None:
            return None
        try:
            lease = RefreshReservation(**json.loads(raw.decode("utf-8")))
        except (TypeError, ValueError) as exc:
            raise ValueError("refresh reservation is malformed") from exc
        return lease if lease.matches(state) else None

    def reserve_refresh(self, state: DirtyState) -> RefreshReservation:
        lease = RefreshReservation(uuid4().hex, int(time()) + _REFRESH_LEASE_SECONDS,
                                   state.code_identity_digest, state.dirty_epoch)
        self.files.write("refresh.json", json.dumps(asdict(lease), sort_keys=True,
                         separators=(",", ":")).encode("utf-8"))
        return lease

    def cancel_refresh_reservation(self) -> None:
        self.files.delete("refresh.json")

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
