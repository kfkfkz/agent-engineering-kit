#!/usr/bin/env python3
"""Static preview output is derived data, never a visual approval."""
import copy
from dataclasses import replace
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import sys
import struct
import unittest
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aek.core.artifact.ui_preview import (
    PreviewItem, PreviewReference, PreviewRejected, build_static_preview,
    RenderConditions, RenderResult,
)


def png(palette=False):
    def chunk(kind, raw):
        return struct.pack('>I', len(raw)) + kind + raw + struct.pack('>I', zlib.crc32(kind + raw))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 3 if palette else 2, 0, 0, 0))
            + (chunk(b'PLTE', b'\xff\0\0') if palette else b'')
            + chunk(b'IDAT', zlib.compress((b'\0' + (b'\0' if palette else b'\xff\0\0') * 64) * 64))
            + chunk(b'IEND', b''))


def selection(*targets):
    targets = targets or [('PAGE-1', 'AREA-1', 'default', 'CTX-1')]
    rows = [dict(zip(('page_id', 'area_id', 'state_id', 'context_id'), target),
                 prototype_path='ui-prototypes/中文<&>.html', prototype_sha256='1' * 64,
                 dependency_sha256='2' * 64, scope_digest='3' * 64,
                 evidence_contexts=[{'evidence_id': 'UI-EV-001', 'contexts': [{
                     'context_id': target[3], 'permissions': {'function_id': 'FP01', 'data_id': 'DP01'},
                     'system_version': None, 'viewport': None, 'relation': 'captured'}]}])
            for target in sorted(targets)]
    result = {'schema_version': 2, 'status': 'PREPARED', 'per_item': rows}
    result['prepare_sha256'] = hashlib.sha256(json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return result


class StaticPreviewTests(unittest.TestCase):
    def test_report_uses_verified_private_preparation_snapshot_after_image_hash_io(self):
        prepared = selection()
        original_sha = prepared['prepare_sha256']
        raw = png()
        real_sha256 = hashlib.sha256
        ref = PreviewReference('UI-EV-001', 'CTX-1', raw, real_sha256(raw).hexdigest())
        def concurrent_change(value=b''):
            if value is raw:
                prepared['per_item'][0]['prototype_path'] = 'unverified-later-path.html'
                prepared['prepare_sha256'] = '0' * 64
            return real_sha256(value)
        with patch('aek.core.artifact.ui_preview.hashlib.sha256', side_effect=concurrent_change):
            report = build_static_preview(prepared, [PreviewItem(
                ('PAGE-1', 'AREA-1', 'default', 'CTX-1'), references=(ref,))])
        self.assertNotIn(b'unverified-later-path', report.html)
        self.assertIn('中文&lt;&amp;&gt;.html', report.html.decode('utf-8'))
        self.assertEqual(report.prepare_sha256, original_sha)
        self.assertIn(original_sha.encode(), report.html)

    def test_actual_doc_gate_preparation_keeps_original_p1_evidence_identity(self):
        path = Path(__file__).with_name('ui-prototype-v2.test.py')
        spec = importlib.util.spec_from_file_location('preview_report_v2_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = module.PrototypeV2Tests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = fixture.f
        raw = png()
        digest = hashlib.sha256(raw).hexdigest()
        row = original.index['screenshots'][0]
        (original.material / row['path']).write_bytes(raw)
        row['validation'].update(content_sha256=digest, bytes=len(raw))
        row['applies_to'][0]['content_review']['reviewed_sha256'] = digest
        row['applies_to'][0]['freshness']['confirmed_sha256'] = digest
        fixture.save()
        target = ('PAGE-1', 'AREA-1', 'default', original.context_id)
        prepared = module.data.GATE['_prepare_prototype_selection'](original.req, [target])
        self.assertEqual(prepared['per_item'][0]['evidence_contexts'], [
            {'evidence_id': 'P1', 'contexts': [{'context_id': original.context_id,
                'permissions': {'function_id': 'FP01', 'data_id': 'DP01'},
                'system_version': None, 'viewport': None, 'relation': 'captured'}]}])
        report = build_static_preview(prepared, [PreviewItem(target, references=(
            PreviewReference('P1', original.context_id, raw, digest),))])
        self.assertIn(('P1 / ' + original.context_id).encode(), report.html)
        self.assertIn(prepared['prepare_sha256'].encode(), report.html)

    def test_each_state_pairs_its_render_and_reference_in_independent_columns(self):
        raw = png()
        sha = hashlib.sha256(raw).hexdigest()
        rendered = RenderResult('RENDERED', 'STATIC_CAPTURE_COMPLETE', '1' * 64, raw, sha,
            RenderConditions(64, 64).checked(), False, (), '4' * 64, '5' * 64)
        reference = PreviewReference('UI-EV-001', 'CTX-1', raw, sha)
        text = build_static_preview(selection(), [PreviewItem(
            ('PAGE-1', 'AREA-1', 'default', 'CTX-1'), (reference,), rendered)]).html.decode('utf-8')
        self.assertEqual(text.count('class="comparison"'), 1)
        self.assertIn('<h3>原型渲染</h3>', text)
        self.assertIn('<h3>当前参考</h3>', text)
        self.assertEqual(text.count('<img loading="lazy"'), 2)

    def test_report_escapes_untrusted_labels_and_has_no_executable_or_external_element(self):
        prepared = selection()
        prepared['per_item'][0]['prototype_path'] = 'https://example.invalid/"><script>alert(1)</script>.html'
        del prepared['prepare_sha256']
        prepared['prepare_sha256'] = hashlib.sha256(json.dumps(
            prepared, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []
            def handle_starttag(self, tag, attrs):
                self.elements.append((tag, dict(attrs)))
        report = build_static_preview(prepared, [PreviewItem(('PAGE-1', 'AREA-1', 'default', 'CTX-1'))])
        text = report.html.decode('utf-8')
        parser = Elements()
        parser.feed(text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('作用域 SHA-256：<code>' + '3' * 64, text)
        self.assertFalse({'script', 'iframe', 'object', 'embed', 'a', 'link', 'form'} & {tag for tag, _ in parser.elements})
        self.assertTrue(all(not any(name.startswith('on') or name in {'href', 'src'} for name in attrs)
                            for _, attrs in parser.elements))
        self.assertIn("default-src &#39;none&#39;", text)

    def test_mixed_render_snapshot_and_unbounded_or_unsafe_metadata_are_rejected(self):
        raw = png()
        complete = RenderResult('RENDERED', 'STATIC_CAPTURE_COMPLETE', '1' * 64, raw,
            hashlib.sha256(raw).hexdigest(), RenderConditions(64, 64).checked(), False, (), '4' * 64, '5' * 64)
        invalid = (replace(complete, input_sha256='0' * 64), replace(complete, output_sha256='0' * 64),
                   replace(complete, conditions=RenderConditions(128, 64).checked()),
                   replace(complete, unknowns=('x' * 513,)), replace(complete, status={}),
                   replace(complete, status='FAILED'), replace(complete, png=bytearray(raw)))
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(PreviewRejected):
                build_static_preview(selection(), [PreviewItem(
                    ('PAGE-1', 'AREA-1', 'default', 'CTX-1'), render=result)])

    def test_total_report_budget_applies_before_repeated_image_expansion(self):
        # Valid ancillary PNG metadata makes file bytes large without allocating
        # a giant decoded image. Every selected item remains independently bound.
        body = b'fixture\0' + b'x' * (7 * 1024 * 1024)
        ancillary = struct.pack('>I', len(body)) + b'tEXt' + body + struct.pack('>I', zlib.crc32(b'tEXt' + body))
        raw = png()[:-12] + ancillary + png()[-12:]
        ref = PreviewReference('UI-EV-001', 'CTX-1', raw, hashlib.sha256(raw).hexdigest())
        targets = [(f'PAGE-{n}', 'AREA-1', 'default', 'CTX-1') for n in range(1, 5)]
        with self.assertRaises(PreviewRejected):
            build_static_preview(selection(*targets), [PreviewItem(target, references=(ref,)) for target in targets])

    def test_reference_display_bounds_ihdr_pixels_without_requiring_render_rgb_format(self):
        target = ('PAGE-1', 'AREA-1', 'default', 'CTX-1')
        raw = png(palette=True)
        reference = PreviewReference('UI-EV-001', 'CTX-1', raw, hashlib.sha256(raw).hexdigest())
        self.assertIn(b'data:image/png', build_static_preview(selection(), [
            PreviewItem(target, references=(reference,))]).html)
        oversized = bytearray(raw)
        oversized[16:24] = struct.pack('>II', 65536, 65536)
        oversized[29:33] = struct.pack('>I', zlib.crc32(bytes(oversized[12:29])))
        for value in (raw[:8], bytes(oversized)):
            invalid = PreviewReference('UI-EV-001', 'CTX-1', value, hashlib.sha256(value).hexdigest())
            with self.subTest(size=len(value)), self.assertRaises(PreviewRejected):
                build_static_preview(selection(), [PreviewItem(target, references=(invalid,))])

    def test_render_status_without_complete_capture_data_is_unavailable(self):
        for result in (RenderResult('RENDERED', 'STATIC_CAPTURE_COMPLETE'),
                       RenderResult('UNAVAILABLE', 'APPROVED_PROVIDER_UNAVAILABLE'),
                       RenderResult('FAILED', 'RENDER_EXECUTION_FAILED'),
                       RenderResult('REJECTED', 'INVALID_STATIC_PREVIEW')):
            with self.subTest(status=result.status):
                text = build_static_preview(selection(), [PreviewItem(
                    ('PAGE-1', 'AREA-1', 'default', 'CTX-1'), render=result)]).html.decode('utf-8')
                self.assertIn('UNAVAILABLE' if result.status == 'RENDERED' else result.status, text)
                self.assertNotIn('data:image/png', text)
                self.assertIn('可比性：未知/未核实', text)

    def test_rendered_snapshot_shows_actual_conditions_and_digest_not_approval(self):
        raw = png()
        sha = hashlib.sha256(raw).hexdigest()
        rendered = RenderResult('RENDERED', 'STATIC_CAPTURE_COMPLETE', '1' * 64, raw, sha,
            RenderConditions(64, 64).checked(), True, ('字体差异 <unknown>',), '4' * 64, '5' * 64)
        text = build_static_preview(selection(), [PreviewItem(
            ('PAGE-1', 'AREA-1', 'default', 'CTX-1'), render=rendered)]).html.decode('utf-8')
        self.assertIn('RENDERED', text)
        self.assertIn('64 × 64', text)
        self.assertIn('DPR=1', text)
        self.assertIn(sha, text)
        self.assertIn('4' * 64, text)
        self.assertIn('5' * 64, text)
        self.assertIn('字体差异 &lt;unknown&gt;', text)
        self.assertIn('可比性：未知/未核实', text)
        self.assertIn('调用方仍须核实源端及渲染来源', text)
        self.assertNotIn('ACCEPTED', text)

    def test_verified_reference_bytes_are_bound_to_selected_evidence_and_context(self):
        raw = png()
        sha = hashlib.sha256(raw).hexdigest()
        prepared = selection()
        target = ('PAGE-1', 'AREA-1', 'default', 'CTX-1')
        reference = PreviewReference('UI-EV-001', 'CTX-1', raw, sha)
        result = build_static_preview(prepared, [PreviewItem(target, references=(reference,))])
        text = result.html.decode('utf-8')
        self.assertIn('data:image/png;base64,', text)
        self.assertIn('UI-EV-001 / CTX-1', text)
        self.assertIn(sha, text)
        for invalid in (PreviewReference('UI-EV-001', 'CTX-2', raw, sha),
                        PreviewReference('UI-EV-002', 'CTX-1', raw, sha),
                        PreviewReference('UI-EV-001', 'CTX-1', raw, '0' * 64),
                        PreviewReference('UI-EV-001', 'CTX-1', bytearray(raw), sha)):
            with self.subTest(reference=invalid), self.assertRaises(PreviewRejected):
                build_static_preview(prepared, [PreviewItem(target, references=(invalid,))])
        with self.assertRaises(PreviewRejected):
            build_static_preview(prepared, [PreviewItem(target, references=(reference, reference))])

    def test_selection_is_exact_not_collapsed_or_expanded_by_preview_items(self):
        targets = [('PAGE-1', 'AREA-1', 'default', 'CTX-1'),
                   ('PAGE-1', 'AREA-1', 'empty', 'CTX-1'),
                   ('PAGE-1', 'AREA-1', 'default', 'CTX-2')]
        prepared = selection(*targets)
        items = [PreviewItem(target) for target in targets]
        for invalid in (items[:-1], items + [items[0]], items + [
                PreviewItem(('PAGE-2', 'AREA-1', 'default', 'CTX-1'))]):
            with self.subTest(items=invalid), self.assertRaises(PreviewRejected):
                build_static_preview(prepared, invalid)
        text = build_static_preview(prepared, list(reversed(items))).html.decode('utf-8')
        for target in targets:
            self.assertEqual(text.count('<h2>' + ' / '.join(target) + '</h2>'), 1)

    def test_preparation_digest_and_bounded_current_schema_are_required(self):
        prepared = selection()
        item = PreviewItem(('PAGE-1', 'AREA-1', 'default', 'CTX-1'))
        invalid = []
        for key, value in (('schema_version', True), ('status', 'ACCEPTED'),
                           ('prepare_sha256', '0' * 64), ('extra', 'unselected data')):
            altered = copy.deepcopy(prepared)
            altered[key] = value
            invalid.append(altered)
        duplicate = copy.deepcopy(prepared)
        duplicate['per_item'].append(copy.deepcopy(duplicate['per_item'][0]))
        invalid += [duplicate, selection(*[(f'PAGE-{n}', 'AREA-1', 'default', 'CTX-1')
                                         for n in range(1, 34)])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PreviewRejected):
                build_static_preview(value, [item])

    def test_prepared_selection_is_utf8_escaped_and_never_claims_rendering(self):
        prepared = selection()
        original = copy.deepcopy(prepared)
        result = build_static_preview(prepared, [PreviewItem(('PAGE-1', 'AREA-1', 'default', 'CTX-1'))])
        self.assertIs(type(result.html), bytes)
        text = result.html.decode('utf-8')
        self.assertIn('中文&lt;&amp;&gt;.html', text)
        self.assertIn('UNAVAILABLE', text)
        self.assertIn('PAGE-1 / AREA-1 / default / CTX-1', text)
        self.assertIn('1' * 64, text)
        self.assertIn('2' * 64, text)
        self.assertIn(prepared['prepare_sha256'], text)
        self.assertEqual(result.sha256, hashlib.sha256(result.html).hexdigest())
        self.assertEqual(result.prepare_sha256, prepared['prepare_sha256'])
        self.assertEqual(prepared, original)
        self.assertNotIn('ACCEPTED', text)
        self.assertNotIn('<iframe', text)
        self.assertNotIn('<script', text)


if __name__ == '__main__':
    unittest.main()
