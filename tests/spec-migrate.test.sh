#!/bin/sh
# Spec Kit 迁移测试：发现、dry-run 无写入、结构迁移、幂等与阻断保护。
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

P="$T/project"
F="$P/.specify/specs/001-export"
TPL="$P/.specify/templates"
mkdir -p "$F" "$TPL"

for name in spec plan tasks; do
    printf '# %s 原文\n\n已有内容必须保留。\n' "$name" > "$F/$name.md"
    printf '# %s template\n' "$name" > "$TPL/$name-template.md"
done

# S1 检查模式发现已有功能，但不改文件。
before="$(sha256sum "$F/spec.md" "$F/plan.md" "$F/tasks.md" | sha256sum | cut -d' ' -f1)"
OUT="$(python3 "$KIT/spec-migrate" --check "$P")"
case "$OUT" in *"功能目录: 1"*"待补字段:"*) ok "--check 输出迁移摘要";; *) bad "--check 摘要缺失: $OUT";; esac
after="$(sha256sum "$F/spec.md" "$F/plan.md" "$F/tasks.md" | sha256sum | cut -d' ' -f1)"
assert_eq "--check 不写文档" "$after" "$before"

# S2 dry-run 给出计划且不写文件。
OUT2="$(python3 "$KIT/spec-migrate" --dry-run "$P")"
case "$OUT2" in *"DRY-RUN"*"001-export"*) ok "--dry-run 输出逐功能计划";; *) bad "--dry-run 计划缺失: $OUT2";; esac
after2="$(sha256sum "$F/spec.md" "$F/plan.md" "$F/tasks.md" | sha256sum | cut -d' ' -f1)"
assert_eq "--dry-run 不写文档" "$after2" "$before"

# S3 apply 只补结构，保留原文并显式标记待人工核验。
python3 "$KIT/spec-migrate" --apply "$P" >/dev/null
assert_grep "spec 补目标范围" '^## 目标与范围' "$F/spec.md"
assert_grep "spec 保留原文" '已有内容必须保留' "$F/spec.md"
assert_grep "plan 补安全威胁模型" '^## 安全与威胁模型' "$F/plan.md"
assert_grep "plan 补交付验证" '^## 交付验证' "$F/plan.md"
assert_grep "plan 补文档记忆影响" '^## 文档与记忆影响' "$F/plan.md"
assert_grep "迁移内容标记待核验" 'agent-engineering-kit:migration-pending' "$F/plan.md"
assert_grep "spec 模板获得覆盖层" 'agent-engineering-kit:template-overlay:start' "$TPL/spec-template.md"
assert_grep "plan 模板获得覆盖层" 'agent-engineering-kit:template-overlay:start' "$TPL/plan-template.md"
assert_grep "tasks 模板获得覆盖层" 'agent-engineering-kit:template-overlay:start' "$TPL/tasks-template.md"

# S4 apply 幂等，不重复章节。
python3 "$KIT/spec-migrate" --apply "$P" >/dev/null
assert_eq "目标范围章节幂等" "$(grep -c '^## 目标与范围' "$F/spec.md")" "1"
assert_eq "模板覆盖层幂等" "$(grep -c 'agent-engineering-kit:template-overlay:start' "$TPL/plan-template.md")" "1"

# S5 JSON 检查可供 CI/Agent 消费，并能识别待核验。
python3 "$KIT/spec-migrate" --check --json "$P" > "$T/report.json"
if python3 -c "import json; d=json.load(open('$T/report.json')); assert d['summary']['features']==1 and d['summary']['pending_review']>0"; then
    ok "--json 输出有效且包含待核验数"
else
    bad "--json 输出无效"
fi

# S6 缺失核心文件时 apply 整体阻断，不产生半迁移。
P2="$T/incomplete"
mkdir -p "$P2/.specify/specs/002-broken"
printf '# spec only\n' > "$P2/.specify/specs/002-broken/spec.md"
if python3 "$KIT/spec-migrate" --apply "$P2" >/dev/null 2>&1; then
    bad "缺 plan/tasks 时 apply 未阻断"
else
    ok "缺 plan/tasks 时 apply 阻断"
fi
if grep -q 'migration-pending' "$P2/.specify/specs/002-broken/spec.md"; then
    bad "阻断后产生半迁移"
else
    ok "阻断后没有半迁移"
fi

# S7 拒绝通过 .specify 内符号链接读取或写入仓库外文档。
P3="$T/symlink-project"; ESC3="$T/external-feature"
mkdir -p "$P3/.specify/specs" "$ESC3"
for name in spec plan tasks; do printf '# %s external\n' "$name" > "$ESC3/$name.md"; done
ln -s "$ESC3" "$P3/.specify/specs/003-escape"
if python3 "$KIT/spec-migrate" --apply "$P3" >/dev/null 2>&1; then
    bad "迁移器接受了逃逸仓库的 feature 符号链接"
else
    ok "迁移器拒绝逃逸仓库的 feature 符号链接"
fi
if grep -q 'migration-pending' "$ESC3/plan.md"; then
    bad "迁移器向仓库外文档写入"
else
    ok "符号链接阻断后仓库外文档未变"
fi

# S8 非 UTF-8 文档整体阻断，避免 read-replace-write 造成静默数据损坏。
P4="$T/invalid-encoding"; F4="$P4/.specify/specs/004-invalid"
mkdir -p "$F4"
printf '# spec\n' > "$F4/spec.md"
printf '# plan\n\377\n' > "$F4/plan.md"
printf '# tasks\n' > "$F4/tasks.md"
before4="$(sha256sum "$F4/plan.md" | cut -d' ' -f1)"
if python3 "$KIT/spec-migrate" --apply "$P4" >/dev/null 2>&1; then
    bad "迁移器接受了非 UTF-8 文档"
else
    ok "迁移器拒绝非 UTF-8 文档"
fi
assert_eq "编码阻断后原文未变" "$(sha256sum "$F4/plan.md" | cut -d' ' -f1)" "$before4"

# S9 子目录同名文件不遮蔽核心文档：apply 两次不重复追加，check 如实报告待核验。
P5="$T/subdir-collision"; F5="$P5/.specify/specs/005-collide"
mkdir -p "$F5/contracts"
for name in spec plan tasks; do printf '# %s 原文\n' "$name" > "$F5/$name.md"; done
printf '# 契约补充\n\n这是 contracts 子目录里的 plan.md，不是交付主文档。\n' > "$F5/contracts/plan.md"
python3 "$KIT/spec-migrate" --apply "$P5" >/dev/null
python3 "$KIT/spec-migrate" --apply "$P5" >/dev/null
assert_eq "子目录同名文件下章节不重复" "$(grep -c '^## 当前证据与复用依据' "$F5/plan.md")" "1"
OUT5="$(python3 "$KIT/spec-migrate" --check "$P5")"
case "$OUT5" in *"待补字段: 0"*) ok "碰撞场景下 apply 后无待补字段";; *) bad "碰撞场景下误报待补: $OUT5";; esac
case "$OUT5" in *"待人工核验: 9"*) ok "碰撞场景下待核验计数完整";; *) bad "碰撞场景下待核验计数异常: $OUT5";; esac

# S10 辅助文档不误判字段：quickstart.md 满足交付验证，但其中「日志安全」不算威胁模型。
P6="$T/aux-files"; F6="$P6/.specify/specs/006-aux"
mkdir -p "$F6"
printf '# spec\n' > "$F6/spec.md"
printf '# plan\n\n## Technical Context\n\n既有证据。\n' > "$F6/plan.md"
printf '# tasks\n\n### Tests for User Story 1\n\n- 用例\n' > "$F6/tasks.md"
printf '# 验证指南\n\n## 9. 回退策略\n\n内容。\n' > "$F6/quickstart.md"
OUT6="$(python3 "$KIT/spec-migrate" --check "$P6")"
case "$OUT6" in *待补=*安全与威胁模型*) ok "quickstart 的日志安全不满足威胁模型";; *) bad "安全字段判定未收紧: $OUT6";; esac
case "$OUT6" in
    *待补=*交付验证*) bad "quickstart.md 存在时仍判定交付验证缺失" ;;
    *) ok "quickstart.md 视为交付验证已覆盖" ;;
esac
python3 "$KIT/spec-migrate" --apply "$P6" >/dev/null
if grep -q '^## 交付验证' "$F6/plan.md"; then
    bad "quickstart.md 存在时仍追加了交付验证小节"
else
    ok "quickstart.md 存在时不追加交付验证小节"
fi
assert_grep "证据类字段仍由核心文档判定" '^## Technical Context' "$F6/plan.md"

# S11 --feature 过滤：只迁移指定 feature，未知名称报错。
P7="$T/feature-filter"
FA="$P7/.specify/specs/007-alpha"; FB="$P7/.specify/specs/008-beta"
mkdir -p "$FA" "$FB" "$P7/.specify/templates"
for d in "$FA" "$FB"; do
    for name in spec plan tasks; do printf '# %s\n' "$name" > "$d/$name.md"; done
done
python3 "$KIT/spec-migrate" --apply --feature 007-alpha "$P7" >/dev/null
if grep -q 'migration-pending' "$FA/plan.md"; then ok "--feature 命中的 feature 被迁移"; else bad "--feature 命中的 feature 未迁移"; fi
if grep -q 'migration-pending' "$FB/plan.md"; then bad "--feature 未命中的 feature 被误迁移"; else ok "--feature 未命中的 feature 保持原样"; fi
if python3 "$KIT/spec-migrate" --apply --feature 009-nope "$P7" >/dev/null 2>&1; then
    bad "未知 feature 名称未报错"
else
    ok "未知 feature 名称报错退出"
fi

# S12 SDD 结构迁移：dry-run 不写、参数校验、落位改名、源目录移除、check 报告。
P8="$T/sdd-project"; F8="$P8/.specify/specs/020-demo"
mkdir -p "$F8/contracts" "$F8/checklists"
printf '# Feature Specification: 演示功能\n\n## Success Criteria\n\n- 可验收\n' > "$F8/spec.md"
printf '# Implementation Plan\n\n## Technical Context\n\n已有证据。\n' > "$F8/plan.md"
printf '# Tasks\n\n### Tests for User Story 1\n\n- 用例\n' > "$F8/tasks.md"
printf '# 数据模型\n' > "$F8/data-model.md"
printf '# 验证指南\n' > "$F8/quickstart.md"
printf '# 契约\n' > "$F8/contracts/api.md"
printf '# 检查\n' > "$F8/checklists/requirements.md"
printf 'PNG' > "$F8/arch.png"
SDD="$P8/docs/03-SDD/V测试一期"
if python3 "$KIT/spec-migrate" --dry-run --layout sdd --sdd-version V测试一期 --feature 020-demo "$P8" | grep -q "02-需求/020-演示功能.md"; then
    ok "SDD dry-run 输出映射计划"
else
    bad "SDD dry-run 计划缺失"
fi
assert_gone "SDD dry-run 不写文件" "$P8/docs"
if python3 "$KIT/spec-migrate" --apply --layout sdd --feature 020-demo "$P8" >/dev/null 2>&1; then
    bad "缺 --sdd-version 未报错"
else
    ok "缺 --sdd-version 报错"
fi
if python3 "$KIT/spec-migrate" --apply --layout sdd --sdd-version V测试一期 "$P8" >/dev/null 2>&1; then
    bad "SDD apply 未要求 --feature"
else
    ok "SDD apply 强制 --feature"
fi
python3 "$KIT/spec-migrate" --apply --layout sdd --sdd-version V测试一期 --feature 020-demo "$P8" >/dev/null
assert_exists "需求迁移落位并改名" "$SDD/02-需求/020-演示功能.md"
assert_exists "架构设计迁移落位" "$SDD/03-架构设计/020-演示功能.md"
assert_exists "实现计划迁移落位" "$SDD/06-实现计划/020-演示功能.md"
assert_exists "详细设计目录（去零序号）" "$SDD/04-详细设计/20-演示功能"
assert_exists "数据库设计改名落位" "$SDD/04-详细设计/20-演示功能/03-数据库设计.md"
assert_exists "契约目录落位" "$SDD/04-详细设计/20-演示功能/02-API设计-契约/api.md"
assert_exists "验证指南落位" "$SDD/04-详细设计/20-演示功能/07-验证指南.md"
assert_exists "检查单落位" "$SDD/07-原始需求材料/020-检查单/requirements.md"
assert_exists "流程设计结构占位" "$SDD/04-详细设计/20-演示功能/04-流程设计.md"
assert_exists "验收标准结构占位" "$SDD/04-详细设计/20-演示功能/05-验收标准.md"
assert_exists "功能设计结构占位" "$SDD/04-详细设计/20-演示功能/01-功能设计.md"
assert_exists "图片进 images 目录" "$SDD/04-详细设计/20-演示功能/images/arch.png"
assert_exists "索引生成" "$SDD/01-索引/README.md"
assert_grep "索引登记序号与功能名" '020 | 需求 | 演示功能' "$SDD/01-索引/README.md"
assert_gone "源 feature 目录移除" "$F8"
assert_grep "安全待核验进架构设计" 'migration-pending:security' "$SDD/03-架构设计/020-演示功能.md"
assert_grep "目标范围待核验进需求" 'migration-pending:target_scope' "$SDD/02-需求/020-演示功能.md"
OUT8="$(python3 "$KIT/spec-migrate" --check "$P8")"
case "$OUT8" in
    *"SDD 待核验"*) ok "check 报告 SDD 待核验标记" ;;
    *) bad "check 未报告 SDD 待核验: $OUT8" ;;
esac
# 目标已存在时拒绝覆盖，不产生半迁移。
F9="$P8/.specify/specs/021-collide"
mkdir -p "$F9"
printf '# Feature Specification: 演示功能\n' > "$F9/spec.md"
printf '# Plan\n' > "$F9/plan.md"
printf '# Tasks\n' > "$F9/tasks.md"
touch "$SDD/02-需求/021-演示功能.md"
if python3 "$KIT/spec-migrate" --apply --layout sdd --sdd-version V测试一期 --feature 021-collide "$P8" >/dev/null 2>&1; then
    bad "SDD 目标冲突未拒绝"
else
    ok "SDD 目标冲突拒绝覆盖"
fi
assert_exists "冲突时源 feature 保留" "$F9/spec.md"

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
