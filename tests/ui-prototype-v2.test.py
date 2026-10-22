from __future__ import annotations

import copy
import errno
import hashlib
import io
import importlib.util
import json
import os
import re
import subprocess
import shutil
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('prototype_v1_fixtures', ROOT / 'tests/ui-prototype-gate.test.py')
data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data)


class PrototypeV2Tests(unittest.TestCase):
    def test_crlf_disk_bytes_bind_public_prepare_confirm_check_and_receipt_recovery(self):
        self.save()
        target = ('PAGE-1', 'AREA-1', 'default', self.f.context_id)
        lf = data.GATE['_prepare_prototype_selection'](self.f.req, [target])
        for name in ('需求分析.md', '概要设计.md', 'UI设计.md'):
            path = self.f.req / name
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
            self.assertIn(b'\r\n', path.read_bytes())
        command = [sys.executable, str(ROOT / 'doc-gate')]
        options = dict(capture_output=True, encoding='utf-8', timeout=15)
        def prepare():
            return subprocess.run(command + ['prototype-prepare', str(self.f.req),
                '--target', '/'.join(target), '--json'], **options)
        result = prepare()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prepared = json.loads(result.stdout)
        self.assertNotEqual(prepared['per_item'][0]['dependency_sha256'], lf['per_item'][0]['dependency_sha256'])
        body = {key: prepared[key] for key in ('schema_version', 'status', 'per_item')}
        self.assertEqual(prepared['prepare_sha256'], data.canonical_digest(body))
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        arguments = ['doc-gate', 'prototype-confirm', str(self.f.req), '--schema-version', '2',
            '--target', '/'.join(target), '--prepared-sha256', prepared['prepare_sha256'], '--reviewer', 'protocol-test']
        with patch.object(sys, 'argv', arguments), patch.object(sys, 'stdin', Terminal()), \
                redirect_stdout(Terminal()), patch('builtins.input',
                return_value='ACCEPT ' + prepared['prepare_sha256'][:12]) as choose:
            self.assertEqual(data.GATE['main'](), 0)
        self.assertEqual(choose.call_count, 1)
        receipts = {path.name: path.read_bytes() for path in
            (self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')}
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(next(iter(receipts.values())))
        self.assertEqual(receipt['selection'], prepared)
        def check_prototype_status():
            checked = subprocess.run(command + ['check', str(self.f.req), '--stage', 'UI设计', '--json'], **options)
            # This focused fixture is not a complete UI document. Assert the
            # actual public report's prototype checks without declaring its
            # unrelated document requirements satisfied.
            report = json.loads(checked.stdout)
            statuses = {row['status'] for row in report['checks'] if row['group'] == 'prototype'}
            self.assertTrue(statuses)
            return statuses
        self.assertEqual(check_prototype_status(), {'PASS'})
        with patch.object(sys, 'argv', arguments), patch.object(sys, 'stdin', Terminal()), \
                redirect_stdout(Terminal()), patch('builtins.input', side_effect=AssertionError('must recover')):
            self.assertEqual(data.GATE['main'](), 0)
        self.assertEqual(receipts, {path.name: path.read_bytes() for path in
            (self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')})
        html = self.f.req / self.f.path
        html.write_bytes(self.f.html + b'changed')
        self.assertEqual(prepare().returncode, 3)
        self.assertIn('FAIL', check_prototype_status())
        html.write_bytes(self.f.html)
        overview = self.f.req / '概要设计.md'
        overview.write_bytes(overview.read_bytes().replace('原型评审记录'.encode(), '变更后的业务依赖'.encode()))
        updated = prepare()
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        self.assertNotEqual(json.loads(updated.stdout)['prepare_sha256'], prepared['prepare_sha256'])
        self.assertIn('FAIL', check_prototype_status())
        self.assertEqual(receipts, {path.name: path.read_bytes() for path in
            (self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')})

    @unittest.skipUnless(os.name == 'posix', 'kernel PTY driver seam')
    def test_pty_reader_accepts_eio_before_exit_status_but_rejects_other_io_errors(self):
        import pty
        import select
        self.save()
        original_open, original_read = pty.openpty, os.read
        for error in (errno.EIO, errno.EINVAL):
            with self.subTest(error=error):
                descriptors = {}
                class Process:
                    returncode = None
                    def __init__(self):
                        self.waits = []
                    def poll(self):
                        return self.returncode
                    def wait(self, timeout):
                        self.waits.append(timeout)
                        self.returncode = 0
                        return 0
                    def kill(self):
                        self.returncode = -9
                process = Process()
                def opened():
                    master, slave = original_open()
                    descriptors['master'] = master
                    return master, slave
                blocks = iter((b'{"status":"fixture-complete"}\n', OSError(error, 'controlled PTY close')))
                def read(fd, size):
                    if fd != descriptors.get('master'):
                        return original_read(fd, size)
                    block = next(blocks)
                    if isinstance(block, OSError):
                        self.assertIsNone(process.poll(), 'the wait status is intentionally not visible yet')
                        raise block
                    return block
                with patch.object(pty, 'openpty', opened), patch.object(os, 'read', read), \
                        patch.object(select, 'select', side_effect=lambda readable, *_args: (readable, [], [])), \
                        patch.object(subprocess, 'Popen', return_value=process):
                    if error == errno.EIO:
                        rc, payload, decisions, _ = self.drive_pty(
                            [('PAGE-1', 'AREA-1', 'default', self.f.context_id)], lambda *_args: 'CANCEL')
                        self.assertEqual((rc, payload, decisions), (0, {'status': 'fixture-complete'}, 0))
                        self.assertEqual(process.waits, [2])
                    else:
                        with self.assertRaises(OSError) as raised:
                            self.drive_pty([('PAGE-1', 'AREA-1', 'default', self.f.context_id)], lambda *_args: 'CANCEL')
                        self.assertEqual(raised.exception.errno, errno.EINVAL)
                        self.assertEqual(process.waits, [5])
                with self.assertRaises(OSError):
                    os.fstat(descriptors['master'])

    def test_fixture_text_matches_disk_even_when_native_text_writes_translate_crlf(self):
        original = Path.write_text
        def crlf_write(path, text, *args, **kwargs):
            kwargs.setdefault('newline', '\r\n')
            return original(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', crlf_write):
            text = self.save()
        self.assertEqual((self.f.req / 'UI设计.md').read_bytes(), text.encode('utf-8'))
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text, require_confirmation=False)
        self.assertEqual(checks.fails, [])

    def drive_pty(self, targets, choose):
        """Real kernel PTY, synthetic protocol response; never business approval."""
        import pty
        import select
        import time
        prepared = data.GATE['_prepare_prototype_selection'](self.f.req, targets)
        command = [sys.executable, str(ROOT / 'doc-gate'), 'prototype-confirm', str(self.f.req),
                   '--schema-version', '2', '--prepared-sha256', prepared['prepare_sha256'],
                   '--reviewer', 'protocol-test']
        for target in targets:
            command += ['--target', '/'.join(target)]
        master, slave = pty.openpty()
        process = None
        output, decisions = bytearray(), 0
        try:
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        block = os.read(master, 65536)
                    except OSError as exc:
                        # A closed PTY slave reports EIO before wait status
                        # necessarily becomes visible; wait/reap below still
                        # verifies exit status and the complete JSON response.
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not block:
                        break
                    output.extend(block)
                    self.assertLess(len(output), 256 * 1024)
                    if decisions == 0 and b'REVISE/BLOCK/CANCEL' in output:
                        text = output.decode('utf-8')
                        phrase = re.search(r'ACCEPT [0-9a-f]{12}', text).group()
                        os.write(master, (choose(text, phrase) + '\n').encode('utf-8'))
                        decisions += 1
                elif process.poll() is not None:
                    break
            rc = process.wait(timeout=2)
            text = output.decode('utf-8').replace('\r\n', '\n')
            payload = json.loads(next(line for line in reversed(text.splitlines()) if line.startswith('{')))
            return rc, payload, decisions, text
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if slave is not None:
                os.close(slave)
            os.close(master)

    @unittest.skipUnless(os.name == 'posix', 'real POSIX PTY; Windows uses its native CI path')
    def test_real_pty_batches_one_protocol_decision_and_preserves_receipts_on_retry(self):
        targets = self.add_context()
        self.save()
        def accept_displayed(text, phrase):
            for target in targets:
                self.assertIn(target[-1], text)
            return phrase
        rc, payload, decisions, _text = self.drive_pty(targets, accept_displayed)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(decisions, 1)
        self.assertEqual([row['status'] for row in payload['per_item']], ['ACCEPTED', 'ACCEPTED'])
        paths = list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        before = {path.name: path.read_bytes() for path in paths}
        self.assertEqual(len(before), 2)
        self.assertEqual({json.loads(raw)['reviewer'] for raw in before.values()}, {'protocol-test'})
        rc, payload, decisions, _text = self.drive_pty(targets,
            lambda *_args: self.fail('current receipts must not request another decision'))
        self.assertEqual(rc, 0, payload)
        self.assertEqual(decisions, 0)
        self.assertEqual(before, {path.name: path.read_bytes() for path in paths})

    @unittest.skipUnless(os.name == 'posix', 'real POSIX PTY; not a maintainer approval')
    def test_real_pty_cancel_then_material_change_cannot_approve_changed_item(self):
        targets = self.add_context()
        self.save()
        rc, payload, decisions, _text = self.drive_pty(targets, lambda *_args: 'CANCEL')
        self.assertEqual(rc, 0, payload)  # A valid cancellation is not a protocol conflict.
        self.assertEqual(decisions, 1)
        self.assertEqual({row['status'] for row in payload['per_item']}, {'CANCELLED'})
        self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])
        def change_displayed_item(_text, phrase):
            self.items[1]['change_intent'] = 'changed after actual terminal display'
            self.save()
            return phrase
        rc, payload, decisions, _text = self.drive_pty(targets, change_displayed_item)
        self.assertEqual(rc, 3, payload)
        self.assertEqual(decisions, 1)
        statuses = {row['context_id']: row['status'] for row in payload['per_item']}
        self.assertEqual(statuses, {'CTX-OTHER': 'CONFLICT', self.f.context_id: 'ACCEPTED'})
        self.assertEqual(len(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))), 1)

    def setUp(self):
        self.f = data.PrototypeGateTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        item = {key: copy.deepcopy(value) for key, value in self.f.item.items()
                if key not in {'status', 'reviewer', 'reviewed_at', 'confirmation_source', 'dependency_sha256'}}
        item.update(state_id='default', context_id=self.f.context_id)
        self.items = [item]

    def confirm(self, decision=None, *, prepared=None, stdin_tty=True, stdout_tty=True, reviewer='k', target=None):
        class Terminal(io.StringIO):
            def isatty(self):
                return stdout_tty
        class Input(io.StringIO):
            def isatty(self):
                return stdin_tty
        target = target or ('PAGE-1', 'AREA-1', 'default', self.f.context_id)
        prepared = prepared or data.GATE['_prepare_prototype_v2'](self.f.req, target)
        output, calls = Terminal(), []
        def choose(prompt):
            calls.append(prompt)
            if callable(decision):
                return decision(prompt)
            return decision or prompt.split('或')[0].split('输入 ')[1].strip()
        with redirect_stdout(output), patch.object(sys, 'stdin', Input()), patch('builtins.input', side_effect=choose):
            rc = data.GATE['cmd_prototype_confirm_v2'](self.f.req, target, reviewer, prepared['prepare_sha256'])
        return rc, output.getvalue(), calls

    def save(self):
        f = self.f
        f.index['screenshots'][0]['prototype_refs'] = [{
            **{key: item[key] for key in ('prototype_path', 'prototype_sha256', 'page_id', 'area_id',
                'feature_ids', 'acceptance_ids', 'change_intent', 'depends_on', 'artifact_plan_id',
                'gate_digest', 'approved_root', 'state_id', 'context_id')}, 'schema_version': 2}
            for item in self.items]
        (f.material / '截图索引.json').write_text(json.dumps(f.index), encoding='utf-8')
        text = f.ui_text + '```aek-prototypes\n' + json.dumps(
            {'schema_version': 2, 'prototypes': self.items}) + '\n```\n'
        # Direct gate calls consume this exact text; disk revalidation must see
        # the same UTF-8 bytes rather than platform-translated newlines.
        (f.req / 'UI设计.md').write_bytes(text.encode('utf-8'))
        return text

    def add_context(self):
        other = copy.deepcopy(self.f.index['screenshots'][0]['applies_to'][0])
        other.update(context_id='CTX-OTHER', relation='shared_layout')
        other['permissions']['data_id'] = 'DP02'
        self.f.index['screenshots'][0]['applies_to'].append(other)
        self.items.append({**copy.deepcopy(self.items[0]), 'context_id': 'CTX-OTHER',
                           'evidence_contexts': [{'evidence_id': 'P1', 'context_id': 'CTX-OTHER'}]})
        return [('PAGE-1', 'AREA-1', 'default', self.f.context_id),
                ('PAGE-1', 'AREA-1', 'default', 'CTX-OTHER')]

    def confirm_batch(self, targets, decision=None, *, prepared=None, reviewer='k'):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        prepared = prepared or data.GATE['_prepare_prototype_selection'](self.f.req, targets)
        output, calls = Terminal(), []
        def choose(prompt):
            calls.append(prompt)
            return decision(prompt) if callable(decision) else (
                decision or prompt.split('或')[0].split('输入 ')[1].strip())
        with redirect_stdout(output), patch.object(sys, 'stdin', Terminal()), patch('builtins.input', side_effect=choose):
            rc = data.GATE['cmd_prototype_confirm_batch'](
                self.f.req, targets, reviewer, prepared['prepare_sha256'])
        return rc, json.loads(output.getvalue().splitlines()[-1]), calls

    def test_one_batch_decision_creates_individual_receipts_and_recovers_without_reapproval(self):
        targets = self.add_context()
        self.save()
        rc, payload, calls = self.confirm_batch(targets)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual([row['status'] for row in payload['per_item']], ['ACCEPTED', 'ACCEPTED'])
        paths = list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        self.assertEqual(len(paths), 2)
        receipts = [json.loads(path.read_bytes()) for path in paths]
        self.assertEqual(receipts[0]['selection'], receipts[1]['selection'])
        self.assertEqual(len(receipts[0]['selection']['per_item']), 2)
        self.assertEqual(receipts[0]['confirmation_phrase'], calls[0].split('或')[0].split('输入 ')[1].strip())
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, self.save())
        self.assertEqual(checks.fails, [])
        before = {path.name: path.read_bytes() for path in paths}
        rc, payload, calls = self.confirm_batch(targets, reviewer='other')
        self.assertEqual(rc, 0, payload)
        self.assertEqual(calls, [])
        self.assertEqual({row['reviewer'] for row in payload['per_item']}, {'k'})
        self.assertEqual(before, {path.name: path.read_bytes() for path in paths})

    def test_batch_change_during_decision_is_local_and_retry_only_prompts_for_unfinished_item(self):
        targets = self.add_context()
        self.save()
        def change_one(prompt):
            self.items[1]['change_intent'] = '仅此权限的新变更'
            self.save()
            return prompt.split('或')[0].split('输入 ')[1].strip()
        rc, payload, calls = self.confirm_batch(targets, change_one)
        self.assertEqual(rc, 3, payload)
        self.assertEqual(len(calls), 1)
        statuses = {row['context_id']: row['status'] for row in payload['per_item']}
        self.assertEqual(statuses, {'CTX-OTHER': 'CONFLICT', self.f.context_id: 'ACCEPTED'})
        paths = list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        self.assertEqual(len(paths), 1)
        before = paths[0].read_bytes()
        rc, payload, calls = self.confirm_batch(targets, reviewer='second')
        self.assertEqual(rc, 0, payload)
        self.assertEqual(len(calls), 1)
        receipts = [json.loads(path.read_bytes()) for path in
                    (self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(paths[0].read_bytes(), before)
        newest = next(row for row in receipts if row['reviewer'] == 'second')
        self.assertEqual([row['context_id'] for row in newest['selection']['per_item']], ['CTX-OTHER'])
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, self.save())
        self.assertEqual(checks.fails, [])

    def test_batch_cli_requires_explicit_v2_and_real_terminal_without_prompting_from_pipe(self):
        targets = self.add_context()
        self.save()
        prepared = data.GATE['_prepare_prototype_selection'](self.f.req, targets)
        command = [sys.executable, str(ROOT / 'doc-gate'), 'prototype-confirm', str(self.f.req),
                   '--schema-version', '2', '--prepared-sha256', prepared['prepare_sha256'], '--reviewer', 'k']
        for target in targets:
            command += ['--target', '/'.join(target)]
        result = subprocess.run(command, input='ACCEPT anything\n',
                                capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('interactive', json.loads(result.stdout)['per_item'][0]['error'])
        self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])

    def test_batch_publication_failure_preserves_completed_fact_and_retries_only_pending(self):
        targets = self.add_context()
        self.save()
        original = data.GATE['_write_prototype_confirmation']
        def fail_one(*args, **kwargs):
            if kwargs.get('context_id') == self.f.context_id:
                raise OSError('controlled publication failure')
            return original(*args, **kwargs)
        with patch.dict(data.GATE['cmd_prototype_confirm_batch'].__globals__,
                        {'_write_prototype_confirmation': fail_one}):
            rc, payload, calls = self.confirm_batch(targets)
        self.assertEqual(rc, 3, payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual({row['context_id']: row['status'] for row in payload['per_item']},
                         {'CTX-OTHER': 'ACCEPTED', self.f.context_id: 'CONFLICT'})
        path = next((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        before = path.read_bytes()
        rc, payload, calls = self.confirm_batch(targets)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual(before, path.read_bytes())
        other = next(path for path in (self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')
                     if path.read_bytes() != before)
        self.assertEqual([row['context_id'] for row in json.loads(other.read_bytes())['selection']['per_item']],
                         [self.f.context_id])

    def test_later_batch_receipt_io_cannot_leave_an_earlier_revoked_context_reported_current(self):
        targets = self.add_context()
        self.save()
        prepared = data.GATE['_prepare_prototype_selection'](self.f.req, targets)
        item = next(row for row in prepared['per_item'] if row['context_id'] == self.f.context_id)
        name = data.GATE['_prototype_confirmation_path']('PAGE-1', 'AREA-1',
            item['prototype_sha256'], item['dependency_sha256'], **{
                key: item[key] for key in ('state_id', 'context_id', 'scope_digest')})
        original = data.check_prototypes.__globals__['_read_beneath']
        reads = 0
        def read_then_revoke(handle, path, limit):
            nonlocal reads
            raw = original(handle, path, limit)
            if str(path).split('/')[-1] == name:
                reads += 1
                if reads == 2:
                    self.f.index['screenshots'][0]['applies_to'][1]['content_review']['status'] = 'not_applicable'
                    self.save()
            return raw
        with patch.dict(data.check_prototypes.__globals__, {'_read_beneath': read_then_revoke}):
            rc, payload, calls = self.confirm_batch(targets, prepared=prepared)
        self.assertGreaterEqual(reads, 2)
        self.assertEqual(rc, 3, payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual({row['context_id']: row['status'] for row in payload['per_item']},
                         {'CTX-OTHER': 'CONFLICT', self.f.context_id: 'ACCEPTED'})
        self.assertEqual(len(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))), 2,
                         'historical facts remain, but revoked facts are not current approvals')

    def test_batch_receipt_rejects_changed_selection_nonmembership_or_individual_phrase(self):
        targets = self.add_context()
        self.save()
        self.assertEqual(self.confirm_batch(targets)[0], 0)
        path = next((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        original = json.loads(path.read_bytes())
        for mutation in ('content', 'membership', 'phrase', 'malformed'):
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(original)
                if mutation == 'content':
                    receipt['selection']['per_item'][0]['prototype_sha256'] = 'a' * 64
                elif mutation == 'membership':
                    receipt['selection'] = data.GATE['_prototype_prepared_payload']([
                        row for row in receipt['selection']['per_item']
                        if row['context_id'] != receipt['context_id']])
                    receipt['confirmation_phrase'] = 'ACCEPT ' + receipt['selection']['prepare_sha256'][:12]
                elif mutation == 'phrase':
                    receipt['confirmation_phrase'] = 'ACCEPT not-the-group'
                else:
                    receipt['selection']['per_item'] = ['not an object']
                path.write_text(json.dumps(receipt), encoding='utf-8')
                checks = data.Checks('UI设计')
                data.check_prototypes(checks, self.f.req, self.save())
                self.assertTrue(checks.fails)
                rc, payload, calls = self.confirm_batch(targets)
                self.assertEqual(rc, 3, payload)
                self.assertEqual(calls, [], 'corrupt immutable fact must not be reapproved or overwritten')
                self.assertEqual(json.loads(path.read_bytes()), receipt)
        path.write_text(json.dumps(original), encoding='utf-8')

    def test_batch_revision_block_and_cancel_preserve_existing_fact_and_do_not_approve_pending(self):
        targets = self.add_context()
        self.save()
        self.assertEqual(self.confirm(target=targets[0])[0], 0)
        path = next((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        before = path.read_bytes()
        for decision, status in (('REVISE', 'NEEDS_REVISION'), ('BLOCK', 'BLOCKED'), ('CANCEL', 'CANCELLED')):
            with self.subTest(decision=decision):
                rc, payload, calls = self.confirm_batch(targets, decision)
                self.assertEqual(rc, 0, payload)
                self.assertEqual(len(calls), 1)
                self.assertEqual({row['context_id']: row['status'] for row in payload['per_item']},
                                 {'CTX-OTHER': status, self.f.context_id: 'ACCEPTED'})
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(len(list(path.parent.glob('v2-*.json'))), 1)

    def test_batch_prepare_cli_is_readonly_and_binds_only_explicit_unique_selection(self):
        targets = self.add_context()
        self.save()
        before = {path.relative_to(self.f.repo): path.read_bytes()
                  for path in self.f.repo.rglob('*') if path.is_file()}
        command = [sys.executable, str(ROOT / 'doc-gate'), 'prototype-prepare', str(self.f.req), '--json']
        for target in targets:
            command += ['--target', '/'.join(target)]
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prepared = json.loads(result.stdout)
        self.assertEqual(prepared['status'], 'PREPARED')
        self.assertEqual({row['context_id'] for row in prepared['per_item']}, {self.f.context_id, 'CTX-OTHER'})
        self.assertNotIn('ACCEPTED', result.stdout)
        repeated = subprocess.run(command + ['--target', '/'.join(targets[0])],
                                  capture_output=True, encoding='utf-8', timeout=15)
        self.assertNotEqual(repeated.returncode, 0)
        after = {path.relative_to(self.f.repo): path.read_bytes()
                 for path in self.f.repo.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_v2_preparation_needs_no_candidate_acceptance_and_keeps_permission_contexts_distinct(self):
        other = copy.deepcopy(self.f.index['screenshots'][0]['applies_to'][0])
        other.update(context_id='CTX-OTHER', relation='shared_layout')
        other['permissions']['data_id'] = 'DP02'
        self.f.index['screenshots'][0]['applies_to'].append(other)
        second = copy.deepcopy(self.items[0])
        second.update(context_id='CTX-OTHER', evidence_contexts=[{'evidence_id': 'P1', 'context_id': 'CTX-OTHER'}])
        self.items.append(second)
        text = self.save()
        before = {path.relative_to(self.f.repo): path.read_bytes() for path in self.f.repo.rglob('*') if path.is_file()}
        checks, dependencies = data.Checks('UI设计'), {}
        data.check_prototypes(checks, self.f.req, text, require_confirmation=False, dependency_out=dependencies)
        self.assertEqual(checks.fails, [])
        self.assertEqual(set(dependencies), {('PAGE-1', 'AREA-1', 'default', self.f.context_id),
                                            ('PAGE-1', 'AREA-1', 'default', 'CTX-OTHER')})
        self.assertEqual(len(set(dependencies.values())), 2)
        after = {path.relative_to(self.f.repo): path.read_bytes() for path in self.f.repo.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text)
        self.assertTrue(checks.fails, 'existing v1 confirmation cannot approve either v2 context')

    def test_read_only_prepare_cli_binds_actual_version_without_writing_an_acceptance(self):
        self.save()
        before = {path.relative_to(self.f.repo): path.read_bytes() for path in self.f.repo.rglob('*') if path.is_file()}
        result = subprocess.run([sys.executable, str(ROOT / 'doc-gate'), 'prototype-prepare', str(self.f.req),
            '--page', 'PAGE-1', '--area', 'AREA-1', '--state', 'default', '--context', self.f.context_id,
            '--json'], capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prepared = json.loads(result.stdout)
        self.assertEqual(prepared['schema_version'], 2)
        self.assertEqual(prepared['status'], 'PREPARED')
        self.assertEqual(len(prepared['per_item']), 1)
        self.assertEqual(prepared['per_item'][0]['prototype_sha256'], self.f.prototype_digest)
        self.assertEqual(prepared['per_item'][0]['context_id'], self.f.context_id)
        self.assertEqual(len(prepared['prepare_sha256']), 64)
        self.assertNotIn('ACCEPTED', result.stdout)
        after = {path.relative_to(self.f.repo): path.read_bytes() for path in self.f.repo.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_single_interactive_v2_decision_creates_scoped_receipt_without_candidate_acceptance(self):
        self.save()
        target = ('PAGE-1', 'AREA-1', 'default', self.f.context_id)
        prepared = data.GATE['_prepare_prototype_v2'](self.f.req, target)
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        output = Terminal()
        calls = []
        def decide(prompt):
            calls.append(prompt)
            return prompt.split('或')[0].split('输入 ')[1].strip()
        with redirect_stdout(output), patch.object(sys, 'stdin', Terminal()), patch('builtins.input', side_effect=decide):
            rc = data.GATE['cmd_prototype_confirm_v2'](self.f.req, target, 'k', prepared['prepare_sha256'])
        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(calls), 1)
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, self.save())
        self.assertEqual(checks.fails, [])
        receipts = list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_bytes())
        self.assertEqual(receipt['schema_version'], 2)
        self.assertEqual(receipt['state_id'], 'default')
        self.assertEqual(receipt['context_id'], self.f.context_id)
        self.assertEqual(receipt['confirmation_method'], 'interactive_tty')
        self.assertNotIn('status', self.items[0])

    def test_pipeline_or_redirected_display_never_prompts_or_writes_v2_acceptance(self):
        self.save()
        for stdin, stdout in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=stdin, stdout=stdout):
                rc, output, calls = self.confirm(stdin_tty=stdin, stdout_tty=stdout)
                self.assertEqual(rc, 3, output)
                self.assertEqual(calls, [])
                self.assertIn('interactive', output)
                self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])

    def test_revision_block_and_cancel_decisions_do_not_write_accepted_receipts(self):
        self.save()
        for decision, expected in (('REVISE', 'NEEDS_REVISION'), ('BLOCK', 'BLOCKED'), ('CANCEL', 'CANCELLED')):
            with self.subTest(decision=decision):
                rc, output, calls = self.confirm(decision)
                self.assertEqual(rc, 0, output)
                self.assertEqual(len(calls), 1)
                self.assertEqual(json.loads(output.splitlines()[-1])['per_item'][0]['status'], expected)
                self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])

    def test_existing_current_receipt_recovers_without_a_second_decision_or_rewriting_history(self):
        self.save()
        self.assertEqual(self.confirm()[0], 0)
        before = {path.name: path.read_bytes() for path in (self.f.req / 'reviews/ui-prototype-confirmations').iterdir()}
        rc, output, calls = self.confirm(reviewer='other')
        self.assertEqual(rc, 0, output)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(output)['per_item'][0]['reviewer'], 'k')
        after = {path.name: path.read_bytes() for path in (self.f.req / 'reviews/ui-prototype-confirmations').iterdir()}
        self.assertEqual(before, after)

    def test_actual_image_changed_while_human_decides_cannot_publish_old_acceptance(self):
        self.save()
        def change_image(prompt):
            (self.f.material / 'function/FP01/data/DP01/P1.png').write_bytes(b'changed screenshot')
            return prompt.split('或')[0].split('输入 ')[1].strip()
        rc, output, calls = self.confirm(change_image)
        self.assertEqual(rc, 3, output)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])

    def test_stale_preparation_is_rejected_before_the_human_prompt(self):
        self.save()
        target = ('PAGE-1', 'AREA-1', 'default', self.f.context_id)
        prepared = data.GATE['_prepare_prototype_v2'](self.f.req, target)
        self.items[0]['change_intent'] = '新变更意图'
        self.save()
        rc, output, calls = self.confirm(prepared=prepared)
        self.assertEqual(rc, 3, output)
        self.assertEqual(calls, [])
        self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])

    def test_state_is_bound_to_actual_image_and_never_reuses_default_confirmation(self):
        self.save()
        self.assertEqual(self.confirm()[0], 0)
        self.items[0]['state_id'] = 'loading'
        text = self.save()
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text, require_confirmation=False)
        self.assertTrue(checks.fails, 'declaring loading cannot reuse a default-state screenshot')
        row = self.f.index['screenshots'][0]
        row['state'] = 'loading'
        row['path'] = 'function/FP01/data/DP01/P1-loading.png'
        (self.f.material / row['path']).write_bytes(self.f.screenshot_bytes)
        text = self.save()
        checks, dependencies = data.Checks('UI设计'), {}
        data.check_prototypes(checks, self.f.req, text, require_confirmation=False, dependency_out=dependencies)
        self.assertEqual(checks.fails, [])
        self.assertEqual(set(dependencies), {('PAGE-1', 'AREA-1', 'loading', self.f.context_id)})
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text)
        self.assertTrue(checks.fails, 'old default acceptance cannot approve loading')
        self.assertEqual(self.confirm(target=('PAGE-1', 'AREA-1', 'loading', self.f.context_id))[0], 0)
        self.assertEqual(len(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))), 2)

    def test_cloned_project_cannot_reuse_another_repositorys_v2_acceptance(self):
        self.save()
        self.assertEqual(self.confirm()[0], 0)
        clone = self.f.repo.parent / 'clone'
        shutil.copytree(self.f.repo, clone)
        req = clone / self.f.req.relative_to(self.f.repo)
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, req, (req / 'UI设计.md').read_bytes().decode('utf-8'))
        self.assertTrue(checks.fails)

    def test_revoked_context_does_not_invalidate_an_independent_current_context(self):
        other = copy.deepcopy(self.f.index['screenshots'][0]['applies_to'][0])
        other.update(context_id='CTX-OTHER', relation='shared_layout')
        other['permissions']['data_id'] = 'DP02'
        self.f.index['screenshots'][0]['applies_to'].append(other)
        self.items.append({**copy.deepcopy(self.items[0]), 'context_id': 'CTX-OTHER',
                           'evidence_contexts': [{'evidence_id': 'P1', 'context_id': 'CTX-OTHER'}]})
        self.save()
        self.assertEqual(self.confirm()[0], 0)
        other_target = ('PAGE-1', 'AREA-1', 'default', 'CTX-OTHER')
        self.assertEqual(self.confirm(target=other_target)[0], 0)
        self.f.index['screenshots'][0]['applies_to'][0]['content_review']['status'] = 'not_applicable'
        text = self.save()
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text, selected_targets={other_target})
        self.assertEqual(checks.fails, [])
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text)
        self.assertTrue(checks.fails, 'full stage must still reject the revoked required context')

    def test_corrupt_existing_receipt_is_not_overwritten_or_reapproved(self):
        self.save()
        self.assertEqual(self.confirm()[0], 0)
        path = next((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'))
        path.write_bytes(b'broken existing receipt')
        rc, output, calls = self.confirm()
        self.assertEqual(rc, 3, output)
        self.assertEqual(calls, [])
        self.assertEqual(path.read_bytes(), b'broken existing receipt')

    def test_material_changed_after_receipt_io_cannot_be_reported_current_on_publish_or_recovery(self):
        original_index = copy.deepcopy(self.f.index)
        original = data.check_prototypes.__globals__['_read_beneath']
        for existing in (False, True):
            for mutation in ('image', 'context', 'html', 'intent'):
                with self.subTest(existing=existing, mutation=mutation):
                    for path in (self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json'):
                        path.unlink()
                    self.f.index = copy.deepcopy(original_index)
                    (self.f.material / 'function/FP01/data/DP01/P1.png').write_bytes(self.f.screenshot_bytes)
                    (self.f.req / self.f.path).write_bytes(self.f.html)
                    self.items[0]['change_intent'] = 'Add confirmation action'
                    self.save()
                    if existing:
                        self.assertEqual(self.confirm()[0], 0)
                    prepared = data.GATE['_prepare_prototype_v2'](
                        self.f.req, ('PAGE-1', 'AREA-1', 'default', self.f.context_id))
                    receipts = 0
                    def read_then_change(handle, path, limit):
                        nonlocal receipts
                        raw = original(handle, path, limit)
                        if str(path).split('/')[-1].startswith('v2-') and str(path).endswith('.json'):
                            receipts += 1
                            if receipts == 2:
                                if mutation == 'image':
                                    (self.f.material / 'function/FP01/data/DP01/P1.png').write_bytes(b'late image')
                                elif mutation == 'context':
                                    self.f.index['screenshots'][0]['applies_to'][0]['content_review']['status'] = 'not_applicable'
                                    self.save()
                                elif mutation == 'html':
                                    (self.f.req / self.f.path).write_bytes(b'late HTML')
                                else:
                                    self.items[0]['change_intent'] = 'late new intent'
                                    self.save()
                        return raw
                    with patch.dict(data.check_prototypes.__globals__, {'_read_beneath': read_then_change}):
                        rc, output, calls = self.confirm(prepared=prepared)
                    self.assertEqual(receipts, 2)
                    self.assertEqual(rc, 3, output)
                    self.assertEqual(len(calls), 0 if existing else 1)

    def test_v2_schema_rejects_self_approval_and_wrong_metadata_types(self):
        original = copy.deepcopy(self.items[0])
        changes = ({'status': 'ACCEPTED'}, {'reviewer': 'k'}, {'dependency_sha256': 'a' * 64},
            {'artifact_plan_id': {}}, {'gate_digest': int('1' * 64)}, {'feature_ids': ['F1', 'F1']},
            {'acceptance_ids': ['A1', 'A1']}, {'evidence_contexts': original['evidence_contexts'] * 2})
        for change in changes:
            with self.subTest(change=change):
                self.items[0] = {**copy.deepcopy(original), **change}
                text = self.save()
                checks = data.Checks('UI设计')
                data.check_prototypes(checks, self.f.req, text, require_confirmation=False)
                self.assertTrue(checks.fails)

    def test_actual_cli_pipe_cannot_supply_a_v2_human_decision(self):
        self.save()
        prepared = data.GATE['_prepare_prototype_v2'](
            self.f.req, ('PAGE-1', 'AREA-1', 'default', self.f.context_id))
        result = subprocess.run([sys.executable, str(ROOT / 'doc-gate'), 'prototype-confirm', str(self.f.req),
            '--schema-version', '2', '--page', 'PAGE-1', '--area', 'AREA-1', '--state', 'default',
            '--context', self.f.context_id, '--prepared-sha256', prepared['prepare_sha256'], '--reviewer', 'k'],
            input='ACCEPT anything\n', capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['per_item'][0]['status'], 'CONFLICT')
        self.assertEqual(list((self.f.req / 'reviews/ui-prototype-confirmations').glob('v2-*.json')), [])

    def test_baseline_added_or_started_after_receipt_io_invalidates_old_dependency(self):
        text = self.save()
        self.assertEqual(self.confirm()[0], 0)
        token = self.f.repo / 'frontend/theme.css'
        token.parent.mkdir()
        token.write_bytes(b'blue')
        baseline = {'schema_version': 2, 'sources': [{'id': 'SRC-1', 'kind': 'token',
            'path': 'frontend/theme.css', 'sha256': hashlib.sha256(b'blue').hexdigest(), 'certainty': 'source_backed'}],
            'regions': [{'page_id': 'PAGE-1', 'area_id': 'AREA-1', 'region_id': 'QUERY',
                'intent': 'PRESERVE', 'feature_ids': ['F1'], 'acceptance_ids': ['A1'], 'source_ids': ['SRC-1'],
                'screenshot_ids': ['P1'], 'rationale': '当前主题来源', 'adjacent_context': None}]}
        original = data.check_prototypes.__globals__['_read_beneath']
        for suffix in ('```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n', '```aek-ui-baseline\n'):
            with self.subTest(suffix=suffix):
                (self.f.req / 'UI设计.md').write_bytes(text.encode('utf-8'))
                changed = False
                def read_then_add(handle, path, limit):
                    nonlocal changed
                    raw = original(handle, path, limit)
                    if str(path).startswith('v2-') and not changed:
                        changed = True
                        (self.f.req / 'UI设计.md').write_bytes((text + '\n## 源码基线\n' + suffix).encode('utf-8'))
                    return raw
                checks, dependencies = data.Checks('UI设计'), {}
                with patch.dict(data.check_prototypes.__globals__, {'_read_beneath': read_then_add}):
                    data.check_prototypes(checks, self.f.req, text, dependency_out=dependencies)
                self.assertTrue(changed)
                self.assertTrue(checks.fails)
                self.assertEqual(dependencies, {})

    def test_v2_source_baseline_remains_usable_and_invalidates_only_when_dependencies_change(self):
        token, _ = self.f._add_source_baseline()
        self.save()
        self.assertEqual(self.confirm()[0], 0)
        path = self.f.req / 'UI设计.md'
        text = path.read_bytes().decode('utf-8') + '\n## 无关说明\n仅修改非依赖说明。\n'
        path.write_bytes(text.encode('utf-8'))
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text)
        self.assertEqual(checks.fails, [])
        token.write_bytes(b'red')
        checks = data.Checks('UI设计')
        data.check_prototypes(checks, self.f.req, text)
        self.assertTrue(checks.fails)

    def test_state_context_source_mappings_are_current_and_do_not_invalidate_unrelated_consumer(self):
        targets = self.add_context()
        frontend = self.f.repo / 'frontend'
        frontend.mkdir()
        sources, regions = [], []
        for number, target in enumerate(targets, 1):
            raw = f'theme-{number}'.encode()
            (frontend / f'theme{number}.css').write_bytes(raw)
            sources.append({'id': f'SRC-{number}', 'kind': 'token', 'path': f'frontend/theme{number}.css',
                'sha256': hashlib.sha256(raw).hexdigest(), 'certainty': 'source_backed'})
            regions.append({'page_id': target[0], 'area_id': target[1], 'state_id': target[2],
                'context_id': target[3], 'region_id': 'QUERY', 'intent': 'PRESERVE',
                'feature_ids': ['F1'], 'acceptance_ids': ['A1'], 'source_ids': [f'SRC-{number}'],
                'screenshot_ids': ['P1'], 'rationale': '当前权限实际组件来源', 'adjacent_context': None})
        self.f.ui_text += '\n## 设计基线\n\n```aek-ui-baseline\n' + json.dumps({
            'schema_version': 2, 'sources': sources, 'regions': regions}) + '\n```\n'
        self.save()
        rc, payload, _ = self.confirm_batch(targets)
        self.assertEqual(rc, 0, payload)
        (frontend / 'theme1.css').write_bytes(b'changed')
        checks, dependencies = data.Checks('UI设计'), {}
        data.check_prototypes(checks, self.f.req, self.save(), dependency_out=dependencies)
        self.assertTrue(checks.fails)
        self.assertEqual(set(dependencies), {targets[1]})
        rc, _, calls = self.confirm(target=targets[1])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], 'unrelated current receipt does not need a repeated decision')

    def test_default_and_loading_baselines_bind_actual_distinct_images_and_keep_local_invalidation(self):
        target = ('PAGE-1', 'AREA-1', 'default', self.f.context_id)
        loading = ('PAGE-1', 'AREA-1', 'loading', self.f.context_id)
        row = copy.deepcopy(self.f.index['screenshots'][0])
        row.update(id='P2', state='loading', path='function/FP01/data/DP01/P2-loading.png')
        self.f.index['screenshots'].append(row)
        (self.f.material / row['path']).write_bytes(self.f.screenshot_bytes)
        self.items.append({**copy.deepcopy(self.items[0]), 'state_id': 'loading', 'depends_on': ['P2'],
                           'evidence_contexts': [{'evidence_id': 'P2', 'context_id': self.f.context_id}]})
        region = {'page_id': 'PAGE-1', 'area_id': 'AREA-1', 'context_id': self.f.context_id,
            'region_id': 'QUERY', 'intent': 'PRESERVE', 'feature_ids': ['F1'], 'acceptance_ids': ['A1'],
            'source_ids': [], 'rationale': '当前状态截图', 'adjacent_context': None}
        baseline = {'schema_version': 2, 'sources': [], 'regions': [
            dict(region, state_id='default', screenshot_ids=['P1']),
            dict(region, state_id='loading', screenshot_ids=['P2'])]}
        self.f.ui_text += '\n## 设计基线\n\n```aek-ui-baseline\n' + json.dumps(baseline) + '\n```\n'
        text = self.save()
        row['prototype_refs'] = [copy.deepcopy(ref) for ref in self.f.index['screenshots'][0]['prototype_refs']
                                 if ref['state_id'] == 'loading']
        (self.f.material / '截图索引.json').write_text(json.dumps(self.f.index), encoding='utf-8')
        rc, payload, _ = self.confirm_batch([target, loading])
        self.assertEqual(rc, 0, payload)
        (self.f.material / row['path']).write_bytes(b'loading image changed')
        checks, dependencies = data.Checks('UI设计'), {}
        data.check_prototypes(checks, self.f.req, text, dependency_out=dependencies)
        self.assertTrue(checks.fails)
        self.assertEqual(set(dependencies), {target})
        rc, _, calls = self.confirm(target=target)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
