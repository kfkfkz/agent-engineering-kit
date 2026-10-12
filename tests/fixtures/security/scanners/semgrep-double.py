"""Own executable protocol double; it performs no SAST."""
import json
from pathlib import Path
import sys

if sys.argv[1:] == ["--version"]:
    print("1.180.0")
    raise SystemExit(0)
assert sys.argv[1:10] == ["scan", "--json", "--error", "--metrics", "off", "--disable-version-check", "--disable-nosem", "--config", sys.argv[9]]
assert len(sys.argv) == 11
assert sorted(p.name for p in Path.cwd().iterdir()) == ["app.py"]
print(json.dumps({"version": "1.180.0", "results": [{"check_id": "own-test-rule", "path": "app.py", "start": {"line": 1}, "end": {"line": 1}, "extra": {"severity": "ERROR"}}], "errors": [], "paths": {"scanned": ["app.py"], "skipped": []}, "skipped_rules": []}))
raise SystemExit(1)
