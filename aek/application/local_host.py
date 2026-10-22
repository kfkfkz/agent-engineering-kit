"""Control-side fixed-provider orchestration; data cannot grant authority.

No filesystem, subprocess, network, terminal or adapter imports live here.
The original host, authority and source ports own those boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from aek.core.security.authority import SecurityAuthorityError, canonical, digest


@dataclass(frozen=True)
class PreparedLocalTool:
    plan: dict
    binding: dict
    source_ref: str
    provider_data: object = None


@dataclass(frozen=True)
class LocalToolCompletion:
    metadata: dict
    facts: dict


class LocalToolHost:
    def __init__(self, *, host_source, authority, source_port, providers):
        self.host_source, self.authority, self.source_port = host_source, authority, source_port
        self._providers = dict(providers)

    def _prepare(self, data):
        if (type(data) is not dict or set(data) != {'profile_id', 'targets'}
                or type(data['profile_id']) is not str
                or re.fullmatch(r'[a-z][a-z0-9-]{0,99}', data['profile_id']) is None
                or type(data['targets']) is not list or not 1 <= len(data['targets']) <= 32
                or any(type(item) is not str or not item or len(item) > 500
                       or any(ord(ch) < 32 for ch in item) for item in data['targets'])
                or len(set(data['targets'])) != len(data['targets'])):
            raise SecurityAuthorityError('INVALID_LOCAL_HOST_REQUEST')
        provider = self._providers.get(data['profile_id'])
        if provider is None:
            return None
        # Constructor-selected control-side objects, not JSON-selected imports.
        if any(not callable(getattr(provider, key, None)) for key in ('prepare', 'revalidate', 'execute')):
            raise SecurityAuthorityError('PROVIDER_UNAVAILABLE')
        prepared = provider.prepare(json.loads(canonical(data)))
        if (type(prepared) is not PreparedLocalTool or type(prepared.plan) is not dict
                or type(prepared.binding) is not dict or type(prepared.source_ref) is not str
                or not prepared.source_ref or len(prepared.source_ref) > 2048
                or self.authority.host_source is not self.host_source
                or prepared.binding.get('repository_ref') != self.authority.repository_ref
                or prepared.binding.get('scope_ref') != self.authority.scope_ref
                or prepared.binding.get('identity_digest') != self.authority.current_identity.envelope_digest
                or prepared.binding.get('operation') != 'execute'):
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        snapshot = canonical({'plan': prepared.plan, 'binding': prepared.binding, 'source_ref': prepared.source_ref})
        authority_snapshot = (self.authority.repository_ref, self.authority.task_id, self.authority.scope_ref,
                              self.authority.current_identity.envelope_digest, self.authority.expires_at)
        def unchanged():
            return (snapshot == canonical({'plan': prepared.plan, 'binding': prepared.binding,
                                           'source_ref': prepared.source_ref})
                and authority_snapshot == (self.authority.repository_ref, self.authority.task_id,
                    self.authority.scope_ref, self.authority.current_identity.envelope_digest,
                    self.authority.expires_at))
        def revalidate():
            return unchanged() and provider.revalidate(prepared) is True and unchanged()
        if not revalidate():
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        return provider, prepared, revalidate, unchanged

    @staticmethod
    def _response(status, action, reason=None, **fields):
        return {'status': status, 'action': action, 'reason_codes': [] if reason is None else [reason], **fields}

    def prepare(self, data):
        value = self._prepare(data)
        if value is None:
            return self._response('UNAVAILABLE', 'not_started', 'PROVIDER_UNAVAILABLE')
        _, prepared, _revalidate, _unchanged = value
        return {'status': 'PREPARED', 'profile_id': data['profile_id'], 'source_ref': prepared.source_ref,
                'plan': json.loads(canonical(prepared.plan)), 'binding': json.loads(canonical(prepared.binding))}

    def _completion(self, prepared, metadata, facts):
        if (type(metadata) is not dict or type(facts) is not dict
                or metadata.get('ref') != prepared.source_ref
                or metadata.get('producer_ref') != prepared.binding['tool_id']
                or metadata.get('scope_ref') != self.authority.scope_ref
                or metadata.get('identity', {}).get('envelope_digest') != self.authority.current_identity.envelope_digest):
            raise SecurityAuthorityError('HOST_COMPLETION_STALE')
        # Deep copies prevent provider mutation during registration IO.
        return json.loads(canonical(metadata)), json.loads(canonical(facts))

    def _source_current(self, prepared, metadata, facts, observation, revalidate, unchanged):
        """Registration can succeed while its final read observes revocation.

        Consume that actual observation, finish provider IO, then re-read the
        original source and its provenance. None of these checks replay a tool
        or overwrite an existing source; failure remains explicitly incomplete.
        """
        try:
            expected_metadata = canonical({**metadata, 'status': 'VERIFIED'})
            expected_facts = canonical(facts)
            def matches(value):
                return (value is not None and canonical(value.metadata) == expected_metadata
                        and canonical(value.facts) == expected_facts)
            if not matches(observation) or revalidate() is not True:
                return False
            current = self.source_port.lookup(prepared.source_ref)
            if not matches(current):
                return False
            receipt = self.authority.read_provenance(current.provenance_ref,
                expected_identity_digest=metadata['identity']['envelope_digest'])
            binding = {'operation': 'source', 'metadata_digest': digest(metadata), 'facts_digest': digest(facts)}
            return (receipt is not None and receipt['binding'] == binding
                    and receipt['event']['kind'] == 'tool-completed'
                    and receipt['event']['operation'] == 'source'
                    and receipt['event']['actor'] == metadata['producer_ref']
                    # Finish all proof IO before the pure operation fence;
                    # provider revalidation here would reopen an IO window.
                    and unchanged())
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return False

    def execute(self, data, *, reviewer):
        value = self._prepare(data)
        if value is None:
            return self._response('UNAVAILABLE', 'not_started', 'PROVIDER_UNAVAILABLE')
        provider, prepared, revalidate, unchanged = value
        restored = self.host_source.restore_attempt(self.authority, plan=prepared.plan,
            binding=prepared.binding, revalidate=revalidate)
        existing = self.source_port.lookup(prepared.source_ref)
        if restored is not None:
            if restored.get('status') != 'COMPLETED':
                return self._response('INCOMPLETE', 'human', 'NATIVE_EXECUTION_RESULT_UNKNOWN')
            metadata, facts = self._completion(prepared, restored['metadata'], restored['facts'])
            if existing is not None:
                if not self._source_current(prepared, metadata, facts, existing, revalidate, unchanged):
                    return self._response('INCOMPLETE', 'human', 'HOST_COMPLETION_STALE')
                return self._response('COMPLETED', 'reuse', source_ref=prepared.source_ref)
            if revalidate() is not True:
                raise SecurityAuthorityError('HOST_PLAN_STALE')
            handle = self.host_source.recover_source(self.authority, restored['completion_token'],
                                                      revalidate=revalidate)
            try:
                registered = self.source_port.register(metadata, facts, handle)
            except Exception:
                return self._response('INCOMPLETE', 'human', 'SOURCE_REGISTRATION_INCOMPLETE')
            if not self._source_current(prepared, metadata, facts, registered, revalidate, unchanged):
                return self._response('INCOMPLETE', 'human', 'HOST_COMPLETION_STALE')
            return self._response('COMPLETED', 'recovered', source_ref=prepared.source_ref)
        if existing is not None:
            return self._response('INCOMPLETE', 'human', 'HOST_COMPLETION_UNKNOWN')
        grant = self.host_source.approve_execution(self.authority, plan=prepared.plan, binding=prepared.binding,
                                                    reviewer=reviewer, revalidate=revalidate)
        if revalidate() is not True:
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        attempt = self.host_source.begin_attempt(self.authority, plan=prepared.plan, binding=prepared.binding, grant=grant)
        try:
            result = provider.execute(prepared, grant, self.authority)
            if type(result) is not LocalToolCompletion or revalidate() is not True:
                raise SecurityAuthorityError('HOST_COMPLETION_STALE')
            metadata, facts = self._completion(prepared, result.metadata, result.facts)
            handle = self.host_source.complete_attempt(self.authority, attempt, metadata=metadata,
                                                        facts=facts, revalidate=revalidate)
        except Exception:
            return self._response('INCOMPLETE', 'human', 'NATIVE_COMPLETION_SOURCE_UNAVAILABLE')
        try:
            registered = self.source_port.register(metadata, facts, handle)
        except Exception:
            return self._response('INCOMPLETE', 'human', 'SOURCE_REGISTRATION_INCOMPLETE')
        if not self._source_current(prepared, metadata, facts, registered, revalidate, unchanged):
            return self._response('INCOMPLETE', 'human', 'HOST_COMPLETION_STALE')
        return self._response('COMPLETED', 'executed', source_ref=prepared.source_ref)
