#!/bin/sh
# shellcheck disable=SC2015,SC2181  # 断言惯用法
# 技能副本一致性:安装到临时目录,校验 skills/ 源 → .claude/.agents 副本一致。
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
XDG_CONFIG_HOME="$T/xdg-config"; export XDG_CONFIG_HOME  # 隔离事务密钥（不动 HOME：zvec 在 user-site）
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }

KIT_SKILLS="memory-check memory-capture repo-delivery codebase-memory systematic-debugging tdd reuse-research security-review delivery-gate spec-migrate task-handoff design-pipeline"

# 先安装到临时目录(确保副本由 install.sh 正确生成)
P="$T/proj"
mkdir -p "$P"
"$SRC/install.sh" "$P" >/dev/null 2>&1
[ -d "$P/.claude/skills" ] || { echo "✗ install.sh 未生成 .claude/skills"; exit 1; }
[ -d "$P/.agents/skills" ] || { echo "✗ install.sh 未生成 .agents/skills"; exit 1; }
ok "install.sh 生成技能副本目录"

# 校验副本与源一致
for tool in $KIT_SKILLS; do
    src="$SRC/skills/$tool/SKILL.md"
    [ -f "$src" ] || { bad "源缺失: skills/$tool/SKILL.md"; continue; }
    src_hash="$(sha256sum "$src" | cut -d' ' -f1)"
    for root in .claude .agents; do
        dst="$P/$root/skills/$tool/SKILL.md"
        if [ -f "$dst" ]; then
            dst_hash="$(sha256sum "$dst" | cut -d' ' -f1)"
            if [ "$src_hash" = "$dst_hash" ]; then
                ok "$root/$tool 一致"
            else
                bad "$root/$tool 漂移"
            fi
        else
            bad "$root/$tool 缺失"
        fi
    done
done

# design-pipeline 必须从仓库根/同级 skill 定位 archify；不能在
# docs/03-SDD/<需求>/diagrams 下使用 ../.claude 这个错误相对路径。
DP="$P/.agents/skills/design-pipeline/SKILL.md"
grep -q 'ARCHIFY_ROOT=.*\.agents/skills/archify' "$DP" \
    && ok "design-pipeline 从仓库根定位 archify" \
    || bad "design-pipeline 缺少稳定 archify 定位"
! grep -q 'node \.\./\.claude/skills/archify' "$DP" \
    && ok "design-pipeline 不再使用错误 ../.claude 路径" \
    || bad "design-pipeline 仍使用错误 ../.claude 路径"
grep -q '\.delivery\.json' "$DP" \
    && ok "design-pipeline 持久化 archify delivery receipt" \
    || bad "design-pipeline 未持久化 delivery receipt"
! grep -q '^```mermaid' "$P/docs/01-需求/_模板/业务流程设计.md" \
    && ok "业务流程模板不再默认诱导 Mermaid" \
    || bad "业务流程模板仍含默认 Mermaid 块"

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
