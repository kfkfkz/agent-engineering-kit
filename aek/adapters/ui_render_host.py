"""One controller-selected Linux render profile for the original local host.

This is not a public execution API or a runtime adoption decision. The fixed
preparation/file readers belong to the composition root; data selects only its
one target. The original host owns confirmation, completion and recovery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import hashlib
import json
from pathlib import PurePosixPath
import re

from aek.adapters.security_runtime import BoundRuntimeAdoption
from aek.adapters.security_evidence import source_binding
from aek.adapters.ui_linux_renderer import LinuxRenderProvider, LinuxRuntime, linux_runtime_fingerprint, render_plan
from aek.adapters.ui_renderer import CdpRenderer, ProviderUnavailable
from aek.application.local_host import LocalToolCompletion, PreparedLocalTool
from aek.core.artifact.ui_preview import (
    PreviewItem, RenderConditions, RenderResult, build_static_preview, validate_html, validate_rendered_png,
)
from aek.core.context.identity import identity_as_dict
from aek.core.security.authority import canonical, digest, timestamp

PROFILE_ID = 'aek-static-render-linux-v2'
MAX_PROFILE_PNG_BYTES = 512 * 1024


@dataclass(frozen=True)
class _Preparation:
    selection: bytes
    html: bytes
    native_plan: object


class LinuxRenderHostProfile:
    def __init__(self, authority, source_port, *, runtime, target, conditions,
                 prepare_selection, read_html, required_components, adoption_proposals,
                 adoption_source_refs, clock, max_age_days=7):
        if (type(runtime) is not LinuxRuntime or type(conditions) is not RenderConditions
                or type(target) is not tuple or len(target) != 4
                or any(type(part) is not str for part in target)
                or re.fullmatch('PAGE-[1-9][0-9]*', target[0]) is None
                or re.fullmatch('AREA-[1-9][0-9]*', target[1]) is None
                or target[2] not in {'default', 'loading', 'empty', 'error', 'dialog', 'drawer', 'permission'}
                or re.fullmatch('[A-Z][A-Z0-9_-]{0,31}', target[3]) is None
                or not callable(prepare_selection) or not callable(read_html)
                or source_port.authority is not authority or clock is not authority._clock):
            raise ValueError('INVALID_FIXED_RENDER_PROFILE')
        conditions.checked()
        self._authority, self._source = authority, source_port
        self._runtime, self._target, self._conditions = runtime, target, conditions
        self._prepare_selection, self._read_html = prepare_selection, read_html
        self._required, self._clock = required_components, clock
        self._adoption = BoundRuntimeAdoption(authority, source_port,
            proposals=adoption_proposals, source_refs=adoption_source_refs,
            required_components=required_components, runtime_sha256=runtime.sha256,
            runtime_fingerprint=linux_runtime_fingerprint, clock=clock, max_age_days=max_age_days)
        self._bound = self._snapshot()

    def _snapshot(self):
        authority = self._authority
        return (id(authority), id(self._source), id(self._source.authority), authority.repository_ref,
            authority.task_id, authority.scope_ref, authority.current_identity, authority.author,
            authority.expires_at, id(authority.host_source), id(authority._clock), id(self._clock),
            self._runtime, self._target, self._conditions, self._required,
            id(self._prepare_selection), id(self._read_html), id(self._adoption), self._adoption._bound)

    def control_fence(self):
        """Pure configuration fence, not current external-file/source proof.

        Consumers call this after their final IO and before publishing. It
        never measures runtime bytes, asks a clock, loads a source or grants
        authority. Actual currentness still requires the original read path.
        """
        try:
            return (self._snapshot() == self._bound
                    and self._adoption._snapshot() == self._adoption._bound)
        except Exception:
            return False

    def _material(self):
        if self._snapshot() != self._bound:
            raise ProviderUnavailable('fixed render context changed')
        provided = self._prepare_selection(self._target)
        selected_bytes = canonical(provided)
        selected = json.loads(selected_bytes)
        # Reuse the existing strict prepared-selection contract. No model or
        # browser runs, and this derived validation preview is not published.
        build_static_preview(selected, [PreviewItem(self._target)])
        if len(selected['per_item']) != 1:
            raise ValueError('FIXED_RENDER_SELECTION_MISMATCH')
        row = selected['per_item'][0]
        relative = row['prototype_path']
        if (PurePosixPath(relative).is_absolute() or '\\' in relative or ':' in relative
                or any(part in {'', '.', '..'} for part in relative.split('/'))):
            raise ValueError('FIXED_RENDER_PATH_REJECTED')
        raw = self._read_html(relative)
        validate_html(raw, row['prototype_sha256'])
        if (self._adoption(self._runtime) is not True or self._snapshot() != self._bound
                or canonical(provided) != selected_bytes):
            raise ProviderUnavailable('current render adoption unavailable')
        return canonical(selected), raw

    def _source_ref(self, selection, native_plan):
        return 'render-' + digest({'task_id': self._authority.task_id, 'target': self._target,
            'prepare_sha256': json.loads(selection)['prepare_sha256'], 'plan_digest': native_plan.plan_digest})

    def _native_plan(self, raw):
        return render_plan(self._runtime, hashlib.sha256(raw).hexdigest(), self._conditions,
            repository_ref=self._authority.repository_ref, scope_ref=self._authority.scope_ref,
            identity_digest=self._authority.current_identity.envelope_digest, max_output_bytes=1_000_000)

    def prepare(self, data):
        if (type(data) is not dict or set(data) != {'profile_id', 'targets'}
                or data['profile_id'] != PROFILE_ID or data['targets'] != ['/'.join(self._target)]):
            raise ValueError('INVALID_FIXED_RENDER_SELECTION')
        selected, raw = self._material()
        plan = self._native_plan(raw)
        if self._snapshot() != self._bound:
            raise ProviderUnavailable('fixed render context changed')
        return PreparedLocalTool(asdict(plan), plan.binding(), self._source_ref(selected, plan),
                                 _Preparation(selected, raw, plan))

    def revalidate(self, prepared):
        try:
            if type(prepared) is not PreparedLocalTool or type(prepared.provider_data) is not _Preparation:
                return False
            data = prepared.provider_data
            selected, raw = self._material()
            return (selected == data.selection and raw == data.html
                and data.native_plan == self._native_plan(raw)
                and canonical(prepared.plan) == canonical(asdict(data.native_plan))
                and canonical(prepared.binding) == canonical(data.native_plan.binding())
                and prepared.source_ref == self._source_ref(selected, data.native_plan)
                and self._snapshot() == self._bound)
        except Exception:
            return False

    def execute(self, prepared, grant, authority):
        if authority is not self._authority or self.revalidate(prepared) is not True:
            raise ProviderUnavailable('fixed render context changed')
        data = prepared.provider_data
        authority.validate_runtime_grant(grant,
            **{key: value for key, value in prepared.binding.items() if key != 'operation'})
        native = LinuxRenderProvider(self._runtime, authority=authority, grant=grant,
                                     adoption_validator=self._adoption)
        result = CdpRenderer(native).render(data.html, self._conditions, data.native_plan,
            expected_sha256=data.native_plan.input_sha256)
        if self.revalidate(prepared) is not True:
            raise ProviderUnavailable('fixed render context changed')
        selected = json.loads(data.selection)
        row = selected['per_item'][0]
        facts = {'target': list(self._target), 'prepare_sha256': selected['prepare_sha256'],
            'dependency_sha256': row['dependency_sha256'], 'scope_digest': row['scope_digest'],
            'render_result': self._encode_result(result, data)}
        metadata = {'id': prepared.source_ref, 'ref': prepared.source_ref, 'run_id': prepared.source_ref,
            'producer_ref': PROFILE_ID, 'identity': identity_as_dict(authority.current_identity),
            'required_components': list(self._required), 'kind': 'static-render', 'status': 'DECLARED',
            'checked_at': self._clock().isoformat(), 'scope_ref': authority.scope_ref,
            'tool_version': self._runtime.browser_product, 'rules_digest': None, 'database_digest': None,
            'coverage': 'COMPLETE_FOR_DECLARED_SCOPE', 'coverage_gaps': [],
            'summary': 'Controlled static capture of one target; not visual acceptance'}
        source_binding(metadata, facts)  # Original bounded facts/metadata contract.
        if self._snapshot() != self._bound:
            raise ProviderUnavailable('fixed render context changed')
        return LocalToolCompletion(metadata, facts)

    def _encode_result(self, result, prepared):
        if (type(result) is not RenderResult or result.status != 'RENDERED'
                or result.reason_code != 'STATIC_CAPTURE_COMPLETE' or result.comparable is not False
                or type(result.png) is not bytes or not 1 <= len(result.png) <= MAX_PROFILE_PNG_BYTES
                or result.input_sha256 != prepared.native_plan.input_sha256
                or result.runtime_sha256 != self._runtime.sha256
                or result.plan_sha256 != prepared.native_plan.plan_digest
                or result.output_sha256 != hashlib.sha256(result.png).hexdigest()
                or result.conditions != self._conditions.checked()
                or type(result.unknowns) is not tuple or len(result.unknowns) > 16
                or any(type(value) is not str or len(value) > 512 for value in result.unknowns)):
            raise ValueError('INVALID_FIXED_RENDER_COMPLETION')
        validate_rendered_png(result.png, self._conditions)
        # An internal codec of the original RenderResult inside original facts,
        # not another receipt, approval or public import schema. Both original
        # completion journal and source store retain the actual bounded PNG.
        value = asdict(result)
        value['png_base64'] = base64.b64encode(value.pop('png')).decode('ascii')
        value['unknowns'] = list(result.unknowns)
        canonical(value)
        return value

    def read_result(self, *, expected_item=None):
        """Read a current original source, optionally narrowed to an exact item.

        None preserves the original no-argument behavior. An expected item is
        data, not authority; its complete canonical content must match the
        current controller-selected item, not only target IDs or one hash.
        """
        try:
            if expected_item is not None and type(expected_item) is not dict:
                return None
            expected_bytes = canonical(expected_item) if expected_item is not None else None
            prepared = self.prepare({'profile_id': PROFILE_ID, 'targets': ['/'.join(self._target)]})
            data = prepared.provider_data
            selected = json.loads(data.selection)
            row = selected['per_item'][0]
            if expected_bytes is not None and canonical(row) != expected_bytes:
                return None
            observation = self._source.lookup(prepared.source_ref)
            if observation is None:
                return None
            metadata, facts = observation.metadata, observation.facts
            if (metadata['status'] != 'VERIFIED' or metadata['kind'] != 'static-render'
                    or metadata['ref'] != prepared.source_ref or metadata['producer_ref'] != PROFILE_ID
                    or metadata['scope_ref'] != self._authority.scope_ref
                    or metadata['identity'] != identity_as_dict(self._authority.current_identity)
                    or metadata['required_components'] != list(self._required)
                    or metadata['coverage'] != 'COMPLETE_FOR_DECLARED_SCOPE' or metadata['coverage_gaps']
                    or timestamp(metadata['checked_at']) > self._clock()
                    or type(facts) is not dict or set(facts) != {'target', 'prepare_sha256',
                        'dependency_sha256', 'scope_digest', 'render_result'}
                    or facts['target'] != list(self._target)
                    or facts['prepare_sha256'] != selected['prepare_sha256']
                    or facts['dependency_sha256'] != row['dependency_sha256']
                    or facts['scope_digest'] != row['scope_digest']):
                return None
            result = self._decode_result(facts['render_result'], data)
            if self.revalidate(prepared) is not True:
                return None
            # Finish current material IO before the original source/proof
            # fence. Do not repeat the renderer or mint recovery provenance.
            current = self._source.lookup(prepared.source_ref)
            if (current is None or canonical(current.metadata) != canonical(metadata)
                    or canonical(current.facts) != canonical(facts)
                    or current.provenance_ref != observation.provenance_ref):
                return None
            receipt = self._authority.read_provenance(current.provenance_ref,
                expected_identity_digest=metadata['identity']['envelope_digest'])
            binding = source_binding({**metadata, 'status': 'DECLARED'}, facts)
            if (receipt is None or receipt['binding'] != binding
                    or receipt['event']['kind'] != 'tool-completed'
                    or receipt['event']['operation'] != 'source'
                    or receipt['event']['actor'] != PROFILE_ID
                    or (expected_bytes is not None and canonical(expected_item) != expected_bytes)
                    or not self.control_fence()):
                return None
            return result
        except Exception:
            return None

    def _decode_result(self, value, prepared):
        fields = {'status', 'reason_code', 'input_sha256', 'png_base64', 'output_sha256',
                  'conditions', 'comparable', 'unknowns', 'runtime_sha256', 'plan_sha256'}
        if (type(value) is not dict or set(value) != fields or type(value['png_base64']) is not str
                or len(value['png_base64']) > 4 * ((MAX_PROFILE_PNG_BYTES + 2) // 3)
                or type(value['unknowns']) is not list or len(value['unknowns']) > 16
                or type(value['conditions']) is not dict):
            raise ValueError('INVALID_FIXED_RENDER_COMPLETION')
        RenderConditions(**value['conditions']).checked()
        result = RenderResult(**{key: item for key, item in value.items()
            if key not in {'png_base64', 'unknowns'}},
            png=base64.b64decode(value['png_base64'], validate=True), unknowns=tuple(value['unknowns']))
        if canonical(self._encode_result(result, prepared)) != canonical(value):
            raise ValueError('INVALID_FIXED_RENDER_COMPLETION')
        return result
