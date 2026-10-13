#!/usr/bin/env python3
"""Safe, local UI screenshot evidence helper for design-pipeline."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time
import unicodedata
import uuid
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


# Resolve only the source-kit or installed-kit package layout, never the caller's
# working directory or an unbounded parent search.
_KIT_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = (_KIT_ROOT.parent / ".repo-memory-kit" / "lib"
                 if _KIT_ROOT.name in {".agents", ".claude"} else _KIT_ROOT)
if not (_PACKAGE_ROOT / "aek" / "adapters" / "ui_filesystem.py").is_file():
    raise ImportError("UI filesystem adapter is missing from the managed kit layout")
sys.path.insert(0, str(_PACKAGE_ROOT))
from aek.adapters import ui_filesystem as fs


INDEX_NAME = "截图索引.json"
MATERIAL_PARTS = ("docs", "01-需求")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_PIXELS = 100_000_000
MAX_ROW_BYTES = 16 * 1024 * 1024
ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
STATES = {"default", "loading", "empty", "error", "dialog", "drawer", "permission"}
DERIVED_SCREENSHOT_FIELDS = {
    "path", "validation", "placeholder_sha256", "content_sha256", "prototype_refs",
    "content_review", "freshness", "git_status",
}


class UiReferenceError(Exception):
    pass


def fail(message: str) -> None:
    raise UiReferenceError(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json_stdin() -> Any:
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            fail("request exceeds 2 MiB")
        return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid UTF-8 JSON: {exc}")


def _keys(value: Any, required: set[str], optional: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{where} must be an object")
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing or extra:
        fail(f"{where} fields invalid (missing={sorted(missing)}, unknown={sorted(extra)})")
    return value


def _text(value: Any, where: str, *, maximum: int = 500, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or (not allow_empty and not value.strip()) or "\x00" in value):
        fail(f"{where} must be a non-empty string up to {maximum} characters")
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        fail(f"{where} must match {ID_RE.pattern}")
    return value


def _requirement_dir(value: Any) -> str:
    """Directory identity is a basename, independently of machine IDs."""
    name = _text(value, "requirement_dir", maximum=180)
    if (name in {".", ".."} or name[-1] in {".", " "}
            or unicodedata.normalize("NFC", name) != name
            or any(ch in name for ch in '/\\:<>"|?*')
            or any(ord(ch) < 32 for ch in name)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name)):
        fail("requirement_dir must be a safe single directory basename")
    return name


def _validate_requirement_identity(change_id: str, requirement_dir: str) -> None:
    numbered = re.fullmatch(r"([0-9]{3})-.+", requirement_dir)
    if numbered and change_id != "CHG-" + numbered.group(1):
        fail("numbered requirement_dir must map to change_id CHG-NNN")


def _ids(value: Any, where: str, prefix: str | None = None) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)):
        fail(f"{where} must be a non-empty list of unique IDs")
    result = [_id(item, where) for item in value]
    if prefix and any(not re.fullmatch(prefix + r"[1-9][0-9]*", item) for item in result):
        fail(f"{where} must contain {prefix} IDs")
    return result


def _viewport(value: Any, where: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", value):
        fail(f"{where} must look like 1440x900")
    width, height = (int(item) for item in value.split("x"))
    if width * height > MAX_PIXELS:
        fail(f"{where} exceeds the pixel limit")
    return value


def _parse_context(value: Any, captured: dict[str, Any], where: str) -> dict[str, Any]:
    item = _keys(value, {"context_id", "permissions", "relation"},
                 {"system_version", "viewport"}, where)
    context_id = _id(item["context_id"], f"{where}.context_id")
    permissions = _keys(item["permissions"], {"function_id", "data_id"}, set(),
                        f"{where}.permissions")
    permissions = {key: _id(val, f"{where}.permissions.{key}") for key, val in permissions.items()}
    relation = item["relation"]
    if relation not in {"captured", "shared_layout"}:
        fail(f"{where}.relation must be captured or shared_layout")
    result: dict[str, Any] = {
        "context_id": context_id, "permissions": permissions, "relation": relation,
        "content_review": {"status": "unverified"}, "freshness": {"status": "unknown"},
    }
    if "system_version" in item:
        result["system_version"] = _id(item["system_version"], f"{where}.system_version")
    if "viewport" in item:
        result["viewport"] = _viewport(item["viewport"], f"{where}.viewport")
    if relation == "captured":
        if permissions != captured["permissions"]:
            fail(f"{where}: captured permissions do not match screenshot capture")
        if result.get("system_version") != captured.get("system_version"):
            fail(f"{where}: captured system_version does not match screenshot")
        if result.get("viewport") != captured.get("viewport"):
            fail(f"{where}: captured viewport does not match screenshot")
    return result


def validate_request(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = _keys(value, {"schema_version", "change_id", "design_system", "coverage", "screenshots"}, {"requirement_dir"}, "request")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        fail("schema_version must be integer 1")
    change_id = _id(root["change_id"], "change_id")
    ds = _keys(root["design_system"], {"name", "source", "evidence"}, {"version", "theme"}, "design_system")
    design_system = {"name": _text(ds["name"], "design_system.name"),
                     "source": ds["source"], "evidence": ds["evidence"]}
    if design_system["source"] not in {"user", "codebase", "unknown"}:
        fail("design_system.source must be user, codebase, or unknown")
    if not isinstance(ds["evidence"], list) or any(not isinstance(x, str) or len(x) > 500 for x in ds["evidence"]):
        fail("design_system.evidence must be a list of bounded strings")
    if "version" in ds:
        design_system["version"] = _text(ds["version"], "design_system.version", maximum=100)
    if "theme" in ds:
        design_system["theme"] = _text(ds["theme"], "design_system.theme", maximum=100)

    screenshots_in = root["screenshots"]
    if not isinstance(screenshots_in, list) or not screenshots_in:
        fail("screenshots must be a non-empty list")
    screenshots: list[dict[str, Any]] = []
    screenshot_ids: set[str] = set()
    feature_ids: set[str] = set()
    acceptance_ids: set[str] = set()
    paths: set[str] = set()
    for number, raw in enumerate(screenshots_in, 1):
        where = f"screenshots[{number}]"
        item = _keys(raw, {"id", "page", "state", "purpose", "feature_ids", "acceptance_ids", "permissions"},
                     {"system_version", "viewport", "applies_to"}, where)
        screenshot_id = _id(item["id"], f"{where}.id")
        if screenshot_id in screenshot_ids:
            fail(f"duplicate screenshot id: {screenshot_id}")
        screenshot_ids.add(screenshot_id)
        if item["state"] not in STATES:
            fail(f"{where}.state is unsupported")
        permissions_raw = _keys(item["permissions"], {"function_id", "data_id"}, set(), f"{where}.permissions")
        permissions = {key: _id(val, f"{where}.permissions.{key}") for key, val in permissions_raw.items()}
        feature_list = _ids(item["feature_ids"], f"{where}.feature_ids", "F")
        acceptance_list = _ids(item["acceptance_ids"], f"{where}.acceptance_ids", "A")
        feature_ids.update(feature_list)
        acceptance_ids.update(acceptance_list)
        row: dict[str, Any] = {
            "id": screenshot_id,
            "page": _text(item["page"], f"{where}.page", maximum=120),
            "state": item["state"],
            "purpose": _text(item["purpose"], f"{where}.purpose"),
            "feature_ids": feature_list,
            "acceptance_ids": acceptance_list,
            "permissions": permissions,
        }
        if "system_version" in item:
            row["system_version"] = _id(item["system_version"], f"{where}.system_version")
        if "viewport" in item:
            row["viewport"] = _viewport(item["viewport"], f"{where}.viewport")
        path = PurePosixPath("function", permissions["function_id"], "data", permissions["data_id"])
        if "system_version" in row:
            path /= PurePosixPath("version", row["system_version"])
        if "viewport" in row:
            path /= PurePosixPath("viewport", row["viewport"])
        filename = screenshot_id + ("" if item["state"] == "default" else "-" + item["state"]) + ".png"
        row["path"] = (path / filename).as_posix()
        if row["path"] in paths:
            fail(f"duplicate screenshot path: {row['path']}")
        paths.add(row["path"])
        row["placeholder_sha256"] = hashlib.sha256(b"").hexdigest()
        row["validation"] = {"status": "pending"}
        row["prototype_refs"] = []
        captured = {"permissions": permissions}
        if "system_version" in row:
            captured["system_version"] = row["system_version"]
        if "viewport" in row:
            captured["viewport"] = row["viewport"]
        contexts = [{"context_id": screenshot_id + "-CAPTURED", **captured, "relation": "captured"}]
        if "applies_to" in item:
            if not isinstance(item["applies_to"], list):
                fail(f"{where}.applies_to must be a list")
            contexts.extend(item["applies_to"])
        normalized_contexts = [_parse_context(ctx, captured, f"{where}.applies_to") for ctx in contexts]
        context_ids = [ctx["context_id"] for ctx in normalized_contexts]
        if len(context_ids) != len(set(context_ids)):
            fail(f"{where}.applies_to has duplicate context_id")
        row["applies_to"] = normalized_contexts
        screenshots.append(row)

    coverage_in = root["coverage"]
    if not isinstance(coverage_in, list) or not coverage_in:
        fail("coverage must be a non-empty list")
    coverage: list[dict[str, Any]] = []
    covered: set[tuple[str, str]] = set()
    for number, raw in enumerate(coverage_in, 1):
        where = f"coverage[{number}]"
        if not isinstance(raw, dict):
            fail(f"{where} must be an object")
        kind = raw.get("kind")
        prefix = "F" if kind == "feature" else "A" if kind == "acceptance" else None
        if prefix is None:
            fail(f"{where}.kind must be feature or acceptance")
        if "screenshot_ids" in raw:
            item = _keys(raw, {"kind", "id", "screenshot_ids"}, set(), where)
            ident = _id(item["id"], f"{where}.id")
            if not re.fullmatch(prefix + r"[1-9][0-9]*", ident):
                fail(f"{where}.id must be {prefix} ID")
            refs = _ids(item["screenshot_ids"], f"{where}.screenshot_ids", "P")
            if not set(refs) <= screenshot_ids:
                fail(f"{where}.screenshot_ids references an unknown screenshot")
            coverage.append({"kind": kind, "id": ident, "screenshot_ids": refs})
        else:
            item = _keys(raw, {"kind", "id", "na_reason"}, set(), where)
            ident = _id(item["id"], f"{where}.id")
            if not re.fullmatch(prefix + r"[1-9][0-9]*", ident):
                fail(f"{where}.id must be {prefix} ID")
            coverage.append({"kind": kind, "id": ident, "na_reason": _text(item["na_reason"], f"{where}.na_reason")})
        key = (kind, ident)
        if key in covered:
            fail(f"duplicate coverage row: {kind}:{ident}")
        covered.add(key)
    expected_coverage = ({("feature", item) for item in feature_ids}
                         | {("acceptance", item) for item in acceptance_ids})
    if not expected_coverage <= covered:
        fail(f"coverage missing screenshot-referenced IDs: {sorted(expected_coverage - covered)}")
    screenshot_map = {row["id"]: row for row in screenshots}
    for row in coverage:
        if "screenshot_ids" not in row:
            continue
        field = "feature_ids" if row["kind"] == "feature" else "acceptance_ids"
        for screenshot_id in row["screenshot_ids"]:
            if row["id"] not in screenshot_map[screenshot_id][field]:
                fail(f"coverage {row['kind']} {row['id']} is not declared by screenshot {screenshot_id}")
    normalized = {"schema_version": 1, "change_id": change_id,
                  "design_system": design_system, "coverage": coverage,
                  "screenshots": screenshots}
    if "requirement_dir" in root:
        normalized["requirement_dir"] = _requirement_dir(root["requirement_dir"])
        _validate_requirement_identity(change_id, normalized["requirement_dir"])
    return normalized, screenshots


_open_absolute_dir = fs.open_absolute_dir
_open_child_dir = fs.open_child_dir


@contextlib.contextmanager
def _material_parent(repo_fd: Any, change_id: str, *, create: bool) -> Iterator[tuple[Any, Any]]:
    current = fs.dup_dir(repo_fd)
    try:
        for part in (*MATERIAL_PARTS, change_id, "原始材料"):
            child = _open_child_dir(current, part, create=create)
            fs.close_dir(current)
            current = child
        yield current, fs.dup_dir(current)
    finally:
        fs.close_dir(current)


def _read_fd(fd: int, limit: int) -> bytes:
    try:
        return fs.read_fd(fd, limit)
    except ValueError as exc:
        fail(str(exc))


def _read_at(dir_fd: Any, name: str, limit: int) -> bytes:
    fd = fs.open_file(dir_fd, name, os.O_RDONLY)
    try:
        return _read_fd(fd, limit)
    finally:
        os.close(fd)


def _write_exclusive(dir_fd: Any, name: str, data: bytes, mode: int = 0o600) -> None:
    fd = fs.open_file(dir_fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_rename_noreplace = fs.rename_noreplace
_exclusive_lock = fs.exclusive_lock


def _index_bytes(index: dict[str, Any]) -> bytes:
    return (json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_stored_index(index: Any, change_id: str, requirement_dir: str) -> dict[str, Any]:
    root = _keys(index, {"schema_version", "change_id", "design_system", "coverage", "screenshots"}, {"requirement_dir"}, "index")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1 or root["change_id"] != change_id:
        fail("index schema_version or change_id is invalid")
    if _requirement_dir(root.get("requirement_dir", root["change_id"])) != requirement_dir:
        fail("index requirement_dir does not match its actual requirement directory")
    if not isinstance(root["screenshots"], list):
        fail("index screenshots must be a list")
    source_rows: list[dict[str, Any]] = []
    for number, row in enumerate(root["screenshots"], 1):
        where = f"index.screenshots[{number}]"
        _keys(row, {"id", "page", "state", "purpose", "feature_ids", "acceptance_ids", "permissions",
                    "path", "placeholder_sha256", "validation", "prototype_refs", "applies_to"},
              {"system_version", "viewport", "git_status", "git_staging", "git_warning"}, where)
        src = {key: row[key] for key in ("id", "page", "state", "purpose", "feature_ids", "acceptance_ids", "permissions")}
        for key in ("system_version", "viewport"):
            if key in row:
                src[key] = row[key]
        contexts = row["applies_to"]
        if not isinstance(contexts, list) or not contexts:
            fail(f"{where}.applies_to must be a non-empty list")
        src_contexts = []
        for context in contexts:
            _keys(context, {"context_id", "permissions", "relation", "content_review", "freshness"},
                  {"system_version", "viewport"}, where + ".applies_to[]")
            static = {key: context[key] for key in ("context_id", "permissions", "relation")}
            for key in ("system_version", "viewport"):
                if key in context:
                    static[key] = context[key]
            src_contexts.append(static)
            review = context["content_review"]
            if not isinstance(review, dict) or review.get("status") not in {"unverified", "applicable", "not_applicable"}:
                fail(f"{where}.content_review is invalid")
            if review["status"] == "unverified":
                _keys(review, {"status"}, set(), where + ".content_review")
            else:
                _keys(review, {"status", "reviewer_type", "reviewer", "reviewed_at", "reviewed_sha256", "rationale"}, set(), where + ".content_review")
                if (review["reviewer_type"] not in {"agent_visual", "maintainer"}
                        or not DIGEST_RE.fullmatch(str(review["reviewed_sha256"]))):
                    fail(f"{where}.content_review evidence is invalid")
                _text(review["reviewer"], where + ".content_review.reviewer", maximum=120)
                _parse_time(review["reviewed_at"], where + ".content_review.reviewed_at")
                _text(review["rationale"], where + ".content_review.rationale", maximum=1000)
            freshness = context["freshness"]
            if not isinstance(freshness, dict) or freshness.get("status") not in {"unknown", "confirmed_current", "stale"}:
                fail(f"{where}.freshness is invalid")
            if freshness["status"] == "unknown":
                _keys(freshness, {"status"}, set(), where + ".freshness")
            else:
                _keys(freshness, {"status", "confirmer_type", "confirmer", "confirmed_at", "confirmed_sha256"}, set(), where + ".freshness")
                if (freshness["confirmer_type"] not in {"agent_visual", "maintainer"}
                        or not DIGEST_RE.fullmatch(str(freshness["confirmed_sha256"]))):
                    fail(f"{where}.freshness evidence is invalid")
                _text(freshness["confirmer"], where + ".freshness.confirmer", maximum=120)
                _parse_time(freshness["confirmed_at"], where + ".freshness.confirmed_at")
        # The captured context is derived from the screenshot capture fields;
        # only explicit logical reuse targets belong in the reconstructed request.
        src["applies_to"] = [ctx for ctx in src_contexts if ctx["relation"] == "shared_layout"]
        source_rows.append(src)
        if not isinstance(row["placeholder_sha256"], str) or not DIGEST_RE.fullmatch(row["placeholder_sha256"]):
            fail(f"{where}.placeholder_sha256 is invalid")
        validation = row["validation"]
        if not isinstance(validation, dict) or validation.get("status") not in {"pending", "valid", "invalid"}:
            fail(f"{where}.validation is invalid")
        if validation["status"] == "pending":
            _keys(validation, {"status"}, {"reason", "content_sha256", "bytes"}, where + ".validation")
        elif validation["status"] == "invalid":
            _keys(validation, {"status", "reason"}, {"content_sha256", "bytes"}, where + ".validation")
            _text(validation["reason"], where + ".validation.reason", maximum=500)
        else:
            _keys(validation, {"status", "bytes", "width", "height", "content_sha256", "validated_at"}, set(), where + ".validation")
            if (type(validation["bytes"]) is not int or not 1 <= validation["bytes"] <= MAX_FILE_BYTES
                    or type(validation["width"]) is not int or type(validation["height"]) is not int
                    or not DIGEST_RE.fullmatch(str(validation["content_sha256"]))):
                fail(f"{where}.validation metrics are invalid")
            _parse_time(validation["validated_at"], where + ".validation.validated_at")
        if row.get("git_status", "unknown") not in {"tracked", "untracked", "unknown"}:
            fail(f"{where}.git_status is invalid")
        if row.get("git_staging", "unknown") not in {"added", "modified", "renamed", "copied", "deleted", "unmerged", "none", "unknown"}:
            fail(f"{where}.git_staging is invalid")
        if "git_warning" in row:
            _text(row["git_warning"], where + ".git_warning", maximum=500)
        if not isinstance(row["prototype_refs"], list):
            fail(f"{where}.prototype_refs must be a list")
        for ref in row["prototype_refs"]:
            _keys(ref, {"prototype_path", "prototype_sha256", "page_id", "area_id", "feature_ids",
                        "acceptance_ids", "change_intent", "depends_on", "artifact_plan_id",
                        "gate_digest", "approved_root"}, set(), where + ".prototype_refs[]")
            if not DIGEST_RE.fullmatch(str(ref["prototype_sha256"])) or not DIGEST_RE.fullmatch(str(ref["gate_digest"])):
                fail(f"{where}.prototype_refs digest is invalid")
            _id(ref["page_id"], where + ".prototype_refs.page_id")
            _id(ref["area_id"], where + ".prototype_refs.area_id")
            _text(ref["artifact_plan_id"], where + ".prototype_refs.artifact_plan_id", maximum=120)
            _text(ref["prototype_path"], where + ".prototype_refs.prototype_path", maximum=500)
            _text(ref["approved_root"], where + ".prototype_refs.approved_root", maximum=300)
            _text(ref["change_intent"], where + ".prototype_refs.change_intent", maximum=1000)
            _ids(ref["feature_ids"], where + ".prototype_refs.feature_ids", "F")
            _ids(ref["acceptance_ids"], where + ".prototype_refs.acceptance_ids", "A")
            if not isinstance(ref["depends_on"], list) or not ref["depends_on"]:
                fail(f"{where}.prototype_refs.depends_on must be a non-empty list")
    request = {"schema_version": 1, "change_id": change_id,
               "design_system": root["design_system"], "coverage": root["coverage"],
               "screenshots": source_rows}
    if "requirement_dir" in root:
        request["requirement_dir"] = root["requirement_dir"]
    expected, _ = validate_request(request)
    for actual, derived in zip(root["screenshots"], expected["screenshots"]):
        for key in ("id", "page", "state", "purpose", "feature_ids", "acceptance_ids",
                    "permissions", "system_version", "viewport", "path", "placeholder_sha256"):
            if actual.get(key) != derived.get(key):
                fail(f"index contains tampered or inconsistent derived field: screenshots.{key}")
        for ctx, expected_ctx in zip(actual["applies_to"], derived["applies_to"]):
            for key in ("context_id", "permissions", "system_version", "viewport", "relation"):
                if ctx.get(key) != expected_ctx.get(key):
                    fail("index contains inconsistent applies_to context")
    return root


def _replace_index(root_fd: Any, data: bytes) -> None:
    temp_name = ".index-tmp-" + uuid.uuid4().hex
    _write_exclusive(root_fd, temp_name, data)
    try:
        fs.replace(root_fd, temp_name, root_fd, INDEX_NAME)
        fs.fsync_dir(root_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            fs.unlink(root_fd, temp_name)
        raise


def _load_index(root_fd: Any, change_id: str, requirement_dir: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_at(root_fd, INDEX_NAME, 8 * 1024 * 1024)
    try:
        index = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"截图索引无法解析: {exc}")
    return _validate_stored_index(index, change_id, requirement_dir), raw


def _png_validate_fd(fd: int, file_size: int) -> tuple[int, int]:
    """Validate PNG with bounded chunk and scanline buffers; caller owns fd."""
    if file_size > MAX_FILE_BYTES:
        fail("PNG exceeds 200 MiB")
    os.lseek(fd, 0, os.SEEK_SET)

    def read_exact(length: int) -> bytes:
        output = bytearray()
        while len(output) < length:
            block = os.read(fd, min(64 * 1024, length - len(output)))
            if not block:
                fail("truncated PNG")
            output.extend(block)
        return bytes(output)

    if read_exact(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
        fail("not a PNG signature")
    consumed = len(PNG_SIGNATURE)
    ihdr: tuple[int, int, int, int, int] | None = None
    seen_idat = False
    ended_idat = False
    seen_iend = False
    palette_entries = 0
    transparency = False
    decompressor: zlib.decompressobj | None = None
    row_size = row_fill = rows = channels = 0
    color = depth = 0

    def accept_output(piece: bytes) -> None:
        nonlocal row_fill, rows
        offset = 0
        while offset < len(piece):
            need = row_size + 1 - row_fill
            count = min(need, len(piece) - offset)
            if row_fill == 0 and piece[offset] not in {0, 1, 2, 3, 4}:
                fail("invalid PNG scanline filter")
            row_fill += count
            offset += count
            if row_fill == row_size + 1:
                rows += 1
                if ihdr is None or rows > ihdr[1]:
                    fail("PNG decompressed stream exceeds declared image")
                row_fill = 0

    def inflate(block: bytes) -> None:
        assert decompressor is not None
        if decompressor.eof and block:
            fail("data follows PNG zlib stream")
        pending = block
        while True:
            limit = row_size + 1 - row_fill
            try:
                piece = decompressor.decompress(pending, limit)
            except zlib.error as exc:
                fail(f"PNG zlib stream invalid: {exc}")
            pending = decompressor.unconsumed_tail
            accept_output(piece)
            if decompressor.eof and (pending or decompressor.unused_data):
                fail("data follows PNG zlib stream")
            if decompressor.eof:
                break
            if pending:
                continue
            if len(piece) == limit:
                # Drain any output already buffered by zlib, still bounded to one row.
                pending = b""
                block = b""
                continue
            break

    while consumed < file_size:
        if file_size - consumed < 12:
            fail("truncated PNG chunk")
        header = read_exact(8)
        consumed += 8
        size = struct.unpack_from(">I", header, 0)[0]
        kind = header[4:8]
        if size + 4 > file_size - consumed:
            fail("truncated PNG chunk payload")
        if ihdr is None and kind != b"IHDR":
            fail("IHDR must be the first PNG chunk")
        crc = zlib.crc32(kind)
        if kind == b"IHDR":
            if ihdr is not None or size != 13:
                fail("duplicate or invalid IHDR")
            payload = read_exact(size)
            crc = zlib.crc32(payload, crc)
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            combos = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (width < 1 or height < 1 or width * height > MAX_PIXELS
                    or color not in combos or depth not in combos[color]
                    or compression != 0 or filtering != 0 or interlace != 0):
                fail("unsupported or unsafe IHDR dimensions/format")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            row_size = (width * channels * depth + 7) // 8
            if row_size > MAX_ROW_BYTES:
                fail("PNG row exceeds 16 MiB")
            ihdr = (width, height, depth, color, interlace)
            decompressor = zlib.decompressobj()
        elif kind == b"PLTE":
            if seen_idat or palette_entries or color in {0, 4} or size == 0 or size % 3 or size > 768:
                fail("invalid PLTE placement or length")
            payload = read_exact(size)
            crc = zlib.crc32(payload, crc)
            palette_entries = size // 3
            if color == 3 and palette_entries > (1 << depth):
                fail("PLTE contains more entries than indexed bit depth allows")
        elif kind == b"tRNS":
            if seen_idat or transparency or color not in {0, 2, 3}:
                fail("invalid tRNS placement or color type")
            if ((color == 0 and size != 2) or (color == 2 and size != 6)
                    or (color == 3 and (not palette_entries or size > palette_entries))):
                fail("invalid tRNS length")
            payload = read_exact(size)
            crc = zlib.crc32(payload, crc)
            transparency = True
        elif kind == b"IDAT":
            if ended_idat or (color == 3 and not palette_entries):
                fail("invalid IDAT order or missing indexed palette")
            seen_idat = True
            remaining = size
            while remaining:
                block = read_exact(min(64 * 1024, remaining))
                remaining -= len(block)
                crc = zlib.crc32(block, crc)
                inflate(block)
        else:
            if seen_idat:
                ended_idat = True
            if kind == b"IEND":
                if size != 0 or not seen_idat or seen_iend:
                    fail("invalid IEND")
            elif kind[:1].isupper():
                fail("unknown critical PNG chunk")
            remaining = size
            while remaining:
                block = read_exact(min(64 * 1024, remaining))
                remaining -= len(block)
                crc = zlib.crc32(block, crc)
        consumed += size
        expected_crc = struct.unpack(">I", read_exact(4))[0]
        consumed += 4
        if crc & 0xFFFFFFFF != expected_crc:
            fail("PNG chunk CRC mismatch")
        if kind == b"IEND":
            seen_iend = True
            if consumed != file_size or os.read(fd, 1):
                fail("trailing bytes after IEND")
            break
    if ihdr is None or not seen_idat or not seen_iend or decompressor is None:
        fail("PNG is missing a required chunk")
    if color == 3 and not palette_entries:
        fail("indexed PNG is missing PLTE")
    if row_fill != 0 or rows != ihdr[1] or not decompressor.eof or decompressor.unused_data:
        fail("PNG zlib/scanline stream is incomplete or has trailing data")
    return ihdr[0], ihdr[1]


def _validate_image_at(dir_fd: Any, name: str) -> tuple[bytes, int, int, int]:
    fd = fs.open_file(dir_fd, name, os.O_RDONLY)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            fail("image must be a regular file no larger than 200 MiB")
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        os.lseek(fd, 0, os.SEEK_SET)
        width, height = _png_validate_fd(fd, before.st_size)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            fail("image changed during validation; retry")
        return digest.digest(), width, height, before.st_size
    finally:
        os.close(fd)


def _git_status(repo_root: Path, relative_path: str) -> tuple[str, str]:
    # Literal pathspecs and optional-lock suppression make this diagnosis safe
    # for requirement names containing Git metacharacters and keep the index untouched.
    prefix = ["git", "--no-optional-locks", "--literal-pathspecs", "-C", str(repo_root)]
    try:
        result = subprocess.run([*prefix, "ls-files", "--error-unmatch", "--", relative_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=3, check=False)
        if result.returncode not in {0, 1}:
            return "unknown", "unknown"
        status = subprocess.run([*prefix, "status", "--porcelain=v1", "-z", "--untracked-files=normal",
                                 "--", relative_path], capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", "unknown"
    tracked = "tracked" if result.returncode == 0 else "untracked"
    if status.returncode != 0:
        return tracked, "unknown"
    if not status.stdout:
        return tracked, "none"
    code = status.stdout[:2].decode("ascii", errors="replace")
    if "U" in code or code in {"AA", "DD"}:
        staging = "unmerged"
    else:
        staging = {"A": "added", "M": "modified", "T": "modified", "R": "renamed",
                   "C": "copied", "D": "deleted", " ": "none", "?": "none"}.get(code[0], "unknown")
    return tracked, staging


def _material_root(repo_fd: Any, change_id: str, *, create: bool = False) -> tuple[Any, Any]:
    parent = fs.dup_dir(repo_fd)
    try:
        for part in (*MATERIAL_PARTS, change_id, "原始材料"):
            child = _open_child_dir(parent, part, create=create)
            fs.close_dir(parent)
            parent = child
        return parent, _open_child_dir(parent, "ui-reference", create=create)
    except BaseException:
        fs.close_dir(parent)
        raise


def _mkdir_path(root_fd: Any, relative: str) -> Any:
    current = fs.dup_dir(root_fd)
    try:
        for part in PurePosixPath(relative).parts:
            child = _open_child_dir(current, part, create=True)
            fs.close_dir(current)
            current = child
        return current
    except BaseException:
        fs.close_dir(current)
        raise


def _remove_tree_at(parent_fd: Any, name: str) -> None:
    """Remove only our private staging tree without following inserted links."""
    try:
        info = fs.stat_at(parent_fd, name)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        fs.unlink(parent_fd, name)
        return
    child_fd = _open_child_dir(parent_fd, name)
    try:
        for child in fs.listdir(child_fd):
            _remove_tree_at(child_fd, child)
    finally:
        fs.close_dir(child_fd)
    fs.rmdir(parent_fd, name)


def _create_staging(parent_fd: Any, index: dict[str, Any]) -> None:
    stage_name = ".kit-stage-" + uuid.uuid4().hex
    fs.mkdir(parent_fd, stage_name, 0o700)
    stage_fd = _open_child_dir(parent_fd, stage_name)
    try:
        for row in index["screenshots"]:
            dir_fd = _mkdir_path(stage_fd, str(PurePosixPath(row["path"]).parent))
            try:
                _write_exclusive(dir_fd, PurePosixPath(row["path"]).name, b"")
            finally:
                fs.close_dir(dir_fd)
        _write_exclusive(stage_fd, INDEX_NAME, _index_bytes(index))
        fs.fsync_dir(stage_fd)
    except BaseException:
        fs.close_dir(stage_fd)
        _remove_tree_at(parent_fd, stage_name)
        raise
    fs.close_dir(stage_fd)
    try:
        _rename_noreplace(parent_fd, stage_name, parent_fd, "ui-reference")
        fs.fsync_dir(parent_fd)
    except FileExistsError:
        try:
            existing_fd = _open_child_dir(parent_fd, "ui-reference")
            try:
                existing = _read_at(existing_fd, INDEX_NAME, 8 * 1024 * 1024)
            finally:
                fs.close_dir(existing_fd)
        finally:
            _remove_tree_at(parent_fd, stage_name)
        if existing == _index_bytes(index):
            return
        fail("material root already exists with a different or missing index; no files were overwritten")
    except BaseException:
        _remove_tree_at(parent_fd, stage_name)
        raise


def _scaffold(repo_root: Path, request: Any) -> dict[str, Any]:
    index, _ = validate_request(request)
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent = fs.dup_dir(repo_fd)
        try:
            for part in (*MATERIAL_PARTS, index.get("requirement_dir", index["change_id"]), "原始材料"):
                child = _open_child_dir(parent, part, create=True)
                fs.close_dir(parent)
                parent = child
            _create_staging(parent, index)
        finally:
            fs.close_dir(parent)
        return {"change_id": index["change_id"], "status": "created_or_identical",
                "screenshots": [{"id": row["id"], "path": row["path"], "status": "pending"}
                                for row in index["screenshots"]]}
    finally:
        fs.close_dir(repo_fd)


def _validate_command(repo_root: Path, change_id: str, requirement_dir: str | None = None) -> tuple[dict[str, Any], int]:
    change_id = _id(change_id, "change_id")
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else change_id)
    _validate_requirement_identity(change_id, requirement_dir)
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent_fd, root_fd = _material_root(repo_fd, requirement_dir)
        fs.close_dir(parent_fd)
        try:
            with _exclusive_lock(root_fd):
                index, old_raw = _load_index(root_fd, change_id, requirement_dir)
                result_rows = []
                pending = False
                for row in index["screenshots"]:
                    path = str(row.get("path", ""))
                    parts = PurePosixPath(path).parts
                    if not parts or path.startswith("/") or ".." in parts or "\\" in path:
                        fail("index contains an unsafe screenshot path")
                    image_dir = fs.dup_dir(root_fd)
                    try:
                        for part in parts[:-1]:
                            child = _open_child_dir(image_dir, part)
                            fs.close_dir(image_dir)
                            image_dir = child
                        try:
                            image_hash, width, height, file_bytes = _validate_image_at(image_dir, parts[-1])
                            content_hash = image_hash.hex()
                            if content_hash == row.get("placeholder_sha256"):
                                validation = {"status": "pending", "reason": "placeholder_unchanged",
                                              "content_sha256": content_hash, "bytes": file_bytes}
                                pending = True
                            else:
                                validation = {"status": "valid", "bytes": file_bytes, "width": width,
                                              "height": height, "content_sha256": content_hash,
                                              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                        except UiReferenceError as exc:
                            validation = {"status": "invalid", "reason": str(exc)}
                            pending = True
                        except FileNotFoundError:
                            validation = {"status": "invalid", "reason": "missing_file"}
                            pending = True
                    finally:
                        fs.close_dir(image_dir)
                    previous_hash = row.get("validation", {}).get("content_sha256")
                    if validation.get("content_sha256") != previous_hash:
                        for context in row.get("applies_to", []):
                            context["content_review"] = {"status": "unverified"}
                            context["freshness"] = {"status": "unknown"}
                    row["validation"] = validation
                    row["git_status"], row["git_staging"] = _git_status(
                        repo_root, "docs/01-需求/" + requirement_dir + "/原始材料/ui-reference/" + path)
                    if row["git_staging"] not in {"none", "unknown"}:
                        row["git_warning"] = "原始截图存在 Git 暂存变更（" + row["git_staging"] + "）；工具仅诊断，不改写 Git 索引或配置"
                    elif row["git_status"] == "tracked":
                        row["git_warning"] = "原始截图已由 Git 跟踪；工具不会改写 Git 状态"
                    else:
                        row.pop("git_warning", None)
                    if validation.get("status") != "valid":
                        pending = True
                    for ctx in row.get("applies_to", []):
                        review = ctx.get("content_review", {})
                        fresh = ctx.get("freshness", {})
                        if (review.get("status") != "applicable"
                                or review.get("reviewed_sha256") != validation.get("content_sha256")
                                or fresh.get("status") != "confirmed_current"
                                or fresh.get("confirmed_sha256") != validation.get("content_sha256")):
                            pending = True
                    result_rows.append({"id": row.get("id"), "path": path,
                                        "validation": validation["status"],
                                        "git_status": row["git_status"], "git_staging": row["git_staging"]})
                new_raw = _index_bytes(index)
                if new_raw != old_raw:
                    _replace_index(root_fd, new_raw)
                return ({"change_id": change_id, "status": "pending" if pending else "ready",
                         "screenshots": result_rows}, 1 if pending else 0)
        finally:
            fs.close_dir(root_fd)
    finally:
        fs.close_dir(repo_fd)


def _parse_time(value: Any, where: str) -> str:
    text = _text(value, where, maximum=40)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", text):
        fail(f"{where} must be an ISO-8601 timestamp with timezone")
    return text


def _update_index(repo_root: Path, change_id: str, expected_sha: str, payload: Any, operation: str,
                  requirement_dir: str | None = None) -> dict[str, Any]:
    change_id = _id(change_id, "change_id")
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else change_id)
    _validate_requirement_identity(change_id, requirement_dir)
    if not DIGEST_RE.fullmatch(expected_sha):
        fail("--expected-index-sha256 must be lowercase SHA-256")
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent, root_fd = _material_root(repo_fd, requirement_dir)
        fs.close_dir(parent)
        try:
            with _exclusive_lock(root_fd):
                index, old_raw = _load_index(root_fd, change_id, requirement_dir)
                if _sha(old_raw) != expected_sha:
                    fail("index CAS conflict: expected SHA-256 does not match current index")
                # Every update validates current referenced file bytes before mutation.
                for row in index["screenshots"]:
                    path = PurePosixPath(str(row.get("path", "")))
                    if path.is_absolute() or ".." in path.parts or "\\" in str(path):
                        fail("index contains an unsafe path")
                    fd = fs.dup_dir(root_fd)
                    try:
                        for part in path.parts[:-1]:
                            child = _open_child_dir(fd, part)
                            fs.close_dir(fd)
                            fd = child
                        image_fd = fs.open_file(fd, path.name, os.O_RDONLY)
                        try:
                            info = os.fstat(image_fd)
                            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                                fail("referenced screenshot is not a bounded regular file")
                            digest = hashlib.sha256()
                            while True:
                                block = os.read(image_fd, 1024 * 1024)
                                if not block:
                                    break
                                digest.update(block)
                            image_hash = digest.hexdigest()
                        finally:
                            os.close(image_fd)
                    finally:
                        fs.close_dir(fd)
                    if image_hash != row.get("validation", {}).get("content_sha256"):
                        fail("referenced screenshot changed; run validate and retry with its new index SHA")
                if operation == "record-review":
                    _keys(payload, {"schema_version", "updates"}, set(), operation)
                    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                        fail("record-review schema_version must be 1")
                    if not isinstance(payload["updates"], list) or not payload["updates"]:
                        fail("record-review updates must be a non-empty list")
                    for update in payload["updates"]:
                        _keys(update, {"screenshot_id", "context_id", "content_review", "freshness"}, set(), operation + ".update")
                        row = next((item for item in index["screenshots"] if item["id"] == update["screenshot_id"]), None)
                        if row is None:
                            fail("record-review references an unknown screenshot")
                        context = next((item for item in row["applies_to"] if item["context_id"] == update["context_id"]), None)
                        if context is None or row.get("validation", {}).get("status") != "valid":
                            fail("record-review target must be a validated screenshot context")
                        digest = row["validation"].get("content_sha256")
                        review = _keys(update["content_review"], {"status", "reviewer_type", "reviewer", "reviewed_at", "rationale"}, set(), "content_review")
                        if review["status"] not in {"applicable", "not_applicable"} or review["reviewer_type"] not in {"agent_visual", "maintainer"}:
                            fail("content_review status or reviewer_type is invalid")
                        context["content_review"] = {"status": review["status"], "reviewer_type": review["reviewer_type"],
                            "reviewer": _text(review["reviewer"], "reviewer", maximum=120),
                            "reviewed_at": _parse_time(review["reviewed_at"], "reviewed_at"),
                            "reviewed_sha256": digest,
                            "rationale": _text(review["rationale"], "rationale", maximum=1000)}
                        fresh = _keys(update["freshness"], {"status", "confirmer_type", "confirmer", "confirmed_at"}, set(), "freshness")
                        if fresh["status"] not in {"confirmed_current", "stale"} or fresh["confirmer_type"] not in {"agent_visual", "maintainer"}:
                            fail("freshness status or confirmer_type is invalid")
                        context["freshness"] = {"status": fresh["status"], "confirmer_type": fresh["confirmer_type"],
                            "confirmer": _text(fresh["confirmer"], "confirmer", maximum=120),
                            "confirmed_at": _parse_time(fresh["confirmed_at"], "confirmed_at"),
                            "confirmed_sha256": digest}
                else:
                    _keys(payload, {"schema_version", "artifact_plan_id", "gate_digest", "approved_root", "prototype_refs"}, set(), operation)
                    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                        fail("bind-prototype schema_version must be 1")
                    _text(payload["artifact_plan_id"], "artifact_plan_id", maximum=120)
                    if not isinstance(payload["gate_digest"], str) or not DIGEST_RE.fullmatch(payload["gate_digest"]):
                        fail("bind-prototype requires a verified gate_digest")
                    approved_root = PurePosixPath(_text(payload["approved_root"], "approved_root", maximum=300))
                    if approved_root.is_absolute() or ".." in approved_root.parts or "\\" in str(approved_root):
                        fail("approved_root must be a safe relative ArtifactPlan path")
                    if not isinstance(payload["prototype_refs"], list) or not payload["prototype_refs"]:
                        fail("prototype_refs must be a non-empty list")
                    for ref in payload["prototype_refs"]:
                        _keys(ref, {"prototype_path", "prototype_sha256", "page_id", "area_id", "feature_ids", "acceptance_ids", "change_intent", "depends_on"}, set(), "prototype_ref")
                        relative = PurePosixPath(_text(ref["prototype_path"], "prototype_path", maximum=500))
                        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
                            fail("prototype_path must remain below the approved root")
                        if not str(ref["prototype_path"]).lower().endswith(".html") or not DIGEST_RE.fullmatch(str(ref["prototype_sha256"])):
                            fail("prototype ref must carry an HTML path and verified SHA-256")
                        ref["page_id"] = _id(ref["page_id"], "page_id")
                        ref["area_id"] = _id(ref["area_id"], "area_id")
                        _ids(ref["feature_ids"], "feature_ids", "F")
                        _ids(ref["acceptance_ids"], "acceptance_ids", "A")
                        ref["change_intent"] = _text(ref["change_intent"], "change_intent", maximum=1000)
                        if not isinstance(ref["depends_on"], list) or not ref["depends_on"]:
                            fail("prototype_ref.depends_on must be a non-empty list")
                        ref["depends_on"] = sorted({_text(item, "depends_on", maximum=120) for item in ref["depends_on"]})
                        ref["artifact_plan_id"] = payload["artifact_plan_id"]
                        ref["gate_digest"] = payload["gate_digest"]
                        ref["approved_root"] = str(approved_root)
                    seen_screenshots: set[str] = set()
                    for ref in payload["prototype_refs"]:
                        if not set(ref["depends_on"]) & {row["id"] for row in index["screenshots"]}:
                            fail("prototype ref must depend on at least one known screenshot UI-EV")
                        for evidence_id in ref["depends_on"]:
                            target = next((row for row in index["screenshots"] if row["id"] == evidence_id), None)
                            if target is not None:
                                if target.get("validation", {}).get("status") != "valid":
                                    fail("prototype ref depends on a non-valid screenshot")
                                target["prototype_refs"].append(ref)
                                seen_screenshots.add(evidence_id)
                    if len(seen_screenshots) == 0:
                        fail("no screenshot references were bound")
                _replace_index(root_fd, _index_bytes(index))
                return {"change_id": change_id, "status": "updated", "operation": operation,
                        "index_sha256": _sha(_index_bytes(index))}
        finally:
            fs.close_dir(root_fd)
    finally:
        fs.close_dir(repo_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("scaffold", "validate", "record-review", "bind-prototype"):
        sub = subs.add_parser(name)
        sub.add_argument("--repo-root", required=True, type=Path)
        if name != "scaffold":
            sub.add_argument("--change-id", required=True)
            sub.add_argument("--requirement-dir")
        if name in {"record-review", "bind-prototype"}:
            sub.add_argument("--expected-index-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scaffold":
            output = _scaffold(args.repo_root, read_json_stdin())
            status = 0
        elif args.command == "validate":
            output, status = _validate_command(args.repo_root, args.change_id, args.requirement_dir)
        else:
            output = _update_index(args.repo_root, args.change_id, args.expected_index_sha256,
                                   read_json_stdin(), args.command, args.requirement_dir)
            status = 0
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return status
    except (UiReferenceError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
