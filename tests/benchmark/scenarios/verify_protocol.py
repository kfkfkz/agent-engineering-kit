"""Trusted public-case probe. Candidate code runs in a separate Python process.

This is a development check, not an adversarial sandbox. POSIX children inherit
the outer check's process group. Native Windows containment still needs M2/M6.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


class CandidateFailure(AssertionError):
    pass


def probe(root, value):
    env = {key: item for key, item in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}}
    child = subprocess.Popen(
        [sys.executable, "-I", str(root / "app.py"), json.dumps(value, ensure_ascii=True)],
        cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    outputs = [bytearray(), bytearray()]
    exceeded = threading.Event()
    lock = threading.Lock()
    count = 0

    def drain(stream, index):
        nonlocal count
        try:
            while True:
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    break
                with lock:
                    remaining = max(0, 65536 - count)
                    outputs[index].extend(chunk[:remaining])
                    count += len(chunk)
                    if count > 65536:
                        exceeded.set()
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((child.stdout, child.stderr))]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + 3
    try:
        while child.poll() is None:
            if exceeded.is_set() or time.monotonic() >= deadline:
                raise RuntimeError("candidate probe budget exceeded")
            time.sleep(0.01)
        for reader in readers:
            reader.join(timeout=0.2)
        if exceeded.is_set() or any(reader.is_alive() for reader in readers):
            raise RuntimeError("candidate probe incomplete")
        if child.returncode < 0:
            raise RuntimeError("candidate terminated by signal")
        if child.returncode != 0:
            raise CandidateFailure("candidate did not implement the CLI contract")
        try:
            return json.loads(bytes(outputs[0]).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise CandidateFailure("candidate did not return valid JSON") from exc
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=0.2)


def expect(root, request, expected):
    observed = probe(root, request)
    if json.dumps(observed, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True):
        raise CandidateFailure("independent expected behavior differs")


def checked_main(verify):
    try:
        verify(Path(sys.argv[1]))
    except AssertionError:
        raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        raise SystemExit(2)
