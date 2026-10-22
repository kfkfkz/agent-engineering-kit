from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def baseline(digest: str) -> dict:
    return {'schema_version': 2, 'sources': [{
        'id': 'SRC-1', 'kind': 'token', 'path': 'frontend/主题.css',
        'sha256': digest, 'certainty': 'source_backed'}], 'regions': [{
        'page_id': 'PAGE-1', 'area_id': 'AREA-1', 'region_id': 'QUERY',
        'intent': 'PRESERVE', 'feature_ids': ['F1'], 'acceptance_ids': ['A1'],
        'source_ids': ['SRC-1'], 'screenshot_ids': ['P1'],
        'rationale': '复用现有查询区布局', 'adjacent_context': None}]}


class BaselineTests(unittest.TestCase):
    def test_state_context_regions_keep_separate_sources_and_propagate_only_shared_changes(self):
        from aek.adapters.ui_baseline import read_baseline
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'frontend').mkdir()
            (repo / 'frontend/主题.css').write_bytes(b'blue')
            (repo / 'frontend/dialog.css').write_bytes(b'dialog')
            value = baseline(hashlib.sha256(b'blue').hexdigest())
            value['sources'].append({'id': 'SRC-2', 'kind': 'layout', 'path': 'frontend/dialog.css',
                'sha256': hashlib.sha256(b'dialog').hexdigest(), 'certainty': 'source_backed'})
            default = value['regions'][0]
            default.update(state_id='default', context_id='CTX-A')
            value['regions'] += [dict(default, state_id='dialog', source_ids=['SRC-2']),
                                 dict(default, context_id='CTX-B')]
            before = read_baseline(repo, value)
            self.assertEqual(set(before), {('PAGE-1', 'AREA-1', 'default', 'CTX-A'),
                ('PAGE-1', 'AREA-1', 'dialog', 'CTX-A'), ('PAGE-1', 'AREA-1', 'default', 'CTX-B')})
            self.assertTrue(all(not row['gaps'] for row in before.values()))
            (repo / 'frontend/主题.css').write_bytes(b'red')
            after = read_baseline(repo, value)
            for target in (('PAGE-1', 'AREA-1', 'default', 'CTX-A'), ('PAGE-1', 'AREA-1', 'default', 'CTX-B')):
                self.assertTrue(after[target]['gaps'])
            self.assertEqual(before[('PAGE-1', 'AREA-1', 'dialog', 'CTX-A')],
                             after[('PAGE-1', 'AREA-1', 'dialog', 'CTX-A')])

    def test_incomplete_or_ambiguous_typed_targets_are_rejected_before_source_io(self):
        from aek.adapters.ui_baseline import read_baseline
        from aek.core.artifact.ui_baseline import BaselineError
        for fields in ({'state_id': 'default'}, {'context_id': 'CTX-A'},
                       {'state_id': True, 'context_id': 'CTX-A'},
                       {'state_id': 'DEFAULT', 'context_id': 'CTX-A'},
                       {'state_id': 'default', 'context_id': '../OTHER'},
                       {'state_id': 'default', 'context_id': 'a' * 33}):
            with self.subTest(fields=fields), patch('aek.adapters.ui_baseline.fs.open_absolute_dir') as opened:
                value = baseline('a' * 64)
                value['regions'][0].update(fields)
                with self.assertRaises(BaselineError):
                    read_baseline(Path('/not-accessed'), value)
                opened.assert_not_called()
        value = baseline('a' * 64)
        value['regions'].append(dict(value['regions'][0], state_id='default', context_id='CTX-A'))
        with patch('aek.adapters.ui_baseline.fs.open_absolute_dir') as opened:
            with self.assertRaisesRegex(BaselineError, 'ambiguous'):
                read_baseline(Path('/not-accessed'), value)
            opened.assert_not_called()

    def test_observation_references_are_bounded_region_dependencies_not_paths_or_approvals(self):
        from aek.core.artifact.ui_baseline import validate_baseline
        value = baseline('a' * 64)
        ref = {'evidence_id': 'P1', 'sha256': 'b' * 64}
        value['regions'][0]['observations'] = [ref]
        self.assertEqual(validate_baseline(value), value)
        for refs in ([], [ref, ref], [{'evidence_id': 'P2', 'sha256': 'b' * 64}],
                     [{'evidence_id': 'P1', 'sha256': '../other'}],
                     [{**ref, 'approved': True}], [{**ref, 'path': '/outside'}]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                changed = copy.deepcopy(value)
                changed['regions'][0]['observations'] = refs
                validate_baseline(changed)
        value['regions'] = [dict(value['regions'][0], region_id=f'R{number}') for number in range(32)]
        for row in value['regions']:
            row['screenshot_ids'] = [f'P{number}' for number in range(1, 6)]
            row['observations'] = [{'evidence_id': ident, 'sha256': 'b' * 64}
                                   for ident in row['screenshot_ids']]
        with self.assertRaisesRegex(ValueError, 'reference count'):
            validate_baseline(value)

    def test_on_demand_source_baseline_resource_is_usable_and_packaged_in_both_surfaces(self):
        import json
        import re
        from installer.registry import REGISTRY
        from aek.core.artifact.ui_baseline import validate_baseline
        path = 'skills/design-pipeline/references/ui-source-baseline.md'
        self.assertEqual({item.destination_path for item in REGISTRY if item.source_path == path}, {
            '.agents/' + path, '.claude/' + path})
        resource = (Path(__file__).resolve().parents[1] / path).read_text(encoding='utf-8')
        self.assertLess(len(resource.encode('utf-8')), 8 * 1024)
        block = re.search(r'```aek-ui-baseline\n(.*?)\n```', resource, re.S)
        self.assertIsNotNone(block)
        # Format-valid example only, not a source/approval fixture.
        self.assertEqual(validate_baseline(json.loads(block.group(1)))['schema_version'], 2)

    def test_actual_source_file_is_bound_without_copying_source_text_or_approval(self):
        from aek.adapters.ui_baseline import read_baseline
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'frontend').mkdir()
            source = repo / 'frontend/主题.css'
            raw = b':root { --brand: blue; }'
            source.write_bytes(raw)
            value = baseline(hashlib.sha256(raw).hexdigest())
            snapshots = read_baseline(repo, value)
            item = snapshots[('PAGE-1', 'AREA-1')]
            self.assertEqual(item['gaps'], [])
            self.assertEqual(len(item['digest']), 64)
            self.assertNotIn('ACCEPTED', str(item))
            self.assertNotIn('--brand', str(item))
            self.assertNotIn('approved', item)
            self.assertEqual(read_baseline(repo, value), snapshots)

    def test_shared_token_change_invalidates_two_consumers_not_unrelated_region(self):
        from aek.adapters.ui_baseline import read_baseline
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'frontend').mkdir()
            token = repo / 'frontend/主题.css'
            token.write_bytes(b'blue')
            layout = repo / 'frontend/layout.css'
            layout.write_bytes(b'grid')
            value = baseline(hashlib.sha256(b'blue').hexdigest())
            value['sources'].append({'id': 'SRC-2', 'kind': 'layout', 'path': 'frontend/layout.css',
                'sha256': hashlib.sha256(b'grid').hexdigest(), 'certainty': 'source_backed'})
            second = {**copy.deepcopy(value['regions'][0]), 'page_id': 'PAGE-2'}
            third = {**copy.deepcopy(value['regions'][0]), 'page_id': 'PAGE-3', 'source_ids': ['SRC-2']}
            value['regions'] += [second, third]
            before = read_baseline(repo, value)
            self.assertTrue(all(not item['gaps'] for item in before.values()))
            token.write_bytes(b'red')
            after = read_baseline(repo, value)
            self.assertEqual(after[('PAGE-1', 'AREA-1')]['gaps'][0]['source_id'], 'SRC-1')
            self.assertEqual(after[('PAGE-2', 'AREA-1')]['gaps'][0]['source_id'], 'SRC-1')
            self.assertEqual(after[('PAGE-3', 'AREA-1')], before[('PAGE-3', 'AREA-1')])
            # Updating the declaration is not approval: it changes both affected
            # dependency digests and cannot reuse their previous confirmations.
            value['sources'][0]['sha256'] = hashlib.sha256(b'red').hexdigest()
            updated = read_baseline(repo, value)
            for target in (('PAGE-1', 'AREA-1'), ('PAGE-2', 'AREA-1')):
                self.assertNotEqual(updated[target]['digest'], before[target]['digest'])
            self.assertEqual(updated[('PAGE-3', 'AREA-1')], before[('PAGE-3', 'AREA-1')])

    def test_unverified_source_never_becomes_a_current_source_fact_just_because_hash_matches(self):
        from aek.core.artifact.ui_baseline import project_baseline
        for certainty in ('inferred', 'unknown'):
            with self.subTest(certainty=certainty):
                value = baseline('a' * 64)
                value['sources'][0]['certainty'] = certainty
                result = project_baseline(value, {'SRC-1': 'a' * 64})
                self.assertEqual(result[('PAGE-1', 'AREA-1')]['gaps'], [
                    {'source_id': 'SRC-1', 'reason': 'source_not_verified'}])

    def test_aggregate_budget_counts_unique_source_bytes_not_two_verification_passes(self):
        from aek.adapters.ui_baseline import read_baseline
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'frontend').mkdir()
            raw = b'a' * (1024 * 1024)
            digest = hashlib.sha256(raw).hexdigest()
            value = baseline(digest)
            value['sources'] = []
            for number in range(6):
                path = f'frontend/theme{number}.css'
                (repo / path).write_bytes(raw)
                value['sources'].append({'id': f'SRC-{number}', 'kind': 'token', 'path': path,
                                         'sha256': digest, 'certainty': 'source_backed'})
            value['regions'][0]['source_ids'] = [item['id'] for item in value['sources']]
            self.assertEqual(read_baseline(repo, value)[('PAGE-1', 'AREA-1')]['gaps'], [])

    def test_region_intents_require_real_screenshot_or_explicit_new_remove_context(self):
        from aek.core.artifact.ui_baseline import validate_baseline, BaselineError
        for intent in ('PRESERVE', 'MODIFY', 'NEW', 'REMOVE'):
            with self.subTest(intent=intent):
                value = baseline('a' * 64)
                value['regions'][0]['intent'] = intent
                value['regions'][0]['screenshot_ids'] = []
                with self.assertRaisesRegex(BaselineError, 'screenshot'):
                    validate_baseline(value)
                value['regions'][0]['adjacent_context'] = '需求F1/A1中明确新增按钮，沿用相邻查询区'
                if intent in ('NEW', 'REMOVE'):
                    self.assertEqual(validate_baseline(value), value)
                else:
                    with self.assertRaisesRegex(BaselineError, 'screenshot'):
                        validate_baseline(value)

    def test_paths_and_self_approved_fields_are_rejected_before_any_source_read(self):
        from aek.adapters.ui_baseline import read_baseline
        from aek.core.artifact.ui_baseline import BaselineError
        invalid = ('../secret', '/etc/passwd', 'C:/secret', 'a/../secret', 'a//b',
                   '.git/config', 'a\\b', 'frontend/CON.css', 'frontend/a.css.', 'a/e\u0301.css',
                   'a/', 'a/./b', '//server/share', 'a\x00b', '/'.join(['a'] * 21))
        for path in invalid:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                value = baseline('a' * 64)
                value['sources'][0]['path'] = path
                with patch('aek.adapters.ui_baseline.fs.open_absolute_dir') as opened:
                    with self.assertRaises(BaselineError):
                        read_baseline(Path(directory), value)
                    opened.assert_not_called()
        value = baseline('a' * 64)
        value['approved'] = True
        with self.assertRaises(BaselineError):
            read_baseline(Path('/not-accessed'), value)

    def test_symlink_source_is_a_gap_and_does_not_read_outside_repository(self):
        from aek.adapters.ui_baseline import read_baseline
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / 'repo'
            (repo / 'frontend').mkdir(parents=True)
            outside = base / 'outside.css'
            outside.write_bytes(b'private outside data')
            try:
                (repo / 'frontend/主题.css').symlink_to(outside)
            except OSError as exc:
                self.skipTest('native symlink unavailable: ' + str(exc))
            value = baseline(hashlib.sha256(outside.read_bytes()).hexdigest())
            result = read_baseline(repo, value)
            self.assertTrue(result[('PAGE-1', 'AREA-1')]['gaps'])
            self.assertNotIn('private outside', str(result))

    def test_two_pass_snapshot_detects_source_replaced_between_different_file_reads(self):
        from aek.adapters.ui_baseline import read_baseline
        from aek.adapters import ui_filesystem as fs
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'frontend').mkdir()
            (repo / 'frontend/主题.css').write_bytes(b'blue')
            (repo / 'frontend/layout.css').write_bytes(b'grid')
            value = baseline(hashlib.sha256(b'blue').hexdigest())
            value['sources'].append({'id': 'SRC-2', 'kind': 'layout', 'path': 'frontend/layout.css',
                'sha256': hashlib.sha256(b'grid').hexdigest(), 'certainty': 'source_backed'})
            value['regions'][0]['source_ids'].append('SRC-2')
            original = fs.read_at
            def read_then_replace(handle, name, limit):
                raw = original(handle, name, limit)
                if name == 'layout.css':
                    (repo / 'frontend/主题.css').write_bytes(b'red')
                return raw
            with patch('aek.adapters.ui_baseline.fs.read_at', side_effect=read_then_replace):
                result = read_baseline(repo, value)
            self.assertEqual(result[('PAGE-1', 'AREA-1')]['gaps'][0]['source_id'], 'SRC-1')

    def test_invalid_schema_references_duplicates_and_limits_are_not_source_facts(self):
        from aek.core.artifact.ui_baseline import validate_baseline
        mutations = []
        value = baseline('a' * 64); value['schema_version'] = True; mutations.append(value)
        value = baseline('a' * 64); value['sources'].append(copy.deepcopy(value['sources'][0])); mutations.append(value)
        value = baseline('a' * 64); value['regions'].append(copy.deepcopy(value['regions'][0])); mutations.append(value)
        value = baseline('a' * 64); value['regions'][0]['source_ids'] = ['MISSING']; mutations.append(value)
        value = baseline('a' * 64); value['regions'][0]['source_ids'] = []; mutations.append(value)
        value = baseline('a' * 64); value['regions'][0]['feature_ids'] = ['A1']; mutations.append(value)
        value = baseline('a' * 64); value['regions'][0]['rationale'] = 'x' * 1001; mutations.append(value)
        value = baseline('a' * 64); value['sources'][0]['sha256'] = True; mutations.append(value)
        value = baseline('a' * 64); value['sources'][0]['kind'] = 'execute'; mutations.append(value)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_baseline(value)

    def test_missing_and_oversized_files_are_explicit_gaps_not_valid_empty_sources(self):
        from aek.adapters.ui_baseline import read_baseline, MAX_SOURCE_BYTES
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'frontend').mkdir()
            value = baseline('a' * 64)
            self.assertTrue(read_baseline(repo, value)[('PAGE-1', 'AREA-1')]['gaps'])
            raw = b'a' * (MAX_SOURCE_BYTES + 1)
            (repo / 'frontend/主题.css').write_bytes(raw)
            value['sources'][0]['sha256'] = hashlib.sha256(raw).hexdigest()
            self.assertTrue(read_baseline(repo, value)[('PAGE-1', 'AREA-1')]['gaps'])

    def test_structural_and_utf8_byte_budgets_fail_for_the_actual_limit(self):
        from aek.core.artifact.ui_baseline import validate_baseline, BaselineError
        value = baseline('a' * 64)
        value['sources'] = [dict(value['sources'][0], id=f'SRC-{number}', path=f'frontend/{number}.css')
                            for number in range(129)]
        with self.assertRaisesRegex(BaselineError, 'source count'):
            validate_baseline(value)
        value = baseline('a' * 64)
        row = value['regions'][0]
        value['regions'] = [dict(row, region_id=f'R{number}') for number in range(33)]
        with self.assertRaisesRegex(BaselineError, 'region count'):
            validate_baseline(value)
        value['regions'] = [dict(row, region_id=f'R{number}', rationale='中' * 1000) for number in range(24)]
        with self.assertRaisesRegex(BaselineError, 'byte limit'):
            validate_baseline(value)


if __name__ == '__main__':
    unittest.main()
