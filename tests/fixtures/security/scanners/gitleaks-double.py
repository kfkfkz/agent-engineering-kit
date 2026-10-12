"""Own synthetic secret double; only simulated data is emitted."""
import json
from pathlib import Path
import sys

if sys.argv[1:] == ["version"]:
    print("8.30.1")
    raise SystemExit(0)
assert sys.argv[1:4] == ["dir", "--config", sys.argv[3]]
assert sys.argv[4:8] == ["--report-format", "json", "--report-path", sys.argv[7]]
assert sys.argv[8:13] == ["--redact=100", "--exit-code", "10", "--ignore-gitleaks-allow", "--no-banner"]
assert len(sys.argv) == 14
assert sorted(p.name for p in Path.cwd().iterdir()) == ["app.py"]
Path(sys.argv[7]).write_text(json.dumps([{"RuleID": "own-test-rule", "File": "app.py", "StartLine": 1, "EndLine": 1, "Match": "key=SIMULATED_SECRET_VALUE", "Secret": "SIMULATED_SECRET_VALUE"}]))
print("SIMULATED_SECRET_VALUE", file=sys.stderr)
raise SystemExit(10)
