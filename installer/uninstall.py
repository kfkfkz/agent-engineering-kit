"""uninstall.py — 事务化卸载（§13）：plan → stage → 可补偿外部步骤 → 残留集缩减
Manifest → 复用 transaction 提交。

删除语义（§2 总表）：owned_file → 整文件删除；容器型片段 → 只移除 Kit 片段、
**容器文件永不删除——即使移除后为空**；seed_file 永不卸载；codex_link →
可补偿外部步骤（意图先行三段式，§13 步骤 5.5）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .manifest import (
    MANIFEST_SPEC_ID,
    Manifest,
    ManifestCorruptError,
    manifest_payload_bytes,
    read_manifest,
    reduced_manifest,
)
from .registry import (
    REGISTRY,
    STATE_REGISTRY,
    ContainerUnreadableError,
    CodexLinkSpec,
    ResourceSpec,
    SecurityError,
    _SPEC_ORDER,
    cleanup_legacy_state,
    determine_status,
    fsync_dir,
    read_codex_workspace_marker,
    resolve_spec,
    secure_mkdir,
    secure_rmdir,
)
from .install import _read_container, group_specs
from .transaction import (
    CommitStep,
    ExternalStep,
    TransactionRecord,
    acquire_install_lock,
    backup_container,
    cleanup_staging,
    commit_one,
    recover,
    restore_external_links,
    stage_resource,
)


# ══════════════════════════ 片段移除内容生成（卸载方向） ══════════════════════════

def remove_block(text: str, spec: ResourceSpec) -> str:
    """从容器移除 Kit 区块（start..end；存量未闭合 → start 至 EOF；无标记但
    有旧行组 → 移除旧行组——只有血统判定为 managed/adopted_legacy 的资源
    会走到这里）。一并移除安装时追加的分隔空行（只动 kit 自己加的）。"""
    from .registry import legacy_section_span
    if spec.block_start in text:
        cut_start = text.index(spec.block_start)
        if spec.block_end in text[cut_start:]:
            cut_end = text.index(spec.block_end, cut_start) + len(spec.block_end)
            rest = text[cut_end:]
        else:
            rest = ""                            # 未闭合：start 至 EOF
    else:
        span = legacy_section_span(spec, text)   # 旧行组（无标记形态）
        if span is None:
            return text
        cut_start, cut_end = span
        rest = text[cut_end:]
    if cut_start >= 1 and text[cut_start - 1] == "\n" and (
            cut_start == 1 or text[cut_start - 2] == "\n"):
        cut_start -= 1                           # 我们追加的分隔空行
    result = text[:cut_start] + rest
    # 区块在 EOF 时移除后可能残留单个尾部空行（我们追加时的 "\n"）——收敛为一个
    if result.endswith("\n\n"):
        result = result[:-1]
    return result


def remove_json_fragment(container: dict, locator: str) -> dict:
    """删除 locator 指向的 key；向上清理**被清空的**父 dict（只删 kit 清空的）。"""
    keys = locator.split(".")
    ancestors: list[tuple[dict, str]] = []
    cur: Any = container
    for key in keys[:-1]:
        if not isinstance(cur, dict):
            return container
        ancestors.append((cur, key))
        cur = cur.get(key)
    if isinstance(cur, dict):
        cur.pop(keys[-1], None)
    for parent, key in reversed(ancestors):
        value = parent.get(key)
        if isinstance(value, dict) and not value:
            del parent[key]
        else:
            break
    return container


def remove_hook(container: dict, spec: ResourceSpec) -> dict:
    """移除 command 精确匹配的 SessionStart 钩子；清理空 entry / 空
    SessionStart / 空 hooks（沿用旧 install.sh 的精细清理口径）。"""
    hooks = container.get("hooks")
    if not isinstance(hooks, dict):
        return container
    ss = hooks.get("SessionStart")
    if not isinstance(ss, list):
        return container
    for entry in ss:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
            entry["hooks"] = [
                h for h in entry["hooks"]
                if not (isinstance(h, dict)
                        and h.get("command") == spec.expected_hook_command)]
    ss = [e for e in ss if isinstance(e, dict) and e.get("hooks")]
    if ss:
        hooks["SessionStart"] = ss
    else:
        del hooks["SessionStart"]
        if not hooks:
            del container["hooks"]
    return container


def build_removal_content(target: Path, group: list[ResourceSpec],
                          pre_bytes: bytes | None) -> bytes | None:
    """组内全部 spec 的移除结果。返回 None = kind "delete"（整文件删除）；
    其余 = 移除 Kit 片段后的完整容器（容器永不删除，§2）。"""
    spec0 = group[0]
    if spec0.resource_type == "owned_file":
        return None

    if spec0.resource_type in ("json_fragment", "hook"):
        container = json.loads(pre_bytes.decode("utf-8")) if pre_bytes is not None else {}
        if not isinstance(container, dict):
            raise ValueError(f"容器顶层不是对象: {spec0.destination_path}")
        for spec in group:
            if spec.resource_type == "json_fragment":
                container = remove_json_fragment(container, spec.locator)
            elif spec.resource_type == "hook":
                container = remove_hook(container, spec)
            else:
                raise ValueError(f"混合分组类型不兼容: {spec.id}")
        return json.dumps(container, ensure_ascii=False, indent=2).encode() + b"\n"

    if spec0.resource_type == "managed_block":
        text = pre_bytes.decode("utf-8") if pre_bytes is not None else ""
        for spec in group:
            text = remove_block(text, spec)
        return text.encode()

    raise ValueError(f"不可卸载的资源类型: {spec0.resource_type}")


# ══════════════════════════ 外部链接（§13 步骤 5.5 三段式） ══════════════════════════

def plan_external_removal(target: Path, codex_root_cli: Path,
                           spec: ResourceSpec) -> ExternalStep:
    """只判定（validate + prior_state），不 unlink——intent 由调用方先落盘。"""
    link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
    link_spec.validate(codex_root_cli, target)   # ★ 任何派生前先验证（v11）
    link = link_spec.derive_link_path(codex_root_cli)
    expected = str(link_spec.derive_target_path(target))
    step = ExternalStep(spec_id=spec.id, skill_name=spec.link_skill_name,
                        codex_root=str(codex_root_cli), prior_state="absent")
    if not link.is_symlink():
        return step                                  # absent：无事可做
    if os.readlink(link) != expected:
        step.prior_state = "other"                   # 不属 kit，保留
        return step
    step.prior_state = "pointing_to_target"
    return step


def execute_external_removal(target: Path, step: ExternalStep) -> None:
    """unlink + 经父目录 fsync（调用方已先落盘 intent）。"""
    link_spec = CodexLinkSpec(skill_name=step.skill_name)
    link_spec.validate(Path(step.codex_root), target)
    link = link_spec.derive_link_path(Path(step.codex_root))
    link.unlink()
    fsync_dir(link.parent)


# ══════════════════════════ 主流程（§13） ══════════════════════════

def run_uninstall(target: Path, *, codex_root_cli: Path | None = None) -> int:
    target = target.resolve()
    if not target.is_dir():
        print(f"✗ 目标目录不存在: {target}")
        return 1

    # 步骤 1：读取 Manifest（无 → 拒绝普通卸载）
    try:
        manifest = read_manifest(target)
    except ManifestCorruptError as e:
        print(f"✗ 安装清单损坏，拒绝卸载（人工处置后重试）: {e}")
        return 1
    if manifest is None:
        print("没有安装清单，无法安全卸载。请运行 doctor 查看现状；若确为 kit 旧版本"
              "产物，先运行 install.py --adopt-legacy（§18）重建清单后再卸载。")
        return 1

    # codex_root 解析：CLI 显式参数优先；否则退回已记录的 workspace marker
    # （沿用旧 install.sh 行为——删除授权始终由 readlink == target 保证）
    if codex_root_cli is None:
        marker_root = read_codex_workspace_marker(target)
        if marker_root is not None:
            codex_root_cli = marker_root
            print(f"提示: 使用已记录的 Codex workspace: {marker_root}"
                  f"（来自 .repo-memory-kit/codex-workspace-root）")

    lock_fd = acquire_install_lock(target)           # 步骤 0（全程持有）
    try:
        # 步骤 2：恢复遗留未完成事务
        rr = recover(target)
        if rr.status in ("blocked", "needs_human"):
            print(f"✗ {rr.detail}")
            return 1
        if rr.status != "no_action":
            print(f"• 已恢复遗留事务: {rr.status}")

        # 步骤 3：创建事务
        tx = TransactionRecord.new(target, "uninstall")
        secure_mkdir(target, f".repo-memory-kit/tx/{tx.tx_id}")
        secure_mkdir(target, f".repo-memory-kit/tx/{tx.tx_id}/backups")
        tx.status = "planning"
        tx.write(target)

        # 步骤 4：Plan / Preflight（只读）——条目分类
        residual: list[tuple[Any, str]] = []        # (entry, 原因)
        removal_specs: list[ResourceSpec] = []
        external_specs: list[ResourceSpec] = []
        entries = manifest.entry_map()
        for spec in REGISTRY:
            entry = entries.get(spec.id)
            if entry is None:
                continue
            try:
                status = determine_status(target, spec, entry, codex_root_cli)
            except ContainerUnreadableError as e:
                residual.append((entry, f"容器不可读（漂移保护）: {e}"))
                continue
            except SecurityError as e:
                # codex link 派生校验失败（workspace 结构异常）→ 漂移保护保留，
                # 不让单个资源把整个卸载打崩
                residual.append((entry, f"路径/权限校验失败（漂移保护）: {e}"))
                continue
            if spec.resource_type == "seed_file":
                residual.append((entry, "seed_file 永不卸载（用户所有）"))
                continue
            if status is None:
                residual.append((entry, "codex_link 未传 --codex-root，无法核验"))
                continue
            if status == "conflict":
                residual.append((entry, "conflict（用户自有内容/记录不符）——漂移保护，保留"))
                continue
            if status == "drifted":
                residual.append((entry, "drifted（内容与记录不符）——漂移保护，保留"))
                continue
            # managed / adopted_legacy → 进入移除
            if spec.resource_type == "codex_link":
                external_specs.append(spec)
            else:
                removal_specs.append(spec)

        # 步骤 5：Plan + 内容生成（内存；读容器失败 → 事务 failed，现场干净）
        plans = []
        try:
            for group in group_specs(removal_specs):
                spec0 = group[0]
                dst = target / spec0.destination_path
                pre_bytes, pre_mode = _read_container(dst)
                content = build_removal_content(target, group, pre_bytes)
                kind = "delete" if content is None else "replace"
                step = CommitStep(
                    spec_id=spec0.id,
                    spec_ids=tuple(s.id for s in group),
                    kind=kind,
                    pre_container_hash=(hashlib.sha256(pre_bytes).hexdigest()
                                       if pre_bytes is not None else None),
                    post_container_hash=(hashlib.sha256(content).hexdigest()
                                        if content is not None else None),
                    pre_mode=pre_mode)
                mode = pre_mode if pre_mode is not None else (spec0.expected_mode or 0o644)
                plans.append((step, content, mode, group))
        except (OSError, ValueError) as e:
            print(f"✗ 卸载中止：plan 阶段读取容器失败（目标被外部修改？）: {e}")
            tx.status = "failed"
            tx.write(target)
            return 1

        # Manifest 的最后一步按残留集决定（§13 步骤 5）
        reduced = reduced_manifest(manifest, [e for e, _ in residual])
        reduced_bytes = manifest_payload_bytes(reduced)
        m_pre_bytes, m_pre_mode = _read_container(
            target / STATE_REGISTRY[MANIFEST_SPEC_ID].destination_path)
        manifest_step = CommitStep(
            spec_id=MANIFEST_SPEC_ID,
            spec_ids=(MANIFEST_SPEC_ID,),
            kind="delete" if not residual else "replace",
            pre_container_hash=(hashlib.sha256(m_pre_bytes).hexdigest()
                                if m_pre_bytes is not None else None),
            post_container_hash=(None if not residual
                                 else hashlib.sha256(reduced_bytes).hexdigest()),
            pre_mode=m_pre_mode)
        manifest_content = None if not residual else reduced_bytes

        # ★ 完整 plan 一次性原子写入 → staging → staged 落盘 → validate → backup
        tx.plan = [p[0] for p in plans] + [manifest_step]
        tx.write(target)
        tx.status = "staging"
        tx.write(target)
        for step, content, mode, group in plans:
            if step.kind == "replace":
                stage_resource(target, step.spec_id, tx.tx_id, content, mode=mode)
        if manifest_step.kind == "replace":
            stage_resource(target, MANIFEST_SPEC_ID, tx.tx_id,
                           manifest_content, mode=0o600)
        for step, content, mode, group in plans:
            if not backup_container(target, step, tx.tx_id):
                print(f"✗ 卸载中止：备份失败（目标在 plan 后被修改）: {step.spec_id}")
                cleanup_staging(target, tx)
                tx.status = "failed"
                tx.write(target)
                return 1
        if not backup_container(target, manifest_step, tx.tx_id):
            print("✗ 卸载中止：Manifest 备份失败")
            cleanup_staging(target, tx)
            tx.status = "failed"
            tx.write(target)
            return 1

        # 步骤 5.5：外部链接移除（意图先行——写序不变量 3）
        link_reports: list[str] = []
        assert codex_root_cli is not None or not external_specs, \
            "codex_link 无 --codex-root 时不会有待移除链接（§5 返回 None → 残留集）"
        for spec in external_specs:
            step = plan_external_removal(target, codex_root_cli, spec)
            if step.prior_state != "pointing_to_target":
                # absent / other：只记录，不 unlink
                step.state = "removed"
                tx.external.append(step)
                tx.write(target)
                link_reports.append(
                    f"• {spec.id}: prior_state={step.prior_state}，未触碰")
                continue
            tx.external.append(step)          # a. intent 先落盘 + fsync
            tx.write(target)
            try:
                execute_external_removal(target, step)   # b. unlink + fsync(parent)
            except OSError as e:
                # 补偿本次已删除链接；失败 → needs_human
                failures = restore_external_links(target, tx)
                if failures:
                    tx.status = "needs_human"
                    tx.detail = f"外部步骤删除失败且补偿失败: {e}; {failures}"
                    tx.write(target)
                    print(f"✗ {tx.detail}")
                    return 1
                tx.status = "failed"
                tx.write(target)
                print(f"✗ 外部链接删除失败（已补偿，可安全重试）: {e}")
                return 1
            step.state = "removed"            # c. 记完成
            tx.write(target)
            link_reports.append(f"✓ 已移除 Codex workspace 技能链接 {spec.id}")

        # 步骤 6：Commit（state.manifest 的 delete/replace 是最后一个提交步骤）
        tx.status = "committing"
        tx.write(target)
        committed = 0
        for step, *_ in plans:
            result = commit_one(target, step, tx.tx_id)
            if result.status != "committed":
                print(f"✗ 卸载 commit 失败: {step.spec_id}: "
                      f"{result.detail or result.status}——自动恢复现场")
                rr2 = recover(target)
                print(f"• 恢复结果: {rr2.status} {rr2.detail or ''}".rstrip())
                return 1
            committed += 1
            tx.write(target)
        result = commit_one(target, manifest_step, tx.tx_id)
        if result.status != "committed":
            print(f"✗ Manifest 步骤 commit 失败: {result.detail or result.status}")
            rr2 = recover(target)
            print(f"• 恢复结果: {rr2.status} {rr2.detail or ''}".rstrip())
            return 1

        # 步骤 7：收尾
        tx.status = "done"
        tx.write(target)
        cleanup_staging(target, tx)
        _cleanup_empty_dirs(target, [g for _, _, _, g in plans])
        # marker 的清理：codex_root 可解析（CLI 或 marker 本身）= 链接已处理过
        for line in cleanup_legacy_state(target, remove_marker=True):
            print(f"  {line}")
    finally:
        os.close(lock_fd)

    # 卸载报告
    print(f"✓ 已移除 {committed} 个资源组")
    for line in link_reports:
        print(f"  {line}")
    if residual:
        print(f"• 残留 {len(residual)} 项（Manifest 已缩减保留其记录）:")
        for entry, reason in residual:
            print(f"  - {entry.spec_id}: {reason}")
    else:
        print("• Manifest 已删除（完整卸载）")
    print("卸载完成。用户数据（README.md 索引、记忆条目、.anchors.json、容器文件）未触碰。")
    return 0


def _cleanup_empty_dirs(target: Path, groups: list[list[ResourceSpec]]) -> None:
    """步骤 7：清理空目录（只 rmdir，绝不递归删除非空目录）。
    范围限定 kit 自建目录（.claude/.agents 技能目录与 .repo-memory-kit）——
    docs/memory 是用户记忆区，即使空也不动（沿用旧 install.sh 口径）。"""
    candidates: set[str] = set()
    for group in groups:
        for spec in group:
            if spec.resource_type == "owned_file":
                from pathlib import PurePosixPath
                parent = str(PurePosixPath(spec.destination_path).parent)
                if parent.startswith((".claude/skills/", ".agents/skills/")):
                    candidates.add(parent)
    candidates.update({".repo-memory-kit/bin", ".repo-memory-kit/tx", ".repo-memory-kit"})
    # 深度优先：先删子目录再尝试父目录
    for rel in sorted(candidates, key=lambda r: -r.count("/")):
        try:
            secure_rmdir(target, rel)
            print(f"• 已移除空目录 {rel}")
        except OSError:
            pass
