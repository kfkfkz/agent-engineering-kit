"""Read-only loading of an explicitly selected trusted scenario JSON file."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core.scenario import (
    MAX_SCENARIO_BYTES,
    ScenarioError,
    ScenarioSpec,
    scenario_from_json,
)

from installer import platform as fs


@dataclass(frozen=True)
class LoadedScenario:
    scenario: ScenarioSpec
    source: Path
    scenario_root: Path
    source_sha256: str


def load_scenario(path: Path) -> LoadedScenario:
    """Structure/digest validation only; fixture bytes and isolation are separate."""
    path = Path(path).absolute()
    if path.suffix != ".json":
        raise ScenarioError("scenario source must be an explicit JSON file")
    root = path.parent.resolve()
    try:
        fd = fs.secure_open(root, path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCENARIO_BYTES:
                raise ScenarioError("scenario must be a bounded regular JSON file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_SCENARIO_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ScenarioError("scenario source cannot be read safely") from exc
    return LoadedScenario(scenario_from_json(raw), root / path.name, root,
                          hashlib.sha256(raw).hexdigest())
