#!/bin/sh
# 校验 docs/memory 记忆区：结构完整性 + .anchors.json 协议（v2）+ 索引一致性
# 用法: ./validate-memory.sh /path/to/your-project
# 依赖 python3（缺解析器时明确失败，不做"假绿"跳过）

TARGET="${1:?用法: ./validate-memory.sh /path/to/your-project}"
MEM="$TARGET/docs/memory"
fail=0

err() { echo "✗ $1"; fail=1; }
ok()   { echo "✓ $1"; }

[ -f "$MEM/RULES.md" ] && ok "RULES.md（规则）存在" || err "缺 docs/memory/RULES.md（运行 install.sh）"
[ -f "$MEM/README.md" ] && ok "README.md（索引）存在" || err "缺 docs/memory/README.md"
[ -f "$MEM/pitfalls/_TEMPLATE.md" ] && ok "条目模板存在" || err "缺 docs/memory/pitfalls/_TEMPLATE.md"
[ -f "$MEM/decisions/_TEMPLATE.md" ] && ok "决定模板存在" || err "缺 docs/memory/decisions/_TEMPLATE.md"
[ -f "$MEM/playbooks/_TEMPLATE.md" ] && ok "流程模板存在" || err "缺 docs/memory/playbooks/_TEMPLATE.md"
[ -f "$MEM/_PROFILE_TEMPLATE.md" ] && ok "画像模板存在" || err "缺 docs/memory/_PROFILE_TEMPLATE.md"
[ -d "$MEM/playbooks" ] && ok "playbooks/ 目录存在" || err "缺 docs/memory/playbooks/"
[ -d "$MEM/pitfalls" ] && ok "pitfalls/ 目录存在" || err "缺 docs/memory/pitfalls/"
[ -d "$MEM/decisions" ] && ok "decisions/ 目录存在" || err "缺 docs/memory/decisions/"

if ! command -v python3 >/dev/null 2>&1; then
    err "无 python3——锚点协议与索引一致性校验无法执行（不跳过、不假绿），请安装 python3 后重跑"
    echo; echo "存在问题"; exit 1
fi

if [ -f "$MEM/.anchors.json" ]; then
    if python3 - "$MEM" <<'PYEOF'
import json, os, re, sys

mem = sys.argv[1]
d = json.load(open(os.path.join(mem, ".anchors.json"), encoding="utf-8"))
assert d.get("schema_version") == 2, "schema_version 须为 2"

KINDS = {"class", "method", "file", "route", "config"}

def check_entries(where, key, entries):
    assert isinstance(entries, list) and entries, f"{where}[{key}]: entries 须为非空数组"
    assert entries == sorted(set(entries)), f"{where}[{key}]: entries 须升序去重"
    for e in entries:
        assert not e.startswith("/") and ".." not in e.split("/"), f"{where}[{key}]: 非法路径 {e}"
        assert os.path.isfile(os.path.join(mem, e)), f"{where}[{key}]: 条目不存在 {e}"

def check_keys(where, obj):
    keys = list(obj)
    assert keys == sorted(keys), f"{where}: 键须升序排序"
    for k in keys:
        kind, _, ident = k.partition(":")
        assert kind in KINDS, f"{where}: 键 {k} 的类型前缀非法（须为 {sorted(KINDS)}）"
        assert ident, f"{where}: 键 {k} 缺标识符"

anchors = d.get("anchors")
assert isinstance(anchors, dict), "anchors 须为对象"
check_keys("anchors", anchors)
for key, val in anchors.items():
    check_entries("anchors", key, val.get("entries", []))
    if "file" in val:
        f = val["file"]
        assert not f.startswith("/") and ".." not in f.split("/"), f"anchors[{key}]: file 非法路径 {f}"

unresolved = d.get("unresolved", {})
assert isinstance(unresolved, dict), "unresolved 须为对象"
check_keys("unresolved", unresolved)
for key, val in unresolved.items():
    assert isinstance(val, dict), f"unresolved[{key}]: 值须为对象"
    check_entries("unresolved", key, val.get("entries", []))
    reason = val.get("reason")
    assert isinstance(reason, str) and reason.strip(), f"unresolved[{key}]: 缺非空 reason"

# ── 索引一致性 + 条目语义完整性 ──
SUBS = ("pitfalls", "decisions", "playbooks")
readme = open(os.path.join(mem, "README.md"), encoding="utf-8").read()
linked = set(re.findall(r"\]\((pitfalls/[^)#]+|decisions/[^)#]+|playbooks/[^)#]+)\.md\)", readme))
actual = set()
for sub in SUBS:
    for name in os.listdir(os.path.join(mem, sub)):
        if name.endswith(".md") and not name.startswith("_"):
            actual.add(f"{sub}/{name[:-3]}")
missing_in_index = actual - linked
dead_links = linked - actual
assert not missing_in_index, f"条目未登记进 README 索引: {sorted(missing_in_index)}"
assert not dead_links, f"README 索引链接指向不存在的条目: {sorted(dead_links)}"

VALID_STATUS = {"已确认", "已验证", "待验证", "deprecated"}

def check_semantic(sub, name, text):
    title = re.search(r"^# (.+)$", text, re.M)
    assert title, f"{sub}/{name}: 缺一级标题"
    assert "<" not in title.group(1) and ">" not in title.group(1), f"{sub}/{name}: 标题仍是模板占位符"
    if sub == "playbooks":
        assert "最后有效" in text, f"{sub}/{name}: 缺「最后有效」日期"
    else:
        m = re.search(r"[-*]?\s*\**状态\**\s*[:：]\s*(.+)$", text, re.M)
        assert m, f"{sub}/{name}: 缺状态行"
        line = m.group(1).strip()
        assert "/" not in line, f"{sub}/{name}: 状态行仍是模板多选占位"
        status = re.split(r"[（(,，]", line)[0].strip()
        assert status in VALID_STATUS, f"{sub}/{name}: 非法状态 {status}（合法: {sorted(VALID_STATUS)}）"
    for dm in re.finditer(r"(?:最后有效|定位日期|日期|最后蒸馏)\s*[:：]\s*(.+)$", text, re.M):
        v = dm.group(1)
        assert "YYYY" not in v, f"{sub}/{name}: 日期仍是模板占位符（YYYY-MM-DD）"
        assert re.search(r"\d{4}-\d{2}-\d{2}", v), f"{sub}/{name}: 缺真实日期（YYYY-MM-DD 格式）"

for sub in SUBS:
    for name in sorted(os.listdir(os.path.join(mem, sub))):
        if not name.endswith(".md") or name.startswith("_"):
            continue
        check_semantic(sub, name, open(os.path.join(mem, sub, name), encoding="utf-8").read())
PYEOF
    then
        ok ".anchors.json 符合协议（schema_version=2，类型化键/路径/排序校验通过）"
        ok "README 索引与实际条目一致（无漏登、无死链）"
        ok "条目语义完整（占位符/多选状态/假日期被拒，状态与日期合法）"
    else
        err ".anchors.json 协议、索引一致性或条目语义校验失败（见上方信息）"
    fi
else
    ok ".anchors.json 尚未生成（首次全量巡检后产生）"
    # 无锚点表时仍校验索引一致性与条目语义
    python3 - "$MEM" <<'PYEOF' && ok "README 索引一致 + 条目语义完整" || err "README 索引或条目语义校验失败"
import os, re, sys
mem = sys.argv[1]
SUBS = ("pitfalls", "decisions", "playbooks")
readme = open(os.path.join(mem, "README.md"), encoding="utf-8").read()
linked = set(re.findall(r"\]\((pitfalls/[^)#]+|decisions/[^)#]+|playbooks/[^)#]+)\.md\)", readme))
actual = set()
for sub in SUBS:
    for name in os.listdir(os.path.join(mem, sub)):
        if name.endswith(".md") and not name.startswith("_"):
            actual.add(f"{sub}/{name[:-3]}")
assert not (actual - linked), f"条目未登记进 README 索引: {sorted(actual - linked)}"
assert not (linked - actual), f"README 索引死链: {sorted(linked - actual)}"
VALID_STATUS = {"已确认", "已验证", "待验证", "deprecated"}
def check_semantic(sub, name, text):
    title = re.search(r"^# (.+)$", text, re.M)
    assert title, f"{sub}/{name}: 缺一级标题"
    assert "<" not in title.group(1) and ">" not in title.group(1), f"{sub}/{name}: 标题仍是模板占位符"
    if sub == "playbooks":
        assert "最后有效" in text, f"{sub}/{name}: 缺「最后有效」日期"
    else:
        m = re.search(r"[-*]?\s*\**状态\**\s*[:：]\s*(.+)$", text, re.M)
        assert m, f"{sub}/{name}: 缺状态行"
        line = m.group(1).strip()
        assert "/" not in line, f"{sub}/{name}: 状态行仍是模板多选占位"
        status = re.split(r"[（(,，]", line)[0].strip()
        assert status in VALID_STATUS, f"{sub}/{name}: 非法状态 {status}"
    for dm in re.finditer(r"(?:最后有效|定位日期|日期|最后蒸馏)\s*[:：]\s*(.+)$", text, re.M):
        v = dm.group(1)
        assert "YYYY" not in v, f"{sub}/{name}: 日期仍是模板占位符"
        assert re.search(r"\d{4}-\d{2}-\d{2}", v), f"{sub}/{name}: 缺真实日期"
for sub in SUBS:
    for name in os.listdir(os.path.join(mem, sub)):
        if not name.endswith(".md") or name.startswith("_"):
            continue
        check_semantic(sub, name, open(os.path.join(mem, sub, name), encoding="utf-8").read())
PYEOF
fi

echo
if [ "$fail" = 0 ]; then
    echo "全部通过"
else
    echo "存在问题"
    exit 1
fi
