"""Current interactive evidence through the original LocalToolHost and source port."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import hashlib
import json
from pathlib import PurePosixPath

from aek.adapters.security_evidence import source_binding
from aek.adapters.ui_interactive_renderer import (
    PROFILE_ID, InteractionCapture, LinuxInteractionPlan, LinuxInteractiveRenderProvider,
    _checked_sequence, interaction_plan, render_interaction,
)
from aek.adapters.ui_render_host import LinuxRenderHostProfile, MAX_PROFILE_PNG_BYTES
from aek.adapters.ui_renderer import ProviderUnavailable
from aek.application.local_host import LocalToolCompletion, PreparedLocalTool
from aek.core.artifact.ui_preview import PreviewItem, RenderResult, build_static_preview, validate_rendered_png
from aek.core.context.identity import identity_as_dict
from aek.core.security.authority import canonical, timestamp


@dataclass(frozen=True)
class _InteractionMaterial:
    item: bytes
    html: bytes
    spec: bytes
    plan: LinuxInteractionPlan


class LinuxInteractiveHostProfile(LinuxRenderHostProfile):
    def __init__(self, *args, sequence, read_interaction_spec, **kwargs):
        if not callable(read_interaction_spec):
            raise ValueError('INVALID_INTERACTION_SPEC_READER')
        self._sequence = sequence
        self._read_spec = read_interaction_spec
        super().__init__(*args, **kwargs)

    def _snapshot(self):
        return super()._snapshot() + (self._sequence, id(self._read_spec))

    def _interaction_material(self):
        if not self.control_fence():
            raise ProviderUnavailable('fixed interactive context changed')
        provided = self._prepare_selection(self._target)
        provided_bytes = canonical(provided)
        selected = json.loads(provided_bytes)
        build_static_preview(selected, [PreviewItem(self._target)])
        if selected['schema_version'] != 3 or len(selected['per_item']) != 1:
            raise ValueError('FIXED_INTERACTION_SELECTION_MISMATCH')
        item = selected['per_item'][0]
        relative = item['prototype_path']
        if (PurePosixPath(relative).is_absolute() or '\\' in relative or ':' in relative
                or any(part in {'', '.', '..'} for part in relative.split('/'))):
            raise ValueError('FIXED_INTERACTION_PATH_REJECTED')
        html, spec = self._read_html(relative), self._read_spec(self._target)
        plan = interaction_plan(self._runtime, self._conditions, repository_ref=self._authority.repository_ref,
            scope_ref=self._authority.scope_ref, identity_digest=self._authority.current_identity.envelope_digest,
            target=self._target, prepare_sha256=selected['prepare_sha256'], dependency_sha256=item['dependency_sha256'],
            input_sha256=item['prototype_sha256'], spec_sha256=item['spec_sha256'], sequence=self._sequence)
        _checked_sequence(html, spec, plan)
        if (self._adoption(self._runtime) is not True or not self.control_fence()
                or canonical(provided) != provided_bytes):
            raise ProviderUnavailable('current interactive adoption unavailable')
        return _InteractionMaterial(canonical(item), html, spec, plan)

    def _request(self):
        return {'profile_id': PROFILE_ID, 'targets': ['/'.join(self._target)]}

    def prepare(self, data):
        if data != self._request() or type(data) is not dict:
            raise ValueError('INVALID_FIXED_INTERACTION_SELECTION')
        material = self._interaction_material()
        plan = material.plan
        # Recovery compares the original plan against its JSON journal image.
        plan_data = json.loads(canonical(asdict(plan)))
        return PreparedLocalTool(plan_data, plan.binding(), 'interaction-' + plan.plan_digest, material)

    def revalidate(self, prepared):
        try:
            return (type(prepared) is PreparedLocalTool and type(prepared.provider_data) is _InteractionMaterial
                    and self.prepare(self._request()) == prepared)
        except (OSError, TypeError, ValueError):
            return False

    def execute(self, prepared, grant, authority):
        if authority is not self._authority or not self.revalidate(prepared):
            raise ProviderUnavailable('fixed interactive context changed')
        material = prepared.provider_data
        provider = LinuxInteractiveRenderProvider(self._runtime, authority=authority, grant=grant,
                                                  adoption_validator=self._adoption)
        captures = render_interaction(provider, material.html, material.spec, self._conditions, material.plan)
        if not self.revalidate(prepared):
            raise ProviderUnavailable('fixed interactive context changed')
        facts = self._encode_facts(material, captures)
        metadata = {'id': prepared.source_ref, 'ref': prepared.source_ref, 'run_id': prepared.source_ref,
            'producer_ref': PROFILE_ID, 'identity': identity_as_dict(authority.current_identity),
            'required_components': list(self._required), 'kind': 'interactive-render', 'status': 'DECLARED',
            'checked_at': self._clock().isoformat(), 'scope_ref': authority.scope_ref,
            'tool_version': self._runtime.browser_product, 'rules_digest': None, 'database_digest': None,
            'coverage': 'COMPLETE_FOR_DECLARED_SCOPE', 'coverage_gaps': [],
            'summary': 'Observed fixed prototype interaction and completed process cleanup'}
        source_binding(metadata, facts)
        if not self.control_fence():
            raise ProviderUnavailable('fixed interactive context changed')
        return LocalToolCompletion(metadata, facts)

    def _encode_facts(self, material, captures):
        plan = material.plan
        steps = _checked_sequence(material.html, material.spec, plan).steps
        if tuple(capture.state for capture in captures) != tuple(step.state for step in steps if step.action == 'capture'):
            raise ValueError('INTERACTION_STATES_INCOMPLETE')
        encoded = []
        for item in captures:
            if (type(item) is not InteractionCapture or type(item.png) is not bytes
                    or not 1 <= len(item.png) <= MAX_PROFILE_PNG_BYTES
                    or hashlib.sha256(item.png).hexdigest() != item.sha256):
                raise ValueError('INVALID_INTERACTION_CAPTURE')
            validate_rendered_png(item.png, self._conditions)
            encoded.append({'state': item.state, 'sha256': item.sha256,
                            'png_base64': base64.b64encode(item.png).decode('ascii')})
        result = {'target': list(plan.target), 'prepare_sha256': plan.prepare_sha256,
            'dependency_sha256': plan.dependency_sha256, 'input_sha256': plan.input_sha256,
            'spec_sha256': plan.spec_sha256, 'runtime_sha256': plan.runtime_sha256, 'plan_sha256': plan.plan_digest,
            'conditions': self._conditions.checked(), 'policy_version': plan.policy_version,
            'scope_digest': json.loads(material.item)['scope_digest'],
            'sequence': plan.sequence, 'actions': [step.action for step in steps],
            'captures': encoded, 'cleanup': 'COMPLETE'}
        # Leave room for the existing journal and source envelope.
        if len(canonical(result)) > 1_900_000:
            raise ValueError('INTERACTION_OUTPUT_LIMIT')
        return result

    def read_result(self, *, expected_item=None, source_ref=None):
        try:
            expected = canonical(expected_item) if expected_item is not None else None
            prepared = self.prepare(self._request())
            material = prepared.provider_data
            if ((expected is not None and expected != material.item)
                    or (source_ref is not None and source_ref != prepared.source_ref)):
                return None
            observation = self._source.lookup(prepared.source_ref)
            if observation is None:
                return None
            metadata, facts = observation.metadata, observation.facts
            if (metadata['status'] != 'VERIFIED' or metadata['kind'] != 'interactive-render'
                    or metadata['ref'] != prepared.source_ref or metadata['producer_ref'] != PROFILE_ID
                    or metadata['scope_ref'] != self._authority.scope_ref
                    or metadata['identity'] != identity_as_dict(self._authority.current_identity)
                    or metadata['required_components'] != list(self._required)
                    or metadata['coverage'] != 'COMPLETE_FOR_DECLARED_SCOPE' or metadata['coverage_gaps']
                    or timestamp(metadata['checked_at']) > self._clock()
                    or type(facts.get('captures')) is not list or not 1 <= len(facts['captures']) <= 32):
                return None
            captures = tuple(InteractionCapture(item['state'], base64.b64decode(item['png_base64'], validate=True),
                item['sha256']) for item in facts['captures'])
            if self._encode_facts(material, captures) != facts or not self.revalidate(prepared):
                return None
            current = self._source.lookup(prepared.source_ref)
            if current != observation:
                return None
            receipt = self._authority.read_provenance(observation.provenance_ref,
                expected_identity_digest=metadata['identity']['envelope_digest'])
            if (receipt is None or receipt['binding'] != source_binding({**metadata, 'status': 'DECLARED'}, facts)
                    or receipt['event']['kind'] != 'tool-completed' or receipt['event']['operation'] != 'source'
                    or receipt['event']['actor'] != PROFILE_ID or not self.control_fence()
                    or (expected is not None and canonical(expected_item) != expected)):
                return None
            return observation
        except (OSError, TypeError, ValueError, KeyError, AttributeError):
            return None

    def read_preview_result(self, *, expected_item):
        observation = self.read_result(expected_item=expected_item)
        if observation is None:
            return None
        facts = observation.facts
        capture = next(row for row in facts['captures'] if row['state'] == self._target[2])
        return RenderResult('RENDERED', 'INTERACTION_CAPTURE_COMPLETE', facts['input_sha256'],
            base64.b64decode(capture['png_base64'], validate=True), capture['sha256'], facts['conditions'],
            False, ('The observed prototype state still requires maintainer acceptance',),
            facts['runtime_sha256'], facts['plan_sha256'])
