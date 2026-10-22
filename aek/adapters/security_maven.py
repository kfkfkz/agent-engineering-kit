"""Read-only Maven 3.11.0 tree import; bytes and hashes never grant authority.

The controlled producer supplies the model closure and sidecar from the same
execution. Preparation does not run Maven or read its cache. Registration uses
the existing SourceEvidencePort and its non-serializable source handle.
"""
from __future__ import annotations
import hashlib
import json
import re

from aek.adapters.security_evidence import source_binding
from aek.core.security.dependencies import is_concrete_version
from aek.core.security.model import SecurityInputError, _inventories
from aek.core.security.record import _sources

MAX_BYTES = 2_000_000
MAX_OCCURRENCES = 128
MAX_DEPTH = 32
PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin"
_SCOPES = {"compile": "production", "runtime": "production", "test": "test",
           "provided": "provided", "system": "provided"}


def _text(value, *, empty=False):
    if (type(value) is not str or len(value) > 2048 or (not empty and not value)
            or any(ord(char) < 32 for char in value)):
        raise SecurityInputError()
    return value


def _sha(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SecurityInputError()
    return value


def _path(value):
    _text(value)
    if (value.startswith("/") or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise SecurityInputError()
    return value


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise SecurityInputError()
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise SecurityInputError()
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(SecurityInputError()))
    except (ValueError, UnicodeError, RecursionError):
        raise SecurityInputError() from None
    if type(value) is not dict:
        raise SecurityInputError()
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def prepare_maven_resolution(raw: bytes, sidecar_raw: bytes, *, model_inputs: dict[str, bytes],
                             metadata: dict) -> dict:
    """Prepare untrusted facts, including an old-schema RESOLVED inventory.

    COMPLETE describes only the sidecar's declared module/profile/scope/model
    closure; it has no trusted standing until original source registration.
    A dependency tree can never produce a SHIPPED inventory.
    """
    _sources([metadata])
    if metadata["kind"] != "inventory" or metadata["tool_version"] != "3.11.0":
        raise SecurityInputError()
    tree, sidecar = _json(raw), _json(sidecar_raw)
    required = {"schema_version", "producer", "module", "profiles", "scopes", "model_inputs",
                "model_complete", "profiles_complete", "tree_sha256", "scanner_refs", "declarations", "coverage_gaps"}
    if set(sidecar) != required or type(sidecar["schema_version"]) is not int or sidecar["schema_version"] != 2:
        raise SecurityInputError()
    producer = sidecar["producer"]
    if (type(producer) is not dict or set(producer) != {"coordinate", "version", "format", "tool_sha256"}
            or producer["coordinate"] != PLUGIN or producer["version"] != "3.11.0"
            or producer["format"] != "tree-json"):
        raise SecurityInputError()
    _sha(producer["tool_sha256"])
    if _sha(sidecar["tree_sha256"]) != _hash(raw):
        raise SecurityInputError()
    # A root module is a logical identity, never an opened filesystem path.
    module = "." if sidecar["module"] == "." else _path(sidecar["module"])
    for name in ("profiles", "scopes", "coverage_gaps"):
        rows = sidecar[name]
        if type(rows) is not list or len(rows) > 128 or len(set(map(str, rows))) != len(rows):
            raise SecurityInputError()
        for row in rows:
            _text(row)
    if (type(sidecar["model_complete"]) is not bool or type(sidecar["profiles_complete"]) is not bool
            or not sidecar["scopes"] or any(scope not in _SCOPES for scope in sidecar["scopes"])):
        raise SecurityInputError()
    models = sidecar["model_inputs"]
    if type(models) is not list or not 0 < len(models) <= 128 or type(model_inputs) is not dict:
        raise SecurityInputError()
    paths, total_bytes = set(), 0
    for model in models:
        if type(model) is not dict or set(model) != {"path", "role", "sha256"}:
            raise SecurityInputError()
        path = _path(model["path"])
        if (path in paths or type(model["role"]) is not str
                or model["role"] not in {"module", "parent", "bom", "settings", "profile"}
                or path not in model_inputs):
            raise SecurityInputError()
        paths.add(path)
        content = model_inputs[path]
        if type(content) is not bytes or len(content) > MAX_BYTES or _sha(model["sha256"]) != _hash(content):
            raise SecurityInputError()
        total_bytes += len(content)
        if total_bytes > MAX_BYTES:
            raise SecurityInputError()
    if set(model_inputs) != paths:
        raise SecurityInputError()
    mappings, declarations = sidecar["scanner_refs"], sidecar["declarations"]
    for rows in (mappings, declarations):
        if type(rows) is not dict or len(rows) > MAX_OCCURRENCES:
            raise SecurityInputError()
        for key in rows:
            if re.fullmatch(r"[0-9]+(?:/[0-9]+)*", key) is None or len(key) > 256:
                raise SecurityInputError()
    for row in mappings.values():
        if type(row) is not dict or set(row) != {"path", "package_id"}:
            raise SecurityInputError()
        _path(row["path"])
        _text(row["package_id"])
    for value in declarations.values():
        _text(value)
    gaps = list(sidecar["coverage_gaps"])
    if not sidecar["model_complete"]:
        gaps.append("MAVEN_MODEL_CLOSURE_UNKNOWN")
    if not sidecar["profiles_complete"]:
        gaps.append("MAVEN_ACTIVE_PROFILES_UNKNOWN")
    if not any(model["role"] == "module" for model in models):
        gaps.append("MAVEN_MODULE_MODEL_MISSING")
    context = _hash(_canonical({"producer": producer, "producer_ref": metadata["producer_ref"],
                                "scope_ref": metadata["scope_ref"], "identity_digest": metadata["identity"]["envelope_digest"],
                                "tree_sha256": _hash(raw), "module": module, "profiles": sidecar["profiles"],
                                "scopes": sidecar["scopes"], "model_inputs": models}))
    occurrences, packages, visited = [], [], set()
    allowed = {"groupId", "artifactId", "version", "type", "classifier", "scope", "optional", "children"}
    def visit(value, index, parents, ancestors=(), *, root=False):
        if type(value) is not dict or not {"groupId", "artifactId", "version", "type"} <= set(value):
            raise SecurityInputError()
        if len(parents) > MAX_DEPTH or len(occurrences) >= MAX_OCCURRENCES:
            raise SecurityInputError()
        for field in ("groupId", "artifactId", "version", "type"):
            _text(value[field])
        classifier = _text(value.get("classifier", ""), empty=True)
        coordinate = tuple(value[field] for field in ("groupId", "artifactId", "version", "type")) + (classifier,)
        scope = _text(value.get("scope", ""), empty=True)
        children = value.get("children", [])
        if type(children) is not list:
            raise SecurityInputError()
        if set(value) - allowed:
            gaps.append("MAVEN_NODE_FIELDS_UNSUPPORTED:" + (index or "root"))
        optional = value.get("optional", "false")
        if type(optional) not in {str, bool} or optional not in {"true", "false", True, False}:
            raise SecurityInputError()
        if not root:
            visited.add(index)
            if coordinate in ancestors:
                gaps.append("MAVEN_CYCLE_OR_REPEATED_ANCESTOR:" + index)
            resolved = value["version"] if is_concrete_version(value["version"]) else None
            if resolved is None:
                gaps.append("MAVEN_VERSION_UNRESOLVED:" + index)
            if scope not in _SCOPES or scope not in sidecar["scopes"]:
                gaps.append("MAVEN_SCOPE_UNKNOWN:" + index)
            if index not in mappings:
                gaps.append("MAVEN_SCANNER_MAPPING_MISSING:" + index)
            instance = "MVN-" + _hash(_canonical({"context": context, "path": index, "node":
                {key: value.get(key, "") for key in ("groupId", "artifactId", "version", "type", "classifier", "scope")}}))
            occurrence = {"instance_id": instance, "ecosystem": "maven", "package": value["groupId"] + ":" + value["artifactId"],
                "resolved_version": resolved, "declared_version": declarations.get(index), "module": module,
                "scope": scope, "classification": _SCOPES.get(scope), "resolution_context": context,
                "parent_path": parents, "scanner_ref": mappings.get(index), "type": value["type"], "classifier": classifier,
                "optional": optional is True or optional == "true"}
            occurrences.append(occurrence)
            packages.append({"id": instance, "ecosystem": "maven", "package": occurrence["package"],
                "version": resolved, "scope": _SCOPES.get(scope, "provided"),
                "relationship": "transitive" if parents else "direct", "introduced_by": parents[-1:],
                "dependency_path": parents + [instance] if parents else [], "source_ref": metadata["ref"]})
            parents = parents + [instance]
        for number, child in enumerate(children):
            visit(child, (index + "/" if index else "") + str(number), parents, ancestors + (coordinate,))
    visit(tree, "", [], root=True)
    if (set(mappings) | set(declarations)) - visited:
        raise SecurityInputError()
    gaps = sorted(set(gaps))
    inventory = {"id": "INV-" + _hash(_canonical({"source": metadata["ref"], "context": context, "tree": _hash(raw)})),
        "kind": "RESOLVED", "scope_ref": metadata["scope_ref"], "identity_digest": metadata["identity"]["envelope_digest"],
        "producer_ref": metadata["producer_ref"], "packages": packages,
        "coverage": "PARTIAL" if gaps else "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": gaps}
    _inventories([inventory])
    facts = {"schema_version": 2, "inventory": inventory, "producer": producer,
             "tree_sha256": _hash(raw), "sidecar_sha256": _hash(sidecar_raw), "module": module,
             "profiles": sidecar["profiles"], "scopes": sidecar["scopes"], "model_inputs": models,
             "occurrences": occurrences}
    source_binding(metadata, facts)  # Apply the original bounded-facts grammar; no handle is minted.
    return facts


def register_maven_resolution(raw: bytes, sidecar_raw: bytes, *, model_inputs: dict[str, bytes],
                              metadata: dict, source_port, handle: object):
    """Registration is deliberately the original source-port authority check."""
    facts = prepare_maven_resolution(raw, sidecar_raw, model_inputs=model_inputs, metadata=metadata)
    if (metadata["coverage"] != facts["inventory"]["coverage"]
            or metadata["coverage_gaps"] != facts["inventory"]["coverage_gaps"]):
        raise SecurityInputError()
    return source_port.register(metadata, facts, handle)
