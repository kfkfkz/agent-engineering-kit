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
