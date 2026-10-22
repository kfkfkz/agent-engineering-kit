#!/usr/bin/env python3
"""Native lifecycle tests are not evidence of a real browser or a host grant."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aek.core.artifact.ui_preview import RenderConditions
from aek.adapters.ui_renderer import CdpRenderer
from aek.adapters.ui_linux_renderer import LinuxRenderProvider, LinuxRuntime, render_plan

QUALIFIED_NATIVE = sys.platform == 'linux' and hasattr(os, 'POSIX_SPAWN_CLOSEFROM')


class LinuxRendererTests(unittest.TestCase):
    def test_missing_closefrom_refuses_before_process_or_descriptor_creation(self):
        from aek.adapters.ui_linux_renderer import _NativeProcess
        from aek.adapters.ui_cdp import CdpError
        process = _NativeProcess((sys.executable, '-c', 'raise RuntimeError()'),
                                 timeout=1, max_bytes=1024, max_events=1)
        with patch.dict(os.__dict__):
            os.__dict__.pop('POSIX_SPAWN_CLOSEFROM', None)
            with patch('aek.adapters.ui_linux_renderer.os.posix_spawn', create=True,
                       side_effect=AssertionError('must not spawn')) as spawn, \
                    patch('aek.adapters.ui_linux_renderer.os.pipe',
                          side_effect=AssertionError('must not open descriptors')):
                with self.assertRaises(CdpError) as caught:
                    process.__enter__()
                self.assertEqual(caught.exception.reason_code, 'RENDER_PROCESS_UNAVAILABLE')
                spawn.assert_not_called()
        self.assertIsNone(process.pid)

    @unittest.skipUnless(QUALIFIED_NATIVE, 'Linux spawn closefrom private pipes')
    def test_spawn_keeps_devnull_stdio_separate_from_cdp_descriptors(self):
        from aek.adapters.ui_linux_renderer import _NativeProcess
        code = '''import json,os,stat
request=json.loads(os.read(3,65536).split(b'\\0')[0])
result={'stdio_private':all(stat.S_ISCHR(os.fstat(fd).st_mode) for fd in (0,1))}
os.write(4,json.dumps({'id':request['id'],'result':result}).encode()+b'\\0')
os.read(3,65536)
'''
        with _NativeProcess((sys.executable, '-c', code), timeout=2,
                            max_bytes=65536, max_events=10) as channel:
            self.assertTrue(channel.rpc('Browser.getVersion')['stdio_private'])

    def _fixture(self, directory):
        from aek.core.security.authority import digest
        spec = importlib.util.spec_from_file_location('native_host_fixture',
            Path(__file__).with_name('security-workflow.test.py'))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        host = fixture.LiveHost()
        authority = fixture.make_authority(host)
        root = Path(directory)
        browser = root / 'browser'
        browser.mkdir()
        (browser / 'chrome-headless-shell').write_bytes(b'fixed browser fixture')
        (root / 'bwrap').write_bytes(b'fixed bwrap fixture')
        (root / 'prlimit').write_bytes(b'fixed prlimit fixture')
        for path in (browser / 'chrome-headless-shell', root / 'bwrap', root / 'prlimit'):
            path.chmod(0o700)
        runtime = LinuxRuntime(str(browser), str(root / 'bwrap'), str(root / 'prlimit'),
            (('chrome-headless-shell', hashlib.sha256(b'fixed browser fixture').hexdigest()),),
            hashlib.sha256(b'fixed bwrap fixture').hexdigest(), hashlib.sha256(b'fixed prlimit fixture').hexdigest(),
            ('/usr/lib', '/usr/lib64', '/usr/share/fonts', '/usr/share/fontconfig', '/etc/fonts'))
        raw = b'<main>native profile fixture</main>'
        sha = hashlib.sha256(raw).hexdigest()
        plan = render_plan(runtime, sha, RenderConditions(), repository_ref=authority.repository_ref,
            scope_ref=authority.scope_ref, identity_digest=authority.current_identity.envelope_digest)
        event = host.issue(authority, 'user-confirmation', 'execute', plan.binding())
        grant = authority.on_user_confirmation(event, binding=plan.binding())
        return runtime, authority, grant, plan, raw

    def test_imported_plan_never_authorizes_runtime_io_or_process_start(self):
        runtime = LinuxRuntime('/unread/browser', '/unread/bwrap', '/usr/bin/prlimit',
            (('chrome-headless-shell', 'a' * 64),), 'b' * 64, 'c' * 64,
            ('/usr/lib', '/usr/lib64', '/usr/share/fonts', '/usr/share/fontconfig', '/etc/fonts'))
        raw = b'<main>not an execution grant</main>'
        sha = hashlib.sha256(raw).hexdigest()
        plan = render_plan(runtime, sha, RenderConditions(), repository_ref='/repo',
                           scope_ref='scope', identity_digest='d' * 64)
        provider = LinuxRenderProvider(runtime, authority=None, grant={'approved': True},
                                       adoption_validator=lambda _runtime: True)
        with patch('aek.adapters.ui_linux_renderer._NativeProcess.__enter__',
                   side_effect=AssertionError('must not start')):
            result = CdpRenderer(provider).render(raw, RenderConditions(), plan, expected_sha256=sha)
        self.assertEqual(result.status, 'UNAVAILABLE')
        self.assertIsNone(result.png)

    @unittest.skipUnless(QUALIFIED_NATIVE, 'Linux spawn closefrom lifecycle')
    def test_native_controller_reaps_owned_process_after_deadline(self):
        from aek.adapters.ui_linux_renderer import _NativeProcess
        from aek.adapters.ui_cdp import CdpError
        process = _NativeProcess((sys.executable, '-c', 'import time; time.sleep(30)'),
                                 timeout=0.15, max_bytes=1024, max_events=10)
        with self.assertRaises((CdpError, ValueError)):
            with process as channel:
                channel.rpc('Browser.getVersion')
        self.assertTrue(process.reaped)
        self.assertEqual(process.reason, 'RENDER_TIMEOUT')
        self.assertFalse(process.remaining())

    @unittest.skipUnless(QUALIFIED_NATIVE, 'Linux spawn closefrom lifecycle')
    def test_native_stderr_limit_fails_without_echoing_output(self):
        from aek.adapters.ui_linux_renderer import _NativeProcess
        from aek.adapters.ui_cdp import CdpError
        process = _NativeProcess((sys.executable, '-c',
            'import os,time; os.write(2,b"private-text"*10000); time.sleep(30)'),
            timeout=2, max_bytes=1024, max_events=10)
        with self.assertRaises(CdpError) as caught:
            with process as channel:
                channel.rpc('Browser.getVersion')
        self.assertEqual(process.reason, 'RENDER_OUTPUT_LIMIT')
        self.assertNotIn('private-text', str(caught.exception))
        self.assertTrue(process.reaped)
        self.assertFalse(process.remaining())

    @unittest.skipUnless(QUALIFIED_NATIVE, 'Linux spawn closefrom lifecycle')
    def test_native_memory_guard_stops_process_without_leaking_handles(self):
        from aek.adapters.ui_linux_renderer import _NativeProcess
        from aek.adapters.ui_cdp import CdpError
        process = _NativeProcess((sys.executable, '-c', 'import time; time.sleep(30)'),
            timeout=2, max_bytes=1024, max_events=10, max_rss_bytes=65536)
        descriptors_before = len(os.listdir('/proc/self/fd'))
        with self.assertRaises(CdpError) as caught:
            with process as channel:
                channel.rpc('Browser.getVersion')
        self.assertEqual(caught.exception.reason_code, 'RENDER_MEMORY_LIMIT')
        self.assertTrue(process.reaped)
        self.assertFalse(process.remaining())
        self.assertEqual(len(os.listdir('/proc/self/fd')), descriptors_before)

    @unittest.skipUnless(sys.platform == 'linux', 'native Linux runtime')
    def test_adopted_profile_is_checked_against_actual_bytes_and_exact_file_set(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, authority, grant, plan, raw = self._fixture(directory)
            provider = LinuxRenderProvider(runtime, authority=authority, grant=grant,
                                           adoption_validator=lambda candidate: candidate == runtime)
            self.assertTrue(provider._runtime_current())
            (Path(runtime.browser_root) / 'unexpected-user-data.txt').write_text('do not mount me')
            self.assertFalse(provider._runtime_current())
            with patch('os.posix_spawn', side_effect=AssertionError('must not start')):
                result = CdpRenderer(provider).render(raw, RenderConditions(), plan,
                    expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(result.status, 'UNAVAILABLE')

    @unittest.skipUnless(sys.platform == 'linux', 'native Linux runtime')
    def test_native_plan_builds_only_fixed_namespace_command_and_retains_original_grant_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, authority, grant, plan, raw = self._fixture(directory)
            provider = LinuxRenderProvider(runtime, authority=authority, grant=grant,
                                           adoption_validator=lambda candidate: candidate == runtime)
            with provider._pin_runtime() as pinned:
                argv = provider._argv(plan, pinned)
            self.assertIn('--unshare-net', argv)
            self.assertIn('--unshare-pid', argv)
            self.assertIn('--clearenv', argv)
            self.assertIn('--remote-debugging-pipe', argv)
            self.assertIn('--browser-subprocess-path=/runtime/chrome-headless-shell', argv)
            self.assertIn('--fsize=8388608', argv)
            self.assertNotIn('--no-sandbox', argv)
            self.assertNotIn('--disable-setuid-sandbox', argv)
            self.assertNotIn('--not-a-security-boundary', argv)
            self.assertNotIn('--remote-debugging-port', ' '.join(argv))
            self.assertNotIn('/repo', argv)
            self.assertNotIn(os.path.expanduser('~'), argv)
            mounts = [argv[index + 2] for index, value in enumerate(argv)
                      if value in ('--ro-bind-fd', '--file')]
            self.assertEqual(set(mounts), set(runtime.readonly_roots) | {'/runtime/chrome-headless-shell', '/tools/prlimit'})
            readonly = [argv[index + 1] for index, value in enumerate(argv) if value == '--remount-ro']
            self.assertEqual(readonly, ['/tools', '/runtime'])
            self.assertNotIn('/usr', mounts)
            self.assertNotIn(runtime.browser_root, argv)
            self.assertNotIn(runtime.bubblewrap, argv)
            self.assertNotIn(runtime.prlimit, argv)
            self.assertEqual(plan.binding()['max_output_bytes'], 2_000_000)
            with self.assertRaises(ValueError):
                render_plan(runtime, hashlib.sha256(raw).hexdigest(), RenderConditions(),
                    repository_ref=authority.repository_ref, scope_ref=authority.scope_ref,
                    identity_digest=authority.current_identity.envelope_digest, max_output_bytes=2_000_001)

    @unittest.skipUnless(sys.platform == 'linux', 'native Linux runtime')
    def test_path_replacement_after_validation_never_executes_unapproved_host_program(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, authority, grant, plan, raw = self._fixture(directory)
            provider = LinuxRenderProvider(runtime, authority=authority, grant=grant,
                                           adoption_validator=lambda candidate: candidate == runtime)
            marker = Path(directory) / 'outside-approved-isolation.txt'
            original_argv = provider._argv
            def replace_after_check(*args):
                argv = original_argv(*args)
                payload = ('#!' + sys.executable + '\nfrom pathlib import Path\nPath('
                           + repr(str(marker)) + ').write_text("unapproved-host-program")\n')
                Path(runtime.bubblewrap).write_text(payload)
                return argv
            with patch.object(provider, '_argv', side_effect=replace_after_check):
                result = CdpRenderer(provider).render(raw, RenderConditions(), plan,
                    expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertFalse(marker.exists(), 'a FAILED result cannot undo host execution')
            self.assertNotEqual(result.status, 'RENDERED')

    @unittest.skipUnless(QUALIFIED_NATIVE, 'Linux spawn closefrom process tree')
    def test_worker_thread_children_are_included_in_the_memory_guard(self):
        from aek.adapters.ui_linux_renderer import _NativeProcess
        from aek.adapters.ui_cdp import CdpError
        code = '''import json,os,subprocess,sys,threading,time
ready=threading.Event()
def run():
 child=subprocess.Popen([sys.executable,'-c','import os,time; x=bytearray(80*1024*1024); os.write(1,b"ready"); time.sleep(10)'],stdout=subprocess.PIPE)
 child.stdout.read(5);ready.set();child.wait()
threading.Thread(target=run,daemon=True).start();ready.wait();buf=b''
while True:
 chunk=os.read(3,65536)
 if not chunk:break
 buf+=chunk
 while b'\\0' in buf:
  frame,buf=buf.split(b'\\0',1);request=json.loads(frame)
  time.sleep(0.005)
  os.write(4,json.dumps({'id':request['id'],'result':{'product':'fixture'}}).encode()+b'\\0')
'''
        process = _NativeProcess((sys.executable, '-c', code), timeout=3, max_bytes=65536,
                                 max_events=10, max_rss_bytes=30 * 1024 * 1024)
        with self.assertRaises(CdpError):
            with process as channel:
                until = time.monotonic() + 1
                while time.monotonic() < until:
                    channel.rpc('Browser.getVersion')
        self.assertEqual(process.reason, 'RENDER_MEMORY_LIMIT')
        self.assertTrue(process.reaped)
        self.assertFalse(process.remaining())


if __name__ == '__main__':
    unittest.main()
