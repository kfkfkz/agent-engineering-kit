#!/bin/sh
# 校验 docs/memory 记忆区：结构完整性 + 委托 memory-build --check
# （frontmatter 格式 / README 索引一致性 / .anchors.json 一致性 / 关系引用，均在 memory-build 中实现）
# 用法: ./validate-memory.sh /path/to/your-project
# 依赖 python3（缺解析器时明确失败，不做"假绿"跳过）

TARGET="${1:?用法: ./validate-memory.sh /path/to/your-project}"
MEM="$TARGET/docs/memory"
fail=0

err() { echo "✗ $1"; fail=1; }
ok()   { echo "✓ $1"; }

if [ -f "$MEM/RULES.md" ]; then ok "RULES.md（规则）存在"; else err "缺 docs/memory/RULES.md（运行 install.sh）"; fi
if [ -f "$MEM/README.md" ]; then ok "README.md（索引宿主）存在"; else err "缺 docs/memory/README.md"; fi
for f in pitfalls/_TEMPLATE.md decisions/_TEMPLATE.md playbooks/_TEMPLATE.md _PROFILE_TEMPLATE.md; do
    if [ -f "$MEM/$f" ]; then ok "模板 $f 存在"; else err "缺 docs/memory/$f"; fi
done
for d in pitfalls decisions playbooks; do
    if [ -d "$MEM/$d" ]; then ok "$d/ 目录存在"; else err "缺 docs/memory/$d/"; fi
done

MB="$TARGET/.repo-memory-kit/bin/memory-build"
if [ ! -f "$MB" ]; then
    err "缺 .repo-memory-kit/bin/memory-build（重新运行 install.sh 升级）"
elif ! command -v python3 >/dev/null 2>&1; then
    err "无 python3——条目/frontmatter/索引/锚点校验无法执行（不假绿），请安装 python3"
elif python3 "$MB" --check "$TARGET"; then
    ok "条目 frontmatter、README 索引、.anchors.json 一致性通过（memory-build --check）"
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
