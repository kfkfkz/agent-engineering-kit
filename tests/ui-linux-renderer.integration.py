#!/usr/bin/env python3
"""Explicit real-browser integration; no download or default suite execution.

The original host-event boundary is a controlled test adapter. This validates
actual binaries and OS/process/protocol behavior, NOT production host adoption,
a human approval, or visual correspondence to a business reference image.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aek.adapters.ui_linux_renderer import LinuxRenderProvider, LinuxRuntime, render_plan
from aek.adapters.ui_renderer import CdpRenderer
from aek.core.artifact.ui_preview import RenderConditions


def archive_sha(path, expected):
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise ValueError('fixed candidate archive differs')


def bwrap_sha(deb):
    # Read a fixed authenticated ar/tar member; never extract archive paths.
    raw = deb.read_bytes()
    if not raw.startswith(b'!<arch>\n') or len(raw) > 1024 * 1024:
        raise ValueError('invalid candidate package')
    offset = 8
    while offset + 60 <= len(raw):
        header = raw[offset:offset + 60]
        size = int(header[48:58])
        name = header[:16].decode('ascii').strip().rstrip('/')
        data = raw[offset + 60:offset + 60 + size]
        offset += 60 + size + (size % 2)
        if name.startswith('data.tar.'):
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
                member = archive.getmember('./usr/bin/bwrap')
                if not member.isfile() or member.size > 1024 * 1024:
                    raise ValueError('invalid candidate executable')
                return hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    raise ValueError('candidate executable is absent')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', required=True, type=Path)
    parser.add_argument('--html', type=Path, help='Explicit local static snapshot, never a URL')
    parser.add_argument('--background-reference', type=Path,
                        help='Local-only PNG for a screenshot-backed modification-region storyboard')
    parser.add_argument('--width', type=int, default=1200)
    parser.add_argument('--height', type=int, default=800)
    args = parser.parse_args()
    root = args.runtime_root.absolute()
    chrome_zip = root / 'chrome-headless-shell-linux64.zip'
    deb = root / 'bubblewrap_0.12.0-1~deb13u1_amd64.deb'
    archive_sha(chrome_zip, '4cea4c32b501aa43cc2b81eb45734af991a6a82feded48380d6dfaf96642a1c1')
    archive_sha(deb, '70aca4fa8daeacb677ec00e8063eb586f08ae3d94b1f11e684370b5524c43431')
    files = []
    with zipfile.ZipFile(chrome_zip) as archive:
        for item in archive.infolist():
            if not item.is_dir():
                prefix = 'chrome-headless-shell-linux64/'
                if not item.filename.startswith(prefix):
                    raise ValueError('candidate runtime layout differs')
                with archive.open(item) as stream:
                    sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                files.append((item.filename[len(prefix):], sha))
    with Path('/usr/bin/prlimit').open('rb') as stream:
        prlimit_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    runtime = LinuxRuntime(str(root / 'chrome-headless-shell-linux64'),
        str(root / 'bwrap-root/usr/bin/bwrap'), '/usr/bin/prlimit', tuple(sorted(files)),
        bwrap_sha(deb), prlimit_sha,
        ('/usr/lib', '/usr/lib64', '/usr/share/fonts', '/usr/share/fontconfig', '/etc/fonts'))
    fixture_spec = importlib.util.spec_from_file_location('integration_host_fixture',
                                                        ROOT / 'tests/security-workflow.test.py')
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    host = fixture.LiveHost()
    authority = fixture.make_authority(host)
    snapshot = ('<!doctype html><html lang="zh"><meta charset="UTF-8"><title>受控渲染集成</title>'
        '<style>body{font-family:sans-serif;background:#f5f6ff;color:#172b4d;padding:48px}'
        'main{background:white;border-radius:12px;padding:24px;border:1px solid #d9dfff}</style>'
        '<main><h1>静态页面 · Linux 真实浏览器</h1><p>无页面脚本，无外部资源，无业务数据。</p></main></html>').encode('utf-8')
    sha = hashlib.sha256(snapshot).hexdigest()
    conditions = RenderConditions(args.width, args.height)
    conditions.checked()
    if args.html is not None:
        from aek.adapters import ui_filesystem as fs
        path = args.html.absolute()
        parent = fs.open_absolute_dir(path.parent)
        try:
            snapshot = fs.read_at(parent, path.name, 5 * 1024 * 1024)
        finally:
            fs.close_dir(parent)
        sha = hashlib.sha256(snapshot).hexdigest()
    reference_sha = None
    if args.background_reference is not None:
        from aek.adapters import ui_filesystem as fs
        path = args.background_reference.absolute()
        parent = fs.open_absolute_dir(path.parent)
        try:
            reference = fs.read_at(parent, path.name, 512 * 1024)
        finally:
            fs.close_dir(parent)
        if args.html is None or not reference.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('a local HTML modification and PNG reference are required')
        reference_sha = hashlib.sha256(reference).hexdigest()
        overlay = (b'<style>header,aside,.main,.dim{display:none!important}.dialog{z-index:1}</style>'
            b'<img alt="Approved local screenshot reference" '
            b'style="position:fixed;inset:0;width:100%;height:100%;object-fit:contain;z-index:0" '
            b'src="data:image/png;base64,' + base64.b64encode(reference) + b'">')
        snapshot = snapshot.replace(b'</html>', overlay + b'</html>')
        sha = hashlib.sha256(snapshot).hexdigest()
    plan = render_plan(runtime, sha, conditions, repository_ref=authority.repository_ref,
        scope_ref=authority.scope_ref, identity_digest=authority.current_identity.envelope_digest)
    event = host.issue(authority, 'user-confirmation', 'execute', plan.binding())
    grant = authority.on_user_confirmation(event, binding=plan.binding())
    provider = LinuxRenderProvider(runtime, authority=authority, grant=grant,
                                   adoption_validator=lambda current: current == runtime)
    result = CdpRenderer(provider).render(snapshot, conditions, plan, expected_sha256=sha)
    output = Path(tempfile.mkdtemp(prefix='native-render-', dir=root))
    (output / 'input.html').write_bytes(snapshot)
    if result.png is not None:
        (output / 'render.png').write_bytes(result.png)
    report = {'schema_version': 2, 'status': result.status, 'reason_code': result.reason_code,
        'actual_browser': result.status == 'RENDERED', 'host_boundary': 'controlled-integration-event',
        'production_host_adoption': False, 'human_approval': False, 'comparable': result.comparable,
        'input_sha256': result.input_sha256, 'output_sha256': result.output_sha256,
        'runtime_sha256': result.runtime_sha256, 'plan_sha256': result.plan_sha256,
        'conditions': result.conditions, 'unknowns': list(result.unknowns), 'output_dir': str(output)}
    report['reference_sha256'] = reference_sha
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if result.status == 'RENDERED' else 1


if __name__ == '__main__':
    sys.exit(main())
