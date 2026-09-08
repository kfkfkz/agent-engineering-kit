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
assert_grep() { if grep -q "$2" "$3"; then ok "$1"; else bad "$1: 在 $3 中未找到 $2"; fi; }

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
assert_exists "Claude 技能（delivery）"  "$P/.claude/skills/repo-delivery/SKILL.md"
assert_exists "Codex 技能（delivery）"   "$P/.agents/skills/repo-delivery/SKILL.md"
assert_exists "Claude 技能（graph）"     "$P/.claude/skills/codebase-memory/SKILL.md"
assert_exists "Codex 技能（debugging）"  "$P/.agents/skills/systematic-debugging/SKILL.md"
assert_exists "Codex 技能（tdd）"        "$P/.agents/skills/tdd/SKILL.md"
for tool in reuse-research security-review delivery-gate spec-migrate task-handoff; do
    assert_exists "新增工程技能（$tool）" "$P/.agents/skills/$tool/SKILL.md"
done
assert_exists "Spec Kit 迁移器随仓库安装" "$P/.repo-memory-kit/bin/spec-migrate"
assert_exists "CLAUDE.md 托管区块"       "$P/CLAUDE.md"
assert_eq "cbmignore 托管区块数=1" "$(grep -c 'repo-memory-kit:start' "$P/.cbmignore")" "1"
assert_eq "CLAUDE.md 区块标记=1"  "$(grep -cF '<!-- repo-memory-kit:start -->' "$P/CLAUDE.md")" "1"
if grep -q '/repo-delivery' "$P/CLAUDE.md"; then ok "CLAUDE.md 含 slash 工作流入口"; else bad "CLAUDE.md 缺 slash 工作流入口"; fi
if grep -qF "\$repo-delivery" "$P/CLAUDE.md"; then ok "CLAUDE.md 含 Codex 工作流入口"; else bad "CLAUDE.md 缺 Codex 工作流入口"; fi
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
assert_gone     "卸载：delivery 技能移除"  "$P4/.agents/skills/repo-delivery"
assert_gone     "卸载：graph 技能移除"     "$P4/.agents/skills/codebase-memory"
assert_gone     "卸载：debugging 技能移除" "$P4/.agents/skills/systematic-debugging"
assert_gone     "卸载：tdd 技能移除"       "$P4/.agents/skills/tdd"
for tool in reuse-research security-review delivery-gate spec-migrate task-handoff; do
    assert_gone "卸载：$tool 技能移除" "$P4/.agents/skills/$tool"
done
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
    if [ -L "$W/.agents/skills/repo-delivery" ] &&
       [ "$(readlink "$W/.agents/skills/repo-delivery")" = "$P12/.agents/skills/repo-delivery" ]; then
        ok "Codex workspace 可发现 target-bound delivery 技能"
    else
        bad "Codex workspace 缺 delivery 技能链接"
    fi
    for tool in codebase-memory systematic-debugging tdd reuse-research security-review delivery-gate spec-migrate task-handoff; do
        if [ -L "$W/.agents/skills/$tool" ] &&
           [ "$(readlink "$W/.agents/skills/$tool")" = "$P12/.agents/skills/$tool" ]; then
            ok "Codex workspace 可发现 target-bound $tool 技能"
        else
            bad "Codex workspace 缺 $tool 技能链接"
        fi
    done
    "$KIT/install.sh" --uninstall "$P12" >/dev/null
    if [ ! -L "$W/.agents/skills/memory-capture" ]; then ok "卸载清理 workspace capture 技能链接"; else bad "卸载遗留 workspace capture 技能链接"; fi
    if [ ! -L "$W/.agents/skills/memory-check" ]; then ok "卸载清理 workspace check 技能链接"; else bad "卸载遗留 workspace check 技能链接"; fi
    if [ ! -L "$W/.agents/skills/repo-delivery" ]; then ok "卸载清理 workspace delivery 技能链接"; else bad "卸载遗留 workspace delivery 技能链接"; fi
    for tool in codebase-memory systematic-debugging tdd reuse-research security-review delivery-gate spec-migrate task-handoff; do
        if [ ! -L "$W/.agents/skills/$tool" ]; then ok "卸载清理 workspace $tool 技能链接"; else bad "卸载遗留 workspace $tool 技能链接"; fi
    done
else
    bad "--codex-root 安装失败"
fi

# ── T21 显式选项复用已安装的 codebase-memory-mcp，不触发网络下载 ──
P13="$T/proj13"; mkdir -p "$P13"
FAKE_BIN="$T/fake-bin"; mkdir -p "$FAKE_BIN"
printf '%s\n' '#!/bin/sh' "printf \"%s\\n\" \"\$*\" > \"\$CBM_TEST_MARKER\"" > "$FAKE_BIN/codebase-memory-mcp"
chmod +x "$FAKE_BIN/codebase-memory-mcp"
CBM_TEST_MARKER="$T/cbm-invoked" PATH="$FAKE_BIN:$PATH" "$KIT/install.sh" --with-codebase-memory "$P13" >/dev/null
assert_eq "--with-codebase-memory 调用官方配置入口" "$(cat "$T/cbm-invoked")" "install"
assert_exists "带图谱安装仍安装项目技能" "$P13/.agents/skills/codebase-memory/SKILL.md"

# ── T22 --dry-run：展示计划但不写目标 ──
P14="$T/proj14"; mkdir -p "$P14"; printf '# P\n' > "$P14/CLAUDE.md"
OUT14="$("$KIT/install.sh" --dry-run "$P14")"
case "$OUT14" in *"DRY-RUN"*"repo-delivery"*) ok "--dry-run 输出安装计划";; *) bad "--dry-run 输出不完整";; esac
assert_gone "--dry-run 不创建安装目录" "$P14/.repo-memory-kit"
assert_eq "--dry-run 不修改 CLAUDE.md" "$(cat "$P14/CLAUDE.md")" "# P"

# ── T23 doctor 发现漂移，repair 恢复受管文件 ──
P15="$T/proj15"; mkdir -p "$P15"; printf '# P\n' > "$P15/CLAUDE.md"
"$KIT/install.sh" "$P15" >/dev/null
if "$KIT/install.sh" --doctor "$P15" >/dev/null 2>&1; then ok "doctor 对健康安装通过"; else bad "doctor 对健康安装失败"; fi
rm "$P15/.agents/skills/tdd/SKILL.md"
# 即使清单中的对应行也被删，doctor 仍应按当前 kit 必备集合发现缺失。
sed -i '\|\.agents/skills/tdd/SKILL.md$|d' "$P15/.repo-memory-kit/manifest"
if "$KIT/install.sh" --doctor "$P15" >/dev/null 2>&1; then bad "doctor 未发现缺失文件"; else ok "doctor 发现缺失受管文件（不盲信清单）"; fi
"$KIT/install.sh" --repair "$P15" >/dev/null
assert_exists "repair 恢复缺失文件" "$P15/.agents/skills/tdd/SKILL.md"
if "$KIT/install.sh" --doctor "$P15" >/dev/null 2>&1; then ok "repair 后 doctor 通过"; else bad "repair 后 doctor 仍失败"; fi

# ── T24 普通安装只发现 Spec Kit；显式迁移才写入 ──
P16="$T/proj16"; F16="$P16/.specify/specs/001-demo"
mkdir -p "$F16" "$P16/.specify/templates"
for name in spec plan tasks; do
    printf '# %s\n' "$name" > "$F16/$name.md"
    printf '# %s template\n' "$name" > "$P16/.specify/templates/$name-template.md"
done
"$KIT/install.sh" "$P16" > "$T/spec-install.out"
if grep -q '发现 Spec Kit' "$T/spec-install.out"; then ok "普通安装发现 Spec Kit"; else bad "普通安装未报告 Spec Kit"; fi
if grep -q 'migration-pending' "$F16/plan.md"; then bad "普通安装静默迁移 Spec 文档"; else ok "普通安装不改 Spec 文档"; fi
"$KIT/install.sh" --migrate-specify "$P16" >/dev/null
assert_grep "显式选项迁移 Spec 文档" 'agent-engineering-kit:migration-pending' "$F16/plan.md"

# ── T24b SDD 结构迁移透传 ──
P18="$T/proj18"; F18="$P18/.specify/specs/001-demo"
mkdir -p "$F18" "$P18/.specify/templates"
for name in spec plan tasks; do printf '# %s\n' "$name" > "$F18/$name.md"; done
if "$KIT/install.sh" --migrate-specify=001-demo --sdd-layout --sdd-version V测试 "$P18" >/dev/null 2>&1; then
    ok "install.sh SDD 透传执行成功"
else
    bad "install.sh SDD 透传执行失败"
fi
assert_exists "SDD 需求落位" "$P18/docs/03-SDD/V测试/02-需求/001-spec.md"
assert_gone "SDD 迁移后源 feature 移除" "$F18"
if "$KIT/install.sh" --sdd-layout --sdd-version V测试 "$P18" >/dev/null 2>&1; then
    bad "--sdd-layout 脱离 --migrate-specify 未报错"
else
    ok "--sdd-layout 需与 --migrate-specify 同用"
fi

# ── T25 安装前拒绝会把受管文件写出仓库的符号链接 ──
P17="$T/proj17"; ESC17="$T/outside-agents"; mkdir -p "$P17" "$ESC17"
ln -s "$ESC17" "$P17/.agents"
if "$KIT/install.sh" "$P17" >/dev/null 2>&1; then
    bad "安装器接受了逃逸仓库的 .agents 符号链接"
else
    ok "安装器拒绝逃逸仓库的 .agents 符号链接"
fi
assert_gone "符号链接阻断后未向仓库外安装技能" "$ESC17/skills"


# ── T26 退役技能清理：update 移除 kit 已合并的旧技能，非 kit 内容保留 ──
P19="$T/proj19"; mkdir -p "$P19"
"$KIT/install.sh" "$P19" >/dev/null
old_rev=""
for rev_candidate in $(git -C "$KIT" log --all --format=%H -- skills/delivery-review/SKILL.md); do
    if git -C "$KIT" show "$rev_candidate:skills/delivery-review/SKILL.md" >/dev/null 2>&1; then
        old_rev="$rev_candidate"
        break
    fi
done
if [ -z "$old_rev" ]; then
    if [ -f "$KIT/.git/shallow" ]; then
        ok "浅克隆无历史，跳过退役清理用例"
    else
        bad "找不到历史版本 delivery-review"
    fi
fi
if [ -n "$old_rev" ]; then
    mkdir -p "$P19/.claude/skills/delivery-review" "$P19/.agents/skills/delivery-review"
    git -C "$KIT" show "$old_rev:skills/delivery-review/SKILL.md" > "$P19/.claude/skills/delivery-review/SKILL.md"
    cp "$P19/.claude/skills/delivery-review/SKILL.md" "$P19/.agents/skills/delivery-review/SKILL.md"
    old_hash="$(sha256sum "$P19/.claude/skills/delivery-review/SKILL.md" | cut -d' ' -f1)"
    printf '%s  %s\n' "$old_hash" ".claude/skills/delivery-review/SKILL.md" >> "$P19/.repo-memory-kit/manifest"
    printf '%s  %s\n' "$old_hash" ".agents/skills/delivery-review/SKILL.md" >> "$P19/.repo-memory-kit/manifest"
    mkdir -p "$P19/.claude/skills/custom-skill"
    echo "user custom" > "$P19/.claude/skills/custom-skill/SKILL.md"
    printf '%s  %s\n' "0000" ".claude/skills/custom-skill/SKILL.md" >> "$P19/.repo-memory-kit/manifest"
    "$KIT/install.sh" --update "$P19" >/dev/null
    assert_gone "update 清理退役技能（.claude）" "$P19/.claude/skills/delivery-review"
    assert_gone "update 清理退役技能（.agents）" "$P19/.agents/skills/delivery-review"
    assert_exists "新技能 delivery-gate 在位" "$P19/.claude/skills/delivery-gate/SKILL.md"
    assert_exists "非 kit 内容的技能目录保留" "$P19/.claude/skills/custom-skill/SKILL.md"
    if "$KIT/install.sh" --doctor "$P19" >/dev/null 2>&1; then ok "清理后 doctor 健康"; else bad "清理后 doctor 失败"; fi
fi

# ── T27 会话提醒钩子：注册进 settings.json、保留既有配置、卸载清理 ──
P20="$T/proj20"; mkdir -p "$P20/.claude"
printf '{\n  "enabledPlugins": {"demo@x": true}\n}\n' > "$P20/.claude/settings.json"
"$KIT/install.sh" "$P20" >/dev/null
assert_grep "钩子注册进 settings.json" "session-reminder" "$P20/.claude/settings.json"
assert_grep "既有配置保留" "demo@x" "$P20/.claude/settings.json"
if python3 -c "import zvec" >/dev/null 2>&1; then
    if sh "$P20/.repo-memory-kit/bin/session-reminder" | grep -q "语义"; then ok "提醒脚本输出状态"; else bad "提醒脚本无输出"; fi
else
    if sh "$P20/.repo-memory-kit/bin/session-reminder" >/dev/null 2>&1; then ok "无 zvec 时静默退出"; else bad "无 zvec 时异常退出"; fi
fi
"$KIT/install.sh" --uninstall "$P20" >/dev/null
if grep -q "session-reminder" "$P20/.claude/settings.json" 2>/dev/null; then bad "卸载未清理钩子"; else ok "卸载清理钩子"; fi
assert_grep "卸载保留既有配置" "demo@x" "$P20/.claude/settings.json"


echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
