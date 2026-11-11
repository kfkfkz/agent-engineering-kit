"""Versioned consumed-approval evidence; validation alone grants no approval."""
from __future__ import annotations

import re
from dataclasses import dataclass

from aek.core.artifact.registry import ARTIFACT_REGISTRY

_SHA = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class VerifiedConsumption:
    """Operation-local fact from recomputing the original approved slices."""
    binding_digest: str
    content_digest: str


def validate_dependency_identity(value):
    if (type(value) is not dict or set(value) != {"schema_version", "rule_version", "content_digest", "approved_sources"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 2
            or value["rule_version"] != "declared-consumption-v1"
            or type(value["content_digest"]) is not str or not _SHA.fullmatch(value["content_digest"])
            or type(value["approved_sources"]) is not dict or not value["approved_sources"]
            or len(value["approved_sources"]) > len(ARTIFACT_REGISTRY.groups)):
        raise ValueError("invalid dependency identity")
    for stage, source in value["approved_sources"].items():
        if (stage not in ARTIFACT_REGISTRY.groups or type(source) is not dict
                or set(source) != {"gate_ref", "doc_hashes"}
                or type(source["gate_ref"]) is not str or not _SHA.fullmatch(source["gate_ref"])
                or type(source["doc_hashes"]) is not dict or not source["doc_hashes"]):
            raise ValueError("invalid approved dependency source")
        allowed = {ARTIFACT_REGISTRY.artifacts[item].filename for item in ARTIFACT_REGISTRY.groups[stage].artifact_ids}
        if (not set(source["doc_hashes"]) <= allowed
                or any(type(digest) is not str or not _SHA.fullmatch(digest) for digest in source["doc_hashes"].values())):
            raise ValueError("invalid approved dependency documents")
    return value
