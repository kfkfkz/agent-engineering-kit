#!/usr/bin/env python3
"""Public init and all document selectors share current package registration."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import runpy
import contextlib
import io
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LayoutCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / '中文仓库'
        (self.repo / '.repo-memory-kit').mkdir(parents=True)
        templates = self.repo / 'docs/01-需求/_模板'
        shutil.copytree(ROOT / 'templates/requirements', templates)
        shutil.copyfile(ROOT / 'templates/requirements-index.md', templates.parent / 'README.md')

    def invoke(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'doc-gate'), *args],
            cwd=ROOT, env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def prepare_review_and_relocation(self):
        from aek.adapters.document_layout_store import initialize_package
        from aek.core.artifact.document_layout import make_package, package_bytes
        module = runpy.run_path(str(ROOT / 'tests/doc-material-e2e.test.py'))
        package, _ = initialize_package(self.repo, '查询页面', '1.0.9')
        directory = self.repo / package.design_root
        marker = '<!-- aek-review-boundary: declared-id-v1 -->\n'
        (directory / '需求分析.md').write_text(marker + module['REQUIREMENTS'], encoding='utf-8')
        (directory / '概要设计.md').write_text(marker + module['OUTLINE'], encoding='utf-8')
        self.invoke('freeze', str(directory), '--by', 'fixture-maintainer', '--json')
        self.invoke('check', str(directory), '--stage', '概要设计', '--json')
        prepared = json.loads(self.invoke('review-input', str(directory), '--stage', '概要设计', '--schema-version', '2', '--json').stdout)
        reviews = directory / 'reviews'
        (reviews / '概要设计.review-receipt.json').write_text(json.dumps(prepared['receipt'], ensure_ascii=False), encoding='utf-8')
        (reviews / '概要设计.issues.json').write_text(json.dumps({'reviewer': 'fixture-independent-reviewer',
            'stage': '概要设计', 'doc_hashes': prepared['receipt']['doc_hashes'], 'issues': []}), encoding='utf-8')
        changed = make_package(change_id=package.change_id, version=package.version, title=package.title,
            layout='project', source_root=package.source_root + '/replacement', design_root=package.design_root)
        (self.repo / changed.source_root).mkdir(parents=True)
        def relocate():
            (reviews / 'package.json').write_bytes(package_bytes(changed))
            registry = self.repo / '.repo-memory-kit/document-packages.json'
            value = json.loads(registry.read_bytes())
            value['packages'][0]['revision'] = changed.revision
            registry.write_text(json.dumps(value), encoding='utf-8')
        return directory, relocate

    def test_old_review_receipt_cannot_approve_a_different_package_context(self):
        directory, relocate = self.prepare_review_and_relocation()
        request = directory / 'reviews/概要设计.review-request.json'
        before = request.read_bytes()
        relocate()
        self.invoke('freeze', str(directory), '--by', 'fixture-maintainer', '--json')
        self.invoke('gate', str(directory), '--stage', '概要设计', '--json', expected=3)
        self.assertEqual(request.read_bytes(), before)
        self.assertFalse((directory / 'reviews/概要设计.gate.json').exists())

    def test_package_change_after_review_validation_cannot_publish_pass(self):
        directory, relocate = self.prepare_review_and_relocation()
        namespace = runpy.run_path(str(ROOT / 'doc-gate'))
        command = namespace['cmd_gate']
        actual = command.__globals__['_check_review_inputs_current']
        def concurrent_change(*args, **kwargs):
            actual(*args, **kwargs)
            relocate()
        output = io.StringIO()
        with mock.patch.dict(command.__globals__, {'_check_review_inputs_current': concurrent_change}), contextlib.redirect_stdout(output):
            result = command(directory, '概要设计', False, True)
        self.assertEqual(result, 3, output.getvalue())
        self.assertFalse((directory / 'reviews/概要设计.gate.json').exists())
        status = json.loads(self.invoke('status', str(directory), '--json').stdout)
        self.assertNotEqual(status['概要设计']['state'], 'FROZEN')

    def test_package_change_after_gate_storage_cannot_be_adopted_by_baseline(self):
        from aek.adapters.document_material_store import MaterialArchive
        directory, relocate = self.prepare_review_and_relocation()
        namespace = runpy.run_path(str(ROOT / 'doc-gate'))
        actual = MaterialArchive.publish_gate
        def change_after_write(archive, *args, **kwargs):
            result = actual(archive, *args, **kwargs)
            relocate()
            return result
        output = io.StringIO()
        with mock.patch.object(MaterialArchive, 'publish_gate', change_after_write), contextlib.redirect_stdout(output):
            result = namespace['cmd_gate'](directory, '概要设计', False, True)
        self.assertEqual(result, 3, output.getvalue())
        self.assertFalse((directory / 'reviews/概要设计.plan-binding.json').exists())
        status = json.loads(self.invoke('status', str(directory), '--json').stdout)
        self.assertNotEqual(status['概要设计']['state'], 'FROZEN')

    def test_new_simple_layout_and_original_legacy_init_both_use_real_paths(self):
        new = json.loads(self.invoke('init', '查询页面', '--version', '1.0.9', '--repo', str(self.repo), '--json').stdout)
        package = new['package']
        self.assertEqual(package['design_root'], 'docs/需求/1.0.9/查询页面')
        self.assertEqual(package['source_root'], package['design_root'])
        self.assertEqual(set(path.name for path in (self.repo / package['design_root']).glob('*.md')),
                         {'原始需求.md', '需求分析.md'})
        repeated = json.loads(self.invoke('init', '查询页面', '--version', '1.0.9', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual(repeated['package'], package)
        self.assertEqual(repeated['created'], [])
        legacy = json.loads(self.invoke('init', '014-原有需求', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual(set(legacy), {'name', 'created', 'skipped', 'index_row_added'})
        self.assertTrue((self.repo / 'docs/01-需求/014-原有需求/原始需求.md').is_file())
        self.assertTrue((self.repo / 'docs/03-SDD/014-原有需求/详细设计.md').is_file())
        self.assertFalse((self.repo / 'docs/03-SDD/查询页面').exists())

    def test_missing_or_changed_registration_is_rejected_before_document_operations(self):
        package = json.loads(self.invoke('init', '查询页面', '--version', '1.0.9', '--repo', str(self.repo), '--json').stdout)['package']
        directory = self.repo / package['design_root']
        metadata = directory / 'reviews/package.json'
        metadata.unlink()
        result = self.invoke('check', str(directory), '--stage', '需求分析', '--json', expected=3)
        self.assertIn('REGISTRATION_MISMATCH', result.stdout)
        self.assertFalse((directory / 'reviews/hard-checks.json').exists())
        self.invoke('freeze', str(directory), '--by', 'fixture-reviewer', '--json', expected=3)
        self.assertFalse((directory / 'reviews/需求分析.gate.json').exists())

    def test_foreign_version_index_is_preserved_without_partial_registration(self):
        parent = self.repo / 'docs/需求/1.0.9'
        parent.mkdir(parents=True)
        index = parent / 'README.md'
        index.write_bytes(b'foreign generated index')
        result = self.invoke('init', '查询页面', '--version', '1.0.9', '--repo', str(self.repo), '--json', expected=3)
        self.assertIn('INDEX_NOT_OWNED', result.stdout)
        self.assertEqual(index.read_bytes(), b'foreign generated index')
        self.assertFalse((parent / '查询页面').exists())
        self.assertFalse((self.repo / '.repo-memory-kit/document-packages.json').exists())


if __name__ == '__main__':
    unittest.main()
