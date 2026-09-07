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

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
