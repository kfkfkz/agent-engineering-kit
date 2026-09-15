"""doctor.py — 只读健康检查（§14）：共享锁 + 6 状态结构化报告。

优先级: INCOMPLETE > CONFLICT > DRIFTED > DEGRADED > OUTDATED
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import Manifest, ManifestCorruptError, read_manifest
from .registry import (
    REGISTRY,
    ContainerUnreadableError,
    SecurityError,
    current_kit_version,
    determine_status,
)
from .transaction import find_unresolved, try_open_install_lock_shared

# 状态优先级（§14.3）
_PRIORITY = ["INCOMPLETE", "CONFLICT", "DRIFTED", "DEGRADED", "OUTDATED"]


@dataclass
class Finding:
    spec_id: str
    path: str
    status: str      # managed / conflict / drifted / missing / degraded / unverifiable / incomplete
    detail: str


@dataclass
class DoctorReport:
    overall: str     # HEALTHY / OUTDATED / DEGRADED / DRIFTED / CONFLICT / INCOMPLETE
    statuses: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def run_doctor(target: Path, *, codex_root_cli: Path | None = None) -> int:
    """CLI 入口：只读检查 + 报告；非 HEALTHY → 退出码 1。"""
    target = target.resolve()
    if not target.is_dir():
        print(f"✗ 目标目录不存在: {target}")
        return 1
    # codex_root 未显式传入时退回已记录的 workspace marker（与卸载同口径，
    # 沿用旧 install.sh 行为）——否则装过链接的项目 doctor 永远 unverifiable
    if codex_root_cli is None:
        from .registry import read_codex_workspace_marker
        marker_root = read_codex_workspace_marker(target)
        if marker_root is not None:
            codex_root_cli = marker_root
            print(f"提示: 使用已记录的 Codex workspace: {marker_root}"
                  f"（来自 .repo-memory-kit/codex-workspace-root）")
    fd = try_open_install_lock_shared(target)      # §14.0（只读，绝不创建）
    try:
        if fd == -1:
            report = DoctorReport(
                overall="INCOMPLETE", statuses=["INCOMPLETE"],
                findings=[Finding("-", "-", "incomplete",
                                  "生命周期进程运行中或锁异常，状态快照不可信")])
        else:
            report = run_doctor_checks(target, codex_root_cli)
    finally:
        if fd is not None and fd >= 0:      # -1 = 锁被排他持有（fd 已在获取时关闭）
            os.close(fd)

    print(f"DOCTOR: {target}")
    print(f"总体状态: {report.overall}")
    for f in report.findings:
        print(f"  [{f.status}] {f.spec_id} ({f.path}): {f.detail}")
    if report.overall == "HEALTHY":
        print("DOCTOR: 健康")
        return 0
    print("DOCTOR: 存在问题；可运行 --repair 修复缺失/漂移的受管产物"
          "（conflict 项需人工处置）")
    return 1


def run_doctor_checks(target: Path,
                      codex_root_cli: Path | None = None) -> DoctorReport:
    statuses: list[str] = []
    findings: list[Finding] = []

    def add(status: str, finding: Finding) -> None:
        if status != "HEALTHY":
            statuses.append(status)
        findings.append(finding)

    # 无 Manifest / 清单损坏 → INCOMPLETE（即使无其他问题）
    manifest: Manifest | None = None
    try:
        manifest = read_manifest(target)
    except ManifestCorruptError as e:
        add("INCOMPLETE", Finding("-", ".repo-memory-kit/manifest.json",
                                  "incomplete", f"清单损坏: {e}"))
    if manifest is None:
        add("INCOMPLETE", Finding("-", ".repo-memory-kit/manifest.json",
                                  "incomplete", "无安装清单（未安装或清单丢失）"))

    # 未完成或 needs_human 事务 → INCOMPLETE
    unresolved = find_unresolved(target)
    for tx in unresolved:
        detail = (f"未完成事务 {tx.tx_id}（status={tx.status}"
                  + ("" if tx.mac_verified else "，HMAC 验证失败")
                  + (f"，{tx.detail}" if tx.detail else "") + "）")
        add("INCOMPLETE", Finding("-", ".repo-memory-kit/tx",
                                  "incomplete", detail))

    entries = manifest.entry_map() if manifest else {}
    for spec in REGISTRY:
        entry = entries.get(spec.id)
        try:
            status = determine_status(target, spec, entry, codex_root_cli)
        except ContainerUnreadableError as e:
            add("DRIFTED", Finding(spec.id, spec.destination_path,
                                   "drifted", f"容器不可读: {e}"))
            continue
        except SecurityError as e:
            # codex link 派生校验失败（workspace 结构异常）：报告为 finding，
            # 不让单个资源把整个 doctor 打崩
            add("CONFLICT", Finding(spec.id, spec.destination_path,
                                    "conflict", f"路径/权限校验失败: {e}"))
            continue

        # codex_link 无法核验 → unverifiable（不猜状态，不计入优先级）
        if status is None:
            findings.append(Finding(
                spec.id, spec.destination_path, "unverifiable",
                "需 --codex-root 才能核验（不猜状态）"))
            continue

        container_absent = not (target / spec.destination_path).exists()
        if spec.resource_type == "seed_file":
            # seed：缺失 → DEGRADED（可 --repair 补种）；drifted 同样映射 DEGRADED
            if status == "drifted":
                add("DEGRADED", Finding(spec.id, spec.destination_path,
                                        "degraded", "seed file 缺失（可 --repair 补种）"))
            continue

        # 容器可选资源：容器不存在时不报状态（沿用 install.sh 行为）
        if spec.create_container is False and container_absent:
            continue

        if entry is not None and status == "drifted":
            add("DRIFTED", Finding(spec.id, spec.destination_path,
                                  "drifted", "必需受管内容被修改或缺失"))
        elif status == "conflict":
            add("CONFLICT", Finding(spec.id, spec.destination_path,
                                    "conflict", "用户自有内容或 Manifest 记录与内容血统不符"))
        elif entry is None and status == "managed":
            # 无记录：未安装 / 可选能力未配置（§14.3"required 资源 entry 缺失且
            # 片段缺失 → 按'未安装'报告"——容器可选资源的容器缺失已在上方跳过）
            if spec.capability is not None:
                add("DEGRADED", Finding(spec.id, spec.destination_path,
                                        "degraded",
                                        f"可选能力未配置（{spec.capability}）"))
            elif spec.required:
                add("DRIFTED", Finding(spec.id, spec.destination_path,
                                      "missing", "受管文件未登记（未安装？）"))
        elif status == "adopted_legacy":
            add("OUTDATED", Finding(spec.id, spec.destination_path,
                                    "managed", "kit 历史版本产物（未登记记录）"))
        elif entry is not None and entry.action == "adopted_legacy":
            add("OUTDATED", Finding(spec.id, spec.destination_path,
                                    "managed", "legacy 采纳记录（重装可归一为当前版本）"))

    # 版本检查
    if manifest is not None and manifest.kit_version != current_kit_version():
        add("OUTDATED", Finding("-", ".repo-memory-kit/manifest.json",
                                "managed",
                                f"kit 版本不一致（已装={manifest.kit_version}，"
                                f"当前={current_kit_version()}）"))

    # 可选能力 archify：文件已装但 Node.js 运行时缺失 → DEGRADED（§14 能力缺失
    # 语义——基础功能不受影响，图表生成降级为 mermaid 回退）
    if any(s.id.startswith("archify-") for s in REGISTRY if s.id in entries):
        node_ok = False
        node_bin = None
        import shutil as _shutil
        import subprocess as _subprocess
        for node_name in ("node", "node.exe"):
            node_bin = _shutil.which(node_name)
            if node_bin:
                try:
                    r = _subprocess.run([node_bin, "--version"],
                                        capture_output=True, text=True, timeout=5)
                    major = (r.stdout or "").strip().lstrip("vV").split(".")[0]
                    node_ok = r.returncode == 0 and major.isdigit() and int(major) >= 18
                except (OSError, ValueError):
                    node_ok = False
                if node_ok:
                    break
        if not node_ok:
            add("DEGRADED", Finding(
                "archify-capability", ".claude/skills/archify/",
                "degraded",
                "archify 图表能力降级：需要 Node.js ≥18（未检测到可用运行时）——"
                "业务流程图回退 mermaid，基础功能不受影响"))

    overall = "HEALTHY"
    for status in _PRIORITY:
        if status in statuses:
            overall = status
            break
    return DoctorReport(overall=overall, statuses=sorted(set(statuses)),
                        findings=findings)
