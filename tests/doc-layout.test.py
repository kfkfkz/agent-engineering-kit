#!/usr/bin/env python3
"""Stable package identity is separate from presentation and safe paths."""
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aek.adapters.document_layout import resolve_package
from aek.core.artifact.document_layout import make_package, package_bytes, load_package


class LayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        (self.repo / ".repo-memory-kit").mkdir()

    def test_explicit_legacy_chinese_directory_keeps_original_identity_and_roots(self):
        relative = "docs/03-SDD/001-中文需求"
        (self.repo / relative).mkdir(parents=True)
        package = resolve_package(self.repo, relative)
        self.assertEqual(package.change_id, "CHG-001")
        self.assertEqual(package.design_root, relative)
        self.assertEqual(package.source_root, "docs/01-需求/001-中文需求")
        self.assertEqual(package.layout, "legacy")
        self.assertFalse((self.repo / relative / "reviews").exists())

    def register(self, title="查询页面", version="1.0.9", change_id="CHG-" + "A" * 28):
        package = make_package(change_id=change_id, title=title, version=version,
            layout="co-located", source_root=f"docs/需求/{version}/{title}",
            design_root=f"docs/需求/{version}/{title}")
        path = self.repo / package.design_root / "reviews"
        path.mkdir(parents=True)
        (path / "package.json").write_bytes(package_bytes(package))
        (self.repo / ".repo-memory-kit/document-packages.json").write_text(json.dumps({
            "schema_version": 1, "packages": [{"change_id": package.change_id, "version": version,
                "package_ref": package.design_root, "revision": package.revision}]}), encoding="utf-8")
        return package

    def test_registered_co_located_package_resolves_by_path_and_stable_id_without_mirrors(self):
        package = self.register()
        self.assertEqual(resolve_package(self.repo, package.design_root), package)
        self.assertEqual(resolve_package(self.repo, package.change_id), package)
        self.assertEqual(load_package(package_bytes(package)), package)
        self.assertFalse((self.repo / "docs/01-需求").exists())
        self.assertFalse((self.repo / "docs/03-SDD").exists())

    def test_numbered_display_name_does_not_override_registered_machine_id(self):
        package = self.register(title='003-中文显示名')
        root = Path(__file__).resolve().parents[1]
        request = runpy.run_path(str(root / 'tests/ui-reference.test.py'))['request']()
        request.update(change_id=package.change_id, requirement_dir=package.title)
        result = subprocess.run([sys.executable, '-X', 'utf8', str(root / 'skills/design-pipeline/scripts/ui-reference.py'),
            'scaffold', '--repo-root', str(self.repo), '--package-ref', package.package_ref],
            input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        index = json.loads((self.repo / package.material_root / '截图索引.json').read_bytes())
        self.assertEqual(index['change_id'], package.change_id)
        self.assertNotEqual(index['change_id'], 'CHG-003')

    def test_registered_unicode_spelling_is_preserved_instead_of_normalized(self):
        package = self.register(title='003-e\u0301页面')
        root = Path(__file__).resolve().parents[1]
        request = runpy.run_path(str(root / 'tests/ui-reference.test.py'))['request']()
        request.update(change_id=package.change_id, requirement_dir=package.title)
        result = subprocess.run([sys.executable, '-X', 'utf8', str(root / 'skills/design-pipeline/scripts/ui-reference.py'),
            'scaffold', '--repo-root', str(self.repo), '--package-ref', package.package_ref],
            input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.repo / package.material_root / '截图索引.json').exists())
        self.assertEqual(resolve_package(self.repo, package.change_id), package)

    def test_duplicate_id_or_registry_metadata_mismatch_is_not_guessed(self):
        package = self.register()
        path = self.repo / ".repo-memory-kit/document-packages.json"
        registry = json.loads(path.read_text(encoding="utf-8"))
        registry["packages"].append({**registry["packages"][0], "package_ref": "docs/需求/other/查询页面"})
        path.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DUPLICATE_ID"):
            resolve_package(self.repo, package.change_id)
        registry["packages"] = registry["packages"][:1]
        registry["packages"][0]["revision"] = "0" * 64
        path.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "REGISTRATION_MISMATCH"):
            resolve_package(self.repo, package.design_root)

    def test_paths_and_unknown_metadata_cannot_expand_repository_scope(self):
        for selector in ("../outside", "/tmp/outside", "docs/../outside", "docs//outside", "docs/NUL", "docs/x:stream"):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                resolve_package(self.repo, selector)
        for namespace in ('memory', 'Memory', 'MEMORY', 'delivery-receipts', 'Delivery-Receipts'):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                make_package(change_id='CHG-' + 'A' * 28, title='查询', version='1.0.9', layout='co-located',
                    source_root=f'docs/{namespace}/查询', design_root=f'docs/{namespace}/查询')
        package = self.register()
        value = json.loads(package_bytes(package))
        value["unexpected"] = True
        with self.assertRaises(ValueError):
            load_package(json.dumps(value).encode())

    @unittest.skipIf(os.name == "nt", "POSIX link fixture; shared filesystem tests cover reparse points")
    def test_registered_path_with_symlink_ancestor_is_rejected(self):
        package = self.register()
        path = self.repo / package.design_root
        moved = path.with_name("真实目录")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            resolve_package(self.repo, package.design_root)


if __name__ == "__main__":
    unittest.main()
