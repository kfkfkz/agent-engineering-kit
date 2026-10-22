"""Bounded UI obligations derived from reviewed upstream facts.

Internal v2 classification contract (stored in the reviewed overview):
``schema_version``, actual ``requirements_sha256`` and ``items``. Each item
classifies one current F/A ID as UI/NON_UI/UNKNOWN with a reason and targets.
A UI target is the full PAGE/AREA/state/context identity. This is data, not an
approval: the caller must establish current upstream credentials separately.
No image, filesystem, model, query or renderer operation belongs in this rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SHA = re.compile(r'[0-9a-f]{64}')
_TRACE = re.compile(r'[FA][1-9][0-9]*')
_TARGET_KEYS = ('page_id', 'area_id', 'state_id', 'context_id')
MAX_CLASSIFICATIONS = 2048
MAX_GAPS = 32


@dataclass(frozen=True)
class UiScope:
    complete: bool
    required: tuple[tuple[str, tuple[str, str, str, str]], ...]
    gaps: tuple[str, ...]
    truncated: bool = False
    # Populated by the existing upstream adapter from actual source snapshots,
    # never from a classification's self-declared approval field.
    upstream_sha256: str | None = None

    @property
    def non_ui(self) -> bool:
        return self.complete and not self.required


def _result(required, gaps) -> UiScope:
    return UiScope(not gaps, tuple(sorted(set(required))), tuple(gaps[:MAX_GAPS]), len(gaps) > MAX_GAPS)


def _target(value) -> tuple[str, str, str, str]:
    if not isinstance(value, dict) or set(value) != set(_TARGET_KEYS):
        raise ValueError('UI target must include PAGE/AREA/state/context')
    values = tuple(value[key] for key in _TARGET_KEYS)
    for key, item in zip(_TARGET_KEYS, values):
        if (type(item) is not str or len(item) > 120 or not item
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', item)):
            raise ValueError('invalid UI target identity')
        if key in ('page_id', 'area_id') and not item.startswith(key.split('_')[0].upper() + '-'):
            raise ValueError('invalid PAGE/AREA target identity')
    return values


def classify_ui_scope(*, feature_ids, acceptance_ids, classification,
                      requirements_sha256, upstream_current: bool,
                      plan_required: bool) -> UiScope:
    """Validate exhaustive reviewed classification without consulting UI output."""
    required, gaps = [], []
    if (not isinstance(feature_ids, (tuple, list)) or not isinstance(acceptance_ids, (tuple, list))
            or type(plan_required) is not bool or type(upstream_current) is not bool):
        return _result([], ['current upstream UI scope is unknown'])
    ids = tuple(feature_ids) + tuple(acceptance_ids)
    if (len(ids) > MAX_CLASSIFICATIONS or not ids
            or any(type(item) is not str or not _TRACE.fullmatch(item) for item in ids)
            or len(ids) != len(set(ids))):
        return _result([], ['current upstream F/A enumeration is unknown'])
    if upstream_current is not True:
        gaps.append('UI classification upstream credentials are not current')
    if (not isinstance(classification, dict)
            or set(classification) != {'schema_version', 'requirements_sha256', 'items'}
            or type(classification.get('schema_version')) is not int
            or classification['schema_version'] != 2
            or type(classification.get('requirements_sha256')) is not str
            or not _SHA.fullmatch(classification['requirements_sha256'])
            or classification['requirements_sha256'] != requirements_sha256
            or type(classification.get('items')) is not list
            or len(classification['items']) > MAX_CLASSIFICATIONS):
        return _result([], gaps + ['missing, stale or invalid UI classification'])
    seen = set()
    for item in classification['items']:
        if (not isinstance(item, dict) or set(item) != {'id', 'classification', 'reason', 'targets'}
                or type(item['id']) is not str or not _TRACE.fullmatch(item['id'])):
            gaps.append('invalid UI classification item')
            continue
        ident = item['id']
        if ident in seen:
            gaps.append(f'{ident}: duplicate or contradictory classification')
        seen.add(ident)
        if ident not in ids:
            gaps.append(f'{ident}: classification has no current upstream owner')
        kind, reason, targets = item['classification'], item['reason'], item['targets']
        if (type(kind) is not str or kind not in ('UI', 'NON_UI', 'UNKNOWN')
                or type(reason) is not str or not reason.strip() or len(reason) > 2048
                or type(targets) is not list or len(targets) > 128):
            gaps.append(f'{ident}: invalid classification or missing reason')
            continue
        if kind != 'UI':
            if targets:
                gaps.append(f'{ident}: non-UI/unknown cannot declare UI targets')
            if kind == 'UNKNOWN':
                gaps.append(f'{ident}: UI relevance remains UNKNOWN')
            continue
        if not targets:
            gaps.append(f'{ident}: UI classification has no required target')
        target_seen = set()
        for target in targets:
            try:
                key = _target(target)
                if key in target_seen:
                    raise ValueError('duplicate UI target')
                target_seen.add(key)
                required.append((ident, key))
            except ValueError:
                gaps.append(f'{ident}: invalid or duplicate UI target')
    gaps.extend(f'{ident}: missing UI classification' for ident in sorted(set(ids) - seen))
    if plan_required is True and not required:
        gaps.append('ArtifactPlan requires UI prototypes; NON_UI cannot waive it')
    return _result(required, gaps)


def check_ui_coverage(scope: UiScope, prototypes, screenshots, *, selected_targets=None) -> UiScope:
    """Reverse-check required target ownership against actual projections.

    Caller still performs the existing bytes, applicability and confirmation
    checks. A matching row here is not an accepted image or prototype.
    """
    gaps = list(scope.gaps)
    required = scope.required
    for ident, target in required:
        if selected_targets is not None and target not in selected_targets:
            continue
        owners = []
        for item in prototypes:
            if not isinstance(item, dict):
                continue
            try:
                identity = {name: item.get(name) for name in _TARGET_KEYS}
                if 'state_id' not in item and 'context_id' not in item:
                    contexts = item.get('evidence_contexts')
                    if (not isinstance(contexts, list) or not contexts
                            or any(not isinstance(ref, dict) or type(ref.get('context_id')) is not str
                                   for ref in contexts)):
                        continue
                    values = {ref['context_id'] for ref in contexts}
                    if len(values) != 1:
                        continue
                    identity.update(state_id='default', context_id=next(iter(values)))
                key = _target(identity)
            except ValueError:
                continue
            trace_key = 'feature_ids' if ident.startswith('F') else 'acceptance_ids'
            if key == target and isinstance(item.get(trace_key), list) and ident in item[trace_key]:
                owners.append(item)
        if not owners:
            gaps.append(f'{ident}: missing prototype target {"/".join(target)}')
            continue
        covered = False
        for prototype in owners:
            if (not isinstance(prototype.get('depends_on'), list)
                    or not isinstance(prototype.get('evidence_contexts'), list)):
                continue
            for image in screenshots:
                if (isinstance(image, dict) and image.get('id') in prototype.get('depends_on', [])
                        and isinstance(image.get(trace_key), list) and ident in image[trace_key]
                        and image.get('state') == target[2]
                        and any(isinstance(ref, dict) and ref.get('evidence_id') == image.get('id')
                                and ref.get('context_id') == target[3]
                                for ref in prototype.get('evidence_contexts', []))
                        and isinstance(image.get('applies_to'), list)
                        and any(isinstance(context, dict) and context.get('context_id') == target[3]
                                for context in image.get('applies_to', []))):
                    covered = True
        if not covered:
            gaps.append(f'{ident}: missing screenshot coverage for {"/".join(target)}')
    result = _result(required, gaps)
    return UiScope(result.complete, result.required, result.gaps, result.truncated or scope.truncated)
