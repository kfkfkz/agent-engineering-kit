"""Own executable contract double; no vulnerability detection or real DB."""
import json
from pathlib import Path
import sys

if sys.argv[1:] == ["--version"]:
    print("Version: 0.75.0")
    raise SystemExit(0)
expected = ["fs", "--scanners", "vuln", "--format", "json", "--list-all-pkgs", "--offline-scan", "--skip-db-update", "--skip-java-db-update", "--skip-version-check", "--disable-telemetry", "--exit-code", "10", "--cache-dir"]
assert sys.argv[1:15] == expected
assert sys.argv[16] == "--config"
assert sorted(p.name for p in Path.cwd().iterdir()) == ["app.py"]
assert Path(sys.argv[15], "db/trivy.db").read_bytes() == b"synthetic-db"
print(json.dumps({"SchemaVersion": 2, "Results": [{"Target": "app.py", "Type": "npm", "Class": "lang-pkgs", "Packages": [{"Name": "example", "Version": "1.0"}], "Vulnerabilities": [{"VulnerabilityID": "CVE-2026-0001", "PkgName": "example", "InstalledVersion": "1.0", "Severity": "HIGH"}]}]}))
raise SystemExit(10)
