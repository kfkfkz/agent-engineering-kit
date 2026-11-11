#!/usr/bin/env python3
"""Init's bounded source/CAS journal never replaces user documents."""
import json
import tempfile
import subprocess
import sys
import base64
import unittest
from unittest import mock
from pathlib import Path

from aek.adapters.document_layout import resolve_package
from aek.adapters.document_layout_store import initialize_package


class LayoutInitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        (self.repo / ".repo-memory-kit").mkdir()
        templates = self.repo / "docs/01-需求/_模板"
        templates.mkdir(parents=True)
        for name in ("原始需求.md", "需求分析.md"):
            (templates / name).write_text("# " + name + "\n模板内容。\n", encoding="utf-8")

    def test_only_initial_requirement_material_is_created_and_idempotence_preserves_user_text(self):
        package, first = initialize_package(self.repo, "查询页面", "1.0.9")
        root = self.repo / "docs/需求/1.0.9/查询页面"
        self.assertEqual(set(path.name for path in root.glob("*.md")), {"原始需求.md", "需求分析.md"})
        self.assertRegex(package.change_id, r"^CHG-[A-F0-9]{28}$")
        self.assertEqual(resolve_package(self.repo, package.change_id), package)
        (root / "需求分析.md").write_text("用户已修改。", encoding="utf-8")
        again, second = initialize_package(self.repo, "查询页面", "1.0.9")
        self.assertEqual(again, package)
        self.assertEqual(second["created"], [])
        self.assertEqual((root / "需求分析.md").read_text(encoding="utf-8"), "用户已修改。")
        another, _ = initialize_package(self.repo, "查询页面", "1.1.0")
        self.assertNotEqual(another.change_id, package.change_id)

    def test_foreign_index_is_reported_before_creating_package(self):
        root = self.repo / "docs/需求/1.0.9"
        root.mkdir(parents=True)
        index = root / "README.md"
        index.write_bytes(b"foreign generated index")
        with self.assertRaisesRegex(ValueError, "INDEX_NOT_OWNED"):
            initialize_package(self.repo, "查询页面", "1.0.9")
        self.assertFalse((root / "查询页面").exists())
        self.assertEqual(index.read_bytes(), b"foreign generated index")

    def test_each_publication_boundary_recovers_and_refuses_changed_inputs(self):
        for cut in range(6):
            with self.subTest(cut=cut):
                title = "故障恢复" + str(cut)
                with self.assertRaises(RuntimeError):
                    initialize_package(self.repo, title, "1.0.9", fault_after=cut)
                with self.assertRaisesRegex(ValueError, "INVALID_JOURNAL"):
                    resolve_package(self.repo, "docs/需求/1.0.9/" + title)
                template = self.repo / "docs/01-需求/_模板/需求分析.md"
                original = template.read_bytes()
                template.write_bytes(original + b"changed")
                with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
                    initialize_package(self.repo, title, "1.0.9")
                self.assertTrue((self.repo / ".repo-memory-kit/document-init.tx.json").exists())
                template.write_bytes(original)
                package, _ = initialize_package(self.repo, title, "1.0.9")
                self.assertEqual(resolve_package(self.repo, package.change_id), package)
                self.assertFalse((self.repo / ".repo-memory-kit/document-init.tx.json").exists())

    def test_project_mapping_takes_priority_and_keeps_real_roots(self):
        policy = self.repo / ".repo-memory-kit/document-layout.json"
        policy.write_text(json.dumps({"schema_version": 1, "source_root": "docs/版本/{version}/需求/{title}",
            "design_root": "docs/版本/{version}/设计/{title}"}), encoding="utf-8")
        package, _ = initialize_package(self.repo, "查询页面", "1.0.9")
        self.assertEqual(package.layout, "project")
        self.assertEqual(package.source_root, "docs/版本/1.0.9/需求/查询页面")
        self.assertEqual(package.design_root, "docs/版本/1.0.9/设计/查询页面")
        self.assertFalse((self.repo / "docs/需求").exists())

    def test_two_processes_register_one_identity_and_one_index_row(self):
        script = """import json,sys
from pathlib import Path
from aek.adapters.document_layout_store import initialize_package
package,result=initialize_package(Path(sys.argv[1]),'并发查询','1.0.9')
print(json.dumps({'id':package.change_id,'created':result['created']}))
"""
        workers = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(self.repo)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8') for _ in range(2)]
        try:
            results = []
            for worker in workers:
                out, err = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0, out + err)
                results.append(json.loads(out))
            self.assertEqual(results[0]['id'], results[1]['id'])
            self.assertEqual(sorted(len(row['created']) for row in results), [0, 5])
            index = (self.repo / 'docs/需求/1.0.9/README.md').read_text(encoding='utf-8')
            self.assertEqual(index.count(results[0]['id']), 1)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()

    def test_changed_mapping_does_not_silently_duplicate_an_existing_version_and_title(self):
        package, _ = initialize_package(self.repo, '查询页面', '1.0.9')
        (self.repo / '.repo-memory-kit/document-layout.json').write_text(json.dumps({
            'schema_version': 1, 'source_root': 'docs/版本/{version}/需求/{title}',
            'design_root': 'docs/版本/{version}/设计/{title}'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'LAYOUT_CONFLICT'):
            initialize_package(self.repo, '查询页面', '1.0.9')
        self.assertEqual(resolve_package(self.repo, package.change_id), package)

    def test_duplicate_owned_index_rows_block_registration_without_writing_another_package(self):
        first, _ = initialize_package(self.repo, '第一项', '1.0.9')
        index = self.repo / 'docs/需求/1.0.9/README.md'
        text = index.read_text(encoding='utf-8')
        row = next(line for line in text.splitlines() if first.change_id in line)
        index.write_text(text.replace('<!-- aek-packages:end -->', row + '\n<!-- aek-packages:end -->'), encoding='utf-8')
        before = index.read_bytes()
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_ID'):
            initialize_package(self.repo, '第二项', '1.0.9')
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.repo / 'docs/需求/1.0.9/第二项').exists())
        self.assertEqual(resolve_package(self.repo, first.change_id), first)

    def test_generated_id_collision_is_rejected_before_any_pointer_is_published(self):
        first, _ = initialize_package(self.repo, '第一项', '1.0.9')
        registry = self.repo / '.repo-memory-kit/document-packages.json'
        before = registry.read_bytes()
        with mock.patch('aek.adapters.document_layout_store.secrets.token_hex', return_value=first.change_id[4:].lower()):
            with self.assertRaisesRegex(ValueError, 'DUPLICATE_ID'):
                initialize_package(self.repo, '碰撞项', '1.0.9')
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse((self.repo / 'docs/需求/1.0.9/碰撞项').exists())
        self.assertEqual(resolve_package(self.repo, first.change_id), first)

    def test_recovery_rejects_a_duplicate_registry_postimage_before_writing_targets(self):
        first, _ = initialize_package(self.repo, '第一项', '1.0.9')
        registry = self.repo / '.repo-memory-kit/document-packages.json'
        before = registry.read_bytes()
        with self.assertRaises(RuntimeError):
            initialize_package(self.repo, '第二项', '1.0.9', fault_after=0)
        path = self.repo / '.repo-memory-kit/document-init.tx.json'
        journal = json.loads(path.read_bytes())
        entry = next(row for row in journal['changes'] if row['path'] == '.repo-memory-kit/document-packages.json')
        intended = json.loads(base64.b64decode(entry['after']))
        intended['packages'].append(intended['packages'][0])
        entry['after'] = base64.b64encode(json.dumps(intended).encode()).decode()
        path.write_text(json.dumps(journal), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_ID'):
            initialize_package(self.repo, '第二项', '1.0.9')
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse((self.repo / 'docs/需求/1.0.9/第二项').exists())
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
