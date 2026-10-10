"""Materialize selected public assets into a fixed, independently checked scenario."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from adapters.check_runner import _read_script
from adapters.scenario_file import LoadedScenario, load_scenario
from adapters.workspace import PreparationError, _git
from core.scenario import MAX_SCENARIO_BYTES

_ROOT = Path(__file__).resolve().parent
_CASES = {
    "SC-001": {
        "name": "Pagination boundary", "category": "simple-bug",
        "prompt": "Fix app.py pagination. Given non-negative offset and limit, return at most limit "
                  "items beginning at offset. Preserve item values, support empty input, zero limit "
                  "and offset beyond the end. The public CLI takes one JSON argument and emits JSON.",
    },
    "SC-002": {
        "name": "CRUD catalog", "category": "crud",
        "prompt": "Implement app.py's batch CRUD protocol in memory. A JSON argument contains operations. "
                  "Create accepts a non-empty string name and allocates increasing IDs starting at 1. "
                  "Get/update return id and name; delete returns deleted=id. Missing IDs return "
                  "error=NOT_FOUND. Empty or non-string names return error=INVALID_NAME without mutation "
                  "or consuming an ID. Unknown operations return error=UNKNOWN_OPERATION. Emit the "
                  "ordered response list; prior responses must not mutate after later operations.",
    },
    "SC-003": {
        "name": "Concurrent reservation", "category": "concurrency",
        "prompt": "Fix app.py's concurrent reservation. A batch contains 0 to 16 string keys. "
                  "Requests can arrive simultaneously. Exactly one request per distinct key may "
                  "succeed; emit accepted=count and committed=sorted distinct keys. Keep the public "
                  "JSON CLI and the concurrent test harness. Do not report duplicate successes.",
    },
    "SC-004": {
        "name": "SQLite compatible migration", "category": "schema-migration",
        "prompt": "Fix app.py migration for a synthetic SQLite database provided as JSON db path. "
                  "Legacy users has id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL. "
                  "Add display_name TEXT NOT NULL DEFAULT '' and backfill existing rows from name; preserve "
                  "legacy columns, IDs, rows and uniqueness. Existing INSERT(name,email) must remain valid. "
                  "Set user_version=2 and allow repeat execution without resetting display_name. "
                  "With fail_after_schema=true on a v1 database, return error=INJECTED_FAILURE and roll "
                  "back the whole schema/data/version change. Otherwise return version=2.",
    },
    "SC-005": {
        "name": "Historical external-key contract", "category": "memory-retention",
        "prompt": "Fix app.py external-key handling according to the binding historical decision in "
                  "docs/memory/external-keys.md. The CLI takes a non-empty string key in a JSON argument "
                  "and emits a JSON key field. Preserve compatibility with the existing integration.",
        "fixture_files": ("app.py", "docs/memory/external-keys.md"),
        "checks": (("behavior", "outcome", "assertions.inc"), ("history", "memory", "history.inc")),
    },
    "SC-006": {
        "name": "Queryable external commit recovery", "category": "recovery",
        "prompt": "Fix app.py's synthetic external-service recovery. Each process receives JSON service "
                  "(a loopback HTTP URL), key and optional crash_after_commit. GET /status?key=... returns "
                  "receipt=null or the prior receipt. POST /commit with JSON key creates a fresh, non-idempotent "
                  "commit and returns receipt. Query before committing, reuse an existing receipt without POST, "
                  "and emit receipt/reused. If a new commit is followed by crash_after_commit=true, emit "
                  "error=INTERRUPTED without a receipt. A later new process must query and reuse that commit.",
    },
}


def reference_files(scenario_id: str) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(scenario_id, str) or scenario_id not in _CASES:
        raise PreparationError("UNKNOWN_PUBLIC_SCENARIO")
    return (("app.py", _read_script(_ROOT, f"{scenario_id}/expected/good/app.py")),)


def materialize_scenario(scenario_id: str, output_parent: Path) -> LoadedScenario:
    if not isinstance(scenario_id, str) or scenario_id not in _CASES:
        raise PreparationError("UNKNOWN_PUBLIC_SCENARIO")
    output_parent = Path(output_parent).absolute()
    if output_parent.is_symlink() or not output_parent.is_dir():
        raise PreparationError("OUTPUT_DIRECTORY_INVALID")
    # Select and snapshot the trusted public definitions before creating any workspace.
    case = _CASES[scenario_id]
    sources = [(name, _read_script(_ROOT, f"{scenario_id}/fixture/{name}"))
               for name in case.get("fixture_files", ("app.py",))]
    protocol = _read_script(_ROOT, "verify_protocol.py")
    scripts = []
    for check_id, kind, fragment in case.get("checks", (("behavior", "outcome", "assertions.inc"),)):
        check = protocol + b"\n" + _read_script(_ROOT, f"{scenario_id}/checks/{fragment}")
        if len(check) > MAX_SCENARIO_BYTES:
            raise PreparationError("PUBLIC_CHECK_SIZE_EXCEEDED")
        compile(check, "public-verifier", "exec")
        scripts.append((check_id, kind, check))
    root = Path(tempfile.mkdtemp(prefix=f"aek-{scenario_id.lower()}-", dir=output_parent.resolve()))
    fixture, checks = root / "fixture", root / "checks"
    fixture.mkdir(mode=0o700)
    checks.mkdir(mode=0o700)
    for name, content in sources:
        path = fixture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)
    for check_id, _kind, content in scripts:
        (checks / f"{check_id}.py").write_bytes(content)
    _git(fixture, "init", "--template=", "--initial-branch=benchmark")
    _git(fixture, "add", "--force", "--all", "--", ".")
    _git(fixture, "commit", "-m", "Pinned benchmark snapshot")
    commit = _git(fixture, "rev-parse", "HEAD").decode("ascii").strip()
    fixture_manifest = {"files": [{"path": name, "mode": "100644",
                                   "sha256": hashlib.sha256(content).hexdigest()}
                                  for name, content in sorted(sources)]}
    digest = hashlib.sha256(json.dumps(fixture_manifest, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    value = {
        "schema_version": 1, "id": scenario_id, "name": case["name"], "category": case["category"],
        "task": {"prompt": case["prompt"]},
        "fixture": {"path": "fixture", "commit": commit, "sha256": digest},
        "execution": {"agents": ["codex", "claude"], "variants": ["vanilla", "aek"],
                      "timeout_seconds": 60, "repeat": 1},
        "evaluation": {
            "checks": [{"id": check_id, "entrypoint": f"checks/{check_id}.py",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "argv": ["{python}", "{check}", "{workspace}"],
                        "timeout_seconds": 10, "kind": kind} for check_id, kind, content in scripts],
            "allowed_paths": ["app.py"], "forbidden_paths": ["checks/**", "expected/**", "docs/memory/**"],
        },
    }
    path = root / "scenario.json"
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return load_scenario(path)
