#!/usr/bin/env python3
"""Safe, local UI screenshot evidence helper for design-pipeline."""
from __future__ import annotations

import argparse
import contextlib
import errno
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
from aek.adapters.ui_image import PngValidationError, validate_png_reader
from aek.adapters.ui_observation_store import ObservationStore, extraction_contract, observation_scope
from aek.core.artifact import ui_observation as observation


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
            fields = {"prototype_path", "prototype_sha256", "page_id", "area_id", "feature_ids",
                        "acceptance_ids", "change_intent", "depends_on", "artifact_plan_id",
                        "gate_digest", "approved_root"}
            if isinstance(ref, dict) and 'schema_version' in ref:
                fields |= {'schema_version', 'state_id', 'context_id'}
                if type(ref['schema_version']) is not int or ref['schema_version'] != 2:
                    fail('invalid versioned prototype reference')
                if (ref.get('state_id') != row['state'] or type(ref.get('context_id')) is not str
                        or sum(context['context_id'] == ref['context_id'] for context in contexts) != 1):
                    fail('prototype reference state/context does not match actual screenshot')
            _keys(ref, fields, set(), where + ".prototype_refs[]")
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
    """Keep the original fd/error contract over the shared streaming decoder."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        return validate_png_reader(lambda size: os.read(fd, size), file_size)
    except PngValidationError as exc:
        fail(str(exc))


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


def _create_staging(parent_fd: Any, index: dict[str, Any], *, allow_identical: bool = True) -> None:
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
        if allow_identical and existing == _index_bytes(index):
            return
        if not allow_identical:
            fail('index CAS conflict: create requires an absent material index')
        fail("material root already exists with a different or missing index; no files were overwritten")
    except BaseException:
        _remove_tree_at(parent_fd, stage_name)
        raise


def _v2_request(request, operation, change_id, requirement_dir, extra):
    value = _keys(request, {'schema_version', 'change_id', 'requirement_dir', 'targets',
                            'expected_index_sha256'} | set(extra), set(), operation + ' v2')
    if (type(value['schema_version']) is not int or value['schema_version'] != 2
            or value['change_id'] != change_id or value['requirement_dir'] != requirement_dir):
        fail(operation + ' v2 identity does not match control-side arguments')
    expected = value['expected_index_sha256']
    if not (operation == 'scaffold' and expected is None):
        if type(expected) is not str or not DIGEST_RE.fullmatch(expected):
            fail(operation + ' v2 requires expected_index_sha256')
    if type(value['targets']) is not list or not 1 <= len(value['targets']) <= 32:
        fail(operation + ' v2 targets must contain 1 to 32 identities')
    return value


def _v2_targets(index, selectors):
    """Resolve only current evidence IDs or complete existing projection keys."""
    selected, seen = {}, set()
    fields = {'page_id', 'area_id', 'state_id', 'context_id'}
    for selector in selectors:
        if isinstance(selector, str):
            ident = _id(selector, 'target evidence ID')
            key = ('evidence', ident)
            matches = [(row, None) for row in index['screenshots'] if row['id'] == ident]
        else:
            selector = _keys(selector, fields, set(), 'target selector')
            for field in fields:
                _id(selector[field], 'target ' + field) if field != 'state_id' else None
            if type(selector['state_id']) is not str or selector['state_id'] not in STATES:
                fail('invalid target state identity')
            key = tuple(selector[field] for field in sorted(fields))
            matches = [(row, selector['context_id']) for row in index['screenshots']
                if row['state'] == selector['state_id']
                and any(context['context_id'] == selector['context_id'] for context in row['applies_to'])
                and any(ref.get('schema_version') == 2 and all(ref.get(field) == selector[field] for field in fields)
                        for ref in row['prototype_refs'])]
        if key in seen or not matches:
            fail('unknown or duplicate v2 target')
        seen.add(key)
        for row, context in matches:
            ident = row['id']
            if ident in selected and (selected[ident] is None or context is None or context in selected[ident]):
                fail('overlapping or overbroad v2 targets')
            if context is None:
                selected[ident] = None
            else:
                selected.setdefault(ident, set()).add(context)
        if len(selected) > 32:
            fail('overbroad v2 target scope exceeds 32 evidence identities')
    return selected


def _v2_result(repo_root, index, raw, operation, selected, status):
    targets, gaps = [], []
    directory = index.get('requirement_dir', index['change_id'])
    for row in index['screenshots']:
        if row['id'] not in selected:
            gaps.append({'id': row['id'], 'reason': 'NOT_EVALUATED'})
            continue
        contexts, current_sha = [], row['validation'].get('content_sha256')
        valid = row['validation']['status'] == 'valid'
        for context in row['applies_to']:
            if selected[row['id']] is not None and context['context_id'] not in selected[row['id']]:
                gaps.append({'id': row['id'], 'context_id': context['context_id'], 'reason': 'NOT_EVALUATED'})
                continue
            review, fresh = context['content_review'], context['freshness']
            contexts.append({**{key: context[key] for key in ('context_id', 'permissions', 'relation', 'system_version', 'viewport')
                                if key in context},
                'applicability': review.get('status', 'unverified') if valid and current_sha
                    and review.get('reviewed_sha256') == current_sha else 'unverified',
                'freshness': fresh.get('status', 'unknown') if valid and current_sha
                    and fresh.get('confirmed_sha256') == current_sha else 'unknown'})
        ready = valid and bool(contexts) and all(context['applicability'] == 'applicable'
            and context['freshness'] == 'confirmed_current' for context in contexts)
        action = 'REPLACE_IMAGE' if not valid else 'CONTINUE_DESIGN' if ready else 'VERIFY_APPLICABILITY'
        targets.append({'id': row['id'], 'page': row['page'], 'state': row['state'],
            'full_path': str(repo_root.absolute() / 'docs/01-需求' / directory / '原始材料/ui-reference' / row['path']),
            'file_status': row['validation']['status'], 'permissions': row['permissions'], 'contexts': contexts,
            'git_status': row.get('git_status', 'unknown'), 'git_staging': row.get('git_staging', 'unknown'),
            'next_action': action})
        if not ready:
            gaps.append({'id': row['id'], 'reason': action})
    return {'schema_version': 2, 'operation': operation, 'change_id': index['change_id'], 'status': status,
            'index_sha256': _sha(raw), 'targets': targets, 'gaps': gaps[:128],
            'coverage': 'PARTIAL' if gaps else 'COMPLETE', 'truncated': len(gaps) > 128}


def _scaffold_v2(repo_root, request, change_id=None, requirement_dir=None):
    if type(request) is not dict:
        fail('scaffold v2 requires an explicit request object')
    change_id = _id(change_id if change_id is not None else request.get('change_id'), 'change_id')
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else request.get('requirement_dir'))
    _validate_requirement_identity(change_id, requirement_dir)
    value = _v2_request(request, 'scaffold', change_id, requirement_dir, {'design_system', 'coverage', 'screenshots'})
    index, _ = validate_request({key: item for key, item in {**value, 'schema_version': 1}.items()
                                if key not in {'targets', 'expected_index_sha256'}})
    repo_fd = _open_absolute_dir(repo_root)
    try:
        if value['expected_index_sha256'] is not None:
            parent, root = _material_root(repo_fd, requirement_dir)
            fs.close_dir(parent)
            try:
                with _exclusive_lock(root, timeout=5):
                    actual, raw = _load_index(root, change_id, requirement_dir)
                    if _sha(raw) != value['expected_index_sha256']:
                        fail('index CAS conflict: expected SHA-256 does not match current index')
                    def static_scope(item):
                        return {**{key: val for key, val in item.items() if key != 'screenshots'},
                            'screenshots': [{**{key: val for key, val in row.items()
                                if key not in {'validation', 'prototype_refs', 'git_status', 'git_staging', 'git_warning', 'applies_to'}},
                                'applies_to': [{key: val for key, val in context.items()
                                    if key not in {'content_review', 'freshness'}} for context in row['applies_to']]}
                                for row in item['screenshots']]}
                    if static_scope(actual) != static_scope(index):
                        fail('index CAS conflict: scaffold cannot alter existing static scope or coverage')
                    selected = _v2_targets(actual, value['targets'])
                    _check_update_images(root, actual, {row['id'] for row in actual['screenshots']
                        if row['id'] in selected and row['validation']['status'] == 'valid'})
                    if _read_at(root, INDEX_NAME, 8 * 1024 * 1024) != raw:
                        fail('index CAS conflict: index changed during scaffold')
                    return _v2_result(repo_root, actual, raw, 'scaffold', selected, 'created_or_identical')
            finally:
                fs.close_dir(root)
        # Only a new index can resolve against the request's static manifest.
        # Existing projection selectors must use its actual persisted refs.
        selected = _v2_targets(index, value['targets'])
        with _material_parent(repo_fd, requirement_dir, create=True) as (parent, unused):
            fs.close_dir(unused)
            with _exclusive_lock(parent, timeout=5):
                try:
                    existing = _open_child_dir(parent, 'ui-reference')
                except FileNotFoundError:
                    pass
                else:
                    fs.close_dir(existing)
                    fail('index CAS conflict: create requires an absent material index')
                if set(selected) != {row['id'] for row in index['screenshots']}:
                    fail('scaffold v2 targets must exactly name the initial screenshot set')
                _create_staging(parent, index, allow_identical=False)
                root = _open_child_dir(parent, 'ui-reference')
                try:
                    actual, raw = _load_index(root, change_id, requirement_dir)
                    _check_update_images(root, actual, set())
                    return _v2_result(repo_root, actual, raw, 'scaffold', selected, 'created_or_identical')
                finally:
                    fs.close_dir(root)
    finally:
        fs.close_dir(repo_fd)


def _scaffold(repo_root: Path, request: Any, *, include_details: bool = False) -> dict[str, Any]:
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
        if include_details:
            return _v2_result(repo_root, index, _index_bytes(index), 'scaffold',
                {row['id']: None for row in index['screenshots']}, 'created_or_identical')
        return {"change_id": index["change_id"], "status": "created_or_identical",
                "screenshots": [{"id": row["id"], "path": row["path"], "status": "pending"}
                                for row in index["screenshots"]]}
    finally:
        fs.close_dir(repo_fd)


def _validate_command(repo_root: Path, change_id: str, requirement_dir: str | None = None, *,
                      include_details: bool = False, request_v2: Any = None) -> tuple[dict[str, Any], int]:
    change_id = _id(change_id, "change_id")
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else change_id)
    _validate_requirement_identity(change_id, requirement_dir)
    selected = None
    if request_v2 is not None:
        value = _keys(request_v2, {'schema_version', 'change_id', 'requirement_dir',
                                 'targets', 'expected_index_sha256'}, set(), 'validate v2')
        if (type(value['schema_version']) is not int or value['schema_version'] != 2
                or value['change_id'] != change_id or value['requirement_dir'] != requirement_dir):
            fail('validate v2 identity does not match control-side arguments')
        if type(value['expected_index_sha256']) is not str or not DIGEST_RE.fullmatch(value['expected_index_sha256']):
            fail('validate v2 requires expected_index_sha256')
        if type(value['targets']) is not list or not 1 <= len(value['targets']) <= 32:
            fail('validate v2 targets must contain 1 to 32 evidence IDs')
        selected = {}  # Resolve IDs or complete projection keys only after the safe index load.
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent_fd, root_fd = _material_root(repo_fd, requirement_dir)
        fs.close_dir(parent_fd)
        try:
            with _exclusive_lock(root_fd, timeout=5 if selected is not None else None):
                index, old_raw = _load_index(root_fd, change_id, requirement_dir)
                if selected is not None:
                    if _sha(old_raw) != request_v2['expected_index_sha256']:
                        fail('index CAS conflict: expected SHA-256 does not match current index')
                    selected = _v2_targets(index, value['targets'])
                    # Validate all stored paths without interpreting sibling
                    # content or upgrading its readiness. Missing siblings are gaps.
                    _check_update_images(root_fd, index, set())
                result_rows = []
                pending = False
                for row in index["screenshots"]:
                    if selected is not None and row['id'] not in selected:
                        continue
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
                        if selected is not None and selected[row['id']] is not None \
                                and ctx['context_id'] not in selected[row['id']]:
                            continue
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
                    if include_details or selected is not None:
                        contexts = []
                        for context in row.get('applies_to', []):
                            review, fresh = context.get('content_review', {}), context.get('freshness', {})
                            current_sha = validation.get('content_sha256')
                            valid = validation['status'] == 'valid'
                            contexts.append({'context_id': context['context_id'],
                                'applicability': review.get('status', 'unverified') if valid and current_sha
                                    and review.get('reviewed_sha256') == current_sha else 'unverified',
                                'freshness': fresh.get('status', 'unknown') if valid and current_sha
                                    and fresh.get('confirmed_sha256') == current_sha else 'unknown'})
                        result_rows[-1]['contexts'] = contexts
                if selected is not None:
                    current_targets = {row['id'] for row in index['screenshots']
                                       if row['id'] in selected and row['validation']['status'] == 'valid'}
                    _check_update_images(root_fd, index, current_targets)
                    if _read_at(root_fd, INDEX_NAME, 8 * 1024 * 1024) != old_raw:
                        fail('index CAS conflict: index changed during validation')
                new_raw = _index_bytes(index)
                if new_raw != old_raw:
                    _replace_index(root_fd, new_raw)
                if selected is not None:
                    return (_v2_result(repo_root, index, new_raw, 'validate', selected,
                                       'pending' if pending else 'ready'), 4 if pending else 0)
                if include_details:
                    return (_v2_result(repo_root, index, new_raw, 'validate',
                        {row['id']: None for row in index['screenshots']},
                        'pending' if pending else 'ready'), 1 if pending else 0)
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


def _check_update_images(root_fd: Any, index: dict[str, Any], targets: set[str]) -> None:
    """Check every path's safety, but only require target content readiness.

    Unrelated missing/placeholder images are gaps, not permissions to follow
    links or accept unsafe index paths. The stored index was already checked
    structurally by _load_index; existing leaves still use the safe adapter.
    """
    for row in index["screenshots"]:
        targeted = row["id"] in targets
        if targeted and row.get("validation", {}).get("status") != "valid":
            fail("update target must be a validated screenshot")
        path = PurePosixPath(row["path"])
        if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in str(path):
            fail("index contains an unsafe path")
        parent = fs.dup_dir(root_fd)
        try:
            try:
                for part in path.parts[:-1]:
                    child = _open_child_dir(parent, part)
                    fs.close_dir(parent)
                    parent = child
                image_fd = fs.open_file(parent, path.name, os.O_RDONLY)
            except FileNotFoundError:
                if targeted:
                    raise
                continue
            try:
                if not targeted:
                    continue
                before = os.fstat(image_fd)
                if before.st_size > MAX_FILE_BYTES:
                    fail("referenced screenshot is not a bounded regular file")
                digest = hashlib.sha256()
                total = 0
                while True:
                    block = os.read(image_fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_FILE_BYTES:
                        fail("referenced screenshot exceeded its byte limit")
                    digest.update(block)
                after = os.fstat(image_fd)
                if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    fail("referenced screenshot changed while reading")
                if digest.hexdigest() != row["validation"].get("content_sha256"):
                    fail("referenced screenshot changed; run validate and retry with its new index SHA")
            finally:
                os.close(image_fd)
        finally:
            fs.close_dir(parent)


def _update_index(repo_root: Path, change_id: str, expected_sha: str, payload: Any, operation: str,
                  requirement_dir: str | None = None, *, explicit_v2: bool = False) -> dict[str, Any]:
    change_id = _id(change_id, "change_id")
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else change_id)
    _validate_requirement_identity(change_id, requirement_dir)
    if not DIGEST_RE.fullmatch(expected_sha):
        fail("--expected-index-sha256 must be lowercase SHA-256")
    request_v2 = None
    if explicit_v2:
        extra = {'updates'} if operation == 'record-review' else {
            'artifact_plan_id', 'gate_digest', 'approved_root', 'prototype_refs'}
        request_v2 = _v2_request(payload, operation, change_id, requirement_dir, extra)
        if request_v2['expected_index_sha256'] != expected_sha:
            fail('index CAS conflict: request and control-side expected SHA-256 differ')
        payload = {key: request_v2[key] for key in extra}
        payload['schema_version'] = 1 if operation == 'record-review' else 2
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent, root_fd = _material_root(repo_fd, requirement_dir)
        fs.close_dir(parent)
        try:
            with _exclusive_lock(root_fd, timeout=5 if explicit_v2 or operation == 'bind-prototype'
                    and isinstance(payload, dict) and payload.get('schema_version') == 2 else None):
                index, old_raw = _load_index(root_fd, change_id, requirement_dir)
                if _sha(old_raw) != expected_sha:
                    fail("index CAS conflict: expected SHA-256 does not match current index")
                selected_v2 = _v2_targets(index, request_v2['targets']) if explicit_v2 else None
                # Determine the actual target/dependency set, never the entire
                # readiness set. Full structural validation remains in _load_index.
                if not isinstance(payload, dict):
                    fail("update payload must be an object")
                entries = payload.get("updates" if operation == "record-review" else "prototype_refs")
                if not isinstance(entries, list) or not entries:
                    fail("update entries must be a non-empty list")
                targets: set[str] = set()
                for entry in entries:
                    if not isinstance(entry, dict):
                        fail("update entry must be an object")
                    if operation == "record-review":
                        targets.add(_id(entry.get("screenshot_id"), "screenshot_id"))
                    else:
                        dependencies = entry.get("depends_on")
                        if not isinstance(dependencies, list) or not dependencies:
                            fail("prototype_ref.depends_on must be a non-empty list")
                        targets.update(_text(item, "depends_on", maximum=120) for item in dependencies)
                if explicit_v2:
                    if targets != set(selected_v2):
                        fail('v2 update entries must match the selected evidence scope')
                    pairs = set()
                    for entry in entries:
                        identifiers = [entry['screenshot_id']] if operation == 'record-review' else entry['depends_on']
                        context = entry.get('context_id')
                        for ident in identifiers:
                            allowed = selected_v2[ident]
                            if allowed is not None and context not in allowed:
                                fail('v2 update context is outside selected scope')
                            if operation == 'bind-prototype' and ident not in request_v2['targets']:
                                fields = ('page_id', 'area_id', 'state_id', 'context_id')
                                projection = {key: entry.get(key) for key in fields}
                                row = next(row for row in index['screenshots'] if row['id'] == ident)
                                if (projection not in request_v2['targets'] or not any(
                                        ref.get('schema_version') == 2
                                        and all(ref.get(key) == projection[key] for key in fields)
                                        for ref in row['prototype_refs'])):
                                    fail('v2 binding is outside the selected page/area/state/context scope')
                        pair = ((entry['screenshot_id'], context) if operation == 'record-review' else (
                            entry.get('prototype_path'), entry.get('page_id'), entry.get('area_id'),
                            entry.get('state_id'), context))
                        if pair in pairs:
                            fail('duplicate v2 update identity')
                        pairs.add(pair)
                _check_update_images(root_fd, index, targets)
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
                    if type(payload["schema_version"]) is not int or payload["schema_version"] not in (1, 2):
                        fail("bind-prototype schema_version must be 1 or explicit 2")
                    version = payload['schema_version']
                    _text(payload["artifact_plan_id"], "artifact_plan_id", maximum=120)
                    if not isinstance(payload["gate_digest"], str) or not DIGEST_RE.fullmatch(payload["gate_digest"]):
                        fail("bind-prototype requires a verified gate_digest")
                    approved_root = PurePosixPath(_text(payload["approved_root"], "approved_root", maximum=300))
                    if approved_root.is_absolute() or ".." in approved_root.parts or "\\" in str(approved_root):
                        fail("approved_root must be a safe relative ArtifactPlan path")
                    if not isinstance(payload["prototype_refs"], list) or not payload["prototype_refs"]:
                        fail("prototype_refs must be a non-empty list")
                    for ref in payload["prototype_refs"]:
                        fields = {"prototype_path", "prototype_sha256", "page_id", "area_id", "feature_ids", "acceptance_ids", "change_intent", "depends_on"}
                        _keys(ref, fields | ({'state_id', 'context_id'} if version == 2 else set()), set(), "prototype_ref")
                        if version == 2:
                            if type(ref['state_id']) is not str or ref['state_id'] not in STATES:
                                fail('invalid prototype state identity')
                            _id(ref['context_id'], 'prototype context_id')
                            ref['schema_version'] = 2
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
                            if version == 2 and (target is None or target['state'] != ref['state_id']
                                    or sum(context['context_id'] == ref['context_id']
                                           for context in target['applies_to']) != 1):
                                fail('prototype state/context or dependency does not match actual screenshot')
                            if target is not None:
                                if target.get("validation", {}).get("status") != "valid":
                                    fail("prototype ref depends on a non-valid screenshot")
                                def binding_identity(value):
                                    return (value.get('schema_version', 1), value['prototype_path'], value['page_id'],
                                            value['area_id'], value.get('state_id'), value.get('context_id'))
                                identity = binding_identity(ref)
                                refs = target["prototype_refs"]
                                matches = [number for number, item in enumerate(refs)
                                           if binding_identity(item) == identity]
                                if not matches:
                                    refs.append(ref)
                                else:
                                    first = matches[0]
                                    refs[first] = ref
                                    refs[:] = [item for number, item in enumerate(refs)
                                               if number == first or number not in matches]
                                seen_screenshots.add(evidence_id)
                    if len(seen_screenshots) == 0:
                        fail("no screenshot references were bound")
                # Locks serialize writers, not user edits to images. Reopen and
                # recheck the actual referenced bytes before publishing facts.
                _check_update_images(root_fd, index, targets)
                if explicit_v2 and _read_at(root_fd, INDEX_NAME, 8 * 1024 * 1024) != old_raw:
                    fail('index CAS conflict: index changed during update')
                new_raw = _index_bytes(index)
                if new_raw != old_raw:
                    _replace_index(root_fd, new_raw)
                if explicit_v2:
                    return _v2_result(repo_root, index, new_raw, operation, selected_v2, 'updated')
                return {"change_id": change_id, "status": "updated", "operation": operation,
                        "index_sha256": _sha(new_raw)}
        finally:
            fs.close_dir(root_fd)
    finally:
        fs.close_dir(repo_fd)


def _observation_contract() -> dict[str, Any]:
    # A managed resource, never a prompt path supplied by candidate JSON.
    return extraction_contract(Path(__file__).absolute().parents[1])


def _observation_source(index: dict[str, Any], repo_root: Path, requirement_dir: str,
                        evidence_id: str, context_ref: str) -> tuple[dict, dict, dict]:
    row = next((item for item in index['screenshots'] if item['id'] == evidence_id), None)
    if row is None:
        fail('unknown observation evidence ID')
    context = next((item for item in row['applies_to'] if item['context_id'] == context_ref), None)
    if context is None:
        fail('unknown observation context reference')
    scope = observation_scope(repo_root, requirement_dir, index['change_id'])
    source = {'scope_digest': scope, 'evidence_id': evidence_id,
              'image_sha256': row['validation']['content_sha256']}
    # Context identity is separate from interpretation and is rechecked on record.
    static = {key: context[key] for key in ('context_id', 'permissions', 'relation',
        'system_version', 'viewport') if key in context}
    return row, source, {'context_ref': context_ref,
                        'context_sha256': _sha(observation.canonical_bytes(static))}


def _observation_failure(exc: Exception) -> tuple[dict, int]:
    """Only the explicit v2 commands use the new error/exit contract."""
    message = str(exc)
    if isinstance(exc, TimeoutError):
        code, status = 'TIMEOUT', 5
    elif isinstance(exc, FileNotFoundError):
        code, status = 'NEEDS_EVIDENCE', 4
    elif message.startswith('NEEDS_EVIDENCE:'):
        code, status = 'NEEDS_EVIDENCE', 4
    elif message.startswith('CONFLICT:'):
        code, status = 'CONFLICT', 3
    elif message.startswith('STALE:') or 'changed' in message or 'digest mismatch' in message:
        code, status = 'STALE', 3
    elif isinstance(exc, OSError):
        code, status = (('UNSAFE_PATH', 2) if exc.errno in
            {errno.ELOOP, errno.ENOTDIR, errno.EINVAL, errno.EACCES, errno.EPERM}
            else ('INTERNAL_ERROR', 5))
    else:
        code, status = 'INVALID_INPUT', 2
    return {'schema_version': 2, 'status': 'error', 'reason_code': code, 'reason': message}, status


def _observation_command(repo_root: Path, change_id: str, expected_sha: str,
                         payload: Any, operation: str, requirement_dir: str | None) -> tuple[dict, int]:
    change_id = _id(change_id, 'change_id')
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else change_id)
    _validate_requirement_identity(change_id, requirement_dir)
    if not DIGEST_RE.fullmatch(expected_sha):
        fail('expected index digest must be lowercase SHA-256')
    fields = ({'schema_version', 'evidence_id', 'context_ref', 'required_regions'}
              if operation == 'observation-prepare' else
              {'schema_version', 'prepare_ref', 'prepare_sha256', 'candidate', 'provenance'})
    request = _keys(payload, fields, set(), operation)
    if type(request['schema_version']) is not int or request['schema_version'] != 2:
        fail('observation request requires explicit schema_version 2')
    if len(observation.canonical_bytes(request)) > 128 * 1024:
        fail('observation request exceeds byte limit')
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent, root_fd = _material_root(repo_fd, requirement_dir)
        fs.close_dir(parent)
        try:
            with _exclusive_lock(root_fd, timeout=5):
                index, raw = _load_index(root_fd, change_id, requirement_dir)
                if _sha(raw) != expected_sha:
                    fail('CONFLICT: screenshot index changed')
                contract = _observation_contract()
                if operation == 'observation-prepare':
                    evidence_id = _id(request['evidence_id'], 'evidence_id')
                    context_ref = _id(request['context_ref'], 'context_ref')
                    regions = _ids(request['required_regions'], 'required_regions')
                    if len(regions) > observation.MAX_REGIONS:
                        fail('required region count exceeds limit')
                else:
                    digest = request['prepare_sha256']
                    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                        fail('invalid preparation digest')
                    if request['prepare_ref'] != f'observations/preparations/{digest}.json':
                        fail('invalid preparation reference')
                    # The reference is data, not authority. The actual repository,
                    # index, context, image and prompt must all match below.
                    with ObservationStore(root_fd) as store:
                        prepared = store.read_preparation(digest)
                    _keys(prepared, {'schema_version', 'evidence_scope', 'extraction_contract',
                        'required_regions', 'context', 'source_index_sha256',
                        'binding_index_sha256', 'snapshot_ref'}, set(), 'preparation')
                    _keys(prepared['evidence_scope'], {'scope_digest', 'evidence_id', 'image_sha256'},
                          set(), 'preparation.evidence_scope')
                    _keys(prepared['context'], {'context_ref', 'context_sha256'}, set(), 'preparation.context')
                    evidence_id = _id(prepared['evidence_scope']['evidence_id'], 'evidence_id')
                    context_ref = _id(prepared['context']['context_ref'], 'context_ref')
                    regions = _ids(prepared['required_regions'], 'required_regions')
                    if len(regions) > observation.MAX_REGIONS:
                        fail('required region count exceeds limit')
                _check_update_images(root_fd, index, {evidence_id})
                row, source, context = _observation_source(index, repo_root, requirement_dir,
                                                          evidence_id, context_ref)
                with ObservationStore(root_fd) as store:
                    bindings, revision = store.binding_snapshot(source['scope_digest'])
                    snapshot_ref = f"observations/snapshots/{source['image_sha256']}.png"
                    baseline = {'schema_version': 2, 'evidence_scope': source,
                        'extraction_contract': contract, 'required_regions': sorted(regions),
                        'context': context, 'source_index_sha256': expected_sha,
                        'binding_index_sha256': revision, 'snapshot_ref': snapshot_ref}
                    if operation == 'observation-prepare':
                        binding = next((item for item in bindings['bindings']
                                        if item['evidence_id'] == evidence_id), None)
                        if binding is not None:
                            try:
                                saved = store.read_observation(binding['observation_sha256'])
                                reusable = observation.can_reuse(saved, source, contract, tuple(regions),
                                    expected_observation_sha256=binding['observation_sha256'])
                            except FileNotFoundError:
                                reusable = False
                            if reusable:
                                _check_update_images(root_fd, index, {evidence_id})
                                if _sha(_read_at(root_fd, INDEX_NAME, 8 * 1024 * 1024)) != expected_sha:
                                    fail('CONFLICT: screenshot index changed during reuse')
                                if _observation_contract() != contract:
                                    fail('STALE: extraction contract changed during reuse')
                                current, current_revision = store.binding_snapshot(source['scope_digest'])
                                current_binding = next((item for item in current['bindings']
                                    if item['evidence_id'] == evidence_id), None)
                                if current_revision != revision or current_binding != binding:
                                    fail('CONFLICT: observation binding changed during reuse')
                                return {'schema_version': 2, 'status': 'REUSED',
                                    'observation_ref': 'observations/' + binding['observation_sha256'] + '.json',
                                    'observation_sha256': binding['observation_sha256'],
                                    'image_sha256': source['image_sha256'],
                                    'contract_digest': _sha(observation.canonical_bytes(contract)),
                                    'coverage': saved['coverage'], 'index_sha256': expected_sha}, 0
                        path = PurePosixPath(row['path'])
                        image_parent = fs.dup_dir(root_fd)
                        try:
                            for part in path.parts[:-1]:
                                child = _open_child_dir(image_parent, part)
                                fs.close_dir(image_parent)
                                image_parent = child
                            image_fd = fs.open_file(image_parent, path.name, os.O_RDONLY)
                            try:
                                store.snapshot_image(image_fd, source['image_sha256'], limit=MAX_FILE_BYTES)
                            finally:
                                os.close(image_fd)
                        finally:
                            fs.close_dir(image_parent)
                        _check_update_images(root_fd, index, {evidence_id})
                        if _sha(_read_at(root_fd, INDEX_NAME, 8 * 1024 * 1024)) != expected_sha:
                            fail('CONFLICT: screenshot index changed during preparation')
                        digest = store.publish_preparation(baseline)
                        return {'schema_version': 2, 'status': 'NEEDS_EVIDENCE',
                            'prepare_ref': f'observations/preparations/{digest}.json',
                            'prepare_sha256': digest, 'snapshot_ref': snapshot_ref,
                            'image_sha256': source['image_sha256'],
                            'contract_digest': _sha(observation.canonical_bytes(contract)),
                            'required_regions': sorted(regions), 'index_sha256': expected_sha}, 0
                    # A changed binding index can be an exact successful retry;
                    # bind() resolves that case without rewriting. All other
                    # prepared source/context/contract fields must still match.
                    baseline['binding_index_sha256'] = prepared['binding_index_sha256']
                    if observation.canonical_bytes(prepared) != observation.canonical_bytes(baseline):
                        fail('STALE: observation preparation no longer matches current sources')
                    store.verify_snapshot(source['image_sha256'], limit=MAX_FILE_BYTES)
                    provenance = _keys(request['provenance'], {'channel', 'model', 'observed_at'},
                                       set(), 'provenance')
                    provenance = {**provenance, 'prepare_sha256': request['prepare_sha256']}
                    record = observation.build_observation(source, contract, tuple(regions),
                        request['candidate'], provenance=provenance)
                    digest = store.publish_observation(record)
                    output = {'schema_version': 2, 'observation_ref': f'observations/{digest}.json',
                        'observation_sha256': digest, 'image_sha256': source['image_sha256'],
                        'contract_digest': _sha(observation.canonical_bytes(contract)),
                        'coverage': record['coverage'], 'index_sha256': expected_sha}
                    try:
                        _check_update_images(root_fd, index, {evidence_id})
                        if _sha(_read_at(root_fd, INDEX_NAME, 8 * 1024 * 1024)) != expected_sha:
                            fail('CONFLICT: screenshot index changed during observation')
                        store.verify_snapshot(source['image_sha256'], limit=MAX_FILE_BYTES)
                        if _observation_contract() != contract:
                            fail('STALE: extraction contract changed during observation')
                        store.bind(source['scope_digest'], prepared['binding_index_sha256'], {
                            'evidence_id': evidence_id, 'image_sha256': source['image_sha256'],
                            'contract_sha256': output['contract_digest'], 'observation_sha256': digest})
                    except (UiReferenceError, ValueError, OSError) as exc:
                        # The immutable file is not itself an effective binding.
                        # Replaying this preparation must still recheck sources.
                        error, status = _observation_failure(exc)
                        return {**output, **error, 'association_status': 'PENDING'}, status
                    return {**output, 'status': 'PARTIAL' if record['coverage']['missing_regions']
                            else 'RECORDED', 'association_status': 'BOUND'}, 0
        finally:
            fs.close_dir(root_fd)
    finally:
        fs.close_dir(repo_fd)


def _query_candidates(index, context_ref, request):
    """Resolve permission scope before reading any observation text."""
    matched = []
    authority = set()
    for row in index['screenshots']:
        contexts = [ctx for ctx in row['applies_to'] if ctx['context_id'] == context_ref]
        if len(contexts) > 1:
            fail('NEEDS_EVIDENCE: query context is ambiguous')
        if not contexts:
            continue
        context = contexts[0]
        identity = {key: context.get(key) for key in ('permissions', 'system_version', 'viewport')}
        authority.add(_sha(observation.canonical_bytes(identity)))
        if request.get('page', row['page']) != row['page'] or request.get('state', row['state']) != row['state']:
            continue
        # Reverse prototype projection/Git diagnostics are not authorization.
        state = {key: value for key, value in row.items()
                 if key not in {'applies_to', 'prototype_refs', 'git_status', 'git_staging', 'git_warning'}}
        matched.append((row, context, {**state, 'context': context}))
    if not authority or len(authority) != 1:
        fail('NEEDS_EVIDENCE: query requires one current permission/version/viewport scope')
    return matched, next(iter(authority))


def _query_context_ready(row, context):
    digest = row['validation'].get('content_sha256')
    return (row['validation']['status'] == 'valid' and digest != row['placeholder_sha256']
        and context['content_review'].get('status') == 'applicable'
        and context['content_review'].get('reviewed_sha256') == digest
        and context['freshness'].get('status') == 'confirmed_current'
        and context['freshness'].get('confirmed_sha256') == digest)


def _query_text(region):
    return '\n'.join([region['layout'], *[' '.join((control['kind'], control['label'], control['state']))
        for control in region['controls']], *region['style_clues']])


def _query_area(repo_root, repo_fd, requirement_dir, area, context_ref, evidence_states):
    """A current source baseline may narrow an already authorized context."""
    from aek.adapters.ui_baseline import read_baseline
    from aek.core.artifact.ui_baseline import validate_baseline, MAX_BASELINE_BYTES
    def mapping():
        current = fs.dup_dir(repo_fd)
        try:
            for part in ('docs', '03-SDD', requirement_dir):
                child = fs.open_child_dir(current, part)
                fs.close_dir(current)
                current = child
            text = fs.read_at(current, 'UI设计.md', 10 * 1024 * 1024).decode('utf-8', errors='strict')
        finally:
            fs.close_dir(current)
        starts = re.findall(r'^```aek-ui-baseline\s*$', text, re.M)
        blocks = re.findall(r'^```aek-ui-baseline\s*\n(.*?)\n```', text, re.M | re.S)
        if len(starts) != 1 or len(blocks) != 1 or len(blocks[0].encode('utf-8')) > MAX_BASELINE_BYTES:
            fail('NEEDS_EVIDENCE: area query requires one bounded current region mapping')
        value = validate_baseline(json.loads(blocks[0], object_pairs_hook=_unique_object))
        regions = [region for region in value['regions'] if region['area_id'] == area
                   and set(region['screenshot_ids']).intersection(evidence_states)
                   and ('state_id' not in region or (
                       region['context_id'] == context_ref and region['state_id'] in evidence_states.values()))]
        if not regions or len({region['page_id'] for region in regions}) != 1:
            fail('NEEDS_EVIDENCE: area mapping is missing or ambiguous across pages')
        if any('state_id' in region and any(ident in evidence_states
                and evidence_states[ident] != region['state_id'] for ident in region['screenshot_ids'])
                for region in regions):
            fail('NEEDS_EVIDENCE: typed area mapping does not match actual screenshot state')
        used = {ident for region in regions for ident in region['source_ids']}
        return {**value, 'regions': regions, 'sources': [row for row in value['sources'] if row['id'] in used]}
    subset = mapping()
    projected = read_baseline(repo_root, subset)
    if any(row['gaps'] for row in projected.values()) or observation.canonical_bytes(mapping()) != observation.canonical_bytes(subset):
        fail('NEEDS_EVIDENCE: area mapping or source baseline is no longer current and verified')
    selected = {}
    for region in subset['regions']:
        refs = {ref['evidence_id']: ref['sha256'] for ref in region.get('observations', [])}
        for ident in region['screenshot_ids']:
            selected.setdefault(ident, {})[region['region_id']] = refs.get(ident)
    digest = _sha(observation.canonical_bytes([
        {'target': list(target), 'digest': row['digest']} for target, row in sorted(projected.items())]))
    return digest, selected


def _observation_query(repo_root: Path, change_id: str, payload: Any, requirement_dir: str | None,
                       control_context: str, *, index_factory=None) -> tuple[dict, int]:
    """Control-selected root/context; request may narrow, never select authority.

    A host may inject an adopted SQLite factory. The standalone CLI has no
    runtime adoption authority, so it explicitly uses the bounded file path.
    """
    change_id = _id(change_id, 'change_id')
    requirement_dir = _requirement_dir(requirement_dir if requirement_dir is not None else change_id)
    _validate_requirement_identity(change_id, requirement_dir)
    control_context = _id(control_context, 'control context')
    request = _keys(payload, {'schema_version', 'query', 'context_ref'},
                    {'page', 'area', 'state', 'limit'}, 'observation-query')
    if type(request['schema_version']) is not int or request['schema_version'] != 2:
        fail('observation-query requires explicit schema_version 2')
    _text(request['query'], 'query', maximum=256)
    if request['context_ref'] != control_context:
        fail('query cannot widen the control-selected context')
    limit = request.get('limit', 20)
    if type(limit) is not int or not 1 <= limit <= 20:
        fail('invalid query result limit')
    for key in ('page', 'state'):
        if key in request:
            _text(request[key], key, maximum=256)
    if 'area' in request:
        if type(request['area']) is not str or not re.fullmatch(r'AREA-[1-9][0-9]*', request['area']):
            fail('invalid area identity')
    repo_fd = _open_absolute_dir(repo_root)
    try:
        parent, root_fd = _material_root(repo_fd, requirement_dir)
        fs.close_dir(parent)
        try:
            with _exclusive_lock(root_fd, timeout=5):
                deadline = time.monotonic() + 2
                def check_deadline():
                    if time.monotonic() >= deadline:
                        raise TimeoutError('observation query deadline exceeded')
                index, _ = _load_index(root_fd, change_id, requirement_dir)
                candidates, context_sha = _query_candidates(index, control_context, request)
                area_evidence_states = {row['id']: row['state'] for row, _, _ in candidates}
                area_snapshot, area_regions = None, None
                if 'area' in request:
                    try:
                        area_snapshot, area_regions = _query_area(repo_root, repo_fd, requirement_dir,
                            request['area'], control_context, area_evidence_states)
                    except (OSError, ValueError):
                        fail('NEEDS_EVIDENCE: current area mapping is unavailable')
                    candidates = [candidate for candidate in candidates if candidate[0]['id'] in area_regions]
                if len(candidates) > 256:
                    fail('query source count exceeds limit')
                scope = observation_scope(repo_root, requirement_dir, change_id)
                contract = _observation_contract()
                contract_sha = _sha(observation.canonical_bytes(contract))
                gaps, loaded, total_bytes = [], {}, 0
                try:
                    store = ObservationStore(root_fd, create=False)
                except FileNotFoundError:
                    fail('NEEDS_EVIDENCE: no current observations in this scope')
                with store:
                    bindings, _ = store.binding_snapshot(scope)
                    by_id = {row['evidence_id']: row for row in bindings['bindings']}
                    for row, context, state in candidates:
                        check_deadline()
                        ident = row['id']
                        binding = by_id.get(ident)
                        if not _query_context_ready(row, context) or binding is None:
                            gaps.append({'evidence_id': ident, 'reason_code': 'NEEDS_EVIDENCE'})
                            continue
                        source = {'scope_digest': scope, 'evidence_id': ident,
                                  'image_sha256': row['validation']['content_sha256']}
                        if binding['image_sha256'] != source['image_sha256'] or binding['contract_sha256'] != contract_sha:
                            gaps.append({'evidence_id': ident, 'reason_code': 'STALE'})
                            continue
                        try:
                            _check_update_images(root_fd, {'screenshots': [row]}, {ident})
                            record = store.read_observation(binding['observation_sha256'])
                            size = len(observation.canonical_bytes(record))
                            if total_bytes + size > 8 * 1024 * 1024:
                                gaps.append({'reason_code': 'LIMIT_EXCEEDED'})
                                break
                            total_bytes += size
                            if record['evidence_scope'] != source or record['extraction_contract'] != contract:
                                raise ValueError('stale observation source')
                        except (OSError, ValueError, UiReferenceError):
                            gaps.append({'evidence_id': ident, 'reason_code': 'STALE'})
                            continue
                        region_ids = None
                        if area_regions is not None:
                            available = {region['region_id'] for region in record['regions']}
                            region_ids = tuple(sorted(region for region, digest in area_regions[ident].items()
                                if region in available and (digest is None or digest == binding['observation_sha256'])))
                            if len(region_ids) != len(area_regions[ident]):
                                gaps.append({'evidence_id': ident, 'reason_code': 'NEEDS_EVIDENCE'})
                            if not region_ids:
                                continue
                        loaded[ident] = {'row': row, 'context': context, 'state': state, 'source': source,
                            'region_ids': region_ids,
                            'binding': binding, 'record': record, 'binding_sha': _sha(
                                observation.canonical_bytes({'binding': binding, 'evidence': state}))}
                        if record['coverage']['missing_regions']:
                            gaps.append({'evidence_id': ident, 'reason_code': 'OBSERVATION_PARTIAL'})
                    check_deadline()
                    # Default file matching does not silently approve a runtime
                    # merely because sqlite3 imports or supports config APIs.
                    backend, hits, backend_truncated = 'bounded_files', [], False
                    sqlite_unavailable = index_factory is None
                    if index_factory is not None:
                        from aek.adapters.ui_query import QueryScope, QuerySource, QueryUnavailable
                        groups = {}
                        for ident, value in loaded.items():
                            context, row = value['context'], value['row']
                            query_scope = QueryScope(scope, context_sha, row['page'], row['state'],
                                context.get('system_version', 'UNKNOWN'), _sha(observation.canonical_bytes(
                                    context['viewport'])) if 'viewport' in context else 'UNKNOWN')
                            groups.setdefault(query_scope, []).append(QuerySource(query_scope,
                                value['binding']['observation_sha256'], value['binding_sha'], value['record'],
                                value['region_ids']))
                        def revalidate(source):
                            check_deadline()
                            ident = source.record['evidence_scope']['evidence_id']
                            value = loaded[ident]
                            try:
                                _check_update_images(root_fd, {'screenshots': [value['row']]}, {ident})
                                current, _ = store.binding_snapshot(scope)
                                binding = next((row for row in current['bindings'] if row['evidence_id'] == ident), None)
                                if binding != value['binding'] or _observation_contract() != contract:
                                    return False
                                record = store.read_observation(source.sha256)
                                return record == value['record']
                            except (OSError, ValueError, UiReferenceError):
                                return False
                        try:
                            for query_scope, sources in groups.items():
                                check_deadline()
                                with index_factory(query_scope, tuple(sources), revalidate=revalidate,
                                    timeout=max(0.000001, min(2, deadline - time.monotonic()))) as projection:
                                    result = projection.query(request['query'], scope=query_scope, limit=limit)
                                    hits.extend(result.hits)
                                    backend_truncated = backend_truncated or result.truncated
                                    if result.stale_count:
                                        gaps.append({'reason_code': 'STALE_QUERY_SOURCE'})
                            backend = 'sqlite_memory'
                        except QueryUnavailable:
                            hits = []
                            backend_truncated = False
                            sqlite_unavailable = True
                        except ValueError:
                            fail('STALE: query sources changed during projection preparation')
                    needle = unicodedata.normalize('NFKC', request['query']).casefold()
                    for ident, value in (loaded.items() if backend == 'bounded_files' else ()):
                        for region in value['record']['regions']:
                            check_deadline()
                            if value['region_ids'] is not None and region['region_id'] not in value['region_ids']:
                                continue
                            display = _query_text(region)
                            if needle not in unicodedata.normalize('NFKC', display).casefold():
                                continue
                            hits.append({'evidence_id': ident, 'region_id': region['region_id'],
                                'observation_sha256': value['binding']['observation_sha256'],
                                'image_sha256': value['source']['image_sha256'], 'contract_sha256': contract_sha,
                                'binding_sha256': value['binding_sha'], 'observation_kind': region['observation_kind'],
                                'source_ref': 'observations/' + value['binding']['observation_sha256'] + '.json',
                                'snippet': display[:512]})
                    hits.sort(key=lambda hit: (hit['observation_sha256'], hit['region_id']))
                    truncated = backend_truncated or len(hits) > limit or any(
                        gap['reason_code'] == 'LIMIT_EXCEEDED' for gap in gaps)
                    hits = hits[:limit]
                    # All text remains private until the source/context fence.
                    # Finish payload IO before the final material/contract/
                    # binding fence so a cached body is not its own authority.
                    invalid = set()
                    for ident, value in loaded.items():
                        check_deadline()
                        try:
                            if store.read_observation(value['binding']['observation_sha256']) != value['record']:
                                invalid.add(ident)
                        except (OSError, ValueError):
                            invalid.add(ident)
                    if area_regions is not None:
                        try:
                            if _query_area(repo_root, repo_fd, requirement_dir, request['area'],
                                    control_context, area_evidence_states) != (area_snapshot, area_regions):
                                raise ValueError('area mapping changed')
                        except (OSError, ValueError, UiReferenceError):
                            invalid.update(loaded)
                            gaps.append({'reason_code': 'SCOPE_CHANGED'})
                    final_index, _ = _load_index(root_fd, change_id, requirement_dir)
                    final_candidates, final_context_sha = _query_candidates(final_index, control_context, request)
                    if area_regions is not None:
                        final_candidates = [candidate for candidate in final_candidates if candidate[0]['id'] in area_regions]
                    final_states = {row['id']: state for row, _, state in final_candidates}
                    if (final_context_sha != context_sha
                            or set(final_states) != {row['id'] for row, _, _ in candidates}):
                        gaps.append({'reason_code': 'SCOPE_CHANGED'})
                    for ident, value in loaded.items():
                        check_deadline()
                        try:
                            if final_states.get(ident) != value['state'] or final_context_sha != context_sha:
                                raise ValueError('context changed')
                            _check_update_images(root_fd, {'screenshots': [value['row']]}, {ident})
                        except (OSError, ValueError, UiReferenceError):
                            invalid.add(ident)
                    final_bindings, _ = store.binding_snapshot(scope)
                    current_bindings = {row['evidence_id']: row for row in final_bindings['bindings']}
                    current_contract = _observation_contract()
                    for ident, value in loaded.items():
                        if current_bindings.get(ident) != value['binding'] or current_contract != contract:
                            invalid.add(ident)
                    for ident in sorted(invalid):
                        gaps.append({'evidence_id': ident, 'reason_code': 'STALE'})
                    hits = [{**hit, 'applicability_ref': {'evidence_id': hit['evidence_id'],
                        'context_ref': control_context, 'context_sha256': _sha(observation.canonical_bytes(
                            loaded[hit['evidence_id']]['context']))}} for hit in hits
                        if hit['evidence_id'] not in invalid]
                    check_deadline()
                    generation = _sha(observation.canonical_bytes({'scope': scope, 'context': context_sha,
                        'area': area_snapshot,
                        'sources': sorted((ident, item['binding_sha']) for ident, item in loaded.items())}))
                    status = ('PARTIAL' if gaps or truncated else
                              ('UNAVAILABLE' if sqlite_unavailable else 'COMPLETE_FOR_SCOPE'))
                    if sqlite_unavailable:
                        gaps.append({'reason_code': ('SQLITE_RUNTIME_NOT_ADOPTED' if index_factory is None
                                                     else 'SQLITE_UNAVAILABLE')})
                    output = {'schema_version': 2, 'status': status,
                        'backend': backend, 'scope_ref': _sha(observation.canonical_bytes(
                            {'scope': scope, 'context_ref': control_context, 'context_sha256': context_sha,
                             'filters': {key: request[key] for key in ('page', 'state', 'area') if key in request}})),
                        'source_generation': generation, 'hits': hits, 'gaps': gaps, 'truncated': truncated,
                        'limits': {'sources': 256, 'source_bytes': 8 * 1024 * 1024, 'hits': limit,
                                   'snippet_chars': 512, 'query_chars': 256, 'seconds': 2}}
        finally:
            fs.close_dir(root_fd)
    finally:
        fs.close_dir(repo_fd)
    # The adopted projection has already closed, but material, binding and
    # contract reads (and handle cleanup) happened afterwards. Its control-side
    # pure mutation fence is therefore the final publication check, not another
    # source/runtime IO callback. Legacy factories without this optional seam
    # retain their original behavior; request JSON never supplies a factory.
    if index_factory is not None:
        try:
            fence = getattr(index_factory, 'control_fence', None)
            current = fence is None or (callable(fence) and fence() is True)
        except Exception:
            current = False
        if not current:
            output['hits'] = []
            output['status'] = 'PARTIAL'
            gap = {'reason_code': 'SQLITE_UNAVAILABLE'}
            if gap not in output['gaps']:
                output['gaps'].append(gap)
    return output, 0


def _human_summary(output: dict) -> str:
    """A bounded display only; it never upgrades evidence or writes approval."""
    def display(value):
        return ''.join(char if char.isprintable() else '\\u%04x' % ord(char) for char in str(value))
    rows = output.get('screenshots', output.get('targets', []))
    lines = ['UI 截图：' + display(output.get('change_id', '')),
             '状态：' + display(output.get('status', 'unknown')),
             '文件有效不代表视觉适用或原型已批准。']
    for row in rows[:20]:
        lines.append(display(row['id']) + ' · ' + display(row.get('full_path', row.get('path', 'unknown'))))
        if 'page' in row:
            lines.append('  页面：' + display(row['page']) + '；状态：' + display(row.get('state', 'unknown')))
            lines.append('  采集权限：' + display(row.get('permissions', {})))
        lines.append('  文件：' + display(row.get('file_status', row.get('validation', row.get('status', 'unknown'))))
            + '；Git：' + display(row.get('git_status', 'unknown'))
            + '；暂存：' + display(row.get('git_staging', 'unknown')))
        if 'next_action' in row:
            lines.append('  下一动作：' + display(row['next_action']))
        for context in row.get('contexts', [])[:20]:
            lines.append('  ' + display(context['context_id']) + ' · 适用性：'
                + display(context['applicability']) + '；时效：' + display(context['freshness']))
            if 'permissions' in context:
                lines.append('    上下文权限：' + display(context['permissions']))
        if len(row.get('contexts', [])) > 20:
            lines.append('  其余权限上下文未展开，请查看完整 JSON；未显示不代表通过。')
    if len(rows) > 20:
        lines.append('未显示其余 ' + str(len(rows) - 20) + ' 项，请查看完整 JSON；未显示不代表通过。')
    if output.get('coverage') == 'PARTIAL':
        lines.append('覆盖：PARTIAL；未检查项或缺口仍保留，局部就绪不代表整体完成。')
    lines.append('下一步：$design-pipeline（Claude Code：/design-pipeline），按缺口补材料或核实适用性。')
    return '\n'.join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("scaffold", "validate", "record-review", "bind-prototype",
                 "observation-prepare", "observation-record", "observation-query"):
        sub = subs.add_parser(name)
        sub.add_argument("--repo-root", required=True, type=Path)
        if name in {'scaffold', 'validate', 'record-review', 'bind-prototype'}:
            sub.add_argument('--format', choices=('json', 'human'), default='json')
        if name in {'scaffold', 'validate', 'record-review', 'bind-prototype'}:
            sub.add_argument('--schema-version', choices=(1, 2), type=int, default=1)
        if name != "scaffold":
            sub.add_argument("--change-id", required=True)
            sub.add_argument("--requirement-dir")
        else:
            sub.add_argument('--change-id')
            sub.add_argument('--requirement-dir')
        if name in {"record-review", "bind-prototype", "observation-prepare", "observation-record"}:
            sub.add_argument("--expected-index-sha256", required=True)
        if name == 'observation-query':
            sub.add_argument('--context-ref', required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = _parser()
    args = parser.parse_args(argv)
    explicit_v2 = getattr(args, 'schema_version', 1) == 2
    try:
        if args.command == "scaffold":
            request = read_json_stdin()
            output = (_scaffold_v2(args.repo_root, request, args.change_id, args.requirement_dir)
                      if explicit_v2 else _scaffold(args.repo_root, request,
                                                    include_details=args.format == 'human'))
            status = 0
        elif args.command == "validate":
            request_v2 = read_json_stdin() if args.schema_version == 2 else None
            if args.schema_version == 2 and request_v2 is None:
                fail('validate v2 requires an explicit request object')
            output, status = _validate_command(args.repo_root, args.change_id, args.requirement_dir,
                include_details=args.format == 'human',
                request_v2=request_v2)
        elif args.command in {'observation-prepare', 'observation-record'}:
            output, status = _observation_command(args.repo_root, args.change_id,
                args.expected_index_sha256, read_json_stdin(), args.command, args.requirement_dir)
        elif args.command == 'observation-query':
            output, status = _observation_query(args.repo_root, args.change_id, read_json_stdin(),
                args.requirement_dir, args.context_ref)
        else:
            output = _update_index(args.repo_root, args.change_id, args.expected_index_sha256,
                                   read_json_stdin(), args.command, args.requirement_dir, explicit_v2=explicit_v2)
            status = 0
        print(_human_summary(output) if getattr(args, 'format', 'json') == 'human'
              else json.dumps(output, ensure_ascii=False, sort_keys=True))
        return status
    except (UiReferenceError, OSError, ValueError, TypeError, RecursionError) as exc:
        if explicit_v2:
            reason = str(exc)
            status = ('CONFLICT' if 'CAS conflict' in reason else 'TIMEOUT' if isinstance(exc, TimeoutError)
                      else 'STALE' if 'screenshot changed' in reason else 'ERROR')
            print(json.dumps({'schema_version': 2, 'operation': args.command, 'change_id': args.change_id,
                'status': status, 'index_sha256': None, 'targets': [], 'gaps': [{'reason': reason}],
                'coverage': 'UNKNOWN', 'truncated': False}, ensure_ascii=False, sort_keys=True))
            return 3 if status in {'CONFLICT', 'STALE'} else 5 if status == 'TIMEOUT' else 2
        if args.command in {'observation-prepare', 'observation-record', 'observation-query'}:
            output, status = _observation_failure(exc)
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            return status
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
