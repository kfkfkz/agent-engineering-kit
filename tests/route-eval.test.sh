#!/bin/sh
# shellcheck disable=SC2015  # 测试断言惯用法：命令 && ok || bad
# route-eval 公共 CLI 回归：任务路由、治理覆盖与最终 diff 漂移。
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0

ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }
assert_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望=$3 实际=$2）"; fi; }
run_rc() { rc=0; "$@" >/dev/null 2>&1 || rc=$?; }

REPO="$T/repo"
mkdir -p "$REPO/.repo-memory-kit"
printf 'strict\n' > "$REPO/.repo-memory-kit/governance"
printf '{"version":1,"rules":[]}\n' > "$REPO/.repo-memory-kit/governance.json"

PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/evidence-adapter.test.py" \
    && ok "EvidenceRef 仓库边界、当前主体与源摘要校验" \
    || bad "EvidenceRef 来源验证失败"

python3 - "$KIT" <<'PY' \
    && ok "ContextBudget 四路线默认值与输入校验" \
    || bad "ContextBudget 默认值或输入校验失败"
import sys
sys.path.insert(0, sys.argv[1])
from aek.core.context.budget import resolve_context_budget

expected = {
    "direct": (0, 0, "target_and_tests", 6, 96 * 1024, 128 * 1024),
    "bounded": (5, 2, "target_module_and_tests", 20, 256 * 1024, 384 * 1024),
    "standard": (8, 3, "impact_closure", 60, 768 * 1024, 1024 * 1024),
    "initiative": (12, 5, "work_unit_closure", 100, 1536 * 1024, 2 * 1024 * 1024),
}
for route, (candidates, expanded, scope, files, code_bytes, stage_bytes) in expected.items():
    budget = resolve_context_budget(route, "routing", "quick")
    assert (budget.memory_candidate_limit, budget.memory_expand_limit,
            budget.code_scope) == (candidates, expanded, scope)
    assert budget.mandatory_sources == (
        "user_request", "root_instructions", "governance", "safety_required")
    assert (budget.code_file_limit, budget.code_byte_limit,
            budget.stage_byte_limit) == (files, code_bytes, stage_bytes)
    assert budget.required_reference_ids
    assert budget.as_dict() == resolve_context_budget(route, "routing", "quick").as_dict()
overlay = resolve_context_budget("bounded", "execution", "thorough",
    ("security_privacy", "public_contract"))
assert overlay.risk_overlays == ("public_contract", "security_privacy")
assert "risk_overlay:public_contract" in overlay.mandatory_sources
assert "RISK_OVERLAY" in overlay.reason_codes
assert resolve_context_budget("bounded", "closeout", "quick").required_reference_ids == (
    "evidence-closeout",)
for args in (("unknown", "routing", "quick"),
             ("bounded", "unknown", "quick"),
             ("bounded", "routing", "shallow")):
    try:
        resolve_context_budget(*args)
    except ValueError:
        pass
    else:
        raise AssertionError(f"invalid budget input accepted: {args}")
PY

python3 - "$KIT" <<'PY' \
    && ok "ImpactFact 三态归并单调且与输入顺序无关" \
    || bad "ImpactFact 三态归并失败"
import sys
sys.path.insert(0, sys.argv[1])
from aek.core.planning.facts import (
    FACT_CATALOG, FactObservation, merge_fact_observations, normalize_fact_id)

subject = "a" * 64
assert len(FACT_CATALOG) == 8
assert normalize_fact_id("api_change") == ("public_api_change", True)
assert normalize_fact_id("public_api_change") == ("public_api_change", False)

def observation(producer, value, complete=True, fact="public_api_change"):
    return FactObservation(fact, producer, value, subject, producer[0] * 64, complete)

diff_false = observation("diff_scanner", "false")
surface_false = observation("api_surface_parser", "false")
assert merge_fact_observations("public_api_change", subject, ()).state == "unknown"
assert merge_fact_observations("public_api_change", subject, (diff_false,)).state == "unknown"
assert merge_fact_observations("public_api_change", subject, (
    diff_false, surface_false)).state == "false"
assert merge_fact_observations("public_api_change", subject, (
    diff_false, observation("api_surface_parser", "false", False))).state == "unknown"
positive = observation("api_surface_parser", "true")
left = merge_fact_observations("public_api_change", subject, (diff_false, positive))
right = merge_fact_observations("api_change", subject, (positive, diff_false))
assert left.state == right.state == "true"
assert "TRUE_FALSE_CONFLICT" in left.diagnostics
assert left.evidence_digests == right.evidence_digests
assert left.digest == right.digest
assert "DEPRECATED_ALIAS" in right.diagnostics
# A cheap, complete applicability scan is enough to rule out a risk. An
# incomplete scan, absent declaration, or contrary positive never is.
irreversible = FactObservation(
    "irreversible_change", "migration_operation_scanner", "false",
    subject, "d" * 64, True)
assert merge_fact_observations(
    "irreversible_change", subject, (irreversible,)).state == "false"
assert merge_fact_observations("irreversible_change", subject, (
    FactObservation("irreversible_change", "migration_operation_scanner",
                    "false", subject, "d" * 64, False),)).state == "unknown"
assert merge_fact_observations("irreversible_change", subject, (
    FactObservation("irreversible_change", "migration_operation_scanner",
                    "unknown", subject, "d" * 64, False),
    FactObservation("irreversible_change", "human_credential", "false",
                    subject, "e" * 64, True))).state == "false"
assert merge_fact_observations("irreversible_change", subject, (
    irreversible, FactObservation("irreversible_change", "human_credential",
                                  "true", subject, "e" * 64, True))).state == "true"
capacity_clear = FactObservation(
    "performance_capacity", "access_change_scanner", "false",
    subject, "f" * 64, True)
assert merge_fact_observations(
    "performance_capacity", subject, (capacity_clear,)).state == "false"
assert merge_fact_observations("performance_capacity", subject, (
    FactObservation("performance_capacity", "access_change_scanner",
                    "false", subject, "f" * 64, False),)).state == "unknown"
assert merge_fact_observations("performance_capacity", subject, (
    capacity_clear, FactObservation("performance_capacity",
                                    "sql_performance_screen", "true",
                                    subject, "1" * 64, True))).state == "true"
try:
    merge_fact_observations("unregistered", subject, ())
except ValueError:
    pass
else:
    raise AssertionError("unknown fact accepted")
try:
    merge_fact_observations("public_api_change", subject, (
        FactObservation("public_api_change", "diff_scanner", "true",
                        "b" * 64, "c" * 64, True),))
except ValueError:
    pass
else:
    raise AssertionError("stale subject accepted")
for bad_observation in (
    ("public_api_change", "diff_scanner", "true", subject, 42, True),
    ("public_api_change", "diff_scanner", 1, subject, "c" * 64, True),
):
    try:
        FactObservation(*bad_observation)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid fact observation accepted")
PY

python3 - "$KIT" <<'PY' \
    && ok "PlanningPolicy 四路线与 Fact 风险映射" \
    || bad "PlanningPolicy 分类或升径规则失败"
import hashlib
import sys
sys.path.insert(0, sys.argv[1])
from aek.core.planning.facts import FACT_CATALOG, FactObservation, merge_fact_observations
from aek.core.planning.policy import preview_plan

subject = "a" * 64
def facts_with(changes=None):
    changes = changes or {}
    facts = {}
    for fact_id, spec in FACT_CATALOG.items():
        observations = tuple(FactObservation(
            fact_id, producer, changes.get(fact_id, "false"), subject,
            hashlib.sha256(f"{fact_id}:{producer}".encode()).hexdigest(), True)
            for producer in spec.required_producers)
        facts[fact_id] = merge_fact_observations(fact_id, subject, observations)
    return facts

all_false = facts_with()
direct = preview_plan("direct", subject, all_false)
assert direct.policy_version == 2
assert direct.minimum_route == "direct"
assert all(item.classification == "skipped" for item in direct.items)
bounded = preview_plan("bounded", subject, all_false)
assert bounded.minimum_route == "bounded"
assert all(item.classification == "skipped" for item in bounded.items)
standard = preview_plan("standard", subject, all_false)
required = {item.artifact_id for item in standard.items if item.classification == "required"}
assert required == {"requirements-analysis", "outline-design", "detail-design", "tasks", "test-plan"}

api = preview_plan("bounded", subject, facts_with({"public_api_change": "true"}))
assert api.minimum_route == "standard"
assert api.by_id("api-design").classification == "required"
db = preview_plan("bounded", subject, facts_with({"database_change": "true"}))
assert db.minimum_route == "bounded"
assert db.by_id("database-design").classification == "required"
assert "sql-performance-screen" in db.required_checks
assert "performance-review" not in db.required_checks
perf = preview_plan("bounded", subject, facts_with({"performance_capacity": "true"}))
assert perf.minimum_route == "standard"
assert "performance-review" in perf.required_checks
assert preview_plan("standard", subject, facts_with({"ui_behavior_change": "true"})).by_id(
    "ui-design").classification == "required"
assert preview_plan("standard", subject, facts_with({"business_flow_change": "true"})).by_id(
    "business-flow").classification == "required"
unknown = dict(all_false)
unknown["performance_capacity"] = merge_fact_observations("performance_capacity", subject, ())
assert preview_plan("bounded", subject, unknown).minimum_route == "standard"
assert "performance-review" in preview_plan("bounded", subject, unknown).required_checks
assert preview_plan("standard", subject, all_false, required_overlays=("ui-design",)).by_id(
    "ui-design").classification == "required"
assert preview_plan("direct", subject, all_false, required_overlays=("ui-design",)).minimum_route == "standard"
PY

python3 - "$KIT" <<'PY' \
    && ok "SQL 初查结构跨方言且不强制深查" \
    || bad "SQL 初查结构或深查触发失败"
import sys
sys.path.insert(0, sys.argv[1])
from aek.core.planning.sql_screen import SqlPerformanceScreenResult

for dialect in ("PostgreSQL", "MySQL", "SQL Server", "SQLite", "Oracle"):
    result = SqlPerformanceScreenResult(
        subject_digest="a" * 64, dialect=dialect, query_kind="orm",
        screened_dimensions=("access_shape", "result_bounds"),
        risk_indicators=(), scope_complete=True, evidence_digest="b" * 64)
    assert result.capacity_state == "false"
    assert result.to_fact_observation().state == "false"
    assert result.to_fact_observation().producer_id == "sql_performance_screen"
risky = SqlPerformanceScreenResult(
    subject_digest="a" * 64, dialect="PostgreSQL", query_kind="raw_sql",
    screened_dimensions=("access_shape",),
    risk_indicators=("unbounded_scan",), scope_complete=False,
    evidence_digest="b" * 64)
assert risky.capacity_state == "true"
assert "performance-review" not in risky.screened_dimensions
incomplete = SqlPerformanceScreenResult(
    subject_digest="a" * 64, dialect="MySQL", query_kind="orm",
    screened_dimensions=("access_shape",),
    risk_indicators=(), scope_complete=False, evidence_digest="b" * 64)
assert incomplete.capacity_state == "unknown"
try:
    SqlPerformanceScreenResult(
        subject_digest="a" * 64, dialect="MySQL", query_kind="orm",
        screened_dimensions=(), risk_indicators=(), scope_complete=True,
        evidence_digest="b" * 64)
except ValueError:
    pass
else:
    raise AssertionError("unsubstantiated clean screen accepted")
PY

write_card() {
    route="$1"; kind="$2"; behavior="$3"; modules="$4"; repos="$5"; sessions="$6"
    risks="$7"; planned="$8"
    cat > "$T/card.json" <<EOF
{
  "version": 1,
  "route": "$route",
  "work_kind": "$kind",
  "intent_gaps": "none",
  "behavior_change": "$behavior",
  "footprint": {
    "planned_paths": $planned,
    "modules": $modules,
    "repos": $repos,
    "sessions": $sessions
  },
  "reversibility": "easy",
  "risk_overlays": $risks,
  "coordination": "single",
  "uncertainty": "low",
  "route_override": null,
  "required_checks": [],
  "escalate_if": []
}
EOF
}

# L0：无行为变化的小修改允许 Direct；strict 只改变回执形态，不抬高任务路线。
write_card direct docs none 1 1 1 '[]' '["README.md"]'
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 - "$T/out.json" <<'PY' \
    && ok "Direct 路线通过，strict 仅要求正式回执" \
    || bad "Direct 路线或治理维度混淆"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["minimum_route"] == "direct"
assert d["selected_route"] == "direct"
assert d["route_valid"] is True
assert d["requirements"]["receipt_mode"] == "formal"
PY

# L1：小型新增行为必须落 Bounded，而不是因为 work_kind=feature 进入完整 SDD。
write_card bounded feature local 1 1 1 '[]' '["src/export.py","tests/test_export.py"]'
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 - "$T/out.json" <<'PY' \
    && ok "小型明确新功能进入 Bounded" \
    || bad "小型新功能未进入 Bounded"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["minimum_route"] == "bounded"
assert d["selected_route"] == "bounded"
assert "targeted-tests" in d["requirements"]["required_checks"]
assert "full-sdd" not in d["requirements"]["required_checks"]
assert d["execution_profile"] == {
    "context_depth": "targeted",
    "planning_depth": "compact",
    "design_depth": "decision_only",
    "verification_depth": "targeted",
    "receipt_detail": "compact",
}
assert d["context_budget"]["schema_version"] == 1
assert d["context_budget"]["route"] == "bounded"
assert d["context_budget"]["stage"] == "routing"
assert d["context_budget"]["memory_candidate_limit"] == 5
assert d["context_budget"]["memory_expand_limit"] == 2
assert d["context_budget"]["over_budget_action"] == "require_purpose_or_reroute"
PY

# 选得过轻时必须用专用退出码 2 阻断，并给出最低路线。
write_card direct bug local 1 1 1 '[]' '["src/fix.py"]'
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json
assert_eq "行为变更不能误走 Direct" "$rc" "2"

# Direct 的每个必要条件都必须成立；中等不确定性即至少 Bounded。
write_card direct docs none 1 1 1 '[]' '["README.md"]'
python3 - "$T/card.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["uncertainty"] = "medium"
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json
assert_eq "Direct 不接受中等不确定性" "$rc" "2"

# 公共契约、Schema 等风险是独立 overlay，至少提升到 Standard。
write_card bounded feature local 1 1 1 '["public_contract"]' '["src/api.py"]'
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json
assert_eq "公共契约风险至少 Standard" "$rc" "2"
write_card standard feature local 1 1 1 '["schema_migration"]' '["db/migration.sql"]'
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['minimum_route']=='standard'" \
    && ok "Schema overlay 计算 Standard 下限" || bad "Schema overlay 下限错误"

# 性能/容量风险只在显式命中时叠加专项审查，并至少提升到 Standard。
write_card standard feature local 1 1 1 '["performance_capacity"]' '["src/query.py"]'
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 - "$T/out.json" <<'PY' \
    && ok "性能风险叠加专项审查" \
    || bad "性能风险未进入路线或专项检查"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["minimum_route"] == "standard"
assert "performance-review" in d["requirements"]["required_checks"]
PY

# SQL 初查是低成本专项检查，本身不把清晰的局部改动升级到 Standard。
write_card bounded feature local 1 1 1 '[]' '["src/query.py"]'
python3 - "$T/card.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["required_checks"] = ["sql-performance-screen"]
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['minimum_route']=='bounded' and 'sql-performance-screen' in d['requirements']['required_checks']" \
    && ok "SQL 初查不自动升径" || bad "SQL 初查被误升为深度性能审查"

# 多仓库/较长多会话工作进入 Initiative；work_kind 本身不参与升级。
write_card initiative feature local 2 2 3 '["cross_repo"]' '["service-a/**","service-b/**"]'
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['minimum_route']=='initiative' and 'full-sdd' in d['requirements']['required_checks']" \
    && ok "跨仓库多会话工作进入 Initiative" || bad "Initiative 判定错误"

# 无证据选择更重路线也是路由错误；只有用户/项目明确要求时才能例外。
write_card standard docs none 1 1 1 '[]' '["README.md"]'
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json
assert_eq "无理由过度路由被阻断" "$rc" "2"
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json" 2>/dev/null || true
python3 -c "import json; d=json.load(open('$T/out.json')); assert not d['route_efficient'] and d['route_fit']=='too_heavy' and d['recommended_route']=='direct' and d['effective_route']=='direct' and d['execution_profile']['context_depth']=='minimal'" \
    && ok "过度路由给出推荐路线" || bad "过度路由缺少机器可读证据"
python3 - "$T/card.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["route_override"] = {
    "kind": "user_requested",
    "reason": "用户明确要求执行 Standard 的完整独立审查"
}
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --json > "$T/out.json"
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['route_valid'] and d['route_efficient'] and d['route_fit']=='overridden' and d['minimum_route']=='direct'" \
    && ok "显式用户要求允许采用更重路线" || bad "合法路线例外被错误拒绝"

# 最终 diff 超出 planned_paths 必须报告路由漂移并返回 2。
write_card bounded feature local 1 1 1 '[]' '["src/export.py","tests/**"]'
cat > "$T/drift.diff" <<'EOF'
diff --git a/src/export.py b/src/export.py
--- a/src/export.py
+++ b/src/export.py
@@ -1 +1,2 @@
 old
+new
diff --git a/db/schema.sql b/db/schema.sql
--- a/db/schema.sql
+++ b/db/schema.sql
@@ -1 +1,2 @@
 old
+ALTER TABLE orders ADD COLUMN exported_at timestamp;
EOF
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --diff "$T/drift.diff" --json
assert_eq "未计划文件触发 route drift" "$rc" "2"
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --diff "$T/drift.diff" --json > "$T/out.json" 2>/dev/null || true
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['drift_detected'] and d['unplanned_files']==['db/schema.sql']" \
    && ok "route drift 精确列出越界文件" || bad "route drift 证据不完整"

# planned_paths 使用路径 glob 语义：* 不跨目录，** 才跨目录。
write_card bounded feature local 1 1 1 '[]' '["src/*.py"]'
cat > "$T/nested.diff" <<'EOF'
diff --git a/src/nested/export.py b/src/nested/export.py
--- a/src/nested/export.py
+++ b/src/nested/export.py
@@ -1 +1,2 @@
 old
+new
EOF
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --diff "$T/nested.diff" --json
assert_eq "planned_paths 单星号不跨目录" "$rc" "2"

# --git-ref 必须同时覆盖 tracked 与 untracked；不能让新建高风险文件逃出 final diff。
GIT_REPO="$T/git-repo"
mkdir -p "$GIT_REPO/src" "$GIT_REPO/.repo-memory-kit"
git -C "$GIT_REPO" init -q
git -C "$GIT_REPO" config user.email test@example.invalid
git -C "$GIT_REPO" config user.name test
printf 'old\n' > "$GIT_REPO/src/export.py"
printf '.repo-memory-kit/\n' > "$GIT_REPO/.gitignore"
git -C "$GIT_REPO" add src/export.py .gitignore
git -C "$GIT_REPO" commit -qm base
printf 'strict\n' > "$GIT_REPO/.repo-memory-kit/governance"
printf '%s\n' '{"version":1,"rules":[{"id":"DB-UNTRACKED","require":["min_route:standard","required_check:migration-plan"],"match":{"files":["*.sql"]}}]}' > "$GIT_REPO/.repo-memory-kit/governance.json"
printf 'new\n' >> "$GIT_REPO/src/export.py"
mkdir -p "$GIT_REPO/db"
printf 'ALTER TABLE orders ADD COLUMN exported_at timestamp;\n' > "$GIT_REPO/db/schema.sql"
write_card bounded feature local 1 1 1 '[]' '["src/**"]'
run_rc python3 "$KIT/route-eval" "$GIT_REPO" --card "$T/card.json" --git-ref HEAD --json
assert_eq "--git-ref 捕获未跟踪文件" "$rc" "2"
python3 "$KIT/route-eval" "$GIT_REPO" --card "$T/card.json" --git-ref HEAD --json > "$T/out.json" 2>/dev/null || true
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['changed_files']==['db/schema.sql','src/export.py'] and d['unplanned_files']==['db/schema.sql'] and d['minimum_route']=='standard' and d['governance']['matched_rules'][0]['rule_id']=='DB-UNTRACKED'" \
    && ok "untracked 同时进入漂移与治理证据" || bad "--git-ref 漏掉工作区文件或治理规则"

# untracked 新增行以 + 开头时仍是内容，不得被误认成 +++ 文件头而漏掉正则规则。
GIT_LINE="$T/git-line"
mkdir -p "$GIT_LINE/.repo-memory-kit" "$GIT_LINE/src"
git -C "$GIT_LINE" init -q
git -C "$GIT_LINE" config user.email test@example.invalid
git -C "$GIT_LINE" config user.name test
printf '.repo-memory-kit/\n' > "$GIT_LINE/.gitignore"
git -C "$GIT_LINE" add .gitignore
git -C "$GIT_LINE" commit -qm base
printf 'strict\n' > "$GIT_LINE/.repo-memory-kit/governance"
printf '%s\n' '{"version":1,"rules":[{"id":"LINE-001","require":["min_route:standard"],"match":{"added_lines_regex":["danger"]}}]}' > "$GIT_LINE/.repo-memory-kit/governance.json"
printf '+++danger\n' > "$GIT_LINE/src/risky.txt"
write_card standard feature local 1 1 1 '[]' '["src/**"]'
python3 "$KIT/route-eval" "$GIT_LINE" --card "$T/card.json" --git-ref HEAD --json > "$T/out.json"
python3 -c "import json; d=json.load(open('$T/out.json')); assert d['governance']['matched_rules'][0]['rule_id']=='LINE-001'" \
    && ok "以 + 开头的新增内容仍参与治理正则" || bad "新增内容被误判成 diff 文件头"

# 治理规则可以为命中的最终 diff 设置路线下限、审查深度和专项检查。
cat > "$REPO/.repo-memory-kit/governance.json" <<'EOF'
{
  "version": 1,
  "rules": [{
    "id": "DB-001",
    "require": ["receipt", "independent_review", "min_route:standard", "required_check:migration-plan", "required_check:performance-review"],
    "match": {"files": ["*.sql"]}
  }]
}
EOF
cat > "$T/db.diff" <<'EOF'
diff --git a/db/schema.sql b/db/schema.sql
--- a/db/schema.sql
+++ b/db/schema.sql
@@ -1 +1,2 @@
 old
+ALTER TABLE orders ADD COLUMN exported_at timestamp;
EOF
write_card bounded feature local 1 1 1 '[]' '["db/schema.sql"]'
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --diff "$T/db.diff" --json
assert_eq "治理规则可提升路线下限" "$rc" "2"
python3 "$KIT/route-eval" "$REPO" --card "$T/card.json" --diff "$T/db.diff" --json > "$T/out.json" 2>/dev/null || true
python3 - "$T/out.json" <<'PY' \
    && ok "治理动作映射到路线/审查/检查" \
    || bad "治理动作未进入路线结果"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["minimum_route"] == "standard"
assert d["effective_route"] == "standard"
assert d["requirements"]["review_depth"] == "thorough"
assert "impact-analysis" in d["requirements"]["required_checks"]
assert "migration-plan" in d["requirements"]["required_checks"]
assert "performance-review" in d["requirements"]["required_checks"]
assert d["governance"]["matched_rules"][0]["rule_id"] == "DB-001"
PY

# 非法卡片 fail-closed；--init 输出可直接作为 schema 起点。
printf '{"version":1,"route":"bounded"}\n' > "$T/bad-card.json"
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/bad-card.json"
assert_eq "不完整 Route Card fail-closed" "$rc" "3"
write_card standard docs none 1 1 1 '[]' '["README.md"]'
python3 - "$T/card.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["route_override"] = {"kind": "project_required", "reason": "项目要求"}
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json"
assert_eq "项目级超配必须给出规则来源" "$rc" "3"
write_card bounded feature local 1 1 1 '[]' '["../outside.py"]'
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json"
assert_eq "planned_paths 拒绝绝对/逃逸路径" "$rc" "3"
printf '{broken\n' > "$REPO/.repo-memory-kit/governance.json"
write_card bounded feature local 1 1 1 '[]' '["src/x.py"]'
run_rc python3 "$KIT/route-eval" "$REPO" --card "$T/card.json"
assert_eq "计划阶段也校验治理策略并 fail-closed" "$rc" "3"
python3 "$KIT/route-eval" --init > "$T/init.json"
python3 -c "import json; d=json.load(open('$T/init.json')); assert d['version']==1 and d['route']=='bounded' and d['footprint']['planned_paths']==[] and d['route_override'] is None" \
    && ok "--init 输出合法 Route Card" || bad "--init 模板非法"

echo
echo "route-eval 测试: $pass 通过, $fail 失败"
[ "$fail" = 0 ]
