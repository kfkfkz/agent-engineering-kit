#!/usr/bin/env python3
"""Trusted negative risk proof over a Git-visible release delta."""
from __future__ import annotations

import subprocess
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aek.adapters.change_scope import resolve_base_commit, scan_negative_risks
from aek.core.planning.facts import FACT_CATALOG, merge_fact_observations
from aek.adapters.document_layout_store import initialize_package


class ChangeScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / "docs" / "03-SDD").mkdir(parents=True)
        self.guide = self.root / "docs" / "03-SDD" / "guide.md"
        self.guide.write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=True, capture_output=True, text=True).stdout

    def register_package(self, *, project=False):
        kit = self.root / '.repo-memory-kit'
        kit.mkdir()
        if project:
            (kit / 'document-layout.json').write_text(json.dumps({'schema_version': 1,
                'source_root': 'docs/版本/{version}/需求/{title}',
                'design_root': 'docs/版本/{version}/设计/{title}'}, ensure_ascii=False), encoding='utf-8')
        templates = self.root / 'docs/01-需求/_模板'
        templates.mkdir(parents=True)
        for name in ('原始需求.md', '需求分析.md'):
            (templates / name).write_text('# document\n', encoding='utf-8')
        package, _ = initialize_package(self.root, '查询', '1.0.9')
        self.git('add', '.')
        self.git('commit', '-qm', 'register package')
        self.base = self.git('rev-parse', 'HEAD').strip()
        return package

    def states(self) -> tuple[str, str]:
        result = scan_negative_risks(self.root, self.base)
        return tuple(merge_fact_observations(
            fact, result.subject_digest,
            (observation,)).state for fact, observation in (
                ("irreversible_change", result.irreversible),
                ("performance_capacity", result.performance)))

    def test_docs_only_proves_both_negative_facts_and_binds_content(self) -> None:
        guide = self.guide
        guide.write_text("after\n")
        first = scan_negative_risks(self.root, self.base)
        self.assertEqual(resolve_base_commit(self.root, "HEAD"), self.base)
        self.assertEqual(self.states(), ("false", "false"))
        self.assertTrue(first.irreversible.scope_complete)
        self.assertEqual(first.irreversible.subject_digest, first.subject_digest)
        guide.write_text("after again\n")
        self.assertNotEqual(first.subject_digest,
                            scan_negative_risks(self.root, self.base).subject_digest)

    def test_docs_only_proves_every_planning_fact_false(self) -> None:
        self.guide.write_text("after\n")
        scan = scan_negative_risks(self.root, self.base)
        for fact_id in FACT_CATALOG:
            observations = tuple(item for item in scan.observations
                                 if item.fact_id == fact_id)
            self.assertEqual(merge_fact_observations(
                fact_id, scan.subject_digest, observations).state, "false",
                fact_id)

    def test_code_delta_proves_no_planning_fact_false(self) -> None:
        (self.root / "worker.py").write_text("pass\n")
        scan = scan_negative_risks(self.root, self.base)
        for fact_id in FACT_CATALOG:
            observations = tuple(item for item in scan.observations
                                 if item.fact_id == fact_id)
            self.assertNotEqual(merge_fact_observations(
                fact_id, scan.subject_digest, observations).state, "false",
                fact_id)

    def test_untracked_code_or_migration_never_proves_false(self) -> None:
        self.guide.write_text("after\n")
        (self.root / "query.py").write_text("execute(sql)\n")
        self.assertEqual(self.states(), ("unknown", "unknown"))
        (self.root / "query.py").unlink()
        (self.root / "migrations").mkdir()
        (self.root / "migrations" / "001.sql").write_text("DROP TABLE customer;\n")
        irreversible, capacity = self.states()
        self.assertNotEqual(irreversible, "false")
        self.assertNotEqual(capacity, "false")

    def test_unreadable_symlink_and_executable_markdown_fail_closed(self) -> None:
        link = self.root / "docs" / "03-SDD" / "link.md"
        try:
            link.symlink_to("guide.md")
        except (OSError, NotImplementedError):
            pass
        else:
            self.assertEqual(self.states(), ("unknown", "unknown"))
            link.unlink()
        script = self.root / "docs" / "03-SDD" / "script.md"
        script.write_text("#!/bin/sh\nrm data\n")
        script.chmod(0o755)
        if script.stat().st_mode & 0o111:
            self.assertEqual(self.states(), ("unknown", "unknown"))

    def test_deletion_is_safe_but_tracked_code_is_not(self) -> None:
        self.guide.unlink()
        self.assertEqual(self.states(), ("false", "false"))
        (self.root / "worker.py").write_text("pass\n")
        self.git("add", "worker.py")
        self.assertEqual(self.states(), ("unknown", "unknown"))

    def test_binary_markdown_is_not_a_negative_proof(self) -> None:
        (self.root / "docs" / "03-SDD" / "payload.md").write_bytes(b"text\0code")
        self.assertEqual(self.states(), ("unknown", "unknown"))

    def test_invalid_ref_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            scan_negative_risks(self.root, "missing-ref")

    def test_git_environment_cannot_redirect_scan_to_another_repo(self) -> None:
        self.guide.write_text("after\n")
        expected = scan_negative_risks(self.root, self.base).subject_digest
        with mock.patch.dict("os.environ", {"GIT_DIR": str(self.root / "elsewhere")}):
            actual = scan_negative_risks(self.root, self.base).subject_digest
        self.assertEqual(expected, actual)

    def test_runtime_memory_markdown_is_not_classified_as_safe_documentation(self) -> None:
        memory = self.root / "docs" / "memory"
        memory.mkdir()
        (memory / "rules.md").write_text("agent instruction\n")
        self.assertEqual(self.states(), ("unknown", "unknown"))

    def test_sdd_review_control_state_does_not_change_task_subject(self) -> None:
        self.guide.write_text("after\n")
        before = scan_negative_risks(self.root, self.base)
        reviews = self.root / "docs" / "03-SDD" / "001-x" / "reviews"
        reviews.mkdir(parents=True)
        (reviews / "artifact-plan.json").write_text('{"generated":true}\n')
        (reviews / "详细设计.gate.json").write_text('{"status":"PASS"}\n')
        after = scan_negative_risks(self.root, self.base)
        self.assertEqual(after.subject_digest, before.subject_digest)
        self.assertEqual(after.scope_digest, before.scope_digest)
        self.assertEqual(after.changed_paths, before.changed_paths)

    def test_registered_package_control_state_does_not_self_invalidate_or_hide_code(self) -> None:
        package = self.register_package()
        document = self.root / package.design_root / '需求分析.md'
        document.write_text('# 需求分析\n更新真实需求。\n', encoding='utf-8')
        before = scan_negative_risks(self.root, self.base)
        self.assertTrue(before.scope_complete)
        reviews = self.root / package.design_root / 'reviews'
        (reviews / 'artifact-plan.json').write_text('{"generated":true}\n', encoding='utf-8')
        (reviews / '.artifact-plan.lock').write_bytes(b'lock')
        (reviews / '需求分析.plan-binding.json').write_bytes(b'{}')
        after = scan_negative_risks(self.root, self.base)
        self.assertEqual(after.subject_digest, before.subject_digest)
        self.assertEqual(after.scope_digest, before.scope_digest)
        self.assertEqual(after.changed_paths, before.changed_paths)
        code = reviews / 'worker.py'
        code.write_bytes(b'execute_sql()\n')
        unsafe = scan_negative_risks(self.root, self.base)
        self.assertFalse(unsafe.scope_complete)
        self.assertIn(code.relative_to(self.root).as_posix(), unsafe.changed_paths)
        self.assertNotEqual(unsafe.subject_digest, before.subject_digest)

    def test_disjoint_registered_source_root_is_recognized_without_design_delta(self):
        package = self.register_package(project=True)
        source = self.root / package.source_root / '原始需求.md'
        source.write_text('# 原始需求\n只修改已登记的来源。\n', encoding='utf-8')
        result = scan_negative_risks(self.root, self.base)
        self.assertTrue(result.scope_complete)
        self.assertEqual(result.changed_paths, (source.relative_to(self.root).as_posix(),))

    def test_agent_instruction_files_in_registered_docs_are_not_plain_negative_evidence(self):
        package = self.register_package()
        for name in ('AGENTS.md', 'CLAUDE.md', 'SKILL.md', 'Agents.md'):
            with self.subTest(name=name):
                source = self.root / package.source_root / name
                source.write_text('# Agent instructions\nRun commands and read configuration.\n', encoding='utf-8')
                result = scan_negative_risks(self.root, self.base)
                self.assertFalse(result.scope_complete)
                self.assertIn(source.relative_to(self.root).as_posix(), result.changed_paths)
                security = merge_fact_observations('security_sensitive', result.subject_digest,
                    tuple(row for row in result.observations if row.fact_id == 'security_sensitive'))
                self.assertNotEqual(security.state, 'false')
                source.unlink()

    def test_unregistered_similar_layout_does_not_get_control_exclusion(self) -> None:
        reviews = self.root / 'docs/需求/1.0.9/未登记/reviews'
        reviews.mkdir(parents=True)
        control = reviews / 'artifact-plan.json'
        control.write_bytes(b'{}')
        result = scan_negative_risks(self.root, self.base)
        self.assertIn(control.relative_to(self.root).as_posix(), result.changed_paths)
        self.assertFalse(result.scope_complete)

    def test_delivery_receipt_control_state_does_not_self_invalidate_subject(self) -> None:
        self.guide.write_text("after\n")
        before = scan_negative_risks(self.root, self.base)
        receipts = self.root / "docs" / "delivery-receipts"
        receipts.mkdir(parents=True)
        (receipts / "current.md").write_text("# generated receipt\n")
        after = scan_negative_risks(self.root, self.base)
        self.assertEqual(after.subject_digest, before.subject_digest)
        self.assertEqual(after.scope_digest, before.scope_digest)
        self.assertEqual(after.changed_paths, before.changed_paths)
        self.assertRegex(after.scope_digest, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
