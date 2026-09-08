#!/bin/sh
# memory-build v3 测试：frontmatter 校验、索引/锚点生成、--check 新鲜度、
# --migrate 旧格式迁移、--changed 变更影响、supersedes 一致性、L3 自动门槛。
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }
assert_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望=$3 实际=$2）"; fi; }
assert_grep() { if grep -q "$2" "$3"; then ok "$1"; else bad "$1: 在 $3 中未找到 $2"; fi; }
assert_exists() { if [ -e "$2" ]; then ok "$1"; else bad "$1: $2 不存在"; fi; }
assert_gone() { if [ ! -e "$2" ]; then ok "$1"; else bad "$1: $2 仍存在"; fi; }
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
if grep -q "^- 状态: 已确认" "$P/docs/memory/pitfalls/2026-01-07-legacy.md"; then bad "旧状态行未从正文移除"; else ok "旧状态行已移入 frontmatter"; fi

# ── B8 --changed：git 变更 → 波及条目（文件名启发式）──
git -C "$P" init -q
git -C "$P" add -A >/dev/null && git -C "$P" -c user.email=t@t -c user.name=t commit -qm init
mkdir -p "$P/src" && printf 'x\n' > "$P/src/AlphaService.java"
OUT="$(MB --changed "$P")"
case "$OUT" in *alpha*) ok "--changed 命中 Alpha 条目";; *) bad "--changed 未命中（输出: $OUT）";; esac
OUT2="$(MB --since HEAD "$P")"
case "$OUT2" in *无变更文件*|*不波及*) ok "--since HEAD 无提交变更时正确报告";; *) bad "--since HEAD 预期无变更（输出: $OUT2）";; esac

# ── B9 --files：stdin 文件清单（VCS 无关入口）──
OUT3="$(printf 'src/BetaService.java\nsrc/other.py\n' | MB --files "$P")"
case "$OUT3" in *beta-flow*) ok "--files 命中 Beta 条目";; *) bad "--files 未命中（输出: $OUT3）";; esac

# ── B10 --report：Markdown 影响报告 ──
printf 'src/AlphaService.java\n' | MB --files "$P" --report "$T/impact.md" >/dev/null
if [ -f "$T/impact.md" ]; then ok "影响报告已生成"; else bad "影响报告未生成"; fi
assert_grep "报告含波及条目"      "2026-01-01-alpha"    "$T/impact.md"
assert_grep "报告含变更来源"      "显式文件列表"        "$T/impact.md"
assert_grep "报告含核验指引"      "重新验证"            "$T/impact.md"

# ── B11 --strict：无波及退出 0，有波及退出 2 ──
if printf 'docs/x.md\n' | MB --files "$P" >/dev/null 2>&1; then ok "--strict 前置：无波及退出 0"; else bad "无波及时异常退出"; fi
if printf 'src/AlphaService.java\n' | MB --files "$P" --strict >/dev/null 2>&1; then
    bad "--strict 有波及时未退出 2"
else
    rc=$?
    if [ "$rc" = "2" ]; then ok "--strict 有波及时退出 2"; else bad "--strict 退出码为 $rc（期望 2）"; fi
fi

# ── B12 L3 门槛：只数 confirmed，并提示建档/重蒸馏 ──
P2="$T/profile-status"; mkdir -p "$P2"
"$KIT/install.sh" "$P2" >/dev/null
i=1
while [ "$i" -le 9 ]; do
    day="$(printf '%02d' "$i")"
    cat > "$P2/docs/memory/pitfalls/2026-02-$day-confirmed.md" <<EOF
---
type: pitfall
status: confirmed
created: 2026-02-$day
---

# Confirmed $i
EOF
    i=$((i+1))
done
cat > "$P2/docs/memory/pitfalls/2026-02-10-unverified.md" <<'EOF'
---
type: pitfall
status: unverified
created: 2026-02-10
---

# Unverified
EOF
cat > "$P2/docs/memory/pitfalls/2026-02-11-deprecated.md" <<'EOF'
---
type: pitfall
status: deprecated
created: 2026-02-11
---

# Deprecated
EOF
OUT4="$(MB "$P2")"
case "$OUT4" in *"confirmed 9/10"*"尚未达到"*) ok "L3 门槛排除 unverified/inactive";; *) bad "L3 有效计数错误（输出: $OUT4）";; esac

cat > "$P2/docs/memory/decisions/2026-02-10-tenth.md" <<'EOF'
---
type: decision
status: confirmed
created: 2026-02-10
---

# 第十条有效记忆
EOF
OUT5="$(MB "$P2")"
case "$OUT5" in *"confirmed 10/10"*"缺少 PROFILE.md"*) ok "达到 L3 门槛时提示创建 PROFILE";; *) bad "L3 建档提示缺失（输出: $OUT5）";; esac

cat > "$P2/docs/memory/PROFILE.md" <<'EOF'
# 项目经验画像（L3）

> 最后蒸馏：2026-02-05
EOF
OUT6="$(MB --check "$P2")"
case "$OUT6" in *"其后新增 confirmed 5 条"*"建议重新蒸馏"*) ok "新增 confirmed 达阈值时提示重蒸馏";; *) bad "L3 重蒸馏提示缺失（输出: $OUT6）";; esac

# ── T7 条目按标题中文重命名：文件、交叉引用、索引、锚点联动 ──
P3="$T/rename"; mkdir -p "$P3"
"$KIT/install.sh" "$P3" >/dev/null
cat > "$P3/docs/memory/pitfalls/2026-03-01-zip-format-mismatch.md" <<'EOF'
---
type: pitfall
status: confirmed
module: arch
created: 2026-03-01
summary: 压缩包格式误判
anchors:
  - class:com.example.ZipRouter
---

# 压缩包格式误判

## 现象
坏了
EOF
cat > "$P3/docs/memory/pitfalls/2026-03-02-related-issue.md" <<'EOF'
---
type: pitfall
status: unverified
module: arch
created: 2026-03-02
related:
  - pitfalls/2026-03-01-zip-format-mismatch.md
---

# 相邻问题

## 现象
有关联
EOF
MB --rename-by-title "$P3" >/dev/null
assert_exists "条目按中文标题重命名" "$P3/docs/memory/pitfalls/2026-03-01-压缩包格式误判.md"
assert_gone "旧英文文件名移除" "$P3/docs/memory/pitfalls/2026-03-01-zip-format-mismatch.md"
assert_grep "交叉引用联动改写" 'pitfalls/2026-03-01-压缩包格式误判.md' "$P3/docs/memory/pitfalls/2026-03-02-相邻问题.md"
assert_grep "README 索引指向新文件名" '2026-03-01-压缩包格式误判.md' "$P3/docs/memory/README.md"
assert_grep "锚点表条目路径更新" '2026-03-01-压缩包格式误判.md' "$P3/docs/memory/.anchors.json"
OUT7="$(MB --rename-by-title "$P3")"
case "$OUT7" in *"无需重命名"*) ok "重命名幂等";; *) bad "重命名不幂等: $OUT7";; esac
if MB --check "$P3" >/dev/null 2>&1; then ok "重命名后 --check 一致"; else bad "重命名后 --check 失败"; fi


# ── T8 memory-recall 语义检索层 ──
P4R="$T/recall"; mkdir -p "$P4R"
"$KIT/install.sh" "$P4R" >/dev/null
assert_exists "语义检索器随仓库安装" "$P4R/.repo-memory-kit/bin/memory-recall"
cat > "$P4R/docs/memory/pitfalls/2026-01-01-alpha.md" <<'EOF2'
---
type: pitfall
status: confirmed
created: 2026-01-01
summary: Alpha 坑
---

# Alpha 坑

内容。
EOF2
OUT8="$(python3 "$KIT/memory-recall" --list "$P4R")"
case "$OUT8" in *"pitfall/confirmed"*"2026-01-01-alpha.md"*) ok "--list 输出语料类型与状态";; *) bad "--list 异常: $OUT8";; esac
if python3 -c "import zvec" >/dev/null 2>&1; then
    python3 "$KIT/memory-recall" --rebuild "$P4R" >/dev/null
    OUT9="$(python3 "$KIT/memory-recall" "Alpha 坑" "$P4R")"
    case "$OUT9" in *"2026-01-01-alpha.md"*) ok "语义检索命中条目";; *) bad "语义检索未命中: $OUT9";; esac
    if python3 "$KIT/memory-recall" --check "$P4R" | grep -q "1 篇"; then ok "--check 报告索引规模"; else bad "--check 异常"; fi
else
    if python3 "$KIT/memory-recall" --rebuild "$P4R" >/dev/null 2>&1; then bad "缺 zvec 未报错"; else ok "缺 zvec 明确报错降级"; fi
fi


echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
