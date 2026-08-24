#!/bin/sh
# repo-memory-kit 接入/更新：把项目记忆系统安装到目标仓库
# 用法:
#   ./install.sh /path/to/your-project          # 首次接入（幂等，可重复运行）
#   ./install.sh --update /path/to/your-project  # 更新 kit 管辖的文件（巡检命令/技能）
#                                                # docs/memory/README.md 属用户所有，不覆盖，仅提示差异

set -e

UPDATE=0
if [ "$1" = "--update" ]; then UPDATE=1; shift; fi

TARGET="${1:?用法: ./install.sh [--update] /path/to/your-project}"
[ -d "$TARGET" ] || { echo "错误: 目标目录不存在: $TARGET"; exit 1; }

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. 记忆区目录 + README（用户所有：更新模式不覆盖，只提示差异）
mkdir -p "$TARGET/docs/memory/pitfalls" "$TARGET/docs/memory/playbooks"
if [ "$UPDATE" = "1" ]; then
    if ! cmp -s "$KIT_DIR/templates/memory-README.md" "$TARGET/docs/memory/README.md" 2>/dev/null; then
        echo "提示: templates/memory-README.md 与目标仓库版本有差异（README 含你的索引与巡检记录，不自动覆盖，请手动比对合并规则部分）"
    fi
elif [ -f "$TARGET/docs/memory/README.md" ]; then
    echo "跳过 docs/memory/README.md（已存在，请手动比对 templates/memory-README.md）"
else
    cp "$KIT_DIR/templates/memory-README.md" "$TARGET/docs/memory/README.md"
    echo "已写入 docs/memory/README.md（含条目模板与全部规则）"
fi

# 2. Claude Code 命令（kit 管辖：始终刷新为最新版）
mkdir -p "$TARGET/.claude/commands"
cp "$KIT_DIR/commands/memory-check.md" "$TARGET/.claude/commands/memory-check.md"
cp "$KIT_DIR/commands/memory-capture.md" "$TARGET/.claude/commands/memory-capture.md"
echo "已安装/更新 Claude Code 命令 /memory-check、/memory-capture"

# 3. Codex 技能（本机 ~/.codex/skills 存在时安装，kit 管辖：始终刷新）
if [ -d "$HOME/.codex/skills" ]; then
    mkdir -p "$HOME/.codex/skills/memory-check" "$HOME/.codex/skills/memory-capture"
    cp "$KIT_DIR/codex/skills/memory-check/SKILL.md" "$HOME/.codex/skills/memory-check/SKILL.md"
    cp "$KIT_DIR/codex/skills/memory-capture/SKILL.md" "$HOME/.codex/skills/memory-capture/SKILL.md"
    echo "已安装/更新 Codex 技能 memory-check、memory-capture（~/.codex/skills/）"
else
    echo "跳过 Codex 技能（~/.codex/skills 不存在）"
fi

# 4. CLAUDE.md / AGENTS.md 追加指引段落（幂等：已有段落则跳过）
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

# 5. 条目模板附赠到 pitfalls 目录（kit 管辖：始终刷新）
cp "$KIT_DIR/templates/pitfall-entry.md" "$TARGET/docs/memory/pitfalls/_TEMPLATE.md"
echo "已放置条目模板 docs/memory/pitfalls/_TEMPLATE.md"

echo
if [ "$UPDATE" = "1" ]; then
    echo "更新完成。kit 管辖文件已刷新；README 规则如有差异请手动合并。"
else
    echo "完成。下一步：把最近一个 bug 写成第一条 pitfall（复制 _TEMPLATE.md，按日期命名），并在 docs/memory/README.md 索引表中登记。"
fi
echo
echo "提醒: 巡检默认强制依赖结构化代码检索工具（代码知识图谱类，如 codebase-memory MCP），未安装请先配置——grep 无法可靠核验记忆条目。"
