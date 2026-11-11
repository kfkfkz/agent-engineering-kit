"""Interactive preparation is source-bound and carries no execution authority."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aek.core.artifact.ui_interaction import load_interaction_spec, validate_interactive_html
from aek.core.artifact.ui_preview import PreviewRejected, validate_html

spec = importlib.util.spec_from_file_location('interactive_preview_fixture',
    ROOT / 'tests/ui-prototype-preview.test.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


HTML = b"""<!doctype html><form onsubmit="return false">
<input id="name" required><button type="button" onclick="show()">Open</button>
</form><dialog id="result">Result</dialog><script>
function show() { document.getElementById('result').showModal(); }
</script>"""


def interaction_bytes(*steps):
    return json.dumps({'schema_version': 1, 'sequences': [{'name': 'review', 'steps': steps}]},
                      ensure_ascii=False).encode('utf-8')


class InteractionPolicyTests(unittest.TestCase):
    def test_interactive_and_static_use_distinct_validation(self):
        validate_interactive_html(HTML, sha(HTML))
        with self.assertRaises(PreviewRejected):
            validate_html(HTML, sha(HTML))
        for raw in (b'<script type="module">console.log(1)</script>', b'<script src="app.js"></script>',
                    b'<form action="next"></form>', b'<input type="file">', b'<a download href="#local">save</a>',
                    b'<style>body {background: url(image.png)}</style>', b'<iframe></iframe>'):
            with self.subTest(raw=raw), self.assertRaisesRegex(PreviewRejected, 'UNSUPPORTED_RESOURCE'):
                validate_interactive_html(raw, sha(raw))

    def test_spec_actions_and_aggregate_limits(self):
        fill = {'action': 'fill', 'selector': '#name', 'value': '演示'}
        raw = interaction_bytes(fill, {'action': 'click', 'selector': 'button'},
            {'action': 'capture', 'state': 'dialog'})
        actions = load_interaction_spec(raw, sha(raw))
        self.assertEqual([step.action for step in actions[0].steps], ['fill', 'click', 'capture'])
        for raw in (interaction_bytes(*([fill] * 33)), interaction_bytes({**fill, 'value': '中' * 342}),
                    interaction_bytes({'action': 'key', 'key': 'F2'}), interaction_bytes({**fill, 'frame': 'child'}),
                    interaction_bytes({'action': 'evaluate', 'value': '1'}),
                    b'{"schema_version":1,"schema_version":1,"sequences":[]}'):
            with self.subTest(prefix=raw[:80]), self.assertRaises(PreviewRejected):
                load_interaction_spec(raw, sha(raw))


class InteractivePreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PrototypePreviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.git('init', '-q')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--allow-empty', '-qm', 'base')
        requirements = self.f.req / '需求分析.md'
        requirements.write_bytes(('## 场景与验收依据\n### 场景 1：查看\n'
            '- **前置**：打开页面\n- **操作**：选择并提交\n- **结果**：显示结果\n'
            '## 验收项\n').encode('utf-8') + requirements.read_bytes())
        self.gate('plan-prepare', '--base', 'HEAD', '--route', 'standard', '--require', 'ui-design', '--json')
        self.gate('plan-freeze', '--base', 'HEAD', '--json')
        self.bind('需求分析')
        page, area, state, context = self.fixture.target
        target = dict(page_id=page, area_id=area, state_id=state, context_id=context)
        overview = self.f.req / '概要设计.md'
        classification = dict(schema_version=2, requirements_sha256=sha((self.f.req / '需求分析.md').read_bytes()),
            items=[dict(id=ident, classification='UI', reason='本轮页面交互', targets=[target]) for ident in ('F1', 'A1')])
        overview_raw = (overview.read_bytes() + b'\n## UI classification\n```aek-ui-classification\n'
            + json.dumps(classification, ensure_ascii=False).encode('utf-8') + b'\n```\n')
        overview.write_bytes(overview_raw.replace(b'\n', b'\r\n'))
        self.bind('概要设计')
        self.spec_path = 'prototypes/interactions.json'
        self.interactions = interaction_bytes({'action': 'click', 'selector': 'button'},
                                               {'action': 'capture', 'state': 'dialog'})
        (self.f.req / self.spec_path).write_bytes(self.interactions)
        (self.f.req / self.f.path).write_bytes(HTML)
        plan = self.read_review('artifact-plan.json')
        credential = self.read_review('artifact-plan.gate.json')
        item = self.fixture.fixture.items[0]
        item.update(prototype_mode='interactive-v1', prototype_sha256=sha(HTML),
            artifact_plan_id=plan['digest'], gate_digest=credential['credential_digest'],
            interaction_spec={'path': self.spec_path, 'sha256': sha(self.interactions)})
        ref = self.f.index['screenshots'][0]['prototype_refs'][0]
        ref.update(schema_version=3, **{key: item[key] for key in ('prototype_mode', 'prototype_sha256',
            'artifact_plan_id', 'gate_digest', 'interaction_spec')})
        (self.f.material / '截图索引.json').write_bytes(json.dumps(self.f.index, ensure_ascii=False).encode('utf-8'))
        text = self.f.ui_text + '```aek-prototypes\n' + json.dumps({'schema_version': 3, 'prototypes': [item]}) + '\n```\n'
        (self.f.req / 'UI设计.md').write_bytes(text.encode('utf-8'))

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.f.repo, check=True, capture_output=True, timeout=20)

    def gate(self, command, *args, expected=0):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'doc-gate'), command, str(self.f.req), *args],
                                cwd=self.f.repo, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def read_review(self, name):
        return json.loads((self.f.req / 'reviews' / name).read_bytes())

    def bind(self, stage):
        # These isolated Gate fixtures represent prior approvals for the test.
        document = stage + '.md'
        data = {'stage': stage, 'status': 'PASS', 'doc_hashes': {document: sha((self.f.req / document).read_bytes())}}
        (self.f.req / 'reviews' / (stage + '.gate.json')).write_bytes(json.dumps(data, ensure_ascii=False).encode())
        self.gate('plan-bind', '--stage', stage, '--base', 'HEAD', '--json')

    def prepare(self, *flags, expected=0):
        return self.gate('prototype-prepare', '--target', '/'.join(self.fixture.target), *flags, expected=expected)

    def test_pending_preview_and_raw_spec_staleness(self):
        before = self.fixture.snapshot()
        self.prepare('--json', expected=3)
        prepared = json.loads(self.prepare('--schema-version', '3', '--json'))
        self.assertEqual(prepared['schema_version'], 3)
        self.assertEqual(prepared['per_item'][0]['spec_sha256'], sha(self.interactions))
        self.assertEqual(prepared['per_item'][0]['interaction_status'], 'PENDING')
        report = self.prepare('--schema-version', '3', '--preview-html')
        self.assertIn('PENDING', report)
        self.assertNotIn('<script', report)
        self.assertNotIn('onclick=', report)
        self.assertEqual(self.fixture.snapshot(), before)
        checks = json.loads(self.gate('check', '--stage', 'UI设计', '--json', expected=1))
        self.assertIn('INTERACTION_UNAVAILABLE', json.dumps(checks))
        (self.f.req / self.spec_path).write_bytes(self.interactions + b'\n')
        conflict = json.loads(self.prepare('--schema-version', '3', '--json', expected=3))
        self.assertIn('specification digest', conflict['error'])

    @unittest.skipUnless(sys.platform == 'linux', 'native host store and fixture scope')
    def test_original_ui_gate_requires_current_signed_host_confirmation(self):
        self.exercise_original_ui_gate(mapped=False)

    @unittest.skipUnless(sys.platform == 'linux', 'native host store and fixture scope')
    def test_original_gate_freezes_external_ui_source_without_a_native_copy(self):
        self.exercise_original_ui_gate(mapped=True)

    def exercise_original_ui_gate(self, *, mapped):
        from aek.adapters.security_host import TerminalHostEventSource
        from aek.adapters.ui_interactive_host import LinuxInteractiveHostProfile
        from aek.adapters.ui_interactive_renderer import PROFILE_ID, InteractionCapture
        from aek.application.local_host import LocalToolHost
        from aek.core.artifact.ui_preview import RenderConditions

        commands = runpy.run_path(str(ROOT / 'doc-gate'))
        host_tests = runpy.run_path(str(ROOT / 'tests/ui-render-host.test.py'))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host_fixture = host_tests['Fixture'](Path(temporary.name))
        repo = Path(host_fixture.authority.repository_ref)
        shutil.copytree(self.f.repo, repo, dirs_exist_ok=True)
        req = repo / self.f.req.relative_to(self.f.repo)
        target = self.fixture.target
        def invoke(action, *args, trusted=True):
            output = io.StringIO()
            with redirect_stdout(output):
                result = commands['main']([action, str(req), *args], interaction_profiles=profiles if trusted else None)
            return result, output.getvalue()
        path = req / 'UI设计.md'
        text = path.read_text(encoding='utf-8')
        text = text.replace('## 原型确认记录', '- 其他状态不适用\n\n## 原型确认记录')
        for name in commands['STAGES']['UI设计']['sections']['UI设计.md']:
            if f'## {name}\n' not in text:
                text += f'\n## {name}\n采用当前页面设计。\n'
        path.write_bytes(text.encode('utf-8'))
        index = repo / self.f.material.relative_to(self.f.repo) / '截图索引.json'
        prototype_base = req
        if mapped:
            from aek.core.artifact.registry import ARTIFACT_REGISTRY
            prototype_base = repo / 'docs' / '产品设计'
            prototype_base.mkdir()
            shutil.move(req / 'prototypes', prototype_base / 'prototypes')
            source = prototype_base / '订单页面.md'
            relative = source.relative_to(repo).as_posix()
            old_root = req.relative_to(repo).as_posix()
            new_root = prototype_base.relative_to(repo).as_posix()
            source.write_bytes(text.replace(old_root, new_root).replace('### PAGE-1：列表', '### 订单列表').encode('utf-8'))
            path.unlink()
            path = source
            index.write_bytes(index.read_bytes().replace(old_root.encode(), new_root.encode()))
            contract = repo / '.agents/skills/product-design/SKILL.md'
            contract.parent.mkdir(parents=True)
            contract.write_bytes(b'# Product design\nProvide the selected page design.\n')
            spec = ARTIFACT_REGISTRY.get_artifact('ui-design')
            mapping = {'schema_version': 2, 'package_ref': old_root, 'artifacts': [
                {'artifact_id': spec.id, 'sources': [relative], 'sections': [
                    {'section': name, 'path': relative, 'heading': name} for name in spec.sections],
                 'points': [{'id': 'PAGE-1', 'path': relative, 'heading': '页面清单 / 订单列表',
                             'covers': ['A1'], 'uses': ['F1']}], 'globals': []}],
                'capability_bindings': [{'artifact_id': spec.id, 'orchestrator': 'aek', 'writer': 'product-author',
                    'provider': 'product-design', 'client': 'codex', 'entry': '$product-design',
                    'contract_path': contract.relative_to(repo).as_posix(),
                    'contract_sha256': sha(contract.read_bytes())}]}
            code, output = invoke('plan-rollback', '--json', trusted=False)
            self.assertEqual(code, 0, output)
            plan = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'doc-gate'), 'plan-prepare',
                str(req), '--base', 'HEAD', '--route', 'standard', '--require', 'ui-design', '--source-map', '--json'],
                cwd=repo, input=json.dumps(mapping, ensure_ascii=False), capture_output=True, encoding='utf-8', timeout=30)
            self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
            code, output = invoke('plan-freeze', '--base', 'HEAD', '--json', trusted=False)
            self.assertEqual(code, 0, output)
            # Reuse actual native ancestor approvals through their regular gate
            # paths below. No manually bound composed-stage approval.
            reviews = req / 'reviews'
            req_file = req / '需求分析.md'
            req_text = req_file.read_text(encoding='utf-8')
            for name in ARTIFACT_REGISTRY.get_artifact('requirements-analysis').sections:
                if f'## {name}\n' not in req_text:
                    req_text += f'\n## {name}\n保持当前功能。\n'
            req_text = req_text.replace('## 冻结记录\n', '## 冻结记录\n冻结状态：已冻结\n确认人：夹具维护者\n')
            req_file.write_bytes(req_text.encode('utf-8'))
            outline = req / '概要设计.md'
            overview = outline.read_text(encoding='utf-8').replace(
                sha((self.f.req / '需求分析.md').read_bytes()), sha(req_file.read_bytes()))
            for name in ARTIFACT_REGISTRY.get_artifact('outline-design').sections:
                if f'## {name}\n' not in overview:
                    overview += f'\n## {name}\n保持当前行为。\n'
            outline.write_bytes(overview.encode('utf-8'))
            for action, flags in (('freeze', ('--stage', '需求分析', '--by', 'fixture-maintainer')),
                                  ('gate', ('--stage', '概要设计', '--no-review'))):
                code, output = invoke(action, *flags, '--schema-version', '2', '--json', trusted=False)
                self.assertEqual(code, 0, output)
            old_plan = self.read_review('artifact-plan.json')['digest']
            old_gate = self.read_review('artifact-plan.gate.json')['credential_digest']
            new_plan = json.loads((reviews / 'artifact-plan.json').read_bytes())['digest']
            new_gate = json.loads((reviews / 'artifact-plan.gate.json').read_bytes())['credential_digest']
            for changed in (path, index):
                changed.write_bytes(changed.read_bytes().replace(old_plan.encode(), new_plan.encode())
                                    .replace(old_gate.encode(), new_gate.encode()))
        interaction_path = prototype_base / self.spec_path
        sequence = interaction_bytes({'action': 'capture', 'state': 'default'})
        interaction_path.write_bytes(sequence)
        original_sha = sha(self.interactions)
        path.write_bytes(path.read_bytes().replace(original_sha.encode(), sha(sequence).encode()))
        index.write_bytes(index.read_bytes().replace(original_sha.encode(), sha(sequence).encode()))
        def prepare(selected):
            return commands['_prepare_prototype_selection'](req, [selected], 3)
        profile = LinuxInteractiveHostProfile(host_fixture.authority, host_fixture.port, runtime=host_fixture.runtime,
            target=target, conditions=RenderConditions(64, 64), prepare_selection=prepare,
            read_html=lambda relative: (prototype_base / relative).read_bytes(), sequence='review',
            read_interaction_spec=lambda _selected: interaction_path.read_bytes(),
            required_components=host_tests['REQUIRED'], adoption_proposals=(host_fixture.proposal,),
            adoption_source_refs=('advisory', 'compatibility'), clock=host_fixture.authority._clock)
        profiles = {target: profile}
        host = LocalToolHost(host_source=host_fixture.host, authority=host_fixture.authority,
                             source_port=host_fixture.port, providers={PROFILE_ID: profile})
        request = {'profile_id': PROFILE_ID, 'targets': ['/'.join(target)]}
        run = host.prepare(request)
        png = fixtures.png()
        terminal = host_tests['host_fixture'].Terminal
        with patch.object(sys, 'stdin', terminal()), redirect_stdout(terminal()), \
                patch('builtins.input', return_value='RUN ' + run['binding']['plan_digest'][:12]), \
                patch('aek.adapters.ui_interactive_host.render_interaction',
                      return_value=(InteractionCapture('default', png, sha(png)),)):
            result = host.execute(request, reviewer='test-runtime')
        self.assertEqual(result['status'], 'COMPLETED', result)
        args = ('--schema-version', '3', '--target', '/'.join(target), '--reviewer', 'test-maintainer',
                '--prepared-sha256', prepare(target)['prepare_sha256'],
                '--interaction-ref', '/'.join(target) + '=' + result['source_ref'])
        self.assertEqual(invoke('prototype-confirm', *args, trusted=False)[0], 3)
        self.assertEqual(invoke('prototype-confirm', *args)[0], 3)
        def choose(prompt):
            import re
            return re.search(r'ACCEPT [0-9a-f]{12}', prompt).group()
        with patch.object(TerminalHostEventSource, '_prototype_terminal', return_value=True), \
                patch('builtins.input', side_effect=choose):
            accepted, output = invoke('prototype-confirm', *args)
        self.assertEqual(accepted, 0, output)
        report = json.loads(output.splitlines()[-1])
        receipt = req / report['per_item'][0]['receipt_ref']
        self.assertEqual(report['per_item'][0]['status'], 'ACCEPTED')
        check, output = invoke('check', '--stage', 'UI设计', '--json')
        self.assertEqual(check, 0, output)
        code, output = invoke('review-input', '--stage', 'UI设计', '--schema-version', '2', '--json')
        self.assertEqual(code, 0, output)
        delivered = json.loads(output)
        if mapped:
            self.assertEqual(dict(delivered['request']['doc_hashes']), {relative: sha(path.read_bytes())})
            self.assertFalse((req / 'UI设计.md').exists())
        (req / delivered['receipt_path']).write_text(json.dumps(delivered['receipt']), encoding='utf-8')
        (req / delivered['issues_path']).write_text(json.dumps({'reviewer': 'test-sensor', 'stage': 'UI设计',
            'doc_hashes': dict(delivered['request']['doc_hashes']), 'issues': []}, ensure_ascii=False), encoding='utf-8')
        code, output = invoke('gate', '--stage', 'UI设计', '--schema-version', '2', '--json')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(invoke('status', '--json')[1])['UI设计']['state'], 'FROZEN')
        initial_receipt = receipt.read_bytes()
        interaction_path.write_bytes(sequence + b'\n')
        self.assertEqual(json.loads(invoke('status', '--json')[1])['UI设计']['state'], 'DRAFT')
        self.assertEqual(receipt.read_bytes(), initial_receipt)


if __name__ == '__main__':
    unittest.main()
