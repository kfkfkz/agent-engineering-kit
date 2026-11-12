#!/usr/bin/env python3
"""codebase-context persists one refresh attempt per dirty epoch."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class CodebaseContextCliTests(unittest.TestCase):
    def call(self, action: str, repo: Path, value: dict):
        result = subprocess.run(
            [sys.executable, str(ROOT / "codebase-context"), action, str(repo),
             "--context-json", json.dumps(value)], text=True,
            capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_mark_plan_observe_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp); (repo / ".repo-memory-kit").mkdir()
            scope = {"worktree_id": "wt", "work_unit_id": "wu"}
            state = self.call("mark-dirty", repo, {**scope,
                "previous_identity_digest": "old", "identity_digest": "new",
                "batch_id": "b1", "paths": ["a.py", "b.py"],
                "reason": "git_update"})
            plan = self.call("plan", repo, {**scope, "identity_digest": "new",
                "query": "who calls service", "candidate_paths": ["c.py"],
                "capabilities": {"configured": True, "visible": True,
                    "indexed": True, "fresh": False}})
            self.assertEqual(plan["required_paths"], ["a.py", "b.py", "c.py"])
            observation = {"canonical_root": str(repo),
                "expected_root": str(repo), "generation": "g1",
                "identity_digest": "old", "covered_paths": plan["required_paths"],
                "metadata_match_paths": []}
            first = self.call("observe", repo, {**scope, "plan": plan,
                                                "observation": observation})
            second = self.call("observe", repo, {**scope, "plan": plan,
                                                 "observation": observation})
            status = self.call("status", repo, scope)
            self.assertEqual(first["required_action"], "REFRESH_ONCE")
            self.assertEqual(second["required_action"], "BOUNDED_SOURCE")
            self.assertEqual(status["state"]["refresh_started_epoch"],
                             state["dirty_epoch"])

    def test_first_refresh_and_checkout_change_each_reserve_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            request = {"worktree_id": "wt", "work_unit_id": "wu", "query": "谁调用 TenantFilter",
                       "identity_digest": "first",
                       "capabilities": {"configured": True, "visible": True,
                                        "indexed": False, "fresh": False}}
            with ThreadPoolExecutor(max_workers=4) as executor:
                plans = executor.map(lambda _: self.call("plan", repo, request), range(4))
                self.assertCountEqual(
                    [plan["required_action"] for plan in plans],
                    ["REFRESH_ONCE", "BOUNDED_SOURCE", "BOUNDED_SOURCE", "BOUNDED_SOURCE"],
                )
            request["identity_digest"] = "second"
            self.assertEqual(self.call("plan", repo, request)["required_action"], "REFRESH_ONCE")
            self.assertEqual(self.call("plan", repo, request)["required_action"], "BOUNDED_SOURCE")

    @unittest.skipUnless(shutil.which("git"), "Git worktree fixture")
    def test_resolve_selects_nested_index_inside_actual_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            main, tenant = root / "main", root / "tenant"
            main.mkdir()

            def git(repo, *args):
                return subprocess.check_output(
                    ["git", "-C", str(repo), "-c", "user.email=test@example.invalid",
                     "-c", "user.name=Fixture", *args], text=True, stderr=subprocess.STDOUT).strip()

            git(main, "init")
            git(main, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
            git(main, "worktree", "add", "-b", "feature/tenant", str(tenant))
            for checkout in (main, tenant):
                (checkout / "backend").mkdir()
            projects = {"projects": [
                {"name": "main", "root_path": str(main / "backend")},
                {"name": "tenant", "root_path": str(tenant / "backend")},
            ]}
            selected = self.call("resolve", tenant / "backend", projects)
            self.assertEqual(selected["project"], "tenant")
            self.assertEqual(selected["worktree_root"], str(tenant))
            self.assertEqual(selected["branch"], "feature/tenant")
            self.assertEqual(selected["head"], git(tenant, "rev-parse", "HEAD"))
            status = {"project": "tenant", "status": "ready", "root_path": str(tenant / "backend"),
                      "git": {"head_sha": selected["head"], "worktree_root": str(tenant),
                              "canonical_root": str(main)}}
            observed = self.call("resolve", tenant / "backend", {**projects, "index_status": status})
            self.assertTrue(observed["status_matches"])
            status["git"]["worktree_root"] = str(main)
            rejected = self.call("resolve", tenant / "backend", {**projects, "index_status": status})
            self.assertEqual(rejected["reason_code"], "PROJECT_STATUS_MISMATCH")
            root_only = self.call("resolve", tenant, projects)
            self.assertEqual(root_only["reason_code"], "PROJECT_SCOPE_REQUIRED")
            self.assertEqual(root_only["nested_scopes"], [str(tenant / "backend")])
            self.assertFalse((tenant / ".repo-memory-kit").exists())


if __name__ == "__main__":
    unittest.main()
