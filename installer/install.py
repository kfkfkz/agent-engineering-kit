"""install.py — 安装方向（§12）：内存内容生成 → 完整 journal → staging → CAS-commit。

流程（严格按 §12）：
0. acquire_install_lock（全程持有）
1. 前置检查（needs_human 阻断 / Python 版本在 CLI 入口检查 / 目标存在）
2. recover(target)
3. 创建事务 + 基础设施目录（tx/<id> 与 backups——§11.1 不变量 1 例外）
4. Preflight（只读；conflict/drifted/不可读 → 跳过并报告）
5. Plan + 内容生成（全部在内存）→ ★ 完整 plan 一次性原子写入（不变量 1）
6. Staging（纯落盘 I/O）
7. Validate（编译 / JSON / staged hash）
8. Backup（仅对实际存在的文件）
9. ★ committing 落盘（不变量 2：先于首个 os.replace/unlink）
10. Commit（逐组 CAS + 进度落盘）
11. workspace 外部步骤（意图先行，可恢复）
12. done
13. 清理 + 释放锁
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import platform as _plat
from .manifest import (
    MANIFEST_SPEC_ID,
    Manifest,
    ManifestCorruptError,
    ManifestEntry,
    build_manifest,
    manifest_payload_bytes,
    read_manifest,
)
from .preflight import PreflightResult, run_preflight
from .registry import (
    _SPEC_ORDER,
    FRAGMENT_ABSENT,
    HOOK_MATCHER,
    KIT_DIR,
    STATE_REGISTRY,
    CodexLinkSpec,
    ResourceSpec,
    compute_fragment_hashes,
    current_kit_version,
    file_sha256,
    fragment_from_bytes,
    generate_fragment,
    generate_hook,
    legacy_state_owned,
    resolve_spec,
    secure_mkdir,
    staged_rel,
)
from .transaction import (
    CommitStep,
    ExternalStep,
    TransactionRecord,
    acquire_install_lock,
    backup_container,
    cleanup_staging,
    commit_one,
    recover,
    stage_resource,
)

# ══════════════════════════ §11.5 内容生成（内存） ══════════════════════════

def merge_fragment(container: dict, fragment: Any, locator: str) -> dict:
    """按 locator 逐级写入 JSON 片段（值为 null 的 key 算存在——只覆盖 kit 自己的 key）。"""
    keys = locator.split(".")
    current = container
    for key in keys[:-1]:
        existing = current.get(key)
        if existing is None:
            current[key] = {}
        elif not isinstance(existing, dict):
            raise ValueError(f"容器路径 {'.'.join(keys)} 的 {key} 不是对象"
                             f"（是 {type(existing).__name__}），拒绝覆盖用户内容")
        current = current[key]
    current[keys[-1]] = fragment
    return container


def merge_hook(container: dict, hook: dict, spec: ResourceSpec) -> dict:
    """合并式写入 SessionStart 钩子：command 精确匹配的既有条目原地更新；
    否则并入 matcher 相同的 entry，或追加新 entry。"""
    hooks = container.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"hooks 不是对象: {spec.destination_path}")
    ss = hooks.setdefault("SessionStart", [])
    if not isinstance(ss, list):
        raise ValueError(f"hooks.SessionStart 不是数组: {spec.destination_path}")
    for entry in ss:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for h in entry["hooks"]:
            if isinstance(h, dict) and h.get("command") == spec.expected_hook_command:
                h.clear()
                h.update(hook)
                return container
    for entry in ss:
        if isinstance(entry, dict) and entry.get("matcher") == HOOK_MATCHER:
            entry_hooks = entry.get("hooks")
            if isinstance(entry_hooks, list):
                entry_hooks.append(hook)
                return container
    ss.append({"matcher": HOOK_MATCHER, "hooks": [hook]})
    return container


def insert_or_replace_block(text: str, spec: ResourceSpec, target: Path) -> str:
    """托管区块写入（managed_block，逐片段累积）：
    - 有完整区块 → 整块替换为当前 kit 区块
    - 有 start 无 end（存量旧行组，§2 .gitignore 场景）→ start 至 EOF 升级为带 end 区块
    - 无标记但存在旧行组（无标记形态——只有血统判定 adopted_legacy 的资源会
      走到这里，conflict 的在 preflight 已被跳过）→ 旧行组升级为标记区块
    - 无区块 → 文件末尾追加（前置空行分隔；空文件直接写区块）"""
    block = generate_fragment(spec, target)
    if spec.block_start in text:
        start_idx = text.index(spec.block_start)
        if spec.block_end in text[start_idx:]:
            end_idx = text.index(spec.block_end, start_idx) + len(spec.block_end)
            return text[:start_idx] + block + text[end_idx:]
        return text[:start_idx] + block          # 未闭合：升级为带 end 的区块
    from .registry import legacy_section_span
    span = legacy_section_span(spec, text)
    if span is not None:
        return text[:span[0]] + block + text[span[1]:]   # 旧行组 → 标记区块
    if text and not text.endswith("\n"):
        text += "\n"
    return (text + "\n" + block) if text else block


def build_final_content(target: Path, group: list[ResourceSpec],
                        pre_bytes: bytes | None) -> bytes:
    """同一 destination 分组的完整最终文件（容器只读一次——pre_bytes 即其内容）。
    约束：owned_file / seed_file 不参与多片段分组（Registry 作者约束）；
    同组的容器型 spec 必须同族（全 JSON 系或全文本系）。"""
    if len(group) == 1:
        spec = group[0]
        if spec.resource_type == "owned_file":
            return (KIT_DIR / spec.source_path).read_bytes()
        if spec.resource_type == "seed_file":
            return pre_bytes if pre_bytes is not None else (KIT_DIR / spec.source_path).read_bytes()

    spec0 = group[0]

    if spec0.resource_type in ("json_fragment", "hook"):
        container = json.loads(pre_bytes.decode("utf-8")) if pre_bytes is not None else {}
        if not isinstance(container, dict):
            raise ValueError(f"容器顶层不是对象: {spec0.destination_path}")
        for spec in group:
            if spec.resource_type == "json_fragment":
                fragment = generate_fragment(spec, target)
                container = merge_fragment(container, fragment, spec.locator)
            elif spec.resource_type == "hook":
                container = merge_hook(container, generate_hook(spec, target), spec)
            else:
                raise ValueError(f"混合分组类型不兼容: {spec.id}")
        return json.dumps(container, ensure_ascii=False, indent=2).encode() + b"\n"

    if spec0.resource_type == "managed_block":
        text = pre_bytes.decode("utf-8") if pre_bytes is not None else ""
        for spec in group:
            text = insert_or_replace_block(text, spec, target)  # 逐片段累积
        return text.encode()

    raise ValueError(f"不可分组的资源类型: {spec0.resource_type}")


# ══════════════════════════ 分组与 plan（内存） ══════════════════════════

@dataclass
class GroupPlan:
    step: CommitStep
    content: bytes                      # 最终内容（staging 输入；post hash 来源）
    mode: int
    specs: list[ResourceSpec]
    before_states: dict[str, str] = field(default_factory=dict)   # spec_id → before_state（审计）


def group_specs(specs: list[ResourceSpec]) -> list[list[ResourceSpec]]:
    """按 destination 分组；组内按 Registry 全序，组间按锚的全序。"""
    groups: dict[str, list[ResourceSpec]] = {}
    for spec in specs:
        groups.setdefault(spec.destination_path, []).append(spec)
    for group in groups.values():
        group.sort(key=lambda s: _SPEC_ORDER[s.id])
    return sorted(groups.values(),
                  key=lambda g: _SPEC_ORDER[g[0].id])


def _read_container(dst: Path) -> tuple[bytes | None, int | None]:
    """只读一次容器（O_NOFOLLOW）；返回 (bytes | None, mode | None)。"""
    try:
        fd = os.open(dst, os.O_RDONLY | _plat.O_NOFOLLOW | _plat.O_BINARY)
    except FileNotFoundError:
        return None, None
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise ValueError(f"目标不是普通文件: {dst}")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat_module.S_IMODE(st.st_mode)
    finally:
        os.close(fd)


class PlanError(Exception):
    """plan 阶段读容器失败（路径被文件占位 / 编码异常等）——发生在任何落盘
    副作用之前，事务以 failed 收尾即可，现场干净。"""

    def __init__(self, spec_id: str, cause: BaseException):
        self.spec_id = spec_id
        self.cause = cause
        super().__init__(f"plan 阶段读取 {spec_id} 的容器失败: {cause}")


def plan_groups(target: Path, install_set: list[ResourceSpec]) -> list[GroupPlan]:
    """§12 步骤 5：每组读容器一次 → pre hash/mode → build_final_content（内存）
    → post hash。纯内存 + 只读，无任何落盘副作用。"""
    plans: list[GroupPlan] = []
    for group in group_specs(install_set):
        spec0 = group[0]
        dst = target / spec0.destination_path
        try:
            pre_bytes, pre_mode = _read_container(dst)
        except (OSError, ValueError) as e:
            raise PlanError(spec0.id, e)
        pre_hash = (hashlib.sha256(pre_bytes).hexdigest()
                    if pre_bytes is not None else None)
        try:
            content = build_final_content(target, group, pre_bytes)
        except (OSError, ValueError) as e:
            raise PlanError(spec0.id, e)
        step = CommitStep(
            spec_id=spec0.id,
            spec_ids=tuple(s.id for s in group),
            kind="replace",
            pre_container_hash=pre_hash,
            post_container_hash=hashlib.sha256(content).hexdigest(),
            pre_mode=pre_mode,
        )
        # before_state（纯审计字段，§4）：plan 前片段的 canonical hash
        before_states: dict[str, str] = {}
        for spec in group:
            if pre_bytes is None:
                before_states[spec.id] = "absent"
            else:
                try:
                    frag = fragment_from_bytes(spec, pre_bytes)
                except (ValueError, UnicodeDecodeError) as e:
                    raise PlanError(spec0.id, e)
                before_states[spec.id] = (
                    "absent" if frag is FRAGMENT_ABSENT else
                    f"previous_canonical_hash:{compute_fragment_hashes(frag, target)[1]}")
        mode = _staging_mode(group[0], pre_mode)
        plans.append(GroupPlan(step=step, content=content, mode=mode,
                               specs=group, before_states=before_states))
    return plans


def _staging_mode(spec: ResourceSpec, pre_mode: int | None) -> int:
    """staged 文件权限：owned_file 由 kit 期望值决定（bin 必须 0755）；
    容器保留既有 mode；均缺失时按 spec 期望或 0644。"""
    if spec.resource_type == "owned_file" and spec.expected_mode is not None:
        return spec.expected_mode
    if pre_mode is not None:
        return pre_mode
    return spec.expected_mode or 0o644


# ══════════════════════════ §12 步骤 7 Validate（只读 staged） ══════════════════════════

def validate_staged(target: Path, plans: list[GroupPlan], tx_id: str) -> list[str]:
    errors: list[str] = []
    for gp in plans:
        if gp.step.kind == "delete":
            continue
        staged = target / staged_rel(resolve_spec(gp.step.spec_id), tx_id)
        try:
            if not stat_module.S_ISREG(staged.lstat().st_mode):
                errors.append(f"{gp.step.spec_id}: staged 不是普通文件")
                continue
        except FileNotFoundError:
            errors.append(f"{gp.step.spec_id}: staged 文件缺失")
            continue
        # staged hash 与 post 一致
        if hashlib.sha256(staged.read_bytes()).hexdigest() != gp.step.post_container_hash:
            errors.append(f"{gp.step.spec_id}: staged hash 与 post 不一致")
            continue
        # Python 编译检查（工具/生成器）
        if gp.content.startswith(b"#!/usr/bin/env python3"):
            try:
                compile(gp.content.decode("utf-8"), str(staged), "exec")
            except (SyntaxError, UnicodeDecodeError) as e:
                errors.append(f"{gp.step.spec_id}: Python 编译失败: {e}")
        # JSON 格式检查（容器为 JSON 的分组）
        if gp.step.spec_id in ("mcp-claude", "session-hook"):
            try:
                json.loads(gp.content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                errors.append(f"{gp.step.spec_id}: staged JSON 非法: {e}")
    return errors


# ══════════════════════════ Manifest 组装（§12 步骤 11） ══════════════════════════

def assemble_manifest(target: Path, preflight: PreflightResult,
                      plans: list[GroupPlan], old_manifest: Manifest | None,
                      codex_root_cli: Path | None) -> Manifest:
    entries: list[ManifestEntry] = []
    committed_ids: set[str] = set()
    for gp in plans:
        for spec in gp.specs:
            committed_ids.add(spec.id)
            frag = fragment_from_bytes(spec, gp.content)
            installed, canonical = compute_fragment_hashes(frag, target)
            status = preflight.status_of(spec.id)
            if status == "adopted_legacy":
                action = "adopted_legacy"
            elif gp.step.pre_container_hash is None:
                action = "created"
            else:
                action = "updated_kit_version"
            entries.append(ManifestEntry(
                spec_id=spec.id, action=action,
                before_state=gp.before_states.get(spec.id, "unknown"),
                installed_hash=installed, canonical_hash=canonical,
                codex_root=None))

    # codex_link：--codex-root 传入时逐链接登记（哈希取期望链接目标的确定性指纹）
    if codex_root_cli is not None:
        for spec in preflight.external_set:
            committed_ids.add(spec.id)
            expected = str(CodexLinkSpec(skill_name=spec.link_skill_name)
                           .derive_target_path(target))
            digest = hashlib.sha256(expected.encode()).hexdigest()
            entries.append(ManifestEntry(
                spec_id=spec.id, action="created", before_state="absent",
                installed_hash=digest, canonical_hash=digest,
                codex_root=str(codex_root_cli)))

    # 残留记录保留：被跳过但先前有记录的条目原样保留（v9 原则：管理记录不丢失）
    if old_manifest is not None:
        for e in old_manifest.entries:
            if e.spec_id not in committed_ids:
                entries.append(e)

    entries.sort(key=lambda e: _SPEC_ORDER.get(e.spec_id, len(_SPEC_ORDER)))
    return build_manifest(target, current_kit_version(), entries)


# ══════════════════════════ §16 安装方向的 codex 链接（post-commit 尽力步骤） ══════════════════════════

def ensure_codex_links(target: Path, codex_root: Path,
                       preflight: PreflightResult, tx,
                       strategy: str = "symlink") -> tuple[list[str], list[str]]:
    """安装方向技能暴露到 workspace——统一外部事务模型（第七轮审计 P1：
    外部资源创建进事务状态机，不靠字符串报告表达成功/失败）。

    意图先行（写序不变量 3 对外部副作用同样成立）：
    1. 逐技能记录 ExternalStep(operation=create, state=intent) 落盘
    2. 创建（symlink / copy——copy 记录源 SKILL.md sha256 作血统）
    3. state=applied 落盘

    冲突（目标被用户内容占用）→ 记入 failures，不创建、不覆盖；
    调用方检查 failures 决定 needs_human。返回 (reports, failures)。"""
    import hashlib as _hashlib
    import shutil as _shutil
    reports: list[str] = []
    failures: list[str] = []
    for spec in preflight.external_set:
        link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
        link_spec.validate(codex_root, target)
        link = link_spec.derive_link_path(codex_root)
        expected = link_spec.derive_target_path(target)
        source_md = expected / "SKILL.md"
        src_hash = (_hashlib.sha256(source_md.read_bytes()).hexdigest()
                    if source_md.is_file() else None)
        step = ExternalStep(
            spec_id=spec.id, skill_name=spec.link_skill_name,
            codex_root=str(codex_root), prior_state="absent",
            operation="create", strategy=strategy, expected_hash=src_hash)
        if strategy == "copy":
            dest = link / "SKILL.md"
            if dest.is_file():
                if src_hash and _hashlib.sha256(
                        dest.read_bytes()).hexdigest() == src_hash:
                    step.prior_state, step.state = "content_matches", "applied"
                    tx.external.append(step); tx.write(target)
                    continue                       # 已在（重装幂等）
                step.prior_state = "other"
                tx.external.append(step); tx.write(target)
                failures.append(f"{dest} 已存在且内容不同，保留现场未创建")
                continue
            if link.exists() and not link.is_dir():
                step.prior_state = "other"
                tx.external.append(step); tx.write(target)
                failures.append(f"{link} 已存在且不是目录，保留现场未创建")
                continue
            tx.external.append(step); tx.write(target)      # intent 先行
            link.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(source_md, dest)
            step.state = "applied"
            tx.write(target)
            reports.append(f"✓ 已复制技能到 Codex workspace {dest}")
        else:
            if link.is_symlink():
                if os.readlink(link) == str(expected):
                    step.prior_state, step.state = "pointing_to_target", "applied"
                    tx.external.append(step); tx.write(target)
                    continue                       # 已在（重装幂等）
                step.prior_state = "other"
                tx.external.append(step); tx.write(target)
                failures.append(f"{link} 已指向其他目标，保留现场未创建")
                continue
            if link.exists():
                step.prior_state = "other"
                tx.external.append(step); tx.write(target)
                failures.append(f"{link} 已存在且不是符号链接，保留现场未创建")
                continue
            tx.external.append(step); tx.write(target)      # intent 先行
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(expected), link)
            step.state = "applied"
            tx.write(target)
            reports.append(f"✓ 已创建 Codex workspace 技能链接 {link}")
    if preflight.external_set:
        from .registry import STATE_REGISTRY, secure_open, write_all
        marker_rel = STATE_REGISTRY["state.codex-workspace-marker"].destination_path
        fd = secure_open(target, marker_rel,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            write_all(fd, (str(codex_root) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    return reports, failures


# ══════════════════════════ 主流程（§12） ══════════════════════════

def run_install(target: Path, *, repair: bool = False,
                codex_root: Path | None = None,
                dry_run: bool = False,
                lightweight: bool = False,
                link_strategy: str = "symlink") -> int:
    target = target.resolve()
    if not target.is_dir():
        print(f"✗ 目标目录不存在: {target}")
        return 1

    if dry_run:
        return _dry_run(target, repair, codex_root)

    lock_fd = acquire_install_lock(target)           # 步骤 0（全程持有）
    try:
        # 步骤 1-2：needs_human 阻断 + 恢复遗留事务（持锁者是唯一恢复者）
        rr = recover(target)
        if rr.status in ("blocked", "needs_human"):
            print(f"✗ {rr.detail}")
            return 1
        if rr.status != "no_action":
            print(f"• 已恢复遗留事务: {rr.status}")

        # 步骤 3：创建事务 + 基础设施目录（§11.1 不变量 1 例外）
        tx = TransactionRecord.new(target, "install")
        tx.link_strategy = link_strategy      # 进 HMAC 保护记录——恢复不得改变产物类型（P1）
        secure_mkdir(target, f".repo-memory-kit/tx/{tx.tx_id}")
        secure_mkdir(target, f".repo-memory-kit/tx/{tx.tx_id}/backups")
        tx.status = "planning"
        tx.write(target)

        # 步骤 4：Preflight（只读；Manifest 损坏 → 阻断，不当作无清单）
        try:
            manifest = read_manifest(target)
        except ManifestCorruptError as e:
            tx.status = "failed"
            tx.write(target)
            print(f"✗ 安装清单损坏，拒绝继续（可人工修复或删除后重装）: {e}")
            return 1
        preflight = run_preflight(target, manifest, repair=repair,
                                  codex_root_cli=codex_root)
        if preflight.fatal:
            for line in preflight.fatal:
                print(f"✗ {line}")
            tx.status = "failed"
            tx.write(target)
            return 1

        # 步骤 5：plan + 内容生成（内存；读容器失败 → 事务 failed，现场干净）
        try:
            plans = plan_groups(target, preflight.install_set)
        except PlanError as e:
            print(f"✗ {e}（目标路径被占位/异常？）")
            tx.status = "failed"
            tx.write(target)
            return 1

        # 步骤 5.5：状态文件入事务（第八轮审计 P1：此前 manifest/治理在外部
        # 步骤前直接写盘，外部冲突回滚后形成"清单在、资源无"的分裂状态——
        # 现在与受管资源同一套 plan/stage/backup/commit/CAS-rollback 机制，
        # 提交顺序：受管资源 → 治理标记 → 治理规则 → Manifest 最后）
        profile = "lightweight" if lightweight else "strict"
        gov_path = target / ".repo-memory-kit" / "governance"
        gov_content = (profile + "\n").encode()
        gov_pre = file_sha256(gov_path) if gov_path.is_file() else None
        state_plans = [GroupPlan(
            step=CommitStep(
                spec_id="state.governance-profile",
                spec_ids=("state.governance-profile",), kind="replace",
                pre_container_hash=gov_pre,
                post_container_hash=hashlib.sha256(gov_content).hexdigest(),
                pre_mode=0o644 if gov_pre else None),
            content=gov_content, mode=0o644, specs=[])]
        # governance.json：**仅在缺失时创建**（团队定制文件永不覆盖、不入 plan）
        gov_json_path = target / ".repo-memory-kit" / "governance.json"
        if not gov_json_path.is_file():
            rules = {
                "version": 1,
                "rules": [
                    {"id": "SEC-001",
                     "require": ["receipt", "independent_review",
                                 "min_route:standard",
                                 "required_check:security-review"],
                     "match": {"paths": ["**/security/**", "**/auth/**"]}},
                    {"id": "DB-001",
                     "require": ["receipt", "independent_review",
                                 "min_route:standard",
                                 "required_check:migration-plan",
                                 "required_check:rollback-verification"],
                     "match": {"files": ["*migration*", "*schema*",
                                         "*ddl*", "*.sql"]}},
                ],
            }
            rules_bytes = (json.dumps(rules, ensure_ascii=False, indent=2)
                           + "\n").encode()
            state_plans.append(GroupPlan(
                step=CommitStep(
                    spec_id="state.governance-rules",
                    spec_ids=("state.governance-rules",), kind="replace",
                    pre_container_hash=None,
                    post_container_hash=hashlib.sha256(rules_bytes).hexdigest(),
                    pre_mode=None),
                content=rules_bytes, mode=0o644, specs=[]))
        # Retire a valid v1 manifest through the transaction. A later rollback
        # can then restore it from the normal backup instead of leaving the
        # repository with neither the v1 nor v2 ownership record.
        legacy_reports: list[str] = []
        legacy_spec = STATE_REGISTRY["state.legacy-manifest-v1"]
        legacy_path = target / legacy_spec.destination_path
        if legacy_path.is_file():
            if legacy_state_owned(target, legacy_spec.destination_path):
                try:
                    legacy_bytes, legacy_mode = _read_container(legacy_path)
                except (OSError, ValueError) as e:
                    print(f"✗ 无法读取退役状态文件 {legacy_spec.destination_path}: {e}")
                    tx.status = "failed"
                    tx.detail = f"{legacy_spec.id}: {e}"
                    tx.write(target)
                    return 1
                if legacy_bytes is not None:
                    state_plans.append(GroupPlan(
                        step=CommitStep(
                            spec_id=legacy_spec.id,
                            spec_ids=(legacy_spec.id,), kind="delete",
                            pre_container_hash=hashlib.sha256(
                                legacy_bytes).hexdigest(),
                            post_container_hash=None,
                            pre_mode=legacy_mode),
                        content=b"", mode=legacy_mode or 0o600, specs=[]))
                    legacy_reports.append(
                        f"• 已清理退役状态文件 {legacy_spec.destination_path}"
                        "（旧 install.sh 产物，v2 清单在 manifest.json）")
            else:
                legacy_reports.append(
                    f"• 退役状态文件 {legacy_spec.destination_path} 内容异常，"
                    "未自动清理（请人工确认）")
        # Manifest：内容在 plan 期一次成型（不变量 1——residual 合并在
        # assemble_manifest 内完成），作为**最后**一个提交步骤（完成标记）
        new_manifest = assemble_manifest(target, preflight, plans, manifest,
                                         codex_root)
        manifest_bytes = manifest_payload_bytes(new_manifest)
        m_dst = target / ".repo-memory-kit" / "manifest.json"
        m_pre = file_sha256(m_dst) if m_dst.is_file() else None
        m_mode = None
        if m_dst.is_file():
            try:
                m_mode = stat_module.S_IMODE(os.stat(m_dst).st_mode)
            except OSError:
                m_mode = None
        state_plans.append(GroupPlan(
            step=CommitStep(
                spec_id=MANIFEST_SPEC_ID, spec_ids=(MANIFEST_SPEC_ID,),
                kind="replace", pre_container_hash=m_pre,
                post_container_hash=hashlib.sha256(manifest_bytes).hexdigest(),
                pre_mode=m_mode),
            content=manifest_bytes, mode=0o600, specs=[]))
        plans = plans + state_plans

        # 步骤 6：Staging
        tx.plan = [gp.step for gp in plans]
        tx.write(target)          # ★ 完整 plan 一次性原子写入（不变量 1）
        tx.status = "staging"
        tx.write(target)
        for gp in plans:
            if gp.step.kind == "replace":
                stage_resource(target, gp.step.spec_id, tx.tx_id,
                               gp.content, mode=gp.mode)

        # 步骤 7：Validate（只读 staged）
        errors = validate_staged(target, plans, tx.tx_id)
        if errors:
            for line in errors:
                print(f"✗ {line}")
            cleanup_staging(target, tx)
            tx.status = "failed"
            tx.write(target)
            print("✗ 安装中止（staging 校验失败，已清理，无受管资源被修改）")
            return 1

        # 步骤 8：Backup（仅对实际存在的文件）
        for gp in plans:
            if not backup_container(target, gp.step, tx.tx_id):
                print(f"✗ 备份失败（目标在 plan 后被修改）: {gp.step.spec_id}")
                cleanup_staging(target, tx)
                tx.status = "failed"
                tx.write(target)
                return 1

        # 步骤 8.5：写 codex workspace marker（P1 回归——必须在 committing
        # 之前：首次安装的链接创建在 done 前发生，若在此处之前崩溃，
        # recover() 从 marker 读 codex_root 补建链接）。
        # 旧值存入事务记录 codex_root_old（HMAC 保护——P1 第六轮：旁路备份
        # 文件 O_TRUNC 崩溃窗口不可信，必须进事务记录）
        if codex_root is not None:
            from .registry import secure_open, write_all
            marker_rel = STATE_REGISTRY["state.codex-workspace-marker"].destination_path
            old_marker_path = target / marker_rel
            old_value = old_marker_path.read_text().strip() if old_marker_path.is_file() else ""
            tx.codex_root_old = old_value    # HMAC 保护的事务字段
            tx.write(target)                 # 刷新记录（含旧值）
            # 写新 marker
            fd = secure_open(target, marker_rel,
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                write_all(fd, (str(codex_root) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)

        # 步骤 9：★ committing 落盘（不变量 2：先于首个 os.replace/unlink）
        tx.status = "committing"
        tx.write(target)

        # 步骤 10：Commit（逐组；每组完成后更新进度）
        for gp in plans:
            result = commit_one(target, gp.step, tx.tx_id)
            if result.status == "needs_human":
                tx.status = "needs_human"
                tx.detail = f"{gp.step.spec_id}: {result.detail}"
                tx.write(target)
                print(f"✗ 事务转入 needs_human: {tx.detail}（evidence 保留；"
                      f"可运行 --recover=rollback|roll-forward 人工处置）")
                return 1
            if result.status == "conflict":
                # 目标在 plan 后被外部修改 → CAS 拒绝。恢复分类会把该步判为
                # CONFLICT → needs_human（evidence 保留，人工经 --recover 处置）
                print(f"✗ commit 冲突（目标在 plan 后被外部修改）: {gp.step.spec_id}，"
                      f"事务转入 needs_human")
                rr2 = recover(target)
                print(f"• 恢复分类结果: {rr2.status} {rr2.detail or ''}".rstrip())
                return 1
            tx.write(target)              # committed 标记进度落盘

        # 步骤 11-11.5：Manifest、治理文件与合法 v1 清单退役均已作为
        # state_plans 随提交循环落盘；这里不再执行事务外状态副作用。

        # 步骤 12：workspace 链接（在 done 之前——P2 回归：done 先写则崩溃
        # 窗口内 recover 无从补偿；链接在 done 前失败 → committing → 可恢复）
        link_reports, link_failures = [], []
        if codex_root is not None:
            link_reports, link_failures = ensure_codex_links(
                target, codex_root, preflight, tx, strategy=link_strategy)
        if link_failures:
            # 第七轮审计 P1：外部冲突不得吞掉——内部资源已提交，needs_human
            # 保留现场（绝不覆盖用户内容）；人工处理后 --recover=roll-forward 补建
            tx.status = "needs_human"
            tx.detail = ("外部技能暴露冲突（用户内容占用目标）: "
                         + "; ".join(link_failures[:3]))
            tx.write(target)
            print(f"✗ {tx.detail}")
            print("  人工移除冲突内容后运行: python3 -m installer "
                  "--recover=roll-forward <target> 补建链接")
            return 1

        # 步骤 13：done + 清理
        tx.status = "done"
        tx.write(target)
        cleanup_staging(target, tx)
    finally:
        os.close(lock_fd)

    _install_report(target, preflight, plans, codex_root)
    for line in legacy_reports:
        print(line)
    for line in link_reports:
        print(line)
    print("完成。需要沉淀经验时使用 memory-capture skill："
          "Claude Code `/memory-capture`；Codex `$memory-capture`。")
    return 0


def _dry_run(target: Path, repair: bool, codex_root: Path | None) -> int:
    """只读计划预览：不取锁、不建事务、不写任何文件。"""
    try:
        manifest = read_manifest(target)
    except ManifestCorruptError as e:
        print(f"✗ 安装清单损坏: {e}")
        return 1
    preflight = run_preflight(target, manifest, repair=repair,
                              codex_root_cli=codex_root)
    if preflight.fatal:
        for line in preflight.fatal:
            print(f"✗ {line}")
        return 1
    print(f"DRY-RUN: 不会写入任何文件（目标: {target}）")
    plans = plan_groups(target, preflight.install_set)
    for gp in plans:
        marker = "→ 更新" if gp.step.pre_container_hash else "→ 新建"
        print(f"  {gp.step.spec_ids}{marker}")
    for line in preflight.skipped_reports():
        print(f"  跳过 {line}")
    for spec in preflight.external_set:
        print(f"  链接 {spec.id} → post-commit 创建（--codex-root）")
    return 0


def _install_report(target: Path, preflight: PreflightResult,
                    plans: list[GroupPlan], codex_root: Path | None) -> None:
    created = sum(1 for gp in plans if gp.step.pre_container_hash is None)
    updated = len(plans) - created
    adopted = sum(1 for d in preflight.decisions
                  if d.action == "install" and d.status == "adopted_legacy")
    print(f"✓ 已安装/更新 {len(plans)} 个资源组（新建 {created} / 更新 {updated}"
          + (f" / legacy 原地升级 {adopted}" if adopted else "") + "）")
    for line in preflight.skipped_reports():
        print(f"• 跳过 {line}")
    if codex_root is None:
        print("• 未传 --codex-root：Codex workspace 技能链接未创建（doctor 将报 unverifiable）")
