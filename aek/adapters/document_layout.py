"""One handle-relative locator for legacy and explicitly registered packages."""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from aek.adapters import ui_filesystem as fs
from aek.core.artifact.document_layout import (
    MAX_LAYOUT_BYTES, component, load_object, load_package, make_package, relative_parts,
)


@contextmanager
def package_directory(repo_handle, relative, *, create=False):
    current = fs.dup_dir(repo_handle)
    try:
        for part in relative_parts(relative):
            child = fs.open_child_dir(current, part, create=create)
            fs.close_dir(current)
            current = child
        yield current
    finally:
        fs.close_dir(current)


def _read(repo_handle, relative):
    parts = relative_parts(relative)
    if len(parts) == 1:
        return fs.read_at(repo_handle, parts[0], MAX_LAYOUT_BYTES)
    with package_directory(repo_handle, "/".join(parts[:-1])) as parent:
        return fs.read_at(parent, parts[-1], MAX_LAYOUT_BYTES)


def package_registry(repo_handle):
    try:
        raw = _read(repo_handle, ".repo-memory-kit/document-packages.json")
    except FileNotFoundError:
        return []
    return validate_package_registry(load_object(raw))


def validate_package_registry(value):
    if (set(value) != {"schema_version", "packages"} or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["packages"]) is not list or len(value["packages"]) > 4096):
        raise ValueError("invalid package registry")
    identities, locations = set(), set()
    for row in value["packages"]:
        if (type(row) is not dict or set(row) != {"change_id", "version", "package_ref", "revision"}
                or type(row["change_id"]) is not str
                or not re.fullmatch(r"CHG-(?:[0-9]{3}|[A-F0-9]{28})", row["change_id"])
                or type(row["revision"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", row["revision"])):
            raise ValueError("invalid package registration")
        component(row["version"])
        relative_parts(row["package_ref"])
        if row["change_id"] in identities:
            raise ValueError("DUPLICATE_ID: package identity is not unique")
        if row["package_ref"] in locations:
            raise ValueError("AMBIGUOUS_PACKAGE: package location is not unique")
        identities.add(row["change_id"])
        locations.add(row["package_ref"])
    return value["packages"]


def _checked_registered_package(raw, relative, matches):
    package = load_package(raw)
    expected = {"change_id": package.change_id, "version": package.version,
                "package_ref": package.package_ref, "revision": package.revision}
    if package.package_ref != relative or matches != [expected]:
        raise ValueError("REGISTRATION_MISMATCH: current metadata is not registered at this location")
    return package


def registered_packages_at(repo_handle):
    """Bounded reverse lookup of actual registered source and design roots.

    Reads only the private registry and fixed package metadata, never document
    bodies or a recursive filesystem search. One captured registry validates
    every row; it is fenced again before returning.
    """
    try:
        _read(repo_handle, '.repo-memory-kit/document-init.tx.json')
    except FileNotFoundError:
        pass
    else:
        raise ValueError('INVALID_JOURNAL: interrupted package initialization')
    registry = package_registry(repo_handle)
    packages, size = [], 0
    for row in registry:
        raw = _read(repo_handle, row['package_ref'] + '/reviews/package.json')
        size += len(raw)
        if size > 16 * 1024 * 1024:
            raise ValueError('package metadata reverse lookup exceeds 16 MiB')
        packages.append(_checked_registered_package(raw, row['package_ref'], [row]))
    if package_registry(repo_handle) != registry:
        raise ValueError('SOURCE_CHANGED: package registry changed during reverse lookup')
    return tuple(packages)


def resolve_package_at(repo_handle, selector):
    try:
        _read(repo_handle, ".repo-memory-kit/document-init.tx.json")
    except FileNotFoundError:
        pass
    else:
        raise ValueError("INVALID_JOURNAL: interrupted package initialization needs its original init entry")
    registry = package_registry(repo_handle)
    matches = [row for row in registry if row["change_id"] == selector or row["package_ref"] == selector]
    if len(matches) > 1:
        raise ValueError("AMBIGUOUS_PACKAGE")
    relative = matches[0]["package_ref"] if matches else selector
    relative_parts(relative)
    with package_directory(repo_handle, relative):
        pass
    try:
        raw = _read(repo_handle, relative + "/reviews/package.json")
    except FileNotFoundError:
        raw = None
    if raw is not None:
        return _checked_registered_package(raw, relative, matches)
    if matches:
        raise ValueError("REGISTRATION_MISMATCH: package metadata missing")
    parts = relative_parts(relative)
    legacy = re.fullmatch(r"([0-9]{3})-(.+)", parts[-1])
    old_id = parts[-1] if re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", parts[-1]) else None
    if len(parts) != 3 or parts[:2] not in (("docs", "03-SDD"), ("docs", "01-需求")) or (legacy is None and old_id is None):
        raise ValueError("NOT_REGISTERED: non-legacy package needs explicit metadata")
    return make_package(change_id="CHG-" + legacy.group(1) if legacy is not None else old_id,
        version="legacy", title=legacy.group(2) if legacy is not None else old_id,
        layout="legacy", source_root="docs/01-需求/" + parts[-1], design_root="docs/03-SDD/" + parts[-1])


def resolve_package(repo, selector):
    root = fs.open_absolute_dir(Path(repo).absolute())
    try:
        return resolve_package_at(root, selector)
    finally:
        fs.close_dir(root)
