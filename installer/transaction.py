"""transaction.py — 事务模型（§11）：互斥锁 + 事务生命周期 + HMAC + 校验 + 崩溃恢复。

三条写序不变量（崩溃恢复正确性的根基，§11.1）：
1. 完整 plan 先于受管资源副作用（基础设施目录例外：锁目录与 tx/<id> 容器目录
   允许在 plan 落盘前创建——幂等、无资源副作用，否则首次安装 ENOENT）
2. committing 先于首个 replace / rename / unlink
3. 外部副作用意图先行（codex link：intent 落盘 fsync → unlink → 记完成）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat as stat_module
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from . import platform as _plat
from .manifest import MANIFEST_SPEC_ID, Manifest, build_manifest_from_tx
from .registry import (
    _SPEC_ORDER,
    CodexLinkSpec,
    codex_copy_members,
    codex_copy_member,
    SecurityError,
    derive_destination,
    derive_transaction_paths,
    file_sha256,
    resolve_spec,
    secure_mkdir,
    secure_open,
    secure_replace,
    secure_rmdir,
    secure_unlink,
    staged_rel,
    validate_tx_id,
    write_all,
    trusted_canonical_hashes,
    current_kit_hash,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")

TransactionStatus = Literal[
    "planning",       # 内容生成与 pre 状态采集全部在内存完成后，一次性写入完整 plan
    "staging",        # staged 文件落盘中（纯 I/O，内容已定）。崩溃 → 全部可安全清理
    "committing",     # 完整 plan 已落盘。★ 此状态落盘必须发生在第一个 replace/rename/unlink 之前
    "done",           # 全部完成，manifest 已写/缩减（install）或已删（完整 uninstall）
    "rolled_back",    # 已回滚（含外部步骤补偿）
    "failed",         # 可安全清理（无已提交项；外部步骤已补偿）
    "needs_human",    # ★ 会被 find_unresolved 扫描到并阻断一切生命周期操作
]

UNRESOLVED_STATUSES = ("planning", "staging", "committing", "needs_human")


@dataclass
class CommitStep:
    spec_id: str                    # 组锚 spec_id（必须等于 spec_ids[0]）
    spec_ids: tuple[str, ...]       # 组内全部 spec（含锚，第一项 = 锚，按 Registry 顺序）
    kind: Literal["replace", "delete"]  # delete = 提交后目标文件应不存在
    pre_container_hash: str | None  # plan 时点整个目标文件 hash（None=当时不存在）
    post_container_hash: str | None # 提交后整个目标文件 hash；kind="delete" 时必须为 None
    pre_mode: int | None            # plan 时点文件权限
    committed: bool = False
    # 注意：不存 destination_path、staged_path（从 resolve_spec + tx_id 派生）；
    # spec_id / spec_ids 允许指向 STATE_REGISTRY（如 state.manifest）

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "spec_ids": list(self.spec_ids),
            "kind": self.kind,
            "pre_container_hash": self.pre_container_hash,
            "post_container_hash": self.post_container_hash,
            "pre_mode": self.pre_mode,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CommitStep":
        return cls(
            spec_id=str(data["spec_id"]),
            spec_ids=tuple(str(s) for s in data["spec_ids"]),
            kind=str(data["kind"]),
            pre_container_hash=data["pre_container_hash"],
            post_container_hash=data["post_container_hash"],
            pre_mode=data["pre_mode"],
            committed=bool(data["committed"]),
        )


@dataclass
class ExternalStep:
    """target 外的副作用——统一外部事务模型（第七轮审计重构）。
    意图先行：state="intent" 落盘 fsync → 执行 → state="applied" 落盘。
    - operation="remove"（卸载）/ "create"（安装）
    - strategy="symlink" | "copy"（恢复不得改变产物类型）
    - expected_hash = plan 时点 kit 源 SKILL.md 的 sha256（copy 血统——崩溃恢复
      时 kit 源可能已删，必须由记录携带）
    恢复以磁盘现状为准（§11.7），state 是线索不是事实。"""
    spec_id: str
    skill_name: str
    codex_root: str                # 本次卸载 CLI 重传的 --codex-root（HMAC 保护下可回放）
    prior_state: Literal["absent", "pointing_to_target", "content_matches", "other"]
    state: Literal["intent", "applied"] = "intent"
    operation: Literal["create", "remove"] = "remove"
    strategy: Literal["symlink", "copy"] = "symlink"
    expected_hash: str | None = None
    member_spec_id: str | None = None
    prior_hash: str | None = None

    def to_dict(self) -> dict:
        result = {
            "spec_id": self.spec_id,
            "skill_name": self.skill_name,
            "codex_root": self.codex_root,
            "prior_state": self.prior_state,
            "state": self.state,
            "operation": self.operation,
            "strategy": self.strategy,
            "expected_hash": self.expected_hash,
        }
        # Omitting new fields for legacy steps preserves their signed payload.
        if self.member_spec_id is not None:
            result.update(member_spec_id=self.member_spec_id, prior_hash=self.prior_hash)
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "ExternalStep":
        # "removed" 是 v1 语义（等价 applied）——**不得在此映射**：MAC 覆盖原始
        # 载荷，from_dict 必须字节级往返；等价性在消费点处理
        return cls(
            spec_id=str(data["spec_id"]),
            skill_name=str(data["skill_name"]),
            codex_root=str(data["codex_root"]),
            prior_state=str(data["prior_state"]),   # type: ignore[arg-type]
            state=str(data.get("state", "intent")),  # type: ignore[arg-type]
            operation=str(data.get("operation") or "remove"),  # type: ignore[arg-type]
            strategy=str(data.get("strategy") or "symlink"),    # type: ignore[arg-type]
            expected_hash=data.get("expected_hash"),
            member_spec_id=data.get("member_spec_id"),
            prior_hash=data.get("prior_hash"),
        )


def _copy_paths(target: Path, step: ExternalStep):
    parent = resolve_spec(step.spec_id)
    if parent.resource_type != "codex_link" or parent.link_skill_name != step.skill_name:
        raise SecurityError("copy parent does not match Registry")
    member = codex_copy_member(step.skill_name, step.member_spec_id)
    root = Path(step.codex_root)
    CodexLinkSpec(step.skill_name).validate(root, target)
    return root, member


def _copy_bytes(root: Path, relative: str) -> bytes | None:
    try:
        fd = secure_open(root, relative, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise SecurityError("copy member is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _copy_hash(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _copy_isolation_name(member, tx_id: str) -> str:
    return f".kit-copy-old-{tx_id}-{PurePosixPath(member.destination_path).name}"


def settle_copy_isolation(root: Path, member, step, tx_id: str) -> None:
    if _plat.security_level() != "strict":
        return
    relative = PurePosixPath(member.destination_path)
    try:
        fd = _plat.secure_walk_dir_fd(root, str(relative.parent))
    except FileNotFoundError:
        return
    try:
        isolation = _copy_isolation_name(member, tx_id)
        try:
            isolated = _hash_regular_at(fd, isolation)
        except FileNotFoundError:
            return
        allowed = {step.prior_hash, step.expected_hash} - {None}
        if isolated not in allowed:
            raise SecurityError("copy isolation digest conflicts")
        try:
            current = _hash_regular_at(fd, relative.name)
        except FileNotFoundError:
            _plat.rename_noreplace(fd, isolation, fd, relative.name)
        else:
            if current not in allowed:
                raise SecurityError("copy destination and isolation conflict")
            os.unlink(isolation, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_backup_rel(tx_id: str, step: ExternalStep) -> str:
    validate_tx_id(tx_id)
    codex_copy_member(step.skill_name, step.member_spec_id)
    return f".repo-memory-kit/tx/{tx_id}/external-backups/{step.member_spec_id}"


def prepare_copy_members(target: Path, root: Path, tx, parent, *, operation="create",
                         expected_contents: dict[str, bytes] | None = None) -> list[ExternalStep]:
    """Plan each registered member without granting ownership from its name."""
    CodexLinkSpec(parent.link_skill_name).validate(root, target)
    steps = []
    for member in codex_copy_members(parent.link_skill_name):
        original_source = _copy_bytes(target, member.destination_path)
        source = ((expected_contents or {}).get(member.id, original_source))
        if source is None:
            raise SecurityError("registered copy source is missing")
        expected = _copy_hash(source)
        prior = _copy_hash(_copy_bytes(root, member.destination_path))
        if operation == "remove" and prior is None:
            continue
        trusted = trusted_canonical_hashes(member, target) | {current_kit_hash(member, target)}
        if expected not in trusted:
            raise SecurityError("copy source does not have kit lineage")
        for internal in tx.plan:
            if member.id in internal.spec_ids and _copy_hash(original_source) == internal.pre_container_hash:
                # Internal plan is already selected through preflight lineage.
                if internal.pre_container_hash is not None:
                    trusted.add(internal.pre_container_hash)
        for internal in tx.plan:
            if member.id in internal.spec_ids and internal.pre_container_hash is not None:
                # Its backup must prove the actual preflight-approved old bytes.
                backup = _copy_bytes(target, f".repo-memory-kit/tx/{tx.tx_id}/backups/{internal.spec_id}")
                if _copy_hash(backup) == internal.pre_container_hash:
                    trusted.add(internal.pre_container_hash)
        prior_state = "absent" if prior is None else "content_matches"
        if prior is not None and prior not in trusted:
            if operation == "remove":
                raise SecurityError("copy member differs from proven kit content")
            # Journal a conflict, with permission to create only after it is empty.
            # Its user bytes are neither backed up nor authorized for replacement.
            prior_state, prior = "other", None
        steps.append(ExternalStep(parent.id, parent.link_skill_name, str(root),
                                  prior_state,
                                  operation=operation, strategy="copy",
                                  expected_hash=expected if operation == "create" else prior,
                                  member_spec_id=member.id, prior_hash=prior))
    return steps


def backup_copy_member(target: Path, tx, step: ExternalStep) -> None:
    if step.prior_hash is None or (step.operation == "create" and step.prior_hash == step.expected_hash):
        return
    root, member = _copy_paths(target, step)
    raw = _copy_bytes(root, member.destination_path)
    if _copy_hash(raw) != step.prior_hash:
        raise SecurityError("copy member changed before backup")
    relative = _copy_backup_rel(tx.tx_id, step)
    secure_mkdir(target, str(PurePosixPath(relative).parent))
    old = _copy_bytes(target, relative)
    if old is not None:
        if _copy_hash(old) != step.prior_hash:
            raise SecurityError("copy backup conflicts")
        return
    fd = secure_open(target, relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def resolve_empty_copy_conflict(target: Path, tx, step: ExternalStep) -> None:
    if step.prior_state != "other" or step.operation != "create":
        return
    root, member = _copy_paths(target, step)
    if _copy_bytes(root, member.destination_path) is None:
        step.prior_state = "absent"
        tx.write(target)  # New absence intent is durable before a formerly blocked create.


def _write_copy_bytes(root: Path, member, raw: bytes, prior_hash: str | None, tx_id: str) -> None:
    relative = member.destination_path
    if _copy_hash(_copy_bytes(root, relative)) != prior_hash:
        raise SecurityError("copy destination changed")
    parent = str(PurePosixPath(relative).parent)
    if not root.exists():
        _plat.mkdirs_checked(Path(root.anchor), root.relative_to(root.anchor).as_posix())
    secure_mkdir(root, parent)
    tmp = str(PurePosixPath(parent) / f".kit-copy-{tx_id}-{PurePosixPath(relative).name}")
    existing = _copy_bytes(root, tmp)
    if existing is None:
        fd = secure_open(root, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, member.expected_mode or 0o644)
        try:
            write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
    elif existing != raw:
        raise SecurityError("copy staged bytes conflict")
    if _copy_hash(_copy_bytes(root, relative)) != prior_hash:
        raise SecurityError("copy destination changed before publication")
    if prior_hash is None:
        if _plat.security_level() == "strict":
            fd = _plat.secure_walk_dir_fd(root, parent)
            try:
                _plat.rename_noreplace(fd, PurePosixPath(tmp).name, fd, PurePosixPath(relative).name)
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            # Windows compatible uses native no-overwrite rename after safe path checks.
            if not _plat.check_path_safe(root, tmp) or not _plat.check_path_safe(root, relative):
                raise SecurityError("copy path is unsafe")
            os.rename(root / tmp, root / relative)
    else:
        if _plat.security_level() == "strict":
            fd = _plat.secure_walk_dir_fd(root, parent)
            isolation = _copy_isolation_name(member, tx_id)
            try:
                _plat.rename_noreplace(fd, PurePosixPath(relative).name, fd, isolation)
                if _hash_regular_at(fd, isolation) != prior_hash:
                    _restore_delete_isolation(fd, isolation, PurePosixPath(relative).name)
                    raise SecurityError("copy destination changed during isolation")
                try:
                    _plat.rename_noreplace(fd, PurePosixPath(tmp).name, fd, PurePosixPath(relative).name)
                except OSError:
                    _restore_delete_isolation(fd, isolation, PurePosixPath(relative).name)
                    raise
                os.unlink(isolation, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            secure_replace(root, tmp, relative)


def _copy_delete_step(step: ExternalStep) -> CommitStep:
    return CommitStep(step.member_spec_id, (step.member_spec_id,), "delete",
                      step.expected_hash, None, 0o644)


def apply_copy_member(target: Path, step: ExternalStep, tx_id: str) -> None:
    validate_tx_id(tx_id)
    root, member = _copy_paths(target, step)
    settle_copy_isolation(root, member, step, tx_id)
    current = _copy_hash(_copy_bytes(root, member.destination_path))
    if step.prior_hash is not None and (step.operation == "remove" or step.prior_hash != step.expected_hash):
        backup = _copy_bytes(target, _copy_backup_rel(tx_id, step))
        if _copy_hash(backup) != step.prior_hash:
            raise SecurityError("copy prior backup missing or changed")
    if step.operation == "create":
        if current == step.expected_hash:
            return
        source = _copy_bytes(target, member.destination_path)
        if _copy_hash(source) != step.expected_hash:
            raise SecurityError("copy source changed")
        _write_copy_bytes(root, member, source, step.prior_hash, tx_id)
    else:
        deletion = _copy_delete_step(step)
        if _plat.security_level() == "strict" and (root / member.destination_path).parent.exists():
            failures = complete_internal_deletions(root, TransactionRecord(tx_id, "uninstall", "committing", plan=[deletion]))
            if failures:
                raise SecurityError("copy deletion isolation conflicts")
        if current is None:
            return
        if current != step.expected_hash:
            raise SecurityError("copy removal content changed")
        if _plat.security_level() == "strict":
            result = _commit_delete_strict(root, deletion, tx_id)
            if result.status != "committed":
                raise SecurityError("copy removal could not complete")
        else:
            secure_unlink(root, member.destination_path)
        parent = PurePosixPath(member.destination_path).parent
        boundary = PurePosixPath(f".agents/skills/{step.skill_name}")
        while len(parent.parts) >= len(boundary.parts):
            try:
                secure_rmdir(root, str(parent))
            except OSError:
                break
            parent = parent.parent


def rollback_copy_member(target: Path, step: ExternalStep, tx_id: str) -> None:
    root, member = _copy_paths(target, step)
    settle_copy_isolation(root, member, step, tx_id)
    if step.operation == "remove" and _plat.security_level() == "strict":
        deletion = _copy_delete_step(step)
        try:
            isolation = _delete_isolation_hash(root, deletion, tx_id)
        except FileNotFoundError:
            isolation = None
        if isolation is not None and not _restore_pending_delete(root, deletion, tx_id):
            raise SecurityError("copy deletion isolation cannot be restored")
    current = _copy_hash(_copy_bytes(root, member.destination_path))
    if current == step.prior_hash:
        return
    if step.prior_hash is None:
        if current != step.expected_hash:
            if step.prior_state == "other" and step.state == "intent":
                return  # Explicitly observed foreign pre-state was never owned.
            raise SecurityError("copy rollback destination changed")
        apply_copy_member(target, ExternalStep(**{**step.__dict__, "operation": "remove"}), tx_id)
        return
    backup = _copy_bytes(target, _copy_backup_rel(tx_id, step))
    if _copy_hash(backup) != step.prior_hash:
        raise SecurityError("copy rollback backup missing or changed")
    allowed_current = step.expected_hash if step.operation == "create" else None
    if current != allowed_current:
        raise SecurityError("copy rollback destination changed")
    _write_copy_bytes(root, member, backup, current, tx_id)


@dataclass
class TransactionRecord:
    tx_id: str                              # UUID v4（生成器只用 uuid4）
    kind: Literal["install", "uninstall"]
    status: TransactionStatus
    plan: list[CommitStep] = field(default_factory=list)
    external: list[ExternalStep] = field(default_factory=list)
    created_at: str = ""
    detail: str | None = None               # needs_human 原因（阻断时展示）
    mac: str | None = None                  # 载入时填充；写入时由 write_transaction_atomic 计算
    mac_verified: bool = False              # find_unresolved 载入即验证（§11.4）
    codex_root_old: str | None = None       # marker 旧值（HMAC 保护——回滚恢复）
    link_strategy: str = "symlink"           # 本事务外部链接策略（HMAC 保护——恢复不得改变产物类型）

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id,
            "kind": self.kind,
            "status": self.status,
            "plan": [s.to_dict() for s in self.plan],
            "external": [e.to_dict() for e in self.external],
            "created_at": self.created_at,
            "detail": self.detail,
            "codex_root_old": self.codex_root_old,
            "link_strategy": self.link_strategy,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TransactionRecord":
        return cls(
            tx_id=str(data["tx_id"]),
            kind=str(data["kind"]),
            status=str(data["status"]),
            plan=[CommitStep.from_dict(s) for s in data.get("plan", [])],
            external=[ExternalStep.from_dict(e) for e in data.get("external", [])],
            created_at=str(data.get("created_at", "")),
            detail=data.get("detail"),
            codex_root_old=data.get("codex_root_old"),
            link_strategy=str(data.get("link_strategy") or "symlink"),
        )

    @classmethod
    def new(cls, target: Path, kind: Literal["install", "uninstall"]) -> "TransactionRecord":
        """§12 步骤 3：创建事务（此时 ensure_tx_key——密钥必须先于首个事务记录存在）。"""
        ensure_tx_key(target)
        return cls(
            tx_id=str(uuid.uuid4()), kind=kind, status="planning",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def write(self, target: Path) -> None:
        write_transaction_atomic(target, self)


@dataclass
class CommitResult:
    status: Literal["committed", "conflict", "needs_human"]
    detail: str = ""


@dataclass
class RecoveryResult:
    status: Literal[
        "no_action", "blocked", "needs_human",
        "rolled_back_to_clean", "rolled_back", "roll_forward_completed",
    ]
    detail: str = ""


# ══════════════════════════ §11.3 仓库级互斥锁 ══════════════════════════

def acquire_install_lock(target: Path) -> int:
    """生命周期进程（install/repair/uninstall/recover）用：排他、可创建。
    LOCK_EX | LOCK_NB，进程生命周期内持有；并发第二个 → 立即失败退出。"""
    secure_mkdir(target, ".repo-memory-kit")           # 首次安装：目录逐级安全创建
    fd = secure_open(target, ".repo-memory-kit/install.lock",
                     os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _plat.lock_exclusive_nb(fd)
    except BlockingIOError:
        os.close(fd)
        sys.exit("✗ 另一个 kit 生命周期进程正在运行（install.lock 被持有），退出")
    return fd


def try_open_install_lock_shared(target: Path) -> int | None:
    """doctor 用：只读打开**已存在**的锁，绝不创建文件。
    - fd   → 已持共享锁（检查期间生命周期进程无法进入 commit）
    - None → 锁不存在 = 当前无生命周期进程 → 继续只读检查
    - -1   → 锁被排他持有，或打开异常（如 ELOOP 符号链接）→ INCOMPLETE"""
    lock_path = target / ".repo-memory-kit" / "install.lock"
    try:
        fd = os.open(lock_path,
                     os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
    except FileNotFoundError:
        return None
    except OSError:
        return -1
    try:
        _plat.lock_shared_nb(fd)
    except BlockingIOError:
        os.close(fd)
        return -1
    return fd


# ══════════════════════════ §11.12 事务密钥（仓库外；读/建分离） ══════════════════════════

def tx_key_path(target: Path) -> Path:
    """密钥在目标仓库之外（运行用户配置目录）——目标仓库写入者不可篡改。
    遵循 XDG base-dir：$XDG_CONFIG_HOME 优先（测试/无 HOME 环境借此隔离），
    否则 ~/.config。以 target 绝对路径哈希命名，多目标仓库共存。"""
    digest = hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:16]
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if not config_home:
        config_home = os.path.join(os.path.expanduser("~"), ".config")
    return Path(config_home) / "agent-engineering-kit" / "keys" / f"{digest}.key"


def load_tx_key(target: Path) -> bytes | None:
    """只读。缺失/不可读/长度异常 → None。验证路径（verify_tx_mac）唯一使用。
    读 33 字节且要求恰好 32（v11）：os.read(fd, 32) 对超长损坏文件（如 40 字节）
    会返回前 32 字节被误接受——读 33 才能确认无多余内容。"""
    try:
        fd = os.open(tx_key_path(target),
                     os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
    except OSError:
        return None
    try:
        key = os.read(fd, 33)               # 33 字节：恰好 32 才有效
        return key if len(key) == 32 else None
    finally:
        os.close(fd)


def ensure_tx_key(target: Path) -> bytes:
    """仅写入方（write_transaction_atomic / Transaction.new）调用。
    ★ 返回值必须是"磁盘上已存在的密钥"（v10）：
    - 读到 → 返回读到的
    - 缺失 → O_CREAT|O_EXCL 写入 + fsync → 回读，一致才返回
    - FileExistsError（并发他方刚建）→ 重读；重读失败 → 显式 SecurityError
    - 已存在但读不出/长度异常（损坏）→ 显式 SecurityError。
      修复路径：删除损坏密钥文件即可重建，但旧事务将全部转为 needs_human
      （与跨机器行为一致）——不静默生成替身密钥。"""
    key = load_tx_key(target)
    if key is not None:
        return key
    path = tx_key_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = os.urandom(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | _plat.O_NOFOLLOW | _plat.O_BINARY, 0o600)
    except FileExistsError:
        existing = load_tx_key(target)          # 并发：他方刚建好
        if existing is None:
            raise SecurityError(
                f"tx key 已存在但不可读（损坏或并发写入中），拒绝生成替身密钥: {path}")
        return existing
    try:
        write_all(fd, fresh)
        os.fsync(fd)
    finally:
        os.close(fd)
    # 密钥目录项持久化（v11）：新建文件必须 fsync 父目录，否则崩溃后密钥可能消失
    # （Windows 无目录 fsync——NTFS 日志保证，platform no-op）
    _plat.fsync_directory(path.parent, ".")
    persisted = load_tx_key(target)             # 落盘后回读
    if persisted != fresh:
        raise SecurityError(f"tx key 落盘后回读不一致: {path}")
    return persisted


# ══════════════════════════ §11.10 事务记录原子写（含 HMAC） ══════════════════════════

def _tx_payload(tx: TransactionRecord) -> bytes:
    return json.dumps(tx.to_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode()


def write_transaction_atomic(target: Path, tx: TransactionRecord) -> None:
    tx_rel = f".repo-memory-kit/tx/{tx.tx_id}"
    secure_mkdir(target, tx_rel)          # 幂等，覆盖所有调用方——含恢复路径写状态更新（v12）
    payload = _tx_payload(tx)
    record = {
        "payload": tx.to_dict(),
        "mac": hmac.new(ensure_tx_key(target), payload, hashlib.sha256).hexdigest(),
    }
    # 临时名带随机后缀：同一事务多次状态更新互不碰撞
    tmp_rel = f"{tx_rel}/.{tx.tx_id}.{uuid.uuid4()}.tmp"
    fd = secure_open(target, tmp_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        write_all(fd, json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    secure_replace(target, tmp_rel, f"{tx_rel}/record.json")   # 双 dir_fd + 经 fd fsync


def verify_tx_mac(target: Path, tx: TransactionRecord) -> bool:
    """重算 payload 的 HMAC 并常量时间比较。
    只调 load_tx_key（只读）——验证路径绝不创建文件（v8 缺陷）。
    密钥缺失/不可读 → False → needs_human。"""
    key = load_tx_key(target)
    if key is None:
        return False
    payload = _tx_payload(tx)
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, tx.mac or "")


def load_transaction(target: Path, tx_id: str) -> TransactionRecord | None:
    """读取单个事务记录（路径只读 + O_NOFOLLOW；读取即做 MAC 验证）。"""
    validate_tx_id(tx_id)
    path = target / ".repo-memory-kit" / "tx" / tx_id / "record.json"
    try:
        fd = os.open(path, os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
    except FileNotFoundError:
        return None
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        record = json.loads(b"".join(chunks).decode("utf-8"))
        tx = TransactionRecord.from_dict(record["payload"])
        tx.mac = str(record["mac"])
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
        tx = TransactionRecord(
            tx_id=tx_id, kind="install", status="needs_human",
            created_at="", detail="事务记录缺失或损坏（record.json 无法解析）")
        tx.mac_verified = verify_tx_mac(target, tx)
        return tx
    tx.mac_verified = verify_tx_mac(target, tx)
    return tx


# ══════════════════════════ §11.4 记录校验与未完成事务发现 ══════════════════════════

def validate_tx_record(tx: TransactionRecord) -> list[str]:
    """返回错误列表；非空 → needs_human，绝不自动清理/回滚。
    组校验模型：group = step.spec_ids（非空、首项=锚、无重复、Registry 全序一致）。"""
    errors: list[str] = []
    try:
        validate_tx_id(tx.tx_id)
    except SecurityError as e:
        errors.append(str(e))
    if tx.kind not in ("install", "uninstall"):
        errors.append(f"kind 非法: {tx.kind!r}")
    if tx.link_strategy not in ("symlink", "copy"):
        errors.append(f"link_strategy 非法: {tx.link_strategy!r}")
    for step in tx.external:
        if step.operation not in ("create", "remove"):
            errors.append(f"{step.spec_id}: external.operation 非法: {step.operation!r}")
        if step.strategy not in ("symlink", "copy"):
            errors.append(f"{step.spec_id}: external.strategy 非法: {step.strategy!r}")
        if step.strategy == "copy" and step.operation == "remove" \
                and step.expected_hash is None:
            errors.append(f"{step.spec_id}: copy 删除步骤缺 expected_hash（血统凭证）")
        if step.member_spec_id is not None:
            try:
                parent = resolve_spec(step.spec_id)
                if (step.strategy != "copy" or parent.resource_type != "codex_link"
                        or step.skill_name != parent.link_skill_name):
                    raise SecurityError("copy member parent is invalid")
                codex_copy_member(step.skill_name, step.member_spec_id)
                for value in (step.expected_hash, step.prior_hash):
                    if value is not None and (not isinstance(value, str) or not HEX64.fullmatch(value)):
                        raise SecurityError("copy member digest is invalid")
                if step.expected_hash is None:
                    raise SecurityError("copy member expected digest is missing")
                if (step.prior_state not in {"absent", "content_matches", "other"}
                        or step.state not in {"intent", "applied", "removed"}
                        or (step.prior_state in {"absent", "other"}) != (step.prior_hash is None)
                        or step.operation != ("create" if tx.kind == "install" else "remove")):
                    raise SecurityError("copy member state is invalid")
            except SecurityError as exc:
                errors.append(str(exc))
    # needs_human 也在可恢复集内（§11.9 人工恢复入口对其选择策略；
    # find_unresolved 的扫描集含 needs_human——v9）
    if tx.status not in ("planning", "staging", "committing", "needs_human"):
        errors.append(f"未完成事务的状态非法: {tx.status!r}")

    seen_destinations: set[str] = set()
    seen_spec_ids: set[str] = set()
    for step in tx.plan:
        group = step.spec_ids
        if not group:
            errors.append(f"{step.spec_id}: spec_ids 为空")
            continue
        if group[0] != step.spec_id:
            errors.append(f"{step.spec_id}: 锚不是 spec_ids 第一项")
        if len(group) != len(set(group)):
            errors.append(f"{step.spec_id}: 组内 spec_id 重复")
        try:
            specs = [resolve_spec(sid) for sid in group]
        except SecurityError as e:
            errors.append(f"{step.spec_id}: {e}")
            continue
        dests = {s.destination_path for s in specs}
        if len(dests) != 1:
            errors.append(f"{step.spec_id}: 组内 destination 不一致: {sorted(dests)}")
        order = [_SPEC_ORDER[s.id] for s in specs]
        if order != sorted(order):
            errors.append(f"{step.spec_id}: 组内顺序与 Registry 全序不符")
        anchor = specs[0]
        if anchor.destination_path in seen_destinations:
            errors.append(f"{step.spec_id}: 同一 destination 出现于多个 step")
        seen_destinations.add(anchor.destination_path)
        if seen_spec_ids.intersection(group):
            errors.append(f"{step.spec_id}: spec_id 跨 step 重复")
        seen_spec_ids.update(group)
        for h in (step.pre_container_hash, step.post_container_hash):
            if h is not None and not HEX64.match(h):
                errors.append(f"{step.spec_id}: hash 格式非法: {h!r}")
        if step.kind not in ("replace", "delete"):
            errors.append(f"{step.spec_id}: kind 非法: {step.kind!r}")
        if step.kind == "delete" and step.post_container_hash is not None:
            errors.append(f"{step.spec_id}: delete 步骤的 post hash 必须为 None")
        if step.kind == "replace" and step.post_container_hash is None:
            errors.append(f"{step.spec_id}: replace 步骤的 post hash 不得为 None")
        if step.pre_mode is not None and not (0 <= step.pre_mode <= 0o777):
            errors.append(f"{step.spec_id}: mode 非法: {oct(step.pre_mode)}")
    return errors


def find_unresolved(target: Path) -> list[TransactionRecord]:
    """扫描 .repo-memory-kit/tx/*/record.json，返回 status ∈ {planning, staging,
    committing, needs_human} 的事务（按 created_at 升序）。
    ★ needs_human 必须在扫描集内（v8 缺陷：只扫前三者，needs_human 写盘后反而
    '隐身'）。读取即做 MAC 验证——验证失败的事务单独标记（mac_verified=False），
    交恢复入口判 needs_human。UUID 目录名不合规范或记录损坏 → 合成
    needs_human 记录（fail-safe：宁可阻断，不可放行）。"""
    tx_root = target / ".repo-memory-kit" / "tx"
    out: list[TransactionRecord] = []
    try:
        names = [e.name for e in os.scandir(tx_root) if e.is_dir()]
    except FileNotFoundError:
        return []
    for name in names:
        try:
            validate_tx_id(name)
        except SecurityError:
            continue                     # 非 UUID 目录名：与事务无关的杂项，跳过
        tx = load_transaction(target, name)
        if tx is None:
            tx = TransactionRecord(
                tx_id=name, kind="install", status="needs_human",
                created_at="",
                detail="事务目录存在但 record.json 缺失")
            tx.mac_verified = verify_tx_mac(target, tx)
        if tx.status in UNRESOLVED_STATUSES:
            out.append(tx)
    out.sort(key=lambda t: t.created_at)
    return out


# ══════════════════════════ §11.5 Staging（内容生成在内存，落盘是纯 I/O） ══════════════════════════

def stage_resource(target: Path, spec_id: str, tx_id: str,
                   final_content: bytes, mode: int = 0o644) -> tuple[str, str]:
    """Staging 阶段调用：纯落盘 I/O，内容来自内存。
    先确保目标父目录存在（kit 创建的目录，如 .claude/skills/<name>；
    根目录文件为 no-op——v12），经 secure_open 写入 staged。"""
    spec = resolve_spec(spec_id)
    rel = staged_rel(spec, tx_id)                  # §7 的相对路径派生（单一来源）
    secure_mkdir(target, str(PurePosixPath(spec.destination_path).parent))
    fd = secure_open(target, rel,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        write_all(fd, final_content)
        os.fsync(fd)
    finally:
        os.close(fd)
    return rel, hashlib.sha256(final_content).hexdigest()


def backup_container(target: Path, step: CommitStep, tx_id: str) -> bool:
    """§12 步骤 8：将被修改/删除的目标文件 → tx/<id>/backups/<锚spec_id>（0600，
    secure_open 写入；仅对实际存在的文件备份——pre_container_hash 非 None）。
    备份内容必须匹配 pre hash（HMAC 保护的 hash 是回滚信任根，§11.12）。"""
    if step.pre_container_hash is None:
        return True
    paths = derive_transaction_paths(target, step.spec_id, tx_id)
    try:
        src_fd = os.open(paths.dst,
                         os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
    except OSError:
        return False
    try:
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        while True:
            chunk = os.read(src_fd, 65536)
            if not chunk:
                break
            hasher.update(chunk)
            chunks.append(chunk)
        if hasher.hexdigest() != step.pre_container_hash:
            return False
    finally:
        os.close(src_fd)
    backup_rel = f".repo-memory-kit/tx/{tx_id}/backups/{step.spec_id}"
    fd = secure_open(target, backup_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        for chunk in chunks:
            write_all(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


# ══════════════════════════ §11.6 Commit 阶段 ══════════════════════════

def _delete_isolation(step: CommitStep, tx_id: str) -> tuple[str, str, str]:
    """返回（父目录、原名、事务隔离名）；隔离路径始终从受信 plan 派生。"""
    rel = PurePosixPath(resolve_spec(step.spec_id).destination_path)
    return str(rel.parent), rel.name, f".kit-txdel-{tx_id}-{rel.name}"


def _hash_regular_at(parent_fd: int, name: str) -> str:
    """在固定父目录内不跟随最终组件读取 hash。"""
    fd = os.open(name, os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY,
                 dir_fd=parent_fd)
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"隔离对象不是普通文件: {name}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(fd)


def _restore_delete_isolation(parent_fd: int, isolation: str,
                              original: str) -> bool:
    """不覆盖竞争文件地恢复隔离对象。"""
    try:
        _plat.rename_noreplace(parent_fd, isolation, parent_fd, original)
        os.fsync(parent_fd)
        return True
    except OSError:
        return False


def _commit_delete_strict(target: Path, step: CommitStep,
                          tx_id: str) -> CommitResult:
    """POSIX 删除：原子隔离最终组件，再核验被移对象。"""
    parent_rel, original, isolation = _delete_isolation(step, tx_id)
    parent_fd = _plat.secure_walk_dir_fd(target, parent_rel)
    try:
        try:
            os.stat(isolation, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return CommitResult(
                "needs_human", detail=f"删除隔离名已存在: {isolation}")

        try:
            _plat.rename_noreplace(parent_fd, original, parent_fd, isolation)
        except FileNotFoundError:
            return CommitResult("conflict", detail="delete target disappeared")
        except FileExistsError:
            return CommitResult(
                "needs_human", detail=f"删除隔离名冲突: {isolation}")
        except OSError as e:
            return CommitResult("needs_human", detail=f"无法原子隔离删除目标: {e}")

        try:
            isolated_hash = _hash_regular_at(parent_fd, isolation)
        except OSError as e:
            restored = _restore_delete_isolation(parent_fd, isolation, original)
            return CommitResult(
                "needs_human",
                detail=f"隔离对象不可安全核验: {e}; restored={restored}")
        if isolated_hash != step.pre_container_hash:
            restored = _restore_delete_isolation(parent_fd, isolation, original)
            return CommitResult(
                "conflict" if restored else "needs_human",
                detail="delete target changed after CAS"
                       + ("" if restored else f"; isolation={isolation}"))

        try:
            os.unlink(isolation, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as e:
            restored = _restore_delete_isolation(parent_fd, isolation, original)
            return CommitResult(
                "needs_human",
                detail=f"隔离对象删除失败: {e}; restored={restored}")
        step.committed = True
        return CommitResult("committed")
    finally:
        os.close(parent_fd)


def _delete_isolation_hash(target: Path, step: CommitStep,
                           tx_id: str) -> str | None:
    """读取中断删除的隔离对象；不存在返回 None。"""
    parent_rel, _original, isolation = _delete_isolation(step, tx_id)
    parent_fd = _plat.secure_walk_dir_fd(target, parent_rel)
    try:
        try:
            os.stat(isolation, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return _hash_regular_at(parent_fd, isolation)
    finally:
        os.close(parent_fd)


def _restore_pending_delete(target: Path, step: CommitStep,
                            tx_id: str) -> bool:
    """回滚时优先恢复原子隔离的原对象。"""
    parent_rel, original, isolation = _delete_isolation(step, tx_id)
    parent_fd = _plat.secure_walk_dir_fd(target, parent_rel)
    try:
        try:
            if _hash_regular_at(parent_fd, isolation) != step.pre_container_hash:
                return False
        except (FileNotFoundError, OSError):
            return False
        try:
            os.stat(original, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _restore_delete_isolation(parent_fd, isolation, original)
        return False
    finally:
        os.close(parent_fd)


def complete_internal_deletions(target: Path,
                                tx: TransactionRecord) -> list[str]:
    """补完崩溃留下的 POSIX 删除隔离对象。"""
    if _plat.security_level() != "strict":
        return []
    failures: list[str] = []
    for step in tx.plan:
        if step.kind != "delete" or step.pre_container_hash is None:
            continue
        try:
            isolated_hash = _delete_isolation_hash(target, step, tx.tx_id)
        except OSError as e:
            failures.append(f"{step.spec_id}: 隔离对象不可读: {e}")
            continue
        if isolated_hash is None:
            continue
        if isolated_hash != step.pre_container_hash:
            failures.append(f"{step.spec_id}: 隔离对象 hash 冲突")
            continue
        parent_rel, original, isolation = _delete_isolation(step, tx.tx_id)
        parent_fd = _plat.secure_walk_dir_fd(target, parent_rel)
        try:
            try:
                os.stat(original, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    # 原路径仍为空才补完隔离删除；原路径被占则
                    # 保留两个对象交给人工，绝不删竞争文件。
                    os.unlink(isolation, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError as e:
                    failures.append(f"{step.spec_id}: 隔离对象删除失败: {e}")
            else:
                failures.append(f"{step.spec_id}: 原路径与隔离对象同时存在")
        finally:
            os.close(parent_fd)
    return failures


def commit_one(target: Path, step: CommitStep, tx_id: str) -> CommitResult:
    paths = derive_transaction_paths(target, step.spec_id, tx_id)
    dst, staged = paths.dst, paths.staged

    # CAS pre（整文件 hash）
    if dst.exists():
        if file_sha256(dst) != step.pre_container_hash:
            return CommitResult("conflict")
    elif step.pre_container_hash is not None:
        return CommitResult("conflict")

    # 删除步骤
    if step.kind == "delete":
        if step.pre_container_hash is None:
            step.committed = True              # 本来就不存在
            return CommitResult("committed")
        if _plat.security_level() == "strict":
            return _commit_delete_strict(target, step, tx_id)
        secure_unlink(target, paths.spec.destination_path)
        step.committed = True
        return CommitResult("committed")

    # 替换步骤：验证 staged 是普通文件且 hash 正确
    try:
        st = staged.lstat()
        if not stat_module.S_ISREG(st.st_mode):
            return CommitResult("needs_human", detail="staged is not regular file")
        if file_sha256(staged) != step.post_container_hash:
            return CommitResult("needs_human", detail="staged hash mismatch")
    except FileNotFoundError:
        return CommitResult("needs_human", detail="staged file missing")

    # 原子替换（全程 dir_fd，父目录 TOCTOU 不回归——v10）
    secure_replace(target, staged_rel(paths.spec, tx_id),
                   paths.spec.destination_path)

    # 验证 post
    if file_sha256(dst) != step.post_container_hash:
        return CommitResult("needs_human", detail="post-hash mismatch")

    step.committed = True
    return CommitResult("committed")


# ══════════════════════════ §11.8 Rollback ══════════════════════════

def rollback_one(target: Path, step: CommitStep, tx: TransactionRecord) -> bool:
    """只对已分类为 NEW 的步骤调用（调用时 dst == post，或 delete 已生效）。"""
    paths = derive_transaction_paths(target, step.spec_id, tx.tx_id)
    dst, backup = paths.dst, paths.backup

    if step.kind == "delete":
        if _plat.security_level() == "strict":
            try:
                isolation_exists = (
                    _delete_isolation_hash(target, step, tx.tx_id) is not None)
            except OSError:
                return False
            if isolation_exists:
                return _restore_pending_delete(target, step, tx.tx_id)
        if dst.exists():
            if step.pre_container_hash is None:
                return True
            return file_sha256(dst) == step.pre_container_hash
        if step.pre_container_hash is None:
            return True
        return restore_from_backup(target, paths.spec.destination_path, backup, tx.tx_id,
                                   step.pre_container_hash,
                                   step.pre_mode or 0o644)

    if dst.exists():
        if file_sha256(dst) != step.post_container_hash:
            return False
    else:
        if step.pre_container_hash is None:
            return True
        return False

    if backup.exists():
        return restore_from_backup(target, paths.spec.destination_path, backup, tx.tx_id,
                                   step.pre_container_hash,
                                   step.pre_mode or 0o644)
    elif step.pre_container_hash is None:
        secure_unlink(target, paths.spec.destination_path)
        return True
    else:
        return False


def restore_from_backup(target: Path, dst_rel: str, backup: Path, tx_id: str,
                        pre_hash: str, pre_mode: int) -> bool:
    # backup 读取允许路径形式（O_NOFOLLOW 防最终组件）：读到的内容必须匹配
    # HMAC 保护的 pre_hash——被换内容只会导致校验失败（fail-safe，无写入）
    try:
        backup_fd = os.open(backup,
                            os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
    except OSError:
        return False

    try:
        st = os.fstat(backup_fd)
        if not stat_module.S_ISREG(st.st_mode):
            return False

        hasher = hashlib.sha256()
        content_parts = []
        os.lseek(backup_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(backup_fd, 65536)
            if not chunk:
                break
            hasher.update(chunk)
            content_parts.append(chunk)
        # pre_hash 来自 HMAC 保护的事务记录——内容与 hash 均不可伪造（§11.12）
        if hasher.hexdigest() != pre_hash:
            return False

        # 恢复文件写入：secure_open（O_CREAT|O_EXCL|O_NOFOLLOW，全程 dir_fd），不先 unlink
        rel = PurePosixPath(dst_rel)
        tmp_rel = str(rel.parent / f"{rel.name}.kit-restore-{tx_id}")
        try:
            tmp_fd = secure_open(target, tmp_rel,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, pre_mode)
        except FileExistsError:
            return False

        try:
            for part in content_parts:
                write_all(tmp_fd, part)
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)

        secure_replace(target, tmp_rel, str(rel))

        return file_sha256(target / str(rel)) == pre_hash
    finally:
        os.close(backup_fd)


# ══════════════════════════ 清理（staging / 事务目录） ══════════════════════════

def cleanup_staging(target: Path, tx: TransactionRecord) -> None:
    """删除 staging 文件、备份、事务目录（needs_human 事务除外——调用方保证
    不对 needs_human 调用本函数）。全部路径从 plan + tx_id 派生（§7），
    绝不 scandir 事务目录按磁盘内容删文件。空目录残留（ENOTEMPTY）安全忽略。"""
    tx_dir = f".repo-memory-kit/tx/{tx.tx_id}"
    for step in tx.external:
        if step.member_spec_id is not None:
            try:
                secure_unlink(target, _copy_backup_rel(tx.tx_id, step))
            except FileNotFoundError:
                pass
            root, member = _copy_paths(target, step)
            relative = member.destination_path
            tmp = str(PurePosixPath(relative).parent /
                      f".kit-copy-{tx.tx_id}-{PurePosixPath(relative).name}")
            tmp_hash = _copy_hash(_copy_bytes(root, tmp))
            if tmp_hash is not None and tmp_hash in {step.expected_hash, step.prior_hash}:
                secure_unlink(root, tmp)
    for step in tx.plan:
        try:
            secure_unlink(target, staged_rel(resolve_spec(step.spec_id), tx.tx_id))
        except FileNotFoundError:
            pass
        try:
            secure_unlink(target, f"{tx_dir}/backups/{step.spec_id}")
        except FileNotFoundError:
            pass
    for extra in ("record.json",):
        try:
            secure_unlink(target, f"{tx_dir}/{extra}")
        except FileNotFoundError:
            pass
    for rel in (f"{tx_dir}/external-backups", f"{tx_dir}/backups", tx_dir):
        try:
            secure_rmdir(target, rel)
        except OSError:
            pass


# ══════════════════════════ §11.7 恢复策略（含外部步骤补偿） ══════════════════════════

def classify_committing(target: Path, tx: TransactionRecord) -> dict[str, list[CommitStep]]:
    """committing 状态事务的磁盘现状分类（§11.7）：
    NEW=已提交（dst==post / delete 已生效）；OLD=未动（dst==pre / 缺失同 plan）；
    CONFLICT=既非 pre 也非 post（外部修改或记录与磁盘不符）。"""
    classified = {"NEW": [], "OLD": [], "CONFLICT": []}
    for step in tx.plan:
        dst = derive_destination(target, step.spec_id)

        if step.kind == "delete":
            if (_plat.security_level() == "strict"
                    and step.pre_container_hash is not None):
                try:
                    isolated_hash = _delete_isolation_hash(
                        target, step, tx.tx_id)
                except OSError:
                    classified["CONFLICT"].append(step)
                    continue
                if isolated_hash is not None:
                    # 隔离对象与原路径同时存在，或隔离内容不符
                    # HMAC 保护的 pre hash，都不能自动处置。
                    if os.path.lexists(dst) or isolated_hash != step.pre_container_hash:
                        classified["CONFLICT"].append(step)
                    else:
                        classified["NEW"].append(step)
                    continue
            if not os.path.lexists(dst):
                classified["NEW"].append(step)
            elif step.pre_container_hash is None:
                classified["OLD"].append(step)
            elif file_sha256(dst) == step.pre_container_hash:
                classified["OLD"].append(step)
            else:
                classified["CONFLICT"].append(step)
            continue

        if not dst.exists():
            if step.pre_container_hash is None:
                classified["OLD"].append(step)
            else:
                classified["CONFLICT"].append(step)
            continue

        container_hash = file_sha256(dst)
        if container_hash == step.post_container_hash:
            classified["NEW"].append(step)
        elif container_hash == step.pre_container_hash:
            classified["OLD"].append(step)
        else:
            classified["CONFLICT"].append(step)
    return classified


def complete_external_removals(target: Path, tx: TransactionRecord) -> list[str]:
    """卸载方向 roll-forward 的外部步骤收尾：链接删除意图已记录但未执行完的
    （state="intent" 且链接仍在），经父目录 fd 核验后补删；链接已消失视为
    已完成。**绝不重建**——roll-forward 是完成事务（删除），不是撤销它。
    P1 回归：复用与 execute_external_removal 相同的 fd 原语——readlink 核验
    与 unlink 不是同一原子操作，路径式 unlink 存在 TOCTOU。
    返回失败清单；非空 → needs_human。"""
    failures = []
    for rec in tx.external:
        if rec.operation == "create":
            continue                              # 卸载事务只收尾 remove 步骤
        if rec.member_spec_id is not None:
            try:
                resolve_empty_copy_conflict(target, tx, rec)
                apply_copy_member(target, rec, tx.tx_id)
                rec.state = "applied"
            except (OSError, SecurityError) as exc:
                failures.append(f"{rec.member_spec_id}: {exc}")
            continue
        if rec.prior_state == "content_matches":
            # 复制模式补删（第七轮审计 P1：此处曾误放补偿重拷逻辑——roll-forward
            # 是完成删除，不是撤销它）：
            # - 副本已不在 → 已完成
            # - 内容与 plan 时 expected_hash 一致 → 删受管文件 + 空目录 rmdir
            # - 内容已变（非 kit 血统）→ 保留现场待人工（绝不删用户内容）
            try:
                spec = resolve_spec(rec.spec_id)
                if rec.skill_name != spec.link_skill_name:
                    failures.append(f"{rec.spec_id}: 记录的 skill_name 与 Registry 不符")
                    continue
                codex_root = Path(rec.codex_root)
                link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
                link_spec.validate(codex_root, target)
                link = link_spec.derive_link_path(codex_root)
                dest = link / "SKILL.md"
                if not dest.is_file():
                    rec.state = "applied"         # 已不在 = 已完成
                    continue
                cur = hashlib.sha256(dest.read_bytes()).hexdigest()
                if rec.expected_hash is None or cur != rec.expected_hash:
                    failures.append(
                        f"{rec.spec_id}: 副本内容与 plan 时血统不符，保留现场待人工: {dest}")
                    continue
                dest.unlink()
                try:
                    link.rmdir()                  # 仅空目录可删
                except OSError:
                    pass
                rec.state = "applied"
            except (OSError, SecurityError) as e:
                failures.append(f"{rec.spec_id}: {e}")
            continue
        if rec.prior_state != "pointing_to_target":
            continue
        try:
            spec = resolve_spec(rec.spec_id)
            if rec.skill_name != spec.link_skill_name:
                failures.append(f"{rec.spec_id}: 记录的 skill_name 与 Registry 不符")
                continue
            codex_root = Path(rec.codex_root)
            link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
            link_spec.validate(codex_root, target)
            link = link_spec.derive_link_path(codex_root)
            expected = str(link_spec.derive_target_path(target))
            from .uninstall import safe_unlink_external_link
            # 原路径和确定性隔离名都检查（lstat 语义——broken symlink 也算存在，
            # Path.exists() 跟随链接对断链返回 False 会漏判 P1）
            iso = link.parent / f".kit-iso-{link.name}"
            def _lexists(p):
                try:
                    os.lstat(p)
                    return True
                except OSError:
                    return False
            if not _lexists(link) and not _lexists(iso):
                rec.state = "applied"          # 原路径和隔离名都不在 → 完成
                continue
            # 原路径或隔离名存在 → 走安全删除（内部会处理孤儿和原子恢复）
            safe_unlink_external_link(link, expected)
            rec.state = "applied"
        except (OSError, SecurityError) as e:
            failures.append(f"{rec.spec_id}: {e}")
    return failures


def complete_external_creations(target: Path, tx: TransactionRecord) -> list[str]:
    """安装方向 roll-forward 的外部步骤收尾：create 意图已记录但未完成的
    （state=intent），按记录的 strategy/expected_hash 补建并核验。
    **绝不删除**——roll-forward 是完成创建，不是撤销它。
    - 目标已存在且与预期一致 → 已完成
    - 目标被用户内容占用 → 保留现场待人工（failures）
    - copy 补建前核验 kit 源血统（sha256 == expected_hash）
    返回失败清单；非空 → needs_human。"""
    import shutil as _shutil
    failures = []
    for rec in tx.external:
        if rec.operation != "create":
            continue
        if rec.member_spec_id is not None:
            try:
                resolve_empty_copy_conflict(target, tx, rec)
                apply_copy_member(target, rec, tx.tx_id)
                rec.state = "applied"
            except (OSError, SecurityError) as exc:
                failures.append(f"{rec.member_spec_id}: {exc}")
            continue
        if rec.state in ("applied", "removed"):
            continue
        try:
            spec = resolve_spec(rec.spec_id)
            if rec.skill_name != spec.link_skill_name:
                failures.append(f"{rec.spec_id}: 记录的 skill_name 与 Registry 不符")
                continue
            codex_root = Path(rec.codex_root)
            link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
            link_spec.validate(codex_root, target)
            link = link_spec.derive_link_path(codex_root)
            expected = link_spec.derive_target_path(target)
            if rec.strategy == "copy":
                dest = link / "SKILL.md"
                if dest.is_file():
                    if rec.expected_hash and hashlib.sha256(
                            dest.read_bytes()).hexdigest() == rec.expected_hash:
                        rec.state = "applied"     # 已在（幂等）
                        continue
                    failures.append(f"{rec.spec_id}: 副本目标被占用且内容不同，保留现场: {dest}")
                    continue
                if link.exists() and not link.is_dir():
                    failures.append(f"{rec.spec_id}: 目标被非目录内容占用，保留现场: {link}")
                    continue
                source_md = expected / "SKILL.md"
                if (not source_md.is_file()) or (
                        rec.expected_hash is None
                        or hashlib.sha256(source_md.read_bytes()).hexdigest()
                        != rec.expected_hash):
                    failures.append(f"{rec.spec_id}: kit 源缺失或血统不符: {source_md}")
                    continue
                link.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(source_md, dest)
                rec.state = "applied"
            else:
                if link.is_symlink():
                    if os.readlink(link) == str(expected):
                        rec.state = "applied"     # 已在（幂等）
                    else:
                        failures.append(f"{rec.spec_id}: 链接已存在但指向不同，保留现场: {link}")
                    continue
                if link.exists():
                    failures.append(f"{rec.spec_id}: 目标被非链接内容占用，保留现场: {link}")
                    continue
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(str(expected), link)
                rec.state = "applied"
        except (OSError, SecurityError) as e:
            failures.append(f"{rec.spec_id}: {e}")
    return failures


def _recreate_external_link(link: Path, expected: Path, strategy: str) -> None:
    """按策略重建外部技能暴露——恢复不得改变产物类型（P1：copy 安装崩溃
    后 roll-forward 曾被无条件补成 symlink）。"""
    if strategy == "copy":
        import shutil as _shutil
        link.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(expected / "SKILL.md", link / "SKILL.md")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(expected), link)


def restore_external_links(target: Path, tx: TransactionRecord) -> list[str]:
    """回滚补偿：只撤销**本事务已执行**的外部副作用（磁盘现状为准，记录是线索）。

    remove 步骤（卸载回滚）——删除已发生的效果要恢复：
    - prior_state == "pointing_to_target"：链接已消失 → 按记录 strategy 重建
      （覆盖崩溃窗口：unlink 后、applied 记录前）
    - prior_state == "content_matches"：副本 SKILL.md 已删 → 从 kit 源重拷
      （回滚时内部资源已恢复，kit 源在位）

    create 步骤（安装回滚）——本事务创建的效果要撤销：
    - 副本内容 == expected_hash（本事务产物）→ 删 SKILL.md + 空目录 rmdir
    - 符号链接指向 expected → unlink
    - 内容不同/缺失 → 非本事务效果，不动（绝不删用户内容）

    恢复不得改变产物类型（copy 重建为 copy）。返回失败清单；非空 → needs_human。"""
    import shutil as _shutil
    failures = []
    for rec in tx.external:
        try:
            if rec.member_spec_id is not None:
                rollback_copy_member(target, rec, tx.tx_id)
                continue
            spec = resolve_spec(rec.spec_id)
            if rec.skill_name != spec.link_skill_name:
                failures.append(f"{rec.spec_id}: 记录的 skill_name 与 Registry 不符")
                continue
            codex_root = Path(rec.codex_root)
            link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
            link_spec.validate(codex_root, target)
            link = link_spec.derive_link_path(codex_root)
            expected = link_spec.derive_target_path(target)

            if rec.operation == "create":
                # 安装回滚：只撤销本事务创建的产物
                if rec.strategy == "copy":
                    dest = link / "SKILL.md"
                    if (dest.is_file() and rec.expected_hash
                            and hashlib.sha256(dest.read_bytes()).hexdigest()
                            == rec.expected_hash):
                        dest.unlink()
                        try:
                            link.rmdir()
                        except OSError:
                            pass
                elif link.is_symlink() and os.readlink(link) == str(expected):
                    link.unlink()
                continue

            # remove 步骤的回滚补偿
            if rec.prior_state == "content_matches":
                if (link / "SKILL.md").is_file():
                    continue                 # 副本仍在，无需补偿
                source_md = expected / "SKILL.md"
                if not source_md.is_file():
                    failures.append(f"{rec.spec_id}: 补偿失败——kit 源缺失: {source_md}")
                    continue
                link.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(source_md, link / "SKILL.md")
                continue
            if rec.prior_state != "pointing_to_target":
                continue
            if link.is_symlink() or link.exists():
                continue                     # 删除未发生或用户已自行处理
            _recreate_external_link(link, expected, rec.strategy)
        except (OSError, SecurityError) as e:
            failures.append(f"{rec.spec_id}: {e}")
    return failures


def _restore_marker_backup(target: Path, tx: TransactionRecord) -> None:
    """回滚时恢复旧 codex workspace marker（值来自 HMAC 保护的事务记录——
    P1 第六轮：旁路备份文件 O_TRUNC 崩溃窗口不可信，必须进事务记录）。"""
    from .registry import STATE_REGISTRY, secure_open, secure_unlink, write_all
    if tx.codex_root_old is None:
        return  # 事务创建时无 marker（首次安装或无 codex_root）
    old_value = tx.codex_root_old.strip()
    marker_rel = STATE_REGISTRY["state.codex-workspace-marker"].destination_path
    if old_value:
        fd = secure_open(target, marker_rel,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            write_all(fd, (old_value + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        try:
            secure_unlink(target, marker_rel)
        except FileNotFoundError:
            pass


def _finish_rollback(target: Path, tx: TransactionRecord,
                     final_status: TransactionStatus) -> RecoveryResult:
    """Restore external pre-bytes before deleting their transaction backups."""
    _restore_marker_backup(target, tx)
    failures = restore_external_links(target, tx)
    if failures:
        tx.status = "needs_human"
        tx.detail = f"外部步骤补偿失败: {failures}"
        write_transaction_atomic(target, tx)
        return RecoveryResult("needs_human", detail=tx.detail)
    cleanup_staging(target, tx)
    tx.status = final_status
    write_transaction_atomic(target, tx)
    return RecoveryResult(
        "rolled_back_to_clean" if final_status == "failed" else "rolled_back")


def recover(target: Path) -> RecoveryResult:
    """恢复入口（生命周期流程步骤 2 调用；持锁者是唯一恢复者）。"""
    txs = find_unresolved(target)
    if not txs:
        return RecoveryResult("no_action")
    blocked = [t for t in txs if t.status == "needs_human"]
    if blocked:
        return RecoveryResult(
            "blocked",
            detail=f"存在 needs_human 事务，阻断一切生命周期操作: "
                   f"{[(t.tx_id, t.detail) for t in blocked]}")
    if len(txs) > 1:
        return RecoveryResult(
            "needs_human",
            detail=f"多个未完成事务（互斥锁下不应出现）: {[t.tx_id for t in txs]}")
    tx = txs[0]
    if not tx.mac_verified:
        return RecoveryResult(
            "needs_human",
            detail=f"HMAC 验证失败（密钥缺失或记录被篡改）: {tx.tx_id}")
    errors = validate_tx_record(tx)
    if errors:
        return RecoveryResult("needs_human", detail=f"事务记录校验失败: {errors}")

    if tx.status in ("planning", "staging"):
        # 写序不变量保证：此两种状态绝无已提交项，清理幂等
        return _finish_rollback(target, tx, "failed")

    if tx.status == "committing":
        classified = classify_committing(target, tx)

        if classified["CONFLICT"]:
            tx.status = "needs_human"
            tx.detail = f"conflicts: {[s.spec_id for s in classified['CONFLICT']]}"
            write_transaction_atomic(target, tx)
            return RecoveryResult("needs_human", detail=tx.detail)

        if not classified["NEW"]:
            return _finish_rollback(target, tx, "failed")

        if not classified["OLD"]:
            # 全部已提交 → roll-forward：补收尾
            failures = complete_internal_deletions(target, tx)
            if tx.kind == "install":
                manifest_error = _finalize_install_manifest(
                    target, classified["NEW"])
                if manifest_error:
                    tx.status = "needs_human"
                    tx.detail = manifest_error
                    write_transaction_atomic(target, tx)
                    return RecoveryResult("needs_human", detail=tx.detail)
                # 外部 create 步骤按记录补建（意图先行——第七轮审计：统一外部
                # 事务模型；记录含 strategy/expected_hash，copy 血统可验）
                if not failures:
                    failures = complete_external_creations(target, tx)
                if not failures and not tx.external:
                    # 崩溃早于 ensure_codex_links（无 create 记录）→ marker 回退：
                    # 从 marker + KIT_SKILLS 按 link_strategy 补建（升级场景）
                    from .registry import read_codex_workspace_marker
                    marker = read_codex_workspace_marker(target)
                    if marker is not None:
                        from .registry import KIT_SKILLS, CodexLinkSpec
                        for skill in KIT_SKILLS:
                            ls = CodexLinkSpec(skill_name=skill)
                            ls.validate(marker, target)
                            link = ls.derive_link_path(marker)
                            expected = ls.derive_target_path(target)
                            if link.is_symlink() or link.exists():
                                continue     # 已存在（用户处理或部分成功）
                            _recreate_external_link(link, expected, tx.link_strategy)
            else:
                # uninstall 的 roll-forward 只补删，绝不重建已删除的链接
                if not failures:
                    failures = complete_external_removals(target, tx)
            if failures:
                tx.status = "needs_human"
                tx.detail = f"事务收尾失败: {failures}"
                write_transaction_atomic(target, tx)
                return RecoveryResult("needs_human", detail=tx.detail)
            tx.status = "done"
            write_transaction_atomic(target, tx)
            cleanup_staging(target, tx)   # done 写入后再清理（与 install 一致）
            return RecoveryResult("roll_forward_completed")

        # 部分提交 → roll-back（倒序，只回滚 NEW）+ marker 恢复 + 外部步骤补偿
        for step in reversed(classified["NEW"]):
            if not rollback_one(target, step, tx):
                tx.status = "needs_human"
                tx.detail = "rollback aborted"
                write_transaction_atomic(target, tx)
                return RecoveryResult("needs_human", detail=tx.detail)

        _restore_marker_backup(target, tx)
        return _finish_rollback(target, tx, "rolled_back")

    return RecoveryResult("no_action")


def _write_manifest(target: Path, manifest: Manifest) -> None:
    from .manifest import write_manifest_atomic
    write_manifest_atomic(target, manifest)


def _rollforward_manifest(target: Path, steps) -> Manifest:
    """roll-forward 收尾的 Manifest：从已提交步骤重建，并保留既有清单中
    未被本事务覆盖的残留条目（v9「管理记录不丢失」——install 的正常路径
    assemble_manifest 会保留残留记录，恢复路径不得丢失它们）。
    既有清单损坏 → 只用重建结果（不阻断恢复收尾）。"""
    from .manifest import ManifestCorruptError, read_manifest
    from .registry import _SPEC_ORDER, current_kit_version
    rebuilt = build_manifest_from_tx(target, current_kit_version(), steps)
    try:
        old = read_manifest(target)
    except ManifestCorruptError:
        old = None
    if old is not None:
        have = {e.spec_id for e in rebuilt.entries}
        for e in old.entries:
            if e.spec_id not in have:
                rebuilt.entries.append(e)
        rebuilt.entries.sort(key=lambda e: _SPEC_ORDER.get(e.spec_id, len(_SPEC_ORDER)))
    return rebuilt


def _finalize_install_manifest(target: Path, steps) -> str | None:
    """Finish recovery without rewriting a transactionally committed manifest.

    New transactions contain ``state.manifest`` as their final CommitStep. Its
    staged bytes are authoritative. Older records have no such step and retain
    the reconstruction path for backward compatibility.
    """
    manifest_steps = [s for s in steps if s.spec_id == MANIFEST_SPEC_ID]
    if not manifest_steps:
        managed_steps = [
            s for s in steps
            if all(not sid.startswith("state.") for sid in s.spec_ids)
        ]
        _write_manifest(target, _rollforward_manifest(target, managed_steps))
        return None
    if len(manifest_steps) != 1:
        return f"manifest step 数量非法: {len(manifest_steps)}"
    step = manifest_steps[0]
    if step.kind != "replace" or step.post_container_hash is None:
        return "install manifest step 必须是带 post hash 的 replace"
    actual = file_sha256(derive_destination(target, MANIFEST_SPEC_ID))
    if actual != step.post_container_hash:
        return ("已提交 Manifest 与事务 post hash 不一致: "
                f"expected={step.post_container_hash} actual={actual}")
    return None


def recover_manual(target: Path, strategy: str) -> RecoveryResult:
    """§11.9 人工恢复入口（--recover=rollback | --recover=roll-forward）。
    约束：只对 MAC 验证通过的事务选择恢复策略；无论哪种策略，都不绕过
    CAS 校验（rollback_one / commit_one）与路径派生（§7）。（--force 不存在）"""
    txs = find_unresolved(target)
    if not txs:
        return RecoveryResult("no_action")
    if len(txs) > 1:
        return RecoveryResult(
            "needs_human",
            detail=f"多个未完成事务（互斥锁下不应出现）: {[t.tx_id for t in txs]}")
    tx = txs[0]
    if not tx.mac_verified:
        return RecoveryResult(
            "needs_human",
            detail=f"HMAC 验证失败（密钥缺失或记录被篡改）: {tx.tx_id}")
    errors = validate_tx_record(tx)
    if errors:
        return RecoveryResult("needs_human", detail=f"事务记录校验失败: {errors}")

    if tx.status in ("planning", "staging"):
        # 无已提交项：两种策略等价于清理
        return _finish_rollback(target, tx, "failed")

    classified = classify_committing(target, tx)
    if classified["CONFLICT"]:
        tx.status = "needs_human"
        tx.detail = f"conflicts: {[s.spec_id for s in classified['CONFLICT']]}"
        write_transaction_atomic(target, tx)
        return RecoveryResult("needs_human", detail=tx.detail)

    if strategy == "rollback":
        for step in reversed(classified["NEW"]):
            if not rollback_one(target, step, tx):
                tx.status = "needs_human"
                tx.detail = "rollback aborted"
                write_transaction_atomic(target, tx)
                return RecoveryResult("needs_human", detail=tx.detail)
        return _finish_rollback(target, tx, "rolled_back")

    if strategy == "roll-forward":
        for step in classified["OLD"]:
            result = commit_one(target, step, tx.tx_id)
            if result.status != "committed":
                tx.status = "needs_human"
                tx.detail = f"roll-forward 中止: {step.spec_id}: {result.detail or result.status}"
                write_transaction_atomic(target, tx)
                return RecoveryResult("needs_human", detail=tx.detail)
        failures = complete_internal_deletions(target, tx)
        if tx.kind == "install":
            manifest_error = _finalize_install_manifest(
                target, classified["NEW"] + classified["OLD"])
            if manifest_error:
                tx.status = "needs_human"
                tx.detail = manifest_error
                write_transaction_atomic(target, tx)
                return RecoveryResult("needs_human", detail=tx.detail)
        # 同 recover()：install 方向补建 create（记录含 strategy/血统）；
        # 卸载方向补删不重建
        if not failures:
            if tx.kind == "install":
                failures = complete_external_creations(target, tx)
            else:
                failures = complete_external_removals(target, tx)
        if failures:
            tx.status = "needs_human"
            tx.detail = f"事务收尾失败: {failures}"
            write_transaction_atomic(target, tx)
            return RecoveryResult("needs_human", detail=tx.detail)
        tx.status = "done"
        write_transaction_atomic(target, tx)
        cleanup_staging(target, tx)   # done 写入后再清理（与 recover 一致）
        return RecoveryResult("roll_forward_completed")

    return RecoveryResult("needs_human", detail=f"未知恢复策略: {strategy!r}")
