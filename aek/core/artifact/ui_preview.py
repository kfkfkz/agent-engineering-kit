"""Pure static-preview inputs; content validity never grants browser execution."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import math
import re
import struct
from typing import Protocol
import zlib

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_PHYSICAL_PIXELS = 16 * 1024 * 1024
MAX_PREVIEW_REPORT_BYTES = 32 * 1024 * 1024


class PreviewRejected(ValueError):
    pass


class SelfContainedHtml(HTMLParser):
    """Shared conservative validator for the existing Gate and renderer."""
    _URL_ATTRS = {"src", "srcset", "imagesrcset", "attributionsrc", "poster", "data", "action", "formaction",
                  "background", "ping", "lowsrc", "dynsrc", "longdesc", "codebase", "archive", "manifest"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self._style_depth = 0

    @staticmethod
    def _safe_data_url(value: str) -> bool:
        return bool(re.match(r"(?i)^data:image/(?:png|jpeg|gif|webp);base64,", value.strip()))

    @staticmethod
    def _unsafe_css(value: str) -> bool:
        # Reject comments rather than stripping them inside quoted CSS strings.
        if "/*" in value or "*/" in value:
            return True
        value = re.sub(r"\\(?:\r\n|\n|\r|\f)", "", value)
        def unescape(match: re.Match[str]) -> str:
            try:
                return chr(int(match.group(1), 16))
            except (ValueError, OverflowError):
                return " "
        value = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", unescape, value)
        value = re.sub(r"\\([^\r\n])", r"\1", value)
        return bool(re.search(r"(?i)url\s*\(|@import", value))

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name.lower(): (value or "") for name, value in attrs}
        if len(attrs_map) != len(attrs):
            self.errors.append(f"duplicate HTML attribute: {tag}")
        if any(name.startswith("on") for name in attrs_map):
            self.errors.append("event-handler attributes are not allowed")
        if any(name in {"xlink:href", "xml:base"} for name in attrs_map):
            self.errors.append("namespaced resource references are not allowed")
        if tag.lower() in {"iframe", "frame", "object", "embed", "applet", "portal", "base", "form"}:
            self.errors.append(f"forbidden resource element: {tag}")
        if tag.lower() == "script":
            self.errors.append("script execution is disabled in prototype previews")
        if tag.lower() == "meta" and attrs_map.get("http-equiv", "").lower() == "refresh":
            self.errors.append("automatic navigation is not allowed in prototype previews")
        for name in self._URL_ATTRS:
            value = attrs_map.get(name)
            if value is None or not value.strip():
                continue
            if name == "src" and tag.lower() in {"img", "source"} and self._safe_data_url(value):
                continue
            self.errors.append(f"non-self-contained {name} attribute")
        href = attrs_map.get("href")
        if href and tag.lower() != "a":
            self.errors.append("non-self-contained href attribute")
        elif href and tag.lower() == "a" and not href.startswith("#"):
            self.errors.append("external navigation is not allowed in prototype preview")
        style = attrs_map.get("style", "")
        if style and self._unsafe_css(style):
            self.errors.append("CSS resource reference is not allowed")
        if tag.lower() == "script" and "src" in attrs_map:
            self.errors.append("external script is not allowed")

    def handle_starttag(self, tag, attrs):
        self._attrs(tag, attrs)
        if tag.lower() == 'style':
            self._style_depth += 1

    def handle_startendtag(self, tag, attrs):
        self._attrs(tag, attrs)

    def handle_endtag(self, tag):
        if tag.lower() == 'style' and self._style_depth:
            self._style_depth -= 1

    def handle_data(self, data):
        if self._style_depth and self._unsafe_css(data):
            self.errors.append('CSS resource reference is not allowed')


@dataclass(frozen=True)
class RenderConditions:
    width: int = 1200
    height: int = 800
    dpr: float = 1
    theme: str = 'light'

    def checked(self) -> dict:
        if (type(self.width) is not int or type(self.height) is not int
                or not 64 <= self.width <= 4096 or not 64 <= self.height <= 4096
                or type(self.dpr) not in (int, float) or not math.isfinite(self.dpr)
                or not 0.5 <= self.dpr <= 3 or type(self.theme) is not str
                or self.theme not in ('light', 'dark')):
            raise PreviewRejected('invalid render conditions')
        # DPR may be fractional, but requested physical dimensions must be exact.
        width, height = self.width * self.dpr, self.height * self.dpr
        if int(width) != width or int(height) != height or width * height > MAX_PHYSICAL_PIXELS:
            raise PreviewRejected('render pixel budget exceeded or fractional physical dimensions')
        return {'width': self.width, 'height': self.height, 'dpr': self.dpr, 'theme': self.theme}


def validate_html(raw: bytes, expected_sha256: str) -> str:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_HTML_BYTES:
        raise PreviewRejected('HTML snapshot must be bounded immutable bytes')
    if (type(expected_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise PreviewRejected('HTML snapshot digest mismatch')
    try:
        text = raw.decode('utf-8', errors='strict')
        parser = SelfContainedHtml()
        parser.feed(text)
        parser.close()
    except (UnicodeError, ValueError, RecursionError):
        raise PreviewRejected('HTML snapshot cannot be parsed as UTF-8') from None
    if parser.errors:
        raise PreviewRejected('unsafe static HTML snapshot')
    return text


@dataclass(frozen=True)
class RenderResult:
    status: str
    reason_code: str
    input_sha256: str | None = None
    png: bytes | None = field(default=None, repr=False)
    output_sha256: str | None = None
    conditions: dict | None = None
    comparable: bool = False
    unknowns: tuple[str, ...] = ()
    runtime_sha256: str | None = None
    plan_sha256: str | None = None


class RendererPort(Protocol):
    def render(self, snapshot: bytes, conditions: RenderConditions, approved_plan, *,
               expected_sha256: str) -> RenderResult: ...


@dataclass(frozen=True)
class PreviewReference:
    """Caller-validated reference PNG; this record does not certify its source."""
    evidence_id: str
    context_id: str
    png: bytes = field(repr=False)
    sha256: str


@dataclass(frozen=True)
class PreviewItem:
    target: tuple[str, str, str, str]
    references: tuple[PreviewReference, ...] = ()
    render: RenderResult | None = None


@dataclass(frozen=True)
class PreviewReport:
    html: bytes = field(repr=False)
    sha256: str
    prepare_sha256: str


def build_static_preview(selection: dict, items: list[PreviewItem]) -> PreviewReport:
    """Project a current prepared selection; never execute or approve its HTML."""
    def text(value: object) -> str:
        return escape(str(value), quote=True)
    image_output_bytes = 0
    def encoded_image(raw: bytes) -> str:
        nonlocal image_output_bytes
        image_output_bytes += 4 * ((len(raw) + 2) // 3)
        if image_output_bytes > MAX_PREVIEW_REPORT_BYTES:
            raise PreviewRejected('static preview report byte budget exceeded')
        return base64.b64encode(raw).decode('ascii')
    if (type(selection) is not dict or set(selection) != {
            'schema_version', 'status', 'per_item', 'prepare_sha256'}
            or type(selection['schema_version']) is not int or selection['schema_version'] != 2
            or selection['status'] != 'PREPARED' or type(selection['per_item']) is not list
            or not 1 <= len(selection['per_item']) <= 32):
        raise PreviewRejected('invalid prepared preview selection')
    fields = {'page_id', 'area_id', 'state_id', 'context_id', 'prototype_path',
              'prototype_sha256', 'dependency_sha256', 'scope_digest', 'evidence_contexts'}
    for row in selection['per_item']:
        if (type(row) is not dict or set(row) != fields
                or any(type(row[key]) is not str or len(row[key]) > 500
                       for key in fields - {'evidence_contexts'})
                or not re.fullmatch(r'PAGE-[1-9][0-9]*', row['page_id'])
                or not re.fullmatch(r'AREA-[1-9][0-9]*', row['area_id'])
                or row['state_id'] not in {'default', 'loading', 'empty', 'error', 'dialog', 'drawer', 'permission'}
                or not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', row['context_id'])
                or any(not re.fullmatch(r'[0-9a-f]{64}', row[key]) for key in
                       ('prototype_sha256', 'dependency_sha256', 'scope_digest'))
                or type(row['evidence_contexts']) is not list or not 1 <= len(row['evidence_contexts']) <= 256):
            raise PreviewRejected('invalid prepared preview member')
        identifiers = set()
        for ref in row['evidence_contexts']:
            if (type(ref) is not dict or set(ref) != {'evidence_id', 'contexts'}
                    or type(ref['evidence_id']) is not str or not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', ref['evidence_id'])
                    or ref['evidence_id'] in identifiers
                    or type(ref['contexts']) is not list or not 1 <= len(ref['contexts']) <= 256
                    or any(type(value) is not dict or set(value) != {
                        'context_id', 'permissions', 'system_version', 'viewport', 'relation'}
                        or value['context_id'] != row['context_id']
                        or (value['permissions'] is not None and (
                            type(value['permissions']) is not dict
                            or set(value['permissions']) != {'function_id', 'data_id'}
                            or any(type(part) is not str or not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', part)
                                   for part in value['permissions'].values())))
                        or any(value[key] is not None and (type(value[key]) is not str or len(value[key]) > 128)
                               for key in ('system_version', 'viewport'))
                        or value['relation'] not in ('captured', 'shared_layout', None)
                        for value in ref['contexts'])
                    or len({value['context_id'] for value in ref['contexts']}) != len(ref['contexts'])):
                raise PreviewRejected('invalid prepared preview evidence')
            identifiers.add(ref['evidence_id'])
    raw = json.dumps({key: selection[key] for key in ('schema_version', 'status', 'per_item')},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    prepare_sha256 = selection['prepare_sha256']
    if (len(raw) > 48 * 1024 or type(prepare_sha256) is not str
            or hashlib.sha256(raw).hexdigest() != prepare_sha256):
        raise PreviewRejected('prepared preview digest mismatch or budget exceeded')
    # Only these digest-checked bytes drive output. Hashing image bytes below
    # may release the GIL; caller-owned lists/dicts are not an immutable input.
    selection = json.loads(raw)
    selection['prepare_sha256'] = prepare_sha256
    rows = {tuple(row[key] for key in ('page_id', 'area_id', 'state_id', 'context_id')): row
            for row in selection['per_item']}
    if len(rows) != len(selection['per_item']) or list(rows) != sorted(rows):
        raise PreviewRejected('prepared preview targets must be unique and ordered')
    if (type(items) not in (list, tuple) or not 1 <= len(items) <= 32
            or any(type(item) is not PreviewItem or type(item.target) is not tuple
                   or len(item.target) != 4 or any(type(part) is not str for part in item.target)
                   for item in items)):
        raise PreviewRejected('invalid preview selection')
    by_target = {item.target: item for item in items}
    if len(by_target) != len(items) or set(by_target) != set(rows):
        raise PreviewRejected('preview items must match the exact prepared selection')
    sections = []
    for target in rows:
        item = by_target[target]
        row = rows[item.target]
        if type(item.references) is not tuple or len(item.references) > 256:
            raise PreviewRejected('invalid preview references')
        allowed = {ref['evidence_id']: {context['context_id']: context for context in ref['contexts']}
                   for ref in row['evidence_contexts']}
        context_details = []
        for ident, contexts in allowed.items():
            context = contexts[target[3]]
            context_details.append('<p>证据上下文：' + text(ident + ' / ' + target[3])
                + '；权限=' + text(json.dumps(context['permissions'], ensure_ascii=False, sort_keys=True)
                                  if context['permissions'] is not None else '未知')
                + '；版本=' + text(context['system_version'] or '未知')
                + '；视口=' + text(context['viewport'] or '未知')
                + '；关系=' + text(context['relation'] or '未知') + '</p>')
        seen, references = set(), []
        for ref in item.references:
            if (type(ref) is not PreviewReference or type(ref.evidence_id) is not str
                    or type(ref.context_id) is not str or ref.context_id != target[3]
                    or ref.context_id not in allowed.get(ref.evidence_id, ())
                    or ref.evidence_id in seen
                    or type(ref.png) is not bytes or not 1 <= len(ref.png) <= MAX_PNG_BYTES
                    or not ref.png.startswith(b'\x89PNG\r\n\x1a\n')
                    or type(ref.sha256) is not str or hashlib.sha256(ref.png).hexdigest() != ref.sha256):
                raise PreviewRejected('reference bytes, evidence or context do not match')
            # The original screenshot adapter owns full PNG decoding. This is
            # an additional display budget, not a replacement source validator.
            if (len(ref.png) < 33 or ref.png[8:16] != b'\0\0\0\rIHDR'
                    or zlib.crc32(ref.png[12:29]) != struct.unpack('>I', ref.png[29:33])[0]):
                raise PreviewRejected('reference PNG header is invalid')
            width, height = struct.unpack('>II', ref.png[16:24])
            if not width or not height or width * height > MAX_PHYSICAL_PIXELS:
                raise PreviewRejected('reference PNG display pixel budget exceeded')
            seen.add(ref.evidence_id)
            references.append('<figure><figcaption>参考：' + text(ref.evidence_id + ' / ' + ref.context_id)
                + '；物理像素：' + text(width) + ' × ' + text(height)
                + '；实际 SHA-256：<code>' + text(ref.sha256) + '</code></figcaption>'
                '<img loading="lazy" alt="参考图" src="data:image/png;base64,'
                + encoded_image(ref.png) + '"></figure>')
        if seen != set(allowed):
            references.append('<p>部分参考图未提供；未完成完整对照。</p>')
        rendered = '<p>渲染：UNAVAILABLE；尚未生成渲染图。</p>'
        if item.render is not None:
            result = item.render
            if (type(result) is not RenderResult or type(result.status) is not str
                    or result.status not in {'RENDERED', 'UNAVAILABLE', 'REJECTED', 'FAILED'}
                    or type(result.reason_code) is not str or len(result.reason_code) > 128
                    or type(result.unknowns) is not tuple or len(result.unknowns) > 16
                    or any(type(value) is not str or len(value) > 512 for value in result.unknowns)
                    or (result.input_sha256 is not None and result.input_sha256 != row['prototype_sha256'])):
                raise PreviewRejected('invalid preview render snapshot')
            complete = all(value is not None for value in (result.input_sha256, result.png,
                result.output_sha256, result.conditions, result.runtime_sha256, result.plan_sha256))
            if result.status != 'RENDERED':
                if result.png is not None or result.output_sha256 is not None:
                    raise PreviewRejected('failed render cannot publish an output image')
                rendered = '<p>渲染：' + text(result.status) + '；' + text(result.reason_code) + '</p>'
            elif not complete:
                rendered = '<p>渲染：UNAVAILABLE；渲染快照、条件或来源身份缺失，未完成对照。</p>'
            else:
                if type(result.conditions) is not dict or set(result.conditions) != {'width', 'height', 'dpr', 'theme'}:
                    raise PreviewRejected('invalid preview render conditions')
                conditions = RenderConditions(**result.conditions)
                checked = conditions.checked()
                validate_rendered_png(result.png, conditions)
                if (result.input_sha256 != row['prototype_sha256'] or type(result.output_sha256) is not str
                        or hashlib.sha256(result.png).hexdigest() != result.output_sha256
                        or any(type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value)
                               for value in (result.runtime_sha256, result.plan_sha256))):
                    raise PreviewRejected('render snapshot digest or identity mismatch')
                rendered = ('<p>渲染：RENDERED（快照数据，不认证工具来源）</p>'
                    '<p>条件：' + text(checked['width']) + ' × ' + text(checked['height'])
                    + '；DPR=' + text(checked['dpr']) + '；主题=' + text(checked['theme']) + '</p>'
                    '<p>输出 PNG SHA-256：<code>' + text(result.output_sha256) + '</code></p>'
                    '<p>运行时 SHA-256：<code>' + text(result.runtime_sha256) + '</code></p>'
                    '<p>计划 SHA-256：<code>' + text(result.plan_sha256) + '</code></p>'
                    '<img loading="lazy" alt="原型渲染图" src="data:image/png;base64,'
                    + encoded_image(result.png) + '">'
                    + ''.join('<p>未核实：' + text(value) + '</p>' for value in result.unknowns))
        sections.append('<section><h2>' + text(' / '.join(item.target)) + '</h2>'
            '<p>原型人工打开路径：<code>' + text(row['prototype_path']) + '</code></p>'
            '<p>输入 HTML SHA-256：<code>' + text(row['prototype_sha256']) + '</code></p>'
            '<p>依赖 SHA-256：<code>' + text(row['dependency_sha256']) + '</code></p>'
            '<p>作用域 SHA-256：<code>' + text(row['scope_digest']) + '</code></p>'
            '<p>可比性：未知/未核实，不提供自动视觉结论。</p>'
            '<p>参考图采集视口、DPR、主题、字体及动态数据：未知/未核实。</p>'
            + ''.join(context_details)
            + '<div class="comparison"><div><h3>原型渲染</h3>' + rendered
            + '</div><div><h3>当前参考</h3>' + ''.join(references) + '</div></div></section>')
    html = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        'img-src data:; style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        '<title>aek 静态状态对照</title><style>body{font:16px sans-serif;max-width:1200px;margin:auto;'
        'padding:1em}section{border:1px solid #888;padding:1em;margin:1em 0}code{overflow-wrap:anywhere}'
        'img{max-width:100%;height:auto}figure{margin:1em 0}'
        '.comparison{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1em}'
        '@media(max-width:700px){.comparison{grid-template-columns:1fr}}'
        '</style></head><body><h1>aek 静态状态对照</h1>'
        '<p>调用方仍须核实源端及渲染来源；本报告不是工具可信证明或人工确认。</p>'
        '<p>图像及报告同级敏感；不自动上传或入库。内容适用性、当前性及人工确认仍由原门禁核实。</p>'
        '<p>准备 SHA-256：<code>' + text(selection['prepare_sha256']) + '</code></p>'
        + ''.join(sections) + '</body></html>').encode('utf-8')
    if len(html) > MAX_PREVIEW_REPORT_BYTES:
        raise PreviewRejected('static preview report byte budget exceeded')
    return PreviewReport(html, hashlib.sha256(html).hexdigest(), selection['prepare_sha256'])


def validate_rendered_png(raw: bytes, conditions: RenderConditions) -> None:
    """Accept bounded RGB/RGBA browser PNGs, including CRC and decode length."""
    if type(conditions) is not RenderConditions:
        raise PreviewRejected('invalid render conditions')
    conditions.checked()
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_PNG_BYTES or raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise PreviewRejected('invalid rendered PNG')
    expected_width = int(conditions.width * conditions.dpr)
    expected_height = int(conditions.height * conditions.dpr)
    offset, header, ended, idat_ended = 8, None, False, False
    compressed, count = [], 0
    while offset < len(raw):
        count += 1
        if count > 4096 or len(raw) - offset < 12:
            raise PreviewRejected('invalid rendered PNG framing')
        size = struct.unpack('>I', raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        if not re.fullmatch(rb'[A-Za-z]{2}[A-Z][A-Za-z]', kind):
            raise PreviewRejected('invalid rendered PNG chunk type')
        end = offset + 12 + size
        if end > len(raw):
            raise PreviewRejected('truncated rendered PNG')
        body = raw[offset + 8:end - 4]
        if zlib.crc32(kind + body) != struct.unpack('>I', raw[end - 4:end])[0]:
            raise PreviewRejected('rendered PNG CRC mismatch')
        if header is None:
            if kind != b'IHDR' or size != 13:
                raise PreviewRejected('missing rendered PNG header')
            header = struct.unpack('>IIBBBBB', body)
            if (header[:2] != (expected_width, expected_height) or header[2] != 8
                    or header[3] not in (2, 6) or header[4:] != (0, 0, 0)):
                raise PreviewRejected('unsupported rendered PNG conditions')
        elif kind == b'IHDR':
            raise PreviewRejected('duplicate rendered PNG header')
        elif kind == b'IDAT':
            if idat_ended:
                raise PreviewRejected('noncontiguous rendered PNG data')
            compressed.append(body)
        elif kind == b'IEND':
            if size or end != len(raw):
                raise PreviewRejected('invalid rendered PNG end')
            ended = True
        else:
            if compressed:
                idat_ended = True
            if kind != b'PLTE' and (not kind[0] & 32):
                raise PreviewRejected('unknown critical rendered PNG chunk')
        offset = end
    if not ended or not compressed:
        raise PreviewRejected('incomplete rendered PNG')
    stride = expected_width * (3 if header[3] == 2 else 4) + 1
    expected = stride * expected_height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b''.join(compressed), expected + 1)
        if (len(pixels) != expected or not decoder.eof or decoder.unconsumed_tail
                or decoder.unused_data or any(value > 4 for value in pixels[::stride])):
            raise PreviewRejected('invalid rendered PNG pixel stream')
    except zlib.error:
        raise PreviewRejected('invalid rendered PNG compression') from None
