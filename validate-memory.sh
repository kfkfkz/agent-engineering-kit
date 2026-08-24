#!/bin/sh
# 校验 docs/memory 记忆区结构完整性：必备文件 + .anchors.json 协议
# 用法: ./validate-memory.sh /path/to/your-project

TARGET="${1:?用法: ./validate-memory.sh /path/to/your-project}"
MEM="$TARGET/docs/memory"
fail=0

err() { echo "✗ $1"; fail=1; }
ok()   { echo "✓ $1"; }

[ -f "$MEM/RULES.md" ] && ok "RULES.md（规则）存在" || err "缺 docs/memory/RULES.md（运行 install.sh）"
[ -f "$MEM/README.md" ] && ok "README.md（索引）存在" || err "缺 docs/memory/README.md"
[ -f "$MEM/pitfalls/_TEMPLATE.md" ] && ok "条目模板存在" || err "缺 docs/memory/pitfalls/_TEMPLATE.md"
[ -f "$MEM/_PROFILE_TEMPLATE.md" ] && ok "画像模板存在" || err "缺 docs/memory/_PROFILE_TEMPLATE.md"
[ -d "$MEM/playbooks" ] && ok "playbooks/ 目录存在" || err "缺 docs/memory/playbooks/"
[ -d "$MEM/pitfalls" ] && ok "pitfalls/ 目录存在" || err "缺 docs/memory/pitfalls/"

if [ -f "$MEM/.anchors.json" ]; then
    if command -v python3 >/dev/null 2>&1; then
        if python3 - "$MEM/.anchors.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d.get("schema_version") == 1, "缺 schema_version:1（或版本不认识）"
anchors = d.get("anchors")
assert isinstance(anchors, dict), "anchors 须为对象"
for sym, entries in anchors.items():
    assert isinstance(sym, str) and sym, "存在空符号"
    assert isinstance(entries, list) and entries, f"{sym}: entries 为空"
    assert entries == sorted(set(entries)), f"{sym}: entries 须升序去重"
unresolved = d.get("unresolved", {})
assert isinstance(unresolved, dict), "unresolved 须为对象"
assert list(anchors) == sorted(anchors), "anchors 键须排序"
assert list(unresolved) == sorted(unresolved), "unresolved 键须排序"
PYEOF
        then ok ".anchors.json 符合协议（schema_version=1）"
        else err ".anchors.json 不符合协议（见上方断言信息）"
        fi
    else
        echo "⚠ 无 python3，跳过 .anchors.json 协议校验"
    fi
else
    ok ".anchors.json 尚未生成（首次全量巡检后产生）"
fi

echo
if [ "$fail" = 0 ]; then
    echo "全部通过"
else
    echo "存在问题"
    exit 1
fi
