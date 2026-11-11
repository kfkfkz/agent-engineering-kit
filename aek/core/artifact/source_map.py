"""Explicit external document roles and single-writer contracts.

Mapping IDs belong to this view, not to the original Markdown. Parsing data
does not prove file contents, capability visibility, execution or approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from aek.core.artifact.document_layout import relative_parts, load_object
from aek.core.artifact.registry import ARTIFACT_REGISTRY

_SHA = re.compile(r'[a-f0-9]{64}\Z')
_ID = re.compile(r'(?:[AFDT][1-9][0-9]*|TC-[1-9][0-9]*|PAGE-[1-9][0-9]*)\Z')
_FAMILY = {'requirements-analysis': 'A', 'outline-design': 'F', 'business-flow': 'F',
           'ui-design': 'PAGE-', 'detail-design': 'D', 'api-design': 'D', 'database-design': 'D',
           'tasks': 'T', 'test-plan': 'TC-'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _text(value, maximum=4096):
    if type(value) is not str or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('UNTRUSTED_MAPPING: invalid mapping text')
    return value


def mapped_source_path(value, *, markdown=True):
    parts = relative_parts(value)
    if (len(parts) < 3 or parts[0] != 'docs' or parts[1].casefold() in {'memory', 'delivery-receipts'}
            or (markdown and not value.endswith('.md'))):
        raise ValueError('UNTRUSTED_MAPPING: source must be a dedicated repository Markdown document')
    # Original authority bindings retain their historical canonical v1 path
    # contract. Unsupported spelling is refused, never normalized to a file.
    if unicodedata.normalize('NFC', value) != value:
        raise ValueError('UNSUPPORTED_SOURCE_PATH_SPELLING: external source path must already be NFC')
    return value


def _ids(values):
    if (type(values) is not list or len(values) > 512
            or any(type(item) is not str or not _ID.fullmatch(item) for item in values)
            or len(set(values)) != len(values)):
        raise ValueError('UNTRUSTED_MAPPING: invalid trace references')


def _locator(value, sources, extra):
    if type(value) is not dict:
        raise ValueError('UNTRUSTED_MAPPING: locator must be an object')
    required = {'path'} | extra
    selectors = {'heading'} if 'heading' in value else {'lines'}
    if (type(value) is not dict or set(value) not in (required | selectors, required | selectors | {'sha256'})
            or value.get('path') not in sources):
        raise ValueError('UNTRUSTED_MAPPING: locator must name an actual participating source')
    if selectors == {'heading'}:
        _text(value['heading'])
    elif (type(value.get('lines')) is not list or len(value['lines']) != 2
            or any(type(item) is not int for item in value['lines'])
            or not 1 <= value['lines'][0] <= value['lines'][1] <= 1_000_000):
        raise ValueError('UNTRUSTED_MAPPING: invalid inclusive source line range')
    if 'sha256' in value and (type(value['sha256']) is not str or not _SHA.fullmatch(value['sha256'])):
        raise ValueError('UNTRUSTED_MAPPING: invalid selected-byte digest')


@dataclass(frozen=True)
class SourceMap:
    package_ref: str
    _bytes: bytes

    @property
    def artifacts(self):
        return tuple(json.loads(self._bytes)['artifacts'])

    @property
    def capability_bindings(self):
        return tuple(json.loads(self._bytes)['capability_bindings'])

    def as_dict(self, *, topology=False):
        value = json.loads(self._bytes)
        if topology:
            for artifact in value['artifacts']:
                for field in ('sections', 'points', 'globals'):
                    for location in artifact[field]:
                        location.pop('sha256', None)
        return value


@dataclass(frozen=True)
class SourceMapEpoch:
    """In-process current read facts; not a persisted approval credential."""
    mapping_digest: str
    package_revision: str
    repository_identity: str
    source_hashes: tuple[tuple[str, str], ...]
    contract_hashes: tuple[tuple[str, str], ...]


def validate_source_map_epoch(source_map: SourceMap, value: SourceMapEpoch) -> SourceMapEpoch:
    checked = load_source_map(source_map._bytes)
    if (not isinstance(value, SourceMapEpoch) or value.mapping_digest != source_map_digest(source_map)
            or type(value.package_revision) is not str or not _SHA.fullmatch(value.package_revision)):
        raise ValueError('SOURCE_CHANGED: current source epoch has a different mapping or package')
    _text(value.repository_identity)
    sources = {path for artifact in checked.artifacts for path in artifact['sources']}
    contracts = {binding['contract_path']: binding['contract_sha256'] for binding in checked.capability_bindings}
    for pairs, expected in ((value.source_hashes, sources), (value.contract_hashes, set(contracts))):
        if (type(pairs) is not tuple or any(type(pair) is not tuple or len(pair) != 2
                or type(pair[0]) is not str or type(pair[1]) is not str or not _SHA.fullmatch(pair[1]) for pair in pairs)
                or pairs != tuple(sorted(dict(pairs).items())) or set(dict(pairs)) != expected):
            raise ValueError('SOURCE_CHANGED: current source epoch lacks the exact physical source set')
    if dict(value.contract_hashes) != contracts:
        raise ValueError('CAPABILITY_CONTRACT_CHANGED: current contract epoch differs from mapping')
    return value


def load_source_map(value):
    if type(value) is bytes:
        value = load_object(value)
    if (type(value) is not dict or set(value) != {'schema_version', 'package_ref', 'artifacts', 'capability_bindings'}
            or type(value['schema_version']) is not int or value['schema_version'] != 2
            or type(value['artifacts']) is not list or not 1 <= len(value['artifacts']) <= len(ARTIFACT_REGISTRY.artifacts)
            or type(value['capability_bindings']) is not list):
        raise ValueError('UNTRUSTED_MAPPING: unknown source mapping schema')
    mapped_source_path(value['package_ref'], markdown=False)
    artifacts, definitions, total_locations, total_sources = set(), set(), 0, set()
    for artifact in value['artifacts']:
        if (type(artifact) is not dict or set(artifact) != {'artifact_id', 'sources', 'sections', 'points', 'globals'}
                or type(artifact['artifact_id']) is not str or artifact['artifact_id'] not in ARTIFACT_REGISTRY.artifacts
                or artifact['artifact_id'] in artifacts):
            raise ValueError('UNTRUSTED_MAPPING: duplicate or unknown artifact role')
        artifact_id = artifact['artifact_id']
        artifacts.add(artifact_id)
        sources = artifact['sources']
        if (type(sources) is not list or not 1 <= len(sources) <= 16
                or any(type(path) is not str for path in sources) or len(set(sources)) != len(sources)):
            raise ValueError('MISSING_ARTIFACT_CONTENT: exact physical source set required')
        for path in sources:
            mapped_source_path(path)
        total_sources.update(sources)
        if any(type(artifact[name]) is not list for name in ('sections', 'points', 'globals')):
            raise ValueError('UNTRUSTED_MAPPING: source locations must be lists')
        required = ARTIFACT_REGISTRY.artifacts[artifact_id].sections
        sections = [row.get('section') for row in artifact['sections'] if type(row) is dict]
        if (len(sections) != len(required) or any(type(section) is not str for section in sections)
                or set(sections) != set(required)):
            raise ValueError('MISSING_ARTIFACT_CONTENT: required section roles are incomplete or duplicated')
        for location in artifact['sections']:
            _locator(location, sources, {'section'})
        own_ids = set()
        for point in artifact['points']:
            _locator(point, sources, {'id', 'covers', 'uses'})
            ident = point['id']
            if (type(ident) is not str or not re.fullmatch(re.escape(_FAMILY[artifact_id]) + r'[1-9][0-9]*', ident)
                    or ident in own_ids or (artifact_id != 'business-flow' and ident in definitions)):
                raise ValueError('UNTRUSTED_MAPPING: duplicate or wrong-family logical owner')
            own_ids.add(ident)
            if artifact_id != 'business-flow':
                definitions.add(ident)
            _ids(point['covers'])
            _ids(point['uses'])
        if not own_ids:
            raise ValueError('MISSING_ARTIFACT_CONTENT: mapped artifact must identify current trace points')
        for location in artifact['globals']:
            _locator(location, sources, set())
        total_locations += sum(len(artifact[name]) for name in ('sections', 'points', 'globals'))
    if total_locations > 4096 or len(definitions) > 512 or len(total_sources) > 64:
        raise ValueError('UNTRUSTED_MAPPING: source mapping exceeds bounds')
    writers = set()
    for binding in value['capability_bindings']:
        fields = {'artifact_id', 'orchestrator', 'writer', 'provider', 'client', 'entry', 'contract_path', 'contract_sha256'}
        if (type(binding) is not dict or set(binding) != fields or type(binding['artifact_id']) is not str
                or binding['artifact_id'] not in artifacts):
            raise ValueError('UNTRUSTED_MAPPING: unsupported capability binding or authority claim')
        if binding['artifact_id'] in writers:
            raise ValueError('WRITER_CONFLICT: one artifact may have only one writer')
        writers.add(binding['artifact_id'])
        for key in ('orchestrator', 'writer', 'provider'):
            _text(binding[key], maximum=120)
        if (not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', binding['provider'])
                or type(binding['client']) is not str or binding['client'] not in {'codex', 'claude'}):
            raise ValueError('UNTRUSTED_MAPPING: unsupported client or provider identity')
        expected_entry = ('$' if binding['client'] == 'codex' else '/') + binding['provider']
        expected_path = ('.agents' if binding['client'] == 'codex' else '.claude') + '/skills/' + binding['provider'] + '/SKILL.md'
        if (binding['entry'] != expected_entry or binding['contract_path'] != expected_path
                or type(binding['contract_sha256']) is not str or not _SHA.fullmatch(binding['contract_sha256'])):
            raise ValueError('UNTRUSTED_MAPPING: capability contract root or entry mismatch')
    if writers != artifacts:
        raise ValueError('WRITER_CONFLICT: each mapped artifact needs one explicit writer')
    raw = _canonical(value)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('UNTRUSTED_MAPPING: mapping exceeds byte limit')
    return SourceMap(value['package_ref'], raw)


def source_map_digest(value):
    if not isinstance(value, SourceMap):
        raise ValueError('source mapping must be validated')
    checked = load_source_map(value._bytes)
    if checked.package_ref != value.package_ref:
        raise ValueError('source mapping package identity mismatch')
    return hashlib.sha256(_canonical(checked.as_dict(topology=True))).hexdigest()
