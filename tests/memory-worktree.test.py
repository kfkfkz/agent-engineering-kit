"""Prove checkout and parent-branch isolation at the CLI and MCP boundaries."""
from __future__ import annotations

import importlib.util
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aek.adapters.memory import fallback_candidates, read_candidate, StaleCandidate
from aek.adapters.memory_scope import resolve_memory_scope


KIT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("git"), "requires Git worktrees")
class WorktreeMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.main = self.base / "trunk"
        self.tenant = self.base / "tenant"
        self.sibling = self.base / "sibling"
        self.main.mkdir()
        self.git("init", "-b", "main")
        self.note(self.main, "120", {})
        self.note(self.main, "shared-lessons", {"memory_scope": "inherit", "memory_ref": "refs/heads/main"})
        self.note(self.main, "tenant-branch", {"memory_scope": "branch", "memory_ref": "refs/heads/tenant"})
        self.git("add", "docs")
        self.git("-c", "user.email=test@example.invalid", "-c", "user.name=AEK", "commit", "-m", "fixture")
        self.git("worktree", "add", "-b", "tenant", str(self.tenant))
        self.git("worktree", "add", "-b", "sibling", str(self.sibling))
        self.note(self.tenant, "125", resolve_memory_scope(self.tenant).entry_fields())
        self.note(self.sibling, "127", resolve_memory_scope(self.sibling).entry_fields())

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.main, capture_output=True, check=True, timeout=15)

    @staticmethod
    def note(worktree, name, fields):
        path = worktree / f"docs/memory/playbooks/{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = {"type": "playbook", **fields}
        header = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
        path.write_text(f"---\n{header}\n---\n# memo {name}\nbody {name}\n", encoding="utf-8")

    def cli(self, repo, *args):
        result = subprocess.run([sys.executable, str(KIT / "memory-recall"), *args, str(repo)],
                                cwd=repo, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_scopes_are_filtered_before_fallback_ranking_and_expansion(self):
        expected = {self.main: {"120", "shared-lessons"},
                    self.tenant: {"125", "shared-lessons", "tenant-branch"},
                    self.sibling: {"127", "shared-lessons"}}
        for path, names in expected.items():
            refs = fallback_candidates(path, "memo", limit=10).candidates
            self.assertEqual({Path(ref.path).stem for ref in refs}, names)
            self.assertEqual(set(n for n in ("120", "125", "127") if f"memo {n}\n" in self.cli(path, "memo")),
                             names & {"120", "125", "127"})
        old = fallback_candidates(self.main, "120", limit=1).candidates[0]
        with self.assertRaises(StaleCandidate):
            read_candidate(self.tenant, old, "fallback")

    def test_parent_chain_is_directional(self):
        self.note(self.sibling, "sibling-lessons", {"memory_scope": "inherit", "memory_ref": "refs/heads/sibling"})
        self.note(self.tenant, "tenant-lessons", {"memory_scope": "inherit", "memory_ref": "refs/heads/tenant"})
        self.assertFalse(fallback_candidates(self.sibling, "tenant-lessons", limit=1).candidates)
        self.assertFalse(fallback_candidates(self.tenant, "sibling-lessons", limit=1).candidates)
        self.git("config", "branch.sibling.aekMemoryParent", "tenant")
        self.note(self.sibling, "tenant-lessons", {"memory_scope": "inherit", "memory_ref": "refs/heads/tenant"})
        self.assertTrue(fallback_candidates(self.sibling, "tenant-lessons", limit=1).candidates)
        self.note(self.sibling, "tenant-local", {"memory_scope": "branch", "memory_ref": "refs/heads/tenant"})
        self.assertFalse(fallback_candidates(self.sibling, "tenant-local", limit=1).candidates)

    def test_parent_experience_added_after_fork_is_available_without_sync(self):
        fields = {"memory_scope": "inherit", "memory_ref": "refs/heads/main"}
        self.note(self.main, "new-parent-lesson", fields)
        # A worktree-bound note stays private even though it is the same parent.
        self.note(self.main, "parent-environment", resolve_memory_scope(self.main).entry_fields())
        ref = fallback_candidates(self.tenant, "new-parent-lesson", limit=1).candidates[0]
        self.assertEqual(ref.origin_ref, "refs/heads/main")
        self.assertIn("body new-parent-lesson", read_candidate(self.tenant, ref, ref.generation))
        self.assertFalse(fallback_candidates(self.tenant, "parent-environment", limit=1).candidates)
        self.assertFalse(fallback_candidates(self.tenant, "127", limit=1).candidates)

    def test_switching_branch_does_not_reuse_memory_from_previous_scope(self):
        def call(*extra):
            return json.loads(self.cli(self.main, "--context-json", "--route", "bounded",
                                       "--subject", "a" * 64, "--session", "branch-switch",
                                       *extra, "shared-lessons"))
        first = call()["expanded"][0]["content_digest"]
        self.assertTrue(call("--known-digest", first)["reused"])
        self.git("switch", "-c", "scratch")
        current = call("--known-digest", first)
        self.assertFalse(current["reused"])
        self.assertEqual(current["expanded"][0]["content_digest"], first)
        self.assertFalse(fallback_candidates(self.main, "120", limit=1).candidates)

    @unittest.skipUnless(importlib.util.find_spec("zvec"), "requires local zvec")
    def test_published_index_switches_to_safe_fallback_on_branch_change(self):
        self.cli(self.main, "--rebuild")
        self.assertIn("memo 120", self.cli(self.main, "120"))
        self.git("switch", "-c", "scratch")
        self.assertNotIn("memo 120", self.cli(self.main, "120"))

    def test_mcp_rejects_wrong_or_unbound_worktree_before_lookup(self):
        for checkout in (self.main, self.tenant):
            bin_dir = checkout / ".repo-memory-kit/bin"
            bin_dir.mkdir(parents=True)
            for tool in ("agent-engineering-mcp", "memory-recall"):
                shutil.copyfile(KIT / tool, bin_dir / tool)
            shutil.copytree(KIT / "aek", bin_dir.parent / "lib/aek",
                            ignore=shutil.ignore_patterns("__pycache__"))

        def mcp(installed, cwd, **arguments):
            messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                         "protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "fixture", "version": "1"}}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                         "name": "memory_recall", "arguments": {"query": "memo", **arguments}}}]
            result = subprocess.run([sys.executable, str(installed / ".repo-memory-kit/bin/agent-engineering-mcp")],
                                    cwd=cwd, input="\n".join(map(json.dumps, messages)) + "\n",
                                    encoding="utf-8", capture_output=True, timeout=30, check=True)
            envelope = json.loads(result.stdout.splitlines()[-1])["result"]
            return json.loads(envelope["content"][0]["text"])

        main = resolve_memory_scope(self.main).digest
        tenant = resolve_memory_scope(self.tenant).digest
        for reply in (mcp(self.main, self.tenant), mcp(self.main, self.base, scope_digest=tenant)):
            self.assertEqual((reply["reason_code"], reply["lookup_started"]), ("MEMORY_SCOPE_MISMATCH", False))
        missing = mcp(self.main, self.main)
        self.assertEqual((missing["reason_code"], missing["lookup_started"]), ("MEMORY_SCOPE_REQUIRED", False))
        self.assertIn("memo 120", mcp(self.main, self.main, scope_digest=main)["output"])
        matched = mcp(self.tenant, self.tenant, scope_digest=tenant)
        self.assertEqual(matched["returncode"], 0)
        self.assertIn("memo 125", matched["output"])
        self.assertNotIn("memo 120", matched["output"])

    def test_mcp_request_replay_revalidates_scope_before_reusing_result(self):
        execute = runpy.run_path(str(KIT / "agent-engineering-mcp"))["_execute_tool"]
        execute.__globals__["_BOUND_REPO"] = str(self.main)
        scope = resolve_memory_scope(self.main)
        args = {"query": "memo", "scope_digest": scope.digest}
        before = execute(10, "memory_recall", args)
        self.assertIn("memo 120", before["output"])
        self.git("switch", "-c", "scratch")
        after = execute(10, "memory_recall", args)
        self.assertEqual((after["reason_code"], after["lookup_started"]),
                         ("MEMORY_SCOPE_MISMATCH", False))
        self.assertEqual(after["output"], "")


if __name__ == "__main__":
    unittest.main()
