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
mkdir -p "$P/docs/memory/pitfalls"
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
mkdir -p "$P/docs/memory/playbooks"
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
assert_grep "索引含 pitfall 条目"     "Alpha 坑的索引一句话" "$P/.repo-memory-kit/memory-index.md"
assert_grep "索引含状态映射"          "已确认"               "$P/.repo-memory-kit/memory-index.md"
assert_grep "锚点表含 class 键"       "class:com.example.AlphaService" "$P/.repo-memory-kit/.anchors.json"
assert_grep "锚点表含 file 键"        "file:src/beta.yml"    "$P/.repo-memory-kit/.anchors.json"
assert_eq   "锚点数=3" "$(python3 -c "import json;print(len(json.load(open('$P/.repo-memory-kit/.anchors.json'))['anchors']))")" "3"

# ── B2 --check 新鲜 ──
if MB --check "$P" >/dev/null 2>&1; then ok "--check 通过（索引/锚点最新）"; else bad "--check 误报过期"; fi

# ── B3 篡改锚点表 → --check 抓到 ──
python3 -c "
import json; p='$P/.repo-memory-kit/.anchors.json'
d=json.load(open(p)); d['anchors']['class:hack:X']={'entries':['pitfalls/2026-01-01-alpha.md']}
json.dump(d, open(p,'w'), ensure_ascii=False, indent=2)"
if MB --check "$P" >/dev/null 2>&1; then bad "--check 未抓到锚点表篡改"; else ok "--check 抓到锚点表篡改"; fi

# ── B4 新增条目未刷新 → --check 抓到；重建后恢复 ──
mkdir -p "$P/docs/memory/decisions"
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
MB "$P" >/dev/null && assert_grep "重建后索引含 decision" "Gamma 决定" "$P/.repo-memory-kit/memory-index.md"

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
mkdir -p "$P2/docs/memory/pitfalls" "$P2/docs/memory/decisions"
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
mkdir -p "$P3/docs/memory/pitfalls"
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
cat > "$P3/docs/memory/PROFILE.md" <<'EOF2'
# 项目经验画像（L3）

> 最后蒸馏：2026-03-02

| 风险域 | 信号 | 等级 | 下钻 |
|---|---|---|---|
| 格式解析 | 压缩包容器歧义 | 反复 | → pitfalls/2026-03-01-zip-format-mismatch、playbooks/120-test-env |
EOF2
MB --rename-by-title "$P3" >/dev/null
assert_exists "条目按中文标题重命名" "$P3/docs/memory/pitfalls/2026-03-01-压缩包格式误判.md"
assert_gone "旧英文文件名移除" "$P3/docs/memory/pitfalls/2026-03-01-zip-format-mismatch.md"
assert_grep "交叉引用联动改写" 'pitfalls/2026-03-01-压缩包格式误判.md' "$P3/docs/memory/pitfalls/2026-03-02-相邻问题.md"
assert_grep "本地索引指向新文件名" '2026-03-01-压缩包格式误判.md' "$P3/.repo-memory-kit/memory-index.md"
assert_grep "锚点表条目路径更新" '2026-03-01-压缩包格式误判.md' "$P3/.repo-memory-kit/.anchors.json"
assert_grep "PROFILE 裸词干引用联动改写" 'pitfalls/2026-03-01-压缩包格式误判' "$P3/docs/memory/PROFILE.md"
OUT7="$(MB --rename-by-title "$P3")"
case "$OUT7" in *"无需重命名"*) ok "重命名幂等";; *) bad "重命名不幂等: $OUT7";; esac
if MB --check "$P3" >/dev/null 2>&1; then ok "重命名后 --check 一致"; else bad "重命名后 --check 失败"; fi


# ── T8 memory-recall 语义检索层 ──
P4R="$T/recall"; mkdir -p "$P4R"
"$KIT/install.sh" "$P4R" >/dev/null
assert_exists "语义检索器随仓库安装" "$P4R/.repo-memory-kit/bin/memory-recall"
mkdir -p "$P4R/docs/memory/pitfalls"
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
    if python3 "$KIT/memory-recall" --check "$P4R" | grep -q "语义索引"; then ok "--check 报告索引规模"; else bad "--check 异常"; fi
    # 单一刷新链:memory-build 构建后语义索引随动刷新
    OUT_MB="$(MB "$P4R" 2>&1)"
    case "$OUT_MB" in *语义索引已重建*) ok "memory-build 随动刷新语义索引";; *) bad "memory-build 未随动刷新语义索引: $OUT_MB";; esac
    case "$(MB --check "$P4R" 2>/dev/null)" in *语义索引已重建*) bad "--check 不应触发语义重建";; *) ok "--check 只读不触发语义重建";; esac
else
    if python3 "$KIT/memory-recall" --rebuild "$P4R" >/dev/null 2>&1; then bad "缺 zvec 未报错"; else ok "缺 zvec 明确报错降级"; fi
fi



# ── T9 domain-check:域地图守卫(索引/状态/证据路径) ──
P5D="$T/domaincheck"; mkdir -p "$P5D"
"$KIT/install.sh" "$P5D" >/dev/null
DM="$P5D/docs/02-业务域地图"
mkdir -p "$DM/01-索引" "$DM/03-核心业务域/好域" "$DM/03-核心业务域/坏域" "$P5D/src/real"
mkdir -p "$P5D/.repo-memory-kit/bin" && cp "$KIT/domain-check" "$P5D/.repo-memory-kit/bin/domain-check"
printf 'x\n' > "$P5D/src/real/Service.java"
cat > "$DM/01-索引/README.md" <<'EOF2'
# 索引
| 域 | 入口 |
| --- | --- |
| 好域 | [README](../03-核心业务域/好域/README.md) |
| 幽灵域 | [README](../03-核心业务域/幽灵域/README.md) |
EOF2
cat > "$DM/03-核心业务域/好域/README.md" <<'EOF2'
# 好域
> 状态：已确认
> 最后核对日期：2026-09-01
## 9. 代码证据索引
| 类型 | 路径 |
| --- | --- |
| 类 | `src/real/Service.java` |
| 缩写 | `proj-x/.../Abbrev` |
| 跨仓库 | `demo-sdk/docs/pair.md` |
EOF2
cat > "$DM/03-核心业务域/坏域/README.md" <<'EOF2'
# 坏域
> 只有标题没有状态行
## 9. 代码证据索引
| 类型 | 路径 |
| --- | --- |
| 类 | `src/gone/Deleted.java` |
EOF2
export CROSS_REPO_PREFIXES="demo-sdk"
OUTD="$(python3 "$KIT/domain-check" "$P5D" 2>&1 || true)"
unset CROSS_REPO_PREFIXES
case "$OUTD" in *证据路径失联*src/gone*) ok "域守卫抓到证据路径失联";; *) bad "域守卫未抓到失联路径: $OUTD";; esac
case "$OUTD" in *缺「状态」行*) ok "域守卫抓到缺状态行";; *) bad "域守卫未抓到缺状态行";; esac
case "$OUTD" in *索引指向不存在的条目*幽灵域*) ok "域守卫抓到索引幽灵条目";; *) bad "域守卫未抓到幽灵条目";; esac
case "$OUTD" in *未登记索引*坏域*) ok "域守卫提示未登记条目";; *) bad "域守卫未提示未登记: $OUTD";; esac
if CROSS_REPO_PREFIXES="demo-sdk" python3 "$KIT/domain-check" "$P5D" >/dev/null 2>&1; then bad "有错误时退出码应为 1"; else ok "有错误时退出码 1"; fi
mv "$DM/03-核心业务域/坏域" "$T/坏域备份"
sed -i '/幽灵域/d' "$DM/01-索引/README.md"
if CROSS_REPO_PREFIXES="demo-sdk" python3 "$KIT/domain-check" "$P5D" >/dev/null 2>&1; then ok "干净域地图通过"; else bad "干净域地图误报: $(python3 "$KIT/domain-check" "$P5D" 2>&1 | head -3)"; fi



# ── T11 v4 模块目录：类型在 frontmatter、索引按模块分组、README 不再改写 ──
PM="$T/mod4"; mkdir -p "$PM"
"$KIT/install.sh" "$PM" >/dev/null
printf '# 索引宿主\n' > "$PM/docs/memory/README.md"
README_HASH_BEFORE="$(sha256sum "$PM/docs/memory/README.md" | cut -d' ' -f1)"
mkdir -p "$PM/docs/memory/订单"
cat > "$PM/docs/memory/订单/2026-04-01-导出坑.md" <<'EOF2'
---
type: pitfall
status: confirmed
module: 订单
created: 2026-04-01
summary: 导出的坑
---
# 导出坑
EOF2
MB "$PM" >/dev/null
assert_grep "索引按模块分组" "## 订单" "$PM/.repo-memory-kit/memory-index.md"
assert_grep "索引含模块条目（类型来自 frontmatter）" "导出坑" "$PM/.repo-memory-kit/memory-index.md"
assert_eq "README 回归纯 seed（构建不改写）" "$README_HASH_BEFORE" "$(sha256sum "$PM/docs/memory/README.md" | cut -d' ' -f1)"
OUTL="$(python3 "$KIT/memory-recall" --list "$PM")"
case "$OUTL" in *"[pitfall/confirmed]"*"订单/2026-04-01-导出坑.md"*) ok "语义层按 frontmatter 判型（--list）";; *) bad "语义层类型判定异常: $OUTL";; esac
# module 与目录不一致被抓
sed -i 's/^module: 订单/module: 库存/' "$PM/docs/memory/订单/2026-04-01-导出坑.md"
if MB "$PM" >/dev/null 2>&1; then bad "module 与目录不一致未被抓"; else ok "module 与目录不一致被抓"; fi
sed -i 's/^module: 库存/module: 订单/' "$PM/docs/memory/订单/2026-04-01-导出坑.md"

# ── T12 --migrate-to-modules：存量类型目录条目迁入模块目录 ──
PMM="$T/mod-mig"; mkdir -p "$PMM"
"$KIT/install.sh" "$PMM" >/dev/null
mkdir -p "$PMM/docs/memory/pitfalls"
cat > "$PMM/docs/memory/pitfalls/2026-05-01-存量坑.md" <<'EOF2'
---
type: pitfall
status: confirmed
module: 订单
created: 2026-05-01
summary: 存量坑
related: []
---
# 存量坑
EOF2
cat > "$PMM/docs/memory/pitfalls/2026-05-02-无模块坑.md" <<'EOF2'
---
type: pitfall
status: unverified
created: 2026-05-02
summary: 无模块坑
---
# 无模块坑
EOF2
MB "$PMM" --migrate-to-modules >/dev/null
assert_exists "有 module 字段的条目迁入对应模块目录" "$PMM/docs/memory/订单/2026-05-01-存量坑.md"
assert_exists "无 module 字段的条目迁入默认模块（通用）" "$PMM/docs/memory/通用/2026-05-02-无模块坑.md"
assert_grep "迁移后补写 module 字段" "^module: 通用$" "$PMM/docs/memory/通用/2026-05-02-无模块坑.md"
MB "$PMM" >/dev/null && assert_grep "迁移后索引按新路径分组" "订单/2026-05-01-存量坑.md" "$PMM/.repo-memory-kit/memory-index.md"
if MB --check "$PMM" >/dev/null 2>&1; then ok "迁移后 --check 通过"; else bad "迁移后 --check 失败"; fi

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
