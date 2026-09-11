#!/bin/sh
# 技能副本一致性:skills/ 是唯一源,.claude/.agents 副本必须与之一致。
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }

KIT_SKILLS="memory-check memory-capture repo-delivery codebase-memory systematic-debugging tdd reuse-research security-review delivery-gate spec-migrate task-handoff"

for tool in $KIT_SKILLS; do
    src="$KIT/skills/$tool/SKILL.md"
    [ -f "$src" ] || { bad "源缺失: skills/$tool/SKILL.md"; continue; }
    src_hash="$(sha256sum "$src" | cut -d' ' -f1)"

    for root in .claude .agents; do
        dst="$KIT/$root/skills/$tool/SKILL.md"
        if [ -f "$dst" ]; then
            dst_hash="$(sha256sum "$dst" | cut -d' ' -f1)"
            if [ "$src_hash" = "$dst_hash" ]; then
                ok "$root/$tool 一致"
            else
                bad "$root/$tool 漂移(运行 ./install.sh . 重新同步)"
            fi
        else
            # 安装产物不存在 = 正常(未自托管),跳过
            :
        fi
    done
done

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
