#!/usr/bin/env python3
"""Read actual external source bytes without mirrors or inferred execution."""
import hashlib
import copy
import os
import runpy
import tempfile
import unittest
import subprocess
import sys
import json
from pathlib import Path

from aek.adapters.document_layout_store import initialize_package
from aek.adapters.source_map import capture_mapped_sources
from aek.core.artifact.source_map import load_source_map
from aek.core.artifact.registry import ARTIFACT_REGISTRY

ROOT = Path(__file__).resolve().parents[1]


class MappedSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        (self.repo / '.repo-memory-kit').mkdir()
        templates = self.repo / 'docs/01-需求/_模板'
        templates.mkdir(parents=True)
        for name in ('原始需求.md', '需求分析.md'):
            (templates / name).write_text('# ' + name + '\n', encoding='utf-8')
        self.package, _ = initialize_package(self.repo, '查询', '1.0.9')
        self.mapping = runpy.run_path(str(ROOT / 'tests/skill-composition.test.py'))['mapping']()
        artifact = self.mapping['artifacts'][0]
        artifact['points'][0].pop('sha256')
        self.path = self.repo / artifact['sources'][0]
        self.path.parent.mkdir(parents=True)
        self.raw = ('# 设计\n' + ''.join('## ' + row['section'] + '\n实际内容。\n'
                                       for row in artifact['sections'])
                    + '## 查询功能\n保持查询返回。\n## 公共约束\n所有查询鉴权。\n').encode('utf-8')
        self.path.write_bytes(self.raw)
        artifact['globals'].append({'path': artifact['sources'][0], 'heading': '设计'})
        binding = self.mapping['capability_bindings'][0]
        self.contract = self.repo / binding['contract_path']
        self.contract.parent.mkdir(parents=True)
        self.contract.write_bytes(b'# product-design\nWrite only the assigned design artifact.\n')
        binding['contract_sha256'] = hashlib.sha256(self.contract.read_bytes()).hexdigest()

    def git_base(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True,
                                  text=True, encoding='utf-8').stdout.strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'README.md').write_bytes(b'base\n')
        git('add', 'README.md')
        git('commit', '-qm', 'base')
        return git('rev-parse', 'HEAD')

    def test_cli_composition_lifecycle_binds_the_selected_package_and_live_contract(self):
        base = self.git_base()
        other, _ = initialize_package(self.repo, '另一需求', '1.0.9')
        encoded = json.dumps(self.mapping, ensure_ascii=False).encode('utf-8')

        def invoke(action, package, *options, payload=None):
            return subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'doc-gate'), action,
                str(self.repo / package.design_root), '--base', base, '--json', *options],
                input=payload, capture_output=True, cwd=self.repo, timeout=30)

        wrong = invoke('plan-prepare', other, '--route', 'standard', '--source-map', payload=encoded)
        self.assertEqual(wrong.returncode, 3, wrong.stdout + wrong.stderr)
        self.assertIn(b'ROOT_MISMATCH', wrong.stdout)
        self.assertFalse((self.repo / other.design_root / 'reviews/artifact-plan.json').exists())
        prepared = invoke('plan-prepare', self.package, '--route', 'standard', '--source-map', payload=encoded)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        self.assertEqual(json.loads(prepared.stdout)['plan']['schema_version'], 2)
        frozen = invoke('plan-freeze', self.package)
        self.assertEqual(frozen.returncode, 0, frozen.stdout + frozen.stderr)
        self.assertEqual(json.loads(frozen.stdout)['state'], 'FROZEN')
        reviews = self.repo / self.package.design_root / 'reviews'
        plan = (reviews / 'artifact-plan.json').read_bytes()
        credential = (reviews / 'artifact-plan.gate.json').read_bytes()
        plain_prepare = invoke('plan-prepare', self.package, '--route', 'standard')
        self.assertEqual(plain_prepare.returncode, 3, plain_prepare.stdout + plain_prepare.stderr)
        self.assertEqual((reviews / 'artifact-plan.json').read_bytes(), plan)
        self.assertEqual((reviews / 'artifact-plan.gate.json').read_bytes(), credential)
        current = invoke('plan-status', self.package)
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        self.assertEqual(json.loads(current.stdout)['state'], 'FROZEN')
        self.contract.write_bytes(b'# updated writer contract\n')
        stale = invoke('plan-status', self.package)
        self.assertEqual(stale.returncode, 3, stale.stdout + stale.stderr)
        self.assertIn('CAPABILITY_CONTRACT_CHANGED', json.loads(stale.stdout)['reason'])
        self.assertEqual((reviews / 'artifact-plan.gate.json').read_bytes(), credential)

    def test_cli_review_checks_real_external_outline_and_delivers_all_its_bytes(self):
        base = self.git_base()
        package = self.repo / self.package.design_root
        (package / '需求分析.md').write_text('''# 查询需求
## 背景
现有查询需要稳定返回。
## 目标
保持原有查询结果。
## 非目标
本次不改变界面。
## 场景与验收依据
### 场景 1：查询
- **前置**：用户已登录。
- **操作**：执行查询。
- **结果**：正常返回查询结果。
## 验收项
- [ ] A1：保留场景 1 的查询结果。
## 非功能要求
查询鉴权保持一致。
## 待确认事项
无。
## 冻结记录
- 冻结状态：已冻结
- 确认人：测试维护者
''', encoding='utf-8')
        # These constraints are deliberately outside all selected ranges. They
        # still belong to the first full review of the actual source file.
        classification = {'schema_version': 2,
            'requirements_sha256': hashlib.sha256((package / '需求分析.md').read_bytes()).hexdigest(),
            'items': [{'id': ident, 'classification': 'NON_UI', 'reason': '保持现有查询接口', 'targets': []}
                      for ident in ('F1', 'A1')]}
        raw = self.raw + ('## 补充约束\n缺少权限时保留原有错误码。\n'
            '## UI 分类\n```aek-ui-classification\n'
            + json.dumps(classification, ensure_ascii=False) + '\n```\n').encode('utf-8')
        self.path.write_bytes(raw)
        api_path = self.path.with_name('02-接口设计.md')
        api_raw = '# 查询接口\n## 接口目录\n查询接口。\n## 兼容契约\n继续返回原有错误码。\n'.encode('utf-8')
        api_path.write_bytes(api_raw)
        api_ref = api_path.relative_to(self.repo).as_posix()
        shared_ref = self.path.relative_to(self.repo).as_posix()
        self.mapping['artifacts'].append({'artifact_id': 'api-design', 'sources': [api_ref, shared_ref],
            'sections': [{'section': role, 'path': api_ref, 'heading': '查询接口 / ' + title}
                         for role, title in (('接口清单', '接口目录'), ('接口详细设计', '兼容契约'))],
            'points': [{'id': 'D2', 'path': api_ref, 'heading': '查询接口 / 兼容契约',
                        'covers': ['F1', 'A1'], 'uses': []}],
            'globals': [{'path': shared_ref, 'heading': '设计 / 公共约束'}]})
        self.mapping['capability_bindings'].append({**self.mapping['capability_bindings'][0], 'artifact_id': 'api-design'})
        flow_path = self.path.with_name('03-产品流程.md')
        flow_ref = flow_path.relative_to(self.repo).as_posix()
        flow_path.write_bytes('''# 查询流程
## 修订记录
初稿。
## 简介
既有查询流程。
## 业务流程总览
> 图表模式：Mermaid 降级
> 降级原因：Node.js 未安装，archify doctor: DEGRADED

```mermaid
flowchart LR
 A[查询] --> B[返回]
```
## 功能设计
保持查询结果和错误码。
## 数据与接口概览
复用已有接口。
## 评审记录
评审结论：通过
'''.encode('utf-8'))
        flow_artifact = ARTIFACT_REGISTRY.get_artifact('business-flow')
        self.mapping['artifacts'].append({'artifact_id': flow_artifact.id, 'sources': [flow_ref],
            'sections': [{'section': name, 'path': flow_ref, 'heading': '查询流程 / ' + name}
                         for name in flow_artifact.sections],
            'points': [{'id': 'F1', 'path': flow_ref, 'heading': '查询流程 / 功能设计',
                        'covers': ['A1'], 'uses': []}], 'globals': []})
        self.mapping['capability_bindings'].append({**self.mapping['capability_bindings'][0],
                                                   'artifact_id': flow_artifact.id})
        for artifact_id, name in (('detail-design', '详细设计.md'), ('database-design', '数据库设计.md')):
            sections = ARTIFACT_REGISTRY.get_artifact(artifact_id).sections
            (package / name).write_text('# 原生设计\n' + ''.join('## ' + section + '\n保持现有行为。\n'
                for section in sections), encoding='utf-8')

        def invoke(action, *options, payload=None):
            return subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'doc-gate'), action,
                str(package), '--json', *options], input=payload, capture_output=True,
                cwd=self.repo, timeout=30)

        prepared = invoke('plan-prepare', '--base', base, '--route', 'standard', '--source-map',
            payload=json.dumps(self.mapping, ensure_ascii=False).encode('utf-8'))
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        frozen = invoke('plan-freeze', '--base', base)
        self.assertEqual(frozen.returncode, 0, frozen.stdout + frozen.stderr)
        requirements = invoke('freeze', '--stage', '需求分析', '--by', '测试维护者')
        self.assertEqual(requirements.returncode, 0, requirements.stdout + requirements.stderr)
        checked = invoke('check', '--stage', '概要设计', '--summary')
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['summary']['fail'], 0)
        request_result = invoke('review-input', '--stage', '概要设计', '--schema-version', '2')
        self.assertEqual(request_result.returncode, 0, request_result.stdout + request_result.stderr)
        delivered = json.loads(request_result.stdout)
        request = delivered['request']
        physical = self.path.relative_to(self.repo).as_posix()
        self.assertEqual(request['mode'], 'full')
        self.assertEqual(dict(request['full_documents']), {physical: raw.decode('utf-8')})
        self.assertEqual(dict(request['doc_hashes']), {physical: hashlib.sha256(raw).hexdigest()})
        self.assertIn('source/source-map.json', dict(request['supporting_context']))
        self.assertFalse((package / '概要设计.md').exists())
        self.assertFalse((package / 'reviews/概要设计.gate.json').exists())
        no_review = invoke('gate', '--stage', '概要设计', '--no-review', '--schema-version', '2')
        self.assertEqual(no_review.returncode, 3, no_review.stdout + no_review.stderr)
        self.assertFalse((package / 'reviews/概要设计.gate.json').exists())
        (package / delivered['receipt_path']).write_text(json.dumps(delivered['receipt'], ensure_ascii=False),
                                                        encoding='utf-8')
        (package / delivered['issues_path']).write_text(json.dumps({'reviewer': 'test-sensor', 'stage': '概要设计',
            'doc_hashes': dict(request['doc_hashes']), 'issues': []}, ensure_ascii=False), encoding='utf-8')
        self.path.write_bytes(raw.replace('原有错误码。'.encode(), '另一错误码。'.encode()))
        stale_review = invoke('gate', '--stage', '概要设计', '--schema-version', '2')
        self.assertEqual(stale_review.returncode, 3, stale_review.stdout + stale_review.stderr)
        self.assertFalse((package / 'reviews/概要设计.gate.json').exists())
        self.path.write_bytes(raw)
        accepted = invoke('gate', '--stage', '概要设计', '--schema-version', '2')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)['doc_hashes'], dict(request['doc_hashes']))
        binding = json.loads((package / 'reviews/概要设计.plan-binding.json').read_bytes())
        self.assertEqual(binding['schema_version'], 2)
        self.assertEqual(dict(binding['doc_hashes']), dict(request['doc_hashes']))
        status = invoke('plan-status', '--base', base)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout)['binding_states']['概要设计'], 'BOUND')
        missing_scope = invoke('freeze', '--stage', '业务流程设计', '--by', '测试维护者')
        self.assertEqual(missing_scope.returncode, 3, missing_scope.stdout + missing_scope.stderr)
        check_flow = invoke('check', '--stage', '业务流程设计', '--summary')
        self.assertEqual(check_flow.returncode, 0, check_flow.stdout + check_flow.stderr)
        flow_review = json.loads(check_flow.stdout)
        original_flow = flow_path.read_bytes()
        self.assertEqual(flow_review['mapped_doc_hashes'], {flow_ref: hashlib.sha256(original_flow).hexdigest()})
        flow_flags = ('--stage', '业务流程设计', '--by', '测试维护者',
                      '--approved-input-sha256', flow_review['approved_input_sha256'])
        flow_path.write_bytes(original_flow + '补充兼容约束。\n'.encode('utf-8'))
        stale_flow = invoke('freeze', *flow_flags)
        self.assertEqual(stale_flow.returncode, 3, stale_flow.stdout + stale_flow.stderr)
        flow_path.write_bytes(original_flow)
        flow = invoke('freeze', *flow_flags)
        self.assertEqual(flow.returncode, 0, flow.stdout + flow.stderr)
        self.assertEqual(json.loads(flow.stdout)['doc_hashes'], flow_review['mapped_doc_hashes'])
        self.assertFalse((package / '业务流程设计.md').exists())
        detail = invoke('review-input', '--stage', '详细设计', '--schema-version', '2')
        self.assertEqual(detail.returncode, 0, detail.stdout + detail.stderr)
        detail_input = json.loads(detail.stdout)
        detail_request = detail_input['request']
        self.assertEqual(detail_request['mode'], 'full')
        self.assertEqual(set(dict(detail_request['doc_hashes'])), {'详细设计.md', '数据库设计.md', api_ref, shared_ref})
        self.assertEqual(dict(detail_request['full_documents'])[api_ref], api_raw.decode('utf-8'))
        detail_context = dict(detail_request['supporting_context'])
        self.assertEqual(detail_context['upstream/业务流程设计/' + flow_ref].encode(), original_flow)
        self.assertNotIn('upstream/概要设计/' + physical, detail_context)
        self.assertEqual(json.loads(detail_context['source/shared-upstream.json']), [
            {'stage': '概要设计', 'path': physical, 'sha256': hashlib.sha256(raw).hexdigest()}])
        (package / detail_input['receipt_path']).write_text(json.dumps(detail_input['receipt'], ensure_ascii=False),
                                                            encoding='utf-8')
        (package / detail_input['issues_path']).write_text(json.dumps({'reviewer': 'test-sensor', 'stage': '详细设计',
            'doc_hashes': dict(detail_request['doc_hashes']), 'issues': []}, ensure_ascii=False), encoding='utf-8')
        detail_gate = invoke('gate', '--stage', '详细设计', '--schema-version', '2')
        self.assertEqual(detail_gate.returncode, 0, detail_gate.stdout + detail_gate.stderr)
        self.assertEqual(json.loads(detail_gate.stdout)['doc_hashes'], dict(detail_request['doc_hashes']))
        self.assertFalse((package / 'API设计.md').exists())
        from aek.adapters.plan_transaction import PlanStore
        gate = runpy.run_path(str(ROOT / 'doc-gate'))
        with PlanStore(package / 'reviews').read_view(package) as view:
            state = gate['_dynamic_state_from_snapshot'](view.snapshot(), req_dir=package)
        checks = gate['Checks']('UI设计')
        scope = gate['_current_ui_coverage'](checks, package, state, False)
        self.assertTrue(scope.non_ui, checks.fails)
        self.assertFalse((package / '概要设计.md').exists())
        original_gate = (package / 'reviews/概要设计.gate.json').read_bytes()
        formatted = raw.replace('保持查询返回。\n'.encode(), '保持查询返回。\r\n'.encode())
        self.assertNotEqual(formatted, raw)
        self.path.write_bytes(formatted)
        material = json.loads(invoke('status', '--schema-version', '2').stdout)['stages']['概要设计']
        self.assertEqual(material['state'], 'DRAFT')
        self.assertEqual(material['material']['classification'], 'EQUIVALENT')
        refreshed = invoke('gate', '--stage', '概要设计', '--refresh-equivalent', '--expected-revision',
            binding['binding_digest'], '--source-hash', physical + '=' + hashlib.sha256(formatted).hexdigest())
        self.assertEqual(refreshed.returncode, 0, refreshed.stdout + refreshed.stderr)
        self.assertEqual(json.loads(refreshed.stdout)['status'], 'REFRESHED')
        self.assertEqual((package / 'reviews/概要设计.gate.json').read_bytes(), original_gate)
        self.assertEqual(json.loads(invoke('plan-status', '--base', base).stdout)['binding_states']['概要设计'], 'BOUND')
        self.path.write_bytes(raw + '\n〈补全本节〉\n'.encode('utf-8'))
        # An external change is checked against the actual source, never a
        # generated mirror. The old review request is left for evidence.
        changed = invoke('check', '--stage', '概要设计', '--summary')
        self.assertEqual(changed.returncode, 1, changed.stdout + changed.stderr)
        self.assertTrue(any(item['group'] == 'placeholder' for item in json.loads(changed.stdout)['failures']))
        stale = invoke('plan-status', '--base', base)
        self.assertEqual(stale.returncode, 0, stale.stdout + stale.stderr)
        self.assertNotEqual(json.loads(stale.stdout)['binding_states']['概要设计'], 'BOUND')

    def test_current_contract_is_read_and_configuration_is_not_execution_or_visibility(self):
        frame = capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))
        self.assertEqual(frame.contracts()[self.contract.relative_to(self.repo).as_posix()], self.contract.read_bytes())
        self.assertFalse(hasattr(frame, 'actually_used'))
        self.assertFalse(hasattr(frame, 'visible'))
        self.contract.write_bytes(b'# changed responsibility\n')
        with self.assertRaisesRegex(ValueError, 'CAPABILITY_CONTRACT_CHANGED'):
            capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))

    def test_first_original_review_receives_full_real_sources_and_explicit_mapping(self):
        from aek.adapters.source_map import mapped_review_input
        self.path.write_bytes(self.raw + '## 未映射约束\n必须保持鉴权错误码。\n'.encode())
        source_map = load_source_map(self.mapping)
        frame = capture_mapped_sources(self.repo, source_map, ('outline-design',), external_ids=('A1',))
        request, material = mapped_review_input(frame, source_map, '概要设计', '0' * 64)
        self.assertEqual(request.mode, 'full')
        path = self.path.relative_to(self.repo).as_posix()
        self.assertEqual(dict(request.full_documents)[path].encode(), self.path.read_bytes())
        self.assertEqual(dict(request.doc_hashes)[path], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(material.document_map()[path].encode(), self.path.read_bytes())
        context = dict(request.supporting_context)
        self.assertIn('F1', context['source/source-map.json'])
        self.assertIn('FULL_REQUIRED', context['source/source-boundaries.json'])
        self.assertIn('source/contracts/' + self.contract.relative_to(self.repo).as_posix(), context)
        self.assertNotIn('概要设计.md', dict(request.full_documents))

    def test_original_plan_freeze_requires_current_composition_evidence_and_contract(self):
        from aek.adapters.source_map import capture_composition
        from aek.adapters.plan_transaction import PlanStore
        from aek.adapters.change_scope import scan_negative_risks
        from aek.application.planning import (prepare_git_current_plan, freeze_git_current_plan,
            git_plan_input_digest, read_frozen_dynamic_state)
        base = self.git_base()
        store = PlanStore(self.repo / self.package.design_root / 'reviews')
        source_map = load_source_map(self.mapping)
        capture = lambda current: capture_composition(self.repo, current)
        with self.assertRaisesRegex(ValueError, 'current source verifier'):
            prepare_git_current_plan(store, self.repo, base, 'standard', scan_scope=scan_negative_risks,
                                     source_map=source_map)
        self.assertNotIn('artifact-plan.json', store.snapshot())
        with self.assertRaisesRegex(ValueError, 'current source epoch'):
            prepare_git_current_plan(store, self.repo, base, 'standard', scan_scope=scan_negative_risks,
                source_map=source_map, verify_source_map=lambda _mapping: {'status': 'PASS'})
        self.assertNotIn('artifact-plan.json', store.snapshot())
        prepared = prepare_git_current_plan(store, self.repo, base, 'standard', scan_scope=scan_negative_risks,
            source_map=source_map, verify_source_map=capture)
        self.assertEqual(prepared.state, 'PREPARED', prepared.reason)
        identity = git_plan_input_digest(prepared.plan, base)
        missing = freeze_git_current_plan(store, self.repo, base, identity, scan_scope=scan_negative_risks,
                                         frozen_at='fixture-time')
        self.assertEqual(missing.state, 'INVALID')
        self.assertNotIn('artifact-plan.gate.json', store.snapshot())
        original_contract = self.contract.read_bytes()
        self.contract.write_bytes(original_contract + b'changed scope\n')
        changed = freeze_git_current_plan(store, self.repo, base, identity, scan_scope=scan_negative_risks,
                                         frozen_at='fixture-time', verify_source_map=capture)
        self.assertEqual(changed.state, 'INVALID')
        self.assertNotIn('artifact-plan.gate.json', store.snapshot())
        self.contract.write_bytes(original_contract)
        contract = self.contract
        class MutateAfterCredentialPublication:
            def __getattr__(self, name):
                return getattr(store, name)

            def publish(self, changes, **kwargs):
                store.publish(changes, **kwargs)
                if changes.get('artifact-plan.gate.json') is not None:
                    contract.write_bytes(original_contract + b'changed immediately after real publication\n')
        raced = freeze_git_current_plan(MutateAfterCredentialPublication(), self.repo, base, identity,
            scan_scope=scan_negative_risks, frozen_at='fixture-time', verify_source_map=capture)
        self.assertEqual(raced.state, 'INVALID')
        self.assertNotIn('artifact-plan.gate.json', store.snapshot())
        self.assertNotEqual(read_frozen_dynamic_state(store, identity).state, 'FROZEN')
        self.contract.write_bytes(original_contract)
        from aek.core.artifact.plan import bind_stage, load_credential_bytes
        from dataclasses import asdict
        import json
        concurrent = {}
        class ConcurrentConsumerAfterCredentialPublication:
            def __getattr__(self, name):
                return getattr(store, name)

            def publish(self, changes, **kwargs):
                store.publish(changes, **kwargs)
                if changes.get('artifact-plan.gate.json') is None:
                    return
                credential = load_credential_bytes(changes['artifact-plan.gate.json'])
                # Controlled competing consumer fact, not a real stage approval.
                binding = bind_stage(prepared.plan, credential, '概要设计', gate_digest='f' * 64,
                    doc_hashes={self_path: hashlib.sha256(self_raw).hexdigest()},
                    prerequisite_fingerprints={'需求分析': 'a' * 64})
                concurrent['raw'] = json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()
                _snapshot, current_consumers = store.snapshot_with_digest()
                store.publish({'概要设计.plan-binding.json': concurrent['raw']}, expected_consumers=current_consumers)
                contract.write_bytes(original_contract + b'expired after competing consumer\n')
        self_path, self_raw = self.path.relative_to(self.repo).as_posix(), self.path.read_bytes()
        competed = freeze_git_current_plan(ConcurrentConsumerAfterCredentialPublication(), self.repo, base, identity,
            scan_scope=scan_negative_risks, frozen_at='fixture-time', verify_source_map=capture)
        self.assertEqual(competed.state, 'INVALID')
        self.assertIn('concurrent state changed', competed.reason)
        self.assertEqual(store.snapshot()['概要设计.plan-binding.json'], concurrent['raw'])
        self.assertEqual(read_frozen_dynamic_state(store, identity, verify_source_map=capture).state, 'INVALID')
        # Reset only this temporary fixture's controlled competing consumers
        # through the same real CAS before the independent positive freeze.
        _snapshot, consumers = store.snapshot_with_digest()
        store.publish({'概要设计.plan-binding.json': None, 'artifact-plan.gate.json': None}, expected_consumers=consumers)
        self.contract.write_bytes(original_contract)
        frozen = freeze_git_current_plan(store, self.repo, base, identity, scan_scope=scan_negative_risks,
                                        frozen_at='fixture-time', verify_source_map=capture)
        self.assertEqual(frozen.state, 'FROZEN', frozen.reason)
        self.assertEqual(frozen.plan.source_map, prepared.plan.source_map)
        self.assertEqual(read_frozen_dynamic_state(store, identity, verify_source_map=capture).state, 'FROZEN')
        self.assertEqual(read_frozen_dynamic_state(store, identity).state, 'INVALID')
        self.contract.write_bytes(original_contract + b'expired contract\n')
        self.assertEqual(read_frozen_dynamic_state(store, identity, verify_source_map=capture).state, 'INVALID')
        self.contract.unlink()
        self.assertEqual(read_frozen_dynamic_state(store, identity, verify_source_map=capture).state, 'INVALID')

    def test_captures_whole_actual_file_and_selected_byte_identity_without_creating_mirrors(self):
        frame = capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',), external_ids=('A1',))
        self.assertEqual(frame.sources()[self.path.relative_to(self.repo).as_posix()], self.raw)
        point = next(row for row in frame.locations if row.category == 'points')
        expected = '## 查询功能\n保持查询返回。\n'.encode('utf-8')
        self.assertEqual(point.sha256, hashlib.sha256(expected).hexdigest())
        self.assertEqual(frame.status, 'COMPLETE_BOUNDARY')
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertFalse((self.repo / self.package.design_root / '概要设计.md').exists())
        self.assertFalse(hasattr(frame, 'approved'))

    def test_unmapped_content_requires_full_review_instead_of_disappearing(self):
        self.path.write_bytes(self.raw + '## 未映射约束\n所有接口必须加密。\n'.encode())
        frame = capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',), external_ids=('A1',))
        self.assertEqual(frame.status, 'FULL_REQUIRED')
        self.assertTrue(frame.gaps)
        self.assertIn('所有接口必须加密。'.encode(), frame.sources()[self.path.relative_to(self.repo).as_posix()])

    def test_uncaptured_provider_declaration_is_not_evidence_for_a_reference(self):
        other = copy.deepcopy(self.mapping['artifacts'][0])
        other['artifact_id'] = 'requirements-analysis'
        path = 'docs/不存在的需求/需求.md'
        other['sources'] = [path]
        other['sections'] = [{'section': section, 'path': path, 'heading': section}
                             for section in ARTIFACT_REGISTRY.artifacts['requirements-analysis'].sections]
        other['points'] = [{'id': 'A1', 'path': path, 'heading': '需求', 'covers': [], 'uses': []}]
        other['globals'] = []
        self.mapping['artifacts'].append(other)
        self.mapping['capability_bindings'].append({**self.mapping['capability_bindings'][0],
            'artifact_id': 'requirements-analysis', 'writer': 'requirements-author'})
        frame = capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))
        self.assertEqual(frame.status, 'FULL_REQUIRED')
        self.assertTrue(any(gap.startswith('UNKNOWN_REFERENCE:') for gap in frame.gaps))

    def test_shared_file_other_role_content_remains_visible_and_forces_full_boundary(self):
        self.path.write_bytes(self.raw + '## 需求安全约束\n必须进行权限验证。\n'.encode())
        other = copy.deepcopy(self.mapping['artifacts'][0])
        other['artifact_id'] = 'requirements-analysis'
        other['sections'] = [{'section': section, 'path': other['sources'][0], 'heading': '设计 / 需求安全约束'}
                             for section in ARTIFACT_REGISTRY.artifacts['requirements-analysis'].sections]
        other['points'] = [{'id': 'A1', 'path': other['sources'][0], 'heading': '设计 / 需求安全约束',
                            'covers': [], 'uses': []}]
        other['globals'] = []
        self.mapping['artifacts'].append(other)
        self.mapping['capability_bindings'].append({**self.mapping['capability_bindings'][0],
            'artifact_id': 'requirements-analysis'})
        frame = capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',), external_ids=('A1',))
        self.assertEqual(frame.status, 'FULL_REQUIRED')
        self.assertTrue(any(row.artifact_id == 'requirements-analysis' for row in frame.locations))
        self.assertTrue(any(gap.startswith('CROSS_ROLE_CONTENT:') for gap in frame.gaps))

    def test_wrong_declared_digest_and_missing_actual_heading_are_not_accepted(self):
        self.mapping['artifacts'][0]['points'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SOURCE_CHANGED'):
            capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))
        self.mapping['artifacts'][0]['points'][0].pop('sha256')
        self.mapping['artifacts'][0]['points'][0]['heading'] = '设计 / 不存在的功能'
        with self.assertRaisesRegex(ValueError, 'MISSING_ARTIFACT_CONTENT'):
            capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))

    def test_duplicate_actual_headings_are_not_disambiguated_by_parser_cache_names(self):
        self.path.write_bytes(self.raw + '## 查询功能\n另一处同名但不同权限的查询。\n'.encode())
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))

    @unittest.skipIf(os.name == 'nt', 'POSIX link fixture; shared native reparse tests cover Windows')
    def test_source_symlink_cannot_escape_the_actual_repository(self):
        outside = self.repo.parent / (self.repo.name + '-outside.md')
        outside.write_bytes(b'user private content')
        self.addCleanup(outside.unlink)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            capture_mapped_sources(self.repo, load_source_map(self.mapping), ('outline-design',))
        self.assertEqual(outside.read_bytes(), b'user private content')


if __name__ == '__main__':
    unittest.main()
