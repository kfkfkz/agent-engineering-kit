#!/usr/bin/env python3
"""Run every root Python behavior suite without a hand-maintained allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def discover(test_root: Path) -> tuple[Path, ...]:
    return tuple(sorted(test_root.glob("*.test.py"), key=lambda path: path.name))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--suite", action="append", default=[], metavar="NAME.test.py",
                        help="Run a specific discovered suite (repeatable)")
    parser.add_argument("--summary-log", type=Path, help="Save full output to a new file and emit a JSON summary")
    args = parser.parse_args(argv)
    if args.list and args.summary_log is not None:
        parser.error("--list cannot be combined with --summary-log")

    test_root = Path(__file__).resolve().parent
    repo = test_root.parent
    suites = discover(test_root)
    if not suites:
        print("no Python behavior suites discovered", file=sys.stderr)
        return 2
    if args.suite:
        requested = set(args.suite)
        unknown = requested - {suite.name for suite in suites}
        if unknown:
            parser.error("unknown suite: " + ", ".join(sorted(unknown)))
        suites = tuple(suite for suite in suites if suite.name in requested)
    if args.list:
        for suite in suites:
            print(suite.relative_to(repo).as_posix())
        return 0

    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo) + (os.pathsep + existing if existing else "")
    if args.summary_log is not None:
        try:
            with args.summary_log.open("x+b") as log:
                failures = run_suites(repo, suites, env, log)
                log.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: log.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            print(f"cannot write or verify suite log: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": "FAIL" if failures else "PASS",
                          "passed": len(suites) - len(failures),
                          "failures": [{"suite": name, "exit_code": rc} for name, rc in failures],
                          "full_result_ref": str(args.summary_log.absolute()),
                          "result_digest": digest.hexdigest()}, ensure_ascii=False))
        return 1 if failures else 0

    failures = run_suites(repo, suites, env)
    if failures:
        for name, returncode in failures:
            print(f"FAILED {name} (exit {returncode})", file=sys.stderr)
        return 1
    print(f"Python behavior suites passed: {len(suites)}")
    return 0


def run_suites(repo, suites, env, log=None):
    failures: list[tuple[str, int]] = []
    for index, suite in enumerate(suites, 1):
        name = suite.relative_to(repo).as_posix()
        progress = f"[{index}/{len(suites)}] {name}"
        if log is None:
            print(progress, flush=True)
        else:
            log.write((progress + "\n").encode("utf-8"))
            log.flush()
        completed = subprocess.run(
            [sys.executable, str(suite)], cwd=repo, env=env, check=False,
            stdout=log, stderr=subprocess.STDOUT if log is not None else None)
        if completed.returncode != 0:
            failures.append((name, completed.returncode))
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
