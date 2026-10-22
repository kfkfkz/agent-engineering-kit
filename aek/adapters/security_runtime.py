"""Control-side runtime adoption bridge; neither a grant nor an authority.

The original source port authenticates metadata bindings. This bridge consumes
those current sources and the original dependency evaluator on every call.
Runtime descriptors, request JSON and a successful version probe cannot grant
adoption. The caller supplies a real, current-byte runtime fingerprint; no
default callback, public request operation or state write is provided.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path

from aek.core.context.identity import IdentityState, compare_identity
from aek.core.security.authority import canonical, digest, timestamp
from aek.core.security.dependencies import evaluate_design_dependency, is_concrete_version
from aek.core.security.model import MAX_SECURITY_BYTES, _proposals
from aek.core.security.policy import SecurityPolicy


class BoundRuntimeAdoption:
    def __init__(self, authority, source_port, *, proposals, source_refs,
                 required_components, runtime_sha256, runtime_fingerprint,
                 clock, max_age_days=7):
        if (type(proposals) is not tuple or not 1 <= len(proposals) <= 32
                or type(source_refs) is not tuple or not 1 <= len(source_refs) <= 128
                or any(type(ref) is not str or not ref or len(ref) > 2048
                       or any(ord(ch) < 32 or ord(ch) == 127 for ch in ref) for ref in source_refs)
                or len(set(source_refs)) != len(source_refs)
                or type(required_components) is not tuple or not required_components
                or any(type(key) is not str for key in required_components)
                or len(set(required_components)) != len(required_components)
                or type(runtime_sha256) is not str or re.fullmatch('[a-f0-9]{64}', runtime_sha256) is None
                or not callable(runtime_fingerprint) or not callable(clock)
                or getattr(source_port, 'authority', None) is not authority
                or getattr(authority, '_clock', None) is not clock):
            raise ValueError('INVALID_RUNTIME_ADOPTION_CONFIG')
        _proposals(list(proposals))
        if (any(not is_concrete_version(proposal['version'])
                or not set(proposal['evidence_refs']) <= set(source_refs) for proposal in proposals)
                or compare_identity(authority.current_identity, authority.current_identity,
                    required_components).state != IdentityState.VALID):
            raise ValueError('INVALID_RUNTIME_ADOPTION_CONFIG')
        # Reuse the original freshness policy validation, not a second policy.
        SecurityPolicy(max_advisory_age_days=max_age_days)
        timestamp(authority.expires_at)
        self._authority, self._source_port = authority, source_port
        self._proposals = canonical(proposals)
        self._source_refs, self._required_components = source_refs, required_components
        self._runtime_sha256, self._fingerprint = runtime_sha256, runtime_fingerprint
        self._clock, self._max_age_days = clock, max_age_days
        self._bound = self._snapshot()

    def _snapshot(self):
        authority = self._authority
        return (authority.repository_ref, authority.task_id, authority.scope_ref,
                authority.current_identity, authority.author, authority.expires_at,
                id(authority.host_source), id(authority._clock), id(authority),
                id(self._source_port), id(self._source_port.authority),
                self._proposals, self._source_refs, self._required_components,
                self._runtime_sha256, id(self._fingerprint), id(self._clock), self._max_age_days)

    def _observations(self):
        observations = []
        total_bytes = 2  # JSON array brackets; include commas as rows arrive.
        for ref in self._source_refs:
            item = self._source_port.lookup(ref)
            if item is None or item.metadata['status'] != 'VERIFIED':
                raise ValueError('RUNTIME_SOURCE_UNAVAILABLE')
            metadata, facts = item.metadata, item.facts
            observation = {'metadata': metadata, 'facts': facts,
                           'provenance_ref': item.provenance_ref}
            total_bytes += len(canonical(observation)) + bool(observations)
            if total_bytes > MAX_SECURITY_BYTES:
                raise ValueError('RUNTIME_SOURCE_BYTES_EXCEEDED')
            receipt = self._authority.read_provenance(item.provenance_ref,
                expected_identity_digest=metadata['identity']['envelope_digest'])
            if (receipt is None or receipt['event']['kind'] != 'tool-completed'
                    or receipt['event']['operation'] != 'source'
                    or receipt['event']['actor'] != metadata['producer_ref']
                    or receipt['binding']['operation'] != 'source'
                    or receipt['binding']['facts_digest'] != digest(facts)):
                raise ValueError('RUNTIME_SOURCE_UNAVAILABLE')
            observations.append(observation)
        return tuple(observations)

    def _adoptable(self, observations, now):
        return all(evaluate_design_dependency(proposal, observations,
            current_identity=self._authority.current_identity,
            required_components=self._required_components,
            scope_ref=self._authority.scope_ref, now=now,
            max_age_days=self._max_age_days)['status'] == 'ADOPTABLE'
            for proposal in json.loads(self._proposals))

    @staticmethod
    def _runtime_snapshot(runtime):
        # Controller-owned primitive descriptors, including the existing
        # LinuxRuntime dataclass. This is only a mutation fence, never proof
        # of the currently loaded bytes; the mandatory callback supplies that.
        value = (str(runtime) if isinstance(runtime, Path) else
                 asdict(runtime) if is_dataclass(runtime) and not isinstance(runtime, type) else runtime)
        return type(runtime), canonical(value)

    def __call__(self, runtime):
        """Return a bounded adoption observation, not a lifetime permission.

        Sources and bytes may change after their observation point. Consumers
        must still validate the original grant and current sources and pin the
        expected runtime bytes for the actual operation. Additional read passes
        would not make independent filesystem writers an atomic transaction.
        """
        try:
            authority = self._authority
            runtime_snapshot = self._runtime_snapshot(runtime)
            if (self._snapshot() != self._bound or self._source_port.authority is not authority
                    or self._fingerprint(runtime) != self._runtime_sha256):
                return False
            now = self._clock()
            if timestamp(authority.expires_at) <= now:
                return False
            observations = self._observations()
            if not self._adoptable(observations, now):
                return False
            if self._fingerprint(runtime) != self._runtime_sha256:
                return False
            # Fingerprinting itself performs IO; do not trust the earlier
            # source pass across it. Re-read the original ports afterwards.
            if canonical(self._observations()) != canonical(observations):
                return False
            final_now = self._clock()
            # No provider/proof IO after this pure control-context fence.
            return (final_now >= now and timestamp(authority.expires_at) > final_now
                    and self._adoptable(observations, final_now)
                    and self._snapshot() == self._bound
                    and self._runtime_snapshot(runtime) == runtime_snapshot)
        except Exception:
            # Measurement/source/clock adapters may fail in platform-specific
            # ways. Failure never yields permission; process interrupts still
            # propagate because they are not Exception subclasses.
            return False
