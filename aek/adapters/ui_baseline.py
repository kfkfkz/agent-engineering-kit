"""Read bounded source bytes through the existing cross-platform handle layer."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from aek.adapters import ui_filesystem as fs
from aek.core.artifact.ui_baseline import project_baseline, validate_baseline

MAX_SOURCE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024


def read_baseline(repo_root: Path, value: object) -> dict[tuple, dict]:
    value = validate_baseline(value)  # all paths/fields before any source IO
    root = fs.open_absolute_dir(repo_root)
    try:
        hashes = {}
        # A second pass detects a previously-read source changed while another
        # was being read. This is an optimistic snapshot, not a filesystem lock.
        for pass_number in range(2):
            total = 0
            for source in value['sources']:
                current = fs.dup_dir(root)
                try:
                    path = PurePosixPath(source['path'])
                    for part in path.parts[:-1]:
                        child = fs.open_child_dir(current, part)
                        fs.close_dir(current)
                        current = child
                    raw = fs.read_at(current, path.name, min(MAX_SOURCE_BYTES, MAX_TOTAL_BYTES - total))
                    total += len(raw)
                    digest = hashlib.sha256(raw).hexdigest()
                    if pass_number == 0:
                        hashes[source['id']] = digest
                    elif hashes[source['id']] != digest:
                        hashes[source['id']] = None
                except (OSError, ValueError):
                    hashes[source['id']] = None
                finally:
                    fs.close_dir(current)
        return project_baseline(value, hashes)
    finally:
        fs.close_dir(root)
