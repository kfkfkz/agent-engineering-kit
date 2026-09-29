#!/usr/bin/env python3
"""Run every root Python behavior suite without a hand-maintained allowlist."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def discover(test_root: Path) -> tuple[Path, ...]:
    return tuple(sorted(test_root.glob("*.test.py"), key=lambda path: path.name))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    test_root = Path(__file__).resolve().parent
    repo = test_root.parent
    suites = discover(test_root)
    if not suites:
        print("no Python behavior suites discovered", file=sys.stderr)
        return 2
    if args.list:
        for suite in suites:
            print(suite.relative_to(repo).as_posix())
        return 0

    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo) + (os.pathsep + existing if existing else "")
    failures: list[tuple[str, int]] = []
    for index, suite in enumerate(suites, 1):
        name = suite.relative_to(repo).as_posix()
        print(f"[{index}/{len(suites)}] {name}", flush=True)
        completed = subprocess.run(
            [sys.executable, str(suite)], cwd=repo, env=env, check=False)
        if completed.returncode != 0:
            failures.append((name, completed.returncode))
    if failures:
        for name, returncode in failures:
            print(f"FAILED {name} (exit {returncode})", file=sys.stderr)
        return 1
    print(f"Python behavior suites passed: {len(suites)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
