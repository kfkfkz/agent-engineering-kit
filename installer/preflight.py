"""preflight.py — 只读前置检查（§12 步骤 4 / §13 步骤 4）：符号链接 / 所有权 / 权限。

对 Registry 每个 spec 做判定并产出安装方向分类（install_set / external /
skip + 原因）。本模块只读——不产生任何文件系统变更。

§12 步骤 4 的"有 issue → 事务 failed 退出"按如下口径执行：符号链接组件与
目标不可写是**仓库级安全事件 → fatal**（沿用旧 install.sh 硬失败口径）；
conflict / drifted / 容器不可读是**资源级问题 → 跳过该资源并报告**（与
"conflict → 报告并跳过该资源（无绕过入口）"一致），其余资源继续。
"""
from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .registry import (
    REGISTRY,
    ContainerUnreadableError,
    ResourceSpec,
    SecurityError,
    determine_status,
)

from .manifest import Manifest


@dataclass
class SpecDecision:
    spec: ResourceSpec
    status: Any            # determine_status 结果（InstallStatus | None）
    action: str             # "install" | "skip" | "external"
    reason: str = ""


@dataclass
class PreflightResult:
    decisions: list[SpecDecision] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)

    @property
    def install_set(self) -> list[ResourceSpec]:
        return [d.spec for d in self.decisions if d.action == "install"]

    @property
    def external_set(self) -> list[ResourceSpec]:
        return [d.spec for d in self.decisions if d.action == "external"]

    def skipped_reports(self) -> list[str]:
        return [f"{d.spec.id} [{d.status}]: {d.reason}" for d in self.decisions
                if d.action == "skip"]

    def status_of(self, spec_id: str) -> Any:
        for d in self.decisions:
            if d.spec.id == spec_id:
                return d.status
        return None


def path_symlink_finding(target: Path, spec: ResourceSpec) -> str | None:
    """检查 destination 路径的已存在组件是否含符号链接（preflight 只读；
    secure_* 写入时会再次拒绝——纵深防御）。codex_link 不适用（target 之外，
    由 CodexLinkSpec.validate 管）。"""
    if spec.resource_type == "codex_link":
        return None
    current = target
    for part in PurePosixPath(spec.destination_path).parts:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            return None            # 组件缺失，其后组件不可能存在
        except OSError:
            return None            # 路径被文件占位等——plan 阶段会以 PlanError 拦下
        if stat_module.S_ISLNK(st.st_mode):
            return f"受管路径包含符号链接，拒绝写入: {current}"
    return None


def run_preflight(target: Path, manifest: Manifest | None, *,
                  repair: bool = False,
                  codex_root_cli: Path | None = None) -> PreflightResult:
    """所有权识别 + 符号链接 / 权限检查（§12 步骤 4；uninstall 侧的分类
    见 uninstall.py——复用 determine_status，判定口径相同）。"""
    result = PreflightResult()
    entries = manifest.entry_map() if manifest else {}

    # 权限检查（全局 fatal）
    if not (os.access(target, os.W_OK) and os.access(target, os.X_OK)):
        result.fatal.append(f"目标目录不可写: {target}")
        return result

    for spec in REGISTRY:
        entry = entries.get(spec.id)
        try:
            status = determine_status(target, spec, entry, codex_root_cli)
        except ContainerUnreadableError as e:
            result.decisions.append(SpecDecision(
                spec, None, "skip", f"容器不可读，阻断该资源: {e}"))
            continue
        except SecurityError as e:
            # codex link 派生校验失败（workspace 结构异常）：跳过该资源并报告，
            # 不让单个资源把整个安装打崩
            result.decisions.append(SpecDecision(
                spec, None, "skip", f"路径/权限校验失败，跳过该资源: {e}"))
            continue

        # 符号链接检查（fatal——校验-写入间越界风险对整个仓库是安全事件，
        # 沿用旧 install.sh 的硬失败口径；§12 步骤 4"有 issue → 事务 failed"）
        link_issue = path_symlink_finding(target, spec)
        if link_issue:
            result.fatal.append(link_issue)
            return result

        # —— codex_link：安装方向为 post-commit 尽力步骤（§16），不进事务 ——
        if spec.resource_type == "codex_link":
            if codex_root_cli is None:
                result.decisions.append(SpecDecision(
                    spec, status, "skip",
                    "codex link 未传 --codex-root，跳过链接创建（提示）"))
            elif status == "conflict":
                result.decisions.append(SpecDecision(
                    spec, status, "skip", "链接被非 kit 内容占用，拒绝覆盖"))
            else:
                result.decisions.append(SpecDecision(spec, status, "external", ""))
            continue

        # —— 容器可选资源：容器不存在时跳过（create_container=False）——
        container_absent = not (target / spec.destination_path).exists()
        if spec.create_container is False and container_absent:
            if entry is None:
                result.decisions.append(SpecDecision(
                    spec, status, "skip", f"容器不存在，跳过 {spec.destination_path}"))
            else:
                # 装过（有记录）但现在连容器都没了 → drifted 报告；容器既然
                # 不存在，--repair 也无从重建（不擅自创建用户容器文件）
                reason = (f"容器缺失（曾装过）：{spec.destination_path}"
                          + ("；--repair 无法重建（容器由用户所有）" if repair else ""))
                result.decisions.append(SpecDecision(spec, "drifted", "skip", reason))
            continue

        # —— 通用判定（§12 步骤 4）——
        if status == "conflict":
            result.decisions.append(SpecDecision(
                spec, status, "skip",
                "用户自有内容或 Manifest 记录与内容血统不符——拒绝覆盖/删除（无绕过入口）"))
        elif status == "drifted":
            if repair:
                result.decisions.append(SpecDecision(
                    spec, status, "install",
                    "drifted -- 显式 --repair 恢复（血统已由 §5 判定）"))
            else:
                result.decisions.append(SpecDecision(
                    spec, status, "skip", "drifted -- 默认跳过并报告（--repair 显式恢复）"))
        else:
            # managed / adopted_legacy → 进入安装（adopted_legacy = 原地升级，§18）
            result.decisions.append(SpecDecision(spec, status, "install", ""))

    return result
