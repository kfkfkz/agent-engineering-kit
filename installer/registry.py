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
import subprocess
import stat as stat_module
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

# ── kit 仓库根（installer/ 的父目录；安装器代码的一部分，目标仓库不可篡改）──
KIT_DIR = Path(__file__).resolve().parent.parent


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
    "delivery-gate", "spec-migrate", "task-handoff",
]

KIT_TOOLS = [
    "validate-memory.sh", "memory-build", "spec-migrate", "memory-recall",
    "domain-check", "session-reminder", "agent-engineering-mcp",
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
REGISTRY: list[ResourceSpec] = (
    _root_specs() + _memory_specs() + _skill_specs()
    + _bin_specs() + _codex_link_specs()
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
                    | {STATE_REGISTRY["state.codex-workspace-marker"].destination_path})
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
    """preflight 只读检查：逐级 lstat 无符号链接，最终路径在 target 内。"""
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


def validate_relative_path(rel: str) -> PurePosixPath:
    """secure_* 文件入口的第一道校验（校验完整文件路径，v12）：
    - 必须是非空**相对**路径——os.open(绝对路径, dir_fd=...) 会忽略 dir_fd，
      以 "/" 开头即逃逸出 target
    - 禁止 .. 组件——包括最后一个组件
    （PurePosixPath 归一化会吞掉单点组件；根目录文件如 "AGENTS.md" 合法——
    其父目录串 "." 由 secure_walk_dir_fd 特判。）"""
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts:
        raise SecurityError(f"secure_* 只接受非空相对路径: {rel!r}")
    if any(part == ".." for part in p.parts):
        raise SecurityError(f"路径含 .. 组件: {rel!r}")
    return p


def secure_walk_dir_fd(target: Path, dir_rel: str) -> int:
    """从 target 根逐级 O_NOFOLLOW|O_DIRECTORY 打开到 dir_rel，返回该目录 fd。

    dir_rel 为 ""/"."（根目录文件的父目录，如 AGENTS.md / CLAUDE.md / .mcp.json /
    .cbmignore / .gitignore）→ 直接返回 target 根 fd（v12 特判）。
    中间任一级是符号链接即失败；调用方负责 os.close(fd)。"""
    if dir_rel in ("", "."):
        return os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    parts = validate_relative_path(dir_rel).parts
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def secure_open(target: Path, rel: str, flags: int, mode: int = 0o600) -> int:
    """kit 一切文件打开/写入的入口：先 validate 完整文件路径（拒绝最后的 ".."
    组件），walk 到父目录（根目录文件的父 = target 根）后以 dir_fd 打开最终
    文件（O_NOFOLLOW）。成功时只返回文件 fd——父目录 fd 已关闭。"""
    p = validate_relative_path(rel)                      # 完整文件路径校验（v12）
    dir_fd = secure_walk_dir_fd(target, str(p.parent))   # "." → target 根
    try:
        return os.open(p.name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def secure_replace(target: Path, src_rel: str, dst_rel: str) -> None:
    """全程持有 dir_fd 的原子替换（v10：不回退路径操作，父目录 TOCTOU 不回归）。
    先各自 validate 完整路径 → walk 到 src/dst 父目录 fd → os.replace(双
    dir_fd) → 经 dst 父目录 fd fsync。"""
    src = validate_relative_path(src_rel)
    dst = validate_relative_path(dst_rel)
    src_dir_fd = secure_walk_dir_fd(target, str(src.parent))
    try:
        dst_dir_fd = secure_walk_dir_fd(target, str(dst.parent))
        try:
            os.replace(src.name, dst.name,
                       src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            os.fsync(dst_dir_fd)               # 经 fd fsync，不回退路径
        finally:
            os.close(dst_dir_fd)
    finally:
        os.close(src_dir_fd)


def secure_unlink(target: Path, rel: str) -> None:
    """dir_fd 版 unlink + 经 fd fsync（不走路径；根目录文件同样支持）。"""
    p = validate_relative_path(rel)                      # 完整文件路径校验（v12）
    dir_fd = secure_walk_dir_fd(target, str(p.parent))
    try:
        os.unlink(p.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def secure_rmdir(target: Path, rel: str) -> None:
    """dir_fd 版 rmdir + 经 fd fsync（§6 家族成员：清理事务目录/空目录用；
    只删空目录——非空时 ENOTEMPTY 抛出，由调用方按需忽略）。"""
    p = validate_relative_path(rel)
    dir_fd = secure_walk_dir_fd(target, str(p.parent))
    try:
        os.rmdir(p.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def secure_mkdir(target: Path, rel: str) -> None:
    """逐级创建缺失目录（幂等——第二次 install/update 必须能跑，v12 全流程接入）：
    - rel 为 ""/"." → no-op（根目录文件的父目录无需创建）
    - 已存在且为真实目录（O_NOFOLLOW 打开成功）→ 继续
    - 已存在但是符号链接（ELOOP）或被文件占位（ENOTDIR）→ SecurityError
    - 缺失（ENOENT）→ os.mkdir(dir_fd=父) + 经父 fd fsync（目录项持久化）

    调用点（v12 接入清单）：acquire_install_lock（.repo-memory-kit）、
    write_transaction_atomic（tx/<id>）、安装流程步骤 3（tx/<id>/backups）、
    stage_resource（destination 父目录，如 .claude/skills/<name>）、
    zvec rebuild（zvec/ 与 generations/<uuid>）。"""
    if rel in ("", "."):
        return                                          # 根目录文件的父目录
    parts = validate_relative_path(rel).parts
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            except FileNotFoundError:                     # 缺失 → 创建
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:                   # 并发创建竞窗 → 复用
                    pass
                nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            except OSError as e:                          # ELOOP（符号链接）/ ENOTDIR（文件占位）
                raise SecurityError(f"目录组件已存在但异常（{e}）: {part}")
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)


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
        hooks = (data_obj.get("hooks", {}) if isinstance(data_obj, dict)
                 else {}).get("SessionStart", [])
        for entry in hooks:
            for h in entry.get("hooks", []):
                if h.get("command") == spec.expected_hook_command:  # 精确匹配
                    return h
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
        content = ".repo-memory-kit/zvec/\n"
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
        expected = str(link_spec.derive_target_path(target))
        if link.is_symlink() and os.readlink(link) == expected:
            return "managed"
        if entry is None:
            return "conflict" if (link.exists() or link.is_symlink()) else "managed"
            # 非 kit 链接占位 → conflict（不覆盖）；空位 → 可创建
        return "drifted"                   # 有记录但链接缺失/不匹配

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
