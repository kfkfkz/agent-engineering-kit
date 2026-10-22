"""Optional controlled renderer. Input JSON never selects or authorizes a provider."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re

from aek.adapters.ui_cdp import CdpError, CdpPipe
from aek.core.artifact.ui_preview import (
    MAX_PNG_BYTES, PreviewRejected, RenderConditions, RenderResult, validate_html, validate_rendered_png,
)


class ProviderUnavailable(ValueError):
    """Only a control-side provider may report missing adoption/isolation."""


@dataclass(frozen=True)
class RenderSession:
    channel: CdpPipe
    runtime_sha256: str
    plan_sha256: str
    browser_product: str


class CdpRenderer:
    def __init__(self, provider=None):
        # Only the trusted composition root may supply an adopted provider.
        # No default PATH browser, environment switch, or automatic installation.
        self._provider = provider

    def render(self, snapshot: bytes, conditions: RenderConditions, approved_plan, *, expected_sha256: str):
        started = False
        completed = False
        try:
            if type(conditions) is not RenderConditions:
                raise PreviewRejected('invalid render conditions')
            checked = conditions.checked()
            html = validate_html(snapshot, expected_sha256)
        except (PreviewRejected, TypeError, ValueError, OverflowError):
            return RenderResult('REJECTED', 'INVALID_STATIC_PREVIEW')
        if (self._provider is None or approved_plan is None
                or not callable(getattr(self._provider, 'open', None))
                or not callable(getattr(self._provider, 'validate_current', None))):
            return RenderResult('UNAVAILABLE', 'APPROVED_PROVIDER_UNAVAILABLE',
                                input_sha256=expected_sha256, conditions=checked,
                                unknowns=('No approved isolated provider is connected',))
        try:
            # The provider validates the original host grant and all runtime,
            # namespace and resource facts BEFORE opening pipes or starting a
            # process. This adapter never interprets JSON as execution authority.
            with self._provider.open(snapshot, dict(checked), approved_plan) as session:
                started = True
                if (type(session) is not RenderSession or type(session.channel) is not CdpPipe
                        or any(type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value)
                               for value in (session.runtime_sha256, session.plan_sha256))
                        or type(session.browser_product) is not str):
                    raise ValueError('invalid provider session')
                channel = session.channel
                if channel.rpc('Browser.getVersion').get('product') != session.browser_product:
                    raise ValueError('browser runtime changed')
                pages = [target for target in channel.rpc('Target.getTargets')['targetInfos']
                         if target.get('type') == 'page']
                if len(pages) != 1 or pages[0].get('url') != 'about:blank':
                    raise ValueError('expected one isolated blank target')
                target_id = pages[0]['targetId']
                attached = channel.rpc('Target.attachToTarget', {'targetId': target_id, 'flatten': True})['sessionId']
                def page(method, params=None):
                    return channel.rpc(method, params, attached)
                page('Page.enable')
                page('Emulation.setScriptExecutionDisabled', {'value': True})
                page('Network.enable')
                # data: raster images passed static preflight and need no
                # network. Blocking '*' also rejects those approved bytes.
                # External/file requests stay blocked; OS net isolation and
                # strict HTML/resource preflight remain independent controls.
                page('Network.setBlockedURLs', {'urls': [
                    'http:*', 'https:*', 'ws:*', 'wss:*', 'ftp:*', 'file:*']})
                page('Emulation.setDeviceMetricsOverride', {'width': checked['width'], 'height': checked['height'],
                    'deviceScaleFactor': checked['dpr'], 'mobile': False})
                page('Emulation.setEmulatedMedia', {'media': 'screen', 'features': [
                    {'name': 'prefers-color-scheme', 'value': checked['theme']}]})
                frame_id = page('Page.getFrameTree')['frameTree']['frame']['id']
                page('Page.setDocumentContent', {'frameId': frame_id, 'html': html})
                page('DOM.getDocument')
                page('Page.getLayoutMetrics')
                encoded = page('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})['data']
                if type(encoded) is not str or len(encoded) > 4 * ((MAX_PNG_BYTES + 2) // 3):
                    raise CdpError('CDP_LIMIT_EXCEEDED')
                png = base64.b64decode(encoded, validate=True)
                validate_rendered_png(png, conditions)
                if self._provider.validate_current(session, expected_sha256, dict(checked), approved_plan) is not True:
                    raise ValueError('provider no longer current')
                completed = True
            # Provider cleanup must succeed before any screenshot is published.
            # __exit__ may suppress an exception; a normal with-exit alone is
            # not evidence that the body reached all validation steps.
            if not completed:
                raise ValueError('render validation did not complete')
            if self._provider.validate_current(session, expected_sha256, dict(checked), approved_plan) is not True:
                raise ValueError('provider changed during cleanup')
            return RenderResult('RENDERED', 'STATIC_CAPTURE_COMPLETE', expected_sha256, png,
                hashlib.sha256(png).hexdigest(), checked, False,
                ('Reference-image applicability and visual comparison have not been verified',
                 'Fonts, animation and dynamic-data comparability require separate evidence'),
                session.runtime_sha256, session.plan_sha256)
        except ProviderUnavailable:
            return RenderResult('FAILED' if started else 'UNAVAILABLE',
                                'RENDER_PROVIDER_REVOKED' if started else 'APPROVED_PROVIDER_UNAVAILABLE',
                                input_sha256=expected_sha256, conditions=checked)
        except Exception as exc:
            # Do not echo browser messages, HTML, paths, or provider secrets.
            return RenderResult('FAILED', exc.reason_code if isinstance(exc, CdpError) else 'RENDER_EXECUTION_FAILED',
                                input_sha256=expected_sha256, conditions=checked)
