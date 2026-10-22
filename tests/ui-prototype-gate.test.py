from __future__ import annotations

import hashlib
import copy
import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GATE = runpy.run_path(str(ROOT / "doc-gate"))
Checks = GATE["Checks"]
check_prototypes = GATE["_check_ui_prototypes"]
write_confirmation = GATE["_write_prototype_confirmation"]
confirmation_digest = GATE["_prototype_confirmation_digest"]
confirmation_path = GATE["_prototype_confirmation_path"]
confirm_command = GATE["cmd_prototype_confirm"]
stage_frozen = GATE["stage_frozen"]
run_checks = GATE["run_checks"]


def required_ui_state():
    from aek.application.planning import DynamicPlanState
    from aek.core.artifact.plan import build_plan, freeze_plan
    from aek.core.planning.facts import FACT_CATALOG, FactObservation, merge_fact_observations
    from aek.core.planning.policy import preview_plan
    subject = "a" * 64
    facts = {name: merge_fact_observations(name, subject, tuple(
        FactObservation(name, producer, "false", subject, "b" * 64, True)
        for producer in spec.required_producers)) for name, spec in FACT_CATALOG.items()}
    plan = build_plan(preview_plan("standard", subject, facts,
                     required_overlays=("ui-design",)), facts,
                     required_overlays=("ui-design",))
    credential = freeze_plan(plan, input_digest="c" * 64, generation_mode="native",
                             frozen_at="2026-10-13T00:00:00Z")
    return DynamicPlanState("FROZEN", "test validated plan", plan, credential)


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PrototypeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.req = self.repo / "docs/03-SDD/CHG-001"
        self.req.mkdir(parents=True)
        self.material = self.repo / "docs/01-需求/CHG-001/原始材料/ui-reference"
        self.material.mkdir(parents=True)
        self.design_system = {"name": "Ant Design", "source": "user", "evidence": []}
        self.screenshot_bytes = b"synthetic previously validated screenshot bytes"
        self.image_digest = hashlib.sha256(self.screenshot_bytes).hexdigest()
        self.context_id = "P1-CAPTURED"
        self.path = "prototypes/page.html"
        self.html = b"<!doctype html><html><head><style>body{color:#222}</style></head><body><h1>Preview</h1></body></html>"
        self.prototype_digest = hashlib.sha256(self.html).hexdigest()
        self.index = {
            "schema_version": 1,
            "change_id": "CHG-001",
            "design_system": self.design_system,
            "screenshots": [{
                "id": "P1", "page": "列表", "state": "default", "purpose": "查看列表",
                "feature_ids": ["F1"], "acceptance_ids": ["A1"],
                "permissions": {"function_id": "FP01", "data_id": "DP01"},
                "path": "function/FP01/data/DP01/P1.png", "placeholder_sha256": "c" * 64,
                "validation": {"status": "valid", "content_sha256": self.image_digest,
                               "bytes": len(self.screenshot_bytes)},
                "applies_to": [{
                    "context_id": self.context_id,
                    "content_review": {"status": "applicable", "reviewed_sha256": self.image_digest},
                    "freshness": {"status": "confirmed_current", "confirmed_sha256": self.image_digest},
                }],
                "prototype_refs": [],
            }],
        }
        self.item = {
            "prototype_path": self.path,
            "prototype_sha256": self.prototype_digest,
            "page_id": "PAGE-1",
            "area_id": "AREA-1",
            "feature_ids": ["F1"],
            "acceptance_ids": ["A1"],
            "change_intent": "Add confirmation action",
            "depends_on": ["P1"],
            "artifact_plan_id": "plan-1",
            "gate_digest": "b" * 64,
            "approved_root": "docs/03-SDD/CHG-001",
            "status": "ACCEPTED",
            "reviewer": "k",
            "reviewed_at": "2026-10-12T20:00:00+08:00",
            "design_system_sha256": canonical_digest(self.design_system),
        }
        self.requirements = "- [x] A1：用户可以查看预览确认结果\n"
        self.overview = "### F1：原型评审记录\n\n覆盖验收项：A1\n"
        self.ui_text = "## 页面清单\n\n### PAGE-1：列表\n\n- 区域映射：AREA-1 → UI-EV P1\n\n## 原型确认记录\n"
        (self.req / "需求分析.md").write_bytes(self.requirements.encode("utf-8"))
        (self.req / "概要设计.md").write_bytes(self.overview.encode("utf-8"))
        page_block = self.ui_text[self.ui_text.index("### PAGE-1"):self.ui_text.index("\n## 原型确认记录")]
        snapshot = {
            "design_system_sha256": self.item["design_system_sha256"],
            "page_design_sha256": hashlib.sha256(page_block.encode("utf-8")).hexdigest(),
            "features": {"F1": self.overview},
            "acceptance": {"A1": "用户可以查看预览确认结果"},
            "evidence": [{"id": "P1", "sha256": self.image_digest,
                "semantic": {"page": "列表", "state": "default", "purpose": "查看列表",
                             "feature_ids": ["F1"], "acceptance_ids": ["A1"]}, "contexts": [{
                "context_id": self.context_id, "permissions": {"function_id": "FP01", "data_id": "DP01"},
                "system_version": None, "viewport": None, "relation": "captured",
            }]}],
        }
        self.item["dependency_sha256"] = canonical_digest(snapshot)
        self.dependency_snapshot = snapshot
        self.item["confirmation_source"] = "maintainer_reply"
        self.item["evidence_contexts"] = [{"evidence_id": "P1", "context_id": self.context_id}]
        self.index["screenshots"][0]["applies_to"][0].update({
            "permissions": {"function_id": "FP01", "data_id": "DP01"}, "relation": "captured",
        })
        self.index["screenshots"][0]["prototype_refs"] = [{key: self.item[key] for key in (
            "prototype_path", "prototype_sha256", "page_id", "area_id", "feature_ids",
            "acceptance_ids", "change_intent", "depends_on", "artifact_plan_id", "gate_digest",
            "approved_root") }]
        self._write_fixtures()
        (self.req / "reviews").mkdir()
        identity = confirmation_digest("PAGE-1", "AREA-1", self.prototype_digest,
                                       self.item["dependency_sha256"])
        write_confirmation(self.req, "PAGE-1", "AREA-1", self.prototype_digest,
                           self.item["dependency_sha256"], "k", self.item["reviewed_at"],
                           f"ACCEPT {identity[:12]}")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_fixtures(self) -> None:
        (self.req / "prototypes").mkdir(exist_ok=True)
        (self.req / "UI设计.md").write_bytes(self._full_ui_text().encode("utf-8"))
        (self.req / self.path).write_bytes(self.html)
        screenshot_path = self.material / "function/FP01/data/DP01/P1.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(self.screenshot_bytes)
        (self.material / "截图索引.json").write_text(
            json.dumps(self.index, ensure_ascii=False), encoding="utf-8")

    def _run(self) -> Checks:
        checks = Checks("UI设计")
        check_prototypes(checks, self.req, self._full_ui_text())
        return checks

    def _full_ui_text(self) -> str:
        manifest = {"schema_version": 1, "prototypes": [self.item]}
        return self.ui_text + "<!-- aek-prototypes:required -->\n```aek-prototypes\n" + json.dumps(manifest) + "\n```\n"

    def test_current_human_acceptance_and_current_evidence_pass(self) -> None:
        checks = self._run()
        self.assertEqual(checks.fails, [])

    def test_opt_in_source_baseline_is_part_of_existing_confirmation_digest(self):
        token, baseline = self._add_source_baseline()
        # Old confirmation cannot acquire new dependencies by merely appending
        # a baseline; the original legacy-only fixture above remains valid.
        self.assertTrue(self._run().fails)
        self.item['dependency_sha256'] = canonical_digest({**self.dependency_snapshot,
            'ui_baseline_sha256': canonical_digest(baseline)})
        self._write_fixtures()
        self.assertTrue(self._run().fails, 'old human confirmation is not reusable for a new joint digest')
        identity = confirmation_digest('PAGE-1', 'AREA-1', self.prototype_digest, self.item['dependency_sha256'])
        write_confirmation(self.req, 'PAGE-1', 'AREA-1', self.prototype_digest,
            self.item['dependency_sha256'], 'k', self.item['reviewed_at'], f'ACCEPT {identity[:12]}')
        self.assertEqual(self._run().fails, [])
        (self.repo / 'frontend/unrelated.css').write_bytes(b'unrelated')
        self.assertEqual(self._run().fails, [])
        token.write_bytes(b'red')
        self.assertTrue(self._run().fails)

    def _add_source_baseline(self):
        (self.repo / 'frontend').mkdir()
        token = self.repo / 'frontend/theme.css'
        token.write_bytes(b'blue')
        source = {'id': 'SRC-1', 'kind': 'token', 'path': 'frontend/theme.css',
                  'sha256': hashlib.sha256(b'blue').hexdigest(), 'certainty': 'source_backed'}
        region = {'page_id': 'PAGE-1', 'area_id': 'AREA-1', 'region_id': 'QUERY',
            'intent': 'PRESERVE', 'feature_ids': ['F1'], 'acceptance_ids': ['A1'],
            'source_ids': ['SRC-1'], 'screenshot_ids': ['P1'],
            'rationale': '沿用现有查询区', 'adjacent_context': None}
        baseline = {'schema_version': 2, 'sources': [source], 'regions': [region]}
        self.ui_text += '\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n'
        self._write_fixtures()
        return token, baseline

    def _add_observation_baseline(self):
        from aek.adapters import ui_filesystem as fs
        from aek.adapters.ui_observation_store import ObservationStore
        from aek.core.artifact.ui_observation import build_observation
        _, baseline = self._add_source_baseline()
        scope = canonical_digest({'repo_root': str(self.repo.absolute()),
            'requirement_dir': self.req.name, 'change_id': self.index['change_id']})
        contract = {'schema_version': 2, 'semantic_version': 'ui-observation-v1',
            'prompt_sha256': hashlib.sha256((ROOT /
                'skills/design-pipeline/references/screenshot-observation.md').read_bytes()).hexdigest()}
        source = {'scope_digest': scope, 'evidence_id': 'P1', 'image_sha256': self.image_digest}
        candidate = {'schema_version': 2, 'regions': [{'region_id': 'QUERY',
            'observation_kind': 'observed', 'layout': '顶部查询区域', 'controls': [],
            'style_clues': []}], 'uncertainties': []}
        record = build_observation(source, contract, ('QUERY',), candidate,
            provenance={'channel': 'agent_visual', 'model': None,
                        'observed_at': '2026-10-19T00:00:00Z'})
        handle = fs.open_absolute_dir(self.material)
        try:
            with ObservationStore(handle) as store:
                digest = store.publish_observation(record)
                store.bind(scope, None, {'evidence_id': 'P1', 'image_sha256': self.image_digest,
                    'contract_sha256': canonical_digest(contract), 'observation_sha256': digest})
        finally:
            fs.close_dir(handle)
        baseline['regions'][0]['observations'] = [{'evidence_id': 'P1', 'sha256': digest}]
        self.ui_text = self.ui_text.split('\n```aek-ui-baseline')[0]
        self.ui_text += '\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n'
        self.item['dependency_sha256'] = canonical_digest({**self.dependency_snapshot,
            'ui_baseline_sha256': canonical_digest(baseline)})
        self._write_fixtures()
        identity = confirmation_digest('PAGE-1', 'AREA-1', self.prototype_digest,
                                       self.item['dependency_sha256'])
        write_confirmation(self.req, 'PAGE-1', 'AREA-1', self.prototype_digest,
            self.item['dependency_sha256'], 'k', self.item['reviewed_at'], f'ACCEPT {identity[:12]}')
        return digest, record

    def test_current_observation_joins_existing_source_and_human_confirmation(self):
        # Missing new behavior is observed at the real existing Gate boundary,
        # using actual immutable records and the actual current binding index.
        self._add_observation_baseline()
        self.assertEqual(self._run().fails, [])

    def test_revoked_observation_never_uses_still_existing_immutable_file(self):
        digest, _ = self._add_observation_baseline()
        index = self.material / 'observations/索引.v2.json'
        value = json.loads(index.read_text(encoding='utf-8'))
        value['bindings'] = []
        index.write_text(json.dumps(value), encoding='utf-8')
        self.assertTrue((index.parent / (digest + '.json')).is_file())
        self.assertTrue(self._run().fails)

    def test_changed_observation_text_cannot_reuse_original_confirmation(self):
        digest, record = self._add_observation_baseline()
        record['regions'][0]['layout'] = '发生变化的查询布局'
        (self.material / 'observations' / (digest + '.json')).write_text(
            json.dumps(record), encoding='utf-8')
        self.assertTrue(self._run().fails)

    def test_observation_changes_after_html_read_cannot_publish_current_dependencies(self):
        self._add_observation_baseline()
        original = check_prototypes.__globals__['_read_beneath']
        index_path = self.material / 'observations/索引.v2.json'
        def read_then_revoke(handle, relative, limit):
            raw = original(handle, relative, limit)
            if relative == self.path:
                value = json.loads(index_path.read_text(encoding='utf-8'))
                value['bindings'] = []
                index_path.write_text(json.dumps(value), encoding='utf-8')
            return raw
        checks, published = Checks('UI设计'), {}
        with patch.dict(check_prototypes.__globals__, {'_read_beneath': read_then_revoke}):
            check_prototypes(checks, self.req, self._full_ui_text(), dependency_out=published)
        self.assertTrue(checks.fails)
        self.assertNotIn(('PAGE-1', 'AREA-1'), published)

    def test_late_screenshot_context_or_binding_change_is_rechecked_before_publication(self):
        self._add_observation_baseline()
        original = check_prototypes.__globals__['_read_beneath']
        binding_path = self.material / 'observations/索引.v2.json'
        binding_bytes = binding_path.read_bytes()
        index_path = self.material / '截图索引.json'
        screenshot_path = self.material / self.index['screenshots'][0]['path']
        index_bytes = index_path.read_bytes()
        for change in ('binding', 'context', 'image'):
            with self.subTest(change=change):
                binding_path.write_bytes(binding_bytes)
                index_path.write_bytes(index_bytes)
                screenshot_path.write_bytes(self.screenshot_bytes)
                def read_then_change(handle, relative, limit):
                    raw = original(handle, relative, limit)
                    # This happens after the first observation verification,
                    # while the existing human receipt is being read.
                    if relative.endswith('.json') and relative != '截图索引.json':
                        if change == 'binding':
                            value = json.loads(binding_path.read_text(encoding='utf-8'))
                            value['bindings'] = []
                            binding_path.write_text(json.dumps(value), encoding='utf-8')
                        elif change == 'context':
                            value = json.loads(index_bytes)
                            value['screenshots'][0]['applies_to'][0]['content_review']['status'] = 'not_applicable'
                            index_path.write_text(json.dumps(value), encoding='utf-8')
                        else:
                            screenshot_path.write_bytes(b'changed screenshot')
                    return raw
                checks, published = Checks('UI设计'), {}
                with patch.dict(check_prototypes.__globals__, {'_read_beneath': read_then_change}):
                    check_prototypes(checks, self.req, self._full_ui_text(), dependency_out=published)
                self.assertTrue(checks.fails)
                self.assertNotIn(('PAGE-1', 'AREA-1'), published)

    def test_observation_missing_store_is_read_only_and_does_not_synthesize_evidence(self):
        _, baseline = self._add_source_baseline()
        baseline['regions'][0]['observations'] = [{'evidence_id': 'P1', 'sha256': 'a' * 64}]
        self.ui_text = self.ui_text.split('\n```aek-ui-baseline')[0]
        self.ui_text += '\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n'
        self.assertTrue(self._run().fails)
        self.assertFalse((self.material / 'observations').exists())

    def test_changed_controlled_prompt_invalidates_cached_observation(self):
        self._add_observation_baseline()
        contract = check_prototypes.__globals__['_ui_observation_contract']()
        with patch.dict(check_prototypes.__globals__, {'_ui_observation_contract': lambda: {
                **contract, 'prompt_sha256': '0' * 64}}):
            self.assertTrue(self._run().fails)

    def test_prompt_changed_during_last_observation_read_cannot_publish_dependency(self):
        from aek.adapters import ui_filesystem as fs
        digest, _ = self._add_observation_baseline()
        kit = Path(self.temp.name) / 'isolated-kit'
        prompt = kit / 'skills/design-pipeline/references/screenshot-observation.md'
        prompt.parent.mkdir(parents=True)
        prompt.write_bytes((ROOT / 'skills/design-pipeline/references' / prompt.name).read_bytes())
        original = fs.read_at
        count = 0
        def read_then_change_prompt(handle, name, limit):
            nonlocal count
            raw = original(handle, name, limit)
            if name == digest + '.json':
                count += 1
                if count == 2:
                    prompt.write_bytes(prompt.read_bytes() + b'\nchanged controlled extraction rules\n')
            return raw
        checks, published = Checks('UI设计'), {}
        with patch.dict(check_prototypes.__globals__, {'_script_dir': kit}), patch(
                'aek.adapters.ui_filesystem.read_at', side_effect=read_then_change_prompt):
            check_prototypes(checks, self.req, self._full_ui_text(), dependency_out=published)
        self.assertEqual(count, 2)
        self.assertTrue(checks.fails)
        self.assertNotIn(('PAGE-1', 'AREA-1'), published)

    def test_source_changed_during_last_observation_read_cannot_publish_dependency(self):
        from aek.adapters import ui_filesystem as fs
        digest, _ = self._add_observation_baseline()
        original = fs.read_at
        count = 0
        def read_then_change_source(handle, name, limit):
            nonlocal count
            raw = original(handle, name, limit)
            if name == digest + '.json':
                count += 1
                if count == 2:
                    (self.repo / 'frontend/theme.css').write_bytes(b'red')
            return raw
        checks, published = Checks('UI设计'), {}
        with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_change_source):
            check_prototypes(checks, self.req, self._full_ui_text(), dependency_out=published)
        self.assertEqual(count, 2)
        self.assertTrue(checks.fails)
        self.assertNotIn(('PAGE-1', 'AREA-1'), published)

    def test_image_and_selected_context_are_checked_after_last_observation_payload_read(self):
        from aek.adapters import ui_filesystem as fs
        digest, _ = self._add_observation_baseline()
        original = fs.read_at
        image = self.material / self.index['screenshots'][0]['path']
        index_path = self.material / '截图索引.json'
        original_index = index_path.read_bytes()
        for change in ('image', 'context'):
            with self.subTest(change=change):
                count = 0
                image.write_bytes(self.screenshot_bytes)
                index_path.write_bytes(original_index)
                def read_then_change(handle, name, limit):
                    nonlocal count
                    raw = original(handle, name, limit)
                    if name == digest + '.json':
                        count += 1
                        if count == 2:
                            if change == 'image':
                                image.write_bytes(b'new screenshot')
                            else:
                                value = json.loads(original_index)
                                value['screenshots'][0]['applies_to'][0]['content_review']['status'] = 'not_applicable'
                                index_path.write_text(json.dumps(value), encoding='utf-8')
                    return raw
                checks, published = Checks('UI设计'), {}
                with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_change):
                    check_prototypes(checks, self.req, self._full_ui_text(), dependency_out=published)
                self.assertEqual(count, 2)
                self.assertTrue(checks.fails)
                self.assertNotIn(('PAGE-1', 'AREA-1'), published)

    def test_later_observation_payload_cannot_revoke_an_earlier_consumer_unnoticed(self):
        from aek.adapters import ui_filesystem as fs
        from aek.adapters.ui_observation_store import ObservationStore
        _, record = self._add_observation_baseline()
        second_record = copy.deepcopy(record)
        second_record['evidence_scope']['evidence_id'] = 'P2'
        handle = fs.open_absolute_dir(self.material)
        try:
            with ObservationStore(handle) as store:
                second_sha = store.publish_observation(second_record)
                scope = record['evidence_scope']['scope_digest']
                _, revision = store.binding_snapshot(scope)
                store.bind(scope, revision, {'evidence_id': 'P2', 'image_sha256': self.image_digest,
                    'contract_sha256': canonical_digest(record['extraction_contract']),
                    'observation_sha256': second_sha})
        finally:
            fs.close_dir(handle)
        before_block, baseline_text = self.ui_text.split('\n```aek-ui-baseline\n')
        before_block = before_block.replace('AREA-1 → UI-EV P1', 'AREA-1 → P1；AREA-2 → P2')
        baseline = json.loads(baseline_text.split('\n```')[0])
        second_region = {**copy.deepcopy(baseline['regions'][0]), 'area_id': 'AREA-2',
            'screenshot_ids': ['P2'], 'observations': [{'evidence_id': 'P2', 'sha256': second_sha}]}
        baseline['regions'].append(second_region)
        self.ui_text = before_block + '\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n'
        page_block = self.ui_text[self.ui_text.index('### PAGE-1'):self.ui_text.index('\n## 原型确认记录')]
        second = {**copy.deepcopy(self.item), 'area_id': 'AREA-2', 'depends_on': ['P2'],
                  'evidence_contexts': [{'evidence_id': 'P2', 'context_id': 'P2-CAPTURED'}]}
        screenshot = copy.deepcopy(self.index['screenshots'][0])
        screenshot.update(id='P2', path='function/FP01/data/DP01/P2.png')
        screenshot['applies_to'][0]['context_id'] = 'P2-CAPTURED'
        screenshot['prototype_refs'] = [{**screenshot['prototype_refs'][0],
                                        'area_id': 'AREA-2', 'depends_on': ['P2']}]
        self.index['screenshots'].append(screenshot)
        for item, region in zip((self.item, second), baseline['regions']):
            snapshot = copy.deepcopy(self.dependency_snapshot)
            snapshot['page_design_sha256'] = hashlib.sha256(page_block.encode('utf-8')).hexdigest()
            snapshot['ui_baseline_sha256'] = canonical_digest({**baseline, 'regions': [region]})
            if item is second:
                snapshot['evidence'][0]['id'] = 'P2'
                snapshot['evidence'][0]['contexts'][0]['context_id'] = 'P2-CAPTURED'
            item['dependency_sha256'] = canonical_digest(snapshot)
            identity = confirmation_digest('PAGE-1', item['area_id'], self.prototype_digest,
                                           item['dependency_sha256'])
            write_confirmation(self.req, 'PAGE-1', item['area_id'], self.prototype_digest,
                item['dependency_sha256'], 'k', item['reviewed_at'], f'ACCEPT {identity[:12]}')
        self._write_fixtures()
        (self.material / screenshot['path']).write_bytes(self.screenshot_bytes)
        text = self.ui_text + '<!-- aek-prototypes:required -->\n```aek-prototypes\n' + json.dumps({
            'schema_version': 1, 'prototypes': [self.item, second]}) + '\n```\n'
        checks = Checks('UI设计')
        check_prototypes(checks, self.req, text)
        self.assertEqual(checks.fails, [])
        original, count = fs.read_at, 0
        binding_path = self.material / 'observations/索引.v2.json'
        def read_then_revoke_earlier(handle, name, limit):
            nonlocal count
            raw = original(handle, name, limit)
            if name == second_sha + '.json':
                count += 1
                if count == 2:
                    value = json.loads(binding_path.read_text(encoding='utf-8'))
                    value['bindings'] = [row for row in value['bindings'] if row['evidence_id'] != 'P1']
                    binding_path.write_text(json.dumps(value), encoding='utf-8')
            return raw
        checks, published = Checks('UI设计'), {}
        with patch('aek.adapters.ui_filesystem.read_at', side_effect=read_then_revoke_earlier):
            check_prototypes(checks, self.req, text, dependency_out=published)
        self.assertEqual(count, 2)
        self.assertTrue(checks.fails)
        self.assertEqual(set(published), {('PAGE-1', 'AREA-2')})

    def test_revocation_invalidates_only_the_observation_consumer(self):
        self._add_observation_baseline()
        self.ui_text = self.ui_text.replace('AREA-1 → UI-EV P1', 'AREA-1、AREA-2 → UI-EV P1')
        before_block, baseline_text = self.ui_text.split('\n```aek-ui-baseline\n')
        baseline = json.loads(baseline_text.split('\n```')[0])
        second_region = {**baseline['regions'][0], 'area_id': 'AREA-2'}
        second_region.pop('observations')
        baseline['regions'].append(second_region)
        self.ui_text = before_block + '\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n'
        page_block = self.ui_text[self.ui_text.index('### PAGE-1'):self.ui_text.index('\n## 原型确认记录')]
        second = {**copy.deepcopy(self.item), 'area_id': 'AREA-2'}
        for item, region in zip((self.item, second), baseline['regions']):
            snapshot = {**self.dependency_snapshot,
                'page_design_sha256': hashlib.sha256(page_block.encode('utf-8')).hexdigest(),
                'ui_baseline_sha256': canonical_digest({**baseline, 'regions': [region]})}
            item['dependency_sha256'] = canonical_digest(snapshot)
        reverse = copy.deepcopy(self.index['screenshots'][0]['prototype_refs'][0])
        reverse['area_id'] = 'AREA-2'
        self.index['screenshots'][0]['prototype_refs'].append(reverse)
        self._write_fixtures()
        text = self.ui_text + '<!-- aek-prototypes:required -->\n```aek-prototypes\n' + json.dumps({
            'schema_version': 1, 'prototypes': [self.item, second]}) + '\n```\n'
        checks, published = Checks('UI设计'), {}
        check_prototypes(checks, self.req, text, dependency_out=published, require_confirmation=False)
        self.assertEqual(checks.fails, [])
        self.assertEqual(set(published), {('PAGE-1', 'AREA-1'), ('PAGE-1', 'AREA-2')})
        binding_path = self.material / 'observations/索引.v2.json'
        value = json.loads(binding_path.read_text(encoding='utf-8'))
        value['bindings'] = []
        binding_path.write_text(json.dumps(value), encoding='utf-8')
        checks, published = Checks('UI设计'), {}
        check_prototypes(checks, self.req, text, dependency_out=published, require_confirmation=False)
        self.assertTrue(checks.fails)
        self.assertEqual(set(published), {('PAGE-1', 'AREA-2')})

    def test_installed_contract_does_not_ignore_a_broken_managed_copy(self):
        current_contract = GATE['_ui_observation_contract']
        script_dir = self.repo / '.repo-memory-kit/bin'
        script_dir.mkdir(parents=True)
        agents = self.repo / '.agents/skills/design-pipeline/references'
        agents.mkdir(parents=True)
        prompt = ROOT / 'skills/design-pipeline/references/screenshot-observation.md'
        (agents / prompt.name).write_bytes(prompt.read_bytes())
        with patch.dict(current_contract.__globals__, {'_installed_layout': True, '_script_dir': script_dir}):
            self.assertEqual(current_contract()['prompt_sha256'], hashlib.sha256(prompt.read_bytes()).hexdigest())
            claude = self.repo / '.claude/skills/design-pipeline/references'
            claude.mkdir(parents=True)
            # Existing but incomplete managed installation is not another
            # absent client: it must be repaired rather than silently skipped.
            with self.assertRaises((OSError, ValueError)):
                current_contract()

    def test_legacy_gate_does_not_read_observations_or_new_prompt_resource(self):
        with patch.dict(check_prototypes.__globals__, {'_ui_observation_contract': lambda:
                self.fail('legacy gate must not read the observation contract')}):
            self.assertEqual(self._run().fails, [])
        self.assertFalse((self.material / 'observations').exists())

    def test_chinese_requirement_observation_scope_uses_directory_and_machine_id(self):
        name = '003-中文需求名称'
        old_req = self.req
        self.req = self.repo / 'docs/03-SDD' / name
        old_req.rename(self.req)
        (self.repo / 'docs/01-需求/CHG-001').rename(self.repo / 'docs/01-需求' / name)
        self.material = self.repo / 'docs/01-需求' / name / '原始材料/ui-reference'
        self.index.update(change_id='CHG-003', requirement_dir=name)
        self.item['approved_root'] = 'docs/03-SDD/' + name
        self.index['screenshots'][0]['prototype_refs'][0]['approved_root'] = self.item['approved_root']
        self._add_observation_baseline()
        self.assertEqual(self._run().fails, [])
        path = self.material / 'observations/索引.v2.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['scope_digest'] = '0' * 64
        path.write_text(json.dumps(value), encoding='utf-8')
        self.assertTrue(self._run().fails)

    def test_source_change_during_later_html_read_cannot_publish_a_current_dependency(self):
        token, baseline = self._add_source_baseline()
        self.item['dependency_sha256'] = canonical_digest({**self.dependency_snapshot,
            'ui_baseline_sha256': canonical_digest(baseline)})
        self._write_fixtures()
        identity = confirmation_digest('PAGE-1', 'AREA-1', self.prototype_digest, self.item['dependency_sha256'])
        write_confirmation(self.req, 'PAGE-1', 'AREA-1', self.prototype_digest,
            self.item['dependency_sha256'], 'k', self.item['reviewed_at'], f'ACCEPT {identity[:12]}')
        self.assertEqual(self._run().fails, [])
        original = check_prototypes.__globals__['_read_beneath']
        def read_then_change_token(handle, relative, limit):
            raw = original(handle, relative, limit)
            if relative == self.path:
                token.write_bytes(b'red')
            return raw
        checks, published = Checks('UI设计'), {}
        with patch.dict(check_prototypes.__globals__, {'_read_beneath': read_then_change_token}):
            check_prototypes(checks, self.req, self._full_ui_text(), dependency_out=published)
        self.assertTrue(checks.fails)
        self.assertNotIn(('PAGE-1', 'AREA-1'), published)

    def test_malformed_duplicate_or_unknown_target_baseline_is_rejected_by_existing_gate(self):
        original = self.ui_text
        unowned = {'schema_version': 2, 'sources': [], 'regions': [{
            'page_id': 'PAGE-2', 'area_id': 'AREA-1', 'region_id': 'QUERY', 'intent': 'NEW',
            'feature_ids': ['F1'], 'acceptance_ids': ['A1'], 'source_ids': [], 'screenshot_ids': [],
            'rationale': '新增区', 'adjacent_context': '相邻查询区域'}]}
        block = '\n```aek-ui-baseline\n' + json.dumps(unowned) + '\n```'
        for suffix in ('\n```aek-ui-baseline\n{}',
                       '\n```aek-ui-baseline\n{"schema_version":2,"schema_version":2}\n```',
                       '\n```aek-ui-baseline\n' + (' ' * (64 * 1024 + 1)) + '{}\n```',
                       block, block + block):
            with self.subTest(suffix=suffix[:70]):
                self.ui_text = original + suffix
                self.assertTrue(self._run().fails)
        self.ui_text = original

    def test_chinese_requirement_directory_binds_machine_identity(self) -> None:
        name = "003-中文需求名称"
        old_req = self.req
        self.req = self.repo / "docs/03-SDD" / name
        old_req.rename(self.req)
        old_material = self.repo / "docs/01-需求/CHG-001"
        old_material.rename(self.repo / "docs/01-需求" / name)
        self.material = self.repo / "docs/01-需求" / name / "原始材料/ui-reference"
        self.index.update(change_id="CHG-003", requirement_dir=name)
        self.item["approved_root"] = "docs/03-SDD/" + name
        self.index["screenshots"][0]["prototype_refs"][0]["approved_root"] = self.item["approved_root"]
        self._write_fixtures()
        self.assertEqual(self._run().fails, [])
        self.index["change_id"] = "CHG-004"
        self._write_fixtures()
        self.assertTrue(self._run().fails)

    def test_upstream_required_ui_plan_blocks_deleted_marker_and_manifest(self) -> None:
        (self.req / "UI设计.md").write_bytes(self.ui_text.encode("utf-8"))
        checks = run_checks(self.req, "UI设计", required_ui_state())
        self.assertTrue(any(item["group"] == "prototype" for item in checks.fails),
                        "required UI plan must survive removal of document-owned markers")

    def test_bound_required_ui_stage_is_draft_after_manifest_removal(self) -> None:
        (self.req / "UI设计.md").write_bytes(self.ui_text.encode("utf-8"))
        state = required_ui_state()
        from aek.core.artifact.registry import ARTIFACT_REGISTRY
        with patch.dict(stage_frozen.__globals__, {
                "evaluate_stage_states": lambda *_: {name: "BOUND" for name in ARTIFACT_REGISTRY.groups},
                "_dynamic_snapshots": lambda *_: {}}):
            self.assertEqual(stage_frozen(self.req, "UI设计", state)[0], "DRAFT")

    def test_check_loads_persisted_plan_and_rejects_document_opt_out(self) -> None:
        from aek.adapters.plan_transaction import PlanStore
        state = required_ui_state()
        store = PlanStore(self.req / "reviews")
        _, expected = store.snapshot_with_digest()
        store.publish({
            "artifact-plan.json": json.dumps(asdict(state.plan), ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8"),
            "artifact-plan.gate.json": json.dumps(asdict(state.credential), ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8"),
        }, expected_consumers=expected)
        (self.req / "UI设计.md").write_bytes(self.ui_text.encode("utf-8"))
        output = io.StringIO()
        with redirect_stdout(output):
            result = GATE["cmd_check"](self.req, "UI设计", True)
        self.assertEqual(result, 1, output.getvalue())
        report = json.loads(output.getvalue())
        self.assertTrue(any(item["group"] == "prototype" and item["status"] == "FAIL"
                            for item in report["checks"]), report)

    def test_prototype_changed_after_acceptance_fails(self) -> None:
        (self.req / self.path).write_bytes(self.html + b"<!-- edited -->")
        self.assertTrue(self._run().fails)

    def test_missing_or_traversing_or_symlink_prototype_fails(self) -> None:
        self.item["prototype_path"] = "../outside.html"
        self.assertTrue(self._run().fails)
        self.item["prototype_path"] = "prototypes/link.html"
        outside = Path(self.temp.name) / "outside.html"
        outside.write_bytes(self.html)
        (self.req / "prototypes/link.html").symlink_to(outside)
        self.assertTrue(self._run().fails)

    def test_external_and_relative_resources_are_rejected(self) -> None:
        for html in (
            b'<html><head><link rel="stylesheet" href="https://example.test/a.css"></head></html>',
            b'<html><body><img src="./screen.png"></body></html>',
            b'<html><style>body{background:url(https://example.test/a.png)}</style></html>',
            b'<html><script>fetch("https://example.test")</script></html>',
            b'<html><svg onload="alert(1)"></svg></html>',
            b'<html><svg><use xlink:href="https://example.test/a.svg#x"></use></svg></html>',
            b'<html><style>body{background:u\\\nrl(https://example.test/x)}</style></html>',
            b'<html><style>p{content:"/*";background:url(https://example.test/pixel)/*"*/}</style></html>',
            b'<html><p style=\'content:"/*";background:url(https://example.test/pixel)/*"*/\'>x</p></html>',
            b'<html><body background="https://example.test/pixel"></body></html>',
            b'<html><a href="#x" ping="https://example.test/p">x</a></html>',
            b'<html><a href="#x" attributionsrc="https://example.test/attribution">x</a></html>',
            b'<html><img lowsrc="https://example.test/low.png"></html>',
            b'<html><link rel="preload" imagesrcset="https://example.test/a.png 1x"></html>',
            b'<html><applet code="X.class" archive="https://example.test/x.jar"></applet></html>',
        ):
            with self.subTest(html=html):
                self.html = html
                self.prototype_digest = hashlib.sha256(html).hexdigest()
                self.item["prototype_sha256"] = self.prototype_digest
                self.index["screenshots"][0]["prototype_refs"][0]["prototype_sha256"] = self.prototype_digest
                parser = GATE["_SelfContainedHtml"]()
                parser.feed(html.decode("utf-8"))
                parser.close()
                self.assertTrue(parser.errors, f"unsafe HTML/CSS was accepted: {html!r}")
                self._write_fixtures()
                self.assertTrue(self._run().fails)

    def test_unconfirmed_or_changed_screenshot_evidence_invalidates_acceptance(self) -> None:
        context = self.index["screenshots"][0]["applies_to"][0]
        context["freshness"]["status"] = "unknown"
        self._write_fixtures()
        self.assertTrue(self._run().fails)

    def test_replacing_screenshot_without_updating_index_invalidates_acceptance(self) -> None:
        (self.material / "function/FP01/data/DP01/P1.png").write_bytes(b"replacement screenshot")
        self.assertTrue(self._run().fails)

    def test_frozen_ui_stage_becomes_draft_when_screenshot_changes(self) -> None:
        ui_path = self.req / "UI设计.md"
        reviews = self.req / "reviews"
        reviews.mkdir(exist_ok=True)
        (reviews / "UI设计.gate.json").write_text(json.dumps({
            "status": "PASS", "doc_hashes": {"UI设计.md": hashlib.sha256(ui_path.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        (self.material / "function/FP01/data/DP01/P1.png").write_bytes(b"replacement screenshot")
        state, reason = stage_frozen(self.req, "UI设计")
        self.assertEqual(state, "DRAFT")
        self.assertIn("截图", reason)

    def test_removed_page_or_area_design_invalidates_acceptance(self) -> None:
        self.ui_text = self.ui_text.replace("AREA-1", "AREA-2")
        self.assertTrue(self._run().fails)

    def test_only_real_named_accepted_decision_can_pass(self) -> None:
        self.item["status"] = "NEEDS_REVISION"
        self.assertTrue(self._run().fails)
        self.item["status"] = "ACCEPTED"
        self.item["reviewer"] = ""
        self.assertTrue(self._run().fails)
        self.item["reviewer"] = "k"
        self.item["confirmation_source"] = "agent_inferred"
        self.assertTrue(self._run().fails)

    def test_missing_or_mismatched_interactive_confirmation_fails(self) -> None:
        receipt = next((self.req / "reviews/ui-prototype-confirmations").iterdir())
        receipt.unlink()
        self.assertTrue(self._run().fails)
        identity = confirmation_digest("PAGE-1", "AREA-1", self.prototype_digest,
                                       self.item["dependency_sha256"])
        write_confirmation(self.req, "PAGE-1", "AREA-1", self.prototype_digest,
                           self.item["dependency_sha256"], "someone-else", self.item["reviewed_at"],
                           f"ACCEPT {identity[:12]}")
        self.assertTrue(self._run().fails)

    def test_confirmation_receipt_cannot_be_overwritten(self) -> None:
        with self.assertRaises(FileExistsError):
            write_confirmation(self.req, "PAGE-1", "AREA-1", self.prototype_digest,
                               self.item["dependency_sha256"], "k", self.item["reviewed_at"],
                               f"ACCEPT {confirmation_digest('PAGE-1', 'AREA-1', self.prototype_digest, self.item['dependency_sha256'])[:12]}")

    def test_updated_dependency_digest_does_not_reuse_old_human_confirmation(self) -> None:
        self.design_system["version"] = "2"
        new_design_digest = canonical_digest(self.design_system)
        self.index["design_system"] = self.design_system
        self.item["design_system_sha256"] = new_design_digest
        page_block = self.ui_text[self.ui_text.index("### PAGE-1"):self.ui_text.index("\n## 原型确认记录")]
        current_snapshot = {
            "design_system_sha256": new_design_digest,
            "page_design_sha256": hashlib.sha256(page_block.encode("utf-8")).hexdigest(),
            "features": {"F1": self.overview},
            "acceptance": {"A1": "用户可以查看预览确认结果"},
            "evidence": [{"id": "P1", "sha256": self.image_digest,
                "semantic": {"page": "列表", "state": "default", "purpose": "查看列表",
                             "feature_ids": ["F1"], "acceptance_ids": ["A1"]}, "contexts": [{
                "context_id": self.context_id, "permissions": {"function_id": "FP01", "data_id": "DP01"},
                "system_version": None, "viewport": None, "relation": "captured",
            }]}],
        }
        self.item["dependency_sha256"] = canonical_digest(current_snapshot)
        self._write_fixtures()
        self.assertTrue(self._run().fails)

    def test_screenshot_semantic_change_invalidates_human_confirmation(self) -> None:
        self.index["screenshots"][0]["purpose"] = "查看列表及筛选结果"
        self._write_fixtures()
        self.assertTrue(self._run().fails)

    def test_interrupted_receipt_write_leaves_no_final_file_and_can_retry(self) -> None:
        new_dependency = "d" * 64
        name = confirmation_path("PAGE-1", "AREA-1", self.prototype_digest, new_dependency)
        phrase = f"ACCEPT {confirmation_digest('PAGE-1', 'AREA-1', self.prototype_digest, new_dependency)[:12]}"
        with patch("os.write", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                write_confirmation(self.req, "PAGE-1", "AREA-1", self.prototype_digest,
                                   new_dependency, "k", self.item["reviewed_at"], phrase)
        receipt_dir = self.req / "reviews/ui-prototype-confirmations"
        self.assertFalse((receipt_dir / name).exists())
        self.assertEqual(list(receipt_dir.glob("*.tmp-*")), [])
        write_confirmation(self.req, "PAGE-1", "AREA-1", self.prototype_digest,
                           new_dependency, "k", self.item["reviewed_at"], phrase)
        self.assertTrue((receipt_dir / name).is_file())

    def test_confirmation_command_rejects_noninteractive_input(self) -> None:
        with patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertEqual(confirm_command(self.req, "PAGE-1", "AREA-1", "k"), 3)

    def test_interactive_confirmation_command_records_current_joint_digest(self) -> None:
        receipt = next((self.req / "reviews/ui-prototype-confirmations").iterdir())
        receipt.unlink()
        identity = confirmation_digest("PAGE-1", "AREA-1", self.prototype_digest,
                                       self.item["dependency_sha256"])
        with patch("sys.stdin") as stdin, patch("builtins.input", return_value=f"ACCEPT {identity[:12]}"):
            stdin.isatty.return_value = True
            with redirect_stdout(io.StringIO()):
                self.assertEqual(confirm_command(self.req, "PAGE-1", "AREA-1", "k"), 0)
        self.assertTrue(next((self.req / "reviews/ui-prototype-confirmations").iterdir()).is_file())

    def test_design_system_change_invalidates_acceptance(self) -> None:
        self.index["design_system"]["version"] = "6"
        self._write_fixtures()
        self.assertTrue(self._run().fails)

    def test_only_changed_requirement_dependencies_invalidate_the_record(self) -> None:
        (self.req / "需求分析.md").write_bytes("- [x] A1：用户可以确认新的审批结果\n".encode("utf-8"))
        self.assertTrue(self._run().fails)


if __name__ == "__main__":
    unittest.main()
