"""Pinned synthetic Git fixtures and development-only independent directories."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.scenario import ScenarioError, _path

from aek.adapters.atomic_file import AtomicFileStore

from .process import run_process
from .scenario_file import LoadedScenario, load_scenario

MAX_FILES = 256
MAX_FILE_BYTES = 1024 * 1024
MAX_FIXTURE_BYTES = 16 * 1024 * 1024


class PreparationError(ValueError):
    """Stable reason code, with no raw command output or host paths."""


@dataclass(frozen=True)
class PreparedWorkspace:
    variant: str
    path: Path
    home: Path
    cache: Path
    session: Path
    baseline_commit: str


@dataclass(frozen=True)
class PreparedPair:
    root: Path
    workspaces: tuple[PreparedWorkspace, ...]
    manifest_bytes: bytes
    manifest_sha256: str

    @property
    def manifest(self) -> dict:
        return json.loads(self.manifest_bytes)

    def workspace(self, variant: str) -> PreparedWorkspace:
        for item in self.workspaces:
            if item.variant == variant:
                return item
        raise ValueError("INVALID_VARIANT")


def _git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
    return env


def _git(root: Path, *args: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    result = run_process(
        ("git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
         "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
         "-c", "user.name=AEK benchmark", "-c", "user.email=benchmark@example.invalid",
         "-C", str(root), *args), root, timeout_seconds=10,
        max_output_bytes=max_bytes, environment=_git_environment())
    if result.reason_code != "PROCESS_COMPLETED" or result.exit_code != 0:
        raise PreparationError("FIXTURE_GIT_FAILED")
    return result.stdout


def _snapshot(loaded: LoadedScenario) -> tuple[tuple[str, str, bytes], ...]:
    try:
        current = load_scenario(loaded.source)
        if current != loaded:
            raise PreparationError("SCENARIO_SOURCE_CHANGED")
        root = loaded.scenario_root / loaded.scenario.fixture_path
        candidate = loaded.scenario_root
        for part in Path(loaded.scenario.fixture_path).parts:
            candidate /= part
            if candidate.is_symlink() or not candidate.is_dir():
                raise PreparationError("FIXTURE_DIRECTORY_INVALID")
        if (root / ".git").is_symlink() or not (root / ".git").is_dir():
            raise PreparationError("FIXTURE_REPOSITORY_REQUIRED")
        commit = loaded.scenario.fixture_commit
        if _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode("ascii").strip() != commit:
            raise PreparationError("FIXTURE_COMMIT_INVALID")
        tree = _git(root, "ls-tree", "-r", "-z", commit)
        entries = tree.rstrip(b"\0").split(b"\0") if tree else []
        if not 0 < len(entries) <= MAX_FILES:
            raise PreparationError("FIXTURE_FILE_COUNT_INVALID")
        files = []
        portable_names: dict[str, str] = {}
        total = 0
        for entry in entries:
            header, name = entry.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split(" ")
            path = _path(name.decode("utf-8", errors="strict"))
            if mode not in {"100644", "100755"} or kind != "blob":
                raise PreparationError("FIXTURE_ENTRY_TYPE_UNSUPPORTED")
            for part in path.split("/"):
                if (part.rstrip(". ") != part or part.casefold() == ".git"
                        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                        or any(char in part for char in '<>"|')):
                    raise PreparationError("FIXTURE_PATH_NOT_PORTABLE")
            parts = path.split("/")
            for end in range(1, len(parts) + 1):
                prefix = "/".join(parts[:end])
                folded = prefix.casefold()
                if folded in portable_names and portable_names[folded] != prefix:
                    raise PreparationError("FIXTURE_PATH_COLLISION")
                portable_names[folded] = prefix
            if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", oid):
                raise PreparationError("FIXTURE_OBJECT_INVALID")
            content = _git(root, "cat-file", "blob", oid)
            total += len(content)
            if total > MAX_FIXTURE_BYTES:
                raise PreparationError("FIXTURE_SIZE_EXCEEDED")
            files.append((path, mode, content))
        files.sort(key=lambda item: item[0])
        payload = {"files": [{"path": path, "mode": mode,
                              "sha256": hashlib.sha256(content).hexdigest()}
                             for path, mode, content in files]}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
        if digest != loaded.scenario.fixture_sha256:
            raise PreparationError("FIXTURE_DIGEST_MISMATCH")
        return tuple(files)
    except (OSError, UnicodeError, ScenarioError) as exc:
        raise PreparationError("FIXTURE_SOURCE_INVALID") from exc


def prepare_pair(loaded: LoadedScenario, output_parent: Path) -> PreparedPair:
    """Snapshot before creating output; never overwrite or adopt an existing run."""
    if (loaded.scenario.timeout_seconds > 3600 or loaded.scenario.max_turns is not None
            or loaded.scenario.repeat != 1):
        raise PreparationError("FAKE_BUDGET_UNSUPPORTED")
    files = _snapshot(loaded)
    output_parent = Path(output_parent).absolute()
    if output_parent.is_symlink() or not output_parent.is_dir():
        raise PreparationError("OUTPUT_DIRECTORY_INVALID")
    output_parent = output_parent.resolve()
    fixture = (loaded.scenario_root / loaded.scenario.fixture_path).resolve()
    if output_parent == fixture or fixture in output_parent.parents:
        raise PreparationError("OUTPUT_INSIDE_FIXTURE")
    root = Path(tempfile.mkdtemp(prefix="aek-pair-", dir=output_parent))
    workspaces = []
    try:
        for variant in ("vanilla", "aek"):
            base = root / variant
            base.mkdir(mode=0o700)
            directories = tuple(base / name for name in ("workspace", "home", "cache", "session"))
            for directory in directories:
                directory.mkdir(mode=0o700)
            workspace, home, cache, session = directories
            for path, mode, content in files:
                destination = workspace / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
                destination.chmod(0o755 if mode == "100755" else 0o644)
            _git(workspace, "init", "--template=", "--initial-branch=benchmark")
            _git(workspace, "add", "--force", "--all", "--", ".")
            _git(workspace, "commit", "-m", "Pinned benchmark snapshot")
            baseline = _git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
            workspaces.append(PreparedWorkspace(variant, workspace, home, cache, session, baseline))
        if workspaces[0].baseline_commit != workspaces[1].baseline_commit:
            raise PreparationError("WORKSPACE_BASELINE_MISMATCH")
        manifest = {
            "schema_version": 1, "pair_id": root.name, "status": "PREPARED",
            "execution_scope": "development_directory_separation",
            "scenario_digest": loaded.scenario.digest, "scenario_source_sha256": loaded.source_sha256,
            "execution_budget": {"timeout_seconds": loaded.scenario.timeout_seconds,
                                 "max_turns": None, "repeat": 1},
            "fixture": {"commit": loaded.scenario.fixture_commit,
                        "sha256": loaded.scenario.fixture_sha256,
                        "digest_recipe": "sorted-file-mode-content-manifest-v1"},
            "workspaces": [{"variant": item.variant,
                            "path": f"{item.variant}/workspace", "home": f"{item.variant}/home",
                            "cache": f"{item.variant}/cache", "session": f"{item.variant}/session",
                            "baseline_commit": item.baseline_commit} for item in workspaces],
            "isolation": {"filesystem": "NOT_EVALUATED", "network": "NOT_EVALUATED"},
            "treatment": "NOT_APPLIED", "live_ready": False,
        }
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        AtomicFileStore(root).write("manifest.json", raw)
        return PreparedPair(root, tuple(workspaces), raw, hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError) as exc:
        # Retain this owned directory for diagnosis; never recursively delete supplied paths.
        AtomicFileStore(root).write("preparation-error.json", b'{"status":"INVALID"}')
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError("WORKSPACE_PREPARATION_FAILED") from exc


def load_prepared_pair(root: Path, *, expected_manifest_sha256: str) -> PreparedPair:
    """Resume only a manifest selected by the trusted caller's external digest."""
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise PreparationError("PREPARED_DIRECTORY_INVALID")
    root = root.resolve()
    raw = AtomicFileStore(root).read("manifest.json")
    if (not isinstance(expected_manifest_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256)
            or raw is None or hashlib.sha256(raw).hexdigest() != expected_manifest_sha256):
        raise PreparationError("PREPARED_MANIFEST_DIGEST_MISMATCH")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        manifest = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(manifest, dict) or set(manifest) != {
                "schema_version", "pair_id", "status", "execution_scope", "scenario_digest",
                "scenario_source_sha256", "execution_budget", "fixture", "workspaces", "isolation", "treatment", "live_ready"}
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or manifest["pair_id"] != root.name or manifest["status"] != "PREPARED"
                or manifest["execution_scope"] != "development_directory_separation"
                or manifest["isolation"] != {"filesystem": "NOT_EVALUATED", "network": "NOT_EVALUATED"}
                or manifest["treatment"] != "NOT_APPLIED" or manifest["live_ready"] is not False):
            raise ValueError("manifest shape")
        budget = manifest["execution_budget"]
        if (not isinstance(budget, dict) or set(budget) != {"timeout_seconds", "max_turns", "repeat"}
                or type(budget["timeout_seconds"]) is not int or not 0 < budget["timeout_seconds"] <= 3600
                or budget["max_turns"] is not None or type(budget["repeat"]) is not int or budget["repeat"] != 1):
            raise ValueError("unsupported fake budget")
        for key in ("scenario_digest", "scenario_source_sha256"):
            if not isinstance(manifest[key], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[key]):
                raise ValueError("scenario digest")
        fixture = manifest["fixture"]
        if (not isinstance(fixture, dict) or set(fixture) != {"commit", "sha256", "digest_recipe"}
                or fixture["digest_recipe"] != "sorted-file-mode-content-manifest-v1"
                or not isinstance(fixture["commit"], str)
                or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", fixture["commit"])
                or not isinstance(fixture["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", fixture["sha256"])):
            raise ValueError("fixture identity")
        rows = manifest["workspaces"]
        if not isinstance(rows, list) or len(rows) != 2:
            raise ValueError("workspace rows")
        workspaces = []
        for variant, row in zip(("vanilla", "aek"), rows):
            if (not isinstance(row, dict) or set(row) != {
                    "variant", "path", "home", "cache", "session", "baseline_commit"}
                    or row["variant"] != variant or row["path"] != f"{variant}/workspace"
                    or any(row[name] != f"{variant}/{name}" for name in ("home", "cache", "session"))
                    or not isinstance(row["baseline_commit"], str)
                    or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", row["baseline_commit"])):
                raise ValueError("workspace shape")
            base = root / variant
            directories = tuple(base / name for name in ("workspace", "home", "cache", "session"))
            if any(directory.is_symlink() or not directory.is_dir() for directory in (base, *directories)):
                raise ValueError("workspace directories")
            workspaces.append(PreparedWorkspace(variant, *directories, row["baseline_commit"]))
        if workspaces[0].baseline_commit != workspaces[1].baseline_commit:
            raise ValueError("workspace baseline")
        return PreparedPair(root, tuple(workspaces), raw, expected_manifest_sha256)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise PreparationError("PREPARED_MANIFEST_INVALID") from exc
