"""Recoverable, bounded sidecar transaction for ArtifactPlan files.

Only Registry-derived plan/credential/binding names may be changed. A journal
contains exact preimages and intended bytes; recovery refuses any unexpected
target or unrelated consumer change. All operations keep the locked parent
directory pinned through publication and recovery.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator, Mapping

from aek.core.artifact.registry import ARTIFACT_REGISTRY
from aek.core.artifact.document_material import MAX_DOCUMENT_BYTES
from aek.core.artifact.plan import ComposedArtifactPlan, MaterialStageBinding, load_binding_bytes, load_plan_bytes
from aek.core.artifact.source_map import load_source_map, mapped_source_path
from aek.adapters.source_map import read_composed_source
from aek.adapters import ui_filesystem as fs


class PlanConflict(RuntimeError):
    """The caller's consumer-set CAS no longer matches."""


class PlanInvalid(RuntimeError):
    """A journal or externally changed file prevents safe recovery."""


_PLAN = "artifact-plan.json"
_CREDENTIAL = "artifact-plan.gate.json"
_JOURNAL = "artifact-plan.tx.json"
_LOCK = ".artifact-plan.lock"
_CLOSEOUT = "计划.closeout.json"
_MUTABLE = frozenset({_PLAN, _CREDENTIAL} | {
    f"{group_id}.plan-binding.json" for group_id in ARTIFACT_REGISTRY.groups
})
_MAX_FILE = 2_000_000
_BINARY = getattr(os, "O_BINARY", 0)
_SOURCE_NAMES = frozenset(item.filename for item in ARTIFACT_REGISTRY.artifacts.values())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_source_name(name: str) -> bool:
    if name in _SOURCE_NAMES:
        return True
    try:
        mapped_source_path(name)
        return True
    except ValueError:
        return False


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


@contextmanager
def _lock_file(directory, *, name=_LOCK) -> Iterator[None]:
    fd = fs.open_file(directory, name, os.O_RDWR | os.O_CREAT | _BINARY,
                      shared_lock_file=True)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PlanInvalid("plan lock is not a regular file")
        if os.name == "nt":
            import msvcrt
            # The CRT locks past EOF, including an empty installer lock file.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class _LockedMaterialView:
    """Operation-local storage view, never a serialized approval capability."""

    def __init__(self, store, source, reviews, *, writable=True):
        self._store, self._source, self._reviews = store, source, reviews
        self._active = True
        self._writable = writable

    def _require_active(self):
        if not self._active:
            raise PlanInvalid("locked material view has expired")

    @property
    def source_identity(self) -> str:
        self._require_active()
        return fs.directory_identity(self._source)

    @property
    def consumer_digest(self) -> str:
        self._require_active()
        return self._store._consumer_digest(self._reviews)

    def read_source(self, name: str) -> bytes:
        self._require_active()
        if name in _SOURCE_NAMES:
            return fs.read_at(self._source, name, MAX_DOCUMENT_BYTES)
        raw = self._store._read(self._reviews, _PLAN)
        plan = load_plan_bytes(raw) if raw is not None else None
        if not isinstance(plan, ComposedArtifactPlan):
            raise ValueError("source is not a Registry document")
        return read_composed_source(self._store.reviews.parent, load_source_map(plan.source_map), name)

    def read_gate(self, stage: str) -> bytes | None:
        self._require_active()
        if stage not in ARTIFACT_REGISTRY.groups:
            raise ValueError("unknown Gate stage")
        return self._store._read(self._reviews, stage + ".gate.json")

    def read_closeout(self) -> bytes | None:
        self._require_active()
        return self._store._read(self._reviews, "计划.closeout.json")

    def snapshot(self, *, for_material_recovery=False) -> dict[str, bytes]:
        self._require_active()
        return self._store._gate_snapshot_locked(self._reviews, for_material_recovery=for_material_recovery)

    def _check_material_epoch(self, epoch, context_loader):
        self._require_active()
        context = epoch["context"]
        if (context["source_root"] != self.source_identity
                or context["package_ref"] != self._store.reviews.parent.as_posix()
                or context_loader(epoch["stage"]) != context
                or {name: _sha(self.read_source(name)) for name in epoch["source_hashes"]} != epoch["source_hashes"]
                or {name: _sha(self.read_source(name)) for name in epoch["dependency_hashes"]} != epoch["dependency_hashes"]):
            raise PlanInvalid("SOURCE_CHANGED: material recovery root, sources or policy changed")

    def recover_material(self, context_loader):
        self._require_active()
        if not self._writable:
            raise PlanInvalid("read-only view cannot recover")
        return self._store._recover_locked(self._reviews, material_view=self, context_loader=context_loader)

    def recover_closeout(self, context_loader):
        self._require_active()
        if not self._writable:
            raise PlanInvalid("read-only view cannot recover")
        journal = self._store._journal(self._reviews)
        if journal is not None and journal["op"] != "closeout-publish":
            raise PlanInvalid("pending transaction requires its original recovery entry")
        return self._store._recover_locked(self._reviews, material_view=self, context_loader=context_loader)

    def commit_closeout(self, content, *, expected_consumers, epoch, context_loader, fault_after=None):
        """Publish only the original closeout controller's source-bound result."""
        self._require_active()
        if not self._writable or not callable(context_loader):
            raise PlanInvalid("closeout publication requires the live locked context")
        changes = {_CLOSEOUT: content}
        self._store._validate_commit(changes, expected_consumers, fault_after, allowed={_CLOSEOUT})
        self._store._validate_closeout(content, epoch)
        if json.loads(content.decode("utf-8"))["plan_gate_sha256"] != _sha(self.read_gate("计划") or b""):
            raise PlanInvalid("closeout does not match the current original Gate")
        self._check_material_epoch(epoch, context_loader)
        self._store._commit_locked(self._reviews, changes, expected_consumers=expected_consumers,
            operation="closeout-publish", fault_after=fault_after, material_epoch=epoch,
            material_view=self, context_loader=context_loader)

    def commit_material_refresh(self, changes, *, expected_consumers, epoch, context_loader, fault_after=None):
        self._require_active()
        if not self._writable or not callable(context_loader):
            raise PlanInvalid("material publication requires the live locked context")
        self._store._validate_commit(changes, expected_consumers, fault_after)
        name = epoch["stage"] + ".plan-binding.json"
        if set(changes) != {name} or changes[name] is None:
            raise ValueError("material transaction may only publish its stage binding")
        binding = load_binding_bytes(changes[name])
        if (not isinstance(binding, MaterialStageBinding) or binding.stage != epoch["stage"]
                or binding.material_revision["context"] != epoch["context"]
                or binding.material_revision["raw_after"] != epoch["source_hashes"]):
            raise ValueError("material transaction does not match its source epoch")
        self._check_material_epoch(epoch, context_loader)
        self._store._commit_locked(self._reviews, changes, expected_consumers=expected_consumers,
            operation="material-refresh", fault_after=fault_after, material_epoch=epoch,
            material_view=self, context_loader=context_loader)

    def publish(self, changes: Mapping[str, bytes | None], *, expected_consumers: str,
                fault_after: int | None = None) -> None:
        """Use the original bounded transaction without reacquiring its lock."""
        self._require_active()
        if not self._writable:
            raise PlanInvalid("read-only material view cannot publish")
        self._store._validate_commit(changes, expected_consumers, fault_after)
        self._store._commit_locked(self._reviews, changes,
                                  expected_consumers=expected_consumers,
                                  operation="publish", fault_after=fault_after)


class PlanStore:
    def __init__(self, reviews: Path) -> None:
        self.reviews = Path(reviews).absolute()
        try:
            directory = fs.open_absolute_dir(self.reviews)
            fs.close_dir(directory)
        except (OSError, ValueError) as exc:
            raise PlanInvalid("reviews must be an existing, real directory with safe ancestors") from exc

    @contextmanager
    def locked_view(self, source_root: Path):
        """Pin one package and its reviews under the original lifecycle lock.

        Reads do not confer approval. The view expires before handles close;
        consumers must not retain it across operations or reacquire this lock.
        """
        with self._view(source_root, writable=True) as view:
            yield view

    @contextmanager
    def read_view(self, source_root: Path):
        """Pinned read-only view; readers still reject interrupted journals."""
        with self._view(source_root, writable=False) as view:
            yield view

    @contextmanager
    def _view(self, source_root: Path, *, writable: bool):
        if Path(source_root).absolute() != self.reviews.parent:
            raise PlanInvalid("source root must own this reviews directory")
        try:
            source = fs.open_absolute_dir(self.reviews.parent)
            try:
                directory = fs.open_child_dir(source, self.reviews.name)
                try:
                    with (_lock_file(directory) if writable else nullcontext()):
                        view = _LockedMaterialView(self, source, directory, writable=writable)
                        try:
                            yield view
                        finally:
                            view._active = False
                finally:
                    fs.close_dir(directory)
            finally:
                fs.close_dir(source)
        except (OSError, ValueError) as exc:
            raise PlanInvalid(f"unsafe or unreadable plan storage: {exc}") from exc

    @contextmanager
    def _locked(self):
        with self.locked_view(self.reviews.parent) as view:
            yield view._reviews

    @contextmanager
    def legacy_gate_lock(self) -> Iterator[None]:
        """Serialize legacy gate consumers with dynamic sidecar transactions.

        Callers must recheck dynamic sidecars *inside* this lock before writing.
        An interrupted plan journal is deliberately not recovered here: the
        legacy path must refuse it rather than infer a static plan.
        """
        with self._locked():
            yield

    @contextmanager
    def gate_snapshot(self) -> Iterator[dict[str, bytes]]:
        """Serialize a gate write with plan transactions and expose its state.

        Gate callers must evaluate dynamic sidecars from the same lock epoch in
        which they write the legacy-compatible gate record.  An interrupted
        plan transaction is not recovered here: a gate may not guess whether
        the preimage or intended plan was authoritative.
        """
        with self.locked_view(self.reviews.parent) as view:
            yield view.snapshot()

    def _gate_snapshot_locked(self, directory, *, for_material_recovery=False) -> dict[str, bytes]:
        journal = self._journal(directory)
        if journal is not None and not (for_material_recovery and journal["schema_version"] == 2):
            raise PlanInvalid("interrupted plan transaction blocks gate write")
        if any(name.startswith(".artifact-plan-write-") for name in fs.listdir(directory)):
            raise PlanInvalid("orphan plan publish temporary file")
        result = {}
        for name in self._consumer_names(directory):
            if name in _MUTABLE:
                content = self._read(directory, name)
                if content is not None:
                    result[name] = content
        return result

    def _read(self, directory, name: str) -> bytes | None:
        try:
            fd = fs.open_file(directory, name, os.O_RDONLY | _BINARY)
        except FileNotFoundError:
            return None
        try:
            return fs.read_fd(fd, _MAX_FILE)
        finally:
            os.close(fd)

    def _atomic_write(self, directory, name: str, content: bytes) -> None:
        if len(content) > _MAX_FILE:
            raise ValueError("plan sidecar exceeds size limit")
        temporary = ".artifact-plan-write-" + uuid.uuid4().hex
        fd = fs.open_file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY)
        owned = True
        try:
            remaining = content
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short sidecar write")
                remaining = remaining[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            fs.replace(directory, temporary, directory, name)
            owned = False
            fs.fsync_dir(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            if owned:
                fs.unlink(directory, temporary)

    def _consumer_names(self, directory) -> tuple[str, ...]:
        names: list[str] = []
        for name in fs.listdir(directory):
            if name in {_JOURNAL, _LOCK} or name.startswith(".artifact-plan-write-"):
                continue
            if name in {_PLAN, _CREDENTIAL, "计划.closeout.json"} or name.endswith(
                    (".gate.json", ".plan-binding.json")):
                if name.endswith(".plan-binding.json") and name not in _MUTABLE:
                    raise PlanInvalid(f"unknown plan binding consumer: {name}")
                names.append(name)
        return tuple(sorted(names))

    def _consumer_digest(self, directory, exclude: frozenset[str] = frozenset()) -> str:
        values = []
        for name in self._consumer_names(directory):
            if name in exclude:
                continue
            content = self._read(directory, name)
            if content is not None:
                values.append((name, _sha(content)))
        return _sha(_json_bytes(values))

    def consumer_digest(self) -> str:
        with self._locked() as directory:
            self._recover_locked(directory)
            return self._consumer_digest(directory)

    def snapshot(self) -> dict[str, bytes]:
        """Recover and read all dynamic consumers under one plan lock.

        A stranded temporary file is not a valid plan state. Callers may
        inspect it manually, but must not silently fall back to legacy gates.
        """
        return self.snapshot_with_digest()[0]

    def snapshot_with_digest(self) -> tuple[dict[str, bytes], str]:
        """Return sidecars and their CAS consumer digest from one locked view."""
        with self._locked() as directory:
            self._recover_locked(directory)
            if any(name.startswith(".artifact-plan-write-")
                   for name in fs.listdir(directory)):
                raise PlanInvalid("orphan plan publish temporary file")
            names = self._consumer_names(directory)
            result: dict[str, bytes] = {}
            for name in names:
                if name not in _MUTABLE:
                    continue
                content = self._read(directory, name)
                if content is not None:
                    result[name] = content
            return result, self._consumer_digest(directory)

    def _journal(self, directory) -> dict[str, object] | None:
        content = self._read(directory, _JOURNAL)
        if content is None:
            return None
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanInvalid("plan journal is malformed") from exc
        if content != _json_bytes(raw):
            raise PlanInvalid("plan journal is not canonical JSON")
        material = isinstance(raw, dict) and type(raw.get("schema_version")) is int and raw["schema_version"] == 2
        fields = {
                "schema_version", "tx_id", "op", "phase", "other_consumers_digest",
                "entries"} | ({"material_epoch"} if material else set())
        if (not isinstance(raw, dict) or set(raw) != fields
                or type(raw["schema_version"]) is not int or raw["schema_version"] not in (1, 2)
                or (raw["op"] not in {"material-refresh", "closeout-publish"} if material else raw["op"] not in {"publish", "rollback-all"})
                or raw["phase"] not in {"PREPARED", "COMMITTED"}
                or not isinstance(raw["tx_id"], str)
                or not isinstance(raw["entries"], list) or not raw["entries"]):
            raise PlanInvalid("plan journal has unknown schema or fields")
        try:
            uuid.UUID(raw["tx_id"])
        except (ValueError, AttributeError) as exc:
            raise PlanInvalid("plan journal transaction ID is invalid") from exc
        if not isinstance(raw["other_consumers_digest"], str) or len(
                raw["other_consumers_digest"]) != 64:
            raise PlanInvalid("plan journal consumer digest is invalid")
        names: set[str] = set()
        allowed = {_CLOSEOUT} if raw["op"] == "closeout-publish" else _MUTABLE
        for row in raw["entries"]:
            if (not isinstance(row, dict) or set(row) != {
                    "name", "preimage", "preimage_digest", "intended", "intended_digest"}
                    or row["name"] not in allowed or row["name"] in names):
                raise PlanInvalid("plan journal entry is unsafe or duplicate")
            names.add(row["name"])
            for field in ("preimage", "intended"):
                encoded = row[field]
                digest = row[field + "_digest"]
                if encoded is None:
                    if digest is not None:
                        raise PlanInvalid("plan journal null digest mismatch")
                else:
                    if not isinstance(encoded, str) or not isinstance(digest, str):
                        raise PlanInvalid("plan journal image is invalid")
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                    except (ValueError, base64.binascii.Error) as exc:
                        raise PlanInvalid("plan journal base64 is invalid") from exc
                    if len(decoded) > _MAX_FILE or _sha(decoded) != digest:
                        raise PlanInvalid("plan journal image digest is invalid")
        if material:
            epoch = raw["material_epoch"]
            if (type(epoch) is not dict or set(epoch) != {"stage", "context", "source_hashes", "dependency_hashes"}
                    or type(epoch["stage"]) is not str or epoch["stage"] not in ARTIFACT_REGISTRY.groups
                    or len(raw["entries"]) != 1
                    or raw["entries"][0]["name"] != (_CLOSEOUT if raw["op"] == "closeout-publish" else epoch["stage"] + ".plan-binding.json")
                    or raw["entries"][0]["intended"] is None):
                raise PlanInvalid("unsafe material journal epoch")
            dependencies = epoch["dependency_hashes"]
            if (type(dependencies) is not dict or type(epoch["source_hashes"]) is not dict
                    or set(dependencies) & set(epoch["source_hashes"])
                    or any(not _valid_source_name(name) or type(value) is not str or len(value) != 64
                           or any(c not in "0123456789abcdef" for c in value) for name, value in dependencies.items())):
                raise PlanInvalid("unsafe material dependency epoch")
            try:
                intended = self._decode(raw["entries"][0]["intended"])
                if raw["op"] == "closeout-publish":
                    self._validate_closeout(intended, epoch)
                else:
                    binding = load_binding_bytes(intended)
                    if (not isinstance(binding, MaterialStageBinding)
                            or binding.stage != epoch["stage"]
                            or binding.material_revision["context"] != epoch["context"]
                            or binding.material_revision["raw_after"] != epoch["source_hashes"]):
                        raise ValueError("journal material does not match intent")
            except (KeyError, TypeError, ValueError) as exc:
                raise PlanInvalid("invalid material journal binding") from exc
        return raw

    @staticmethod
    def _validate_closeout(content, epoch):
        result = json.loads(content.decode("utf-8"))
        context, hashes = epoch["context"], epoch["source_hashes"]
        plan_names = {ARTIFACT_REGISTRY.artifacts[item].filename
                      for item in ARTIFACT_REGISTRY.groups["计划"].artifact_ids}
        if (epoch["stage"] != "计划" or type(hashes) is not dict or not hashes
                or not set(hashes) <= plan_names
                or any(type(value) is not str or len(value) != 64
                       or any(c not in "0123456789abcdef" for c in value) for value in hashes.values())
                or type(context) is not dict or set(context) != {"package_ref", "source_root", "policy_digest", "source_set"}
                or any(type(context[key]) is not str or not context[key] for key in ("package_ref", "source_root", "policy_digest"))
                or len(context["policy_digest"]) != 64
                or context["source_set"] != sorted(hashes)
                or type(result) is not dict or result.get("stage") != "计划"
                or result.get("status") != "PASS" or result.get("kind") != "execution_closeout"
                or result.get("doc_hashes") != hashes
                or type(result.get("plan_gate_sha256")) is not str or len(result["plan_gate_sha256"]) != 64):
            raise ValueError("invalid closeout intent or source epoch")

    @staticmethod
    def _decode(value: str | None) -> bytes | None:
        return base64.b64decode(value) if value is not None else None

    def _remove_journal(self, directory) -> None:
        fs.unlink(directory, _JOURNAL)
        fs.fsync_dir(directory)

    def _apply(self, directory, name: str, intended: bytes | None, preimage_digest: str | None) -> None:
        current = self._read(directory, name)
        current_digest = _sha(current) if current is not None else None
        if current_digest != preimage_digest:
            raise PlanInvalid(f"sidecar changed before publish: {name}")
        if intended is None:
            if current is not None:
                fs.unlink(directory, name)
                fs.fsync_dir(directory)
        else:
            self._atomic_write(directory, name, intended)

    def _recover_locked(self, directory, *, material_view=None, context_loader=None) -> str:
        journal = self._journal(directory)
        if journal is None:
            return "NONE"
        if journal["schema_version"] == 2:
            if material_view is None or not callable(context_loader):
                raise PlanInvalid("material transaction requires explicit source/policy recovery")
            material_view._check_material_epoch(journal["material_epoch"], context_loader)
            if journal["op"] == "closeout-publish":
                intent = json.loads(self._decode(journal["entries"][0]["intended"]).decode("utf-8"))
                if intent["plan_gate_sha256"] != _sha(material_view.read_gate("计划") or b""):
                    raise PlanInvalid("closeout recovery original Gate changed")
        entries = journal["entries"]
        names = frozenset(row["name"] for row in entries)
        if self._consumer_digest(directory, names) != journal["other_consumers_digest"]:
            raise PlanInvalid("plan consumers changed during interrupted transaction")
        states = []
        for row in entries:
            current = self._read(directory, row["name"])
            digest = _sha(current) if current is not None else None
            if digest not in {row["preimage_digest"], row["intended_digest"]}:
                raise PlanInvalid(f"sidecar is neither preimage nor intended: {row['name']}")
            states.append(digest == row["intended_digest"])
        if journal["phase"] == "COMMITTED":
            if not all(states):
                raise PlanInvalid("committed plan journal has an incomplete target")
            self._remove_journal(directory)
            return "CLEANED"
        if not any(states) and journal["schema_version"] == 1:
            self._remove_journal(directory)
            return "ROLLED_BACK"
        for row, is_intended in zip(entries, states):
            if not is_intended:
                if journal["schema_version"] == 2:
                    material_view._check_material_epoch(journal["material_epoch"], context_loader)
                self._apply(directory, row["name"], self._decode(row["intended"]),
                            row["preimage_digest"])
        if journal["schema_version"] == 2:
            material_view._check_material_epoch(journal["material_epoch"], context_loader)
        journal["phase"] = "COMMITTED"
        self._atomic_write(directory, _JOURNAL, _json_bytes(journal))
        self._remove_journal(directory)
        return "ROLLED_FORWARD"

    def recover(self) -> str:
        with self._locked() as directory:
            return self._recover_locked(directory)

    @staticmethod
    def _validate_commit(changes, expected_consumers, fault_after, *, allowed=_MUTABLE) -> None:
        if (not isinstance(changes, Mapping) or not changes
                or any(name not in allowed or (value is not None and
                        (not isinstance(value, bytes) or len(value) > _MAX_FILE))
                       for name, value in changes.items())):
            raise ValueError("plan changes contain unsafe name or bytes")
        if not isinstance(expected_consumers, str) or len(expected_consumers) != 64:
            raise ValueError("expected consumer digest is invalid")
        if fault_after is not None and (type(fault_after) is not int or fault_after < 0):
            raise ValueError("invalid fault injection step")

    def _commit(
        self, changes: Mapping[str, bytes | None], *, expected_consumers: str,
        operation: str, fault_after: int | None,
    ) -> None:
        self._validate_commit(changes, expected_consumers, fault_after)
        with self._locked() as directory:
            self._commit_locked(directory, changes, expected_consumers=expected_consumers,
                                operation=operation, fault_after=fault_after)

    def _commit_locked(
        self, directory, changes: Mapping[str, bytes | None], *, expected_consumers: str,
        operation: str, fault_after: int | None,
        material_epoch=None, material_view=None, context_loader=None,
    ) -> None:
        self._recover_locked(directory, material_view=material_view, context_loader=context_loader)
        if self._consumer_digest(directory) != expected_consumers:
            raise PlanConflict("plan consumer set changed")
        effective = {name: value for name, value in changes.items()
                     if self._read(directory, name) != value}
        if not effective:
            return
        if operation == "publish":
            names = sorted(effective, key=lambda name: (
                0 if name == _PLAN else 1 if name == _CREDENTIAL else 2, name))
        else:
            names = sorted(effective, key=lambda name: (
                0 if name.endswith(".plan-binding.json") else
                1 if name == _CREDENTIAL else 2, name))
        entries = []
        for name in names:
            prior = self._read(directory, name)
            intended = effective[name]
            entries.append({
                "name": name,
                "preimage": base64.b64encode(prior).decode("ascii") if prior is not None else None,
                "preimage_digest": _sha(prior) if prior is not None else None,
                "intended": base64.b64encode(intended).decode("ascii")
                if intended is not None else None,
                "intended_digest": _sha(intended) if intended is not None else None,
            })
        journal: dict[str, object] = {
            "schema_version": 1, "tx_id": str(uuid.uuid4()), "op": operation,
            "phase": "PREPARED", "other_consumers_digest":
            self._consumer_digest(directory, frozenset(names)), "entries": entries,
        }
        if material_epoch is not None:
            journal.update(schema_version=2, material_epoch=material_epoch)
            material_view._check_material_epoch(material_epoch, context_loader)
        self._atomic_write(directory, _JOURNAL, _json_bytes(journal))
        if fault_after == 0:
            raise RuntimeError("injected crash before first plan publish")
        for index, row in enumerate(entries, 1):
            if material_epoch is not None:
                material_view._check_material_epoch(material_epoch, context_loader)
            self._apply(directory, row["name"], self._decode(row["intended"]),
                        row["preimage_digest"])
            if fault_after == index:
                raise RuntimeError(f"injected crash after plan publish {index}")
        if material_epoch is not None:
            material_view._check_material_epoch(material_epoch, context_loader)
        journal["phase"] = "COMMITTED"
        self._atomic_write(directory, _JOURNAL, _json_bytes(journal))
        if material_epoch is not None and fault_after == 2:
            raise RuntimeError("injected crash after material commit record")
        self._remove_journal(directory)
        if material_epoch is not None and fault_after == 3:
            raise RuntimeError("injected lost material acknowledgement")

    def publish(
        self, changes: Mapping[str, bytes | None], *, expected_consumers: str,
        fault_after: int | None = None,
    ) -> None:
        self._commit(changes, expected_consumers=expected_consumers,
                     operation="publish", fault_after=fault_after)

    def rollback_all(
        self, *, expected_consumers: str, fault_after: int | None = None,
    ) -> None:
        # Recovery must precede the no-op decision. A crash after the last
        # deletion leaves no target files but still leaves a PREPARED journal.
        with self._locked() as directory:
            self._recover_locked(directory)
            present = {name: None for name in _MUTABLE
                       if self._read(directory, name) is not None}
        if not present:
            return
        self._commit(present, expected_consumers=expected_consumers,
                     operation="rollback-all", fault_after=fault_after)
