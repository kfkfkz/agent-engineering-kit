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
11. 写 Manifest（原子写；残留记录保留——v9 管理记录不丢失原则）
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

from .manifest import (
    Manifest,
    ManifestCorruptError,
    ManifestEntry,
    build_manifest,
    read_manifest,
    write_manifest_atomic,
)
from .preflight import PreflightResult, run_preflight
from .registry import (
    FRAGMENT_ABSENT,
    HOOK_MATCHER,
    KIT_DIR,
    REGISTRY,
    ResourceSpec,
    CodexLinkSpec,
    _SPEC_ORDER,
    cleanup_legacy_state,
    compute_fragment_hashes,
    current_kit_version,
    determine_status,
    fragment_from_bytes,
    generate_fragment,
    generate_hook,
    resolve_spec,
    secure_mkdir,
    staged_rel,
)
from .transaction import (
    CommitStep,
    TransactionRecord,
    acquire_install_lock,
    backup_container,
    commit_one,
    cleanup_staging,
    recover,
    stage_resource,
)


# ══════════════════════════ §11.5 内容生成（内存） ══════════════════════════

def merge_fragment(container: dict, fragment: Any, locator: str) -> dict:
    """按 locator 逐级写入 JSON 片段（值为 null 的 key 算存在——只覆盖 kit 自己的 key）。"""
    keys = locator.split(".")
    current = container
    for key in keys[:-1]:
        if not isinstance(current.get(key), dict):
            current[key] = {}
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
    for entry in ss:
        for h in entry.get("hooks", []):
            if h.get("command") == spec.expected_hook_command:
                h.clear()
                h.update(hook)
                return container
    for entry in ss:
        if entry.get("matcher") == HOOK_MATCHER:
            entry.setdefault("hooks", []).append(hook)
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
        fd = os.open(dst, os.O_RDONLY | os.O_NOFOLLOW)
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
                        preflight: PreflightResult) -> list[str]:
    """安装方向链接创建不在事务内（§12 无外部步骤；§16 仅卸载方向三段式）：
    validate → 空位创建；已指 target → 无事；他指/被占 → 报告不覆盖。"""
    reports: list[str] = []
    for spec in preflight.external_set:
        link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
        link_spec.validate(codex_root, target)      # ★ 任何派生前先验证（v11）
        link = link_spec.derive_link_path(codex_root)
        expected = link_spec.derive_target_path(target)
        if link.is_symlink():
            if os.readlink(link) == str(expected):
                continue
            reports.append(f"✗ {link} 已指向其他目标，拒绝覆盖")
            continue
        if link.exists():
            reports.append(f"✗ {link} 已存在且不是符号链接，拒绝创建")
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(expected), link)
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
    return reports


# ══════════════════════════ 主流程（§12） ══════════════════════════

def run_install(target: Path, *, repair: bool = False,
                codex_root: Path | None = None,
                dry_run: bool = False) -> int:
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

        # 步骤 6：Staging
        tx.plan = [gp.step for gp in plans]
        tx.write(target)          # ★ 完整 plan 一次性原子写入（不变量 1）
        tx.status = "staging"
        tx.write(target)
        for gp in plans:
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

        # 步骤 11：写 Manifest（原子写；残留记录保留）
        new_manifest = assemble_manifest(target, preflight, plans, manifest,
                                        codex_root)
        write_manifest_atomic(target, new_manifest)
        legacy_reports = cleanup_legacy_state(target)     # v1 清单/marker 退役清理

        # 步骤 12：done
        tx.status = "done"
        tx.write(target)

        # 步骤 13：清理 + 报告
        cleanup_staging(target, tx)
    finally:
        os.close(lock_fd)

    _install_report(target, preflight, plans, codex_root)
    for line in legacy_reports:
        print(line)
    if codex_root is not None:
        for line in ensure_codex_links(target, codex_root, preflight):
            print(line)
    print("完成。下一步：把最近一个 bug 写成第一条 pitfall（复制 _PITFALL_TEMPLATE.md 到"
          "对应模块目录、按日期命名、frontmatter 填 type/module）；记忆索引由 memory-build "
          "本地生成（.repo-memory-kit/memory-index.md），匹配首选 memory-recall 语义检索。")
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
