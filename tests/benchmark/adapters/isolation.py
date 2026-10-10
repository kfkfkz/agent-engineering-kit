"""Development-side Linux namespace diagnostics, never a live authorization.

Policy reference: https://github.com/containers/bubblewrap/blob/main/README.md
The backend is an optional development dependency, not an installer requirement.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .process import ProcessResult, run_process

_BASE_POLICY = ("--unshare-all", "--unshare-user", "--disable-userns", "--assert-userns-disabled", "--die-with-parent",
                "--new-session", "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp")


@dataclass(frozen=True)
class IsolatedExecution:
    process: ProcessResult = field(repr=False)
    backend_sha256: str
    policy_sha256: str
    scope: str = "closed_network_namespace_execution"


def _backend_bytes(backend: Path) -> tuple[Path, bytes]:
    backend = Path(backend).resolve(strict=True)
    fd = os.open(backend, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
            raise ValueError("ISOLATION_BACKEND_INVALID")
        binary = stream.read(32 * 1024 * 1024 + 1)
    if len(binary) > 32 * 1024 * 1024:
        raise ValueError("ISOLATION_BACKEND_INVALID")
    return backend, binary


def run_isolated_process(argv: tuple[str, ...], owned_root: Path, workspace: Path, home: Path, cache: Path, session: Path, *,
                         backend_sha256: str, timeout_seconds: int, max_output_bytes: int = 128 * 1024,
                         backend: Path = Path("/usr/bin/bwrap")) -> IsolatedExecution:
    """Run within explicit mounts, with no host networking, secrets or fallback."""
    if sys.platform != "linux":
        raise ValueError("ISOLATION_PLATFORM_UNSUPPORTED")
    if (type(argv) is not tuple or not argv or any(type(arg) is not str or "\0" in arg for arg in argv)
            or not argv[0].startswith(("/usr/", "/bin/", "/workspace/", "/session/"))
            or type(timeout_seconds) is not int or not 0 < timeout_seconds <= 3600
            or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 16 * 1024 * 1024
            or type(backend_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", backend_sha256)):
        raise ValueError("ISOLATION_EXECUTION_INPUT_INVALID")
    root = Path(owned_root).absolute()
    paths = tuple(Path(path).absolute() for path in (workspace, home, cache, session))
    if (root != root.resolve(strict=True) or not root.is_dir()
            or any(path != path.resolve(strict=True) or not path.is_dir() or path == root
                   or not path.is_relative_to(root) for path in paths)
            or len(set(paths)) != 4
            or any(first.is_relative_to(second) for first in paths for second in paths if first != second)):
        raise ValueError("ISOLATION_DIRECTORY_SCOPE_INVALID")
    selected, binary = _backend_bytes(backend)
    if hashlib.sha256(binary).hexdigest() != backend_sha256:
        raise ValueError("ISOLATION_BACKEND_DIGEST_MISMATCH")
    mounts = tuple(value for path, target in zip(paths, ("/workspace", "/home", "/cache", "/session"))
                   for value in ("--bind", str(path), target))
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/home", "USERPROFILE": "/home", "XDG_CONFIG_HOME": "/home/config",
                   "XDG_CACHE_HOME": "/cache", "CODEX_HOME": "/session", "CLAUDE_CONFIG_DIR": "/session"}
    settings = tuple(value for key, value in sorted(environment.items()) for value in ("--setenv", key, value))
    command = (str(selected), *_BASE_POLICY, *mounts, *settings, "--chdir", "/workspace", "--", *argv)
    policy = {"base": _BASE_POLICY, "mount_roles": ("workspace", "home", "cache", "session"),
              "environment": environment, "network": "deny"}
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = run_process(command, root, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes,
                         environment={"PATH": "/usr/bin:/bin"})
    return IsolatedExecution(result, backend_sha256, digest)

_CHECKS = ("external_file_unreadable", "symlink_escape_blocked",
           "parent_environment_absent", "host_network_unreachable")
_PROGRAM = '''import json, os, socket, sys
def unreadable(path):
    try:
        with open(path, "rb") as stream:
            stream.read(1)
        return False
    except OSError:
        return True
checks = {"external_file_unreadable": unreadable(sys.argv[1]),
          "symlink_escape_blocked": unreadable("/workspace/escape"),
          "parent_environment_absent": "AEK_PROBE_PARENT_CANARY" not in os.environ}
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=1):
        checks["host_network_unreachable"] = False
except OSError:
    checks["host_network_unreachable"] = True
print(json.dumps(checks, sort_keys=True))
'''


def probe_linux_isolation(backend: Path = Path("/usr/bin/bwrap"), *, timeout_seconds: int = 5) -> dict:
    if type(timeout_seconds) is not int or not 0 < timeout_seconds <= 30:
        raise ValueError("ISOLATION_PROBE_BUDGET_INVALID")
    report = {"schema_version": 1, "backend": "bubblewrap", "status": "UNAVAILABLE",
              "reason_code": "BACKEND_UNAVAILABLE", "live_ready": False, "model_calls": 0,
              "scope": "development_offline_isolation_probe", "checks": {}}
    if sys.platform != "linux":
        report["reason_code"] = "PLATFORM_NOT_SUPPORTED"
        return report
    try:
        backend, binary = _backend_bytes(backend)
        report["backend_sha256"] = hashlib.sha256(binary).hexdigest()
        with tempfile.TemporaryDirectory(prefix="aek-isolation-probe-") as directory, socket.socket() as server:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "outside-canary"
            secret.write_text(uuid.uuid4().hex, encoding="ascii")
            (workspace / "escape").symlink_to(secret)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            policy = (*_BASE_POLICY, "--dir", "/home", "--setenv", "HOME", "/home",
                      "--setenv", "PATH", "/usr/bin:/bin")
            # Only the disposable workspace is writable; its parent/canary is not mounted.
            command = (str(backend), *policy, "--bind", str(workspace), "/workspace", "--chdir", "/workspace",
                       "--", "/usr/bin/python3", "-I", "-c", _PROGRAM, str(secret), str(port))
            report["policy_sha256"] = hashlib.sha256(json.dumps(policy).encode("utf-8")).hexdigest()
            report["probe_sha256"] = hashlib.sha256(_PROGRAM.encode("utf-8")).hexdigest()
            result = run_process(command, root, timeout_seconds=timeout_seconds, max_output_bytes=16 * 1024,
                                 environment={"PATH": "/usr/bin:/bin", "AEK_PROBE_PARENT_CANARY": "disposable-canary"})
        report["process"] = {"reason_code": result.reason_code, "exit_code": result.exit_code,
                             "stdout_sha256": result.stdout_sha256, "stderr_sha256": result.stderr_sha256}
        if result.reason_code != "PROCESS_COMPLETED" or result.exit_code != 0:
            report["reason_code"] = "NAMESPACE_PROBE_UNAVAILABLE"
            return report
        checks = json.loads(result.stdout.decode("utf-8"))
        if type(checks) is not dict or set(checks) != set(_CHECKS) or any(type(value) is not bool for value in checks.values()):
            report.update(status="FAILED", reason_code="PROBE_RESULT_INVALID")
            return report
        report["checks"] = checks
        report.update(status="PROBED" if all(checks.values()) else "FAILED",
                      reason_code="OFFLINE_BOUNDARIES_OBSERVED" if all(checks.values()) else "PROBE_BOUNDARY_FAILURE")
        report["limitations"] = ["LIVE_CLI_NOT_BOUND", "NETWORK_RELAY_NOT_IMPLEMENTED", "NO_GENERAL_SANDBOX_SECURITY_CLAIM"]
    except (OSError, ValueError, UnicodeError, RecursionError):
        report["reason_code"] = "ISOLATION_PROBE_UNAVAILABLE"
    return report
