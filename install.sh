#!/bin/sh
# repo-memory-kit 一键接入：把项目记忆系统安装到目标仓库
# 用法: ./install.sh /path/to/your-project

set -e

TARGET="${1:?用法: ./install.sh /path/to/your-project}"
[ -d "$TARGET" ] || { echo "错误: 目标目录不存在: $TARGET"; exit 1; }

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. 记忆区目录 + README
mkdir -p "$TARGET/docs/memory/pitfalls" "$TARGET/docs/memory/playbooks"
if [ -f "$TARGET/docs/memory/README.md" ]; then
    echo "跳过 docs/memory/README.md（已存在，请手动比对 templates/memory-README.md）"
else
    cp "$KIT_DIR/templates/memory-README.md" "$TARGET/docs/memory/README.md"
    echo "已写入 docs/memory/README.md（含条目模板与全部规则）"
fi

# 2. 巡检命令（幂等：覆盖为最新版）
mkdir -p "$TARGET/.claude/commands"
cp "$KIT_DIR/commands/memory-check.md" "$TARGET/.claude/commands/memory-check.md"
echo "已安装 /memory-check 巡检命令"

# 3. CLAUDE.md / AGENTS.md 追加指引段落（幂等：已有段落则跳过）
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

# 4. 条目模板附赠到 pitfalls 目录（不占索引，用前删除）
if [ ! -f "$TARGET/docs/memory/pitfalls/_TEMPLATE.md" ]; then
    cp "$KIT_DIR/templates/pitfall-entry.md" "$TARGET/docs/memory/pitfalls/_TEMPLATE.md"
    echo "已放置条目模板 docs/memory/pitfalls/_TEMPLATE.md"
fi

echo
echo "完成。下一步：把最近一个 bug 写成第一条 pitfall（复制 _TEMPLATE.md，按日期命名），并在 docs/memory/README.md 索引表中登记。"
