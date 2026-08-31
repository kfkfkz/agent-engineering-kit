#!/bin/sh
# install.sh 测试。全程使用临时 HOME——绝不触碰开发者真实全局技能。
# 覆盖：首次安装、幂等、--update 保护、空格路径、残缺 .cbmignore、安全清理
#      （指纹匹配才删）、旧段落自动迁移、定制段落保护、卸载、清单、校验器。
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
export HOME="$T/home"          # 隔离 HOME：全局技能清理只作用于沙箱
mkdir -p "$HOME"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0

ok()   { pass=$((pass+1)); echo "✓ $1"; }
bad()  { fail=$((fail+1)); echo "✗ $1"; }
assert_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望=$3 实际=$2）"; fi; }
assert_exists() { if [ -e "$2" ]; then ok "$1"; else bad "$1: $2 不存在"; fi; }
assert_gone() { if [ ! -e "$2" ]; then ok "$1"; else bad "$1: $2 仍存在"; fi; }

# ── T1 首次安装 ──
P="$T/proj1"; mkdir -p "$P"; printf '# T\n' > "$P/CLAUDE.md"
"$KIT/install.sh" "$P" >/dev/null
assert_exists "RULES.md 安装"            "$P/docs/memory/RULES.md"
assert_exists "README.md 创建"           "$P/docs/memory/README.md"
assert_exists "条目模板安装"              "$P/docs/memory/pitfalls/_TEMPLATE.md"
assert_exists "PROFILE 模板安装"         "$P/docs/memory/_PROFILE_TEMPLATE.md"
assert_exists "校验器随仓库安装"          "$P/.repo-memory-kit/bin/validate-memory.sh"
assert_exists "Claude 技能（check）"     "$P/.claude/skills/memory-check/SKILL.md"
assert_exists "Codex 技能（capture）"    "$P/.agents/skills/memory-capture/SKILL.md"
assert_exists "CLAUDE.md 托管区块"       "$P/CLAUDE.md"
assert_eq "cbmignore 托管区块数=1" "$(grep -c 'repo-memory-kit:start' "$P/.cbmignore")" "1"
assert_eq "CLAUDE.md 区块标记=1"  "$(grep -cF '<!-- repo-memory-kit:start -->' "$P/CLAUDE.md")" "1"
assert_exists "安装清单"                  "$P/.repo-memory-kit/manifest"
if grep -q '^kit_version=' "$P/.repo-memory-kit/manifest"; then ok "清单含 kit_version"; else bad "清单缺 kit_version"; fi

# ── T2 幂等：区块/段落不重复，用户文件不覆盖 ──
echo "CUSTOM INDEX LINE" >> "$P/docs/memory/README.md"
"$KIT/install.sh" "$P" >/dev/null
assert_eq "二次安装 cbmignore 区块仍=1" "$(grep -c 'repo-memory-kit:start' "$P/.cbmignore")" "1"
assert_eq "二次安装 CLAUDE.md 段落仍=1" "$(grep -c '^## 项目记忆（坑与流程）' "$P/CLAUDE.md")" "1"
if grep -q "CUSTOM INDEX LINE" "$P/docs/memory/README.md"; then ok "README 用户内容保留"; else bad "README 被覆盖"; fi

# ── T3 --update：kit 文件刷新、用户文件不动 ──
echo "# local hack" >> "$P/docs/memory/RULES.md"
"$KIT/install.sh" --update "$P" >/dev/null
if grep -q "local hack" "$P/docs/memory/RULES.md"; then bad "RULES.md（kit 管辖）未被刷新"; else ok "RULES.md 刷新为最新"; fi
if grep -q "CUSTOM INDEX LINE" "$P/docs/memory/README.md"; then ok "README（用户所有）未被覆盖"; else bad "README 被覆盖"; fi

# ── T4 路径含空格 ──
P2="$T/proj with space"; mkdir -p "$P2"
"$KIT/install.sh" "$P2" >/dev/null
assert_exists "空格路径：技能安装" "$P2/.claude/skills/memory-check/SKILL.md"
assert_exists "空格路径：RULES 安装" "$P2/docs/memory/RULES.md"
assert_eq "空格路径：区块=1" "$(grep -c 'repo-memory-kit:start' "$P2/.cbmignore")" "1"

# ── T5 残缺 .cbmignore（只有 !docs/memory/ 一行）──
P3="$T/proj3"; mkdir -p "$P3"; printf '!docs/memory/\n' > "$P3/.cbmignore"
"$KIT/install.sh" "$P3" >/dev/null
assert_eq "残缺修复：区块=1" "$(grep -c 'repo-memory-kit:start' "$P3/.cbmignore")" "1"
assert_eq "残缺修复：区块含 !docs/" "$(sed -n '/repo-memory-kit:start/,/repo-memory-kit:end/p' "$P3/.cbmignore" | grep -c '^!docs/$')" "1"
assert_eq "残缺修复：区块含 docs/*" "$(sed -n '/repo-memory-kit:start/,/repo-memory-kit:end/p' "$P3/.cbmignore" | grep -c '^docs/\*$')" "1"

# ── T6 安全清理：全局技能仅指纹匹配才删 ──
P4="$T/proj4"; mkdir -p "$P4"; printf '# P\n' > "$P4/CLAUDE.md"
mkdir -p "$HOME/.codex/skills/memory-check" "$HOME/.codex/skills/memory-capture"
cp "$KIT/skills/memory-check/SKILL.md" "$HOME/.codex/skills/memory-check/SKILL.md"       # 与 kit 一致 → 应删
printf '# 用户自定义技能，提到了 docs/memory 但不是 kit 产物\n' > "$HOME/.codex/skills/memory-capture/SKILL.md"  # 不一致 → 应保留
"$KIT/install.sh" "$P4" >/dev/null
assert_gone  "指纹匹配的全局技能已清理"     "$HOME/.codex/skills/memory-check"
assert_exists "指纹不匹配的全局技能被保留" "$HOME/.codex/skills/memory-capture/SKILL.md"

# ── T7 旧版段落自动迁移（内容与 kit 模板一致 → 托管区块）──
P5="$T/proj5"; mkdir -p "$P5"
{ printf '# P\n\n'; cat "$KIT/templates/claude-md-section.md"; } > "$P5/CLAUDE.md"
"$KIT/install.sh" "$P5" >/dev/null
assert_eq "旧段落迁移后段落=1" "$(grep -c '^## 项目记忆（坑与流程）' "$P5/CLAUDE.md")" "1"
if grep -qF '<!-- repo-memory-kit:start -->' "$P5/CLAUDE.md"; then ok "旧段落已迁移为托管区块"; else bad "旧段落未迁移"; fi

# ── T8 定制段落保护（内容与模板不一致 → 不动）──
P6="$T/proj6"; mkdir -p "$P6"
{ printf '# P\n\n'; cat "$KIT/templates/claude-md-section.md"; echo "- 项目专属的定制行"; } > "$P6/CLAUDE.md"
"$KIT/install.sh" "$P6" >/dev/null
if grep -q "项目专属的定制行" "$P6/CLAUDE.md"; then ok "定制段落内容保留"; else bad "定制段落被改动"; fi
if grep -qF '<!-- repo-memory-kit:start -->' "$P6/CLAUDE.md"; then bad "定制段落被追加新区块（应只提示）"; else ok "定制段落未被追加新区块"; fi

# ── T9 RULES.md 引用的文件均可达 ──
for f in README.md pitfalls/_TEMPLATE.md _PROFILE_TEMPLATE.md; do
    assert_exists "RULES 引用可达: $f" "$P/docs/memory/$f"
done

# ── T10 校验器：全新安装通过 ──
if sh "$KIT/validate-memory.sh" "$P4" >/dev/null 2>&1; then ok "validate-memory.sh 全新安装通过"; else bad "validate-memory.sh 对全新安装报错"; fi

# ── T11 校验器：缺 schema_version 被抓 ──
printf '{"anchors": {"class:X": {"entries": ["pitfalls/a.md"]}}}' > "$P4/docs/memory/.anchors.json"
if sh "$KIT/validate-memory.sh" "$P4" >/dev/null 2>&1; then bad "校验器未抓到缺 schema_version"; else ok "校验器抓到缺 schema_version"; fi

# ── T12 校验器：路径逃逸被抓 ──
printf '{"schema_version": 2, "anchors": {"class:X": {"entries": ["../../etc/passwd"]}}, "unresolved": {}}' > "$P4/docs/memory/.anchors.json"
if sh "$KIT/validate-memory.sh" "$P4" >/dev/null 2>&1; then bad "校验器未抓到路径逃逸"; else ok "校验器抓到路径逃逸"; fi

# ── T13 校验器：索引一致性（条目漏登被抓）──
rm -f "$P4/docs/memory/.anchors.json"
printf '# fake entry\n' > "$P4/docs/memory/pitfalls/2099-01-01-not-in-index.md"
if sh "$KIT/validate-memory.sh" "$P4" >/dev/null 2>&1; then bad "校验器未抓到条目漏登"; else ok "校验器抓到条目漏登"; fi

# ── T14 --uninstall：清 kit 产物，保用户数据 ──
rm -f "$P4/docs/memory/pitfalls/2099-01-01-not-in-index.md"
"$KIT/install.sh" --uninstall "$P4" >/dev/null
assert_gone     "卸载：RULES.md 移除"      "$P4/docs/memory/RULES.md"
assert_gone     "卸载：技能移除"           "$P4/.claude/skills/memory-check"
assert_gone     "卸载：清单移除"           "$P4/.repo-memory-kit"
assert_gone     "卸载：_TEMPLATE.md（清单内）移除" "$P4/docs/memory/pitfalls/_TEMPLATE.md"
if grep -qF '<!-- repo-memory-kit:start -->' "$P4/CLAUDE.md"; then bad "卸载后 CLAUDE.md 仍含区块"; else ok "卸载：CLAUDE.md 区块移除"; fi
if [ -f "$P4/CLAUDE.md" ]; then ok "卸载：CLAUDE.md 本体保留"; else bad "卸载误删 CLAUDE.md 本体"; fi
assert_exists   "卸载：README 索引保留"    "$P4/docs/memory/README.md"
assert_exists   "卸载：用户条目目录保留"   "$P4/docs/memory/pitfalls"

# ── T15 卸载漂移保护：被修改过的 kit 文件不删 ──
P7="$T/proj7"; mkdir -p "$P7"; printf '# P\n' > "$P7/CLAUDE.md"
"$KIT/install.sh" "$P7" >/dev/null
echo "# 手工修改" >> "$P7/docs/memory/RULES.md"
"$KIT/install.sh" --uninstall "$P7" >/dev/null
assert_exists "卸载漂移保护：修改过的 RULES.md 保留" "$P7/docs/memory/RULES.md"
assert_gone     "卸载：未修改的技能正常移除" "$P7/.claude/skills/memory-check"

# ── T16 卸载路径安全：篡改清单的越界路径被拒 ──
P8="$T/proj8"; mkdir -p "$P8"; printf '# P\n' > "$P8/CLAUDE.md"
"$KIT/install.sh" "$P8" >/dev/null
printf 'deadbeef  docs/../escape-marker\ndeadbeef  %s\n' "$T/abs-escape-marker" >> "$P8/.repo-memory-kit/manifest"
touch "$P8/escape-marker" "$T/abs-escape-marker"
"$KIT/install.sh" --uninstall "$P8" >/dev/null
assert_exists "篡改清单：../ 路径未越界删除" "$P8/escape-marker"
assert_exists "篡改清单：绝对路径未删除"    "$T/abs-escape-marker"

# ── T17 空文件不再误判为 kit 产物（历史指纹的空哈希修复）──
P9="$T/proj9"; mkdir -p "$P9"
mkdir -p "$HOME/.codex/skills/memory-check"
: > "$HOME/.codex/skills/memory-check/SKILL.md"
"$KIT/install.sh" "$P9" >/dev/null
assert_exists "空的同名用户技能文件被保留" "$HOME/.codex/skills/memory-check/SKILL.md"

# ── T18 历史版本段落自动迁移（哈希口径统一修复）──
P10="$T/proj10"; mkdir -p "$P10"
prev_rev="$(git -C "$KIT" log --all --format=%H -- templates/claude-md-section.md | sed -n '2p')"
if [ -n "$prev_rev" ]; then
    { printf '# P\n\n'; git -C "$KIT" show "$prev_rev:templates/claude-md-section.md"; } > "$P10/CLAUDE.md"
    "$KIT/install.sh" "$P10" >/dev/null
    if grep -qF '<!-- repo-memory-kit:start -->' "$P10/CLAUDE.md"; then ok "历史版本段落自动迁移"; else bad "历史版本段落未迁移（被误判定制）"; fi
    assert_eq "迁移后段落=1" "$(grep -c '^## 项目记忆（坑与流程）' "$P10/CLAUDE.md")" "1"
else
    ok "（跳过：kit 无历史提交）历史版本段落自动迁移"
fi

# ── T19 校验器拒绝未填写的模板条目 ──
P11="$T/proj11"; mkdir -p "$P11"
"$KIT/install.sh" "$P11" >/dev/null
cp "$KIT/templates/pitfall-entry.md" "$P11/docs/memory/pitfalls/2026-01-01-tpl.md"
printf '\n| [tpl](pitfalls/2026-01-01-tpl.md) | 已确认 | 测试 |\n' >> "$P11/docs/memory/README.md"
if sh "$KIT/validate-memory.sh" "$P11" >/dev/null 2>&1; then bad "校验器接受了未填写的模板条目"; else ok "校验器拒绝未填写的模板条目"; fi
rm -f "$P11/docs/memory/pitfalls/2026-01-01-tpl.md"

# ── T20 Codex 工作区与记忆仓库分离时，技能仍可被发现 ──
W="$T/workspace"; P12="$W/backend"; mkdir -p "$P12"
if "$KIT/install.sh" --codex-root "$W" "$P12" >/dev/null 2>&1; then
    if [ -L "$W/.agents/skills/memory-capture" ] &&
       [ "$(readlink "$W/.agents/skills/memory-capture")" = "$P12/.agents/skills/memory-capture" ]; then
        ok "Codex workspace 可发现 target-bound capture 技能"
    else
        bad "Codex workspace 缺 capture 技能链接"
    fi
    if [ -L "$W/.agents/skills/memory-check" ] &&
       [ "$(readlink "$W/.agents/skills/memory-check")" = "$P12/.agents/skills/memory-check" ]; then
        ok "Codex workspace 可发现 target-bound check 技能"
    else
        bad "Codex workspace 缺 check 技能链接"
    fi
    "$KIT/install.sh" --uninstall "$P12" >/dev/null
    if [ ! -L "$W/.agents/skills/memory-capture" ]; then ok "卸载清理 workspace capture 技能链接"; else bad "卸载遗留 workspace capture 技能链接"; fi
    if [ ! -L "$W/.agents/skills/memory-check" ]; then ok "卸载清理 workspace check 技能链接"; else bad "卸载遗留 workspace check 技能链接"; fi
else
    bad "--codex-root 安装失败"
fi

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
