"""manifest.py — Manifest v2（§4）：schema + 原子读写 + 安全校验 + 缩减版写回。

Manifest 是目标仓库内的记录：路径不可信（从 Registry 派生）、授权不可信
（血统证明另行判定）、before_state 是纯审计字段——永不参与删除决策。
唯一权威路径 = STATE_REGISTRY["state.manifest"] = .repo-memory-kit/manifest.json。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import platform as _plat
from .registry import (
    STATE_REGISTRY,
    secure_open,
    secure_replace,
    write_all,
)

MANIFEST_VERSION = 2
MANIFEST_SPEC_ID = "state.manifest"
# 与 registry.STATE_REGISTRY 中的 destination 保持一致（单一权威定义）
MANIFEST_REL = STATE_REGISTRY[MANIFEST_SPEC_ID].destination_path


class ManifestCorruptError(Exception):
    """manifest.json 存在但无法解析/结构非法。调用方：install → 阻断报告；
    uninstall → 拒绝卸载；doctor → INCOMPLETE。严禁当作'无 Manifest'。"""


@dataclass
class ManifestEntry:
    spec_id: str               # 必须存在于 REGISTRY（读取时过滤未知 spec_id）
    action: Literal["created", "updated_kit_version", "adopted_legacy"]
    before_state: Literal["absent", "unknown"] | str  # absent | unknown | previous_canonical_hash:<sha256>
    installed_hash: str        # 实际片段字节 hash（漂移检测）
    canonical_hash: str        # 路径规范化 hash（血统证明）
    codex_root: str | None     # 仅审计展示；禁止用于派生任何删除路径

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "action": self.action,
            "before_state": self.before_state,
            "installed_hash": self.installed_hash,
            "canonical_hash": self.canonical_hash,
            "codex_root": self.codex_root,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ManifestEntry":
        if not isinstance(data, dict):
            raise ManifestCorruptError("manifest entry 不是对象")
        try:
            return cls(
                spec_id=str(data["spec_id"]),
                action=str(data["action"]),
                before_state=str(data["before_state"]),
                installed_hash=str(data["installed_hash"]),
                canonical_hash=str(data["canonical_hash"]),
                codex_root=data.get("codex_root"),
            )
        except KeyError as e:
            raise ManifestCorruptError(f"manifest entry 缺字段: {e}")


@dataclass
class Manifest:
    manifest_version: int     # 2
    kit_version: str
    target_repo: str          # 安装时的绝对路径
    installed_at: str
    entries: list[ManifestEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "manifest_version": self.manifest_version,
            "kit_version": self.kit_version,
            "target_repo": self.target_repo,
            "installed_at": self.installed_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Manifest":
        if not isinstance(data, dict):
            raise ManifestCorruptError("manifest 顶层不是对象")
        if data.get("manifest_version") != MANIFEST_VERSION:
            raise ManifestCorruptError(
                f"manifest_version 非法: {data.get('manifest_version')!r}（期望 {MANIFEST_VERSION}）")
        try:
            entries = [ManifestEntry.from_dict(e) for e in data["entries"]]
            kit_version = str(data["kit_version"])
            target_repo = str(data["target_repo"])
            installed_at = str(data["installed_at"])
        except KeyError as e:
            raise ManifestCorruptError(f"manifest 缺字段: {e}")
        return cls(
            manifest_version=MANIFEST_VERSION,
            kit_version=kit_version,
            target_repo=target_repo,
            installed_at=installed_at,
            entries=entries,
        )

    def entry_map(self) -> dict[str, ManifestEntry]:
        """spec_id → entry（首个匹配；正常情况下 spec_id 唯一）。"""
        out: dict[str, ManifestEntry] = {}
        for e in self.entries:
            out.setdefault(e.spec_id, e)
        return out


def read_manifest(target: Path) -> Manifest | None:
    """读取 Manifest。缺失 → None；存在但损坏 → ManifestCorruptError。
    未知 spec_id 的条目按 §4 忽略（不进 entries）。"""
    path = target / MANIFEST_REL
    try:
        fd = os.open(path, os.O_RDONLY | _plat.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ManifestCorruptError(f"manifest 不可读: {e}")
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
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ManifestCorruptError(f"manifest JSON 解析失败: {e}")
    manifest = Manifest.from_dict(data)
    # §4 安全规则：清单只记录受管资源。state.* 是事务实现细节，既不能
    # 授予删除权，也不能作为 residual 在后续安装中永久传播。
    from .registry import REGISTRY_BY_ID
    manifest.entries = [
        e for e in manifest.entries if e.spec_id in REGISTRY_BY_ID]
    return manifest


def write_manifest_atomic(target: Path, manifest: Manifest) -> None:
    """原子写 Manifest（secure_open 临时文件 + secure_replace 双 dir_fd）。
    路径权威 = state.manifest；.repo-memory-kit 为基础设施目录（§11.1 例外）。"""
    from .registry import secure_mkdir
    secure_mkdir(target, str(Path(MANIFEST_REL).parent))
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False,
                         sort_keys=True, indent=2).encode() + b"\n"
    tmp_rel = f"{MANIFEST_REL}.{uuid.uuid4()}.tmp"
    fd = secure_open(target, tmp_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     STATE_REGISTRY[MANIFEST_SPEC_ID].expected_mode or 0o600)
    try:
        write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    secure_replace(target, tmp_rel, MANIFEST_REL)


def build_manifest(target: Path, kit_version: str,
                   entries: list[ManifestEntry]) -> Manifest:
    """全新 Manifest（install 完成方向 / --adopt-legacy / roll-forward 收尾）。"""
    return Manifest(
        manifest_version=MANIFEST_VERSION,
        kit_version=kit_version,
        target_repo=str(target),
        installed_at=datetime.now(timezone.utc).isoformat(),
        entries=entries,
    )


def reduced_manifest(manifest: Manifest,
                     residual: list[ManifestEntry]) -> Manifest:
    """部分卸载：只含残留项的缩减版 Manifest（残留条目原样保留，§13）。
    installed_at 刷新（本次操作时间）；其余字段不变。"""
    return Manifest(
        manifest_version=manifest.manifest_version,
        kit_version=manifest.kit_version,
        target_repo=manifest.target_repo,
        installed_at=datetime.now(timezone.utc).isoformat(),
        entries=list(residual),
    )


def manifest_payload_bytes(manifest: Manifest) -> bytes:
    """缩减版 Manifest 的 staged 内容（与 write_manifest_atomic 字节形态一致，
    保证 post_container_hash 可复算）。"""
    return json.dumps(manifest.to_dict(), ensure_ascii=False,
                      sort_keys=True, indent=2).encode() + b"\n"


def build_manifest_from_tx(target: Path, kit_version: str, steps) -> Manifest:
    """roll-forward 收尾（§11.7）：从已提交步骤（CommitStep）构建 Manifest。

    before_state 取 "unknown"——恢复路径无法可信重建 plan 前的片段形态，
    而该字段是纯审计字段，永不参与决策（§4）。
    steps 为 transaction.CommitStep 的鸭子类型列表（避免模块循环依赖）。"""
    from .registry import (
        FRAGMENT_ABSENT,
        REGISTRY_BY_ID,
        compute_fragment_hashes,
        generate_fragment,
        read_fragment,
        resolve_spec,
    )
    entries: list[ManifestEntry] = []
    for step in steps:
        for sid in step.spec_ids:
            # State files are transaction machinery, not managed-resource
            # entries. Older recovery code may pass a mixed plan here.
            if sid not in REGISTRY_BY_ID:
                continue
            spec = resolve_spec(sid)
            dst = target / spec.destination_path
            if spec.resource_type == "seed_file":
                frag = generate_fragment(spec, target)   # §2：不读用户种子
            else:
                frag = read_fragment(dst, spec)
            if frag is FRAGMENT_ABSENT:
                continue          # 已提交但片段读不出（异常现场）——不伪造记录
            installed, canonical = compute_fragment_hashes(frag, target)
            entries.append(ManifestEntry(
                spec_id=sid,
                action=("created" if step.pre_container_hash is None
                        else "updated_kit_version"),
                before_state="unknown",
                installed_hash=installed,
                canonical_hash=canonical,
                codex_root=None))
    return build_manifest(target, kit_version, entries)
