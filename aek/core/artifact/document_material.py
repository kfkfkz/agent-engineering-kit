"""Bounded material comparisons; a comparison never grants approval."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


FORMAT_RULE_VERSION = "markdown-format-v1"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class MaterialComparison:
    classification: str
    reason_code: str
    before_sha256: str
    after_sha256: str
    rule_version: str
    content_sha256: str | None = None


def _ordinary_prose(line: str) -> bool:
    return (bool(line) and line[0].isalpha() and not any(c.isdigit() for c in line)
            and not any(c in line for c in "`~#>|[](){}*_\\/=:<：")
            and not line.endswith(("  ", "\\"))
            and not any(word in line.casefold() for word in
                        ("签核", "确认人", "评审人", "sha256", "digest", "approved", "reviewer")))


def _closed_inline(line: str) -> bool:
    """Reject ambiguous cross-line spans rather than guess their extent."""
    delimiter = 0
    for match in re.finditer(r"`+", line):
        length = len(match.group())
        if not delimiter:
            delimiter = length
        elif length == delimiter:
            delimiter = 0
    if delimiter or line.count("*") % 2 or line.count("_") % 2:
        return False
    for opening, closing in (("[", "]"), ("(", ")")):
        depth = 0
        for char in line:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth < 0:
                    return False
        if depth:
            return False
    return True


def _projection(text: str) -> bytes | None:
    # Markdown line boundaries are LF/CRLF, never Python's other Unicode
    # splitlines separators. Keep the final line terminator, if any.
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    projected: list[str] = []
    ordinary: list[bool] = []
    fence_char, fence_size = "", 0
    container = False
    for line in lines:
        if line.endswith("\r\n"):
            body, ending = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, ending = line[:-1], "\n"
        else:
            body, ending = line, ""
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", body)
        if fence_char:
            if (fence and fence.group(1)[0] == fence_char
                    and len(fence.group(1)) >= fence_size
                    and not fence.group(2).strip(" \t")):
                fence_char, fence_size = "", 0
            projected.append(line)
            ordinary.append(False)
            continue
        if fence:
            if fence.group(1)[0] == "`" and "`" in fence.group(2):
                return None
            fence_char, fence_size = fence.group(1)[0], len(fence.group(1))
            projected.append(line)
            ordinary.append(False)
            continue
        if body == "<!-- aek-review-boundary: declared-id-v1 -->":
            projected.append(line)
            ordinary.append(False)
            continue
        # Unknown HTML and multiline inline spans must not expose false prose.
        if (re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", body)
                # Reference-link titles may span following physical lines.
                # v1 does not prove that boundary, even if quotes balance on
                # the definition line or the title starts on the next line.
                or re.match(r"^ {0,3}\[.*\]:", body)
                or any(c in body for c in "~<>") or not _closed_inline(body)):
            return None
        if not body:
            container = False
        elif re.match(r"^(?:[ \t]|[-+*>]|[0-9]+[.)])", body) or "|" in body:
            container = True
        # Lists/quotes can have lazy, unindented continuation lines. Only a
        # genuine empty separator lets a later top-level paragraph qualify.
        safe = not container and _ordinary_prose(body)
        if safe and ending:
            ending = "\n"
        projected.append(body + ending)
        ordinary.append(safe)
    if fence_char:
        return None
    end = len(projected)
    while end and projected[end - 1] in ("\n", "\r\n"):
        end -= 1
    if end and ordinary[end - 1]:
        projected = projected[:end]
    return "".join(projected).encode("utf-8")


def compare_material(before: bytes, after: bytes, *,
                     rule_version: str = FORMAT_RULE_VERSION) -> MaterialComparison:
    """Compare immutable document bytes, not roles, approval or dependencies."""
    if (type(before) is not bytes or type(after) is not bytes
            or not 1 <= len(before) <= MAX_DOCUMENT_BYTES
            or not 1 <= len(after) <= MAX_DOCUMENT_BYTES
            or type(rule_version) is not str):
        raise ValueError("document material must be bounded immutable bytes")
    before_sha = hashlib.sha256(before).hexdigest()
    after_sha = hashlib.sha256(after).hexdigest()
    def result(classification, reason, content=None):
        return MaterialComparison(classification, reason, before_sha, after_sha,
                                  rule_version, content)
    if rule_version != FORMAT_RULE_VERSION:
        return result("UNKNOWN", "UNKNOWN_RULE")
    try:
        old_text, new_text = before.decode("utf-8"), after.decode("utf-8")
    except UnicodeDecodeError:
        return result("UNKNOWN", "INVALID_UTF8")
    unsupported_control = any(
        (ord(c) < 32 and c not in "\r\n\t") or
        (c == "\r" and (i + 1 == len(text) or text[i + 1] != "\n"))
        for text in (old_text, new_text) for i, c in enumerate(text))
    if before == after:
        projected = None if unsupported_control else _projection(old_text)
        content = before if projected is None else projected
        return result("EXACT", "RAW_UNCHANGED", hashlib.sha256(content).hexdigest())
    if unsupported_control:
        return result("UNKNOWN", "UNSUPPORTED_CONTROL")
    old, new = _projection(old_text), _projection(new_text)
    if old is None or new is None:
        return result("UNKNOWN", "UNSUPPORTED_STRUCTURE")
    if old == new:
        return result("EQUIVALENT", "SUPPORTED_FORMAT_ONLY", hashlib.sha256(old).hexdigest())
    return result("REVIEW_REQUIRED", "CONTENT_CHANGED")
