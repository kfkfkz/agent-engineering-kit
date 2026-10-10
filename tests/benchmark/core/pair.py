"""Deterministic paired-run control/provenance checks, separate from outcomes."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from typing import Mapping

CONTROL_KEYS = ("budget", "cli", "cli_version", "configuration", "controller", "dependencies",
                "environment", "fixture", "input", "permissions", "provider", "scenario", "verifier")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HASH = re.compile(r"[a-f0-9]{64}")


def _hash(value) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


@dataclass(frozen=True)
class RunControls:
    agent: str
    model: str
    protocol: str
    aek_source_commit: str
    fingerprints: tuple[tuple[str, str], ...]
    observed_model_sha256: str | None

    def __post_init__(self):
        if (not isinstance(self.agent, str) or self.agent not in {"codex", "claude"}
                or not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+\[\]-]{0,127}", self.model)
                or not isinstance(self.protocol, str) or self.protocol not in {"openai_responses", "anthropic_messages"}
                or not isinstance(self.aek_source_commit, str)
                or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", self.aek_source_commit)
                or self.observed_model_sha256 is not None and not _hash(self.observed_model_sha256)):
            raise ValueError("RUN_CONTROLS_INVALID")
        if (self.agent, self.protocol) not in {("codex", "openai_responses"), ("claude", "anthropic_messages")}:
            raise ValueError("AGENT_PROTOCOL_INVALID")
        if (not isinstance(self.fingerprints, tuple) or len(self.fingerprints) != len(CONTROL_KEYS)
                or any(not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[0], str)
                       or not _hash(row[1]) for row in self.fingerprints)
                or set(dict(self.fingerprints)) != set(CONTROL_KEYS)):
            raise ValueError("RUN_FINGERPRINTS_INVALID")
        object.__setattr__(self, "fingerprints", tuple(sorted(self.fingerprints)))


@dataclass(frozen=True)
class RunManifest:
    experiment_id: str
    pair_id: str
    run_id: str
    task_id: str
    repetition: int
    variant: str
    order: int
    controls: RunControls
    storage_identities: tuple[str, str, str, str]  # workspace, home, cache, session (not paths)
    treatment_sha256: str
    isolation_sha256: str | None

    def __post_init__(self):
        if (any(not isinstance(value, str) or not _ID.fullmatch(value)
                for value in (self.experiment_id, self.pair_id, self.run_id, self.task_id))
                or type(self.repetition) is not int or not 0 < self.repetition <= 1000
                or not isinstance(self.variant, str) or self.variant not in {"vanilla", "aek"}
                or type(self.order) is not int or self.order not in {0, 1}
                or not isinstance(self.controls, RunControls)
                or not isinstance(self.storage_identities, tuple) or len(self.storage_identities) != 4
                or any(not _hash(value) for value in self.storage_identities)
                or len(set(self.storage_identities)) != 4
                or not _hash(self.treatment_sha256)
                or self.isolation_sha256 is not None and not _hash(self.isolation_sha256)):
            raise ValueError("RUN_MANIFEST_INVALID")

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def manifest_to_json(manifest: RunManifest) -> bytes:
    if not isinstance(manifest, RunManifest):
        raise ValueError("RUN_MANIFEST_INVALID")
    return json.dumps({"schema_version": 1, "manifest": asdict(manifest), "manifest_sha256": manifest.digest},
                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def manifest_from_json(raw: bytes) -> RunManifest:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError("nonfinite")

    try:
        if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
            raise ValueError("size")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "manifest", "manifest_sha256"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError("schema")
        payload = value["manifest"]
        if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(RunManifest)}:
            raise ValueError("manifest fields")
        controls = payload["controls"]
        if (not isinstance(controls, dict) or set(controls) != {field.name for field in fields(RunControls)}
                or not isinstance(controls["fingerprints"], list)
                or any(not isinstance(row, list) for row in controls["fingerprints"])
                or not isinstance(payload["storage_identities"], list)):
            raise ValueError("control fields")
        controls["fingerprints"] = tuple(tuple(row) for row in controls["fingerprints"])
        payload["controls"] = RunControls(**controls)
        payload["storage_identities"] = tuple(payload["storage_identities"])
        manifest = RunManifest(**payload)
        if not _hash(value["manifest_sha256"]) or value["manifest_sha256"] != manifest.digest:
            raise ValueError("digest")
        return manifest
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError) as exc:
        raise ValueError("MANIFEST_JSON_INVALID") from exc


@dataclass(frozen=True)
class PairValidity:
    status: str
    reason_codes: tuple[str, ...]
    scope: str = "manifest_identity_and_provenance"
    outcome_evaluated: bool = False


@dataclass(frozen=True)
class ScheduledPair:
    experiment_id: str
    pair_id: str
    task_id: str
    repetition: int
    variants: tuple[str, str]
    run_ids: tuple[str, str]


def schedule_pairs(experiment_id: str, task_ids: tuple[str, ...], *, repeat: int,
                   first_variant: str = "vanilla") -> tuple[ScheduledPair, ...]:
    if (not isinstance(experiment_id, str) or not _ID.fullmatch(experiment_id)
            or not isinstance(task_ids, tuple) or not task_ids or len(task_ids) > 64
            or any(not isinstance(task, str) or not _ID.fullmatch(task) for task in task_ids)
            or len(set(task_ids)) != len(task_ids)
            or type(repeat) is not int or not 0 < repeat <= 1000 or repeat * len(task_ids) > 1024
            or not isinstance(first_variant, str) or first_variant not in {"vanilla", "aek"}):
        raise ValueError("PAIR_SCHEDULE_INVALID")
    other = "aek" if first_variant == "vanilla" else "vanilla"
    planned = []
    for task_index, task in enumerate(task_ids):
        for repetition in range(1, repeat + 1):
            identity = json.dumps({"experiment": experiment_id, "task": task, "repetition": repetition},
                                  sort_keys=True, separators=(",", ":")).encode("utf-8")
            pair_id = "PAIR-" + hashlib.sha256(identity).hexdigest()
            variants = ((first_variant, other) if (task_index + repetition - 1) % 2 == 0
                        else (other, first_variant))
            run_ids = tuple("RUN-" + hashlib.sha256(f"{pair_id}:{variant}".encode("utf-8")).hexdigest()
                            for variant in variants)
            planned.append(ScheduledPair(experiment_id, pair_id, task, repetition, variants, run_ids))
    return tuple(planned)


def validate_pair(left: RunManifest, right: RunManifest, *,
                  trusted_manifest_digests: Mapping[str, str] | None = None,
                  trusted_isolation_digests: frozenset[str] | None = None,
                  expected_treatments: Mapping[str, str] | None = None) -> PairValidity:
    if not isinstance(left, RunManifest) or not isinstance(right, RunManifest):
        raise ValueError("PAIR_INPUT_INVALID")
    invalid, unknown = [], []
    if (left.experiment_id, left.pair_id, left.task_id, left.repetition) != (
            right.experiment_id, right.pair_id, right.task_id, right.repetition):
        invalid.append("PAIR_SUBJECT_MISMATCH")
    if left.run_id == right.run_id or {left.variant, right.variant} != {"vanilla", "aek"}:
        invalid.append("PAIR_MEMBERS_INVALID")
    if {left.order, right.order} != {0, 1}:
        invalid.append("PAIR_ORDER_INVALID")
    if left.controls != right.controls:
        invalid.append("CONTROL_CONDITIONS_MISMATCH")
    if left.treatment_sha256 == right.treatment_sha256:
        invalid.append("TREATMENT_NOT_DISTINCT")
    if set(left.storage_identities) & set(right.storage_identities):
        invalid.append("SHARED_RUNTIME_STORAGE")
    if left.controls.observed_model_sha256 is None or right.controls.observed_model_sha256 is None:
        unknown.append("RESOLVED_MODEL_UNOBSERVED")
    for manifest in (left, right):
        if trusted_manifest_digests is None or manifest.run_id not in trusted_manifest_digests:
            unknown.append("MANIFEST_ANCHOR_MISSING")
        elif trusted_manifest_digests[manifest.run_id] != manifest.digest:
            invalid.append("MANIFEST_ANCHOR_MISMATCH")
        if (manifest.isolation_sha256 is None or trusted_isolation_digests is None
                or manifest.isolation_sha256 not in trusted_isolation_digests):
            unknown.append("ISOLATION_UNPROVEN")
        if expected_treatments is None or manifest.variant not in expected_treatments:
            unknown.append("TREATMENT_PLAN_MISSING")
        elif expected_treatments[manifest.variant] != manifest.treatment_sha256:
            invalid.append("UNDECLARED_TREATMENT")
    reasons = tuple(dict.fromkeys((*invalid, *unknown)))
    return PairValidity("INVALID" if invalid else "NOT_EVALUATED" if unknown else "VALID", reasons)
