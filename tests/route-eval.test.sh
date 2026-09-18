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
