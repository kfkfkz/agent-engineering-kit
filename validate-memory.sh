#!/bin/sh
# 校验 docs/memory 记忆区：结构完整性 + 委托 memory-build --check
# （frontmatter 格式 / 本地索引一致性 / .anchors.json 一致性 / 关系引用，均在 memory-build 中实现）
# 用法: ./validate-memory.sh /path/to/your-project
# 依赖 python3（缺解析器时明确失败，不做"假绿"跳过）

TARGET="${1:?用法: ./validate-memory.sh /path/to/your-project}"
MEM="$TARGET/docs/memory"
fail=0

err() { echo "✗ $1"; fail=1; }
ok()   { echo "✓ $1"; }

if [ -f "$MEM/RULES.md" ]; then ok "RULES.md（规则）存在"; else err "缺 docs/memory/RULES.md（运行 install.sh）"; fi
if [ -f "$MEM/README.md" ]; then ok "README.md（用户索引/笔记，纯 seed）存在"; else err "缺 docs/memory/README.md"; fi
for f in _PITFALL_TEMPLATE.md _DECISION_TEMPLATE.md _PLAYBOOK_TEMPLATE.md _PROFILE_TEMPLATE.md; do
    if [ -f "$MEM/$f" ]; then ok "模板 $f 存在"; else err "缺 docs/memory/$f"; fi
done
if ls "$MEM" >/dev/null 2>&1; then
    n_mod=$(find "$MEM" -mindepth 1 -maxdepth 1 -type d \
        ! -name 'pitfalls' ! -name 'decisions' ! -name 'playbooks' ! -name '_*' ! -name '.*' | wc -l)
    if [ "$n_mod" -gt 0 ]; then
        ok "模块目录存在（$n_mod 个；条目按模块分目录，类型在 frontmatter）"
    elif [ -d "$MEM/pitfalls" ] || [ -d "$MEM/decisions" ] || [ -d "$MEM/playbooks" ]; then
        ok "（v3 存量类型目录可读；建议运行 memory-build --migrate-to-modules 迁入模块目录）"
    else
        ok "尚无条目目录（暂无记忆条目，属正常；首条入库时按模块建目录）"
    fi
fi

MB="$TARGET/.repo-memory-kit/bin/memory-build"
if [ ! -f "$MB" ]; then
    err "缺 .repo-memory-kit/bin/memory-build（重新运行 install.sh 升级）"
elif ! command -v python3 >/dev/null 2>&1; then
    err "无 python3——条目/frontmatter/索引/锚点校验无法执行（不假绿），请安装 python3"
elif python3 "$MB" --check "$TARGET"; then
    ok "条目 frontmatter、本地索引、.anchors.json 一致性通过（memory-build --check）"
else
    err "memory-build --check 未通过（见上方信息）"
fi

echo
if [ "$fail" = 0 ]; then
    echo "全部通过"
else
    echo "存在问题"
    exit 1
fi
