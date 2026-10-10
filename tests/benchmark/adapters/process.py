"""Bounded subprocess execution for offline checks; not a sandbox backend."""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    reason_code: str
    stdout: bytes
    stderr: bytes
    stdout_sha256: str
    stderr_sha256: str
    duration_seconds: float


def _terminate(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_process(argv: tuple[str, ...], cwd: Path, *, timeout_seconds: int,
                max_output_bytes: int = 128 * 1024,
                environment: dict[str, str] | None = None) -> ProcessResult:
    started = time.monotonic()
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL"}}
    if environment is not None:
        if (not isinstance(environment, dict)
                or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                       or not isinstance(value, str) or "\0" in value
                       for key, value in environment.items())):
            raise ValueError("INVALID_PROCESS_ENVIRONMENT")
        env = environment.copy()
    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    except OSError:
        empty = hashlib.sha256(b"").hexdigest()
        return ProcessResult(None, "PROCESS_START_FAILED", b"", b"", empty, empty,
                             time.monotonic() - started)
    outputs = [bytearray(), bytearray()]
    digests = [hashlib.sha256(), hashlib.sha256()]
    exceeded = threading.Event()
    lock = threading.Lock()
    count = 0

    def drain(stream, index):
        nonlocal count
        try:
            while True:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    break
                digests[index].update(chunk)
                with lock:
                    remaining = max(0, max_output_bytes - count)
                    outputs[index].extend(chunk[:remaining])
                    count += len(chunk)
                    if count > max_output_bytes:
                        exceeded.set()
        finally:
            stream.close()

    threads = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    reason = "PROCESS_COMPLETED"
    deadline = started + timeout_seconds
    try:
        while process.poll() is None:
            if exceeded.is_set():
                reason = "OUTPUT_LIMIT_EXCEEDED"
                break
            if time.monotonic() >= deadline:
                reason = "PROCESS_TIMEOUT"
                break
            time.sleep(0.01)
        # The leader may exit while its children still hold the pipe handles.
        if reason != "PROCESS_COMPLETED":
            _terminate(process)
        process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=0.2)
        if any(thread.is_alive() for thread in threads):
            _terminate(process)
            for thread in threads:
                thread.join(timeout=1)
            reason = "PROCESS_DESCENDANT_OPEN_PIPE"
        if any(thread.is_alive() for thread in threads):
            reason = "PROCESS_CLEANUP_INCOMPLETE"
        elif exceeded.is_set():
            reason = "OUTPUT_LIMIT_EXCEEDED"
    except KeyboardInterrupt:
        try:
            _terminate(process)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        finally:
            for thread in threads:
                thread.join(timeout=0.2)
        raise
    except (OSError, subprocess.TimeoutExpired):
        reason = "PROCESS_CLEANUP_INCOMPLETE"
        process.kill()
        process.wait(timeout=5)
    return ProcessResult(process.returncode, reason, bytes(outputs[0]), bytes(outputs[1]),
                         digests[0].hexdigest(), digests[1].hexdigest(),
                         time.monotonic() - started)
