"""Bounded recovery for the original init lifecycle, under its install lock.

Only initial requirement files, fixed package metadata and aek-owned index
regions may be published. This journal carries no Gate or approval authority.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import re
import uuid
from dataclasses import asdict
from pathlib import Path

from aek.adapters import ui_filesystem as fs
from aek.adapters.document_layout import _read, package_directory, package_registry, validate_package_registry
from aek.adapters.plan_transaction import _lock_file
from aek.core.artifact.document_layout import (
    component, load_object, load_package, make_package, package_bytes, relative_parts,
)

_TX = ".repo-memory-kit/document-init.tx.json"
_REGISTRY = ".repo-memory-kit/document-packages.json"
_POLICY = ".repo-memory-kit/document-layout.json"
_TEMPLATES = {name: "docs/01-需求/_模板/" + name for name in ("原始需求.md", "需求分析.md")}
_START, _END = "<!-- aek-packages:start -->", "<!-- aek-packages:end -->"
_MAX_TX = 16 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _optional(root, path, limit=2 * 1024 * 1024):
    parts = relative_parts(path)
    try:
        with package_directory(root, "/".join(parts[:-1])) as parent:
            return fs.read_at(parent, parts[-1], limit)
    except FileNotFoundError:
        return None


def _publish(root, path, content, before):
    parts = relative_parts(path)
    with package_directory(root, "/".join(parts[:-1]), create=True) as parent:
        try:
            current = fs.read_at(parent, parts[-1], _MAX_TX)
        except FileNotFoundError:
            current = None
        if current != before:
            raise ValueError("CAS_CONFLICT: init destination changed")
        temporary = ".document-init-write-" + uuid.uuid4().hex
        fd = fs.open_file(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        owned = True
        try:
            try:
                remaining = memoryview(content)
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError("short init write")
                    remaining = remaining[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                current = fs.read_at(parent, parts[-1], _MAX_TX)
            except FileNotFoundError:
                current = None
            if current != before:
                raise ValueError("CAS_CONFLICT: init destination changed before publish")
            if before is None:
                fs.rename_noreplace(parent, temporary, parent, parts[-1])
            else:
                fs.replace(parent, temporary, parent, parts[-1])
            owned = False
            fs.fsync_dir(parent)
        finally:
            if owned:
                fs.unlink(parent, temporary)


def _proposal(root, title, version, layout, change_id, *, captured=None):
    policy_raw = _optional(root, _POLICY) if captured is None else captured[_POLICY]
    if layout not in {"auto", "co-located", "project"}:
        raise ValueError("LAYOUT_CONFLICT: unsupported new package layout")
    if policy_raw is not None and layout == "co-located":
        raise ValueError("LAYOUT_CONFLICT: existing project mapping takes priority")
    source = design = f"docs/需求/{version}/{title}"
    selected = "co-located"
    if layout == "project" or policy_raw is not None:
        if policy_raw is None:
            raise ValueError("LAYOUT_CONFLICT: project mapping missing")
        policy = load_object(policy_raw)
        if (set(policy) != {"schema_version", "source_root", "design_root"}
                or type(policy["schema_version"]) is not int or policy["schema_version"] != 1):
            raise ValueError("LAYOUT_CONFLICT: unknown project mapping")
        def expand(value):
            if type(value) is not str or value.count("{title}") != 1 or value.count("{version}") != 1:
                raise ValueError("LAYOUT_CONFLICT: mapping needs one title and version")
            result = value.replace("{title}", title).replace("{version}", version)
            if "{" in result or "}" in result:
                raise ValueError("LAYOUT_CONFLICT: unsupported project placeholder")
            return result
        source, design, selected = expand(policy["source_root"]), expand(policy["design_root"]), "project"
    return make_package(change_id=change_id, title=title, version=version, layout=selected,
                        source_root=source, design_root=design), policy_raw


def _index_path(package):
    source, design = relative_parts(package.source_root)[:-1], relative_parts(package.design_root)[:-1]
    common = []
    for left, right in zip(source, design):
        if left != right:
            break
        common.append(left)
    if len(common) < 2:
        raise ValueError("INDEX_NOT_OWNED: no bounded common version index")
    return "/".join(common) + "/README.md"


def _index_registration(package):
    return {"status": "REGISTERED", "path": _index_path(package), "change_id": package.change_id}


def _inputs(root):
    return _input_hashes(_capture_inputs(root))


def _capture_inputs(root):
    return {name: _optional(root, name) for name in [*_TEMPLATES.values(), _POLICY]}


def _input_hashes(captured):
    return {name: hashlib.sha256(raw).hexdigest() if raw is not None else None for name, raw in captured.items()}


def _registration(package):
    return {"change_id": package.change_id, "version": package.version,
            "package_ref": package.package_ref, "revision": package.revision}


def _owned_rows(before):
    if before is None:
        return {}
    index = before.decode('utf-8')
    if index.count(_START) != 1 or index.count(_END) != 1 or index.index(_START) >= index.index(_END):
        raise ValueError('INDEX_NOT_OWNED: version index has no unique aek region')
    region = index.split(_START, 1)[1].split(_END, 1)[0]
    lines = [line.strip() for line in region.splitlines() if line.strip()]
    if (len(lines) < 2 or lines[0] != '| ID | 功能 | 负责人 | 状态 | 入口 |'
            or lines[1] != '| --- | --- | --- | --- | --- |'):
        raise ValueError('INVALID_INDEX: owned index structure changed')
    rows = {}
    for line in lines[2:]:
        if not line.startswith('| ') or not line.endswith(' |'):
            raise ValueError('INVALID_INDEX: malformed owned row boundary')
        fields = [cell.strip() for cell in line[1:-1].split('|')]
        if len(fields) != 5 or not re.fullmatch(r'CHG-(?:[0-9]{3}|[A-F0-9]{28})', fields[0]):
            raise ValueError('INVALID_INDEX: malformed owned package row')
        if fields[0] in rows:
            raise ValueError('DUPLICATE_ID: owned index repeats a package identity')
        rows[fields[0]] = fields
    return rows


def _index_link(package):
    from urllib.parse import quote
    relative = os.path.relpath(package.design_root, str(Path(_index_path(package)).parent)).replace(os.sep, '/')
    return quote(relative, safe='/')


def _registered_package(root, row):
    package = load_package(_read(root, row['package_ref'] + '/reviews/package.json'))
    if _registration(package) != row:
        raise ValueError('REGISTRATION_MISMATCH: registered metadata changed')
    return package


def _check_index_registration(root, package, registrations, before):
    rows, expected = _owned_rows(before), {}
    for row in registrations:
        if row['version'] != package.version:
            continue
        existing = _registered_package(root, row)
        if _index_path(existing) == _index_path(package):
            expected[existing.change_id] = existing
    if set(rows) != set(expected):
        raise ValueError('REGISTRATION_MISMATCH: owned index IDs differ from registered packages')
    for ident, existing in expected.items():
        link = re.fullmatch(r'\[[^\r\n]*\]\(([^)]+)\)', rows[ident][4])
        if link is None or link.group(1) != _index_link(existing):
            raise ValueError('REGISTRATION_MISMATCH: owned index points to another package')


def _index_bytes(package, before):
    existing_rows = _owned_rows(before)
    if package.change_id in existing_rows:
        raise ValueError('DUPLICATE_ID: new package ID already exists in the owned index')
    index = before.decode("utf-8") if before is not None else f"# {package.version}\n\n{_START}\n| ID | 功能 | 负责人 | 状态 | 入口 |\n| --- | --- | --- | --- | --- |\n{_END}\n"
    if index.count(_START) != 1 or index.count(_END) != 1 or index.index(_START) >= index.index(_END):
        raise ValueError("INDEX_NOT_OWNED: version index has no unique aek region")
    # File names remain exact; Markdown display/link escaping is presentation.
    display = package.title.replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;")
    row = f"| {package.change_id} | {display} | 待填写 | 草拟 | [{display}]({_index_link(package)}) |\n"
    result = index.replace(_END, row + _END).encode("utf-8")
    _owned_rows(result)
    return result


def _validate_journal(root, journal, title, version, layout):
    if (set(journal) != {"schema_version", "package", "inputs", "changes"}
            or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
            or type(journal["changes"]) is not list or not 1 <= len(journal["changes"]) <= 5):
        raise ValueError("INVALID_JOURNAL: unknown init transaction")
    package = load_package(_json(journal["package"]))
    expected, _policy = _proposal(root, title, version, layout, package.change_id)
    if package != expected or journal["inputs"] != _inputs(root):
        raise ValueError("SOURCE_CHANGED: init templates, mapping or package changed")
    allowed = {_REGISTRY, _index_path(package), package.source_root + "/原始需求.md",
               package.design_root + "/需求分析.md", package.design_root + "/reviews/package.json"}
    seen = set()
    rows = []
    registrations_before = None
    for row in journal["changes"]:
        if (type(row) is not dict or set(row) != {"path", "before", "after"}
                or row["path"] not in allowed or row["path"] in seen):
            raise ValueError("INVALID_JOURNAL: init destination outside declared package")
        seen.add(row["path"])
        before = base64.b64decode(row["before"], validate=True) if row["before"] is not None else None
        after = base64.b64decode(row["after"], validate=True)
        if not after or len(after) > 2 * 1024 * 1024 or (before is not None and len(before) > 2 * 1024 * 1024):
            raise ValueError("INVALID_JOURNAL: oversized init image")
        path = row["path"]
        metadata = package.design_root + "/reviews/package.json"
        if path == metadata:
            if before is not None or after != package_bytes(package):
                raise ValueError("INVALID_JOURNAL: init metadata does not match package")
        elif path == _REGISTRY:
            prior = load_object(before) if before is not None else {"schema_version": 1, "packages": []}
            registrations_before = validate_package_registry(prior)
            validate_package_registry(load_object(after))
            if (set(prior) != {"schema_version", "packages"} or type(prior["packages"]) is not list
                    or after != _json({"schema_version": 1, "packages": [*prior["packages"], _registration(package)]})):
                raise ValueError("INVALID_JOURNAL: init registry does not preserve its preimage")
        elif path == _index_path(package):
            if after != _index_bytes(package, before):
                raise ValueError("INVALID_JOURNAL: init index does not match its owned region")
        else:
            template = _TEMPLATES[path.rsplit("/", 1)[-1]]
            if before is not None or after != _read(root, template):
                raise ValueError("INVALID_JOURNAL: init may only create current templates")
        current = _optional(root, row["path"])
        if current not in (before, after):
            raise ValueError("CAS_CONFLICT: interrupted init destination changed")
        rows.append((row["path"], before, after, current))
    if not {_REGISTRY, _index_path(package), package.design_root + "/reviews/package.json"} <= seen:
        raise ValueError("INVALID_JOURNAL: required registration targets missing")
    index_before = next(before for path, before, _after, _current in rows if path == _index_path(package))
    _check_index_registration(root, package, registrations_before, index_before)
    return package, rows


def _resume(root, journal, title, version, layout):
    package, rows = _validate_journal(root, journal, title, version, layout)
    for path, before, after, current in rows:
        if _inputs(root) != journal["inputs"]:
            raise ValueError("SOURCE_CHANGED: init inputs changed during recovery")
        if current != after:
            _publish(root, path, after, before)
    if _inputs(root) != journal["inputs"]:
        raise ValueError("SOURCE_CHANGED: init inputs changed before completion")
    if any(_optional(root, path) != after for path, _before, after, _current in rows):
        raise ValueError("CAS_CONFLICT: init targets changed before completion")
    with package_directory(root, ".repo-memory-kit") as kit:
        fs.unlink(kit, "document-init.tx.json")
        fs.fsync_dir(kit)
    return package, {"created": [path for path, before, _after, _current in rows if before is None],
                     "skipped": [], "index_row_added": True, "index_registration": _index_registration(package)}


def initialize_package(repo: Path, title: str, version: str, *, layout="auto", fault_after=None):
    """Original init entry owns this lifecycle lock; no approved source changes."""
    component(title)
    component(version)
    root = fs.open_absolute_dir(Path(repo).absolute())
    try:
        with package_directory(root, ".repo-memory-kit") as kit, _lock_file(kit, name="install.lock"):
            pending = _optional(root, _TX, _MAX_TX)
            if pending is not None:
                journal = load_object(pending, limit=_MAX_TX)
                return _resume(root, journal, title, version, layout)
            captured = _capture_inputs(root)
            package, _policy = _proposal(root, title, version, layout,
                "CHG-" + secrets.token_hex(14).upper(), captured=captured)
            registrations = package_registry(root)
            from aek.adapters.document_layout import resolve_package_at
            for row in registrations:
                if row['version'] == version and row['package_ref'] != package.package_ref:
                    existing = resolve_package_at(root, row['package_ref'])
                    if existing.title == title:
                        raise ValueError('LAYOUT_CONFLICT: existing package needs explicit relocation, not another initialization')
            matching = [row for row in registrations if row["package_ref"] == package.package_ref]
            old_index = _optional(root, _index_path(package))
            _check_index_registration(root, package, registrations, old_index)
            if matching:
                existing = resolve_package_at(root, package.package_ref)
                if (existing.title, existing.version, existing.layout) != (title, version, package.layout):
                    raise ValueError("LAYOUT_CONFLICT: location already has another identity")
                return existing, {"created": [], "skipped": [package.package_ref], "index_row_added": False,
                                  "index_registration": _index_registration(existing)}
            if any(row['change_id'] == package.change_id for row in registrations):
                raise ValueError('DUPLICATE_ID: generated identity already belongs to another package')
            if _optional(root, package.design_root + "/reviews/package.json") is not None:
                raise ValueError("REGISTRATION_MISMATCH: existing unregistered metadata")
            inputs = _input_hashes(captured)
            if any(inputs[path] is None for path in _TEMPLATES.values()):
                raise ValueError("SOURCE_CHANGED: installed init templates missing")
            index_path = _index_path(package)
            index = _index_bytes(package, old_index)
            registration = _registration(package)
            validate_package_registry({'schema_version': 1, 'packages': [*registrations, registration]})
            destinations = {
                package.source_root + "/原始需求.md": captured[_TEMPLATES["原始需求.md"]],
                package.design_root + "/需求分析.md": captured[_TEMPLATES["需求分析.md"]],
                package.design_root + "/reviews/package.json": package_bytes(package),
                index_path: index,
                _REGISTRY: _json({"schema_version": 1, "packages": [*registrations, registration]}),
            }
            changes, skipped = [], []
            for path, after in destinations.items():
                before = _optional(root, path)
                if path.endswith(("/原始需求.md", "/需求分析.md")) and before is not None:
                    skipped.append(path)
                    continue
                changes.append({"path": path, "before": base64.b64encode(before).decode() if before is not None else None,
                                "after": base64.b64encode(after).decode()})
            journal = {"schema_version": 1, "package": asdict(package), "inputs": inputs, "changes": changes}
            encoded = _json(journal)
            if len(encoded) > _MAX_TX:
                raise ValueError("init journal exceeds bounds")
            if _inputs(root) != inputs:
                raise ValueError("SOURCE_CHANGED: init inputs changed before prepare")
            _validate_journal(root, journal, title, version, layout)
            _publish(root, _TX, encoded, None)
            if fault_after == 0:
                raise RuntimeError("injected crash after init prepare")
            for number, change in enumerate(changes, 1):
                if _inputs(root) != inputs:
                    raise ValueError("SOURCE_CHANGED: init inputs changed before publish")
                _publish(root, change["path"], base64.b64decode(change["after"]),
                         base64.b64decode(change["before"]) if change["before"] is not None else None)
                if fault_after == number:
                    raise RuntimeError("injected crash after init destination")
            result, output = _resume(root, journal, title, version, layout)
            output["skipped"] = skipped
            return result, output
    finally:
        fs.close_dir(root)
