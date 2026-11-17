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
    def call(self, action: str, repo: Path, value: dict, *, expected=0):
        result = subprocess.run(
            [sys.executable, str(ROOT / "codebase-context"), action, str(repo),
             "--context-json", json.dumps(value)], text=True,
            capture_output=True, check=False)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout) if expected == 0 else result.stderr

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
            self.assertEqual(first["required_action"], "REFRESH_ONCE")
            self.assertEqual(second["required_action"], "BOUNDED_SOURCE")
            self.assertTrue(second["refresh_pending"])
            self.assertEqual(self.call("status", repo, scope)["state"]["refresh_started_epoch"], -1)
            self.call("refresh-result", repo, {**scope, "outcome": "executed",
                                               "refresh_token": first["refresh_token"]})
            status = self.call("status", repo, scope)
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

    def test_expired_or_canceled_reservation_can_retry_without_repeating_executed_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            scope = {"worktree_id": "wt", "work_unit_id": "wu"}
            request = {**scope, "query": "who calls the handler", "identity_digest": "head",
                       "capabilities": {"configured": True, "visible": True,
                                        "indexed": False, "fresh": False}}
            initial = self.call("plan", repo, request)
            self.assertEqual(initial["required_action"], "REFRESH_ONCE")
            self.assertTrue(self.call("plan", repo, request)["refresh_pending"])
            state_path = repo / ".repo-memory-kit/codebase-freshness/wt/wu/refresh.json"
            persisted = json.loads(state_path.read_bytes())
            persisted["expires_at"] = 0
            state_path.write_text(json.dumps(persisted), encoding="utf-8")
            replacement = self.call("plan", repo, request)
            self.assertEqual(replacement["required_action"], "REFRESH_ONCE")
            self.assertNotEqual(replacement["refresh_token"], initial["refresh_token"])
            self.assertIn("no longer matches", self.call("refresh-result", repo, {
                **scope, "refresh_token": initial["refresh_token"], "outcome": "executed"}, expected=3))
            self.call("refresh-result", repo, {
                **scope, "refresh_token": replacement["refresh_token"], "outcome": "not_started"})
            third = self.call("plan", repo, request)
            self.assertEqual(third["required_action"], "REFRESH_ONCE")
            confirmation = {**scope, "refresh_token": third["refresh_token"], "outcome": "executed"}
            self.assertEqual(self.call("refresh-result", repo, confirmation)["refresh_started_epoch"], 0)
            self.assertEqual(self.call("refresh-result", repo, confirmation)["refresh_started_epoch"], 0)
            completed = self.call("plan", repo, request)
            self.assertEqual(completed["required_action"], "BOUNDED_SOURCE")
            self.assertNotIn("refresh_pending", completed)

    def test_pre_upgrade_reservation_can_be_recovered_when_index_is_still_unproven(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            state = repo / ".repo-memory-kit/codebase-freshness/wt/wu/state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({
                "code_identity_digest": "head", "dirty_epoch": 0, "active_batch_id": "",
                "changed_paths": [], "reasons": [], "refresh_started_epoch": 0,
            }), encoding="utf-8")
            scope = {"worktree_id": "wt", "work_unit_id": "wu"}
            request = {**scope, "query": "who calls the handler", "identity_digest": "head",
                       "capabilities": {"configured": True, "visible": True,
                                        "indexed": False, "fresh": False}}
            plan = self.call("plan", repo, request)
            self.assertEqual(plan["required_action"], "REFRESH_ONCE")
            self.call("refresh-result", repo, {
                **scope, "refresh_token": plan["refresh_token"], "outcome": "executed"})
            self.assertEqual(json.loads(state.read_bytes())["schema_version"], 2)
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
            duplicate = {"projects": [*projects["projects"],
                {"name": "tenant-alias", "root_path": str(tenant / "backend")}]}
            ambiguous = self.call("resolve", tenant / "backend", duplicate)
            self.assertEqual(ambiguous["reason_code"], "PROJECT_AMBIGUOUS")
            self.assertEqual(ambiguous["project_candidates"], ["tenant", "tenant-alias"])
            selected_alias = self.call("resolve", tenant / "backend",
                {**duplicate, "index_status": {**status, "project": "tenant-alias"}})
            self.assertEqual(selected_alias["project"], "tenant-alias")
            self.assertTrue(selected_alias["status_matches"])
            wrong_head = self.call("resolve", tenant / "backend", {
                **duplicate, "index_status": {**status, "git": {**status["git"], "head_sha": "0" * 40}}})
            self.assertEqual(wrong_head["reason_code"], "PROJECT_STATUS_MISMATCH")
            self.assertIsNone(wrong_head["project"])
            status["git"]["worktree_root"] = str(main)
            rejected = self.call("resolve", tenant / "backend", {**projects, "index_status": status})
            self.assertEqual(rejected["reason_code"], "PROJECT_STATUS_MISMATCH")
            root_only = self.call("resolve", tenant, projects)
            self.assertEqual(root_only["reason_code"], "PROJECT_SCOPE_REQUIRED")
            self.assertEqual(root_only["nested_scopes"], [str(tenant / "backend")])
            self.assertFalse((tenant / ".repo-memory-kit").exists())


if __name__ == "__main__":
    unittest.main()
