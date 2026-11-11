"""Validate declared interactive inputs without granting or executing them."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from aek.core.artifact.ui_preview import MAX_HTML_BYTES, PreviewRejected, SelfContainedHtml


INTERACTIVE_MODE = 'interactive-v1'
MAX_INTERACTION_BYTES = 128 * 1024
MAX_INTERACTION_STEPS = 32
_DIGEST = re.compile(r'[0-9a-f]{64}')
_SEQUENCE_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
_ACTION_FIELDS = {
    'click': {'selector'}, 'fill': {'selector', 'value'}, 'select': {'selector', 'value'},
    'key': {'key'}, 'capture': {'state'},
}


class InteractiveHtml(SelfContainedHtml):
    _inline_interaction = True

    def _attrs(self, tag, attrs):
        super()._attrs(tag, attrs)
        values = {name: value or '' for name, value in attrs}
        if (values.keys() & {'download', 'target', 'formtarget'}
                or (tag == 'input' and values.get('type', '').strip().lower() == 'file')
                or (tag == 'meta' and 'http-equiv' in values)
                or (tag == 'script' and values.get('type', '').strip().lower()
                    not in {'', 'text/javascript', 'application/javascript'})):
            self.errors.append(f'unsupported interactive element or attribute: {tag}')


def validate_interactive_html(raw: bytes, expected_sha256: str) -> None:
    if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_HTML_BYTES
            or type(expected_sha256) is not str or not _DIGEST.fullmatch(expected_sha256)
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise PreviewRejected('interactive HTML snapshot digest or byte limit mismatch')
    parser = InteractiveHtml()
    try:
        parser.feed(raw.decode('utf-8', errors='strict'))
        parser.close()
    except (ValueError, RecursionError) as exc:
        raise PreviewRejected('invalid interactive HTML snapshot') from exc
    if parser.errors:
        raise PreviewRejected(f'UNSUPPORTED_RESOURCE: {parser.errors[0]}')


def interaction_reference(item: dict) -> tuple[str, str]:
    ref = item.get('interaction_spec')
    if (item.get('prototype_mode') != INTERACTIVE_MODE or type(ref) is not dict
            or set(ref) != {'path', 'sha256'} or type(ref['path']) is not str
            or not 1 <= len(ref['path']) <= 500 or not ref['path'].endswith('.json')
            or any(part in {'', '.', '..'} for part in ref['path'].split('/'))
            or any(ch in ref['path'] for ch in ('\\', ':', '\0'))
            or type(ref['sha256']) is not str or not _DIGEST.fullmatch(ref['sha256'])):
        raise PreviewRejected('invalid interactive mode or specification reference')
    return ref['path'], ref['sha256']


@dataclass(frozen=True)
class InteractionStep:
    action: str
    selector: str | None = None
    value: str | None = None
    key: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class InteractionSequence:
    name: str
    steps: tuple[InteractionStep, ...]


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise PreviewRejected('duplicate interaction specification key')
    return result


def load_interaction_spec(raw: bytes, expected_sha256: str) -> tuple[InteractionSequence, ...]:
    if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_INTERACTION_BYTES
            or type(expected_sha256) is not str or not _DIGEST.fullmatch(expected_sha256)
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise PreviewRejected('interaction specification digest or byte limit mismatch')
    try:
        data = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise PreviewRejected('invalid UTF-8 interaction specification') from exc
    if (type(data) is not dict or set(data) != {'schema_version', 'sequences'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or type(data['sequences']) is not list or not 1 <= len(data['sequences']) <= MAX_INTERACTION_STEPS):
        raise PreviewRejected('invalid interaction specification shape')
    count = 0
    sequences = {}
    for sequence in data['sequences']:
        if (type(sequence) is not dict or set(sequence) != {'name', 'steps'}
                or type(sequence['name']) is not str or not _SEQUENCE_NAME.fullmatch(sequence['name'])
                or sequence['name'] in sequences or type(sequence['steps']) is not list
                or not sequence['steps']):
            raise PreviewRejected('invalid or duplicate interaction sequence')
        count += len(sequence['steps'])
        if count > MAX_INTERACTION_STEPS:
            raise PreviewRejected('interaction specification exceeds 32 steps')
        steps = []
        for step in sequence['steps']:
            action = step.get('action') if type(step) is dict else None
            if type(action) is not str or action not in _ACTION_FIELDS or set(step) != {'action'} | _ACTION_FIELDS[action]:
                raise PreviewRejected('invalid interaction action or arguments')
            try:
                valid = all(type(value) is str and len(value.encode('utf-8', errors='strict')) <= 1024
                            and '\0' not in value for value in step.values())
            except UnicodeError:
                valid = False
            if (not valid or ('selector' in step and not step['selector'].strip())
                    or (action == 'key' and step['key'] not in {'Tab', 'Enter', 'Escape'})
                    or (action == 'capture' and not _SEQUENCE_NAME.fullmatch(step['state']))):
                raise PreviewRejected('invalid interaction argument value')
            steps.append(InteractionStep(**step))
        sequences[sequence['name']] = InteractionSequence(sequence['name'], tuple(steps))
    return tuple(sequences.values())
