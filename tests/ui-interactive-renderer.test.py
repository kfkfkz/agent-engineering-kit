"""Fixed interactive plans and pre-launch authority separation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aek.adapters.ui_cdp import CdpError
from aek.adapters.ui_interactive_renderer import (
    LinuxInteractiveRenderProvider, _TargetGuard, interaction_plan, render_interaction,
)
from aek.adapters.ui_linux_renderer import LinuxRuntime, render_plan
from aek.adapters.ui_renderer import ProviderUnavailable
from aek.core.artifact.ui_preview import RenderConditions


HTML = b'<!doctype html><main>Preview</main>'
SPEC = (b'{"schema_version":1,"sequences":[{"name":"review","steps":'
        b'[{"action":"capture","state":"default"}]}]}')


class InteractiveRendererTests(unittest.TestCase):
    def setUp(self):
        self.runtime = LinuxRuntime('/runtime', '/tools/bwrap', '/tools/prlimit',
            (('chrome-headless-shell', 'a' * 64),), 'b' * 64, 'c' * 64, ())
        self.conditions = RenderConditions(640, 480)
        fixture = runpy.run_path(str(ROOT / 'tests/security-workflow.test.py'))
        self.host = fixture['LiveHost']()
        self.authority = fixture['make_authority'](self.host)
        self.scope = {'repository_ref': self.authority.repository_ref, 'scope_ref': self.authority.scope_ref,
                      'identity_digest': self.authority.current_identity.envelope_digest}
        self.plan = interaction_plan(self.runtime, self.conditions, **self.scope,
            target=('PAGE-1', 'AREA-1', 'default', 'CTX-1'), prepare_sha256='d' * 64, dependency_sha256='e' * 64,
            input_sha256=hashlib.sha256(HTML).hexdigest(), spec_sha256=hashlib.sha256(SPEC).hexdigest(),
            sequence='review')

    def provider(self, plan):
        binding = plan.binding()
        event = self.host.issue(self.authority, 'user-confirmation', 'execute', binding)
        grant = self.authority.on_user_confirmation(event, binding=binding)
        return LinuxInteractiveRenderProvider(self.runtime, authority=self.authority, grant=grant,
                                               adoption_validator=lambda _runtime: True)

    def test_plan_binds_preparation_target_spec_sequence_policy_and_fixed_limits(self):
        binding = self.plan.binding()
        self.assertEqual((binding['tool_id'], binding['action'], binding['timeout_seconds'],
                          binding['max_output_bytes'], binding['network_mode']),
                         ('aek-interactive-render-linux-v1', 'render-interactive', 35, 2_000_000, 'offline'))
        for field, value in (('prepare_sha256', 'f' * 64), ('dependency_sha256', 'f' * 64),
                             ('spec_sha256', 'f' * 64), ('sequence', 'another'),
                             ('target', ('PAGE-2', 'AREA-1', 'default', 'CTX-1'))):
            with self.subTest(field=field):
                self.assertNotEqual(binding['plan_digest'], replace(self.plan, **{field: value}).binding()['plan_digest'])
        for field, value in (('policy_version', 'unknown'), ('policy_version', 'isolated-preview-v1'),
                             ('timeout_seconds', 36), ('max_output_bytes', 8192)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.plan, **{field: value}).binding()

    def test_old_static_grant_cannot_start_the_interactive_process(self):
        static = render_plan(self.runtime, self.plan.input_sha256, self.conditions, **self.scope)
        provider = self.provider(static)
        with patch('aek.adapters.ui_linux_renderer._NativeProcess') as native:
            with self.assertRaises(ProviderUnavailable):
                render_interaction(provider, HTML, SPEC, self.conditions, self.plan)
            native.assert_not_called()

    def test_changed_spec_or_missing_target_capture_cannot_start_process(self):
        provider = self.provider(self.plan)
        with patch('aek.adapters.ui_linux_renderer._NativeProcess') as native:
            with self.assertRaisesRegex(ValueError, 'specification digest'):
                render_interaction(provider, HTML, SPEC + b'\n', self.conditions, self.plan)
            with self.assertRaisesRegex(ValueError, 'INTERACTION_STATES_INCOMPLETE'):
                render_interaction(provider, HTML, SPEC, self.conditions,
                    replace(self.plan, target=('PAGE-1', 'AREA-1', 'dialog', 'CTX-1')))
            native.assert_not_called()

    def test_browser_csp_issue_aborts_interaction_without_exposing_details(self):
        guard = _TargetGuard('page-1')
        guard({'method': 'Audits.issueAdded', 'params': {'issue': {'code': 'DeprecationIssue'}}})
        with self.assertRaisesRegex(CdpError, '^INTERACTION_POLICY_VIOLATION$'):
            guard({'method': 'Audits.issueAdded', 'params': {'issue': {
                'code': 'ContentSecurityPolicyIssue',
                'details': {'contentSecurityPolicyIssueDetails': {'violatedDirective': 'worker-src'}},
            }}})


if __name__ == '__main__':
    unittest.main()
