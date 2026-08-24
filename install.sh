#!/bin/sh
# repo-memory-kit 接入/更新：把项目记忆系统安装到目标仓库
# 用法:
#   ./install.sh /path/to/your-project          # 首次接入（幂等，可重复运行）
#   ./install.sh --update /path/to/your-project  # 更新 kit 管辖文件（规则/技能/模板）
#
# 文件所有权约定：
#   kit 管辖（始终刷新）: RULES.md、双端技能、条目/画像模板、.cbmignore 托管区块
#   用户所有（绝不覆盖）: README.md（索引）、CLAUDE.md/AGENTS.md 段落、全部记忆条目、.anchors.json

set -e

UPDATE=0
if [ "$1" = "--update" ]; then UPDATE=1; shift; fi

TARGET="${1:?用法: ./install.sh [--update] /path/to/your-project}"
[ -d "$TARGET" ] || { echo "错误: 目标目录不存在: $TARGET"; exit 1; }

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 1. 记忆区目录与规则 ──────────────────────────────────────────────
mkdir -p "$TARGET/docs/memory/pitfalls" "$TARGET/docs/memory/playbooks"

# RULES.md（kit 管辖：始终刷新）
cp "$KIT_DIR/templates/memory-RULES.md" "$TARGET/docs/memory/RULES.md"
echo "已安装/更新 docs/memory/RULES.md（规则，kit 管辖）"

# README.md（用户索引：仅首次创建，绝不覆盖）
if [ -f "$TARGET/docs/memory/README.md" ]; then
    if grep -q "条目大小与时效" "$TARGET/docs/memory/README.md" 2>/dev/null; then
        echo "迁移提示: README.md 含旧版内嵌规则（规则已迁至 RULES.md）——请从 README 删除规则章节，仅保留索引与巡检记录"
    fi
else
    cp "$KIT_DIR/templates/memory-README.md" "$TARGET/docs/memory/README.md"
    echo "已创建 docs/memory/README.md（用户索引）"
fi

# ── 2. 条目/画像模板（kit 管辖：始终刷新）────────────────────────────
cp "$KIT_DIR/templates/pitfall-entry.md" "$TARGET/docs/memory/pitfalls/_TEMPLATE.md"
cp "$KIT_DIR/templates/profile.md" "$TARGET/docs/memory/_PROFILE_TEMPLATE.md"
echo "已刷新条目模板 _TEMPLATE.md 与画像模板 _PROFILE_TEMPLATE.md"

# ── 3. 双端技能（kit 管辖：仓库级安装，两端同一份 SKILL.md）──────────
for tool in memory-check memory-capture; do
    mkdir -p "$TARGET/.claude/skills/$tool" "$TARGET/.agents/skills/$tool"
    cp "$KIT_DIR/skills/$tool/SKILL.md" "$TARGET/.claude/skills/$tool/SKILL.md"
    cp "$KIT_DIR/skills/$tool/SKILL.md" "$TARGET/.agents/skills/$tool/SKILL.md"
done
echo "已安装/更新双端技能（.claude/skills + .agents/skills，仓库级，随仓库版本化）"

# 迁移清理 A：旧版全局 Codex 技能（内容与当前版本一致时移除，被手工改过则提示）
for tool in memory-check memory-capture; do
    old="$HOME/.codex/skills/$tool/SKILL.md"
    if [ -f "$old" ]; then
        if cmp -s "$old" "$KIT_DIR/skills/$tool/SKILL.md" || grep -q "docs/memory" "$old"; then
            rm -rf "$HOME/.codex/skills/$tool"
            echo "已清理旧版全局 Codex 技能 $tool（现改为仓库级 .agents/skills）"
        else
            echo "提示: ~/.codex/skills/$tool 内容非本 kit 产物，未清理，请自行确认"
        fi
    fi
done

# 迁移清理 B：旧版 .claude/commands 命令文件（已由 .claude/skills 取代）
for tool in memory-check memory-capture; do
    if [ -f "$TARGET/.claude/commands/$tool.md" ]; then
        rm -f "$TARGET/.claude/commands/$tool.md"
        echo "已清理旧版命令 .claude/commands/$tool.md（现为 .claude/skills）"
    fi
done

# ── 4. .cbmignore 托管区块（整体校验与替换，防部分存在/重复/顺序问题）──
CBM="$TARGET/.cbmignore"
BLOCK_START="# repo-memory-kit:start"
BLOCK_END="# repo-memory-kit:end"
if [ -f "$CBM" ]; then
    if grep -q "^${BLOCK_START}\$" "$CBM"; then
        # 已有托管区块：删除后重新追加（幂等）
        sed -i.bak "/^${BLOCK_START}\$/,/^${BLOCK_END}\$/d" "$CBM"
    elif grep -q '^!docs/memory/' "$CBM" || grep -q '^!docs/$' "$CBM"; then
        # 旧版裸规则（可能残缺）：全部清除
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
echo "已写入 .cbmignore 托管区块（图谱索引否定规则：逐层否定规避父子裁剪）"

# ── 5. CLAUDE.md / AGENTS.md 追加指引段落（幂等：已有段落则跳过）──────
append_section() {
    file="$1"; section="$2"; marker="$3"
    if [ ! -f "$TARGET/$file" ]; then
        echo "跳过 $file（不存在）"
        return
    fi
    if grep -q "$marker" "$TARGET/$file"; then
        echo "跳过 $file（已含项目记忆段落）"
    else
        printf '\n%s\n' "$(cat "$KIT_DIR/templates/$section")" >> "$TARGET/$file"
        echo "已向 $file 追加「项目记忆」段落"
    fi
}

append_section "CLAUDE.md" "claude-md-section.md" "项目记忆（坑与流程）"
append_section "AGENTS.md" "agents-md-section.md" "项目记忆（坑与流程）"

# ── 6. 完成提示 ─────────────────────────────────────────────────────
echo
if [ "$UPDATE" = "1" ]; then
    echo "更新完成。kit 管辖文件已刷新；README 如有旧版规则请按迁移提示手动清理。"
else
    echo "完成。下一步：把最近一个 bug 写成第一条 pitfall（复制 _TEMPLATE.md，按日期命名），并在 README.md 索引表中登记。"
fi
echo
echo "提醒: 巡检默认强制依赖结构化代码检索工具（代码知识图谱类，如 codebase-memory MCP），未安装请先配置——grep 无法可靠核验记忆条目。"
