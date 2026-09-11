#!/bin/sh
# agent-engineering-kit 接入/更新/卸载：把统一 AI Agent 工程工作流安装到目标仓库
# 用法:
#   ./install.sh /path/to/your-project            # 首次接入（幂等）
#   ./install.sh --update /path/to/your-project    # 更新 kit 管辖文件
#   ./install.sh --repair /path/to/your-project    # 修复缺失/漂移的 kit 管辖文件
#   ./install.sh --doctor /path/to/your-project    # 只读检查安装健康度
#   ./install.sh --dry-run /path/to/your-project   # 只展示计划，不写入
#   ./install.sh --migrate-specify /path/to/your-project
#                                                  # 显式增量迁移已有 Spec Kit 文档
#   ./install.sh --migrate-specify=001-demo /path/to/your-project
#                                                  # 只迁移指定 feature（逗号分隔可多个）
#   ./install.sh --migrate-specify=001-demo --sdd-layout --sdd-version V1.0 /path/to/your-project
#                                                  # 迁移为 SDD 目录结构（docs/03-SDD/V<版本>）
#   ./install.sh --uninstall /path/to/your-project # 卸载 kit 管辖产物（不触碰用户数据）
#   ./install.sh --with-codebase-memory /path/to/your-project
#                                                  # 同时安装/配置代码图谱 MCP
#   ./install.sh --codex-root /workspace /path/to/your-project
#                                                  # Codex 从父级 workspace 启动时暴露仓库技能
#
# 文件所有权约定（.repo-memory-kit 为向后兼容的内部状态目录）：
#   kit 管辖（记录于 .repo-memory-kit/manifest，自动刷新）:
#     RULES.md、双端技能、条目/画像模板、校验器、CLAUDE/AGENTS 托管区块、.cbmignore 托管区块
#   用户所有（绝不覆盖/删除）:
#     README.md（索引）、全部记忆条目、.anchors.json
#
# 安全约定：任何删除只针对"内容指纹匹配 kit 当前或历史版本"的文件——
#   与 kit 产物不一致的文件一律保留并提示，杜绝误删用户定制内容。

set -e

MODE=install
DRY_RUN=0
MIGRATE_SPECIFY=0
MIGRATE_FEATURES=""
SDD_LAYOUT=0
SDD_VERSION=""
INSTALL_CODEBASE_MEMORY=0
CODEX_ROOT=""
TARGET=""
set_mode() {
    requested="$1"
    if [ "$MODE" != "install" ] && [ "$MODE" != "$requested" ]; then
        echo "错误: --$requested 不能与 --$MODE 同时使用"; exit 1
    fi
    MODE="$requested"
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --update) set_mode update ;;
        --repair) set_mode repair ;;
        --doctor) set_mode doctor ;;
        --uninstall) set_mode uninstall ;;
        --dry-run) DRY_RUN=1 ;;
        --migrate-specify) MIGRATE_SPECIFY=1 ;;
        --sdd-layout) SDD_LAYOUT=1 ;;
        --sdd-version)
            shift
            [ "$#" -gt 0 ] || { echo "错误: --sdd-version 需要版本目录名参数"; exit 1; }
            SDD_VERSION="$1"
            ;;
        --migrate-specify=*)
            MIGRATE_SPECIFY=1
            MIGRATE_FEATURES="${1#*=}"
            [ -n "$MIGRATE_FEATURES" ] || { echo "错误: --migrate-specify= 需要至少一个 feature 名（逗号分隔）"; exit 1; }
            ;;
        --with-codebase-memory) INSTALL_CODEBASE_MEMORY=1 ;;
        --codex-root)
            shift
            [ "$#" -gt 0 ] || { echo "错误: --codex-root 需要目录参数"; exit 1; }
            CODEX_ROOT="$1"
            ;;
        -*) echo "错误: 未知选项 $1"; exit 1 ;;
        *)
            [ -z "$TARGET" ] || { echo "错误: 只能指定一个目标仓库"; exit 1; }
            TARGET="$1"
            ;;
    esac
    shift
done

[ -n "$TARGET" ] || { echo "用法: ./install.sh [--update|--repair|--doctor|--uninstall] [--dry-run] [--migrate-specify[=FEATURE,...]] [--with-codebase-memory] [--codex-root /workspace] /path/to/your-project"; exit 1; }
[ -d "$TARGET" ] || { echo "错误: 目标目录不存在: $TARGET"; exit 1; }
[ "$MODE" != "uninstall" ] || [ "$INSTALL_CODEBASE_MEMORY" = "0" ] || {
    echo "错误: --with-codebase-memory 不能与 --uninstall 同时使用"; exit 1;
}
[ "$MODE" != "doctor" ] || [ "$INSTALL_CODEBASE_MEMORY" = "0" ] || {
    echo "错误: --with-codebase-memory 不能与 --doctor 同时使用"; exit 1;
}
[ "$MODE" != "uninstall" ] || [ "$MIGRATE_SPECIFY" = "0" ] || {
    echo "错误: --migrate-specify 不能与 --uninstall 同时使用"; exit 1;
}
[ "$MODE" != "doctor" ] || [ "$MIGRATE_SPECIFY" = "0" ] || {
    echo "错误: --migrate-specify 不能与 --doctor 同时使用"; exit 1;
}
[ "$SDD_LAYOUT" = "0" ] || [ -n "$SDD_VERSION" ] || {
    echo "错误: --sdd-layout 需要 --sdd-version <版本目录名>（如 V1.0）"; exit 1;
}
[ -z "$SDD_VERSION" ] || [ "$SDD_LAYOUT" = "1" ] || {
    echo "错误: --sdd-version 仅在 --sdd-layout 时使用"; exit 1;
}
[ "$SDD_LAYOUT" = "0" ] || [ "$MIGRATE_SPECIFY" = "1" ] || {
    echo "错误: --sdd-layout 仅在 --migrate-specify 时使用"; exit 1;
}
TARGET="$(cd "$TARGET" && pwd)"

CODEX_ROOT_MARKER="$TARGET/.repo-memory-kit/codex-workspace-root"
if [ -z "$CODEX_ROOT" ] && [ -f "$CODEX_ROOT_MARKER" ]; then
    _saved_root="$(sed -n '1p' "$CODEX_ROOT_MARKER")"
    case "$_saved_root" in /*) CODEX_ROOT="$_saved_root" ;; esac
    # 已记录的 workspace 根需用户确认或与当前目标目录在同一文件系统
    if [ -n "$CODEX_ROOT" ] && [ "$MODE" != "uninstall" ] && [ "$MODE" != "doctor" ]; then
        echo "提示: 使用已记录的 Codex workspace: $CODEX_ROOT（来自 .repo-memory-kit/codex-workspace-root）"
    fi
fi
if [ -n "$CODEX_ROOT" ]; then
    [ -d "$CODEX_ROOT" ] || { echo "错误: Codex workspace 不存在: $CODEX_ROOT"; exit 1; }
    CODEX_ROOT="$(cd "$CODEX_ROOT" && pwd)"
    if [ "$MODE" != "uninstall" ] && [ "$MODE" != "doctor" ] && [ "$DRY_RUN" != "1" ] && [ "$CODEX_ROOT" != "$TARGET" ]; then
        if [ -d "$CODEX_ROOT/.agents" ]; then
            [ -w "$CODEX_ROOT/.agents" ] || {
                echo "错误: Codex workspace 的 .agents 不可写: $CODEX_ROOT/.agents"; exit 1;
            }
        else
            [ -w "$CODEX_ROOT" ] || {
                echo "错误: Codex workspace 不可写，无法创建 .agents: $CODEX_ROOT"; exit 1;
            }
        fi
    fi
fi

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

SEC_START="<!-- repo-memory-kit:start -->"
SEC_END="<!-- repo-memory-kit:end -->"
BLOCK_START="# repo-memory-kit:start"
BLOCK_END="# repo-memory-kit:end"

KIT_SKILLS="memory-check memory-capture repo-delivery codebase-memory systematic-debugging tdd reuse-research security-review delivery-gate spec-migrate task-handoff"

MANAGED_FILES="
docs/memory/RULES.md
docs/memory/pitfalls/_TEMPLATE.md
docs/memory/decisions/_TEMPLATE.md
docs/memory/playbooks/_TEMPLATE.md
docs/memory/_PROFILE_TEMPLATE.md
.claude/skills/memory-check/SKILL.md
.claude/skills/memory-capture/SKILL.md
.claude/skills/repo-delivery/SKILL.md
.claude/skills/codebase-memory/SKILL.md
.claude/skills/systematic-debugging/SKILL.md
.claude/skills/tdd/SKILL.md
.claude/skills/reuse-research/SKILL.md
.claude/skills/security-review/SKILL.md
.claude/skills/delivery-gate/SKILL.md
.claude/skills/spec-migrate/SKILL.md
.claude/skills/task-handoff/SKILL.md
.agents/skills/memory-check/SKILL.md
.agents/skills/memory-capture/SKILL.md
.agents/skills/repo-delivery/SKILL.md
.agents/skills/codebase-memory/SKILL.md
.agents/skills/systematic-debugging/SKILL.md
.agents/skills/tdd/SKILL.md
.agents/skills/reuse-research/SKILL.md
.agents/skills/security-review/SKILL.md
.agents/skills/delivery-gate/SKILL.md
.agents/skills/spec-migrate/SKILL.md
.agents/skills/task-handoff/SKILL.md
.repo-memory-kit/bin/validate-memory.sh
.repo-memory-kit/bin/memory-build
.repo-memory-kit/bin/spec-migrate
.repo-memory-kit/bin/memory-recall
.repo-memory-kit/bin/domain-check
.repo-memory-kit/bin/session-reminder
.repo-memory-kit/bin/agent-engineering-mcp
.repo-memory-kit/codex-workspace-root
"

# 哈希命令探测（Linux: sha256sum / macOS: shasum -a 256）
if command -v sha256sum >/dev/null 2>&1; then
    hash_of() { sha256sum | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    hash_of() { shasum -a 256 | cut -d' ' -f1; }
else
    echo "错误: 找不到 sha256sum 或 shasum，无法继续" >&2
    exit 1
fi
file_hash() { hash_of < "$1"; }

install_codebase_memory_runtime() {
    if command -v codebase-memory-mcp >/dev/null 2>&1; then
        echo "正在使用已安装的 codebase-memory-mcp 配置当前 Agent 环境..."
        codebase-memory-mcp install
        return
    fi

    command -v curl >/dev/null 2>&1 || {
        echo "错误: --with-codebase-memory 需要 curl 下载官方安装器" >&2; return 1;
    }
    cbm_tmp_dir="$(mktemp -d)"
    cbm_installer="$cbm_tmp_dir/install.sh"
    echo "正在下载并执行 DeusData/codebase-memory-mcp 官方安装器..."
    if ! curl -fsSL "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh" -o "$cbm_installer"; then
        rmdir "$cbm_tmp_dir" 2>/dev/null || true
        echo "错误: codebase-memory-mcp 安装器下载失败" >&2
        return 1
    fi
    if ! sh "$cbm_installer"; then
        rm -f "$cbm_installer"
        rmdir "$cbm_tmp_dir" 2>/dev/null || true
        echo "错误: codebase-memory-mcp 官方安装器执行失败" >&2
        return 1
    fi
    rm -f "$cbm_installer"
    rmdir "$cbm_tmp_dir" 2>/dev/null || true
}

# kit 内某路径在 git 历史中所有版本的内容指纹。
# norm=1 时先去空行再哈希（用于段落匹配——目标文件中的段落与模板原文的空行口径不同）。
# blob 不存在的提交必须跳过：git show 失败时管道仍会产出"空内容哈希"，
# 导致空的用户文件被误判为 kit 产物而删除。
kit_path_hashes() { # $1=kit 内相对路径 [$2=0原文|1去空行]
    [ -d "$KIT_DIR/.git" ] || return 0
    norm="${2:-0}"
    git -C "$KIT_DIR" log --all --format=%H -- "$1" 2>/dev/null | while read -r rev; do
        if git -C "$KIT_DIR" cat-file -e "$rev:$1" 2>/dev/null; then
            if [ "$norm" = "1" ]; then
                git -C "$KIT_DIR" show "$rev:$1" | sed '/^[[:space:]]*$/d' | hash_of
            else
                git -C "$KIT_DIR" show "$rev:$1" | hash_of
            fi
        fi
    done | sort -u
}

# 文件内容与 kit 当前或任一历史版本一致 → 判定为本 kit 产物
content_is_kit_artifact() { # $1=文件 $2=kit 内相对路径
    [ -f "$1" ] || return 1
    h="$(file_hash "$1")"
    if [ -f "$KIT_DIR/$2" ] && [ "$(hash_of < "$KIT_DIR/$2")" = "$h" ]; then return 0; fi
    kit_path_hashes "$2" | grep -q "^$h$"
}

assert_no_symlink_components() { # $1=基准目录 $2=相对路径
    safe_base="$1"
    safe_rel="$2"
    safe_current="$safe_base"
    safe_rest="$safe_rel"
    while [ -n "$safe_rest" ]; do
        case "$safe_rest" in
            */*) safe_part="${safe_rest%%/*}"; safe_rest="${safe_rest#*/}" ;;
            *) safe_part="$safe_rest"; safe_rest="" ;;
        esac
        [ -n "$safe_part" ] || continue
        safe_current="$safe_current/$safe_part"
        if [ -L "$safe_current" ]; then
            echo "错误: 受管路径包含符号链接，拒绝写入: $safe_current" >&2
            return 1
        fi
    done
}

preflight_managed_paths() {
    for safe_rel in docs/memory .claude/skills .agents/skills .repo-memory-kit .cbmignore CLAUDE.md AGENTS.md; do
        assert_no_symlink_components "$TARGET" "$safe_rel" || return 1
    done
    # 文件级检查:所有受管文件不能是符号链接(防通过链接写仓库外)
    for _file in $MANAGED_FILES .claude/settings.json; do
        if [ -L "$TARGET/$_file" ]; then
            echo "错误: $TARGET/$_file 是符号链接——拒绝安装(防越界写入)" >&2
            return 1
        fi
    done
    if [ -n "$CODEX_ROOT" ] && [ "$CODEX_ROOT" != "$TARGET" ]; then
        assert_no_symlink_components "$CODEX_ROOT" ".agents/skills" || return 1
    fi
}

# 安全:校验托管区块起止标记完整(都存在且顺序正确)
check_block_markers() { # $1=文件 $2=起始 $3=结束
    [ -f "$1" ] || return 0
    [ -L "$1" ] && return 0  # 符号链接不检查(由其他机制拒绝)
    start_line=0; end_line=0
    # 用 grep -n 找行号(同时验证顺序),避免 grep -c 的 || echo 双行问题
    start_line="$(grep -nF "$2" "$1" 2>/dev/null | head -1 | cut -d: -f1)"
    end_line="$(grep -nF "$3" "$1" 2>/dev/null | head -1 | cut -d: -f1)"
    # 无标记时 grep -n 输出空行,head -1 | cut -d: -f1 得空串
    [ -z "$start_line" ] && start_line=0
    [ -z "$end_line" ] && end_line=0
    if [ "$start_line" -gt 0 ] && [ "$end_line" -eq 0 ]; then
        echo "错误: $1 有起始标记但缺结束标记——请手工修复后再运行"
        return 1
    fi
    if [ "$start_line" -eq 0 ] && [ "$end_line" -gt 0 ]; then
        echo "错误: $1 有结束标记但缺起始标记——请手工修复后再运行"
        return 1
    fi
    if [ "$start_line" -gt 0 ] && [ "$end_line" -gt 0 ]; then
        # 顺序校验:START 必须在 END 之前
        if [ "$start_line" -ge "$end_line" ]; then
            echo "错误: $1 结束标记(行 $end_line)在起始标记(行 $start_line)之前——请手工修复后再运行"
            return 1
        fi
    fi
    # 重复检查:多个标记
    start_count=$(grep -cF "$2" "$1" 2>/dev/null) || start_count=0
    end_count=$(grep -cF "$3" "$1" 2>/dev/null) || end_count=0
    if [ "$start_count" -gt 1 ] || [ "$end_count" -gt 1 ]; then
        echo "错误: $1 托管区块标记重复(start=$start_count end=$end_count)——请手工修复后再运行"
        return 1
    fi
    return 0
}

# ── 只读生命周期命令 ────────────────────────────────────────────────
run_spec_migrate() { # $1=mode $2=脚本路径 $3=目标仓库
    mode="$1"; script="$2"; repo="$3"
    set --
    if [ -n "$MIGRATE_FEATURES" ]; then
        oldIFS=$IFS
        IFS=,
        for f in $MIGRATE_FEATURES; do set -- "$@" --feature "$f"; done
        IFS=$oldIFS
    fi
    if [ "$SDD_LAYOUT" = "1" ]; then
        set -- "$@" --layout sdd --sdd-version "$SDD_VERSION"
    fi
    python3 "$script" "$mode" "$@" "$repo"
}
doctor() {
    manifest="$TARGET/.repo-memory-kit/manifest"
    doctor_errors=0
    echo "DOCTOR: $TARGET"
    if [ ! -f "$manifest" ]; then
        echo "错误: 缺少安装清单 .repo-memory-kit/manifest"
        return 1
    fi

    for rel in $MANAGED_FILES; do
        [ "$rel" = ".repo-memory-kit/codex-workspace-root" ] && continue
        if [ ! -f "$TARGET/$rel" ]; then
            echo "错误: 缺失当前 kit 必备文件 $rel"
            doctor_errors=$((doctor_errors+1))
        elif ! grep -qF "  $rel" "$manifest"; then
            echo "错误: 受管文件未登记在清单 $rel"
            doctor_errors=$((doctor_errors+1))
        fi
    done

    installed_ver="$(sed -n 's/^kit_version=//p' "$manifest" | sed -n '1p')"
    current_ver="$(git -C "$KIT_DIR" describe --tags --always 2>/dev/null || echo unknown)"
    if [ -z "$installed_ver" ] || [ "$installed_ver" != "$current_ver" ]; then
        echo "错误: kit 版本不一致（已安装=${installed_ver:-未知}，当前=$current_ver）"
        doctor_errors=$((doctor_errors+1))
    fi

    managed_spaced=""
    for mf in $MANAGED_FILES; do managed_spaced="$managed_spaced $mf"; done
    while read -r line; do
        case "$line" in
            *"  "*) expected_hash="${line%%  *}"; rel="${line#*  }" ;;
            *) continue ;;
        esac
        case "$rel" in
            /*|*..*) echo "错误: 清单含可疑路径 $rel"; doctor_errors=$((doctor_errors+1)); continue ;;
        esac
        case " $managed_spaced " in
            *" $rel "*) ;;
            *) echo "错误: 清单含非受管路径 $rel"; doctor_errors=$((doctor_errors+1)); continue ;;
        esac
        if [ ! -f "$TARGET/$rel" ]; then
            echo "错误: 缺失受管文件 $rel"
            doctor_errors=$((doctor_errors+1))
        elif [ "$(file_hash "$TARGET/$rel")" != "$expected_hash" ]; then
            echo "错误: 受管文件漂移 $rel"
            doctor_errors=$((doctor_errors+1))
        fi
    done < "$manifest"

    for f in CLAUDE.md AGENTS.md; do
        if [ -f "$TARGET/$f" ] && ! grep -qF "$SEC_START" "$TARGET/$f"; then
            echo "警告: $f 存在但未安装托管区块（可能是受保护的定制旧段落）"
        fi
    done
    if [ -f "$CODEX_ROOT_MARKER" ]; then
        saved_codex_root="$(sed -n '1p' "$CODEX_ROOT_MARKER")"
        case "$saved_codex_root" in
            /*)
                for tool in $KIT_SKILLS; do
                    link="$saved_codex_root/.agents/skills/$tool"
                    expected="$TARGET/.agents/skills/$tool"
                    if [ ! -L "$link" ] || [ "$(readlink "$link" 2>/dev/null || true)" != "$expected" ]; then
                        echo "错误: Codex workspace 技能链接异常 $link"
                        doctor_errors=$((doctor_errors+1))
                    fi
                done
                ;;
            *) echo "错误: Codex workspace 记录路径可疑"; doctor_errors=$((doctor_errors+1)) ;;
        esac
    fi
    # 扩展检查:.cbmignore 区块(存在但缺区块=error;完全不存在=跳过,项目可能不需要)
    if [ -f "$TARGET/.cbmignore" ]; then
        if ! grep -qF "$BLOCK_START" "$TARGET/.cbmignore"; then
            echo "警告: .cbmignore 存在但缺托管区块"
            doctor_errors=$((doctor_errors+1))
        fi
    fi
    # hook 检查(有 settings.json 且 kit 装过 hook 但被删=error)
    if [ -f "$TARGET/.claude/settings.json" ] && command -v python3 >/dev/null 2>&1; then
        if ! python3 -c "
import json,os
try:
    # 只在 manifest 存在(装过 kit)时检查
    if not os.path.exists('$TARGET/.repo-memory-kit/manifest'): exit(0)
    d=json.load(open('$TARGET/.claude/settings.json'))
    ss=d.get('hooks',{}).get('SessionStart',[])
    for e in ss:
        for h in e.get('hooks',[]):
            if 'session-reminder' in h.get('command',''): exit(0)
    exit(1)
except: exit(0)
" 2>/dev/null; then
            echo "警告: SessionStart 钩子缺失(运行 --repair 恢复)"
            doctor_errors=$((doctor_errors+1))
        fi
    fi
    if [ "$doctor_errors" -gt 0 ]; then
        echo "DOCTOR: 发现 $doctor_errors 个错误；可运行 --repair 修复受管产物"
        return 1
    fi
    echo "DOCTOR: 健康"
}

dry_run() {
    echo "DRY-RUN: 不会写入文件或执行联网安装"
    echo "目标: $TARGET"
    echo "模式: $MODE"
    case "$MODE" in
        uninstall) echo "计划: 校验指纹后移除 kit 受管文件、托管区块和 workspace 技能链接" ;;
        update|repair|install)
            echo "计划: 安装/刷新技能: $KIT_SKILLS"
            echo "计划: 刷新记忆规则、模板、校验器、生成器和 Spec 迁移器"
            echo "计划: 更新 CLAUDE.md、AGENTS.md 与 .cbmignore 托管区块（存在时）"
            [ -n "$CODEX_ROOT" ] && [ "$CODEX_ROOT" != "$TARGET" ] && echo "计划: 在 $CODEX_ROOT 暴露 target-bound Codex 技能链接"
            [ "$INSTALL_CODEBASE_MEMORY" = "0" ] || echo "计划: 配置 codebase-memory-mcp（实际执行时可能联网）"
            if [ -d "$TARGET/.specify/specs" ]; then
                echo "发现 Spec Kit: $TARGET/.specify/specs"
                if [ "$MIGRATE_SPECIFY" = "1" ]; then
                    [ "$SDD_LAYOUT" = "0" ] || echo "计划: 迁移为 SDD 结构 docs/03-SDD/$SDD_VERSION"
                    run_spec_migrate --dry-run "$KIT_DIR/spec-migrate" "$TARGET"
                else
                    run_spec_migrate --check "$KIT_DIR/spec-migrate" "$TARGET"
                    echo "计划: 仅报告；添加 --migrate-specify 才会迁移"
                fi
            fi
            ;;
    esac
}

# ── 卸载(标记完整性预检) ──
for _md in "$TARGET/CLAUDE.md" "$TARGET/AGENTS.md"; do
    check_block_markers "$_md" "$SEC_START" "$SEC_END" || exit 1
done
check_block_markers "$TARGET/.cbmignore" "$BLOCK_START" "$BLOCK_END" || exit 1

# 卸载 ────────────────────────────────────────────────────────────
mcp_server_uninstall() {
    command -v python3 >/dev/null 2>&1 || return 0
    python3 - "$TARGET" <<'PYUNMCP'
import json, sys
from pathlib import Path
target = Path(sys.argv[1])

# 清理 .mcp.json
mcp_json = target / ".mcp.json"
if mcp_json.exists():
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        mcp = data.get("mcpServers", {})
        if "agent-engineering" in mcp:
            # 只删除我们注册的(检查 command 路径)
            cmd = mcp["agent-engineering"].get("command", "")
            if ".repo-memory-kit" in cmd:
                del mcp["agent-engineering"]
                if not mcp:
                    del data["mcpServers"]
                mcp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print("已移除 .mcp.json 中的 agent-engineering")
    except Exception:
        pass

# 清理 .codex/config.toml
config_toml = target / ".codex" / "config.toml"
if config_toml.exists():
    try:
        content = config_toml.read_text(encoding="utf-8")
        if "[mcp_servers.agent-engineering]" in content:
            # 删除 [mcp_servers.agent-engineering] 段及其 command 行
            lines = content.splitlines()
            keep = []
            in_our_section = False
            for ln in lines:
                if ln.strip().startswith("[mcp_servers.agent-engineering]"):
                    in_our_section = True
                    continue
                if in_our_section and ln.strip().startswith("["):
                    in_our_section = False
                if not in_our_section:
                    keep.append(ln)
            config_toml.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")
            print("已移除 .codex/config.toml 中的 agent-engineering")
    except Exception:
        pass

# 清理 settings.json 里可能残留的旧格式
settings = target / ".claude" / "settings.json"
if settings.exists():
    try:
        sdata = json.loads(settings.read_text(encoding="utf-8"))
        mcp = sdata.get("mcpServers", {})
        if "agent-engineering" in mcp:
            del mcp["agent-engineering"]
            if not mcp:
                del sdata["mcpServers"]
            settings.write_text(json.dumps(sdata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("已移除 settings.json 中的旧格式 mcpServers 条目")
    except Exception:
        pass
PYUNMCP
}

# ── 卸载 ────────────────────────────────────────────────────────
uninstall() {
    # 仅清理由本目标仓库创建、且仍指回本目标的 workspace 技能链接。
    if [ -f "$CODEX_ROOT_MARKER" ]; then
        saved_codex_root="$(sed -n '1p' "$CODEX_ROOT_MARKER")"
        case "$saved_codex_root" in
            /*)
                for tool in $KIT_SKILLS; do
                    link="$saved_codex_root/.agents/skills/$tool"
                    expected="$TARGET/.agents/skills/$tool"
                    if [ -L "$link" ] && [ "$(readlink "$link")" = "$expected" ]; then
                        rm -f "$link"
                        echo "已移除 Codex workspace 技能链接 $link"
                    elif [ -L "$link" ]; then
                        echo "漂移保护: $link 已改指其他目标，保留不删"
                    fi
                done
                ;;
            *) echo "拒绝处理可疑 Codex workspace 路径: $saved_codex_root" ;;
        esac
    fi
    MANIFEST="$TARGET/.repo-memory-kit/manifest"
    if [ -f "$MANIFEST" ]; then
        managed_spaced=""
        for mf in $MANAGED_FILES; do managed_spaced="$managed_spaced $mf"; done
        while read -r line; do
            case "$line" in
                *"  "*) h="${line%%  *}"; rel="${line#*  }" ;;
                *) continue ;;
            esac
            # 路径安全：拒绝绝对路径与含 .. 的路径（防篡改清单越界删除）
            case "$rel" in
                /*|*..*) echo "拒绝删除可疑路径: $rel（清单被篡改？）"; continue ;;
            esac
            # 范围限制：只删清单白名单内的路径
            case " $managed_spaced " in
                *" $rel "*) ;;
                *) echo "拒绝删除清单外路径: $rel"; continue ;;
            esac
            [ -f "$TARGET/$rel" ] || continue
            # 漂移保护：指纹不一致（被手工修改过）一律保留
            if [ "$(file_hash "$TARGET/$rel")" != "$h" ]; then
                echo "漂移保护: $rel 内容与清单指纹不一致（疑似被手工修改），保留不删"
                continue
            fi
            rm -f "$TARGET/$rel"
        done < "$MANIFEST"
        echo "已按清单移除 kit 管辖文件（通过路径校验与指纹比对）"
    else
        echo "警告: 无安装清单，仅移除托管区块与技能目录；kit 管辖文件（RULES.md 等）请手动删除"
    fi
    for f in CLAUDE.md AGENTS.md; do
        if [ -f "$TARGET/$f" ] && grep -qF "$SEC_START" "$TARGET/$f"; then
            sed -i.bak "/^${SEC_START}\$/,/^${SEC_END}\$/d" "$TARGET/$f"
            rm -f "$TARGET/$f.bak"
            echo "已移除 $f 中的托管区块"
        fi
    done
    if [ -f "$TARGET/.cbmignore" ] && grep -q "^${BLOCK_START}\$" "$TARGET/.cbmignore"; then
        sed -i.bak "/^${BLOCK_START}\$/,/^${BLOCK_END}\$/d" "$TARGET/.cbmignore"
        rm -f "$TARGET/.cbmignore.bak"
        echo "已移除 .cbmignore 托管区块"
    fi
    # 精细清理:manifest 逐文件处理已完成(含 bin 下工具的漂移保护);
    # 此处只删 manifest 本身和 marker,不 rm -rf 任何目录
    rm -f "$MANIFEST" 2>/dev/null || true
    rm -f "$TARGET/.repo-memory-kit/codex-workspace-root" 2>/dev/null || true
    # 尝试清理空目录(bin/ 在 manifest 处理后可能已空)
    rmdir "$TARGET/.repo-memory-kit/bin" 2>/dev/null || true
    rmdir "$TARGET/.repo-memory-kit" 2>/dev/null || true
    for skill_root in .claude/skills .agents/skills; do
        for tool in $KIT_SKILLS; do
            d="$skill_root/$tool"
            rmdir "$TARGET/$d" 2>/dev/null && echo "已移除空目录 $d"
        done
    done
    if command -v python3 >/dev/null 2>&1 && [ -f "$TARGET/.claude/settings.json" ]; then
    python3 - "$TARGET" <<'PYUNHOOK' || true
import json, sys
from pathlib import Path
sp = Path(sys.argv[1]) / ".claude" / "settings.json"
try:
    data = json.loads(sp.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
ss = data.get("hooks", {}).get("SessionStart", [])
# 精确删除:仅移除 command 包含 session-reminder 的 hook 条目,不动同组其他 hook
changed = False
for entry in ss:
    hooks_list = entry.get("hooks", [])
    remaining = [h for h in hooks_list if "session-reminder" not in str(h.get("command", ""))]
    if len(remaining) != len(hooks_list):
        if remaining:
            entry["hooks"] = remaining
        else:
            entry["hooks"] = []
        changed = True
# 清理空 entry(只有 hook 全被删的 entry)
ss = [e for e in ss if e.get("hooks")]
data["hooks"]["SessionStart"] = ss
if not ss:
    del data["hooks"]["SessionStart"]
    if not data["hooks"]:
        del data["hooks"]
if changed:
    sp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已移除会话提醒钩子（同组其他 hook 保留）")
PYUNHOOK
fi
echo "卸载完成。用户数据（README.md 索引、记忆条目、.anchors.json、settings.json 其余配置）未触碰。"
}

[ "$MODE" = "doctor" ] && { doctor; exit $?; }
[ "$MIGRATE_SPECIFY" = "0" ] || [ -d "$TARGET/.specify/specs" ] || {
    echo "错误: --migrate-specify 要求目标存在 .specify/specs" >&2; exit 1;
}
[ "$DRY_RUN" = "1" ] && { dry_run; exit 0; }
[ "$MODE" = "uninstall" ] && { uninstall; mcp_server_uninstall; exit 0; }
preflight_managed_paths

[ "$INSTALL_CODEBASE_MEMORY" = "0" ] || install_codebase_memory_runtime

# ── 漂移检测（覆盖前提示）────────────────────────────────────────────
if [ -f "$TARGET/.repo-memory-kit/manifest" ]; then
    while read -r line; do
        case "$line" in
            *"  "*) h="${line%%  *}"; rel="${line#*  }" ;;
            *) continue ;;
        esac
        if [ -f "$TARGET/$rel" ] && [ "$(file_hash "$TARGET/$rel")" != "$h" ]; then
            echo "漂移提示: kit 管辖文件 $rel 与上次安装版本不同（疑似被手工修改）——本次将覆盖为 kit 最新版；如需保留修改请提交到 kit 上游"
        fi
    done < "$TARGET/.repo-memory-kit/manifest"
fi

# ── 1. 记忆区目录与规则 ──────────────────────────────────────────────
mkdir -p "$TARGET/docs/memory/pitfalls" "$TARGET/docs/memory/playbooks" "$TARGET/docs/memory/decisions"

cp "$KIT_DIR/templates/memory-RULES.md" "$TARGET/docs/memory/RULES.md"
echo "已安装/更新 docs/memory/RULES.md（规则，kit 管辖）"

if [ -f "$TARGET/docs/memory/README.md" ]; then
    if grep -q "条目大小与时效" "$TARGET/docs/memory/README.md" 2>/dev/null; then
        echo "迁移提示: README.md 含旧版内嵌规则（规则已迁至 RULES.md）——请从 README 删除规则章节，仅保留索引与巡检记录"
    fi
else
    cp "$KIT_DIR/templates/memory-README.md" "$TARGET/docs/memory/README.md"
    echo "已创建 docs/memory/README.md（用户索引）"
fi

cp "$KIT_DIR/templates/pitfall-entry.md" "$TARGET/docs/memory/pitfalls/_TEMPLATE.md"
cp "$KIT_DIR/templates/decision-entry.md" "$TARGET/docs/memory/decisions/_TEMPLATE.md"
cp "$KIT_DIR/templates/playbook-entry.md" "$TARGET/docs/memory/playbooks/_TEMPLATE.md"
cp "$KIT_DIR/templates/profile.md" "$TARGET/docs/memory/_PROFILE_TEMPLATE.md"
echo "已刷新条目/决定/流程/画像模板"

# ── 2. 双端技能（仓库级，两端同一份 SKILL.md）────────────────────────
# 先清理已从 kit 退役的受管技能（如技能合并改名）：从旧安装清单推导
# "清单里有、当前技能清单没有"的目录，仅当内容指纹匹配 kit 当前/历史
# 版本才删除；被手工改过的保留并提示。
if [ -f "$TARGET/.repo-memory-kit/manifest" ]; then
    retired_skills=""
    while read -r manifest_line; do
        case "$manifest_line" in
            *"  "*) rel="${manifest_line#*  }" ;;
            *) continue ;;
        esac
        case "$rel" in
            .claude/skills/*/SKILL.md) retired_tool="${rel#.claude/skills/}" ;;
            .agents/skills/*/SKILL.md) retired_tool="${rel#.agents/skills/}" ;;
            *) continue ;;
        esac
        retired_tool="${retired_tool%/SKILL.md}"
        case " $KIT_SKILLS " in
            *" $retired_tool "*) continue ;;
        esac
        case " $retired_skills " in
            *" $retired_tool "*) continue ;;
        esac
        retired_skills="$retired_skills $retired_tool"
    done < "$TARGET/.repo-memory-kit/manifest"
    for retired_tool in $retired_skills; do
        for skill_base in .claude .agents; do
            retired_dir="$TARGET/$skill_base/skills/$retired_tool"
            [ -e "$retired_dir" ] || continue
            if content_is_kit_artifact "$retired_dir/SKILL.md" "skills/$retired_tool/SKILL.md"; then
                rm -rf "$retired_dir"
                echo "已清理退役技能 $skill_base/skills/$retired_tool（kit 已合并/移除该技能）"
            else
                echo "提示: $skill_base/skills/$retired_tool 已不在 kit 技能清单且内容与 kit 历史版本不一致（疑似手工定制），保留不动"
            fi
        done
        if [ -n "$CODEX_ROOT" ] && [ "$CODEX_ROOT" != "$TARGET" ]; then
            retired_link="$CODEX_ROOT/.agents/skills/$retired_tool"
            if [ -L "$retired_link" ] && [ "$(readlink "$retired_link" 2>/dev/null || true)" = "$TARGET/.agents/skills/$retired_tool" ]; then
                rm -f "$retired_link"
                echo "已清理 Codex workspace 退役技能链接 $retired_link"
            fi
        fi
    done
fi

for tool in $KIT_SKILLS; do
    for _root in .claude/skills .agents/skills; do
        _dst="$TARGET/$_root/$tool/SKILL.md"
        if [ -f "$_dst" ]; then
            # 首次安装(无 manifest)时同名 Skill 已存在:
            # 内容是 kit 产物→允许覆盖(更新场景)
            # 内容不是 kit 产物→拒绝,提示用户处理
            if ! content_is_kit_artifact "$_dst" "skills/$tool/SKILL.md" || [ ! -f "$TARGET/.repo-memory-kit/manifest" ]; then
                echo "错误: $_root/$tool/SKILL.md 已存在且非 kit 产物,拒绝覆盖"
                echo "  处理方式: 备份后删除,或将内容合并到 kit 版本,再重新安装"
                exit 1
            fi
        fi
    done
    mkdir -p "$TARGET/.claude/skills/$tool" "$TARGET/.agents/skills/$tool"
    cp "$KIT_DIR/skills/$tool/SKILL.md" "$TARGET/.claude/skills/$tool/SKILL.md"
    cp "$KIT_DIR/skills/$tool/SKILL.md" "$TARGET/.agents/skills/$tool/SKILL.md"
done
echo "已安装/更新双端技能（.claude/skills + .agents/skills，仓库级）"

# Codex 只从当前工作目录向上扫描 .agents/skills。若实际 workspace 在目标仓库父级，
# 通过 target-bound 符号链接暴露同一份受管技能，避免复制后版本漂移。
if [ -n "$CODEX_ROOT" ] && [ "$CODEX_ROOT" != "$TARGET" ]; then
    mkdir -p "$CODEX_ROOT/.agents/skills" "$TARGET/.repo-memory-kit"
    for tool in $KIT_SKILLS; do
        src="$TARGET/.agents/skills/$tool"
        link="$CODEX_ROOT/.agents/skills/$tool"
        if [ -L "$link" ]; then
            [ "$(readlink "$link")" = "$src" ] || {
                echo "错误: $link 已指向其他目标，拒绝覆盖"; exit 1;
            }
        elif [ -e "$link" ]; then
            echo "错误: $link 已存在且不是符号链接，拒绝覆盖"; exit 1
        else
            ln -s "$src" "$link"
        fi
    done
    printf '%s\n' "$CODEX_ROOT" > "$CODEX_ROOT_MARKER"
    echo "已向 Codex workspace 暴露技能：$KIT_SKILLS"
fi

# 旧版全局 Codex 技能：仅当内容指纹匹配 kit 当前/历史版本才清理
for tool in memory-check memory-capture repo-delivery; do
    d="$HOME/.codex/skills/$tool"; old="$d/SKILL.md"
    if [ -e "$d" ]; then
        if content_is_kit_artifact "$old" "skills/$tool/SKILL.md" \
           || content_is_kit_artifact "$old" "codex/skills/$tool/SKILL.md"; then
            rm -rf "$d"
            echo "已清理旧版全局 Codex 技能 $tool（内容指纹匹配 kit 产物）"
        else
            echo "提示: ~/.codex/skills/$tool 内容与 kit 版本不一致（非本 kit 产物或被手工改过），保留不动"
        fi
    fi
done

# 旧版 .claude/commands 命令：仅当内容指纹匹配 kit 历史版本才清理
for tool in memory-check memory-capture; do
    f="$TARGET/.claude/commands/$tool.md"
    if [ -f "$f" ]; then
        if content_is_kit_artifact "$f" "commands/$tool.md"; then
            rm -f "$f"
            echo "已清理旧版命令 .claude/commands/$tool.md（内容指纹匹配 kit 历史版本）"
        else
            echo "提示: .claude/commands/$tool.md 与 kit 历史版本不一致（疑似手工定制），保留——请确认后手动删除，避免与新技能双入口"
        fi
    fi
done

# ── 3. 校验器随仓库安装 ─────────────────────────────────────────────
mkdir -p "$TARGET/.repo-memory-kit/bin"
cp "$KIT_DIR/validate-memory.sh" "$TARGET/.repo-memory-kit/bin/validate-memory.sh"
cp "$KIT_DIR/memory-build" "$TARGET/.repo-memory-kit/bin/memory-build"
cp "$KIT_DIR/spec-migrate" "$TARGET/.repo-memory-kit/bin/spec-migrate"
cp "$KIT_DIR/memory-recall" "$TARGET/.repo-memory-kit/bin/memory-recall"
cp "$KIT_DIR/session-reminder" "$TARGET/.repo-memory-kit/bin/session-reminder"
cp "$KIT_DIR/domain-check" "$TARGET/.repo-memory-kit/bin/domain-check"
cp "$KIT_DIR/agent-engineering-mcp" "$TARGET/.repo-memory-kit/bin/agent-engineering-mcp"
chmod +x "$TARGET/.repo-memory-kit/bin/validate-memory.sh" "$TARGET/.repo-memory-kit/bin/memory-build" "$TARGET/.repo-memory-kit/bin/spec-migrate" "$TARGET/.repo-memory-kit/bin/memory-recall" "$TARGET/.repo-memory-kit/bin/domain-check" "$TARGET/.repo-memory-kit/bin/agent-engineering-mcp" "$TARGET/.repo-memory-kit/bin/session-reminder"
echo "已安装校验器、生成器、Spec 迁移器、语义检索器与域地图守卫 .repo-memory-kit/bin/{validate-memory.sh,memory-build,spec-migrate,memory-recall,domain-check,session-reminder,agent-engineering-mcp}"

# MCP 服务器注册(合并式写入 .claude/settings.json,同 hook 逻辑)
mcp_server_install() {
    # Claude Code:项目级 MCP 写 .mcp.json(不是 settings.json 里的 mcpServers)
    # Codex:写 .codex/config.toml
    command -v python3 >/dev/null 2>&1 || { echo "提示: 无 python3，跳过 MCP 注册"; return 0; }
    python3 - "$TARGET" <<'PYMCP'
import json, os, sys
from pathlib import Path
target = Path(sys.argv[1])

# ── Claude Code: .mcp.json ──
mcp_json = target / ".mcp.json"
data = {}
if mcp_json.exists():
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
    except Exception:
        print("提示: .mcp.json 不是合法 JSON，跳过 MCP 注册"); sys.exit(0)
servers = data.setdefault("mcpServers", {})
server_cmd = str(target / ".repo-memory-kit" / "bin" / "agent-engineering-mcp")
ours = {"command": server_cmd, "args": [], "env": {}}
if "agent-engineering" in servers and servers["agent-engineering"] != ours:
    # 用户已有同名 MCP:不覆盖,提示
    print(f"提示: .mcp.json 已有 agent-engineering 条目(非 kit 产物)，跳过注册")
else:
    servers["agent-engineering"] = ours
    mcp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已注册 Claude Code MCP: .mcp.json → agent-engineering")

# ── Codex: .codex/config.toml ──
codex_dir = target / ".codex"
codex_dir.mkdir(parents=True, exist_ok=True)
config_toml = codex_dir / "config.toml"
server_name = "agent-engineering"
entry = f'\n[mcp_servers.{server_name}]\ncommand = "{server_cmd}"\n'
if config_toml.exists():
    content = config_toml.read_text(encoding="utf-8")
    if f"[mcp_servers.{server_name}]" not in content:
        config_toml.write_text(content.rstrip() + "\n" + entry, encoding="utf-8")
        print(f"已注册 Codex MCP: .codex/config.toml → {server_name}")
    # 已存在则幂等跳过
else:
    config_toml.write_text(entry.lstrip(), encoding="utf-8")
    print(f"已创建 Codex MCP 配置: .codex/config.toml → {server_name}")

# ── 清理:从 settings.json 里移除错误的 mcpServers(如果之前写入了) ──
settings = target / ".claude" / "settings.json"
if settings.exists():
    try:
        sdata = json.loads(settings.read_text(encoding="utf-8"))
        mcp = sdata.get("mcpServers", {})
        if "agent-engineering" in mcp:
            del mcp["agent-engineering"]
            if not mcp:
                del sdata["mcpServers"]
            settings.write_text(json.dumps(sdata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("已从 settings.json 移除错误的 mcpServers 条目(正确位置是 .mcp.json)")
    except Exception:
        pass
PYMCP
}

# 卸载时清理 MCP 服务器注册
# 会话提醒钩子：合并写入项目 .claude/settings.json（需 python3；失败只降级不阻断）
# shellcheck disable=SC2016  # CLAUDE_PROJECT_DIR 需在钩子运行时由 Claude Code 展开,此处保留字面量
RECALL_HOOK_CMD='"$CLAUDE_PROJECT_DIR/.repo-memory-kit/bin/session-reminder"'
recall_hook_install() {
    # 安全:settings.json 为符号链接时拒绝写入(防越界修改)
    if [ -L "$TARGET/.claude/settings.json" ]; then
        echo "提示: .claude/settings.json 是符号链接，跳过钩子注册（防越界写入）"
        return 0
    fi
    command -v python3 >/dev/null 2>&1 || { echo "提示: 无 python3，跳过会话钩子；语义检索路由由 CLAUDE.md 承担"; return 0; }
    python3 - "$TARGET" "$RECALL_HOOK_CMD" <<'PYHOOK' || echo "提示: 会话钩子写入失败；语义检索路由由 CLAUDE.md 承担"
import json, sys
from pathlib import Path
target, cmd = Path(sys.argv[1]), sys.argv[2]
sp = target / ".claude" / "settings.json"
sp.parent.mkdir(parents=True, exist_ok=True)
data = {}
if sp.exists():
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        print("提示: .claude/settings.json 不是合法 JSON，跳过钩子注册"); sys.exit(0)
if not isinstance(data, dict):
    print("提示: .claude/settings.json 结构异常，跳过钩子注册"); sys.exit(0)
ss = data.setdefault("hooks", {}).setdefault("SessionStart", [])
for entry in ss:
    for h in entry.get("hooks", []):
        if ".repo-memory-kit" in str(h.get("command", "")):
            h["command"] = cmd
            sp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            sys.exit(0)
ss.append({"matcher": "startup|resume", "hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
sp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("已注册会话提醒钩子 .claude/settings.json（SessionStart，合并式写入）")
PYHOOK
}
recall_hook_install
mcp_server_install

# 默认只发现和报告已有 Spec Kit 文档；显式选项才执行保留原文的增量迁移。
if [ -d "$TARGET/.specify/specs" ]; then
    echo "发现 Spec Kit: $TARGET/.specify/specs"
    if [ "$MIGRATE_SPECIFY" = "1" ]; then
        run_spec_migrate --apply "$TARGET/.repo-memory-kit/bin/spec-migrate" "$TARGET"
    else
        run_spec_migrate --check "$TARGET/.repo-memory-kit/bin/spec-migrate" "$TARGET"
        echo "提示: 当前仅检查未修改文档；确认后可使用 --migrate-specify[=FEATURE,...] 增量迁移"
    fi
fi

# ── 3b. .gitignore 补充语义索引(如果项目有 .gitignore) ─────────────
if [ -f "$TARGET/.gitignore" ]; then
    if ! grep -qF ".repo-memory-kit/zvec" "$TARGET/.gitignore"; then
        printf '\n# agent-engineering-kit: 语义索引派生数据(可随时重建)\n.repo-memory-kit/zvec/\n' >> "$TARGET/.gitignore"
        echo "已补充 .gitignore: .repo-memory-kit/zvec/"
    fi
fi

# ── 4. .cbmignore 托管区块（整体校验与替换）───────────────────────────
CBM="$TARGET/.cbmignore"
if [ -f "$CBM" ]; then
    if grep -q "^${BLOCK_START}\$" "$CBM"; then
        sed -i.bak "/^${BLOCK_START}\$/,/^${BLOCK_END}\$/d" "$CBM"
    elif grep -q '^!docs/memory/' "$CBM" || grep -q '^!docs/$' "$CBM"; then
        sed -i.bak '/^!docs\/$/d;/^docs\/\*$/d;/^!docs\/memory\/$/d' "$CBM"
        echo "已替换旧版 .cbmignore 规则为托管区块"
    fi
    rm -f "$CBM.bak"
fi
{
    echo "$BLOCK_START"
    echo "!docs/"
    echo "docs/*"
    echo "!docs/memory/"
    echo "$BLOCK_END"
} >> "$CBM"
echo "已写入 .cbmignore 托管区块（图谱索引否定规则）"

# ── 5. CLAUDE.md / AGENTS.md 托管区块(标记完整性预检) ──
for _md in "$TARGET/CLAUDE.md" "$TARGET/AGENTS.md"; do
    check_block_markers "$_md" "$SEC_START" "$SEC_END" || exit 1
done
check_block_markers "$TARGET/.cbmignore" "$BLOCK_START" "$BLOCK_END" || exit 1

# CLAUDE.md / AGENTS.md 托管区块 ─────────────────────────────────
section_body() { # 提取「项目记忆」段落（含标题，到下一个二级标题前）
    awk '/^## 项目记忆（坑与流程）/{p=1} p{if(/^## / && $0 !~ /^## 项目记忆（坑与流程）/) exit; print}' "$1"
}

remove_legacy_section() {
    awk '/^## 项目记忆（坑与流程）/{s=1; next} s && /^## /{s=0} !s{print}' "$1" > "$1.rmk-tmp"
    mv "$1.rmk-tmp" "$1"
}

install_section() { # $1=目标文件名 $2=kit 模板名
    f="$TARGET/$1"; tpl="$KIT_DIR/templates/$2"
    [ -f "$f" ] || { echo "跳过 $1（不存在）"; return; }
    if grep -qF "$SEC_START" "$f" 2>/dev/null; then
        sed -i.bak "/^${SEC_START}\$/,/^${SEC_END}\$/d" "$f"
        rm -f "$f.bak"
    elif grep -q "^## 项目记忆（坑与流程）" "$f" 2>/dev/null; then
        # 两端统一"去空行后哈希"口径——段落从目标文件提取时空行与模板原文不同，
        # 历史模板同样归一化后再比对，否则历史版本永远匹配不上（被误判为定制内容）
        body_h="$(section_body "$f" | sed '/^[[:space:]]*$/d' | hash_of)"
        tpl_h="$(sed '/^[[:space:]]*$/d' < "$tpl" | hash_of)"
        if [ "$body_h" = "$tpl_h" ] || kit_path_hashes "templates/$2" 1 | grep -q "^$body_h$"; then
            remove_legacy_section "$f"
            echo "已自动迁移 $1 的旧版「项目记忆」段落 → 托管区块"
        else
            echo "提示: $1 存在旧版「项目记忆」段落（内容与 kit 模板不一致，疑似手工定制）——未改动。新版段落内容如下，请自行比对合并："
            sed 's/^/    | /' "$tpl"
            return
        fi
    fi
    {
        printf '\n%s\n' "$SEC_START"
        cat "$tpl"
        printf '%s\n' "$SEC_END"
    } >> "$f"
    echo "已安装/更新 $1「项目记忆」托管区块"
}

install_section "CLAUDE.md" "claude-md-section.md"
install_section "AGENTS.md" "agents-md-section.md"

# ── 6. 安装清单（版本 + 受管文件指纹，供安全升级/卸载/漂移检测）──────
KIT_VER="$(git -C "$KIT_DIR" describe --tags --always 2>/dev/null || echo unknown)"
{
    echo "kit_version=$KIT_VER"
    echo "anchors_schema=2"
    for rel in $MANAGED_FILES; do
        [ -f "$TARGET/$rel" ] && echo "$(file_hash "$TARGET/$rel")  $rel"
    done
} > "$TARGET/.repo-memory-kit/manifest"
echo "已写入安装清单 .repo-memory-kit/manifest（kit_version=$KIT_VER）"

# ── 7. 完成提示 ─────────────────────────────────────────────────────
echo
if [ "$MODE" = "update" ]; then
    echo "更新完成。kit 管辖文件已刷新；README 如有旧版规则请按迁移提示手动清理。"
elif [ "$MODE" = "repair" ]; then
    echo "修复完成。kit 受管文件、托管区块和技能链接已恢复为当前版本。"
else
    echo "完成。下一步：把最近一个 bug 写成第一条 pitfall（复制 _TEMPLATE.md，按日期命名）；README 索引由 memory-build 自动生成，禁止手改。"
fi
echo
echo "提醒: 巡检默认强制依赖结构化代码检索工具（代码知识图谱类，如 codebase-memory MCP），未安装请先配置——grep 无法可靠核验记忆条目。"
