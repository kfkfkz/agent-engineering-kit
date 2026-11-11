"""Private immutable content storage; stored bytes never prove approval."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from aek.adapters import ui_filesystem as fs
from aek.core.artifact.document_material import MAX_DOCUMENT_BYTES
from aek.core.artifact.registry import ARTIFACT_REGISTRY


class MaterialCorrupt(ValueError):
    """An existing immutable object is not its expected bytes."""


def _digest(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("material identity must be a SHA-256")
    return value


class MaterialArchive:
    def __init__(self, reviews: Path):
        self.reviews = Path(reviews)

    @contextmanager
    def _directory(self, *, create: bool):
        root = fs.open_absolute_dir(self.reviews)
        try:
            child = fs.open_child_dir(root, "materials", create=create)
            try:
                if create:
                    fs.fsync_dir(root)
                yield child
            finally:
                fs.close_dir(child)
        finally:
            fs.close_dir(root)

    @staticmethod
    def _read(directory, sha):
        raw = fs.read_at(directory, sha, MAX_DOCUMENT_BYTES)
        if not raw or hashlib.sha256(raw).hexdigest() != sha:
            raise MaterialCorrupt("material digest mismatch")
        return raw

    def put(self, raw: bytes, *, expected_sha256: str) -> str:
        sha = _digest(expected_sha256)
        if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_DOCUMENT_BYTES
                or hashlib.sha256(raw).hexdigest() != sha):
            raise ValueError("material bytes do not match the bounded identity")
        with self._directory(create=True) as directory:
            try:
                self._read(directory, sha)
                return sha
            except FileNotFoundError:
                pass
            name = ".material-" + uuid.uuid4().hex
            fd = fs.open_file(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            owned = True
            try:
                try:
                    remaining = memoryview(raw)
                    while remaining:
                        count = os.write(fd, remaining)
                        if count <= 0:
                            raise OSError("short material write")
                        remaining = remaining[count:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    fs.rename_noreplace(directory, name, directory, sha)
                    owned = False
                except FileExistsError:
                    # Another writer may publish the same immutable bytes,
                    # never permission to replace or repair its object.
                    self._read(directory, sha)
                fs.fsync_dir(directory)
                return sha
            finally:
                if owned:
                    fs.unlink(directory, name)

    def get(self, expected_sha256: str) -> bytes:
        sha = _digest(expected_sha256)
        with self._directory(create=False) as directory:
            return self._read(directory, sha)

    def publish_gate(self, source_root: Path, gate_bytes: bytes, *, capture_sources=None) -> None:
        """Persist an already authorized Gate with its exact material.

        This is storage, not a Judge or approval API. The original caller must
        hold its plan lock and complete its original checks/approval first.
        This bounded source fence and safe publication do not authorize a
        StageBinding, a model result, or a user-supplied PASS declaration.
        """
        source_root = Path(source_root)
        if (self.reviews != source_root / "reviews" or type(gate_bytes) is not bytes
                or not 1 <= len(gate_bytes) <= MAX_DOCUMENT_BYTES):
            raise ValueError("Gate storage must belong to the source package")
        gate = json.loads(gate_bytes.decode("utf-8"))
        if type(gate) is not dict or gate.get("status") != "PASS":
            raise ValueError("only an original PASS Gate has approved material")
        group = ARTIFACT_REGISTRY.get_group(gate["stage"])
        allowed = {ARTIFACT_REGISTRY.get_artifact(item).filename for item in group.artifact_ids}
        hashes = gate.get("doc_hashes")
        if (type(hashes) is not dict or not hashes
                or (capture_sources is None and not set(hashes) <= allowed)):
            raise ValueError("Gate material must name participating source documents")
        for sha in hashes.values():
            _digest(sha)
        root = fs.open_absolute_dir(source_root)
        try:
            reviews = fs.open_child_dir(root, "reviews")
            try:
                name = gate["stage"] + ".gate.json"
                def current_gate():
                    try:
                        return fs.read_at(reviews, name, MAX_DOCUMENT_BYTES)
                    except FileNotFoundError:
                        return None
                prior = current_gate()
                def current_sources():
                    if capture_sources is not None:
                        return capture_sources()
                    return {doc: fs.read_at(root, doc, MAX_DOCUMENT_BYTES) for doc in hashes}
                materials = current_sources()
                if set(materials) != set(hashes):
                    raise MaterialCorrupt("Gate source set differs from the current captured sources")
                for doc, raw in materials.items():
                    self.put(raw, expected_sha256=hashes[doc])
                if prior is not None:
                    self.put(prior, expected_sha256=hashlib.sha256(prior).hexdigest())
                self.put(gate_bytes, expected_sha256=hashlib.sha256(gate_bytes).hexdigest())
                # An unpublished Gate temporary cannot change PlanStore's
                # consumer state. Keep its name distinct from plan journals
                # so the final source fence can verify existing prerequisites.
                temporary = ".material-gate-write-" + uuid.uuid4().hex
                fd = fs.open_file(reviews, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                owned = True
                try:
                    try:
                        remaining = memoryview(gate_bytes)
                        while remaining:
                            count = os.write(fd, remaining)
                            if count <= 0:
                                raise OSError("short Gate material write")
                            remaining = remaining[count:]
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    if current_sources() != materials or current_gate() != prior:
                        raise MaterialCorrupt("Gate sources or current credential changed before publish")
                    fs.replace(reviews, temporary, reviews, name)
                    owned = False
                    fs.fsync_dir(reviews)
                finally:
                    if owned:
                        fs.unlink(reviews, temporary)
            finally:
                fs.close_dir(reviews)
        finally:
            fs.close_dir(root)
