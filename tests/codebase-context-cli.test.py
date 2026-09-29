#!/usr/bin/env python3
"""codebase-context persists one refresh attempt per dirty epoch."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class CodebaseContextCliTests(unittest.TestCase):
    def call(self, action: str, repo: Path, value: dict):
        result = subprocess.run(
            [str(ROOT / "codebase-context"), action, str(repo),
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


if __name__ == "__main__":
    unittest.main()
