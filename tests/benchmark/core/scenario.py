"""Strict, immutable scenario definitions; parsing never starts an Agent."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_SCENARIO_BYTES = 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PLACEHOLDERS = frozenset({"{python}", "{check}", "{workspace}", "{scenario_root}"})


class ScenarioError(ValueError):
    """The trusted scenario is invalid; messages omit input values."""


@dataclass(frozen=True)
class CheckSpec:
    id: str
    entrypoint: str
    sha256: str
    argv: tuple[str, ...]
    timeout_seconds: int
    kind: str


@dataclass(frozen=True)
class ScenarioSpec:
    schema_version: int
    id: str
    name: str
    category: str
    prompt: str
    fixture_path: str
    fixture_commit: str
    fixture_sha256: str
    agents: tuple[str, ...]
    variants: tuple[str, ...]
    timeout_seconds: int
    repeat: int
    max_turns: int | None
    checks: tuple[CheckSpec, ...]
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    digest: str


def _object(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if (not isinstance(value, dict) or not required <= value.keys()
            or not value.keys() <= required | (optional or set())):
        raise ScenarioError("unexpected or missing scenario fields")
    return value


def _text(value: object, *, identifier: bool = False) -> str:
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value) > 100_000
            or identifier and not _ID.fullmatch(value)):
        raise ScenarioError("invalid scenario text or identity")
    return value


def _path(value: object, *, pattern: bool = False) -> str:
    value = _text(value)
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or not pattern and any(char in value for char in "*?[]")):
        raise ScenarioError("scenario path must be canonical and repository-relative")
    return value


def _strings(value: object, *, paths: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ScenarioError("scenario list is invalid")
    items = tuple(_path(item, pattern=True) if paths else _text(item, identifier=True)
                  for item in value)
    if len(set(items)) != len(items):
        raise ScenarioError("duplicate scenario list entries")
    return items


def _positive(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**31 - 1:
        raise ScenarioError("scenario budget must be a bounded positive integer")
    return value


def _sha(value: object, *, commit: bool = False) -> str:
    if not isinstance(value, str) or not (_COMMIT if commit else _DIGEST).fullmatch(value):
        raise ScenarioError("scenario requires an exact commit or SHA-256")
    return value


def parse_scenario(value: object) -> ScenarioSpec:
    """Validate a trusted definition, not its filesystem or isolation claims."""
    root = _object(value, {"schema_version", "id", "name", "category", "task",
                           "fixture", "execution", "evaluation"})
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ScenarioError("unsupported scenario schema")
    task = _object(root["task"], {"prompt"})
    fixture = _object(root["fixture"], {"path", "commit", "sha256"})
    fixture_path = _path(fixture["path"])
    execution = _object(root["execution"],
                        {"agents", "variants", "timeout_seconds", "repeat"}, {"max_turns"})
    agents = _strings(execution["agents"])
    variants = _strings(execution["variants"])
    if not agents or set(variants) != {"vanilla", "aek"}:
        raise ScenarioError("scenario requires agents and both paired variants")
    evaluation = _object(root["evaluation"],
                         {"checks", "allowed_paths", "forbidden_paths"}, {"required_artifacts"})
    if not isinstance(evaluation["checks"], list) or not evaluation["checks"]:
        raise ScenarioError("scenario requires independent checks")
    checks = []
    for raw in evaluation["checks"]:
        check = _object(raw, {"id", "entrypoint", "sha256", "argv", "timeout_seconds", "kind"})
        entrypoint = _path(check["entrypoint"])
        if (not entrypoint.endswith(".py") or entrypoint == fixture_path
                or entrypoint.startswith(fixture_path + "/")):
            raise ScenarioError("independent Python check must be outside the fixture")
        argv = check["argv"]
        if (not isinstance(argv, list) or len(argv) < 2
                or argv[:2] != ["{python}", "{check}"]
                or any(not isinstance(arg, str) or "\x00" in arg
                       or any(char in arg for char in "{}") and arg not in _PLACEHOLDERS
                       for arg in argv)):
            raise ScenarioError("check must use a pinned Python entrypoint and argument array")
        if (not isinstance(check["kind"], str)
                or check["kind"] not in {"outcome", "memory", "regression"}):
            raise ScenarioError("unsupported independent check kind")
        checks.append(CheckSpec(_text(check["id"], identifier=True), entrypoint,
                                _sha(check["sha256"]), tuple(argv),
                                _positive(check["timeout_seconds"]), check["kind"]))
    if len({check.id for check in checks}) != len(checks) or not any(
            check.kind == "outcome" for check in checks):
        raise ScenarioError("independent checks need unique IDs and an outcome check")
    max_turns = execution.get("max_turns")
    if "max_turns" in execution:
        max_turns = _positive(max_turns)
    # Raw key order is irrelevant; array order remains meaningful (run/check order).
    fields = (
        1, _text(root["id"], identifier=True), _text(root["name"]),
        _text(root["category"], identifier=True), _text(task["prompt"]),
        fixture_path, _sha(fixture["commit"], commit=True), _sha(fixture["sha256"]),
        agents, variants, _positive(execution["timeout_seconds"]), _positive(execution["repeat"]),
        max_turns, tuple(checks), _strings(evaluation["allowed_paths"], paths=True),
        _strings(evaluation["forbidden_paths"], paths=True),
        _strings(evaluation.get("required_artifacts", []), paths=True))
    try:
        canonical = json.dumps(root, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise ScenarioError("scenario cannot be represented as canonical JSON") from exc
    if len(canonical) > MAX_SCENARIO_BYTES:
        raise ScenarioError("scenario input exceeds the JSON budget")
    return ScenarioSpec(*fields, hashlib.sha256(canonical).hexdigest())


def scenario_from_json(raw: bytes) -> ScenarioSpec:
    """Decode bounded UTF-8 JSON without duplicate keys or non-finite values."""
    if not isinstance(raw, bytes) or len(raw) > MAX_SCENARIO_BYTES:
        raise ScenarioError("scenario input exceeds the JSON budget")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ScenarioError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ScenarioError("non-finite JSON is not supported")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject_constant)
        return parse_scenario(value)
    except ScenarioError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ScenarioError("scenario JSON cannot be decoded") from exc
