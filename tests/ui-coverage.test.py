#!/usr/bin/env python3
"""Upstream UI obligations, not the remaining screenshot rows, define scope."""
from __future__ import annotations

import copy
import hashlib
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aek.core.artifact.ui_coverage import classify_ui_scope, check_ui_coverage


class UiCoverageTests(unittest.TestCase):
    def setUp(self):
        self.digest = hashlib.sha256('当前需求'.encode()).hexdigest()
        self.target = dict(page_id='PAGE-1', area_id='AREA-1', state_id='default', context_id='CTX-1')
        self.classification = dict(schema_version=2, requirements_sha256=self.digest, items=[
            dict(id=ident, classification='UI', reason='本轮页面交互', targets=[self.target.copy()])
            for ident in ('F1', 'A1')])
        self.prototype = dict(**self.target, feature_ids=['F1'], acceptance_ids=['A1'],
                              depends_on=['P1'], evidence_contexts=[dict(evidence_id='P1', context_id='CTX-1')])
        self.screenshot = dict(id='P1', feature_ids=['F1'], acceptance_ids=['A1'], state='default',
                               applies_to=[dict(context_id='CTX-1')])

    def scope(self, **changes):
        args = dict(feature_ids=('F1',), acceptance_ids=('A1',), classification=self.classification,
                    requirements_sha256=self.digest, upstream_current=True, plan_required=True)
        return classify_ui_scope(**{**args, **changes})

    def test_removing_f3_from_both_classification_and_screenshots_does_not_remove_obligation(self):
        scope = self.scope(feature_ids=('F1', 'F3'))
        self.assertFalse(scope.complete)
        self.assertIn('F3', ' '.join(scope.gaps))
        self.assertTrue(check_ui_coverage(scope, [self.prototype], [self.screenshot]).gaps)

    def test_current_non_ui_needs_a_reason_and_performs_no_image_io(self):
        candidate = copy.deepcopy(self.classification)
        for row in candidate['items']:
            row.update(classification='NON_UI', reason='仅后端日志，不改UI', targets=[])
        scope = self.scope(classification=candidate, plan_required=False)
        self.assertTrue(scope.non_ui)
        candidate['items'][0]['reason'] = ' '
        self.assertFalse(self.scope(classification=candidate, plan_required=False).complete)

    def test_reviewed_non_ui_classification_uses_only_bounded_upstream_text(self):
        gate = runpy.run_path(str(ROOT / 'doc-gate'))
        candidate = copy.deepcopy(self.classification)
        with tempfile.TemporaryDirectory() as temporary:
            req = Path(temporary) / 'repo/docs/03-SDD/014-中文需求'
            req.mkdir(parents=True)
            requirement = '# 需求\n- [ ] A1：保持日志兼容\n'
            candidate['requirements_sha256'] = hashlib.sha256(requirement.encode()).hexdigest()
            for row in candidate['items']:
                row.update(classification='NON_UI', reason='只改变服务端日志', targets=[])
            overview = '### F1：日志\n覆盖验收项：A1\n\n## UI 分类\n```aek-ui-classification\n' + json.dumps(candidate, ensure_ascii=False) + '\n```\n'
            for name, value in (('需求分析.md', requirement), ('概要设计.md', overview)):
                (req / name).write_text(value, encoding='utf-8')
            reviews = req / 'reviews'
            reviews.mkdir()
            (reviews / '需求分析.gate.json').write_text(json.dumps(dict(status='PASS',
                doc_hashes={'需求分析.md': hashlib.sha256(requirement.encode()).hexdigest()})), encoding='utf-8')
            (reviews / '概要设计.gate.json').write_text(json.dumps(dict(status='PASS',
                doc_hashes={'概要设计.md': hashlib.sha256(overview.encode()).hexdigest()},
                upstream_fingerprints={'需求分析': gate['_stage_fingerprint'](req, '需求分析')})), encoding='utf-8')
            reads = []
            original = gate['_read_beneath']
            def text_only(root, relative, maximum):
                reads.append(relative)
                self.assertIn(relative, ('需求分析.md', '概要设计.md'))
                return original(root, relative, maximum)
            with patch.dict(gate['_check_ui_prototypes'].__globals__, {'_read_beneath': text_only}):
                checks = gate['Checks']('UI设计')
                gate['_check_ui_prototypes'](checks, req, '')
            self.assertEqual(checks.fails, [])
            self.assertEqual(reads, ['概要设计.md', '需求分析.md'] * 2)
            # Removing a reason cannot be hidden by a locally edited overview:
            # the existing credential is now stale before any image work.
            (req / '概要设计.md').write_text(overview.replace('只改变服务端日志', ''), encoding='utf-8')
            checks = gate['Checks']('UI设计')
            gate['_check_ui_prototypes'](checks, req, '')
            self.assertTrue(checks.fails)

    def test_unknown_stale_duplicate_and_unreviewed_facts_cannot_waive_ui(self):
        for mutation in ('unknown', 'stale', 'duplicate', 'unreviewed', 'empty', 'plan'):
            candidate = copy.deepcopy(self.classification)
            kwargs = {}
            if mutation == 'unknown':
                candidate['items'][0].update(classification='UNKNOWN', targets=[])
            elif mutation == 'stale':
                candidate['requirements_sha256'] = '0' * 64
            elif mutation == 'duplicate':
                candidate['items'].append(candidate['items'][0].copy())
            elif mutation == 'unreviewed':
                kwargs['upstream_current'] = False
            elif mutation == 'empty':
                candidate['items'] = []
            else:
                for row in candidate['items']:
                    row.update(classification='NON_UI', targets=[])
            with self.subTest(mutation=mutation):
                self.assertFalse(self.scope(classification=candidate, **kwargs).complete)

    def test_coverage_checks_actual_target_state_permission_and_both_trace_owners(self):
        scope = self.scope()
        self.assertTrue(check_ui_coverage(scope, [self.prototype], [self.screenshot]).complete)
        for mutation in ('state', 'context', 'trace', 'dependency'):
            prototype, image = copy.deepcopy(self.prototype), copy.deepcopy(self.screenshot)
            if mutation == 'state':
                image['state'] = 'error'
            elif mutation == 'context':
                image['applies_to'][0]['context_id'] = 'CTX-OTHER'
            elif mutation == 'trace':
                prototype['acceptance_ids'] = []
            else:
                prototype['depends_on'] = ['P2']
            with self.subTest(mutation=mutation):
                self.assertFalse(check_ui_coverage(scope, [prototype], [image]).complete)

    def test_legacy_default_single_context_mapping_is_not_reused_for_other_states(self):
        prototype = {key: value for key, value in self.prototype.items()
                     if key not in ('state_id', 'context_id')}
        self.assertTrue(check_ui_coverage(self.scope(), [prototype], [self.screenshot]).complete)
        candidate = copy.deepcopy(self.classification)
        for row in candidate['items']:
            row['targets'][0]['state_id'] = 'error'
        self.assertFalse(check_ui_coverage(self.scope(classification=candidate), [prototype], [self.screenshot]).complete)
        prototype['evidence_contexts'].append(dict(evidence_id='P1', context_id='CTX-OTHER'))
        self.assertFalse(check_ui_coverage(self.scope(), [prototype], [self.screenshot]).complete)

    def test_chinese_upstream_directory_retains_f3_after_projection_rows_are_removed(self):
        fixture_module = runpy.run_path(str(ROOT / 'tests/ui-prototype-gate.test.py'))
        fixture = fixture_module['PrototypeGateTests']()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        gate = fixture_module['GATE']
        fixture.requirements = '# 需求\n' + fixture.requirements
        (fixture.req / '需求分析.md').write_text(fixture.requirements, encoding='utf-8')
        name = '014-中文需求'
        destination = fixture.repo / 'docs/03-SDD' / name
        fixture.req.rename(destination)
        fixture.req = destination
        (fixture.repo / 'docs/01-需求/CHG-001').rename(fixture.repo / 'docs/01-需求' / name)
        fixture.material = fixture.repo / 'docs/01-需求' / name / '原始材料/ui-reference'
        fixture.index.update(change_id='CHG-014', requirement_dir=name)
        fixture.item['approved_root'] = 'docs/03-SDD/' + name
        fixture.index['screenshots'][0]['prototype_refs'][0]['approved_root'] = fixture.item['approved_root']
        fixture._write_fixtures()
        # Keep the old current F1 receipt intact while adding a separately
        # reviewed classification section to the upstream overview.
        candidate = copy.deepcopy(self.classification)
        candidate['requirements_sha256'] = hashlib.sha256(fixture.requirements.encode()).hexdigest()
        for row in candidate['items']:
            row['targets'][0]['context_id'] = fixture.context_id
        overview = fixture.overview + '\n## UI 分类\n```aek-ui-classification\n' + json.dumps(candidate) + '\n```\n'
        (fixture.req / '概要设计.md').write_text(overview, encoding='utf-8')
        reviews = fixture.req / 'reviews'
        (reviews / '需求分析.gate.json').write_text(json.dumps(dict(status='PASS',
            doc_hashes={'需求分析.md': hashlib.sha256(fixture.requirements.encode()).hexdigest()})), encoding='utf-8')
        def freeze_overview():
            (reviews / '概要设计.gate.json').write_text(json.dumps(dict(status='PASS',
                doc_hashes={'概要设计.md': hashlib.sha256((fixture.req / '概要设计.md').read_bytes()).hexdigest()},
                upstream_fingerprints={'需求分析': gate['_stage_fingerprint'](fixture.req, '需求分析')})), encoding='utf-8')
        freeze_overview()
        checks = gate['Checks']('UI设计')
        gate['_check_ui_prototypes'](checks, fixture.req, fixture._full_ui_text())
        self.assertEqual(checks.fails, [])
        original = gate['_read_beneath']
        changed = []
        def change_upstream_after_receipt(root, relative, maximum):
            raw = original(root, relative, maximum)
            if relative.endswith('.json') and '截图索引' not in relative and not changed:
                changed.append(True)
                (fixture.req / '概要设计.md').write_text(
                    overview + '\n### F3：确认读取期间新增\n覆盖验收项：A1\n', encoding='utf-8')
                freeze_overview()
            return raw
        with patch.dict(gate['_check_ui_prototypes'].__globals__, {'_read_beneath': change_upstream_after_receipt}):
            checks = gate['Checks']('UI设计')
            gate['_check_ui_prototypes'](checks, fixture.req, fixture._full_ui_text())
        self.assertTrue(changed)
        self.assertTrue(checks.fails, 'upstream changes during receipt IO cannot reuse the old coverage decision')
        (fixture.req / '概要设计.md').write_text(overview, encoding='utf-8')
        freeze_overview()
        # F3 exists in the frozen actual overview, but was removed from both
        # classification and screenshots/prototypes. It must remain a gap.
        (fixture.req / '概要设计.md').write_text(overview + '\n### F3：仍需要页面\n覆盖验收项：A1\n', encoding='utf-8')
        freeze_overview()
        checks = gate['Checks']('UI设计')
        gate['_check_ui_prototypes'](checks, fixture.req, fixture._full_ui_text().replace('<!-- aek-prototypes:required -->', ''))
        self.assertTrue(checks.fails)
        self.assertIn('F3', json.dumps(checks.fails))

    def test_gap_projection_is_bounded_and_explicitly_truncated(self):
        scope = self.scope(feature_ids=tuple(f'F{i}' for i in range(1, 80)))
        self.assertFalse(scope.complete)
        self.assertEqual(len(scope.gaps), 32)
        self.assertTrue(scope.truncated)

    def test_untrusted_shapes_cannot_raise_or_become_complete_coverage(self):
        self.assertFalse(self.scope(feature_ids=(['F1'],)).complete)
        self.assertFalse(self.scope(plan_required='false').complete)
        for key in ('depends_on', 'evidence_contexts'):
            candidate = {**self.prototype, key: None}
            with self.subTest(key=key):
                self.assertFalse(check_ui_coverage(self.scope(), [candidate], [self.screenshot]).complete)

    def test_dynamic_required_ui_without_classification_fails_before_image_access(self):
        gate = runpy.run_path(str(ROOT / 'doc-gate'))
        from types import SimpleNamespace
        state = SimpleNamespace(state='FROZEN', plan=SimpleNamespace(
            by_id=lambda ident: SimpleNamespace(classification='required')))
        with tempfile.TemporaryDirectory() as temporary:
            req = Path(temporary) / 'repo/docs/03-SDD/014-中文需求'
            req.mkdir(parents=True)
            (req / '需求分析.md').write_text('- [ ] A1：查看列表\n', encoding='utf-8')
            (req / '概要设计.md').write_text('### F1：列表\n覆盖验收项：A1\n', encoding='utf-8')
            checks = gate['Checks']('UI设计')
            gate['_check_ui_prototypes'](checks, req, '', dynamic_state=state)
            self.assertTrue(checks.fails)
            self.assertIn('classification', json.dumps(checks.fails))

    def test_dynamic_skip_cannot_bypass_unknown_classification(self):
        gate = runpy.run_path(str(ROOT / 'doc-gate'))
        from aek.application.planning import DynamicPlanState
        from aek.core.artifact.plan import build_plan, freeze_plan
        from aek.core.planning.facts import FACT_CATALOG, FactObservation, merge_fact_observations
        from aek.core.planning.policy import preview_plan
        subject = 'a' * 64
        facts = {name: merge_fact_observations(name, subject, tuple(
            FactObservation(name, producer, 'false', subject, 'b' * 64, True)
            for producer in spec.required_producers)) for name, spec in FACT_CATALOG.items()}
        plan = build_plan(preview_plan('standard', subject, facts), facts)
        state = DynamicPlanState('FROZEN', 'fixture', plan, freeze_plan(plan,
            input_digest='c' * 64, generation_mode='native', frozen_at='2026-10-19T00:00:00Z'))
        with tempfile.TemporaryDirectory() as temporary:
            req = Path(temporary) / 'repo/docs/03-SDD/014-中文需求'
            req.mkdir(parents=True)
            (req / '概要设计.md').write_text('### F1：未知影响\n', encoding='utf-8')
            checks = gate['run_checks'](req, 'UI设计', state)
            self.assertTrue(checks.fails)
            self.assertEqual(gate['stage_frozen'](req, 'UI设计', state)[0], 'INVALID')
            # The public gate must not report success on a plan-only skip when
            # the current upstream text does not justify that skip.
            self.assertNotEqual(gate['_cmd_gate_locked'](req, 'UI设计', False, True, state), 0)


if __name__ == '__main__':
    unittest.main()
