#!/bin/sh
# memory-build v3 测试：frontmatter 校验、索引/锚点生成、--check 新鲜度、
# --migrate 旧格式迁移、--changed 变更影响、supersedes 一致性。
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1（期望=$3 实际=$2）"; }
assert_grep() { grep -q "$2" "$3" && ok "$1" || bad "$1: 在 $3 中未找到 $2"; }
MB() { python3 "$KIT/memory-build" "$@"; }

P="$T/proj"; mkdir -p "$P"
"$KIT/install.sh" "$P" >/dev/null

# 合法条目 ×2
cat > "$P/docs/memory/pitfalls/2026-01-01-alpha.md" <<'EOF'
---
type: pitfall
status: confirmed
module: mod-a
created: 2026-01-01
summary: Alpha 坑的索引一句话
anchors:
  - class:com.example.AlphaService
---

# Alpha 坑

## 现象
坏了
EOF
cat > "$P/docs/memory/playbooks/2026-01-02-beta-flow.md" <<'EOF'
---
type: playbook
status: confirmed
module: mod-b
created: 2026-01-02
verified: 2026-01-03
anchors:
  - file:src/beta.yml
  - class:com.example.BetaService
---

# Beta 流程

- 适用范围/环境: 测试环境

## 操作步骤
一步步来
EOF

# ── B1 生成：索引与锚点表 ──
MB "$P" >/dev/null
assert_grep "索引含 pitfall 条目"     "Alpha 坑的索引一句话" "$P/docs/memory/README.md"
assert_grep "索引含状态映射"          "已确认"               "$P/docs/memory/README.md"
assert_grep "锚点表含 class 键"       "class:com.example.AlphaService" "$P/docs/memory/.anchors.json"
assert_grep "锚点表含 file 键"        "file:src/beta.yml"    "$P/docs/memory/.anchors.json"
assert_eq   "锚点数=3" "$(python3 -c "import json;print(len(json.load(open('$P/docs/memory/.anchors.json'))['anchors']))")" "3"

# ── B2 --check 新鲜 ──
if MB --check "$P" >/dev/null 2>&1; then ok "--check 通过（索引/锚点最新）"; else bad "--check 误报过期"; fi

# ── B3 篡改锚点表 → --check 抓到 ──
python3 -c "
import json; p='$P/docs/memory/.anchors.json'
d=json.load(open(p)); d['anchors']['class:hack:X']={'entries':['pitfalls/2026-01-01-alpha.md']}
json.dump(d, open(p,'w'), ensure_ascii=False, indent=2)"
if MB --check "$P" >/dev/null 2>&1; then bad "--check 未抓到锚点表篡改"; else ok "--check 抓到锚点表篡改"; fi

# ── B4 新增条目未刷新 → --check 抓到；重建后恢复 ──
cat > "$P/docs/memory/decisions/2026-01-04-gamma.md" <<'EOF'
---
type: decision
status: confirmed
created: 2026-01-04
---

# Gamma 决定
选了 A 方案
EOF
if MB --check "$P" >/dev/null 2>&1; then bad "--check 未抓到索引过期"; else ok "--check 抓到索引过期"; fi
MB "$P" >/dev/null && assert_grep "重建后索引含 decision" "Gamma 决定" "$P/docs/memory/README.md"

# ── B5 非法 status / 假日期被拒 ──
cat > "$P/docs/memory/pitfalls/2026-01-05-bad.md" <<'EOF'
---
type: pitfall
status: 已确认
created: YYYY-MM-DD
---

# 坏条目
EOF
if MB "$P" >/dev/null 2>&1; then bad "非法 status/假日期未被拒"; else ok "非法 status/假日期被拒"; fi
rm "$P/docs/memory/pitfalls/2026-01-05-bad.md"

# ── B6 supersedes 一致性 ──
cat > "$P/docs/memory/pitfalls/2026-01-06-new.md" <<'EOF'
---
type: pitfall
status: confirmed
created: 2026-01-06
supersedes: pitfalls/2026-01-01-alpha.md
---

# 新版替代旧版
EOF
if MB "$P" >/dev/null 2>&1; then bad "superseded 一致性未触发（旧条目仍 confirmed）"; else ok "supersedes 指向但旧条目未标 superseded → 拒绝"; fi
sed -i 's/^status: confirmed/status: superseded/' "$P/docs/memory/pitfalls/2026-01-01-alpha.md"
if MB "$P" >/dev/null 2>&1; then ok "旧条目标 superseded 后通过"; else bad "superseded 修复后仍拒绝"; fi

# ── B7 --migrate 旧格式条目 ──
cat > "$P/docs/memory/pitfalls/2026-01-07-legacy.md" <<'EOF'
# 旧格式条目

- 状态: 已确认
- 模块: mod-legacy

## 现象
老问题
EOF
MB --migrate "$P" >/dev/null
assert_grep "迁移合成 frontmatter status" "status: confirmed" "$P/docs/memory/pitfalls/2026-01-07-legacy.md"
assert_grep "迁移合成 created（文件名日期）" "created: 2026-01-07" "$P/docs/memory/pitfalls/2026-01-07-legacy.md"
grep -q "^- 状态: 已确认" "$P/docs/memory/pitfalls/2026-01-07-legacy.md" && bad "旧状态行未从正文移除" || ok "旧状态行已移入 frontmatter"

# ── B8 --changed：git 变更 → 波及条目（文件名启发式）──
git -C "$P" init -q
git -C "$P" add -A >/dev/null && git -C "$P" -c user.email=t@t -c user.name=t commit -qm init
mkdir -p "$P/src" && printf 'x\n' > "$P/src/AlphaService.java"
OUT="$(MB --changed "$P")"
case "$OUT" in *alpha*) ok "--changed 命中 Alpha 条目";; *) bad "--changed 未命中（输出: $OUT）";; esac
OUT2="$(MB --since HEAD "$P")"
case "$OUT2" in *无变更文件*|*不波及*) ok "--since HEAD 无提交变更时正确报告";; *) bad "--since HEAD 预期无变更（输出: $OUT2）";; esac

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
