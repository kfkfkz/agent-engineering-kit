#!/bin/sh
# install.sh 测试：首次安装、幂等、空格路径、--update 保护、残缺 .cbmignore、引用完整性
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0

ok()   { pass=$((pass+1)); echo "✓ $1"; }
bad()  { fail=$((fail+1)); echo "✗ $1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1（期望=$3 实际=$2）"; }
assert_exists() { [ -e "$2" ] && ok "$1" || bad "$1: $2 不存在"; }

# ── T1 首次安装 ──
P="$T/proj1"; mkdir -p "$P"; printf '# T\n' > "$P/CLAUDE.md"
"$KIT/install.sh" "$P" >/dev/null
assert_exists "RULES.md 安装"          "$P/docs/memory/RULES.md"
assert_exists "README.md 创建"         "$P/docs/memory/README.md"
assert_exists "条目模板安装"            "$P/docs/memory/pitfalls/_TEMPLATE.md"
assert_exists "PROFILE 模板安装"       "$P/docs/memory/_PROFILE_TEMPLATE.md"
assert_exists "Claude 技能（memory-check）"   "$P/.claude/skills/memory-check/SKILL.md"
assert_exists "Claude 技能（memory-capture）" "$P/.claude/skills/memory-capture/SKILL.md"
assert_exists "Codex 技能（memory-check）"    "$P/.agents/skills/memory-check/SKILL.md"
assert_exists "Codex 技能（memory-capture）"  "$P/.agents/skills/memory-capture/SKILL.md"
assert_exists "CLAUDE.md 段落追加"      "$P/CLAUDE.md"
assert_eq "cbmignore 托管区块数=1" "$(grep -c 'repo-memory-kit:start' "$P/.cbmignore")" "1"

# ── T2 幂等：二次安装区块不重复、用户文件不覆盖 ──
echo "CUSTOM INDEX LINE" >> "$P/docs/memory/README.md"
"$KIT/install.sh" "$P" >/dev/null
assert_eq "二次安装区块仍=1" "$(grep -c 'repo-memory-kit:start' "$P/.cbmignore")" "1"
grep -q "CUSTOM INDEX LINE" "$P/docs/memory/README.md" && ok "README 用户内容保留" || bad "README 被覆盖"

# ── T3 --update：kit 文件刷新、用户文件不动 ──
echo "# local hack" >> "$P/docs/memory/RULES.md"
"$KIT/install.sh" --update "$P" >/dev/null
grep -q "local hack" "$P/docs/memory/RULES.md" && bad "RULES.md（kit 管辖）未被刷新" || ok "RULES.md 刷新为最新"
grep -q "CUSTOM INDEX LINE" "$P/docs/memory/README.md" && ok "README（用户所有）未被覆盖" || bad "README 被覆盖"

# ── T4 路径含空格 ──
P2="$T/proj with space"; mkdir -p "$P2"
"$KIT/install.sh" "$P2" >/dev/null
assert_exists "空格路径：技能安装" "$P2/.claude/skills/memory-check/SKILL.md"
assert_exists "空格路径：RULES 安装" "$P2/docs/memory/RULES.md"
assert_eq "空格路径：区块=1" "$(grep -c 'repo-memory-kit:start' "$P2/.cbmignore")" "1"

# ── T5 残缺 .cbmignore（只有 !docs/memory/ 一行，旧检查会误判完整）──
P3="$T/proj3"; mkdir -p "$P3"; printf '!docs/memory/\n' > "$P3/.cbmignore"
"$KIT/install.sh" "$P3" >/dev/null
assert_eq "残缺修复：区块=1" "$(grep -c 'repo-memory-kit:start' "$P3/.cbmignore")" "1"
assert_eq "残缺修复：区块含 !docs/" "$(sed -n '/repo-memory-kit:start/,/repo-memory-kit:end/p' "$P3/.cbmignore" | grep -c '^!docs/$')" "1"
assert_eq "残缺修复：区块含 docs/*" "$(sed -n '/repo-memory-kit:start/,/repo-memory-kit:end/p' "$P3/.cbmignore" | grep -c '^docs/\*$')" "1"

# ── T6 RULES.md 引用的文件均可达 ──
for f in README.md pitfalls/_TEMPLATE.md _PROFILE_TEMPLATE.md; do
    assert_exists "RULES 引用可达: $f" "$P/docs/memory/$f"
done

# ── T7 校验器对全新安装通过 ──
if sh "$KIT/validate-memory.sh" "$P" >/dev/null 2>&1; then ok "validate-memory.sh 全新安装通过"; else bad "validate-memory.sh 对全新安装报错"; fi

# ── T8 校验器能抓坏协议（schema_version 缺失）──
P4="$T/proj4"; mkdir -p "$P4"
"$KIT/install.sh" "$P4" >/dev/null
printf '{"anchors": {"X": ["a.md"]}}' > "$P4/docs/memory/.anchors.json"
if sh "$KIT/validate-memory.sh" "$P4" >/dev/null 2>&1; then bad "校验器未抓到缺 schema_version"; else ok "校验器抓到缺 schema_version"; fi

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
