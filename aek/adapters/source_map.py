"""Operation-local full external sources and their actual selected slices.

No source is rewritten. COMPLETE_BOUNDARY describes the captured declaration
model; it is not a review, current upstream credential or execution proof.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from aek.adapters import ui_filesystem as fs
from aek.adapters.document_layout import package_directory, resolve_package_at
from aek.adapters.review_markdown import parse_review_documents, extract_trace_ids, _HEADING as _REVIEW_HEADING
from aek.core.artifact.document_material import MAX_DOCUMENT_BYTES
from aek.core.artifact.document_layout import relative_parts
from aek.core.artifact.source_map import SourceMap, SourceMapEpoch, load_source_map, source_map_digest, validate_source_map_epoch
from aek.core.artifact.registry import ARTIFACT_REGISTRY


@dataclass(frozen=True)
class SourceLocation:
    artifact_id: str
    category: str
    label: str
    path: str
    start: int
    end: int
    sha256: str


@dataclass(frozen=True)
class MappedSources:
    package_revision: str
    repository_identity: str
    mapping_digest: str
    artifact_ids: tuple[str, ...]
    _sources: tuple[tuple[str, bytes], ...]
    _contracts: tuple[tuple[str, bytes], ...]
    locations: tuple[SourceLocation, ...]
    status: str
    gaps: tuple[str, ...]

    def sources(self):
        return dict(self._sources)

    def source_hashes(self):
        return {name: hashlib.sha256(raw).hexdigest() for name, raw in self._sources}

    def contracts(self):
        return dict(self._contracts)


def _read_source(root, path):
    parts = path.split('/')
    with package_directory(root, '/'.join(parts[:-1])) as parent:
        fd = fs.open_file(parent, parts[-1], os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        try:
            identity = os.fstat(fd)
            raw = fs.read_fd(fd, MAX_DOCUMENT_BYTES)
            if not raw:
                raise ValueError('MISSING_ARTIFACT_CONTENT: empty mapped source')
            raw.decode('utf-8', errors='strict')
            return raw, (identity.st_dev, identity.st_ino)
        finally:
            os.close(fd)


def read_composed_source(package_dir: Path, source_map: SourceMap, name: str) -> bytes:
    """Read an exact physical source allowed by the active plan's mapping."""
    source_map_digest(source_map)
    if not any(name in artifact['sources'] for artifact in source_map.artifacts):
        raise ValueError('UNTRUSTED_MAPPING: source is outside the composed plan')
    repository = package_dir.absolute()
    for part in reversed(relative_parts(source_map.package_ref)):
        if repository.name != part:
            raise ValueError('ROOT_MISMATCH: source map does not belong to the locked package')
        repository = repository.parent
    root = fs.open_absolute_dir(repository)
    try:
        package = resolve_package_at(root, source_map.package_ref)
        if package.design_root != source_map.package_ref:
            raise ValueError('ROOT_MISMATCH: expected the registered design root')
        raw, _identity = _read_source(root, name)
        return raw
    finally:
        fs.close_dir(root)


def _selected(raw, sections, line_offsets, location):
    if 'heading' in location:
        matches = sections.get(location['heading'], ())
        if len(matches) != 1:
            raise ValueError('MISSING_ARTIFACT_CONTENT: selected source heading is missing or ambiguous')
        start, end = matches[0]
    else:
        first, last = location['lines']
        if last >= len(line_offsets):
            raise ValueError('MISSING_ARTIFACT_CONTENT: selected source lines do not exist')
        start, end = line_offsets[first - 1], line_offsets[last]
    selected = raw[start:end]
    if not selected.strip():
        raise ValueError('MISSING_ARTIFACT_CONTENT: selected source slice is empty')
    digest = hashlib.sha256(selected).hexdigest()
    if 'sha256' in location and location['sha256'] != digest:
        raise ValueError('SOURCE_CHANGED: actual selected bytes differ from the requested digest')
    return start, end, digest


def capture_mapped_sources(repo: Path, source_map: SourceMap, artifact_ids: tuple[str, ...], *, external_ids=()):
    if not isinstance(source_map, SourceMap):
        raise ValueError('UNTRUSTED_MAPPING: validated source map required')
    source_map = load_source_map(source_map._bytes)
    artifacts = {item['artifact_id']: item for item in source_map.artifacts}
    if (type(artifact_ids) is not tuple or not artifact_ids or len(set(artifact_ids)) != len(artifact_ids)
            or any(ident not in artifacts for ident in artifact_ids)
            or type(external_ids) is not tuple or any(type(ident) is not str or not re.fullmatch(
                r'(?:[AFDT][1-9][0-9]*|TC-[1-9][0-9]*|PAGE-[1-9][0-9]*)', ident) for ident in external_ids)):
        raise ValueError('UNTRUSTED_MAPPING: invalid controller-selected artifact scope')
    names = sorted({path for ident in artifact_ids for path in artifacts[ident]['sources']})
    if len(names) > 16:
        raise ValueError('UNTRUSTED_MAPPING: current stage has more than 16 physical files')
    root = fs.open_absolute_dir(Path(repo).absolute())
    try:
        package = resolve_package_at(root, source_map.package_ref)
        root_identity = fs.directory_identity(root)
        sources, contracts, identities, gaps = {}, {}, {}, []
        for binding in source_map.capability_bindings:
            if binding['artifact_id'] not in artifact_ids:
                continue
            path = binding['contract_path']
            raw = contracts.get(path)
            if raw is None:
                raw, _identity = _read_source(root, path)
                contracts[path] = raw
            if hashlib.sha256(raw).hexdigest() != binding['contract_sha256']:
                raise ValueError('CAPABILITY_CONTRACT_CHANGED: current configured contract differs from source mapping')
        total = 0
        for name in names:
            raw, identity = _read_source(root, name)
            total += len(raw)
            if total > 16 * 1024 * 1024:
                raise ValueError('UNTRUSTED_MAPPING: current source set exceeds 16 MiB')
            if identity[1]:
                if identity in identities:
                    raise ValueError('AMBIGUOUS_SOURCE_PATH: distinct paths refer to the same physical file')
                identities[identity] = name
            else:
                gaps.append('FILE_IDENTITY_UNAVAILABLE:' + name)
            sources[name] = raw
        boundaries = {}
        # Unopened provider declarations are not evidence of actual IDs. The
        # controller may supply IDs from separately verified upstream inputs.
        known = set(external_ids) | {point['id'] for artifact in artifacts.values()
                                    if artifact['artifact_id'] in artifact_ids for point in artifact['points']}
        for path, raw in sources.items():
            material = parse_review_documents('source-map', {path: raw.decode('utf-8', errors='strict')}, '0' * 64)
            if not material.snapshot.parse_complete or material.snapshot.unknown_edges:
                gaps.append('PARSER_OR_EDGE_GAP:' + path)
            offsets, offset, stack = {}, 0, []
            for document, heading, text in material.section_texts:
                body = text.encode('utf-8')
                if document != path or raw[offset:offset + len(body)] != body:
                    raise ValueError('SOURCE_CHANGED: parsed source sections do not reconstruct the captured bytes')
                # Review's cache gives duplicate headings unique diagnostic
                # keys. Source selectors must retain the actual original
                # heading spelling and ambiguity, not inherit those aliases.
                match = _REVIEW_HEADING.match(text.splitlines(keepends=True)[0]) if text else None
                if match is not None:
                    level, title = len(match.group(1)), match.group(2).strip()
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, title))
                    heading = ' / '.join(item[1] for item in stack)
                offsets.setdefault(heading, []).append((offset, offset + len(body)))
                offset += len(body)
            if offset != len(raw):
                raise ValueError('SOURCE_CHANGED: parsed source does not cover its captured full bytes')
            line_offsets = [0]
            for line in raw.splitlines(keepends=True):
                line_offsets.append(line_offsets[-1] + len(line))
            boundaries[path] = offsets, tuple(line_offsets)
            unknown = set(extract_trace_ids(raw.decode('utf-8'))) - known
            if unknown:
                gaps.append('UNKNOWN_REFERENCE:' + path)
        locations, coverage = [], {name: [] for name in names}
        for artifact in artifacts.values():
            for category in ('sections', 'points', 'globals'):
                for location in artifact[category]:
                    path = location['path']
                    if path not in sources:
                        continue
                    start, end, digest = _selected(sources[path], *boundaries[path], location)
                    coverage[path].append((start, end))
                    label = location.get('id', location.get('section', location.get('heading', str(location.get('lines')))))
                    locations.append(SourceLocation(artifact['artifact_id'], category, label, path, start, end, digest))
                    if artifact['artifact_id'] not in artifact_ids:
                        gaps.append('CROSS_ROLE_CONTENT:' + path)
                    refs = set(location.get('uses', [])) | set(location.get('covers', []))
                    if refs - known:
                        gaps.append('UNKNOWN_REFERENCE:' + path)
        for path, ranges in coverage.items():
            covered = 0
            for start, end in sorted(ranges):
                if start > covered and sources[path][covered:start].strip():
                    gaps.append('UNMAPPED_CONTENT:' + path)
                covered = max(covered, end)
            if sources[path][covered:].strip():
                gaps.append('UNMAPPED_CONTENT:' + path)
        # Identity and proofs refer to the original full bytes, not another
        # read taken during parsing. Only this final fence reads them again.
        if (resolve_package_at(root, source_map.package_ref) != package
                or fs.directory_identity(root) != root_identity
                or any(_read_source(root, name)[0] != raw for name, raw in sources.items())
                or any(_read_source(root, name)[0] != raw for name, raw in contracts.items())):
            raise ValueError('SOURCE_CHANGED: mapped source or package changed before returning evidence')
        return MappedSources(package.revision, root_identity, source_map_digest(source_map), tuple(sorted(artifact_ids)),
            tuple(sorted(sources.items())), tuple(sorted(contracts.items())), tuple(locations),
            'FULL_REQUIRED' if gaps else 'COMPLETE_BOUNDARY',
            tuple(sorted(set(gaps))))
    finally:
        fs.close_dir(root)


def mapped_review_input(captured: MappedSources, source_map: SourceMap, stage: str, dependency_fingerprint: str,
                        *, supporting_context=None, native_sources=None):
    """Deliver the first full input through the existing Review factory.

    Original physical paths and exact full bytes remain the review documents.
    Logical IDs are visibly attributed to the mapping context, never inserted
    into third-party Markdown. This factory grants no writer/use/approval.
    """
    from aek.application.review import full_review_request_v2

    if not isinstance(captured, MappedSources) or captured.mapping_digest != source_map_digest(source_map):
        raise ValueError('SOURCE_CHANGED: captured source topology differs from review mapping')
    checked = load_source_map(source_map._bytes)
    names = {path for artifact in checked.artifacts if artifact['artifact_id'] in captured.artifact_ids
             for path in artifact['sources']}
    if names != set(captured.sources()):
        raise ValueError('SOURCE_CHANGED: full review source set differs from source topology')
    native_sources = dict(native_sources or {})
    group = ARTIFACT_REGISTRY.get_group(stage)
    native_names = {ARTIFACT_REGISTRY.get_artifact(role).filename for role in group.artifact_ids
                    if role not in captured.artifact_ids}
    if (not set(captured.artifact_ids) <= set(group.artifact_ids)
            or not native_sources.keys() <= native_names
            or any(type(raw) is not bytes or len(raw) > MAX_DOCUMENT_BYTES for raw in native_sources.values())):
        raise ValueError('SOURCE_CHANGED: full review sources do not match stage roles')
    actual = {**captured.sources(), **native_sources}
    if len(actual) > 16 or sum(map(len, actual.values())) > 16 * 1024 * 1024:
        raise ValueError('UNTRUSTED_MAPPING: current stage source set exceeds its limits')
    documents = {name: raw.decode('utf-8', errors='strict') for name, raw in actual.items()}
    material = parse_review_documents(stage, documents, dependency_fingerprint)
    context = dict(supporting_context or {})
    metadata = {
        'schema_version': 1, 'status': captured.status, 'gaps': captured.gaps,
        'mapping_digest': captured.mapping_digest, 'package_revision': captured.package_revision,
        'repository_identity': captured.repository_identity,
        'source_hashes': captured.source_hashes(),
        'location_hashes': [{'artifact_id': item.artifact_id, 'category': item.category, 'label': item.label,
                            'path': item.path, 'start': item.start, 'end': item.end, 'sha256': item.sha256}
                           for item in captured.locations],
    }
    owned_context = {'source/source-map.json': json.dumps(checked.as_dict(topology=True), ensure_ascii=False,
                                                        sort_keys=True, separators=(',', ':')),
                     'source/source-boundaries.json': json.dumps(metadata, ensure_ascii=False,
                                                                sort_keys=True, separators=(',', ':'))}
    owned_context.update({'source/contracts/' + path: raw.decode('utf-8', errors='strict')
                          for path, raw in captured.contracts().items()})
    if set(context) & set(owned_context):
        raise ValueError('UNTRUSTED_MAPPING: caller context shadows source evidence')
    context.update(owned_context)
    request = full_review_request_v2(material.snapshot, documents=material.document_map(),
        section_texts=material.section_map(), supporting_context=context)
    return request, material


def capture_composition(repo: Path, source_map: SourceMap) -> SourceMapEpoch:
    """Check every mapped stage in bounded groups, then fence the full epoch."""
    if not isinstance(source_map, SourceMap):
        raise ValueError('UNTRUSTED_MAPPING: validated source map required')
    checked = load_source_map(source_map._bytes)
    selected = {artifact['artifact_id'] for artifact in checked.artifacts}
    frames = []
    for group in ARTIFACT_REGISTRY.groups.values():
        roles = tuple(ident for ident in group.artifact_ids if ident in selected)
        if roles:
            frames.append(capture_mapped_sources(repo, checked, roles))
    sources, contracts = {}, {}
    first = frames[0]
    for frame in frames:
        if (frame.repository_identity != first.repository_identity or frame.package_revision != first.package_revision):
            raise ValueError('SOURCE_CHANGED: composition stages have different repository or package epochs')
        for target, values in ((sources, frame.source_hashes()),
                               (contracts, {path: hashlib.sha256(raw).hexdigest() for path, raw in frame.contracts().items()})):
            for path, digest in values.items():
                if path in target and target[path] != digest:
                    raise ValueError('SOURCE_CHANGED: shared composition bytes changed between stages')
                target[path] = digest
    root = fs.open_absolute_dir(Path(repo).absolute())
    try:
        if (fs.directory_identity(root) != first.repository_identity
                or resolve_package_at(root, checked.package_ref).revision != first.package_revision
                or any(hashlib.sha256(_read_source(root, path)[0]).hexdigest() != digest
                       for path, digest in (*sources.items(), *contracts.items()))):
            raise ValueError('SOURCE_CHANGED: composition source changed before returning its epoch')
    finally:
        fs.close_dir(root)
    return validate_source_map_epoch(checked, SourceMapEpoch(source_map_digest(checked), first.package_revision,
        first.repository_identity, tuple(sorted(sources.items())), tuple(sorted(contracts.items()))))
