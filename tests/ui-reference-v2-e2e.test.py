"""Installed Chinese v2 protocol regression, not production visual approval.

All screenshots, observations, reviewers and PTY decisions are synthetic test
data. A real kernel PTY exercises the interaction boundary; it does not certify
human visual intent, browser rendering or Windows ConPTY support.
Set AEK_UI_V2_SOURCE_ONLY=1 for development without copying a moving source kit.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = runpy.run_path(str(ROOT / 'tests/ui-reference-e2e.test.py'))
NAME = '018-中文多状态原型'
CHANGE = 'CHG-018'
TIME = '2026-10-20T00:00:00Z'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def block(kind, value):
    return '\n```' + kind + '\n' + json.dumps(value, ensure_ascii=False) + '\n```\n'


class ChineseV2EndToEndTests(unittest.TestCase):
    def invoke(self, command, *arguments, payload=None, expected=0):
        result = subprocess.run(command + list(arguments), cwd=ROOT, env=self.env,
            input=None if payload is None else json.dumps(payload, ensure_ascii=False),
            capture_output=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / '项目仓库'
        self.repo.mkdir()
        self.env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONUTF8': '1',
            'PYTHONIOENCODING': 'utf-8', 'XDG_CONFIG_HOME': str(self.base / 'config')}
        if os.environ.get('AEK_UI_V2_SOURCE_ONLY') == '1':
            self.gate = [sys.executable, str(ROOT / 'doc-gate')]
            self.helper = [sys.executable, str(ROOT / 'skills/design-pipeline/scripts/ui-reference.py')]
            # init requires an existing repository marker; no source is copied.
            (self.repo / '.git').mkdir()
            (self.repo / '.repo-memory-kit').mkdir()
            requirement_root = self.repo / 'docs/01-需求'
            requirement_root.mkdir(parents=True)
            shutil.copytree(ROOT / 'templates/requirements', requirement_root / '_模板')
            (requirement_root / 'README.md').write_text('| 编号 | 需求名称 | 状态 |\n| --- | --- | --- |\n', encoding='utf-8')
        else:
            self.invoke([sys.executable, '-m', 'installer'], '--link-strategy', 'copy', str(self.repo))
            self.gate = [sys.executable, str(self.repo / '.repo-memory-kit/bin/doc-gate')]
            self.helper = [sys.executable, str(self.repo / '.agents/skills/design-pipeline/scripts/ui-reference.py')]
        self.invoke(self.gate, 'init', NAME, '--repo', str(self.repo), '--json')
        self.req = self.repo / 'docs/03-SDD' / NAME
        self.material = self.repo / 'docs/01-需求' / NAME / '原始材料/ui-reference'
        self.index_path = self.material / '截图索引.json'
        self.common = ('--repo-root', str(self.repo), '--change-id', CHANGE, '--requirement-dir', NAME)

    def helper_call(self, operation, payload=None, *, expected=0, cas=False):
        arguments = self.common
        if operation in {'scaffold', 'validate', 'record-review', 'bind-prototype'}:
            # Exercise the public explicit v2 common envelope, not the legacy
            # helper payload with only the prototype manifest upgraded to v2.
            payload = dict(payload or {})
            expected_sha = None if operation == 'scaffold' else sha(self.index_path.read_bytes())
            if operation == 'scaffold':
                targets = [row['id'] for row in payload['screenshots']]
            elif operation == 'record-review':
                targets = sorted({row['screenshot_id'] for row in payload['updates']})
            elif operation == 'bind-prototype':
                targets = sorted({ident for row in payload['prototype_refs'] for ident in row['depends_on']})
            else:
                targets = [row['id'] for row in json.loads(self.index_path.read_bytes())['screenshots']]
            payload.update(schema_version=2, change_id=CHANGE, requirement_dir=NAME,
                           targets=targets, expected_index_sha256=expected_sha)
            arguments += ('--schema-version', '2')
            if operation in {'record-review', 'bind-prototype'}:
                arguments += ('--expected-index-sha256', expected_sha)
        elif cas:
            arguments += ('--expected-index-sha256', sha(self.index_path.read_bytes()))
        result = self.invoke(self.helper, operation, *arguments, payload=payload, expected=expected)
        if operation in {'scaffold', 'validate', 'record-review', 'bind-prototype'}:
            response = json.loads(result.stdout)
            self.assertEqual(response['schema_version'], 2)
            self.assertEqual(response['operation'], operation)
            self.assertEqual(response['change_id'], CHANGE)
            self.assertEqual(response['index_sha256'], sha(self.index_path.read_bytes()))
            self.assertEqual({row['id'] for row in response['targets']}, set(targets))
        return result

    def prepare(self, targets, *, expected=0):
        arguments = ['prototype-prepare', str(self.req), '--json']
        for target in targets:
            arguments += ['--target', '/'.join(target)]
        return self.invoke(self.gate, *arguments, expected=expected)

    def confirm_in_real_pty(self, prepared, targets):
        import errno
        import pty
        import select
        import time
        command = self.gate + ['prototype-confirm', str(self.req), '--schema-version', '2',
            '--reviewer', 'synthetic-protocol-reviewer', '--prepared-sha256', prepared['prepare_sha256']]
        for target in targets:
            command += ['--target', '/'.join(target)]
        master, slave = pty.openpty()
        process, output, sent = None, bytearray(), 0
        try:
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                cwd=ROOT, env=self.env, close_fds=True)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        raw = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not raw:
                        break
                    output.extend(raw)
                    self.assertLess(len(output), 256 * 1024)
                    # PTY reads may split a UTF-8 character. Only the final
                    # complete stream is decoded strictly for result parsing.
                    text = output.decode('utf-8', errors='replace')
                    if not sent and '或 REVISE/BLOCK/CANCEL' in text:
                        phrase = re.search(r'ACCEPT [0-9a-f]{12}', text).group()
                        self.assertEqual(phrase, 'ACCEPT ' + prepared['prepare_sha256'][:12])
                        os.write(master, (phrase + '\n').encode('utf-8'))
                        sent += 1
                elif process.poll() is not None:
                    break
            self.assertEqual(process.wait(timeout=3), 0, output.decode('utf-8'))
            self.assertEqual(sent, 1)
            result = json.loads(next(line for line in reversed(output.decode('utf-8').splitlines())
                if line.startswith('{"schema_version"')))
            self.assertEqual([row['status'] for row in result['per_item']], ['ACCEPTED', 'ACCEPTED'])
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if slave is not None:
                os.close(slave)
            os.close(master)

    @unittest.skipUnless(os.name == 'posix', 'full interaction requires real kernel PTY; no fake Windows TTY')
    def test_chinese_observation_state_context_confirmation_freeze_and_local_invalidation(self):
        screenshots = [dict(id=ident, page='查询列表', state=state, purpose='合成协议截图，不是业务视觉验收',
            feature_ids=['F1'], acceptance_ids=['A1'], permissions=dict(function_id='FP01', data_id='DP01'))
            for ident, state in (('P1', 'default'), ('P2', 'loading'))]
        design_system = dict(name='Ant Design', source='user', evidence=[])
        request = dict(design_system=design_system,
            coverage=[dict(kind=kind, id=ident, screenshot_ids=['P1', 'P2'])
                for kind, ident in (('feature', 'F1'), ('acceptance', 'A1'))], screenshots=screenshots)
        self.helper_call('scaffold', request)
        index = json.loads(self.index_path.read_bytes())
        self.assertEqual(index['requirement_dir'], NAME)
        self.assertFalse((self.repo / 'docs/01-需求' / CHANGE).exists())
        for row in index['screenshots']:
            self.assertEqual((self.material / row['path']).read_bytes(), b'')
            self.assertEqual(row['placeholder_sha256'], sha(b''))
        self.helper_call('validate', expected=4)
        images = {}
        for number, row in enumerate(index['screenshots'], 1):
            images[row['id']] = self.material / row['path']
            images[row['id']].write_bytes(FIXTURE['image'](bytes([number, 20, 30])))
        self.helper_call('validate', expected=4)
        self.assertTrue(all(row['validation']['status'] == 'valid'
            and row['applies_to'][0]['content_review']['status'] == 'unverified'
            for row in json.loads(self.index_path.read_bytes())['screenshots']))
        observations = {}
        for row in screenshots:
            prepared = json.loads(self.helper_call('observation-prepare', dict(schema_version=2,
                evidence_id=row['id'], context_ref=row['id'] + '-CAPTURED', required_regions=['QUERY']), cas=True).stdout)
            recorded = json.loads(self.helper_call('observation-record', dict(schema_version=2,
                prepare_ref=prepared['prepare_ref'], prepare_sha256=prepared['prepare_sha256'],
                candidate=dict(schema_version=2, regions=[dict(region_id='QUERY', observation_kind='observed',
                    layout='仅合成布局测试', controls=[], style_clues=[])], uncertainties=['非实际业务视觉核实']),
                provenance=dict(channel='agent_visual', model=None, observed_at=TIME)), cas=True).stdout)
            observations[row['id']] = recorded['observation_sha256']
        review = dict(updates=[dict(screenshot_id=row['id'], context_id=row['id'] + '-CAPTURED',
            content_review=dict(status='applicable', reviewer_type='maintainer', reviewer='synthetic-reviewer',
                reviewed_at=TIME, rationale='自动化合成协议夹具，不代表真实人工看图'),
            freshness=dict(status='confirmed_current', confirmer_type='maintainer', confirmer='synthetic-reviewer',
                confirmed_at=TIME)) for row in screenshots])
        self.helper_call('record-review', review, cas=True)
        self.helper_call('validate')

        requirements = FIXTURE['REQUIREMENTS'].replace('- 签核人：k', '- 签核人：synthetic-reviewer')
        (self.req / '需求分析.md').write_text(requirements, encoding='utf-8')
        targets = [('PAGE-1', 'AREA-1', row['state'], row['id'] + '-CAPTURED') for row in screenshots]
        classification = dict(schema_version=2, requirements_sha256=sha(requirements.encode()), items=[
            dict(id=ident, classification='UI', reason='合成多状态协议', targets=[
                dict(zip(('page_id', 'area_id', 'state_id', 'context_id'), target)) for target in targets])
            for ident in ('F1', 'A1')])
        overview = ('# 概要设计\n## 设计目标\n查询。\n## 现状分析\n沿用。\n## 总体方案\n显示。\n'
            '## 功能点设计\n' + FIXTURE['FEATURE'] + '\n## UI 分类\n'
            + block('aek-ui-classification', classification)
            + '\n## 方案取舍\n沿用。\n## 数据与接口影响\n不变。\n## 风险\n无新增。\n')
        (self.req / '概要设计.md').write_text(overview, encoding='utf-8')
        self.invoke(self.gate, 'freeze', str(self.req), '--stage', '需求分析', '--by', 'synthetic-reviewer')
        self.invoke(self.gate, 'gate', str(self.req), '--stage', '概要设计', '--no-review')
        (self.req / 'prototypes').mkdir()
        gate_sha = sha((self.req / 'reviews/概要设计.gate.json').read_bytes())
        root_rel = self.req.relative_to(self.repo).as_posix()
        prototypes, regions = [], []
        for row, target in zip(screenshots, targets):
            html = ('<!doctype html><html><body>合成状态 ' + row['state'] + '</body></html>').encode()
            path = 'prototypes/' + row['id'] + '.html'
            (self.req / path).write_bytes(html)
            reference = dict(prototype_path=path, prototype_sha256=sha(html), page_id='PAGE-1', area_id='AREA-1',
                state_id=row['state'], context_id=target[3], feature_ids=['F1'], acceptance_ids=['A1'],
                change_intent='合成状态隔离回归', depends_on=[row['id']])
            self.helper_call('bind-prototype', dict(schema_version=2, artifact_plan_id='synthetic-static-018',
                gate_digest=gate_sha, approved_root=root_rel, prototype_refs=[reference]), cas=True)
            prototypes.append(dict(reference, artifact_plan_id='synthetic-static-018', gate_digest=gate_sha,
                approved_root=root_rel, design_system_sha256=FIXTURE['canonical'](design_system),
                evidence_contexts=[dict(evidence_id=row['id'], context_id=target[3])]))
            regions.append(dict(page_id='PAGE-1', area_id='AREA-1', state_id=row['state'], context_id=target[3],
                region_id='QUERY', intent='PRESERVE', feature_ids=['F1'], acceptance_ids=['A1'], source_ids=[],
                screenshot_ids=[row['id']], rationale='合成截图的局部依赖', adjacent_context=None,
                observations=[dict(evidence_id=row['id'], sha256=observations[row['id']])]))
        ui = ('# UI设计\n## 界面目标\n查询。\n## 用户核心任务\n查看。\n## 信息架构\n列表。\n'
            '## 页面清单\n' + FIXTURE['PAGE'].replace('UI-EV P1', 'UI-EV P1 / P2')
            + '\n## 设计系统落地\nAnt Design。\n## 交互规则\n状态反馈。\n## 响应式与兼容\n沿用。\n'
            '## 可访问性\n键盘。\n## 验证清单\n合成协议待确认。\n## 原型确认记录\n'
            '<!-- aek-prototypes:required -->\n' + block('aek-prototypes', dict(schema_version=2, prototypes=prototypes))
            + '\n## 设计基线\n' + block('aek-ui-baseline', dict(schema_version=2, sources=[], regions=regions)))
        ui_path = self.req / 'UI设计.md'
        ui_path.write_text(ui, encoding='utf-8')
        prepared = json.loads(self.prepare(targets).stdout)
        self.assertNotIn('ACCEPTED', json.dumps(prepared))
        self.assertFalse((self.req / 'reviews/ui-prototype-confirmations').exists())
        command = ['prototype-confirm', str(self.req), '--schema-version', '2', '--reviewer', 'synthetic-reviewer',
                   '--prepared-sha256', prepared['prepare_sha256']]
        for target in targets:
            command += ['--target', '/'.join(target)]
        self.invoke(self.gate, *command, payload={}, expected=3)
        self.assertFalse((self.req / 'reviews/ui-prototype-confirmations').exists())
        self.confirm_in_real_pty(prepared, targets)
        receipts = self.req / 'reviews/ui-prototype-confirmations'
        receipt_bytes = {path.name: path.read_bytes() for path in receipts.glob('v2-*.json')}
        self.assertEqual(len(receipt_bytes), 2)
        self.invoke(self.gate, 'check', str(self.req), '--stage', 'UI设计', '--json')
        (self.req / 'reviews/UI设计.issues.json').write_text(json.dumps(dict(stage='UI设计',
            doc_hashes={'UI设计.md': sha(ui_path.read_bytes())}, issues=[])), encoding='utf-8')
        self.invoke(self.gate, 'gate', str(self.req), '--stage', 'UI设计', '--json')
        state = json.loads(self.invoke(self.gate, 'status', str(self.req), '--json').stdout)
        self.assertEqual(state['UI设计']['state'], 'FROZEN')
        images['P2'].write_bytes(FIXTURE['image'](b'\x66\x77\x88'))
        state = json.loads(self.invoke(self.gate, 'status', str(self.req), '--json').stdout)
        self.assertEqual(state['UI设计']['state'], 'DRAFT')
        self.prepare([targets[1]], expected=3)
        unchanged = json.loads(self.prepare([targets[0]]).stdout)
        self.assertEqual(unchanged['per_item'][0], prepared['per_item'][0])
        self.assertEqual(receipt_bytes, {path.name: path.read_bytes() for path in receipts.glob('v2-*.json')})
        self.helper_call('validate', expected=4)
        rows = {row['id']: row for row in json.loads(self.index_path.read_bytes())['screenshots']}
        self.assertEqual(rows['P1']['applies_to'][0]['content_review']['status'], 'applicable')
        self.assertEqual(rows['P2']['applies_to'][0]['content_review']['status'], 'unverified')
        self.assertEqual(json.loads(self.prepare([targets[0]]).stdout)['per_item'][0], prepared['per_item'][0])

    def test_artifact_plan_obligation_cannot_be_removed_with_ui_markers(self):
        from aek.adapters.plan_transaction import PlanStore
        (self.req / 'UI设计.md').write_text('# UI设计\n没有required或原型JSON区块。\n', encoding='utf-8')
        baseline = json.loads(self.invoke(self.gate, 'check', str(self.req), '--stage', 'UI设计',
            '--json', expected=1).stdout)
        self.assertFalse(any(row['group'] == 'prototype' and row['status'] == 'FAIL'
            for row in baseline['checks']), 'legacy v1 without upstream obligation must retain its original opt-in behavior')
        fixture = runpy.run_path(str(ROOT / 'tests/ui-prototype-gate.test.py'))
        state = fixture['required_ui_state']()
        (self.req / 'reviews').mkdir(exist_ok=True)
        store = PlanStore(self.req / 'reviews')
        _, expected = store.snapshot_with_digest()
        store.publish({'artifact-plan.json': json.dumps(asdict(state.plan), ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode(),
            'artifact-plan.gate.json': json.dumps(asdict(state.credential), ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()}, expected_consumers=expected)
        checked = self.invoke(self.gate, 'check', str(self.req), '--stage', 'UI设计', '--json', expected=1)
        payload = json.loads(checked.stdout)
        self.assertTrue(any(row['group'] == 'prototype' and row['status'] == 'FAIL' for row in payload['checks']))


if __name__ == '__main__':
    unittest.main()
