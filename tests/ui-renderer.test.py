from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import struct
import unittest
import zlib
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RendererTests(unittest.TestCase):
    def png(self, width=1200, height=800):
        def chunk(kind, body):
            return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(b'\0' * ((width * 3 + 1) * height))) + chunk(b'IEND', b''))

    def provider(self, *, image=None, current=True, fail_method=None, cleanup_error=False):
        # Controlled protocol peer, not a real browser or an adopted provider.
        from aek.adapters.ui_cdp import CdpPipe
        from aek.adapters.ui_renderer import RenderSession
        owner = self
        class Provider:
            opens = 0
            cleaned = False
            methods = []
            parameters = {}
            seen_html = None
            @contextmanager
            def open(self, snapshot, conditions, plan):
                self.opens += 1
                owner.assertIs(plan, self.plan)
                browser_read, controller_write = os.pipe()
                controller_read, browser_write = os.pipe()
                errors = []
                def browser():
                    pending = b''
                    try:
                        while True:
                            raw = os.read(browser_read, 65536)
                            if not raw:
                                break
                            pending += raw
                            while b'\0' in pending:
                                frame, pending = pending.split(b'\0', 1)
                                request = json.loads(frame)
                                method = request['method']
                                self.methods.append(method)
                                self.parameters[method] = request['params']
                                result = {}
                                if method == 'Browser.getVersion':
                                    result = {'product': 'HeadlessChrome/155.0.8059.79'}
                                elif method == 'Target.getTargets':
                                    result = {'targetInfos': [{'type': 'page', 'url': 'about:blank', 'targetId': 'T1'}]}
                                elif method == 'Target.attachToTarget':
                                    result = {'sessionId': 'SESSION-1'}
                                elif method == 'Page.getFrameTree':
                                    result = {'frameTree': {'frame': {'id': 'FRAME-1'}}}
                                elif method == 'Page.setDocumentContent':
                                    self.seen_html = request['params']['html']
                                elif method == 'Page.captureScreenshot':
                                    import base64
                                    result = {'data': base64.b64encode(owner.png() if image is None else image).decode()}
                                reply = {'id': request['id'], 'result': result}
                                if 'sessionId' in request:
                                    reply['sessionId'] = request['sessionId']
                                if method == fail_method:
                                    reply.pop('result')
                                    reply['error'] = {'message': 'sensitive peer diagnostic'}
                                out = json.dumps(reply).encode() + b'\0'
                                while out:
                                    out = out[os.write(browser_write, out):]
                    except BrokenPipeError:
                        pass
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        os.close(browser_read)
                        os.close(browser_write)
                worker = threading.Thread(target=browser, daemon=True)
                worker.start()
                try:
                    with CdpPipe(controller_read, controller_write, timeout_seconds=3) as channel:
                        yield RenderSession(channel, 'a' * 64, 'b' * 64, 'HeadlessChrome/155.0.8059.79')
                finally:
                    self.cleaned = True
                    worker.join(3)
                    owner.assertFalse(worker.is_alive())
                    owner.assertEqual(errors, [])
                    if cleanup_error:
                        raise RuntimeError('sensitive cleanup diagnostic')
            def validate_current(self, session, snapshot_sha256, conditions, plan):
                owner.assertIs(plan, self.plan)
                return current
        instance = Provider()
        instance.plan = object()  # Test control-side token; never a production grant.
        instance.methods = []
        instance.parameters = {}
        return instance

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_controlled_renderer_blocks_scripts_and_network_before_transferring_actual_snapshot(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        provider = self.provider()
        raw = '<!doctype html><html><body>当前原型</body></html>'.encode()
        result = CdpRenderer(provider).render(raw, RenderConditions(theme='dark'), provider.plan,
                                             expected_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.status, 'RENDERED')
        self.assertEqual(result.input_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.output_sha256, hashlib.sha256(self.png()).hexdigest())
        self.assertEqual(result.png, self.png())
        self.assertFalse(result.comparable, 'render success is not comparison or maintainer acceptance')
        self.assertTrue(result.unknowns)
        self.assertEqual(provider.seen_html.encode(), raw)
        for method in ('Emulation.setScriptExecutionDisabled', 'Network.setBlockedURLs'):
            self.assertLess(provider.methods.index(method), provider.methods.index('Page.setDocumentContent'))
        self.assertNotIn('Runtime.evaluate', provider.methods)
        self.assertEqual(provider.parameters['Emulation.setScriptExecutionDisabled'], {'value': True})
        self.assertEqual(provider.parameters['Network.setBlockedURLs'],
                         {'urls': ['http:*', 'https:*', 'ws:*', 'wss:*', 'ftp:*', 'file:*']})
        self.assertEqual(provider.parameters['Emulation.setEmulatedMedia']['features'],
                         [{'name': 'prefers-color-scheme', 'value': 'dark'}])
        self.assertEqual(provider.opens, 1)
        self.assertTrue(provider.cleaned)

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_render_failure_stale_provider_or_cleanup_failure_cannot_return_a_screenshot(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        raw = b'<html><body>preview</body></html>'
        for options in ({'current': False}, {'cleanup_error': True}, {'fail_method': 'Network.setBlockedURLs'}):
            with self.subTest(options=options):
                provider = self.provider(**options)
                result = CdpRenderer(provider).render(raw, RenderConditions(), provider.plan,
                    expected_sha256=hashlib.sha256(raw).hexdigest())
                self.assertEqual(result.status, 'FAILED')
                self.assertIsNone(result.png)
                self.assertIsNone(result.output_sha256)
                self.assertFalse(result.comparable)
                self.assertTrue(provider.cleaned)
                self.assertEqual(provider.opens, 1, 'no silent retry or alternative provider')
                self.assertNotIn('sensitive', repr(result))
                if 'fail_method' in options:
                    self.assertNotIn('Page.setDocumentContent', provider.methods)

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_invalid_screenshot_bytes_are_not_reported_as_rendered(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        valid = self.png()
        for image in (valid[:30], valid[:-12], valid + b'trailing bytes',
                      self.png(800, 600), valid[:30] + b'x' + valid[31:]):
            with self.subTest(image_sha=hashlib.sha256(image).hexdigest()):
                provider = self.provider(image=image)
                raw = b'<html><body>preview</body></html>'
                result = CdpRenderer(provider).render(raw, RenderConditions(), provider.plan,
                    expected_sha256=hashlib.sha256(raw).hexdigest())
                self.assertEqual(result.status, 'FAILED')
                self.assertIsNone(result.png)
                self.assertTrue(provider.cleaned)

    def test_rejected_input_does_not_invoke_an_injected_provider(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        class Provider:
            def open(self, *_args):
                self.fail('illegal input must not reach a process boundary')
            def validate_current(self, *_args):
                return True
        provider = Provider()
        provider.fail = self.fail
        raw = b'<html><script>secret()</script></html>'
        self.assertEqual(CdpRenderer(provider).render(raw, RenderConditions(), object(),
            expected_sha256=hashlib.sha256(raw).hexdigest()).status, 'REJECTED')

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_capability_revoked_after_start_is_execution_failure_not_prestart_unavailability(self):
        from aek.adapters.ui_renderer import CdpRenderer, ProviderUnavailable
        from aek.core.artifact.ui_preview import RenderConditions
        provider = self.provider()
        def revoked(*_args):
            raise ProviderUnavailable('sensitive revocation diagnostic')
        provider.validate_current = revoked
        raw = b'<html><body>preview</body></html>'
        result = CdpRenderer(provider).render(raw, RenderConditions(), provider.plan,
            expected_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.status, 'FAILED')
        self.assertIsNone(result.png)
        self.assertTrue(provider.cleaned)

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_provider_cleanup_cannot_suppress_validation_failure_into_render_success(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        raw = b'<html><body>preview</body></html>'
        for options in ({'current': False}, {'image': b'invalid png'}):
            with self.subTest(options=options):
                provider = self.provider(**options)
                original = provider.open
                @contextmanager
                def suppress(*args):
                    try:
                        with original(*args) as session:
                            yield session
                    except ValueError:
                        pass
                provider.open = suppress
                result = CdpRenderer(provider).render(raw, RenderConditions(), provider.plan,
                    expected_sha256=hashlib.sha256(raw).hexdigest())
                self.assertEqual(result.status, 'FAILED')
                self.assertIsNone(result.png)
                self.assertIsNone(result.output_sha256)
                self.assertTrue(provider.cleaned)

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_revocation_during_provider_cleanup_cannot_publish_a_stale_render(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        provider = self.provider()
        original = provider.open
        revoked = False
        @contextmanager
        def cleanup_then_revoke(*args):
            nonlocal revoked
            with original(*args) as session:
                yield session
            revoked = True
        provider.open = cleanup_then_revoke
        provider.validate_current = lambda *_args: not revoked
        raw = b'<html><body>preview</body></html>'
        result = CdpRenderer(provider).render(raw, RenderConditions(), provider.plan,
            expected_sha256=hashlib.sha256(raw).hexdigest())
        self.assertTrue(revoked)
        self.assertEqual(result.status, 'FAILED')
        self.assertIsNone(result.png)

    def test_png_validation_rechecks_its_physical_pixel_and_condition_budget(self):
        from aek.core.artifact.ui_preview import RenderConditions, PreviewRejected, validate_rendered_png
        with self.assertRaises(PreviewRejected):
            validate_rendered_png(self.png(1, 1), RenderConditions(width=1, height=1))

    def test_invalid_descriptor_types_do_not_mask_channel_rejection_with_cleanup_errors(self):
        from aek.adapters.ui_cdp import CdpPipe, CdpError
        with self.assertRaises(CdpError):
            CdpPipe([], {})

    @contextmanager
    def response_pipe(self, reply, **kwargs):
        from aek.adapters.ui_cdp import CdpPipe
        browser_read, controller_write = os.pipe()
        controller_read, browser_write = os.pipe()
        def browser():
            try:
                raw = b''
                while b'\0' not in raw:
                    chunk = os.read(browser_read, 4096)
                    if not chunk:
                        return
                    raw += chunk
                pending = reply
                while pending:
                    pending = pending[os.write(browser_write, pending):]
            except BrokenPipeError:
                pass  # Expected when the controller rejects this controlled peer.
            finally:
                os.close(browser_read)
                os.close(browser_write)
        worker = threading.Thread(target=browser, daemon=True)
        worker.start()
        try:
            with CdpPipe(controller_read, controller_write, timeout_seconds=2, **kwargs) as channel:
                yield channel
        finally:
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'controlled peer must terminate after the channel closes')

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_cdp_backpressure_and_missing_response_share_a_bounded_deadline(self):
        from aek.adapters.ui_cdp import CdpPipe, CdpError
        for blocked_direction in ('write', 'read'):
            with self.subTest(direction=blocked_direction):
                browser_read, controller_write = os.pipe()
                controller_read, browser_write = os.pipe()
                start = time.monotonic()
                try:
                    with CdpPipe(controller_read, controller_write, timeout_seconds=0.06) as channel:
                        with self.assertRaisesRegex(CdpError, 'CDP_TIMEOUT'):
                            channel.rpc('Page.setDocumentContent', {'html': 'x' * (
                                1024 * 1024 if blocked_direction == 'write' else 1)})
                        with self.assertRaisesRegex(CdpError, 'CDP_CLOSED'):
                            channel.rpc('Browser.getVersion')
                    self.assertLess(time.monotonic() - start, 2)
                    for fd in (controller_read, controller_write):
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                finally:
                    os.close(browser_read)
                    os.close(browser_write)

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_cdp_rejects_invalid_frames_mismatched_identity_and_remote_error_without_echoing_body(self):
        from aek.adapters.ui_cdp import CdpError
        replies = (b'{"id":1,"id":1,"result":{}}\0', b'{"id":true,"result":{}}\0',
            b'{"id":2,"result":{}}\0', b'{"id":1,"result":{},"sessionId":"foreign"}\0',
            b'{"id":1,"result":[],"secret":"do not echo"}\0', b'[]\0', b'\xff\0',
            b'{"id":1,"result":{"x":NaN}}\0', b'{"id":1,"error":{"message":"secret"}}\0')
        for reply in replies:
            with self.subTest(reply=reply), self.response_pipe(reply) as channel:
                with self.assertRaises(CdpError) as failure:
                    channel.rpc('Browser.getVersion')
                self.assertNotIn('secret', str(failure.exception))

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_cdp_limits_output_and_event_floods_and_rejects_script_commands(self):
        from aek.adapters.ui_cdp import CdpError
        for reply, bounds in ((b'x' * 257, {'max_bytes': 256}),
                (b'{"method":"Page.lifecycleEvent","params":{}}\0' * 3, {'max_events': 2})):
            with self.subTest(bounds=bounds), self.response_pipe(reply, **bounds) as channel:
                with self.assertRaisesRegex(CdpError, 'CDP_LIMIT_EXCEEDED'):
                    channel.rpc('Browser.getVersion')
        with self.response_pipe(b'') as channel:
            with self.assertRaisesRegex(CdpError, 'CDP_COMMAND_REJECTED'):
                channel.rpc('Runtime.evaluate', {'expression': 'secret()'})

    @unittest.skipUnless(os.name == 'posix', 'CDP pipe provider is Linux/POSIX only')
    def test_cdp_pipe_handles_real_partial_reads_writes_and_closes_owned_descriptors(self):
        from aek.adapters.ui_cdp import CdpPipe
        browser_read, controller_write = os.pipe()
        controller_read, browser_write = os.pipe()
        errors = []
        def browser():
            try:
                raw = b''
                while b'\0' not in raw:
                    chunk = os.read(browser_read, 997)
                    if not chunk:
                        raise RuntimeError('controlled peer received an incomplete request')
                    raw += chunk
                request = json.loads(raw.split(b'\0')[0])
                self.assertEqual(request['params']['html'], '页面' * 70000)
                response = (json.dumps({'method': 'Page.lifecycleEvent', 'params': {}}) + '\0' +
                            json.dumps({'id': request['id'], 'result': {'ok': '当前'}}) + '\0').encode('utf-8')
                for offset in range(0, len(response), 7):
                    os.write(browser_write, response[offset:offset + 7])
            except BaseException as exc:
                errors.append(exc)
            finally:
                os.close(browser_read)
                os.close(browser_write)
        worker = threading.Thread(target=browser, daemon=True)
        worker.start()
        with CdpPipe(controller_read, controller_write, timeout_seconds=3) as channel:
            result = channel.rpc('Page.setDocumentContent', {'html': '页面' * 70000})
            self.assertEqual(result, {'ok': '当前'})
            self.assertEqual(channel.events_seen, 1)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        for fd in (controller_read, controller_write):
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_conditions_and_snapshot_identity_cannot_bypass_preflight(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        raw = b'<html><body>preview</body></html>'
        digest = hashlib.sha256(raw).hexdigest()
        for conditions in (RenderConditions(width=True), RenderConditions(height=-1),
                           RenderConditions(width=5000), RenderConditions(dpr=True),
                           RenderConditions(dpr=float('nan')), RenderConditions(dpr=float('inf')),
                           RenderConditions(theme='unknown'), RenderConditions(width=4096, height=4096, dpr=2)):
            with self.subTest(conditions=conditions):
                result = CdpRenderer().render(raw, conditions, None, expected_sha256=digest)
                self.assertEqual(result.status, 'REJECTED')
        self.assertEqual(CdpRenderer().render(raw, RenderConditions(), None,
                                             expected_sha256='a' * 64).status, 'REJECTED')

    def test_illegal_html_is_rejected_even_without_a_provider_and_valid_input_is_unavailable(self):
        from aek.adapters.ui_renderer import CdpRenderer
        from aek.core.artifact.ui_preview import RenderConditions
        renderer = CdpRenderer()
        for raw in (b'<script>secret()</script>', b'<img src="file:///private">',
                    b'<style>body{background:url(https://example.invalid/a)}</style>'):
            with self.subTest(raw=raw):
                result = renderer.render(raw, RenderConditions(), None,
                                         expected_sha256=hashlib.sha256(raw).hexdigest())
                self.assertEqual(result.status, 'REJECTED')
                self.assertIsNone(result.png)
                self.assertFalse(result.comparable)
        raw = b'<!doctype html><html><body><h1>Preview</h1></body></html>'
        result = renderer.render(raw, RenderConditions(), None,
                                 expected_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.status, 'UNAVAILABLE')
        self.assertEqual(result.reason_code, 'APPROVED_PROVIDER_UNAVAILABLE')
        self.assertFalse(result.comparable)
        self.assertIsNone(result.png)


if __name__ == '__main__':
    unittest.main()
