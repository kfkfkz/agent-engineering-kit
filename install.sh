#!/bin/sh
# agent-engineering-kit 接入/更新/卸载：把统一 AI Agent 工程工作流安装到目标仓库
# 用法:
#   ./install.sh /path/to/your-project            # 首次接入（幂等）
#   ./install.sh --update /path/to/your-project    # 更新 kit 管辖文件
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
INSTALL_CODEBASE_MEMORY=0
CODEX_ROOT=""
TARGET=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --update) MODE=update ;;
        --uninstall) MODE=uninstall ;;
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

[ -n "$TARGET" ] || { echo "用法: ./install.sh [--update|--uninstall] [--with-codebase-memory] [--codex-root /workspace] /path/to/your-project"; exit 1; }
[ -d "$TARGET" ] || { echo "错误: 目标目录不存在: $TARGET"; exit 1; }
[ "$MODE" != "uninstall" ] || [ "$INSTALL_CODEBASE_MEMORY" = "0" ] || {
    echo "错误: --with-codebase-memory 不能与 --uninstall 同时使用"; exit 1;
}
TARGET="$(cd "$TARGET" && pwd)"

CODEX_ROOT_MARKER="$TARGET/.repo-memory-kit/codex-workspace-root"
if [ -z "$CODEX_ROOT" ] && [ -f "$CODEX_ROOT_MARKER" ]; then
    CODEX_ROOT="$(sed -n '1p' "$CODEX_ROOT_MARKER")"
fi
if [ -n "$CODEX_ROOT" ]; then
    [ -d "$CODEX_ROOT" ] || { echo "错误: Codex workspace 不存在: $CODEX_ROOT"; exit 1; }
    CODEX_ROOT="$(cd "$CODEX_ROOT" && pwd)"
    if [ "$MODE" != "uninstall" ] && [ "$CODEX_ROOT" != "$TARGET" ]; then
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

KIT_SKILLS="memory-check memory-capture repo-delivery codebase-memory systematic-debugging tdd"

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
.agents/skills/memory-check/SKILL.md
.agents/skills/memory-capture/SKILL.md
.agents/skills/repo-delivery/SKILL.md
.agents/skills/codebase-memory/SKILL.md
.agents/skills/systematic-debugging/SKILL.md
.agents/skills/tdd/SKILL.md
.repo-memory-kit/bin/validate-memory.sh
.repo-memory-kit/bin/memory-build
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

# ── 卸载 ────────────────────────────────────────────────────────────
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
    rm -rf "$TARGET/.repo-memory-kit"
    for skill_root in .claude/skills .agents/skills; do
        for tool in $KIT_SKILLS; do
            d="$skill_root/$tool"
            rmdir "$TARGET/$d" 2>/dev/null && echo "已移除空目录 $d"
        done
    done
    echo "卸载完成。用户数据（README.md 索引、记忆条目、.anchors.json）未触碰。"
}

[ "$MODE" = "uninstall" ] && { uninstall; exit 0; }

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
for tool in $KIT_SKILLS; do
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
chmod +x "$TARGET/.repo-memory-kit/bin/validate-memory.sh"
echo "已安装校验器与生成器 .repo-memory-kit/bin/{validate-memory.sh,memory-build}"

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

# ── 5. CLAUDE.md / AGENTS.md 托管区块 ─────────────────────────────────
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
else
    echo "完成。下一步：把最近一个 bug 写成第一条 pitfall（复制 _TEMPLATE.md，按日期命名），并在 README.md 索引表中登记。"
fi
echo
echo "提醒: 巡检默认强制依赖结构化代码检索工具（代码知识图谱类，如 codebase-memory MCP），未安装请先配置——grep 无法可靠核验记忆条目。"
