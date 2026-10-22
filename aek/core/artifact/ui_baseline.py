"""Data-only source/region baselines; neither paths nor hashes grant approval."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from aek.core.artifact.ui_observation import canonical_bytes

MAX_BASELINE_BYTES = 64 * 1024
MAX_SOURCES = 128
MAX_REGIONS = 32
_ID = re.compile(r'[A-Z][A-Z0-9_-]{0,63}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


class BaselineError(ValueError):
    pass


def _object(value, fields):
    if type(value) is not dict or set(value) != fields:
        raise BaselineError('invalid baseline fields')
    return value


def _ids(value, *, prefix=None, allow_empty=False):
    if (type(value) is not list or len(value) > MAX_SOURCES or (not value and not allow_empty)
            or any(type(item) is not str or _ID.fullmatch(item) is None for item in value)
            or len(value) != len(set(value))
            or (prefix and any(re.fullmatch(prefix + r'[1-9][0-9]*', item) is None for item in value))):
        raise BaselineError('invalid baseline identity list')
    return value


def _text(value, *, maximum=1000):
    if type(value) is not str or not value.strip() or len(value) > maximum or '\0' in value:
        raise BaselineError('invalid baseline text')


def _path(value):
    _text(value, maximum=500)
    parts = value.split('/')
    if (not 1 <= len(parts) <= 20
            or unicodedata.normalize('NFC', value) != value):
        raise BaselineError('unsafe baseline source path')
    for part in parts:
        if (not part or part.startswith('.') or part[-1] in '. '
                or any(ch in part for ch in '\\:<>"|?*') or any(ord(ch) < 32 for ch in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', part)):
            raise BaselineError('unsafe baseline source path')


def validate_baseline(value: object) -> dict:
    raw = canonical_bytes(value)
    if len(raw) > MAX_BASELINE_BYTES:
        raise BaselineError('baseline exceeds byte limit')
    root = _object(value, {'schema_version', 'sources', 'regions'})
    if type(root['schema_version']) is not int or root['schema_version'] != 2:
        raise BaselineError('baseline requires explicit schema_version 2')
    if type(root['sources']) is not list or len(root['sources']) > MAX_SOURCES:
        raise BaselineError('invalid baseline source count')
    sources, paths = set(), set()
    for source in root['sources']:
        source = _object(source, {'id', 'kind', 'path', 'sha256', 'certainty'})
        _ids([source['id']])
        _path(source['path'])
        if source['id'] in sources or source['path'] in paths:
            raise BaselineError('duplicate baseline source identity or path')
        sources.add(source['id'])
        paths.add(source['path'])
        if (type(source['sha256']) is not str or _SHA.fullmatch(source['sha256']) is None
                or source['kind'] not in ('component', 'token', 'layout')
                or source['certainty'] not in ('source_backed', 'inferred', 'unknown')):
            raise BaselineError('invalid baseline source evidence')
    if type(root['regions']) is not list or not 1 <= len(root['regions']) <= MAX_REGIONS:
        raise BaselineError('invalid baseline region count')
    seen, used, target_forms = set(), set(), {}
    observation_count = 0
    for region in root['regions']:
        fields = {'page_id', 'area_id', 'region_id', 'intent', 'feature_ids',
            'acceptance_ids', 'source_ids', 'screenshot_ids', 'rationale', 'adjacent_context'}
        if type(region) is dict and 'observations' in region:
            fields.add('observations')
        typed = type(region) is dict and ('state_id' in region or 'context_id' in region)
        if typed:
            fields.update(('state_id', 'context_id'))
        region = _object(region, fields)
        if (type(region['page_id']) is not str or not re.fullmatch(r'PAGE-[1-9][0-9]*', region['page_id'])
                or type(region['area_id']) is not str or not re.fullmatch(r'AREA-[1-9][0-9]*', region['area_id'])):
            raise BaselineError('invalid baseline page or area identity')
        _ids([region['region_id']])
        if typed and (type(region['state_id']) is not str
                or region['state_id'] not in {'default', 'loading', 'empty', 'error', 'dialog', 'drawer', 'permission'}
                or type(region['context_id']) is not str
                or not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', region['context_id'])):
            raise BaselineError('invalid baseline state or context identity')
        pair = (region['page_id'], region['area_id'])
        if pair in target_forms and target_forms[pair] != typed:
            raise BaselineError('ambiguous shared and state-specific baseline targets')
        target_forms[pair] = typed
        identity = baseline_target(region) + (region['region_id'],)
        if identity in seen:
            raise BaselineError('duplicate baseline region identity')
        seen.add(identity)
        if region['intent'] not in ('PRESERVE', 'MODIFY', 'NEW', 'REMOVE'):
            raise BaselineError('invalid baseline region intent')
        _ids(region['feature_ids'], prefix='F')
        _ids(region['acceptance_ids'], prefix='A')
        _ids(region['source_ids'], allow_empty=True)
        _ids(region['screenshot_ids'], prefix='P', allow_empty=True)
        if not set(region['source_ids']) <= sources:
            raise BaselineError('unknown baseline source reference')
        used.update(region['source_ids'])
        _text(region['rationale'])
        if region['adjacent_context'] is not None:
            _text(region['adjacent_context'])
        if not region['screenshot_ids']:
            if region['intent'] not in ('NEW', 'REMOVE') or region['adjacent_context'] is None:
                raise BaselineError('missing baseline screenshot or justified adjacent context')
        if 'observations' in region:
            refs = region['observations']
            if type(refs) is not list or not refs or len(refs) > MAX_SOURCES:
                raise BaselineError('invalid baseline observation references')
            observation_count += len(refs)
            if observation_count > MAX_SOURCES:
                raise BaselineError('baseline observation reference count exceeds limit')
            evidence_ids = set()
            for ref in refs:
                ref = _object(ref, {'evidence_id', 'sha256'})
                if (type(ref['evidence_id']) is not str
                        or ref['evidence_id'] not in region['screenshot_ids']
                        or ref['evidence_id'] in evidence_ids
                        or type(ref['sha256']) is not str or _SHA.fullmatch(ref['sha256']) is None):
                    raise BaselineError('invalid baseline observation identity or digest')
                evidence_ids.add(ref['evidence_id'])
    if used != sources:
        raise BaselineError('unreferenced baseline source')
    return json.loads(raw.decode('utf-8'))


def baseline_target(region: dict) -> tuple:
    """Use explicit state/context together, or preserve the legacy shared pair."""
    pair = (region['page_id'], region['area_id'])
    return pair + (region['state_id'], region['context_id']) if 'state_id' in region else pair


def project_baseline(value: dict, actual_sources: dict[str, str | None]) -> dict[tuple, dict]:
    """Only the authorized reader supplies actual hashes; never model output."""
    value = validate_baseline(value)
    by_id = {source['id']: source for source in value['sources']}
    groups = {}
    for region in value['regions']:
        groups.setdefault(baseline_target(region), []).append(region)
    result = {}
    for identity, regions in groups.items():
        source_ids = sorted({ident for row in regions for ident in row['source_ids']})
        sources = [by_id[ident] for ident in source_ids]
        gaps = []
        for source in sources:
            if actual_sources.get(source['id']) != source['sha256']:
                gaps.append({'source_id': source['id'], 'reason': 'source_changed_or_unavailable'})
            elif source['certainty'] != 'source_backed':
                gaps.append({'source_id': source['id'], 'reason': 'source_not_verified'})
        snapshot = {'schema_version': 2, 'sources': sources,
                    'regions': sorted(regions, key=lambda row: row['region_id'])}
        result[identity] = {'digest': hashlib.sha256(canonical_bytes(snapshot)).hexdigest(),
                            'gaps': gaps, 'regions': snapshot['regions']}
    return result
