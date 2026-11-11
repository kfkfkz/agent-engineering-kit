"""Declared current consumption using the existing Review parser/owner model.

These are selection facts. An original Gate and its archived exact sources
must independently anchor them before any caller may preserve an approval.
"""
from __future__ import annotations

import hashlib
import json

from aek.application.review import _full_boundary_evidence
from aek.core.artifact.document_material import compare_material
from aek.core.review.incremental import plan_full_review


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def consumed_review_context(consumer, providers, supporting_context, *, consumer_sections=None):
    """Return actual selected upstream bodies; unknown scope keeps full input."""
    identity = consumption_identity(consumer, providers, consumer_sections=consumer_sections)
    if identity["status"] != "SCOPED":
        return identity, dict(supporting_context)
    context = {name: text for name, text in supporting_context.items()
               if not name.startswith(("upstream/", "consumed/upstream/"))}
    for row in identity["consumed_sections"]:
        # This role is distinct from an asserted frozen full-document hash.
        # Actual bodies remain bound in the original v2 input/receipt digest.
        name = f"consumed/upstream/{row['stage']}/{row['document']}#{row['heading']}"
        context[name] = row["text"]
    return identity, context


def consumption_identity(consumer, providers, *, consumer_sections=None):
    """Select upstream owner groups and global context, or explicitly FULL.

    Inputs are operation-local full ReviewMaterial values. No disk, persisted
    digest or author assertion is treated as an ownership/approval proof.
    """
    def full(reason):
        return {"schema_version": 2, "rule_version": "declared-consumption-v1", "status": "FULL",
                "reason_code": reason, "content_digest": None, "consumed_sections": [], "source_hashes": {}}

    materials = {consumer.snapshot.stage: consumer, **providers}
    if not providers:
        return full("NO_UPSTREAM_CONSUMPTION")
    if (consumer.snapshot.stage in providers or len(materials) > 16
            or any(stage != material.snapshot.stage for stage, material in materials.items())):
        return full("SOURCE_SET_CHANGED")
    evidence, sections, texts, owners, globals_, definitions = {}, {}, {}, {}, set(), {}
    for stage, material in sorted(materials.items()):
        snapshot = material.snapshot
        plan_full_review(snapshot)
        if not snapshot.parse_complete or snapshot.unknown_edges:
            return full("PARSER_OR_EDGE_GAP")
        documents = material.document_map()
        if (len(documents) > 16 or sum(len(text.encode("utf-8")) for text in documents.values()) > 16 * 1024 * 1024
                or len(snapshot.sections) > 4096
                or dict(snapshot.doc_hashes) != {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                                                 for name, text in documents.items()}):
            return full("SOURCE_SET_CHANGED")
        boundary = _full_boundary_evidence(snapshot, documents, {})
        if any(count != 1 for _name, count in boundary["document_markers"]):
            return full("UNKNOWN_SCOPE")
        section_map = material.section_map()
        if (set(section_map) != {section.key for section in snapshot.sections}
                or any("".join(section_map[section.key] for section in snapshot.sections if section.document == name) != text
                       for name, text in documents.items())):
            return full("SOURCE_SET_CHANGED")
        evidence[stage] = boundary
        for document, heading, owner in boundary["section_owners"]:
            owners[(stage, document, heading)] = (stage, owner) if owner is not None else None
        globals_.update((stage, *key) for key in boundary["global_sections"] if stage in providers)
        for section in snapshot.sections:
            key = (stage, *section.key)
            body = section_map[section.key]
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != section.digest:
                return full("SOURCE_SET_CHANGED")
            sections[key], texts[key] = section, body
            for defined in section.defines_ids:
                if defined in definitions:
                    return full("DUPLICATE_OWNER")
                definitions[defined] = key
    if any(ref not in definitions for section in sections.values() for ref in section.uses_ids):
        return full("UNTRUSTED_REFERENCE")
    groups = {}
    for key, owner in owners.items():
        if owner is not None:
            groups.setdefault(owner, set()).add(key)
    if len(groups) > 512:
        return full("SCOPE_LIMIT")
    selected = set(globals_)
    own_keys = {key for key in sections if key[0] == consumer.snapshot.stage}
    if consumer_sections is not None:
        requested = {(consumer.snapshot.stage, *key) for key in consumer_sections}
        if not requested <= own_keys:
            return full("UNKNOWN_REVIEW_TARGET")
        own_keys = requested | {(consumer.snapshot.stage, *key)
                                for key in evidence[consumer.snapshot.stage]["global_sections"]}
    pending = list(own_keys | globals_)
    seen = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        for ref in sections[key].uses_ids:
            provider = definitions[ref]
            related = groups.get(owners.get(provider), {provider})
            for member in related:
                if member[0] in providers:
                    selected.add(member)
                if member not in seen:
                    pending.append(member)
    rows = []
    for key in sorted(selected):
        stage, document, heading = key
        raw = texts[key].encode("utf-8")
        content = compare_material(raw, raw).content_sha256
        if content is None:
            return full("UNKNOWN_PROJECTION")
        rows.append({"stage": stage, "document": document, "heading": heading,
                     "content_digest": content, "text": texts[key]})
    topology = {stage: {"evidence": evidence[stage], "sections": [
        [section.document, section.heading, section.defines_ids, section.uses_ids]
        for section in material.snapshot.sections]} for stage, material in sorted(materials.items())}
    semantic = {"rule_version": "declared-consumption-v1", "boundary": topology,
                "selected": [{key: value for key, value in row.items() if key != "text"} for row in rows]}
    return {"schema_version": 2, "rule_version": "declared-consumption-v1", "status": "SCOPED",
            "reason_code": "CURRENT_DECLARED_CLOSURE", "content_digest": _digest(semantic),
            "consumed_sections": rows, "source_hashes": {
                stage: dict(material.snapshot.doc_hashes) for stage, material in sorted(providers.items())}}
