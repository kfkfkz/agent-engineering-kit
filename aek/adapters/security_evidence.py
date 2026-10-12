"""Immutable observations registered by authenticated live producer events.

This index stores content and provenance only. The SecurityRecord remains the
single current finding/lifecycle/resolution state. Imported data cannot register
a source; persisted entries need independent host receipt validation on restart.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
from typing import Protocol
from contextlib import nullcontext

from aek.adapters.atomic_file import AtomicFileStore
from aek.adapters.work_unit_store import _exclusive_lock
from aek.core.context.identity import IdentityState, compare_identity, decode_identity
from aek.core.security.authority import SecurityAuthorityError, canonical, digest, timestamp
from aek.core.security.record import _sources


@dataclass(frozen=True)
class SourceObservation:
    metadata_json: str
    facts_json: str
    provenance_ref: str

    @property
    def metadata(self) -> dict:
        return json.loads(self.metadata_json)

    @property
    def facts(self) -> dict:
        return json.loads(self.facts_json)


class SourceEvidencePort(Protocol):
    def lookup(self, ref: str) -> SourceObservation | None: ...


def _bounded_facts(facts: dict) -> None:
    if type(facts) is not dict:
        raise SecurityAuthorityError()
    canonical(facts)
    count = 0
    pending = [(facts, 0)]
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 10_000 or depth > 64:
            raise SecurityAuthorityError()
        if isinstance(value, dict):
            if any(not isinstance(k, str) or k.lower() in {"secret", "match", "raw_secret", "raw_match"}
                   for k in value):
                raise SecurityAuthorityError()
            pending.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            pending.extend((v, depth + 1) for v in value)


def source_binding(metadata: dict, facts: dict) -> dict:
    _sources([metadata])
    _bounded_facts(facts)
    return {"operation": "source", "metadata_digest": digest(metadata), "facts_digest": digest(facts)}


class BuiltinSourceEvidencePort:
    def __init__(self, authority, *, root: Path | None = None) -> None:
        self.authority = authority
        self._observations: dict[str, dict] = {}
        self._live_refs: set[str] = set()
        self._files = AtomicFileStore(root) if root is not None else None

    def register(self, metadata: dict, facts: dict, handle: object) -> SourceObservation:
        binding = source_binding(metadata, facts)
        caller = self.authority.validate(handle, operation="source", binding=binding)
        if (metadata["scope_ref"] != self.authority.scope_ref
                or metadata["producer_ref"] != caller["actor"]
                or timestamp(metadata["checked_at"]) > self.authority._clock()
                or compare_identity(decode_identity(metadata["identity"]), self.authority.current_identity,
                                    tuple(metadata["required_components"])).state != IdentityState.VALID):
            raise SecurityAuthorityError()
        ref = metadata["ref"]
        body = {"metadata": metadata, "facts": facts, "provenance_ref": caller["provenance_ref"]}
        raw = canonical(body)
        name = "source-" + digest(ref)
        lock = _exclusive_lock(self._files.root / (name + ".lock")) if self._files is not None else nullcontext()
        with lock:
            old = self._observations.get(ref)
            if self._files is not None:
                persisted = self._files.read(name + ".json")
                if persisted is not None and persisted != raw:
                    raise SecurityAuthorityError()
            if old is not None and canonical(old) != raw:
                raise SecurityAuthorityError()
            if self._files is not None:
                self._files.write(name + ".json", raw)
            self._observations[ref] = json.loads(raw)
            self._live_refs.add(ref)
        return self.read(ref)

    def lookup(self, ref: str) -> SourceObservation | None:
        if not isinstance(ref, str) or not ref or len(ref) > 2048:
            return None
        body = self._observations.get(ref)
        if body is None and self._files is not None:
            raw = self._files.read("source-" + digest(ref) + ".json")
            try:
                body = json.loads(raw) if raw is not None else None
            except (ValueError, TypeError):
                return None
        if body is None:
            return None
        try:
            if set(body) != {"metadata", "facts", "provenance_ref"}:
                return None
            metadata, facts = body["metadata"], body["facts"]
            binding = source_binding(metadata, facts)
            if metadata["ref"] != ref:
                return None
            receipt = self.authority.read_provenance(body["provenance_ref"], live=ref in self._live_refs,
                expected_identity_digest=metadata["identity"]["envelope_digest"])
            valid = (receipt is not None and receipt["event"]["operation"] == "source"
                     and receipt["binding"] == binding
                     and receipt["event"]["actor"] == metadata["producer_ref"])
            state = compare_identity(decode_identity(metadata["identity"]), self.authority.current_identity,
                                     tuple(metadata["required_components"])).state
            status = ("STALE" if state == IdentityState.STALE else
                      "INVALID" if state == IdentityState.INVALID else
                      "VERIFIED" if valid else "DECLARED")
            metadata = {**metadata, "status": status}
            return SourceObservation(canonical(metadata).decode(), canonical(facts).decode(), body["provenance_ref"])
        except (TypeError, ValueError, KeyError):
            return None

    def read(self, ref: str) -> SourceObservation:
        observation = self.lookup(ref)
        if observation is None:
            raise SecurityAuthorityError("EVIDENCE_INCOMPLETE")
        return observation
