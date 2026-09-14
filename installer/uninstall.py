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
    """只移除 command 精确匹配的 kit 钩子元素——**容器保真**（P1 回归）：
    - 非对象条目、hooks 非列表的条目原样保留（用户自己的结构）
    - 父 entry 只在"过滤后 hooks 为空 **且** 其余字段只有 kit 创建的 matcher"
      （即 kit 自建 entry 形态）时移除；带 description 等用户元数据的空 entry
      保留不删
    - SessionStart / hooks 键只在结果为空列表时移除"""
    hooks = container.get("hooks")
    if not isinstance(hooks, dict):
        return container
    ss = hooks.get("SessionStart")
    if not isinstance(ss, list):
        return container
    kept = []
    for entry in ss:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept.append(entry)                     # 用户结构，原样保留
            continue
        entry["hooks"] = [
            h for h in entry["hooks"]
            if not (isinstance(h, dict)
                    and h.get("command") == spec.expected_hook_command)]
        if entry["hooks"]:
            kept.append(entry)
            continue
        # 过滤后为空：仅当剩余字段只有 matcher（kit 自建 entry 形态）才整体移除
        if set(entry.keys()) == {"matcher", "hooks"}:
            continue
        kept.append(entry)                          # 带用户元数据，保留空 entry
    if kept:
        hooks["SessionStart"] = kept
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


def safe_unlink_external_link(link: Path, expected_target: str) -> None:
    """原子安全删除外部符号链接（P1 回归第三轮：dir_fd 只固定父目录，
    不固定最终目录项——readlink 与 unlink 之间仍可被替换）。

    正确原语：rename 隔离 → 核验被移对象 → 匹配则删除 / 不匹配则恢复。
    rename 是目录项原子操作，移入隔离名后其他进程无法再通过原名触达。"""
    import stat as stat_module
    import uuid as _uuid
    parent_fd = os.open(link.parent,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    iso_name = f".kit-iso-{_uuid.uuid4()}"
    try:
        # 1. 原子移入隔离名（其他进程此后无法通过原名触达该目录项）
        try:
            os.rename(link.name, iso_name,
                      src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except FileNotFoundError:
            raise OSError(f"链接不存在（已被删除？）: {link}")
        # 2. 核验被移入隔离名的对象
        try:
            st = os.lstat(iso_name, dir_fd=parent_fd)
            if not stat_module.S_ISLNK(st.st_mode):
                raise OSError(f"目标不是符号链接（被用户文件替换）: {link}")
            if os.readlink(iso_name, dir_fd=parent_fd) != expected_target:
                raise OSError(f"链接已改指其他目标: {link}")
            # 3. 匹配 → 删除隔离名
            os.unlink(iso_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            # 4. 不匹配 → 恢复原位（不吞用户对象）
            try:
                os.rename(iso_name, link.name,
                          src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except OSError:
                pass  # 恢复失败时隔离名留存，人工处理
            raise
    finally:
        os.close(parent_fd)


def execute_external_removal(target: Path, step: ExternalStep) -> None:
    """安全删除外部链接（复用 safe_unlink_external_link 的 rename 隔离原语）。"""
    link_spec = CodexLinkSpec(skill_name=step.skill_name)
    link_spec.validate(Path(step.codex_root), target)
    link = link_spec.derive_link_path(Path(step.codex_root))
    expected = str(link_spec.derive_target_path(target))
    safe_unlink_external_link(link, expected)


# ══════════════════════════ 主流程（§13） ══════════════════════════

def _classify_entries(target: Path, manifest, codex_root_cli):
    """步骤 4：条目分类 → removal_specs / external_specs / residual。"""
    residual: list[tuple[Any, str]] = []
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
            residual.append((entry, f"路径/权限校验失败（漂移保护）: {e}"))
            continue
        if spec.resource_type == "seed_file":
            residual.append((entry, "seed_file 永不卸载（用户所有）"))
            continue
        if status is None:
            residual.append((entry, "codex_link 未传 --codex-root，无法核验"))
            continue
        if status in ("conflict", "drifted"):
            residual.append((entry, f"{status}（漂移保护）——保留"))
            continue
        if spec.resource_type == "codex_link":
            external_specs.append(spec)
        else:
            removal_specs.append(spec)
    return removal_specs, external_specs, residual


def _build_removal_plan(target: Path, removal_specs, manifest, residual):
    """步骤 5：内存构建全部 CommitStep（含 Manifest 最后一步）。"""
    plans = []
    for group in group_specs(removal_specs):
        spec0 = group[0]
        dst = target / spec0.destination_path
        pre_bytes, pre_mode = _read_container(dst)
        content = build_removal_content(target, group, pre_bytes)
        kind = "delete" if content is None else "replace"
        step = CommitStep(
            spec_id=spec0.id, spec_ids=tuple(s.id for s in group), kind=kind,
            pre_container_hash=(hashlib.sha256(pre_bytes).hexdigest()
                               if pre_bytes is not None else None),
            post_container_hash=(hashlib.sha256(content).hexdigest()
                                if content is not None else None),
            pre_mode=pre_mode)
        mode = pre_mode if pre_mode is not None else (spec0.expected_mode or 0o644)
        plans.append((step, content, mode, group))

    reduced = reduced_manifest(manifest, [e for e, _ in residual])
    reduced_bytes = manifest_payload_bytes(reduced)
    m_pre, m_mode = _read_container(
        target / STATE_REGISTRY[MANIFEST_SPEC_ID].destination_path)
    manifest_step = CommitStep(
        spec_id=MANIFEST_SPEC_ID, spec_ids=(MANIFEST_SPEC_ID,),
        kind="delete" if not residual else "replace",
        pre_container_hash=(hashlib.sha256(m_pre).hexdigest()
                            if m_pre is not None else None),
        post_container_hash=(None if not residual
                             else hashlib.sha256(reduced_bytes).hexdigest()),
        pre_mode=m_mode)
    manifest_content = None if not residual else reduced_bytes
    return plans, manifest_step, manifest_content


def _stage_and_backup(target: Path, tx, plans, manifest_step, manifest_content):
    """步骤 5 后半：staging 落盘 + 备份（写序不变量 1：完整 plan 先行）。"""
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
    for step, *_ in plans:
        if not backup_container(target, step, tx.tx_id):
            print(f"✗ 卸载中止：备份失败: {step.spec_id}")
            _fail_tx(target, tx)
            return False
    if not backup_container(target, manifest_step, tx.tx_id):
        print("✗ 卸载中止：Manifest 备份失败")
        _fail_tx(target, tx)
        return False
    return True


def _fail_tx(target, tx):
    cleanup_staging(target, tx)
    tx.status = "failed"
    tx.write(target)


def _remove_external_links(target: Path, tx, external_specs, codex_root_cli):
    """步骤 5.5：外部链接移除（意图先行——写序不变量 3）。返回 link_reports 或 None=失败。"""
    link_reports = []
    assert codex_root_cli is not None or not external_specs
    for spec in external_specs:
        step = plan_external_removal(target, codex_root_cli, spec)
        if step.prior_state != "pointing_to_target":
            step.state = "removed"
            tx.external.append(step)
            tx.write(target)
            link_reports.append(f"• {spec.id}: prior_state={step.prior_state}，未触碰")
            continue
        tx.external.append(step)
        tx.write(target)
        try:
            execute_external_removal(target, step)
        except OSError as e:
            failures = restore_external_links(target, tx)
            if failures:
                tx.status = "needs_human"
                tx.detail = f"外部步骤删除失败且补偿失败: {e}; {failures}"
                tx.write(target)
                print(f"✗ {tx.detail}")
                return None
            tx.status = "failed"
            tx.write(target)
            print(f"✗ 外部链接删除失败（已补偿，可安全重试）: {e}")
            return None
        step.state = "removed"
        tx.write(target)
        link_reports.append(f"✓ 已移除 Codex workspace 技能链接 {spec.id}")
    return link_reports


def _commit_removal(target: Path, tx, plans, manifest_step):
    """步骤 6：逐组 CAS commit（state.manifest 最后）。返回已提交数或 None=失败。"""
    tx.status = "committing"
    tx.write(target)
    committed = 0
    for step, *_ in plans:
        result = commit_one(target, step, tx.tx_id)
        if result.status != "committed":
            print(f"✗ 卸载 commit 失败: {step.spec_id}: "
                  f"{result.detail or result.status}——自动恢复现场")
            recover(target)
            return None
        committed += 1
        tx.write(target)
    result = commit_one(target, manifest_step, tx.tx_id)
    if result.status != "committed":
        print(f"✗ Manifest 步骤 commit 失败: {result.detail or result.status}")
        recover(target)
        return None
    return committed


def run_uninstall(target: Path, *, codex_root_cli: Path | None = None) -> int:
    target = target.resolve()
    if not target.is_dir():
        print(f"✗ 目标目录不存在: {target}")
        return 1

    lock_fd = acquire_install_lock(target)
    try:
        # P1 回归：先 recover 再读 Manifest——首次安装在写 Manifest 前崩溃时，
        # recover 会 roll-forward 完成安装并写 Manifest；否则卸载直接因"无清单"
        # 退出，committing 事务永远没有恢复机会
        rr = recover(target)
        if rr.status in ("blocked", "needs_human"):
            print(f"✗ {rr.detail}")
            return 1
        if rr.status != "no_action":
            print(f"• 已恢复遗留事务: {rr.status}")

        manifest = _read_manifest_locked(target)
        if manifest is None:
            return 1
        codex_root_cli = _resolve_codex_root(target, codex_root_cli)

        tx = TransactionRecord.new(target, "uninstall")
        secure_mkdir(target, f".repo-memory-kit/tx/{tx.tx_id}")
        secure_mkdir(target, f".repo-memory-kit/tx/{tx.tx_id}/backups")
        tx.status = "planning"
        tx.write(target)

        removal_specs, external_specs, residual = \
            _classify_entries(target, manifest, codex_root_cli)

        try:
            plans, manifest_step, manifest_content = \
                _build_removal_plan(target, removal_specs, manifest, residual)
        except (OSError, ValueError) as e:
            print(f"✗ 卸载中止：plan 阶段读取容器失败: {e}")
            tx.status = "failed"
            tx.write(target)
            return 1

        if not _stage_and_backup(target, tx, plans, manifest_step, manifest_content):
            return 1

        link_reports = _remove_external_links(target, tx, external_specs, codex_root_cli)
        if link_reports is None:
            return 1

        committed = _commit_removal(target, tx, plans, manifest_step)
        if committed is None:
            return 1

        tx.status = "done"
        tx.write(target)
        cleanup_staging(target, tx)
        _cleanup_empty_dirs(target, [g for _, _, _, g in plans])
        for line in cleanup_legacy_state(target, remove_marker=True):
            print(f"  {line}")
    finally:
        os.close(lock_fd)

    _uninstall_report(committed, link_reports, residual)
    return 0


def _read_manifest_locked(target):
    """锁内读取 Manifest（P2 回归：锁外读取会与并发安装竞态）。"""
    try:
        manifest = read_manifest(target)
    except ManifestCorruptError as e:
        print(f"✗ 安装清单损坏，拒绝卸载: {e}")
        return None
    if manifest is None:
        print("没有安装清单，无法安全卸载。请运行 doctor 查看现状；若确为 kit 旧版本"
              "产物，先运行 install.py --adopt-legacy（§18）重建清单后再卸载。")
        return None
    return manifest


def _resolve_codex_root(target, codex_root_cli):
    if codex_root_cli is None:
        marker_root = read_codex_workspace_marker(target)
        if marker_root is not None:
            codex_root_cli = marker_root
            print(f"提示: 使用已记录的 Codex workspace: {marker_root}"
                  f"（来自 .repo-memory-kit/codex-workspace-root）")
    return codex_root_cli


def _uninstall_report(committed, link_reports, residual):
    print(f"✓ 已移除 {committed} 个资源组")
    for line in link_reports:
        print(f"  {line}")
    if residual:
        print(f"• 残留 {len(residual)} 项（Manifest 已缩减保留其记录）:")
        for entry, reason in residual:
            print(f"  - {entry.spec_id}: {reason}")
    else:
        print("• Manifest 已删除（完整卸载）")
    print("卸载完成。用户数据（记忆条目、容器文件）未触碰。")


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
