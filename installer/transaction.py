"""transaction.py — 事务模型（§11）：互斥锁 + 事务生命周期 + HMAC + 校验 + 崩溃恢复。

三条写序不变量（崩溃恢复正确性的根基，§11.1）：
1. 完整 plan 先于受管资源副作用（基础设施目录例外：锁目录与 tx/<id> 容器目录
   允许在 plan 落盘前创建——幂等、无资源副作用，否则首次安装 ENOENT）
2. committing 先于首个替换
3. 外部副作用意图先行（codex link：intent 落盘 fsync → unlink → 记完成）
"""
from __future__ import annotations

import fcntl
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

from .manifest import Manifest, build_manifest_from_tx
from .registry import (
    CodexLinkSpec,
    SecurityError,
    _SPEC_ORDER,
    derive_destination,
    derive_transaction_paths,
    file_sha256,
    fsync_dir,
    resolve_spec,
    secure_mkdir,
    secure_open,
    secure_replace,
    secure_rmdir,
    secure_unlink,
    staged_rel,
    validate_tx_id,
    write_all,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")

TransactionStatus = Literal[
    "planning",       # 内容生成与 pre 状态采集全部在内存完成后，一次性写入完整 plan
    "staging",        # staged 文件落盘中（纯 I/O，内容已定）。崩溃 → 全部可安全清理
    "committing",     # 完整 plan 已落盘。★ 此状态落盘必须发生在第一个 os.replace/unlink 之前
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
    """target 外的副作用（codex link 删除）。
    意图先行：state="intent" 落盘 fsync → unlink → state="removed" 落盘。
    恢复以磁盘现状为准（§11.7），state 是线索不是事实。"""
    spec_id: str
    skill_name: str
    codex_root: str                # 本次卸载 CLI 重传的 --codex-root（HMAC 保护下可回放）
    prior_state: Literal["absent", "pointing_to_target", "other"]
    state: Literal["intent", "removed"] = "intent"

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "skill_name": self.skill_name,
            "codex_root": self.codex_root,
            "prior_state": self.prior_state,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ExternalStep":
        return cls(
            spec_id=str(data["spec_id"]),
            skill_name=str(data["skill_name"]),
            codex_root=str(data["codex_root"]),
            prior_state=str(data["prior_state"]),   # type: ignore[arg-type]
            state=str(data.get("state", "intent")),  # type: ignore[arg-type]
        )


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
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return -1
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
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
        fd = os.open(tx_key_path(target), os.O_RDONLY | os.O_NOFOLLOW)
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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
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
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
        src_fd = os.open(paths.dst, os.O_RDONLY | os.O_NOFOLLOW)
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
        backup_fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW)
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
    for rel in (f"{tx_dir}/backups", tx_dir):
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
            if not dst.exists():
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
                if rec.state != "removed":
                    rec.state = "removed"      # 原路径和隔离名都不在 → 完成
                continue
            # 原路径或隔离名存在 → 走安全删除（内部会处理孤儿和原子恢复）
            safe_unlink_external_link(link, expected)
            rec.state = "removed"
        except (OSError, SecurityError) as e:
            failures.append(f"{rec.spec_id}: {e}")
    return failures


def restore_external_links(target: Path, tx: TransactionRecord) -> list[str]:
    """可补偿外部步骤的逆操作：以**磁盘现状**为准（记录只是线索）。
    - 只处理 prior_state == "pointing_to_target" 的步骤
    - 链接路径由 skill_name + codex_root 经 CodexLinkSpec 重新派生并验证
    - 链接仍在磁盘上（无论记录处于 intent 还是 removed）→ 无需补偿；
      链接已消失 → 重建（覆盖崩溃窗口：unlink 后、removed 记录前）
    - 返回失败清单；非空 → 调用方进 needs_human（绝不静默吞掉）"""
    failures = []
    for rec in tx.external:
        if rec.prior_state != "pointing_to_target":
            continue
        try:
            spec = resolve_spec(rec.spec_id)
            # ★ 双重验证（v11）：记录中的 skill_name 必须等于 Registry 的
            # link_skill_name；codex_root 必须过 CodexLinkSpec.validate
            if rec.skill_name != spec.link_skill_name:
                failures.append(f"{rec.spec_id}: 记录的 skill_name 与 Registry 不符")
                continue
            codex_root = Path(rec.codex_root)
            link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
            link_spec.validate(codex_root, target)
            link = link_spec.derive_link_path(codex_root)
            expected = link_spec.derive_target_path(target)
            if link.is_symlink() or link.exists():
                continue                     # 删除未发生或用户已自行处理
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(expected), link)
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
    """planning/staging/无 NEW 的 committing 收尾：清理 → marker 恢复 →
    外部补偿 → 状态落盘。"""
    _restore_marker_backup(target, tx)
    cleanup_staging(target, tx)
    failures = restore_external_links(target, tx)
    if failures:
        tx.status = "needs_human"
        tx.detail = f"外部步骤补偿失败: {failures}"
        write_transaction_atomic(target, tx)
        return RecoveryResult("needs_human", detail=tx.detail)
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
            if tx.kind == "install":
                _write_manifest(target, _rollforward_manifest(
                    target, classified["NEW"]))
                # P2 回归：roll-forward 补建 workspace 链接（崩溃窗口——
                # 内部资源已提交但链接创建前崩溃；从 marker 读 codex_root）
                from .registry import read_codex_workspace_marker
                marker = read_codex_workspace_marker(target)
                if marker is not None:
                    from .uninstall import safe_unlink_external_link
                    import glob as _glob
                    from .registry import CodexLinkSpec, KIT_SKILLS
                    for skill in KIT_SKILLS:
                        ls = CodexLinkSpec(skill_name=skill)
                        ls.validate(marker, target)
                        link = ls.derive_link_path(marker)
                        expected = str(ls.derive_target_path(target))
                        if link.is_symlink() or link.exists():
                            continue         # 已存在（用户处理或部分成功）
                        link.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(expected, link)
            # 外部步骤：install 事务无外部步骤；uninstall 的 roll-forward 只补删
            # （complete_external_removals），绝不重建已删除的链接
            failures = (complete_external_removals(target, tx)
                        if tx.kind == "uninstall" else [])
            if failures:
                tx.status = "needs_human"
                tx.detail = f"外部步骤收尾失败: {failures}"
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
        if tx.kind == "install":
            _write_manifest(target, _rollforward_manifest(
                target, classified["NEW"] + classified["OLD"]))
        # 同 recover()：卸载 roll-forward 补删不重建；install 无外部步骤
        failures = (complete_external_removals(target, tx)
                    if tx.kind == "uninstall" else [])
        if failures:
            tx.status = "needs_human"
            tx.detail = f"外部步骤收尾失败: {failures}"
            write_transaction_atomic(target, tx)
            return RecoveryResult("needs_human", detail=tx.detail)
        tx.status = "done"
        write_transaction_atomic(target, tx)
        cleanup_staging(target, tx)   # done 写入后再清理（与 recover 一致）
        return RecoveryResult("roll_forward_completed")

    return RecoveryResult("needs_human", detail=f"未知恢复策略: {strategy!r}")
