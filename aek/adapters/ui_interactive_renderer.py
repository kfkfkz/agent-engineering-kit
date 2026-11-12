"""One selected interactive sequence in the existing Linux render sandbox."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from html import escape
import re

from aek.adapters.ui_cdp import CdpError, InteractiveCdpPipe
from aek.adapters.ui_linux_renderer import LinuxRenderPlan, LinuxRenderProvider, render_plan
from aek.adapters.ui_renderer import ProviderUnavailable
from aek.core.artifact.ui_interaction import load_interaction_spec, validate_interactive_html
from aek.core.artifact.ui_preview import RenderConditions, validate_rendered_png
from aek.core.security.authority import runtime_binding


PROFILE_ID = 'aek-interactive-render-linux-v1'
POLICY_VERSION = 'isolated-preview-v2'
_SHA = re.compile(r'[0-9a-f]{64}')
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
_STATES = {'default', 'loading', 'empty', 'error', 'dialog', 'drawer', 'permission'}
_RESOURCES = ['http:*', 'https:*', 'ws:*', 'wss:*', 'ftp:*', 'file:*']
_PERMISSIONS = ('geolocation', 'notifications', 'clipboard-read', 'clipboard-write',
                'camera', 'microphone', 'midi', 'persistent-storage', 'display-capture',
                'storage-access', 'window-management')
_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; "
        "object-src 'none'; form-action 'none'; base-uri 'none'")
# A srcdoc document inherits the parent policy. The trusted outer shell has
# no scripts of its own and permits the same inline capabilities as the child.
_PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "img-src data:; frame-src about:; base-uri 'none'; form-action 'none'")
_AUTO_ATTACH = {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}


@dataclass(frozen=True, kw_only=True)
class LinuxInteractionPlan(LinuxRenderPlan):
    target: tuple[str, str, str, str]
    prepare_sha256: str
    dependency_sha256: str
    spec_sha256: str
    sequence: str
    policy_version: str = POLICY_VERSION

    def binding(self):
        if (self.policy_version != POLICY_VERSION or self.timeout_seconds != 35
                or self.max_output_bytes != 2_000_000 or self.max_items != 2000
                or type(self.target) is not tuple or len(self.target) != 4
                or any(type(part) is not str for part in self.target)
                or not re.fullmatch(r'PAGE-[1-9][0-9]*', self.target[0])
                or not re.fullmatch(r'AREA-[1-9][0-9]*', self.target[1])
                or self.target[2] not in _STATES or not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', self.target[3])
                or type(self.sequence) is not str or not _NAME.fullmatch(self.sequence)
                or any(type(value) is not str or not _SHA.fullmatch(value) for value in (
                    self.prepare_sha256, self.dependency_sha256, self.input_sha256, self.spec_sha256))):
            raise ValueError('INVALID_INTERACTION_PLAN')
        return runtime_binding(plan_digest=self.plan_digest, repository_ref=self.repository_ref,
            scope_ref=self.scope_ref, identity_digest=self.identity_digest,
            tool_id=PROFILE_ID, executable_digest=self.runtime_sha256, action='render-interactive',
            network_mode='offline', write_scope=(), timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes, max_items=self.max_items)


def interaction_plan(runtime, conditions, *, repository_ref, scope_ref, identity_digest,
                     target, prepare_sha256, dependency_sha256, input_sha256, spec_sha256, sequence):
    static = render_plan(runtime, input_sha256, conditions, repository_ref=repository_ref,
                         scope_ref=scope_ref, identity_digest=identity_digest)
    plan = LinuxInteractionPlan(static.repository_ref, static.scope_ref, static.identity_digest,
        static.runtime_sha256, static.input_sha256, static.conditions_json,
        target=target, prepare_sha256=prepare_sha256, dependency_sha256=dependency_sha256,
        spec_sha256=spec_sha256, sequence=sequence)
    plan.binding()
    return plan


class LinuxInteractiveRenderProvider(LinuxRenderProvider):
    _plan_type = LinuxInteractionPlan
    _channel_type = InteractiveCdpPipe

    def open(self, snapshot, conditions, plan):
        raise ProviderUnavailable('use the exact authorized interaction specification')

    @contextmanager
    def open_interaction(self, snapshot, spec, conditions, plan):
        sequence = _checked_sequence(snapshot, spec, plan)
        with super().open(snapshot, conditions, plan) as session:
            yield session, sequence


@dataclass(frozen=True)
class InteractionCapture:
    state: str
    png: bytes
    sha256: str


def _checked_sequence(html, spec, plan):
    if type(plan) is not LinuxInteractionPlan:
        raise ValueError('INVALID_INTERACTION_PLAN')
    plan.binding()
    validate_interactive_html(html, plan.input_sha256)
    sequences = load_interaction_spec(spec, plan.spec_sha256)
    matches = [sequence for sequence in sequences if sequence.name == plan.sequence]
    if len(matches) != 1:
        raise ValueError('INVALID_INTERACTION_SEQUENCE')
    captures = [step.state for step in matches[0].steps if step.action == 'capture']
    if plan.target[2] not in captures or len(set(captures)) != len(captures):
        raise ValueError('INTERACTION_STATES_INCOMPLETE')
    return matches[0]


class _TargetGuard:
    def __init__(self, target_id):
        self.target_id = target_id
        self.session_id = None

    def __call__(self, message):
        method, params = message['method'], message.get('params', {})
        if method == 'Target.attachedToTarget':
            info = params.get('targetInfo', {})
            session = params.get('sessionId')
            if (self.session_id is not None or message.get('sessionId') is not None
                    or info.get('targetId') != self.target_id or info.get('type') != 'page'
                    or info.get('url') != 'about:blank' or params.get('waitingForDebugger') is not False
                    or type(session) is not str or not session):
                raise CdpError('INTERACTION_TARGET_REJECTED')
            self.session_id = session
        elif method in {'Target.detachedFromTarget', 'Inspector.targetCrashed', 'Page.windowOpen',
                        'Page.fileChooserOpened', 'Browser.downloadWillBegin', 'Network.webSocketCreated'}:
            raise CdpError('INTERACTION_POLICY_VIOLATION')
        elif method == 'Audits.issueAdded' and params.get('issue', {}).get('code') == 'ContentSecurityPolicyIssue':
            raise CdpError('INTERACTION_POLICY_VIOLATION')
        elif method == 'Network.requestWillBeSent':
            url = params.get('request', {}).get('url', '')
            if url not in {'about:blank', 'about:srcdoc'} and not url.startswith('data:image/'):
                raise CdpError('INTERACTION_POLICY_VIOLATION')
        elif method in {'Page.frameRequestedNavigation', 'Page.frameStartedNavigating', 'Page.navigatedWithinDocument'}:
            if params.get('url') not in {'about:blank', 'about:srcdoc'}:
                raise CdpError('INTERACTION_POLICY_VIOLATION')
        elif method == 'Page.frameNavigated' and params.get('frame', {}).get('url') not in {'about:blank', 'about:srcdoc'}:
            raise CdpError('INTERACTION_POLICY_VIOLATION')


_CLICK = """function () {
    if (!this.isConnected || !(this instanceof HTMLElement) || this.matches(':disabled')) return false;
    HTMLElement.prototype.focus.call(this);
    HTMLElement.prototype.click.call(this);
    return true;
}"""
_FILL = """function (value) {
    if (!this.isConnected || this.disabled || this.readOnly) return false;
    let proto;
    if (this instanceof HTMLTextAreaElement) proto = HTMLTextAreaElement.prototype;
    else if (this instanceof HTMLInputElement &&
             ['text', 'email', 'search', 'tel', 'url', 'number', 'password'].includes(this.type))
        proto = HTMLInputElement.prototype;
    else return false;
    HTMLElement.prototype.focus.call(this);
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, value);
    if (this.value !== value) return false;
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
}"""
_SELECT = """function (value) {
    if (!this.isConnected || !(this instanceof HTMLSelectElement) || this.disabled ||
        Array.from(this.options).filter(option => option.value === value && !option.matches(':disabled')).length !== 1)
        return false;
    HTMLElement.prototype.focus.call(this);
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(this, value);
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
}"""
_FOCUSED = "function () { return this.isConnected && this.ownerDocument.hasFocus(); }"
_DOM_ACTIONS = {'click': _CLICK, 'fill': _FILL, 'select': _SELECT}
_KEY_CODES = {'Tab': 9, 'Enter': 13, 'Escape': 27}


class _InteractivePage:
    def __init__(self, channel, html, checked):
        self.channel = channel
        targets = channel.rpc('Target.getTargets')['targetInfos']
        if (len(targets) != 1 or targets[0].get('type') != 'page' or targets[0].get('url') != 'about:blank'):
            raise CdpError('INTERACTION_TARGET_REJECTED')
        guard = _TargetGuard(targets[0]['targetId'])
        channel.set_event_guard(guard)
        channel.rpc('Target.setAutoAttach', _AUTO_ATTACH)
        channel.rpc('Target.getTargets')
        self.session = guard.session_id
        if self.session is None:
            raise CdpError('INTERACTION_TARGET_REJECTED')
        # Every subsequently attached target stays paused and aborts the run.
        self.rpc('Target.setAutoAttach', _AUTO_ATTACH)
        channel.rpc('Browser.setDownloadBehavior', {'behavior': 'deny', 'eventsEnabled': True})
        for permission in _PERMISSIONS:
            channel.rpc('Browser.setPermission', {'permission': {'name': permission}, 'setting': 'denied'})
        self.rpc('Page.enable')
        self.rpc('Page.setInterceptFileChooserDialog', {'enabled': True})
        self.rpc('Audits.enable')
        self.rpc('Network.enable')
        self.rpc('Network.setBlockedURLs', {'urls': _RESOURCES})
        self.rpc('Emulation.setDeviceMetricsOverride', {'width': checked['width'], 'height': checked['height'],
            'deviceScaleFactor': checked['dpr'], 'mobile': False})
        self.rpc('Emulation.setEmulatedMedia', {'media': 'screen', 'features': [
            {'name': 'prefers-color-scheme', 'value': checked['theme']}]})
        root = self.rpc('Page.getFrameTree')['frameTree']
        if root.get('childFrames') or root['frame']['url'] != 'about:blank':
            raise CdpError('INTERACTION_TARGET_REJECTED')
        self.outer = root['frame']['id']
        child = ('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
            + escape(_CSP, quote=True) + '">' + html)
        shell = ('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
            + escape(_PAGE_CSP, quote=True) + '"><style>body{margin:0}iframe{position:fixed;inset:0;'
            'width:100vw;height:100vh;border:0}</style><iframe sandbox="allow-scripts" srcdoc="'
            + escape(child, quote=True) + '"></iframe>')
        self.rpc('Page.setDocumentContent', {'frameId': self.outer, 'html': shell})
        self.frame = self.current_frame()
        self.world = self.rpc('Page.createIsolatedWorld',
            {'frameId': self.frame, 'worldName': 'aek-interaction'})['executionContextId']

    def rpc(self, method, params=None):
        return self.channel.rpc(method, params, self.session)

    def current_frame(self):
        tree = self.rpc('Page.getFrameTree')['frameTree']
        children = tree.get('childFrames', [])
        if (tree['frame']['id'] != self.outer or tree['frame']['url'] != 'about:blank' or len(children) != 1
                or children[0].get('childFrames') or children[0]['frame']['url'] != 'about:srcdoc'
                or children[0]['frame'].get('securityOrigin') not in {'://', 'null'}):
            raise CdpError('INTERACTION_FRAME_REJECTED')
        return children[0]['frame']['id']

    def document(self):
        root = self.rpc('DOM.getDocument', {'depth': 5, 'pierce': True})['root']
        matches = []
        pending = [root]
        while pending:
            node = pending.pop()
            if node.get('frameId') == self.frame and node.get('nodeName') == 'IFRAME':
                matches.append(node.get('contentDocument', {}).get('nodeId'))
            pending.extend(node.get('children', ()))
        if len(matches) != 1 or type(matches[0]) is not int or not matches[0]:
            raise CdpError('INTERACTION_FRAME_REJECTED')
        return matches[0]

    def invoke(self, selector, function, value=None):
        nodes = self.rpc('DOM.querySelectorAll', {'nodeId': self.document(), 'selector': selector})['nodeIds']
        if len(nodes) != 1:
            raise CdpError('INTERACTION_SELECTOR_NOT_UNIQUE')
        result = self.rpc('DOM.resolveNode', {'nodeId': nodes[0], 'executionContextId': self.world})
        response = self.rpc('Runtime.callFunctionOn', {'objectId': result['object']['objectId'],
            'functionDeclaration': function, 'arguments': [] if value is None else [{'value': value}],
            'returnByValue': True})
        if 'exceptionDetails' in response or response.get('result', {}).get('value') is not True:
            raise CdpError('INTERACTION_ACTION_REJECTED')

    def step(self, step, conditions):
        if self.current_frame() != self.frame:
            raise CdpError('INTERACTION_FRAME_REJECTED')
        if step.action in _DOM_ACTIONS:
            self.invoke(step.selector, _DOM_ACTIONS[step.action], step.value)
        elif step.action == 'key':
            self.invoke('html', _FOCUSED)
            code = _KEY_CODES[step.key]
            for event_type in ('rawKeyDown', 'keyUp'):
                self.rpc('Input.dispatchKeyEvent', {'type': event_type, 'key': step.key,
                    'code': step.key, 'windowsVirtualKeyCode': code})
            self.invoke('html', _FOCUSED)
        else:
            encoded = self.rpc('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})['data']
            png = base64.b64decode(encoded, validate=True)
            validate_rendered_png(png, conditions)
            return InteractionCapture(step.state, png, hashlib.sha256(png).hexdigest())
        return None


def render_interaction(provider, html, spec, conditions, plan) -> tuple[InteractionCapture, ...]:
    """Run one authorized sequence; never publish a partial or unclean run."""
    if type(provider) is not LinuxInteractiveRenderProvider or type(conditions) is not RenderConditions:
        raise ProviderUnavailable('approved interactive provider unavailable')
    checked = conditions.checked()
    captures = []
    with provider.open_interaction(html, spec, checked, plan) as (session, sequence):
        page = _InteractivePage(session.channel, html.decode('utf-8', errors='strict'), checked)
        for step in sequence.steps:
            captured = page.step(step, conditions)
            if captured is not None:
                captures.append(captured)
        page.rpc('Emulation.setScriptExecutionDisabled', {'value': True})
        if page.current_frame() != page.frame:
            raise CdpError('INTERACTION_FRAME_REJECTED')
        if provider.validate_current(session, plan.input_sha256, checked, plan) is not True:
            raise ProviderUnavailable('interactive provider revoked')
    if provider.validate_current(session, plan.input_sha256, checked, plan) is not True:
        raise ProviderUnavailable('interactive provider revoked during cleanup')
    return tuple(captures)
