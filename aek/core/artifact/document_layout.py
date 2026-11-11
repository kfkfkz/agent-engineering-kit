"""Bounded package-location metadata. Location is never an approval."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

MAX_LAYOUT_BYTES = 2 * 1024 * 1024


def component(value):
    if (type(value) is not str or not value or len(value) > 180 or value in {".", ".."}
            or value[-1:] in {".", " "} or any(c in value for c in '/\\:<>"|?*')
            or any(ord(c) < 32 for c in value)
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value)):
        raise ValueError("PATH_REJECTED: unsafe package component")
    return value


def relative_parts(value):
    if type(value) is not str or len(value) > 4096 or value.startswith("/"):
        raise ValueError("PATH_REJECTED: expected repository-relative path")
    return tuple(component(part) for part in value.split("/"))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class DocumentPackage:
    schema_version: int
    change_id: str
    version: str
    title: str
    layout: str
    package_ref: str
    source_root: str
    design_root: str
    revision: str

    @property
    def material_root(self):
        return self.source_root + "/原始材料/ui-reference"


def make_package(*, change_id, version, title, layout, source_root, design_root):
    pattern = r"[A-Z][A-Z0-9_-]{0,31}" if layout == "legacy" else r"CHG-(?:[0-9]{3}|[A-F0-9]{28})"
    if (type(change_id) is not str or not re.fullmatch(pattern, change_id)
            or layout not in {"legacy", "co-located", "project"}):
        raise ValueError("invalid package identity or layout")
    component(version)
    component(title)
    for root in (source_root, design_root):
        parts = relative_parts(root)
        if len(parts) < 3 or parts[0] != "docs" or parts[1].casefold() in {"memory", "delivery-receipts"}:
            raise ValueError("PATH_REJECTED: package root must be a dedicated documentation directory")
    if layout == "co-located" and source_root != design_root:
        raise ValueError("LAYOUT_CONFLICT: co-located roots must match")
    value = {"schema_version": 1, "change_id": change_id, "version": version, "title": title,
             "layout": layout, "package_ref": design_root, "source_root": source_root, "design_root": design_root}
    return DocumentPackage(**value, revision=hashlib.sha256(_canonical(value)).hexdigest())


def package_bytes(package):
    return _canonical(asdict(package))


def load_object(raw, *, limit=MAX_LAYOUT_BYTES):
    if type(raw) is not bytes or len(raw) > limit:
        raise ValueError("package metadata exceeds bounds")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate package metadata key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique)
    if type(value) is not dict:
        raise ValueError("package metadata must be an object")
    return value


def load_package(raw):
    value = load_object(raw)
    if (set(value) != set(DocumentPackage.__dataclass_fields__)
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("unknown package metadata schema or fields")
    result = make_package(**{key: value[key] for key in (
        "change_id", "version", "title", "layout", "source_root", "design_root")})
    if value != asdict(result):
        raise ValueError("package metadata revision or root mismatch")
    return result
