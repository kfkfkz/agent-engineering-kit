"""Explicit local terminal HostEventSource; imported JSON cannot mint events.

Only the trusted control-side composition root constructs this adapter and
supplies its private store, currentness checks and fixed provider plans. It
does not execute tools or expose a public source-event signing endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from aek.adapters import ui_filesystem as fs
from aek.adapters.security_authority import HostEventSource
from aek.adapters.security_evidence import source_binding
from aek.core.security.authority import (
    HostEvent, SecurityAuthorityError, canonical, digest, runtime_binding, timestamp, utc_now,
)


class _LiveEvent:
    __slots__ = ()


class _Attempt:
    __slots__ = ()


class _Completion:
    __slots__ = ()


class TerminalHostEventSource(HostEventSource):
    def __init__(self, *, state_root: Path, key_file: Path, clock=utc_now):
        self._root, self._key_file = Path(state_root).absolute(), Path(key_file).absolute()
        self._clock, self._tokens = clock, {}
        self._attempts, self._completions = {}, {}

    @staticmethod
    def _private(info, *, directory=False):
        return ((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                and info.st_uid == os.getuid() and not info.st_mode & 0o077
                and (directory or info.st_nlink == 1))

    @contextmanager
    def _store(self):
        # No guessed Windows ACL check or weaker chmod fallback. A qualified
        # Windows private-state provider can be supplied in a later profile.
        if os.name != 'posix':
            raise SecurityAuthorityError('HOST_PRIVATE_STORAGE_UNAVAILABLE')
        root = key_parent = None
        key_fd = None
        try:
            root = fs.open_absolute_dir(self._root)
            key_parent = fs.open_absolute_dir(self._key_file.parent)
            if not self._private(os.fstat(root), directory=True) or not self._private(os.fstat(key_parent), directory=True):
                raise SecurityAuthorityError('HOST_PRIVATE_STORAGE_UNAVAILABLE')
            key_fd = fs.open_file(key_parent, self._key_file.name, os.O_RDONLY)
            if not self._private(os.fstat(key_fd)):
                raise SecurityAuthorityError('HOST_PRIVATE_STORAGE_UNAVAILABLE')
            key = fs.read_fd(key_fd, 32)
            if len(key) != 32:
                raise SecurityAuthorityError('HOST_PRIVATE_STORAGE_UNAVAILABLE')
            yield root, key
        finally:
            if key_fd is not None:
                os.close(key_fd)
            if key_parent is not None:
                fs.close_dir(key_parent)
            if root is not None:
                fs.close_dir(root)

    def capabilities(self):
        try:
            with self._store():
                pass
        except (OSError, ValueError):
            return {'available': False, 'mode': 'unavailable'}
        return {'available': True, 'mode': 'local-terminal'}

    @staticmethod
    def _write_new(root, name, raw):
        temporary = '.host-' + uuid.uuid4().hex + '.tmp'
        fd = fs.open_file(root, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            try:
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError('short host-state write')
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            fs.rename_noreplace(root, temporary, root, name)
            fs.fsync_dir(root)
        finally:
            try:
                fs.unlink(root, temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_signed(root, key, name):
        value = json.loads(fs.read_at(root, name, 2_000_000))
        if (type(value) is not dict or set(value) != {'body', 'hmac_sha256'}
                or type(value['hmac_sha256']) is not str or not hmac.compare_digest(value['hmac_sha256'],
                    hmac.new(key, canonical(value['body']), hashlib.sha256).hexdigest())):
            raise SecurityAuthorityError('HOST_JOURNAL_INVALID')
        return value['body']

    def _write_signed(self, root, key, name, body):
        signed = {'body': body, 'hmac_sha256': hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}
        self._write_new(root, name, canonical(signed))

    def _publish(self, event, binding, plan, *, completion=None):
        body = {'schema_version': 1, 'event': asdict(event), 'binding': binding, 'plan': plan}
        if completion is not None:
            body.update(schema_version=2, **completion)
        with self._store() as (root, key), fs.exclusive_lock(root, timeout=5):
            self._write_signed(root, key, 'event-' + event.event_id + '.json', body)
            if completion is not None:
                self._write_signed(root, key, 'completion-' + completion['attempt_key'] + '.json', body)
        token = _LiveEvent()
        self._tokens[token] = event
        return token

    @staticmethod
    def _plan_matches(plan, binding):
        # Original ScannerPlan uses a domain-separated digest. Do not change
        # that contract to a generic JSON digest or grant a different budget.
        if binding.get('tool_id') in {'trivy', 'semgrep', 'gitleaks'}:
            from aek.core.security.scanners import load_scanner_plan
            original = load_scanner_plan(json.loads(canonical(plan)))
            return (original.plan_digest == binding['plan_digest'] and all(
                canonical(getattr(original, key)) == canonical(value)
                for key, value in binding.items() if key != 'operation'))
        return digest(plan) == binding.get('plan_digest')

    def approve_execution(self, authority, *, plan: dict, binding: dict, reviewer: str, revalidate):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SecurityAuthorityError('INTERACTIVE_TERMINAL_REQUIRED')
        if (authority.host_source is not self or not callable(revalidate)
                or type(reviewer) is not str or not reviewer.strip() or len(reviewer) > 120
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in reviewer)):
            raise SecurityAuthorityError()
        fields = {'plan_digest', 'repository_ref', 'scope_ref', 'identity_digest', 'tool_id',
                  'executable_digest', 'action', 'network_mode', 'write_scope', 'timeout_seconds',
                  'max_output_bytes', 'max_items'}
        if (type(plan) is not dict or type(binding) is not dict or set(binding) != fields | {'operation'}
                or binding != runtime_binding(**{key: binding[key] for key in fields})
                or not self._plan_matches(plan, binding) or binding['repository_ref'] != authority.repository_ref
                or binding['scope_ref'] != authority.scope_ref
                or binding['identity_digest'] != authority.current_identity.envelope_digest):
            raise SecurityAuthorityError()
        repo = Path(authority.repository_ref)
        if (not repo.is_absolute() or self._root.is_relative_to(repo)
                or self._key_file.is_relative_to(repo) or len(self._tokens) >= 256):
            raise SecurityAuthorityError('HOST_PRIVATE_STORAGE_UNAVAILABLE')
        if self.capabilities()['available'] is not True or revalidate() is not True:
            raise SecurityAuthorityError('HOST_PLAN_UNAVAILABLE')
        if (timestamp(authority.expires_at) - self._clock()).total_seconds() < binding['timeout_seconds']:
            raise SecurityAuthorityError('HOST_PLAN_EXPIRED')
        displayed_plan, displayed_binding = canonical(plan), canonical(binding)
        displayed_authority = (authority.repository_ref, authority.task_id, authority.scope_ref,
                               authority.current_identity.envelope_digest, authority.expires_at)
        print('固定执行计划（不是安全采用结论）；仅批准当前所列范围：')
        print(canonical({'plan': plan, 'binding': binding}).decode('utf-8'))
        phrase = input('确认执行请输入 RUN ' + binding['plan_digest'][:12] + '，否则输入 CANCEL：').strip()
        if phrase != 'RUN ' + binding['plan_digest'][:12]:
            raise SecurityAuthorityError('HOST_CONFIRMATION_CANCELLED')
        if (revalidate() is not True or canonical(plan) != displayed_plan or canonical(binding) != displayed_binding
                or displayed_authority != (authority.repository_ref, authority.task_id, authority.scope_ref,
                                          authority.current_identity.envelope_digest, authority.expires_at)):
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        now = self._clock()
        if (timestamp(authority.expires_at) - now).total_seconds() < binding['timeout_seconds']:
            raise SecurityAuthorityError('HOST_PLAN_EXPIRED')
        event = HostEvent(uuid.uuid4().hex, 'user-confirmation', reviewer, authority.task_id,
            authority.repository_ref, authority.scope_ref, authority.current_identity.envelope_digest,
            'execute', digest(binding), now.isoformat(), authority.expires_at)
        token = self._publish(event, binding, plan)
        return authority.on_user_confirmation(token, binding=binding)

    def _attempt_identity(self, authority, plan, binding):
        fields = {'plan_digest', 'repository_ref', 'scope_ref', 'identity_digest', 'tool_id',
                  'executable_digest', 'action', 'network_mode', 'write_scope', 'timeout_seconds',
                  'max_output_bytes', 'max_items'}
        if (authority.host_source is not self or type(plan) is not dict or type(binding) is not dict
                or set(binding) != fields | {'operation'}
                or binding != runtime_binding(**{key: binding[key] for key in fields})
                or not self._plan_matches(plan, binding) or binding['repository_ref'] != authority.repository_ref
                or binding['scope_ref'] != authority.scope_ref
                or binding['identity_digest'] != authority.current_identity.envelope_digest):
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        authority._active('source')
        return digest({'task_id': authority.task_id, 'binding': binding})

    def begin_attempt(self, authority, *, plan, binding, grant):
        """Control-side only. Persist intent before a fixed provider starts.

        A second caller cannot claim the same intent. A crash is UNKNOWN, not
        permission to replay; the existing WorkUnit remains authoritative.
        """
        attempt_key = self._attempt_identity(authority, plan, binding)
        authority.validate_runtime_grant(grant, **{k: v for k, v in binding.items() if k != 'operation'})
        caller = authority.validate(grant, operation='execute', binding=binding)
        receipt = authority.read_provenance(caller['provenance_ref'])
        if receipt is None or len(self._attempts) >= 256:
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        body = {'schema_version': 1, 'kind': 'started', 'attempt_key': attempt_key,
                'task_id': authority.task_id, 'plan': plan, 'binding': binding,
                'execution_provenance_ref': caller['provenance_ref'], 'execution_event': receipt['event']}
        frozen = json.loads(canonical(body))
        with self._store() as (root, key), fs.exclusive_lock(root, timeout=5):
            try:
                self._write_signed(root, key, 'attempt-' + attempt_key + '.json', frozen)
            except FileExistsError:
                raise SecurityAuthorityError('HOST_ATTEMPT_ALREADY_STARTED') from None
        attempt = _Attempt()
        self._attempts[attempt] = (frozen, grant)
        return attempt

    def complete_attempt(self, authority, attempt, *, metadata, facts, revalidate):
        """Only the composition-root provider completion path calls this.

        Imported JSON is not an attempt. The provider owns metadata/facts;
        callers cannot supply them through the ordinary read-only CLI/MCP.
        """
        if type(attempt) is not _Attempt:
            raise SecurityAuthorityError()
        entry = self._attempts.pop(attempt, None)
        if entry is None or not callable(revalidate) or revalidate() is not True:
            raise SecurityAuthorityError('HOST_COMPLETION_STALE')
        started, grant = entry
        plan, binding = started['plan'], started['binding']
        attempt_key = self._attempt_identity(authority, plan, binding)
        # Completion needs a live original grant, not an extra full execution
        # duration remaining. validate_runtime_grant is still required at start.
        caller = authority.validate(grant, operation='execute', binding=binding)
        if caller['provenance_ref'] != started['execution_provenance_ref']:
            raise SecurityAuthorityError()
        source = source_binding(metadata, facts)
        if (metadata['producer_ref'] != binding['tool_id'] or metadata['producer_ref'] == authority.author
                or metadata['scope_ref'] != authority.scope_ref
                or metadata['identity']['envelope_digest'] != authority.current_identity.envelope_digest
                or timestamp(metadata['checked_at']) > self._clock()):
            raise SecurityAuthorityError('HOST_COMPLETION_STALE')
        now = self._clock()
        event = HostEvent(uuid.uuid4().hex, 'tool-completed', metadata['producer_ref'], authority.task_id,
            authority.repository_ref, authority.scope_ref, authority.current_identity.envelope_digest,
            'source', digest(source), now.isoformat(), started['execution_event']['expires_at'])
        token = self._publish(event, source, plan, completion={
            'attempt_key': attempt_key, 'execution_binding': binding,
            'execution_event': started['execution_event'],
            'execution_provenance_ref': started['execution_provenance_ref'], 'metadata': metadata, 'facts': facts})
        return authority.on_source_completed(token, binding=source)

    def restore_attempt(self, authority, *, plan, binding, revalidate):
        attempt_key = self._attempt_identity(authority, plan, binding)
        if not callable(revalidate) or revalidate() is not True:
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        try:
            with self._store() as (root, key), fs.exclusive_lock(root, timeout=5):
                try:
                    started = self._read_signed(root, key, 'attempt-' + attempt_key + '.json')
                except FileNotFoundError:
                    return None
                if (type(started) is not dict or set(started) != {'schema_version', 'kind', 'attempt_key',
                        'task_id', 'plan', 'binding', 'execution_provenance_ref', 'execution_event'}
                        or started['schema_version'] != 1 or started['kind'] != 'started'
                        or started['attempt_key'] != attempt_key or started['task_id'] != authority.task_id
                        or started['plan'] != plan or started['binding'] != binding):
                    raise SecurityAuthorityError('HOST_JOURNAL_INVALID')
                execution = authority.read_provenance(started['execution_provenance_ref'])
                if execution is None or execution['binding'] != binding or execution['event'] != started['execution_event']:
                    raise SecurityAuthorityError('HOST_PLAN_STALE')
                try:
                    complete = self._read_signed(root, key, 'completion-' + attempt_key + '.json')
                except FileNotFoundError:
                    return {'status': 'UNKNOWN'}
            event = HostEvent(**complete['event'])
            if (complete['attempt_key'] != attempt_key or complete['plan'] != plan
                    or complete['execution_binding'] != binding or complete['execution_event'] != execution['event']
                    or complete['execution_provenance_ref'] != started['execution_provenance_ref']
                    or not self.restore(event) or revalidate() is not True):
                raise SecurityAuthorityError('HOST_COMPLETION_STALE')
            token = next((token for token, prior in self._completions.items()
                          if prior == complete), None)
            if token is None:
                if len(self._completions) >= 256:
                    raise SecurityAuthorityError('HOST_COMPLETION_STALE')
                token = _Completion()
                self._completions[token] = complete
            return {'status': 'COMPLETED', 'metadata': complete['metadata'], 'facts': complete['facts'],
                    'handle': None, 'completion_token': token}
        except (KeyError, TypeError):
            raise SecurityAuthorityError('HOST_JOURNAL_INVALID') from None

    def recover_source(self, authority, completion_token, *, revalidate):
        if type(completion_token) is not _Completion:
            raise SecurityAuthorityError()
        complete = self._completions.pop(completion_token, None)
        if complete is None or not callable(revalidate) or revalidate() is not True:
            raise SecurityAuthorityError('HOST_COMPLETION_STALE')
        self._attempt_identity(authority, complete['plan'], complete['execution_binding'])
        event = HostEvent(**complete['event'])
        if not self.restore(event):
            raise SecurityAuthorityError('HOST_COMPLETION_STALE')
        token = _LiveEvent()
        self._tokens[token] = event
        return authority.on_source_completed(token, binding=complete['binding'])

    def consume(self, kind, live_event):
        if type(live_event) is not _LiveEvent:
            raise SecurityAuthorityError()
        event = self._tokens.pop(live_event, None)
        if event is None or event.kind != kind or not self.restore(event):
            raise SecurityAuthorityError()
        return event

    def restore(self, event):
        try:
            if (type(event) is not HostEvent or not isinstance(event.event_id, str)
                    or len(event.event_id) != 32 or any(ch not in '0123456789abcdef' for ch in event.event_id)
                    or timestamp(event.expires_at) <= self._clock() or timestamp(event.occurred_at) > self._clock()):
                return False
            with self._store() as (root, key):
                try:
                    fs.stat_at(root, 'revoked-' + event.event_id + '.json')
                    return False
                except FileNotFoundError:
                    pass
                raw = fs.read_at(root, 'event-' + event.event_id + '.json', 2_000_000)
                value = json.loads(raw)
                if type(value) is not dict or set(value) != {'body', 'hmac_sha256'}:
                    return False
                body = value['body']
                common = {'schema_version', 'event', 'binding', 'plan'}
                extra = {'attempt_key', 'execution_binding', 'execution_event', 'execution_provenance_ref',
                         'metadata', 'facts'}
                if (type(body) is not dict or type(body.get('schema_version')) is not int
                        or body['schema_version'] not in (1, 2)
                        or set(body) != common | (extra if body['schema_version'] == 2 else set())
                        or body['event'] != asdict(event) or type(value['hmac_sha256']) is not str
                        or digest(body['binding']) != event.binding_digest):
                    return False
                if not hmac.compare_digest(value['hmac_sha256'],
                        hmac.new(key, canonical(body), hashlib.sha256).hexdigest()):
                    return False
                if body['schema_version'] == 1:
                    if not self._plan_matches(body['plan'], body['binding']):
                        return False
                else:
                    executed = HostEvent(**body['execution_event'])
                    if (event.kind != 'tool-completed' or event.operation != 'source'
                            or body['binding'] != source_binding(body['metadata'], body['facts'])
                            or body['metadata']['producer_ref'] != event.actor
                            or body['metadata']['scope_ref'] != event.scope_ref
                            or body['metadata']['identity']['envelope_digest'] != event.identity_digest
                            or not self._plan_matches(body['plan'], body['execution_binding'])
                            or executed.kind != 'user-confirmation' or executed.operation != 'execute'
                            or executed.binding_digest != digest(body['execution_binding'])
                            or (executed.task_id, executed.repository_ref, executed.scope_ref, executed.identity_digest)
                                != (event.task_id, event.repository_ref, event.scope_ref, event.identity_digest)
                            or timestamp(event.occurred_at) < timestamp(executed.occurred_at)
                            or timestamp(event.expires_at) > timestamp(executed.expires_at)
                            or not self.restore(executed)):
                        return False
                # Authentication is not freshness. Finish journal/key IO before
                # the final revocation and real-clock checks, without claiming
                # protection against changes after this bounded observation.
                try:
                    fs.stat_at(root, 'revoked-' + event.event_id + '.json')
                    return False
                except FileNotFoundError:
                    pass
                now = self._clock()
                return timestamp(event.occurred_at) <= now < timestamp(event.expires_at)
        except (OSError, TypeError, ValueError, KeyError):
            return False
