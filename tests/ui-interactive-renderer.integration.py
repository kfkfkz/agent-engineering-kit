#!/usr/bin/env python3
"""Run a benign interaction on the existing pinned Linux candidate runtime."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aek.adapters import ui_interactive_host
from aek.adapters.ui_cdp import CdpError
from aek.adapters.ui_interactive_host import LinuxInteractiveHostProfile
from aek.adapters.ui_interactive_renderer import PROFILE_ID
from aek.adapters.ui_linux_renderer import _NativeProcess
from aek.application.local_host import LocalToolHost
from aek.core.artifact.ui_preview import PreviewItem, RenderConditions, build_static_preview
from aek.core.security.authority import digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--through-doc-gate', action='store_true',
                      help='also check external source, terminal confirmation, original UI Gate and invalidation')
    mode.add_argument('--check-blocked-worker', action='store_true',
                      help='verify a blocked local worker cannot become a completed observation')
    args = parser.parse_args()
    static = runpy.run_path(str(ROOT / 'tests/ui-linux-renderer.integration.py'))
    runtime = static['pinned_runtime'](args.runtime_root.absolute())
    if args.through_doc_gate:
        cases = runpy.run_path(str(ROOT / 'tests/ui-interactive-prototype.test.py'))
        case = cases['InteractivePreparationTests']()
        try:
            case.setUp()
            case.exercise_original_ui_gate(mapped=True, runtime=runtime)
        finally:
            case.doCleanups()
        print(json.dumps({'actual_browser': True, 'external_ui_gate': 'FROZEN',
                          'after_spec_change': 'DRAFT', 'human_approval': False}))
        return 0
    tests = runpy.run_path(str(ROOT / 'tests/ui-render-host.test.py'))
    html = b'''<!doctype html><form id="form" onsubmit="return false">
<label>Name <input id="name" required></label>
<label>View <select id="view"><option value="basic">Basic</option>
<option value="advanced">Advanced</option></select></label>
<button id="open" type="button" onclick="show()">Open</button></form>
<dialog id="dialog"></dialog><script>
function show() {
    const form = document.getElementById("form");
    if (!form.checkValidity() || form.elements.view.value !== "advanced") return;
    const dialog = document.getElementById("dialog");
    dialog.textContent = "Advanced view for " + form.elements.name.value;
    dialog.showModal();
}</script>'''
    if args.check_blocked_worker:
        html = html.replace(b'    dialog.showModal();', b'''    dialog.showModal();
    const url = URL.createObjectURL(new Blob(['postMessage("ready")'], {type: 'text/javascript'}));
    try {
        const worker = new Worker(url);
        worker.onmessage = worker.onerror = () => { worker.terminate(); URL.revokeObjectURL(url); };
    } catch (_) { URL.revokeObjectURL(url); }''')
    spec = json.dumps({'schema_version': 1, 'sequences': [{'name': 'open-dialog', 'steps': [
        {'action': 'capture', 'state': 'default'},
        {'action': 'click', 'selector': '#open'},
        {'action': 'capture', 'state': 'required-field'},
        {'action': 'fill', 'selector': '#name', 'value': 'Demo'},
        {'action': 'key', 'key': 'Tab'},
        {'action': 'select', 'selector': '#view', 'value': 'advanced'},
        {'action': 'click', 'selector': '#open'},
        {'action': 'capture', 'state': 'dialog'},
    ]}]}).encode('utf-8')
    with tempfile.TemporaryDirectory() as directory:
        fixture = tests['Fixture'](Path(directory))
        target = ('PAGE-1', 'AREA-1', 'dialog', 'CTX-1')
        selection = tests['preview'].selection(target)
        selection['schema_version'] = 3
        item = selection['per_item'][0]
        item.update(prototype_path='prototype.html', prototype_sha256=hashlib.sha256(html).hexdigest(),
            scope_digest=fixture.authority.scope_ref, mode='interactive-v1', interaction_status='PENDING',
            spec_sha256=hashlib.sha256(spec).hexdigest())
        selection['prepare_sha256'] = digest({key: selection[key] for key in ('schema_version', 'status', 'per_item')})
        fixture.path.write_bytes(html)
        spec_path = fixture.path.with_name('interaction.json')
        spec_path.write_bytes(spec)
        def build_host():
            profile = LinuxInteractiveHostProfile(fixture.authority, fixture.port, runtime=runtime,
                target=target, conditions=RenderConditions(640, 480), prepare_selection=lambda _target: selection,
                read_html=lambda relative: (fixture.path.parent / relative).read_bytes(),
                read_interaction_spec=lambda _target: spec_path.read_bytes(), required_components=tests['REQUIRED'],
                adoption_proposals=(fixture.proposal,), adoption_source_refs=('advisory', 'compatibility'),
                clock=fixture.authority._clock, sequence='open-dialog')
            return profile, LocalToolHost(host_source=fixture.host, authority=fixture.authority,
                source_port=fixture.port, providers={PROFILE_ID: profile})
        profile, host = build_host()
        request = {'profile_id': PROFILE_ID, 'targets': ['/'.join(target)]}
        prepared = host.prepare(request)
        terminal = tests['host_fixture'].Terminal
        render = ui_interactive_host.render_interaction
        close_process = _NativeProcess.__exit__
        render_errors = []
        cleanup_results = []

        def observe_render(*args, **kwargs):
            try:
                return render(*args, **kwargs)
            except CdpError as exc:
                render_errors.append(exc.reason_code)
                raise

        def observe_cleanup(process, *args):
            try:
                return close_process(process, *args)
            finally:
                cleanup_results.append(process.reaped and not process.remaining())

        with patch.object(sys, 'stdin', terminal()), redirect_stdout(terminal()), patch('builtins.input',
                return_value='RUN ' + prepared['binding']['plan_digest'][:12]), \
                patch.object(ui_interactive_host, 'render_interaction', observe_render), \
                patch.object(_NativeProcess, '__exit__', observe_cleanup):
            result = host.execute(request, reviewer='protocol-test')
        if args.check_blocked_worker:
            if (result['status'] != 'INCOMPLETE' or result.get('source_ref')
                    or render_errors != ['INTERACTION_POLICY_VIOLATION'] or cleanup_results != [True]):
                raise ValueError('blocked worker must reject observation and clean up: '
                                 + repr((result, render_errors, cleanup_results)))
            print(json.dumps({'actual_browser': True, 'status': 'INCOMPLETE',
                              'policy_violation': True, 'source_registered': False, 'cleanup': 'COMPLETE'}))
            return 0
        if result['status'] != 'COMPLETED':
            raise ValueError('original host did not register the interaction completion: ' + repr(result))
        observed = profile.read_result(expected_item=item, source_ref=result['source_ref'])
        if observed is None:
            raise ValueError('original source evidence could not verify the interaction')
        facts = observed.facts
        captures = facts['captures']
        if ([item['state'] for item in captures] != ['default', 'required-field', 'dialog']
                or captures[0]['sha256'] == captures[-1]['sha256'] or facts['cleanup'] != 'COMPLETE'):
            raise ValueError('the browser did not show the expected dialog change')
        rendered = profile.read_preview_result(expected_item=item)
        preview = build_static_preview(selection, [PreviewItem(target, render=rendered)])
        if rendered is None or b'OBSERVED' not in preview.html or b'<script' in preview.html:
            raise ValueError('the current observation could not be displayed without scripts')
        fixture.restart()
        restarted, host = build_host()
        with patch('builtins.input', side_effect=AssertionError('current completion must not rerun')), \
                patch('aek.adapters.ui_linux_renderer._NativeProcess',
                    side_effect=AssertionError('restored completion must not spawn a browser')):
            reused = host.execute(request, reviewer='protocol-test')
            restored = restarted.read_result(expected_item=item, source_ref=result['source_ref'])
        if reused.get('action') != 'reuse' or restored != observed:
            raise ValueError('the original host did not reuse the registered source')
        print(json.dumps({'status': 'OBSERVED', 'states': {item['state']: item['sha256'] for item in captures},
            'actual_browser': True, 'source_registered': True, 'cleanup': facts['cleanup'], 'restart_reuse': True,
            'production_host_adoption': False, 'human_approval': False,
            'runtime_sha256': runtime.sha256, 'plan_sha256': facts['plan_sha256']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
