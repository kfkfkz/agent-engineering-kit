"""Exercise task boundaries and selective receipt reuse through the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from aek.application.delivery_gate import compose_delivery_gate
from aek.application.review_evidence import ReviewEvidencePlan
from aek.core.context.identity import IdentityComparison, IdentityState


KIT = Path(__file__).resolve().parents[1]


class TaskScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.repo = self.state / "项目"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.put("page/filter.py", "old\n")
        self.put("shared/defaults.py", "original\n")
        self.put("query.sql", "SELECT id FROM items\n")
        self.put("user.txt", "prior\n")
        self.git("add", "--all")
        self.git("commit", "-qm", "base")
        self.card_path = self.state / "card.json"
        self.scope = self.state / "scope.json"
        self.card = self.tool("route-eval", "--init")[1]
        self.card["footprint"]["planned_paths"] = ["page/*", "tests/test_filter.py"]
        self.write_card()

    def put(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, capture_output=True,
                       check=True, timeout=20)

    def tool(self, name: str, *args: str, expect: int = 0) -> tuple[int, dict]:
        proc = subprocess.run([sys.executable, str(KIT / name), *args],
                              cwd=self.repo, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, expect, (proc.stdout, proc.stderr))
        return proc.returncode, json.loads(proc.stdout) if proc.stdout else {}

    def write_card(self) -> None:
        self.card_path.write_text(json.dumps(self.card), encoding="utf-8")

    def route(self, *args: str, expect: int = 0) -> dict:
        return self.tool("route-eval", str(self.repo), "--card", str(self.card_path),
                         "--json", *args, expect=expect)[1]

    def start(self) -> None:
        self.route("--start-scope", str(self.scope), "--confirmation", "active-task")

    def evaluate(self, *args: str, expect: int = 0) -> dict:
        return self.route("--scope", str(self.scope), *args, expect=expect)

    def test_local_fix_and_tests_use_original_scope_prior_changes_are_excluded(self) -> None:
        self.put("user.txt", "user staged\n")
        self.git("add", "user.txt")
        self.put("user.txt", "user unstaged\n")
        self.put("existing.txt", "user untracked\n")
        self.start()
        self.assertEqual(self.evaluate("--propose", "page/filter.py",
                                      "--propose", "tests/test_filter.py")["scope"]["phase"],
                         "preflight")
        self.put("page/filter.py", "fixed\n")
        self.put("tests/test_filter.py", "regression\n")
        self.git("add", "page/filter.py")
        self.assertEqual(self.evaluate()["changed_files"],
                         ["page/filter.py", "tests/test_filter.py"])
        self.put("user.txt", "user changed again\n")
        result = self.evaluate(expect=2)
        self.assertEqual(result["scope"]["overlapping_paths"], ["user.txt"])
        self.assertIn("SCOPE_OWNERSHIP_UNKNOWN", result["route_verdict"]["reason_codes"])

    def test_path_and_behavior_drift_survive_route_override(self) -> None:
        self.start()
        result = self.evaluate("--propose", "shared/defaults.py", expect=2)
        self.assertEqual(result["unplanned_files"], ["shared/defaults.py"])
        self.assertIn("SCOPE_CONFIRMATION_REQUIRED", self.evaluate(
            "--propose", "page/filter.py", "--material-change", expect=2
        )["route_verdict"]["reason_codes"])
        self.put("shared/defaults.py", "global expansion\n")
        self.git("add", "--", "shared/defaults.py")
        self.put("shared/defaults.py", "original\n")
        self.card["footprint"]["planned_paths"].append("shared/defaults.py")
        self.card["route"] = "standard"
        self.card["route_override"] = {"kind": "user_requested", "reason": "more review"}
        self.write_card()
        self.assertEqual(self.evaluate(expect=2)["unplanned_files"], ["shared/defaults.py"])
        dest = self.state / "late.json"
        self.evaluate("--amend-scope", str(dest), "--confirmation", "late approval", expect=2)
        self.assertFalse(dest.exists())

    def test_large_prior_file_and_generated_state_do_not_block_local_edit(self) -> None:
        artifact = self.repo / "local-sdk.jar"
        artifact.write_bytes(b"a" * 2_100_000)
        self.start()
        self.put(".repo-memory-kit/validation/live.log", "local tool output\n")
        report = self.evaluate("--propose", "page/filter.py")
        self.assertEqual(report["changed_files"], ["page/filter.py"])
        self.git("add", ".repo-memory-kit/validation/live.log")
        self.assertEqual(self.evaluate(expect=2)["unplanned_files"],
                         [".repo-memory-kit/validation/live.log"])
        self.git("rm", "--cached", ".repo-memory-kit/validation/live.log")
        with artifact.open("r+b") as stream:
            stream.seek(2_000_001)
            stream.write(b"b")
        self.assertEqual(self.evaluate(expect=2)["scope"]["overlapping_paths"],
                         ["local-sdk.jar"])

    def test_prior_large_tracked_deletion_does_not_expand_binary_patch(self) -> None:
        artifact = self.repo / "tracked-sdk.jar"
        rng = random.Random(0)
        with artifact.open("wb") as stream:
            stream.write(b"\0")
            for _ in range(27):
                stream.write(rng.randbytes(1024 * 1024))
        self.git("add", "tracked-sdk.jar")
        self.git("commit", "-qm", "add binary fixture")
        artifact.unlink()
        self.start()
        self.assertEqual(self.evaluate()["changed_files"], [])

    def test_prospective_amendment_keeps_origin_and_applies_governance(self) -> None:
        self.put("user.txt", "user edit\n")
        marker = ".repo-memory-kit/governance"
        self.put(marker, "lightweight\n")
        self.put(".repo-memory-kit/governance.json", json.dumps({
            "version": 1,
            "rules": [{"id": "database", "match": {"files": ["*.sql"]},
                       "require": ["independent_review"]}],
        }))
        self.start()
        self.card["footprint"]["planned_paths"].append("query.sql")
        self.write_card()
        new = self.state / "amended.json"
        self.evaluate("--amend-scope", str(new), "--confirmation", "confirmed query change")
        self.scope = new
        decision = self.evaluate("--propose", "query.sql")
        self.assertIn("independent", decision["governance"]["risk_requirements"]["reviews"])
        self.put("query.sql", "SELECT id FROM items ORDER BY id\n")
        result = self.evaluate()
        self.assertEqual(result["changed_files"], ["query.sql"])
        self.assertIn("independent", result["governance"]["risk_requirements"]["reviews"])
        review = ReviewEvidencePlan((), (), (), "high", True)
        identity = IdentityComparison(IdentityState.VALID)
        gate = compose_delivery_gate(
            route_result=result, identity=identity, review_plan=review,
            evidence_by_requirement={})
        self.assertIn("reviews:independent", gate["pending_requirements"])
        self.assertEqual(gate["verdict"], "NEEDS_HUMAN_REVIEW")
        self.put("shared/defaults.py", "unconfirmed global change\n")
        gate = compose_delivery_gate(
            route_result=self.evaluate(expect=2), identity=identity, review_plan=review,
            evidence_by_requirement={})
        self.assertEqual(gate["verdict"], "NOT_READY")
        self.assertIn("ROUTE_OR_SCOPE_NOT_READY", gate["reason_codes"])

    def test_rename_captures_old_and_new_paths_and_switch_invalidates_scope(self) -> None:
        self.start()
        self.git("mv", "shared/defaults.py", "page/defaults.py")
        self.assertEqual(self.evaluate(expect=2)["unplanned_files"], ["shared/defaults.py"])
        self.git("checkout", "-qb", "another-branch")
        self.evaluate(expect=3)

    def test_unborn_git_worktree_and_invalid_paths(self) -> None:
        self.git("checkout", "--orphan", "unborn")
        self.start()
        self.assertTrue(self.evaluate("--propose", "tests/test_filter.py")["ok"])
        self.evaluate("--propose", "../outside", expect=3)

    def test_installed_tools_do_not_add_scope_drift_to_an_unborn_repo(self) -> None:
        self.git("checkout", "--orphan", "unborn")
        config = str(self.state / "config")
        env = {**os.environ, "XDG_CONFIG_HOME": config, "LOCALAPPDATA": config}
        install = subprocess.run([sys.executable, "-m", "installer", str(self.repo)],
                                 cwd=KIT, env=env, capture_output=True, timeout=30)
        self.assertEqual(install.returncode, 0, (install.stdout, install.stderr))
        bin_dir = self.repo / ".repo-memory-kit" / "bin"
        route = str(bin_dir / "route-eval")
        args = (str(self.repo), "--card", str(self.card_path), "--json")
        self.tool(route, *args, "--start-scope", str(self.scope), "--confirmation", "test")
        self.tool(str(bin_dir / "validation-run"), "--repo", str(self.repo), "--fingerprint")
        result = self.tool(route, *args, "--scope", str(self.scope),
                           "--propose", "tests/test_filter.py")[1]
        self.assertEqual(result["changed_files"], ["tests/test_filter.py"])

    def test_reuse_runs_each_current_check_only_once(self) -> None:
        self.start()
        self.put("page/filter.py", "fixed\n")
        self.assertTrue(self.evaluate()["ok"])
        metadata = self.state / "current.json"
        metadata.write_text('{"environment":"test","dataset":"fixture-a"}', encoding="utf-8")
        paths = ("page/filter.py", "query.sql")

        def run(i: int, *options: str, expect: int = 0) -> dict:
            counter = self.state / f"runs-{i}"
            script = ("from pathlib import Path; import sys; "
                      "p=Path(sys.argv[1]); p.open('ab').write(b'1'); print('ok')")
            if i == 1:
                script += (
                    "; import sqlite3; db=sqlite3.connect(':memory:'); "
                    "db.executescript('CREATE TABLE items(id); INSERT INTO items VALUES (1),(2);'); "
                    "calls=[]; db.set_trace_callback(calls.append); "
                    "assert db.execute(Path('query.sql').read_text(encoding='utf-8')).fetchall() == [(1,), (2,)]; "
                    "assert len(calls) == 1"
                )
            return self.tool("validation-run", "--repo", str(self.repo),
                             "--scope", str(metadata), "--input", paths[i],
                             "--log", str(self.state / f"log-{i}"),
                             "--receipt", str(self.state / f"receipt-{i}"), *options,
                             "--", sys.executable, "-c", script, str(counter), expect=expect)[1]

        for i in range(2):
            self.assertEqual(run(i)["status"], "PASS")
            for _ in ("review", "delivery"):
                self.assertTrue(run(i, "--reuse")["reused"])
        self.put("query.sql", "SELECT id FROM items LIMIT 1\n")
        self.assertTrue(run(0, "--reuse")["reused"])
        self.assertEqual(run(1, "--reuse", expect=2)["reason_code"], "EVIDENCE_SCOPE_MISMATCH")
        for i in range(2):
            self.assertEqual((self.state / f"runs-{i}").read_bytes(), b"1")
        stamp = self.tool("validation-run", "--repo", str(self.repo),
                          "--input", paths[0], "--fingerprint")[1]["scope_digest"]
        self.git("checkout", "-qb", "sibling")
        self.tool("validation-run", "--repo", str(self.repo), "--input", paths[0],
                  "--verify-fingerprint", stamp, expect=2)


if __name__ == "__main__":
    unittest.main()
