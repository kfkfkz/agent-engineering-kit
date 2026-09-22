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

# repo-delivery 是薄入口；重型规则只能从固定 Catalog 按 route/stage 完整读取。
RD_SRC="$SRC/skills/repo-delivery/SKILL.md"
rd_lines="$(wc -l < "$RD_SRC" | tr -d ' ')"
rd_bytes="$(wc -c < "$RD_SRC" | tr -d ' ')"
[ "$rd_lines" -le 100 ] && ok "repo-delivery 主入口不超过 100 行" \
    || bad "repo-delivery 主入口过长（$rd_lines 行）"
[ "$rd_bytes" -le 12288 ] && ok "repo-delivery 主入口不超过 12 KiB" \
    || bad "repo-delivery 主入口过大（$rd_bytes bytes）"

RD_REFS="direct bounded standard initiative evidence-closeout"
for ref in $RD_REFS; do
    src="$SRC/skills/repo-delivery/references/$ref.md"
    [ -f "$src" ] || { bad "repo-delivery reference 源缺失: $ref"; continue; }
    for root in .claude .agents; do
        dst="$P/$root/skills/repo-delivery/references/$ref.md"
        if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
            ok "$root/repo-delivery reference $ref 一致"
        else
            bad "$root/repo-delivery reference $ref 缺失或漂移"
        fi
    done
done

python3 - "$SRC" "$P" "$T" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

src, target, tmp = map(Path, sys.argv[1:])
sys.path.insert(0, str(src))
from aek.core.context.reference_catalog import REFERENCE_CATALOG, ReferenceSpec
from installer.registry import _skill_reference_specs

expected = {"direct", "bounded", "standard", "initiative", "evidence-closeout"}
assert set(REFERENCE_CATALOG.references) == expected
reference_specs = _skill_reference_specs()
assert len(reference_specs) == 2 * len(expected)
for spec in reference_specs:
    name = spec.source_path.rsplit("/", 1)[-1].removesuffix(".md")
    assert spec.source_path == REFERENCE_CATALOG.references[name].source_path
for route in ("direct", "bounded", "standard", "initiative"):
    route_refs = REFERENCE_CATALOG.for_route(route, "route")
    closeout_refs = REFERENCE_CATALOG.for_route(route, "closeout")
    expected_route_ids = {
        "direct": ("direct",), "bounded": ("bounded",),
        "standard": ("standard",),
        "initiative": ("standard", "initiative"),
    }
    assert tuple(ref.id for ref in route_refs) == expected_route_ids[route]
    assert tuple(ref.id for ref in closeout_refs) == ("evidence-closeout",)
    assert "evidence-closeout" not in {ref.id for ref in route_refs}
    assert len({ref.authority for ref in route_refs + closeout_refs}) == len(route_refs + closeout_refs)
for invalid in ("skills/repo-delivery/references/../escape.md",
                "skills/repo-delivery/references/nested/escape.md"):
    try:
        ReferenceSpec("invalid", invalid, ("direct",), ("route",), "invalid")
    except ValueError:
        pass
    else:
        raise AssertionError(f"unsafe reference accepted: {invalid}")
try:
    REFERENCE_CATALOG.for_route("direct", "unknown")
except ValueError:
    pass
else:
    raise AssertionError("unknown stage accepted")

manifest = json.loads((target / ".repo-memory-kit/manifest.json").read_text())
ids = {entry["spec_id"] for entry in manifest["entries"]}
for ref in expected:
    assert f"skill-agents-repo-delivery-reference-{ref}" in ids
    assert f"skill-claude-repo-delivery-reference-{ref}" in ids
for relative in ("__init__.py", "core/__init__.py", "core/context/__init__.py",
                 "core/context/reference_catalog.py", "core/context/budget.py",
                 "core/artifact/__init__.py", "core/artifact/registry.py",
                 "core/planning/__init__.py", "core/planning/facts.py",
                 "core/planning/policy.py",
                 "core/planning/sql_screen.py", "core/context/telemetry.py",
                 "core/context/memory.py",
                 "core/context/capsule.py",
                 "core/artifact/plan.py",
                 "core/review/__init__.py", "core/review/judge.py",
                 "application/__init__.py", "adapters/__init__.py",
                 "adapters/telemetry.py", "adapters/memory.py",
                 "adapters/plan_transaction.py"):
    assert f"lib-aek-{relative.replace('/', '-').replace('.', '-')}" in ids

from installer.registry import AEK_PACKAGE_FILES
source_files = {
    path.relative_to(src / "aek").as_posix()
    for path in (src / "aek").rglob("*.py")
}
assert source_files == set(AEK_PACKAGE_FILES), (
    "package registry mismatch", sorted(source_files - set(AEK_PACKAGE_FILES)),
    sorted(set(AEK_PACKAGE_FILES) - source_files))

env = {**os.environ, "PYTHONPATH": str(target / ".repo-memory-kit/lib")}
proc = subprocess.run(
    [sys.executable, "-c",
     "from aek.core.context.reference_catalog import REFERENCE_CATALOG; "
     "from aek.core.context.budget import resolve_context_budget; "
     "from aek.core.artifact.registry import ARTIFACT_REGISTRY; "
     "from aek.core.planning.facts import FACT_CATALOG; "
     "from aek.core.planning.policy import preview_plan; "
     "from aek.core.planning.sql_screen import SqlPerformanceScreenResult; "
     "from aek.core.context.telemetry import build_context_report; "
     "from aek.adapters.telemetry import ContextLedger; "
     "from aek.application.context_telemetry import report_for_budget; "
     "from aek.core.context.memory import CandidateRef; "
     "from aek.core.context.capsule import build_capsule; "
     "from aek.core.artifact.plan import build_plan; "
     "from aek.core.review.judge import judge_gate; "
     "from aek.core.review.result import validate_review_payload; "
     "from aek.core.policy.evaluator import evaluate_policy; "
     "from aek.adapters.memory import fallback_candidates; "
     "from aek.adapters.plan_transaction import PlanStore; "
     "assert REFERENCE_CATALOG.for_route('bounded', 'route'); "
     "assert resolve_context_budget('bounded', 'routing', 'quick'); "
     "assert ARTIFACT_REGISTRY.get_group('计划'); assert len(FACT_CATALOG) == 8; "
     "assert callable(preview_plan); assert SqlPerformanceScreenResult; "
     "assert callable(build_context_report); assert ContextLedger; "
     "assert callable(report_for_budget); "
     "assert CandidateRef; assert callable(fallback_candidates); "
     "assert callable(build_capsule); assert callable(build_plan); "
     "assert PlanStore; assert callable(judge_gate); "
     "assert callable(validate_review_payload); assert callable(evaluate_policy)"],
    cwd=tmp, env=env, capture_output=True, text=True, check=False)
assert proc.returncode == 0, proc.stderr
proc = subprocess.run(
    [str(target / ".repo-memory-kit/bin/doc-gate"), "--help"],
    cwd=tmp, env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    capture_output=True, text=True, check=False)
assert proc.returncode == 0, proc.stderr
decoy = target / ".repo-memory-kit/bin/aek/core/artifact"
decoy.mkdir(parents=True)
(decoy / "registry.py").write_text("raise RuntimeError('decoy imported')\n")
(decoy.parent / "context").mkdir(parents=True)
(decoy.parent / "context/budget.py").write_text(
    "raise RuntimeError('decoy imported')\n")
proc = subprocess.run(
    [str(target / ".repo-memory-kit/bin/doc-gate"), "--help"],
    cwd=tmp, env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    capture_output=True, text=True, check=False)
assert proc.returncode == 0 and "decoy imported" not in proc.stderr, proc.stderr
proc = subprocess.run(
    [str(target / ".repo-memory-kit/bin/route-eval"), "--help"],
    cwd=tmp, env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    capture_output=True, text=True, check=False)
assert proc.returncode == 0 and "decoy imported" not in proc.stderr, proc.stderr
for cli, module_file in (
    ("doc-gate", "core/artifact/registry.py"),
    ("route-eval", "core/context/budget.py"),
):
    managed = target / ".repo-memory-kit/lib/aek" / module_file
    held = tmp / f"{cli}.held"
    managed.rename(held)
    try:
        narrow_env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        narrow_env.pop("PYTHONPATH", None)
        failed = subprocess.run(
            [str(target / ".repo-memory-kit/bin" / cli), "--help"],
            cwd=tmp, env=narrow_env, capture_output=True, text=True, check=False)
        assert failed.returncode == 3, (cli, failed.returncode, failed.stderr)
        assert "UnicodeEncodeError" not in failed.stderr, failed.stderr
    finally:
        held.rename(managed)
PY
if [ "$?" -eq 0 ]; then
    ok "ReferenceCatalog 与安装 package 可从任意 cwd 使用"
else
    bad "ReferenceCatalog 或安装 package 不可用"
fi

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
grep -q 'doc-gate review-input' "$DP" \
    && grep -q 'review-receipt.json' "$DP" \
    && grep -q 'request.mode=full|incremental' "$DP" \
    && ok "design-pipeline 通过 ReviewService 绑定实际送审输入" \
    || bad "design-pipeline 未接 ReviewService/receipt"
! grep -q '^```mermaid' "$P/docs/01-需求/_模板/业务流程设计.md" \
    && ok "业务流程模板不再默认诱导 Mermaid" \
    || bad "业务流程模板仍含默认 Mermaid 块"

if python3 "$SRC/tests/check-skill-routing.py"; then
    ok "用户可见的 skill 路由名可解析且平台写法完整"
else
    bad "用户可见的 skill 路由名存在歧义或无效引用"
fi

# repo-delivery 必须先做低成本分流，并精确定义会导致误升径的字段。
RD="$P/.agents/skills/repo-delivery/SKILL.md"
grep -q '^## 零阶段路线' "$RD" \
    && ok "repo-delivery 在深读上下文前先做零阶段分流" \
    || bad "repo-delivery 缺少零阶段快速分流"
grep -q '只有正向证据才升径' "$RD" \
    && ok "repo-delivery 要求用正向证据升级路线" \
    || bad "repo-delivery 缺少防保守过度路由规则"
grep -q '不是源代码与测试目录的数量' "$RD" \
    && ok "modules 按独立边界而非目录计数" \
    || bad "modules 计数语义仍可能误升径"
grep -q '不是对话轮数或工具调用次数' "$RD" \
    && ok "sessions 按跨会话交接而非工具调用计数" \
    || bad "sessions 计数语义仍可能误升径"
grep -q 'route_fit=too_heavy' "$RD" \
    && grep -q 'route_override' "$RD" \
    && ok "无理由超配路线会被机械阻断" \
    || bad "repo-delivery 未说明超配路线阻断机制"
grep -q 'execution_profile' "$RD" \
    && ok "repo-delivery 消费机器可读执行深度" \
    || bad "repo-delivery 未消费 execution_profile"

DG="$P/.agents/skills/delivery-gate/SKILL.md"
grep -q 'receipt_detail=compact' "$DG" \
    && grep -q '不得扩成全仓库审查' "$DG" \
    && ok "delivery-gate 对轻路线保持紧凑验证预算" \
    || bad "delivery-gate 仍可能把轻路线扩成重型门禁"

for section in "$SRC/templates/agents-md-section.md" "$SRC/templates/claude-md-section.md"; do
    grep -q '只有正向证据才升径' "$section" \
        && grep -q 'execution_profile' "$section" \
        && ok "$(basename "$section") 固化轻量路由纪律" \
        || bad "$(basename "$section") 缺少轻量路由纪律"
done

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
