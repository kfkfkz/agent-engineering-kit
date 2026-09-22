"""registry.py — 权限边界（设计文档 §3/§5/§6/§7/§8/§9/§10/§16 的唯一实施载体）。

ResourceRegistry + StateRegistry 是安装器内置的不可变资源定义：一切删除/覆盖
路径从 Registry 派生，绝不从 Manifest / 事务记录读取（§3 关键规则）。
§6 secure_* 家族（dir_fd 逐级 O_NOFOLLOW）是全部文件写入的唯一入口；
片段读取 / hash 计算 / 状态判定 / CodexLinkSpec 同居本模块——它们全部是
"从 ResourceSpec 派生" 的可信安装器侧逻辑（§20 未给这些助手单独开模块，
归入唯一权限源所在文件，保持依赖方向单向：其余模块只 import 本模块）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Literal, TypeVar

from aek.core.context.reference_catalog import REFERENCE_CATALOG

# ── kit 仓库根（installer/ 的父目录；安装器代码的一部分，目标仓库不可篡改）──
KIT_DIR = Path(__file__).resolve().parent.parent

# 平台后端（延迟导入——registry 是底层模块，避免循环依赖）
def _get_platform_fs():
    from installer import platform as _p
    return _p

_platform_fs = None  # 延迟初始化（首次调用 secure_* 时加载）


def _fs():
    global _platform_fs
    if _platform_fs is None:
        _platform_fs = _get_platform_fs()
    return _platform_fs


class SecurityError(Exception):
    """路径校验 / 权限边界违规。"""


class ContainerUnreadableError(Exception):
    """容器文件存在但无法解析（§8）。

    调用方必须处理：preflight → 阻断该资源；uninstall → 漂移保护跳过并报告；
    doctor → finding；recover → needs_human。严禁静默当作 FRAGMENT_ABSENT
    （会把用户损坏文件覆盖掉）。
    """

    def __init__(self, path: Path, cause: Exception):
        self.path = path
        self.cause = cause
        super().__init__(f"容器文件无法解析: {path}: {cause}")


FRAGMENT_ABSENT = object()  # sentinel，不是 None（§8）

ResourceType = Literal[
    "owned_file",     # Kit 独占的完整文件——唯一可整文件删除的类型
    "seed_file",      # 仅首次创建，此后归用户所有——永不卸载、永不判 conflict
    "json_fragment",  # JSON 路径片段——只移除片段
    "managed_block",  # 带起止标记的文本区块——只移除片段
    "hook",           # settings.json SessionStart 数组中的特定 hook 元素
    "codex_link",     # workspace 符号链接（target 之外；可补偿外部步骤）
]


@dataclass(frozen=True)
class ResourceSpec:
    id: str                     # 唯一标识（如 "skill-claude-tdd"）
    source_path: str | None    # kit 仓库内的源文件路径（None = 生成型）
    destination_path: str      # 目标仓库内的完整路径（codex_link 例外，见下）
    resource_type: ResourceType
    locator: str | None        # 片段定位（JSON path / block markers）
    merge_policy: str          # replace | merge_if_absent | seed_if_absent
    expected_mode: int | None  # 文件权限
    required: bool = True      # True=缺失时 doctor 报 DRIFTED（seed_file 见 §2 特例）
    capability: str | None = None      # 可选能力标识（缺失时报 DEGRADED）
    block_start: str | None = None     # managed_block 的起始标记
    block_end: str | None = None       # managed_block 的结束标记
    expected_hook_command: str | None = None  # hook 资源的精确 command 匹配键（§8）
    link_skill_name: str | None = None # codex_link 资源对应的 skill 名（§16）
    # 实现保真补充（设计未定义此旗标）：False = 容器文件不存在时跳过安装、
    # doctor 不报状态——沿用旧 install.sh 对 CLAUDE.md/AGENTS.md/.gitignore 的
    # "只写入已存在的容器" 行为；True = 容器缺失时创建（.cbmignore/.codex 等）。
    create_container: bool = True


# ── 托管区块标记（沿用旧 install.sh 的既有标记，保证存量可识别）──
SEC_START = "<!-- repo-memory-kit:start -->"
SEC_END = "<!-- repo-memory-kit:end -->"
HASH_START = "# repo-memory-kit:start"
HASH_END = "# repo-memory-kit:end"
CODEX_START = "# kit:agent-engineering:start"
CODEX_END = "# kit:agent-engineering:end"
GITIGNORE_START = "# agent-engineering-kit: 语义索引派生数据(可随时重建)"
GITIGNORE_END = "# agent-engineering-kit:end"

# 会话提醒钩子（CLAUDE_PROJECT_DIR 由 Claude Code 运行时展开，此处保留字面量）
HOOK_COMMAND = '"$CLAUDE_PROJECT_DIR/.repo-memory-kit/bin/session-reminder"'
HOOK_MATCHER = "startup|resume"
HOOK_TIMEOUT = 5

# ── 存量旧行组（无标记形态）识别——仅限有历史包袱的三个容器资源 ──
# 旧 install.sh 曾以"裸段落"形式写入：CLAUDE/AGENTS 的「项目记忆」二级标题段
# （v1 前）与 .codex/config.toml 的无标记 TOML 节。不带标记无法用 block 标记
# 提取，改用结构化边界（标题行→下一个二级标题；节头→下一个 [ 节）切出片段，
# 血统仍由 canonical hash 精确判定——不匹配（用户定制）即 conflict，不吞行。
LEGACY_MEMORY_HEADING = "## 项目记忆（坑与流程）"
LEGACY_CODEX_SECTION = "[mcp_servers.agent-engineering]"

KIT_SKILLS = [
    "memory-check", "memory-capture", "repo-delivery", "codebase-memory",
    "systematic-debugging", "tdd", "reuse-research", "security-review",
    "delivery-gate", "spec-migrate", "task-handoff", "design-pipeline",
]

KIT_TOOLS = [
    "validate-memory.sh", "memory-build", "spec-migrate", "memory-recall",
    "domain-check", "session-reminder", "agent-engineering-mcp",
    "doc-gate", "governance-eval", "route-eval",
]


def _root_specs() -> list[ResourceSpec]:
    return [
        ResourceSpec(
            id="claude-md-block", source_path="templates/claude-md-section.md",
            destination_path="CLAUDE.md", resource_type="managed_block",
            locator=None, merge_policy="replace", expected_mode=None,
            block_start=SEC_START, block_end=SEC_END, create_container=False),
        ResourceSpec(
            id="agents-md-block", source_path="templates/agents-md-section.md",
            destination_path="AGENTS.md", resource_type="managed_block",
            locator=None, merge_policy="replace", expected_mode=None,
            block_start=SEC_START, block_end=SEC_END, create_container=False),
        ResourceSpec(
            id="mcp-claude", source_path=None,
            destination_path=".mcp.json", resource_type="json_fragment",
            locator="mcpServers.agent-engineering", merge_policy="merge_if_absent",
            expected_mode=None, capability="mcp"),
        ResourceSpec(
            id="cbmignore-block", source_path=None,
            destination_path=".cbmignore", resource_type="managed_block",
            locator=None, merge_policy="replace", expected_mode=None,
            block_start=HASH_START, block_end=HASH_END),
        ResourceSpec(
            id="gitignore-block", source_path=None,
            destination_path=".gitignore", resource_type="managed_block",
            locator=None, merge_policy="replace", expected_mode=None,
            block_start=GITIGNORE_START, block_end=GITIGNORE_END,
            create_container=False),
        ResourceSpec(
            id="mcp-codex", source_path=None,
            destination_path=".codex/config.toml", resource_type="managed_block",
            locator=None, merge_policy="replace", expected_mode=None,
            capability="mcp-codex",
            block_start=CODEX_START, block_end=CODEX_END),
        ResourceSpec(
            id="session-hook", source_path=None,
            destination_path=".claude/settings.json", resource_type="hook",
            locator="hooks.SessionStart", merge_policy="merge_if_absent",
            expected_mode=None, capability="session-hook",
            expected_hook_command=HOOK_COMMAND),
    ]


def _memory_specs() -> list[ResourceSpec]:
    return [
        ResourceSpec(
            id="memory-rules", source_path="templates/memory-RULES.md",
            destination_path="docs/memory/RULES.md", resource_type="owned_file",
            locator=None, merge_policy="replace", expected_mode=0o644),
        ResourceSpec(
            id="memory-readme-seed", source_path="templates/memory-README.md",
            destination_path="docs/memory/README.md", resource_type="seed_file",
            locator=None, merge_policy="seed_if_absent", expected_mode=0o644),
        # v4：条目按模块分目录（docs/memory/<模块>/），类型在 frontmatter——
        # 模板随之移到 memory 根（旧 <类型>/_TEMPLATE.md 是 v3 布局，存量文件不删不动）
        ResourceSpec(
            id="pitfall-template", source_path="templates/pitfall-entry.md",
            destination_path="docs/memory/_PITFALL_TEMPLATE.md",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o644),
        ResourceSpec(
            id="decision-template", source_path="templates/decision-entry.md",
            destination_path="docs/memory/_DECISION_TEMPLATE.md",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o644),
        ResourceSpec(
            id="playbook-template", source_path="templates/playbook-entry.md",
            destination_path="docs/memory/_PLAYBOOK_TEMPLATE.md",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o644),
        ResourceSpec(
            id="profile-template", source_path="templates/profile.md",
            destination_path="docs/memory/_PROFILE_TEMPLATE.md",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o644),
    ]


def _skill_specs() -> list[ResourceSpec]:
    out: list[ResourceSpec] = []
    for name in KIT_SKILLS:
        out.append(ResourceSpec(
            id=f"skill-claude-{name}", source_path=f"skills/{name}/SKILL.md",
            destination_path=f".claude/skills/{name}/SKILL.md",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o644))
    for name in KIT_SKILLS:
        out.append(ResourceSpec(
            id=f"skill-agents-{name}", source_path=f"skills/{name}/SKILL.md",
            destination_path=f".agents/skills/{name}/SKILL.md",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o644))
    return out


def _skill_reference_specs() -> list[ResourceSpec]:
    """Derive managed files from the fixed Catalog, never scan a directory."""
    out: list[ResourceSpec] = []
    for reference in REFERENCE_CATALOG.references.values():
        name = reference.id
        for tree in ("claude", "agents"):
            out.append(ResourceSpec(
                id=f"skill-{tree}-repo-delivery-reference-{name}",
                source_path=reference.source_path,
                destination_path=(
                    f".{tree}/skills/repo-delivery/references/{name}.md"),
                resource_type="owned_file", locator=None,
                merge_policy="replace", expected_mode=0o644))
    return out


AEK_PACKAGE_FILES = (
    "__init__.py",
    "core/__init__.py",
    "core/context/__init__.py",
    "core/context/reference_catalog.py",
    "core/context/budget.py",
    "core/context/telemetry.py",
    "core/context/memory.py",
    "core/context/capsule.py",
    "core/artifact/__init__.py",
    "core/artifact/registry.py",
    "core/artifact/plan.py",
    "core/review/__init__.py",
    "core/review/judge.py",
    "core/review/result.py",
    "core/review/incremental.py",
    "core/policy/__init__.py",
    "core/policy/evaluator.py",
    "core/dispatch.py",
    "core/planning/__init__.py",
    "core/planning/facts.py",
    "core/planning/policy.py",
    "core/planning/sql_screen.py",
    "application/__init__.py",
    "application/planning.py",
    "application/document_gate.py",
    "application/review.py",
    "application/context_telemetry.py",
    "application/context_capsule.py",
    "application/memory_recall.py",
    "application/dispatch.py",
    "application/routing.py",
    "application/governance.py",
    "adapters/__init__.py",
    "adapters/capsule.py",
    "adapters/telemetry.py",
    "adapters/memory.py",
    "adapters/plan_transaction.py",
    "adapters/evidence.py",
    "adapters/change_scope.py",
    "adapters/review_markdown.py",
)


def _package_specs() -> list[ResourceSpec]:
    """Explicit managed package files for source/install dual layout."""
    return [
        ResourceSpec(
            id=f"lib-aek-{relative.replace('/', '-').replace('.', '-')}",
            source_path=f"aek/{relative}",
            destination_path=f".repo-memory-kit/lib/aek/{relative}",
            resource_type="owned_file", locator=None,
            merge_policy="replace", expected_mode=0o644)
        for relative in AEK_PACKAGE_FILES
    ]


def _bin_specs() -> list[ResourceSpec]:
    return [
        ResourceSpec(
            id=f"bin-{name}", source_path=name,
            destination_path=f".repo-memory-kit/bin/{name}",
            resource_type="owned_file", locator=None, merge_policy="replace",
            expected_mode=0o755)
        for name in KIT_TOOLS
    ]


def _codex_link_specs() -> list[ResourceSpec]:
    return [
        ResourceSpec(
            id=f"codex-link-{name}", source_path=None,
            # codex_link 的 destination 在 target 之外（codex_root 下），
            # 此处仅作展示性登记；真实路径一律经 CodexLinkSpec 派生（§16），
            # 绝不与 target 拼接。
            destination_path=f"<codex-root>/.agents/skills/{name}",
            resource_type="codex_link", locator=None, merge_policy="replace",
            expected_mode=None, link_skill_name=name)
        for name in KIT_SKILLS
    ]


# 面向用户仓库的全部资源（有序——组锚/顺序校验的依据）
def _requirement_specs() -> list[ResourceSpec]:
    """需求工程资源（2026-09-15 用户裁决：索引与模板随 kit 常备，不再只有 --svn 才有）：
    - docs/01-需求/README.md——指派索引（**seed**：首次创建后归用户，永不覆盖）
    - docs/01-需求/_模板/——12 件文档模板（**owned**：doc-gate init 的复制源，
      随 kit 升级更新——定制模板会破坏 doc-gate 结构校验，不开放用户改）"""
    tpl_map = [
        ("req-tpl-raw", "原始需求.md"),
        ("req-tpl-analysis", "需求分析.md"),
        ("req-tpl-outline", "概要设计.md"),
        ("req-tpl-ui", "UI设计.md"),
        ("req-tpl-flow", "业务流程设计.md"),
        ("req-tpl-detail", "详细设计.md"),
        ("req-tpl-api", "API设计.md"),
        ("req-tpl-db", "数据库设计.md"),
        ("req-tpl-tasks", "任务清单.md"),
        ("req-tpl-test", "测试方案.md"),
        ("req-tpl-verify", "线上联调功能验证清单.md"),
        ("req-tpl-final", "终稿.md"),
    ]
    specs = [ResourceSpec(
        id="requirements-index", source_path="templates/requirements-index.md",
        destination_path="docs/01-需求/README.md", resource_type="seed_file",
        locator=None, merge_policy="seed_if_absent", expected_mode=0o644)]
    for tid, fname in tpl_map:
        specs.append(ResourceSpec(
            id=tid, source_path=f"templates/requirements/{fname}",
            destination_path=f"docs/01-需求/_模板/{fname}",
            resource_type="owned_file", locator=None,
            merge_policy="replace", expected_mode=0o644))
    return specs


_PathT = TypeVar("_PathT", bound=PurePath)


def _snapshot_rel_bytes(relative_path: PurePath) -> bytes:
    """供应链快照路径的跨平台规范字节；不得把宿主分隔符写入摘要。"""
    return relative_path.as_posix().encode("utf-8")


def _sort_snapshot_files(vendor: _PathT, files: Iterable[_PathT]) -> list[_PathT]:
    """以规范路径字节排序，避免 Windows 大小写折叠改变聚合顺序。"""
    return sorted(files, key=lambda path: _snapshot_rel_bytes(path.relative_to(vendor)))


def _archify_specs() -> list[ResourceSpec]:
    """vendor/archify/ 快照逐文件展开为 owned_file spec（两棵技能树）。
    - 供应链锁定：快照聚合哈希与 vendor/archify.manifest.json 不符 → SecurityError
      （拒绝生成 specs——安装/卸载/doctor 全部走 REGISTRY，一处锁定全局生效）
    - Node.js ≥18 是**运行时**可选能力：文件照常安装，运行时缺失由 doctor 报
      DEGRADED（§14 能力缺失语义），不阻断基础安装
    - vendor 缺失（异常的仓库状态）→ 空列表（kit 自身问题，不静默炸安装）"""
    import hashlib as _hashlib
    vendor = KIT_DIR / "vendor" / "archify"
    manifest_path = KIT_DIR / "vendor" / "archify.manifest.json"
    if not vendor.is_dir() or not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise SecurityError("vendor/archify.manifest.json 不可解析")
    files = _sort_snapshot_files(vendor, (p for p in vendor.rglob("*") if p.is_file()))
    digest = _hashlib.sha256()
    for p in files:
        digest.update(_snapshot_rel_bytes(p.relative_to(vendor)))
        digest.update(b"\0")
        digest.update(_hashlib.sha256(p.read_bytes()).digest())
    if digest.hexdigest() != manifest.get("snapshot_sha256"):
        raise SecurityError("vendor/archify 快照哈希与 manifest 不符——供应链锁定，拒绝安装")
    specs: list[ResourceSpec] = []
    for p in files:
        rel = p.relative_to(vendor).as_posix()
        for tree in (".claude", ".agents"):
            specs.append(ResourceSpec(
                id=f"archify-{tree}-{rel.replace('/', '-')}",
                source_path=f"vendor/archify/{rel}",
                destination_path=f"{tree}/skills/archify/{rel}",
                resource_type="owned_file",
                locator=None, merge_policy="replace",
                expected_mode=None, capability="archify"))
    return specs


REGISTRY: list[ResourceSpec] = (
    _root_specs() + _memory_specs() + _requirement_specs() + _skill_specs()
    + _skill_reference_specs() + _package_specs() + _bin_specs()
    + _codex_link_specs() + _archify_specs()
)

# kit 内部状态文件（同级信任、同约束）
STATE_REGISTRY: dict[str, ResourceSpec] = {
    "state.manifest": ResourceSpec(
        id="state.manifest", source_path=None,
        destination_path=".repo-memory-kit/manifest.json",
        resource_type="owned_file", locator=None,
        merge_policy="replace", expected_mode=0o600,
    ),
    # 旧 install.sh 的退役状态文件（只登记路径使删除合规，§3 关键规则；
    # 安装方向永不写入，移除必须过 _legacy_state_owned 严格格式校验）
    "state.legacy-manifest-v1": ResourceSpec(
        id="state.legacy-manifest-v1", source_path=None,
        destination_path=".repo-memory-kit/manifest",
        resource_type="owned_file", locator=None,
        merge_policy="replace", expected_mode=0o600,
    ),
    "state.governance-profile": ResourceSpec(
        id="state.governance-profile", source_path=None,
        destination_path=".repo-memory-kit/governance",
        resource_type="owned_file", locator=None,
        merge_policy="replace", expected_mode=0o644),
    # governance.json 是团队自持文件：仅在缺失时由安装事务**创建**（首次），
    # 创建本身是事务步骤（回滚即删）；既有文件永不覆盖、不入 plan
    "state.governance-rules": ResourceSpec(
        id="state.governance-rules", source_path=None,
        destination_path=".repo-memory-kit/governance.json",
        resource_type="owned_file", locator=None,
        merge_policy="replace", expected_mode=0o644),
    "state.codex-workspace-marker": ResourceSpec(
        id="state.codex-workspace-marker", source_path=None,
        destination_path=".repo-memory-kit/codex-workspace-root",
        resource_type="owned_file", locator=None,
        merge_policy="replace", expected_mode=0o644,
    ),
}


def legacy_state_owned(target: Path, rel: str) -> bool:
    """退役状态文件的严格归属校验（删除前的唯一授权依据）：
    - `.repo-memory-kit/manifest`（v1 文本清单）：非空行必须逐行匹配
      kit_version= / anchors_schema= 头或 `<64hex>  <受管路径>`，且路径必须在
      v1 时代的受管集合内（Registry owned_file 目标 ∪ codex marker）——
      任何杂行 → 不是 kit 产物，不动
    - `.repo-memory-kit/codex-workspace-root`：恰一行绝对路径"""
    path = target / rel
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if rel == STATE_REGISTRY["state.legacy-manifest-v1"].destination_path:
        v1_paths = ({s.destination_path for s in REGISTRY
                    if s.resource_type == "owned_file"}
                    | {STATE_REGISTRY["state.codex-workspace-marker"].destination_path}
                    # v3 时代的模板路径（按类型目录布局）——存量 v1 清单里有这些行
                    | {"docs/memory/pitfalls/_TEMPLATE.md",
                       "docs/memory/decisions/_TEMPLATE.md",
                       "docs/memory/playbooks/_TEMPLATE.md"})
        for line in text.splitlines():
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith(("kit_version=", "anchors_schema=")):
                continue
            m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
            if not m or m.group(2) not in v1_paths:
                return False
        return bool(text.strip())
    if rel == STATE_REGISTRY["state.codex-workspace-marker"].destination_path:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return len(lines) == 1 and lines[0].startswith("/")
    return False


def cleanup_legacy_state(target: Path, *, remove_marker: bool = False) -> list[str]:
    """移除退役的 v1 状态文件（严格格式校验通过才删；返回报告行）。
    - v1 文本清单：成功安装（v2 manifest 已落盘）与卸载提交后均可清理
    - codex-workspace-root marker：仅卸载方向按需清理（remove_marker=True）——
      它是卸载时链接清理的线索，安装方向保持存活"""
    reports: list[str] = []
    spec_ids = ["state.legacy-manifest-v1"]
    if remove_marker:
        spec_ids.append("state.codex-workspace-marker")
    for spec_id in spec_ids:
        spec = STATE_REGISTRY[spec_id]
        if not (target / spec.destination_path).is_file():
            continue
        if not legacy_state_owned(target, spec.destination_path):
            reports.append(f"• 退役状态文件 {spec.destination_path} 内容异常，"
                           f"未自动清理（请人工确认）")
            continue
        secure_unlink(target, spec.destination_path)
        reports.append(f"• 已清理退役状态文件 {spec.destination_path}"
                       f"（旧 install.sh 产物，v2 清单在 manifest.json）")
    return reports


def read_codex_workspace_marker(target: Path) -> Path | None:
    """读取 codex workspace marker（卸载方向的链接清理线索，沿用旧 install.sh
    行为）。授权不由 marker 决定——链接删除仍要求 readlink == 本 target。
    marker 缺失/格式异常 → None（调用方按无线索处理）。"""
    marker_rel = STATE_REGISTRY["state.codex-workspace-marker"].destination_path
    if not (target / marker_rel).is_file():
        return None
    if not legacy_state_owned(target, marker_rel):
        return None
    return Path((target / marker_rel).read_text(encoding="utf-8").strip())

REGISTRY_BY_ID: dict[str, ResourceSpec] = {s.id: s for s in REGISTRY}

# Registry 全序索引（REGISTRY 列表顺序 + STATE_REGISTRY 在后）——组锚与顺序校验的权威
_SPEC_ORDER: dict[str, int] = {
    spec.id: i for i, spec in enumerate(REGISTRY)
} | {spec.id: len(REGISTRY) + i for i, spec in enumerate(STATE_REGISTRY.values())}


def resolve_spec(spec_id: str) -> ResourceSpec:
    """REGISTRY 与 STATE_REGISTRY 的统一查找——路径派生/事务校验的唯一入口。"""
    spec = REGISTRY_BY_ID.get(spec_id) or STATE_REGISTRY.get(spec_id)
    if spec is None:
        raise SecurityError(f"spec_id {spec_id!r} not in registry")
    return spec


_KIT_VERSION: str | None = None


def current_kit_version() -> str:
    """kit 版本（git describe --tags --always；无 git 时 unknown）。"""
    global _KIT_VERSION
    if _KIT_VERSION is None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(KIT_DIR), "describe", "--tags", "--always"],
                capture_output=True, text=True, timeout=10)
            _KIT_VERSION = proc.stdout.strip() if proc.returncode == 0 else "unknown"
        except Exception:
            _KIT_VERSION = "unknown"
        if not _KIT_VERSION:
            _KIT_VERSION = "unknown"
    return _KIT_VERSION


# ══════════════════════════ §6 路径安全 ══════════════════════════

def validate_safe_path(path: Path, target: Path) -> None:
    """preflight 只读检查：逐级 lstat 无符号链接，最终路径在 target 内。
    Windows：只检查 target 内的组件（islink+junction 预检），target 外的
    路径组件不在管辖范围（drive/UNC 路径的 lstat 语义不适用）。"""
    import sys as _sys
    if _sys.platform == "win32":
        # Windows：检查 target 内的路径组件无 reparse point
        try:
            rel = path.resolve().relative_to(target.resolve())
        except ValueError:
            raise SecurityError(f"path escapes target: {path}")
        _fs().check_path_safe(target, str(rel).replace(os.sep, "/"))
    else:
        current = Path("/")
        for part in path.parts[1:]:
            current = current / part
            try:
                st = current.lstat()
                if stat_module.S_ISLNK(st.st_mode):
                    raise SecurityError(f"symlink in path: {current}")
            except FileNotFoundError:
                pass

    try:
        common = os.path.commonpath([str(path.resolve()), str(target.resolve())])
        if common != str(target.resolve()):
            raise SecurityError(f"path escapes target: {path}")
    except ValueError:
        raise SecurityError(f"path on different filesystem: {path}")


def validate_tx_id(tx_id: str) -> uuid.UUID:
    """严格 UUID v4 校验（canonical 小写）。zvec generation 名复用本校验（§15.2）。"""
    try:
        parsed = uuid.UUID(tx_id)
    except (ValueError, AttributeError, TypeError):
        raise SecurityError(f"tx_id {tx_id!r} is not a valid UUID")
    if str(parsed) != tx_id:
        raise SecurityError(f"tx_id {tx_id!r} is not in canonical lowercase form")
    if parsed.version != 4:
        raise SecurityError(f"tx_id {tx_id!r} is not UUID v4")
    return parsed


def validate_relative_path(rel: str) -> Path:
    """secure_* 文件入口的第一道校验（校验完整文件路径）。
    **唯一校验器在 platform/pathcheck**（第七轮审计 P1：此前固定 PurePosixPath，
    Windows 的 C:\\x / UNC / ..\\outside 全部逃逸）——本函数只是把 ValueError
    归一为 SecurityError 的适配层，POSIX/Windows 后端的 _validate_rel 同源委托。
    - 必须是非空**相对**路径——os.open(绝对路径, dir_fd=...) 会忽略 dir_fd
    - 拒绝盘符/根/UNC/.. 组件/NUL（按目标平台语义）
    （根目录文件如 "AGENTS.md" 合法——其父目录串 "." 由 secure_walk_dir_fd 特判。）"""
    from .platform.pathcheck import validate_relative_rel
    try:
        return validate_relative_rel(rel)
    except ValueError as e:
        raise SecurityError(str(e))


def _wrap_fs_error(func, *args, **kwargs):
    """包装平台后端的 PermissionError → SecurityError（保持 registry API 兼容）。"""
    try:
        return func(*args, **kwargs)
    except PermissionError as e:
        raise SecurityError(str(e)) from e


def secure_walk_dir_fd(target: Path, dir_rel: str) -> int:
    """打开到指定目录，返回 fd。POSIX: O_NOFOLLOW|O_DIRECTORY 逐级；Windows: islink 预检。"""
    return _wrap_fs_error(_fs().secure_walk_dir_fd, target, dir_rel)


def secure_open(target: Path, rel: str, flags: int, mode: int = 0o600) -> int:
    """kit 一切文件打开/写入的入口。路径校验后委托到平台后端。"""
    p = validate_relative_path(rel)
    return _wrap_fs_error(_fs().secure_open, target, str(p), flags, mode)


def secure_replace(target: Path, src_rel: str, dst_rel: str) -> None:
    """原子替换。路径校验后委托到平台后端。"""
    src = validate_relative_path(src_rel)
    dst = validate_relative_path(dst_rel)
    _wrap_fs_error(_fs().secure_replace, target, str(src), str(dst))


def secure_unlink(target: Path, rel: str) -> None:
    """删除文件。路径校验后委托到平台后端。"""
    p = validate_relative_path(rel)
    _wrap_fs_error(_fs().secure_unlink, target, str(p))


def secure_rmdir(target: Path, rel: str) -> None:
    """删除空目录。路径校验后委托到平台后端。"""
    p = validate_relative_path(rel)
    _wrap_fs_error(_fs().secure_rmdir, target, str(p))


def secure_mkdir(target: Path, rel: str) -> None:
    """逐级创建目录（幂等）。路径校验后委托到平台后端。"""
    if rel in ("", "."):
        return
    validate_relative_path(rel)
    _wrap_fs_error(_fs().secure_mkdir, target, rel)


# ★ 规则：所有文件系统变更（打开写入、replace/rename、unlink、rmdir）一律经
# 上述 dir_fd 变体（相对 target 的路径）；正文各流程中不再出现路径型写操作。
# 只读操作（file_sha256 校验、backup 的 O_NOFOLLOW 读取）允许路径形式——
# 被换内容只会导致 hash 校验失败（fail-safe），不产生写入。
# 有界例外：zvec generation 回收的 shutil.rmtree（§15.4）——受 rebuild.lock、
# UUID 目录名校验与 reader 排他锁三重约束，且目标为可再生派生数据。


# ══════════════════════════ §11.11 工具函数 ══════════════════════════

def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """循环写，防短写和死循环。"""
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("os.write returned 0")
        remaining = remaining[written:]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ══════════════════════════ §7 路径派生 ══════════════════════════

@dataclass(frozen=True)
class TxPaths:
    dst: Path
    staged: Path
    backup: Path
    spec: ResourceSpec


def derive_destination(target: Path, spec_id: str) -> Path:
    """doctor / uninstall / 恢复分类用：只需目标路径。"""
    spec = resolve_spec(spec_id)   # REGISTRY ∪ STATE_REGISTRY
    dst = target / spec.destination_path
    validate_safe_path(dst, target)
    return dst


def staged_rel(spec: ResourceSpec, tx_id: str) -> str:
    """staged 文件相对 target 的路径（secure_* 系列 consumes 相对路径，单一来源）。"""
    dst_rel = PurePosixPath(spec.destination_path)
    return str(dst_rel.parent / f"{dst_rel.name}.kit-stage-{tx_id}-{spec.id}")


def derive_transaction_paths(target: Path, spec_id: str, tx_id: str) -> TxPaths:
    """install / uninstall / rollback 用：需要 staged 和 backup 路径。"""
    spec = resolve_spec(spec_id)
    validate_tx_id(tx_id)

    dst = target / spec.destination_path
    staged = dst.parent / f"{dst.name}.kit-stage-{tx_id}-{spec.id}"
    backup = target / ".repo-memory-kit" / "tx" / tx_id / "backups" / spec.id

    validate_safe_path(dst, target)
    validate_safe_path(staged, target)
    validate_safe_path(backup, target)

    return TxPaths(dst=dst, staged=staged, backup=backup, spec=spec)


# ══════════════════════════ §9 Hash 计算 ══════════════════════════

def stable_serialize(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def canonicalize_paths(obj, target: Path):
    """递归替换片段中的目标绝对路径为 ${TARGET}（只对片段，不碰用户其他内容）。"""
    target_str = str(target)
    if isinstance(obj, str):
        return obj.replace(target_str, "${TARGET}")
    if isinstance(obj, dict):
        return {k: canonicalize_paths(v, target) for k, v in obj.items()}
    if isinstance(obj, list):
        return [canonicalize_paths(v, target) for v in obj]
    return obj


def compute_fragment_hashes(fragment, target: Path) -> tuple[str, str]:
    """返回 (installed_hash, canonical_hash)。形态必须与比对对象一致：
    - bytes（owned_file / seed_file）：直接 hash，canonical 做字节级路径替换
    - str（managed_block）：encode 后 hash，canonical 做文本替换
    - dict/list（json_fragment / hook）：稳定序列化后 hash
    """
    if isinstance(fragment, bytes):
        installed = hashlib.sha256(fragment).hexdigest()
        canonical = hashlib.sha256(
            fragment.replace(str(target).encode(), b"${TARGET}")).hexdigest()
    elif isinstance(fragment, str):
        installed = hashlib.sha256(fragment.encode()).hexdigest()
        canonical = hashlib.sha256(
            fragment.replace(str(target), "${TARGET}").encode()).hexdigest()
    else:
        installed = hashlib.sha256(stable_serialize(fragment)).hexdigest()
        canonical = hashlib.sha256(
            stable_serialize(canonicalize_paths(fragment, target))).hexdigest()
    return installed, canonical


# ══════════════════════════ §8 片段读取 ══════════════════════════

def read_fragment(path: Path, spec: ResourceSpec):
    """只服务通用片段模型（owned_file / seed_file / json_fragment /
    managed_block / hook）。codex_link 永不进入本函数（§5 已分派）。"""
    if spec.resource_type in ("owned_file", "seed_file"):
        return path.read_bytes() if path.is_file() else FRAGMENT_ABSENT

    # 容器型片段：容器文件不存在 → FRAGMENT_ABSENT（不是异常）
    if not path.is_file():
        return FRAGMENT_ABSENT

    return fragment_from_bytes(spec, path.read_bytes())


def fragment_from_bytes(spec: ResourceSpec, data: bytes):
    """从容器/文件的内存字节提取片段（read_fragment 的无路径版本——
    plan 阶段容器只读一次的配套；owned/seed 语义不同：data 即片段整体）。"""
    if spec.resource_type in ("owned_file", "seed_file"):
        return data if data is not None else FRAGMENT_ABSENT

    if spec.resource_type == "json_fragment":
        try:
            current = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ContainerUnreadableError(Path(spec.destination_path), e)
        for key in spec.locator.split("."):
            if not isinstance(current, dict) or key not in current:
                return FRAGMENT_ABSENT  # key 不存在（值为 null 的 key 算存在）
            current = current[key]
        return current

    if spec.resource_type == "managed_block":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ContainerUnreadableError(Path(spec.destination_path), e)
        block = extract_block(text, spec.block_start, spec.block_end)
        if block is not FRAGMENT_ABSENT:
            return block
        # 标记区块不存在 → 尝试存量旧行组（无标记形态，血统由 hash 判定）
        span = legacy_section_span(spec, text)
        if span is None:
            return FRAGMENT_ABSENT
        section = text[span[0]:span[1]]
        if spec.id == "mcp-codex":
            # TOML 节后随空行不参与血统判定（旧 install.sh 的追加形态以 \n 收尾）
            section = section.rstrip("\n") + "\n"
        return section

    if spec.resource_type == "hook":
        try:
            data_obj = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ContainerUnreadableError(Path(spec.destination_path), e)
        hooks = data_obj.get("hooks") if isinstance(data_obj, dict) else None
        ss = hooks.get("SessionStart") if isinstance(hooks, dict) else None
        if not isinstance(ss, list):
            return FRAGMENT_ABSENT        # 结构异常（非数组）按片段缺失处理，不崩溃
        for entry in ss:
            entry_hooks = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(entry_hooks, list):
                continue
            for h in entry_hooks:
                if isinstance(h, dict) and h.get("command") == spec.expected_hook_command:
                    return h              # 精确匹配
        return FRAGMENT_ABSENT

    raise ValueError(f"Unknown type: {spec.resource_type}")


def extract_block(text: str, block_start: str, block_end: str):
    """返回标记区块的实际内容（含标记行）：
    - start、end 都在 → 区块
    - 只有 start（存量旧行组，如 v6 前的 .gitignore）→ start 至 EOF 的实际内容
    - 都不在 → FRAGMENT_ABSENT
    任何情况下都不得按"固定行数"截取——用户行不可被吞并。"""
    if block_start in text:
        start_idx = text.index(block_start)
        if block_end in text[start_idx:]:
            end_idx = text.index(block_end, start_idx) + len(block_end)
            return text[start_idx:end_idx]
        return text[start_idx:]          # 未闭合：供 legacy 精确匹配判定
    return FRAGMENT_ABSENT


def legacy_section_span(spec: ResourceSpec, text: str):
    """存量旧行组（无标记形态）的 (start, end) 切片区间；无则 None。

    - claude-md-block / agents-md-block：`## 项目记忆（坑与流程）` 标题行起，
      到下一个二级标题前或 EOF（结构化边界，非固定行数）
    - mcp-codex：`[mcp_servers.agent-engineering]` 节头行起，到下一个 `[` 节
      前或 EOF——无论内容是否 kit 形态都切出原文，血统由 canonical hash
      判定（不匹配 → conflict，绝不吞并用户节）
    - 其余 spec：无旧行组概念 → None"""
    if spec.id in ("claude-md-block", "agents-md-block"):
        pattern = LEGACY_MEMORY_HEADING
    elif spec.id == "mcp-codex":
        pattern = LEGACY_CODEX_SECTION
    else:
        return None
    m = re.search(rf"^{re.escape(pattern)}[ \t]*$", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    m2 = re.search(r"^## " if spec.id != "mcp-codex" else r"^\[", rest, re.M)
    end = m.end() + (m2.start() if m2 else len(rest))
    return (m.start(), end)


# ══════════════════════════ 内容生成（安装器侧可信） ══════════════════════════

def generate_hook(spec: ResourceSpec, target: Path) -> dict:
    """hook 资源的当前 kit 片段（§8 精确 command 匹配键）。"""
    return {"type": "command", "command": spec.expected_hook_command,
            "timeout": HOOK_TIMEOUT}


def generate_fragment(spec: ResourceSpec, target: Path):
    """spec 的当前 kit 片段（installed 形态）——current_kit_hash /
    trusted_canonical_hashes 的数据源。返回形态与 read_fragment 一致：
    owned/seed → bytes；json_fragment/hook → dict；managed_block → str。"""
    if spec.resource_type == "owned_file":
        return (KIT_DIR / spec.source_path).read_bytes()
    if spec.resource_type == "seed_file":
        # 种子内容（§2：存在时不读不比对，此返回值只用于全新创建路径）
        return (KIT_DIR / spec.source_path).read_bytes()
    if spec.resource_type == "json_fragment":
        return {"command": str(target / ".repo-memory-kit" / "bin" / "agent-engineering-mcp"),
                "args": [], "env": {}}
    if spec.resource_type == "managed_block":
        return _generate_block(spec, target)
    if spec.resource_type == "hook":
        return generate_hook(spec, target)
    raise ValueError(f"codex_link 无片段模型: {spec.resource_type}")


def _generate_block(spec: ResourceSpec, target: Path) -> str:
    """managed_block 的当前 kit 区块（start 行 + 内容 + end 行；内容以换行收尾）。"""
    if spec.id == "cbmignore-block":
        content = "!docs/\ndocs/*\n!docs/memory/\n"
    elif spec.id == "gitignore-block":
        # 多人协作（git / SVN 同一套清单——SVN 由 --svn 写入 svn:ignore）：
        # 每机状态/派生/含本机绝对路径的文件，入库即成永久冲突源。
        # settings.json 不在列——hook 命令是 $CLAUDE_PROJECT_DIR 字面量（运行时
        # 展开），可跨机器移植，按 Claude Code 项目设置惯例随库共享。
        content = (".repo-memory-kit/\n"
                   ".mcp.json\n"
                   ".codex/\n")
    elif spec.id == "mcp-codex":
        content = (f"[mcp_servers.agent-engineering]\n"
                   f"command = \"{target / '.repo-memory-kit' / 'bin' / 'agent-engineering-mcp'}\"\n")
    else:
        content = (KIT_DIR / spec.source_path).read_text()
        if not content.endswith("\n"):
            content += "\n"
    return f"{spec.block_start}\n{content}{spec.block_end}"


# ══════════════════════════ §10 可信产物集合 ══════════════════════════

_LEGACY_HASHES: dict[str, set[str]] | None = None


def legacy_canonical_hashes_from_file(spec_id: str) -> set[str]:
    """installer/legacy_hashes.json（CI 构建时生成，随 kit 发布）中该 spec 的
    全部历史 canonical hash。文件缺失/条目缺失 → 空集。首次加载即解析全部
    spec 的条目（缓存按 spec 取用——只装首个请求的 spec 会让后续 spec 拿到
    空集，全部误判 conflict）。"""
    global _LEGACY_HASHES
    if _LEGACY_HASHES is None:
        hashes: dict[str, set[str]] = {}
        path = KIT_DIR / "installer" / "legacy_hashes.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for version, entries in (data.get("kit_versions") or {}).items():
            if not isinstance(entries, dict):
                continue
            for sid, value in entries.items():
                if isinstance(value, str) and value.startswith("canonical_sha256:"):
                    hashes.setdefault(sid, set()).add(value.split(":", 1)[1])
        _LEGACY_HASHES = hashes
    return set(_LEGACY_HASHES.get(spec_id, ()))


def trusted_canonical_hashes(spec: ResourceSpec, target: Path) -> set[str]:
    """当前版本预期内容 + 历史版本内容（来自 legacy_hashes.json）。
    两处均为安装器侧可信数据（§18）；canonical ∈ 此集合 = kit 血统。"""
    hashes = set()
    current = generate_fragment(spec, target)
    _, canonical = compute_fragment_hashes(current, target)
    hashes.add(canonical)
    # CLAUDE/AGENTS 的旧安装形态是“当前模板裸段落”，没有 block marker。
    # 它与带标记 current fragment 的 hash 必然不同；若只依赖构建时生成的
    # legacy_hashes，新模板在下次发布哈希生成前会被误判为用户冲突。模板来自
    # 只读 KIT_DIR + Registry，因此裸形态同样是安装器侧可信血统。
    if (spec.resource_type == "managed_block"
            and spec.id in ("claude-md-block", "agents-md-block")
            and spec.source_path is not None):
        bare = (KIT_DIR / spec.source_path).read_text(encoding="utf-8")
        _, bare_canonical = compute_fragment_hashes(bare, target)
        hashes.add(bare_canonical)
    hashes |= legacy_canonical_hashes_from_file(spec.id)
    return hashes


# ══════════════════════════ §5 安装状态 ══════════════════════════

def current_kit_hash(spec: ResourceSpec, target: Path) -> str:
    """generate_fragment(spec, target) 的 installed 形态 hash。"""
    installed, _ = compute_fragment_hashes(generate_fragment(spec, target), target)
    return installed


def determine_status(target: Path, spec: ResourceSpec, entry,
                     codex_root_cli: Path | None = None):
    """codex_link 无 --codex-root 时返回 None = 无法核验（调用方按'跳过并提示'
    处理，doctor 报告'需 --codex-root 才能核验'，不猜状态）。"""
    # —— seed_file：存在即用户所有，不读内容、不比对、永不 conflict（§2）——
    if spec.resource_type == "seed_file":
        if (target / spec.destination_path).is_file():
            return "managed"
        return "managed" if entry is None else "drifted"
        # doctor 把 seed 的 drifted 映射为 DEGRADED（§14.3，可 --repair 补种）

    # —— codex_link：不进 read_fragment（文件片段模型不适用）——
    if spec.resource_type == "codex_link":
        if codex_root_cli is None:
            return None                     # 无法核验
        link_spec = CodexLinkSpec(skill_name=spec.link_skill_name)
        link_spec.validate(codex_root_cli, target)   # ★ 任何派生前先验证（v11）
        link = link_spec.derive_link_path(codex_root_cli)
        expected = link_spec.derive_target_path(target)

        # 符号链接判定（POSIX）
        if link.is_symlink():
            if os.readlink(link) == str(expected):
                return "managed"
            if entry is None:
                return "conflict"
            return "drifted"

        # 复制模式判定（Windows）：SKILL.md 内容 hash 匹配 → managed
        skill_md = link / "SKILL.md"
        if skill_md.is_file():
            source_md = expected / "SKILL.md"
            if source_md.is_file():
                import hashlib as _h
                if (_h.sha256(skill_md.read_bytes()).hexdigest()
                        == _h.sha256(source_md.read_bytes()).hexdigest()):
                    return "managed"      # 复制的 kit 内容，一致

        if entry is None:
            return "conflict" if link.exists() else "managed"
            # 非 kit 占位 → conflict；空位 → 可创建
        return "drifted"                   # 有记录但资源缺失/不匹配

    # —— 通用片段模型（owned_file / json_fragment / managed_block / hook）——
    dst = target / spec.destination_path
    fragment = read_fragment(dst, spec)
    if fragment is FRAGMENT_ABSENT:
        installed = canonical = None
    else:
        installed, canonical = compute_fragment_hashes(fragment, target)

    if entry is None:
        if fragment is FRAGMENT_ABSENT:
            return "managed"          # Kit 可创建（owned/seed）或合并
        if installed == current_kit_hash(spec, target):
            return "managed"          # 与当前版本一致
        if canonical in trusted_canonical_hashes(spec, target):
            return "adopted_legacy"   # 历史版本产物（legacy 只存 canonical）
                                      # 仅授权原地升级 kit 自有旧内容（§18）
        return "conflict"             # 用户自有内容

    if fragment is FRAGMENT_ABSENT:
        return "drifted"             # 装过，现在没了
    lineage = canonical in trusted_canonical_hashes(spec, target)
    if installed == entry.installed_hash and lineage:
        return "managed"             # 双条件：记录一致 + 血统证明
    if lineage:
        return "drifted"             # kit 血统但与记录不符 → 报告，--repair 可恢复
    return "conflict"                # 无 kit 血统但 Manifest 声称管理过 → 伪造/错乱


# ══════════════════════════ §16 CodexLinkSpec ══════════════════════════

@dataclass(frozen=True)
class CodexLinkSpec:
    """codex_link 资源不遵循"路径必须在 target 内"约束，有自己受限的 scope。
    信任边界：codex_root 只能来自 CLI 参数，绝不来自 Manifest——
    entry.codex_root 是可篡改的审计展示字段（§4）。"""
    skill_name: str  # 只能是 REGISTRY 中已定义的 skill 名

    def derive_link_path(self, codex_root: Path) -> Path:
        return codex_root / ".agents" / "skills" / self.skill_name

    def derive_target_path(self, target: Path) -> Path:
        return target / ".agents" / "skills" / self.skill_name

    def validate(self, codex_root: Path, target: Path) -> None:
        if not codex_root.is_absolute():
            raise SecurityError("codex_root must be absolute")
        expected_target = self.derive_target_path(target)
        validate_safe_path(expected_target, target)
        link_path = self.derive_link_path(codex_root)
        # 前缀判断有碰撞漏洞（"/a/b" 会放行 "/a/bc/x"），必须用 relative_to。
        # 注意不能对 link_path 整体 .resolve()——kit 链接本来就指向 target（必然在
        # codex_root 之外），跟随后必然"逃逸"。解析父目录链、保留最终组件字面值：
        # 父链中的符号链接（如 .agents/skills 被换）仍会被查出。
        try:
            (link_path.parent.resolve() / link_path.name).relative_to(codex_root.resolve())
        except ValueError:
            raise SecurityError(f"codex link escapes workspace: {link_path}")
