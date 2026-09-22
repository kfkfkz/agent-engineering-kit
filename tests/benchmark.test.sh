#!/bin/sh
# benchmark evaluate.py 回归：NUL 路径解析 + “真实新增断言”识别。
set -eu
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

python3 - "$KIT" "$T/repo" <<'PY'
import importlib.util
import subprocess
import sys
from pathlib import Path

kit, repo = Path(sys.argv[1]), Path(sys.argv[2])
repo.mkdir()
subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
test_file = repo / "tests" / "test_api.py"
test_file.parent.mkdir()
baseline = "def helper():\n    return 1\n"
test_file.write_text(baseline, encoding="utf-8")
subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
base = subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

spec = importlib.util.spec_from_file_location(
    "benchmark_evaluate", kit / "tests" / "benchmark" / "evaluate.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

test_file.write_text(
    baseline + "# regression test documentation only: test('not executable')\n",
    encoding="utf-8")
assert module._regression_test_files(repo, base) == [], (
    "comment-only test edit must not count as a regression test")

test_file.write_text(
    baseline + "\ndef test_regression():\n    assert helper() == 1\n", encoding="utf-8")
assert module._regression_test_files(repo, base) == ["tests/test_api.py"]

test_file.write_text(baseline, encoding="utf-8")
(repo / "tests" / "test_empty.py").write_bytes(b"")
assert module._regression_test_files(repo, base) == [], (
    "empty new test file must not count")

shell_test = repo / "tests" / "regression.test.sh"
shell_test.write_text(
    "assert_eq 'regression result' \"$actual\" expected\n", encoding="utf-8")
assert module._regression_test_files(repo, base) == ["tests/regression.test.sh"]
shell_test.unlink()

odd = repo / "src" / "odd\nname.py"
odd.parent.mkdir()
odd.write_text("value = 1\n", encoding="utf-8")
changed = module._get_changed_files(repo, base)
assert "src/odd\nname.py" in changed, changed
print("benchmark evaluator: 5 assertions passed")
PY

PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/context-telemetry.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/context-service.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/memory-recall-service.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/context-budget.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/context-memory.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/context-capsule.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/context-capsule-store.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/cli-service-parity.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/benchmark/complete_context.test.py"
PYTHONPATH="$KIT${PYTHONPATH:+:$PYTHONPATH}" python3 "$KIT/tests/benchmark/performance.test.py"

# 1.0.1 T1：先冻结可复现的 Context 基线与发布比较语义。
python3 - "$KIT" <<'PY'
import importlib.util
import dataclasses
import json
import sys
from pathlib import Path

kit = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "context_benchmark", kit / "tests" / "benchmark" / "context.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

manifest = module.load_baseline(
    kit / "tests" / "benchmark" / "context-baseline-v1.0.0.json")
module.verify_baseline_materials(manifest, kit)
first_case = manifest.cases[0]
tampered_material = dataclasses.replace(first_case.materials[0], sha256="0" * 64)
tampered_case = dataclasses.replace(first_case, materials=(tampered_material,))
tampered_manifest = dataclasses.replace(
    manifest, cases=(tampered_case,) + manifest.cases[1:])
try:
    module.verify_baseline_materials(tampered_manifest, kit)
except module.BaselineError:
    pass
else:
    raise AssertionError("historical material mismatch accepted")
assert manifest.schema_version == 1
assert manifest.scope == "selected_skill_material_only"
assert manifest.baseline_commit == "88944283eb8030b4c44363f21f5ef0110252e1fa"
assert {case.route for case in manifest.cases} == {
    "direct", "bounded", "standard", "initiative"
}
assert all(case.cold_cache for case in manifest.cases)
assert all(case.baseline_bytes > 0 and case.baseline_chars > 0
           for case in manifest.cases)

direct = next(case for case in manifest.cases if case.route == "direct")
equivalent = module.EquivalenceReceipt(
    task=True, security=True, coverage=True, exit_codes=True, diagnostics=True,
    receipt_digest="a" * 64,
)

met = module.compare_case(direct, {
    "coverage": "complete",
    "case_id": direct.case_id,
    "route": "direct",
    "baseline_commit": manifest.baseline_commit,
    "fixture_digest": direct.fixture_digest,
    "diff_digest": direct.diff_digest,
    "environment_digest": direct.environment_digest,
    "cold_cache": True,
    "delivered_bytes": direct.baseline_bytes // 2,
    "delivered_chars": direct.baseline_chars,
}, equivalent)
assert met.verdict == "THRESHOLD_MET", met
assert met.reduction_bytes >= 0.50

regression = module.compare_case(direct, {
    "coverage": "complete",
    "case_id": direct.case_id,
    "route": "direct",
    "baseline_commit": manifest.baseline_commit,
    "fixture_digest": direct.fixture_digest,
    "diff_digest": direct.diff_digest,
    "environment_digest": direct.environment_digest,
    "cold_cache": True,
    "delivered_bytes": direct.baseline_bytes,
    "delivered_chars": direct.baseline_chars,
}, equivalent)
assert regression.verdict == "REGRESSION", regression

partial = module.compare_case(direct, {
    "coverage": "partial",
    "case_id": direct.case_id,
    "route": "direct",
    "baseline_commit": manifest.baseline_commit,
    "fixture_digest": direct.fixture_digest,
    "diff_digest": direct.diff_digest,
    "environment_digest": direct.environment_digest,
    "cold_cache": True,
    "delivered_bytes": 1,
    "delivered_chars": 1,
}, equivalent)
assert partial.verdict == "INVALID" and "coverage" in partial.reasons

not_equivalent = module.compare_case(
    direct,
    {
        "coverage": "complete",
        "case_id": direct.case_id,
        "route": "direct",
        "baseline_commit": manifest.baseline_commit,
        "fixture_digest": direct.fixture_digest,
        "diff_digest": direct.diff_digest,
        "environment_digest": direct.environment_digest,
        "cold_cache": True,
        "delivered_bytes": 1,
        "delivered_chars": 1,
    },
    module.EquivalenceReceipt(
        task=True, security=False, coverage=True, exit_codes=True,
        diagnostics=True,
        receipt_digest="b" * 64,
    ),
)
assert not_equivalent.verdict == "INVALID"
assert "equivalence" in not_equivalent.reasons

route = module.aggregate_route("direct", [met, regression])
assert route.verdict == "REGRESSION"
assert route.worst_reduction_bytes == regression.reduction_bytes

# A selected-Skill-only baseline must never be promoted to an A23 release proof.
release = module.release_verdict(manifest, (route,))
assert release.verdict == "INVALID" and "baseline_scope" in release.reasons

print("context benchmark baseline: 18 assertions passed")
PY
