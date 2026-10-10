"""Read one explicitly selected bounded regular CLI capture, without modifying it."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from collectors.cli_events import MAX_STREAM_BYTES

from installer import platform as fs


def read_cli_capture(path: Path) -> bytes:
    path = Path(path).absolute()
    root = path.parent.resolve()
    try:
        fd = fs.secure_open(root, path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STREAM_BYTES:
                raise ValueError("CAPTURE_FILE_INVALID")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_STREAM_BYTES + 1)
            if len(raw) > MAX_STREAM_BYTES:
                raise ValueError("CAPTURE_FILE_OVERSIZED")
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError("CAPTURE_FILE_UNREADABLE") from exc
