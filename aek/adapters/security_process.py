"""Bounded processes, POSIX groups and Windows Jobs; not an OS sandbox."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

ENVIRONMENT_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL", "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"})


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    reason_code: str
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    stdout_sha256: str
    stderr_sha256: str
    duration_seconds: float
    cleanup_complete: bool


class _WindowsJob:
    """Assign a suspended leader before any tool code can spawn descendants."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel = self.kernel
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("WINDOWS_JOB_UNAVAILABLE")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                or not kernel.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise OSError("WINDOWS_JOB_UNAVAILABLE")
        # Popen closes the primary thread handle. Obtain that suspended thread
        # through documented Toolhelp thread enumeration and ResumeThread.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                        ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                        ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        kernel.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            self.close()
            raise OSError("WINDOWS_JOB_UNAVAILABLE")
        resumed = False
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel.OpenThread(2, False, entry.th32ThreadID)
                    if thread:
                        try:
                            resumed = kernel.ResumeThread(thread) != 0xFFFFFFFF
                        finally:
                            kernel.CloseHandle(thread)
                    break
                present = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        if not resumed:
            self.close()
            raise OSError("WINDOWS_JOB_UNAVAILABLE")

    def terminate(self):
        if self.handle:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise OSError("WINDOWS_JOB_TERMINATION_FAILED")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessPort:
    # These describe implemented controls, not live tool/platform certification.
    supported_boundaries = frozenset({"process_group"}) if os.name == "posix" else frozenset({"process_job"}) if os.name == "nt" else frozenset()

    def run(self, argv, cwd, **kwargs):
        return run_process(argv, cwd, **kwargs)

    def diagnostics(self):
        return {"platform": os.name, "process_control": "POSIX_PROCESS_GROUP" if os.name == "posix" else "WINDOWS_JOB" if os.name == "nt" else "UNAVAILABLE",
                "network_boundary": "UNAVAILABLE", "filesystem_boundary": "UNAVAILABLE",
                "platform_validation": "REQUIRES_PLATFORM_TEST_RESULT"}


def run_process(argv: tuple[str, ...], cwd: Path, *, timeout_seconds: float,
                max_output_bytes: int = 2000000, environment: dict[str, str] | None = None,
                output_file: Path | None = None, pass_fds: tuple[int, ...] = ()) -> ProcessResult:
    """Run one bounded process, optionally retaining controller-owned POSIX FDs.

    The caller owns selected descriptors and must keep them open, unchanged,
    through this call. This is not an execution grant or a sandbox capability;
    only a fixed native provider may supply them, never request JSON. Other
    descriptors remain closed. Empty selection preserves the Windows path.
    """
    if (type(argv) is not tuple or not argv or any(type(arg) is not str or "\0" in arg for arg in argv)
            or type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 300
            or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 2000000):
        raise ValueError("INVALID_PROCESS_REQUEST")
    if (type(pass_fds) is not tuple or len(pass_fds) > 32
            or any(type(fd) is not int or fd < 3 for fd in pass_fds)
            or len(set(pass_fds)) != len(pass_fds)):
        raise ValueError("INVALID_PROCESS_DESCRIPTORS")
    if pass_fds and os.name != "posix":
        raise ValueError("PROCESS_DESCRIPTOR_BOUNDARY_UNAVAILABLE")
    try:
        for fd in pass_fds:
            os.fstat(fd)
    except (OSError, OverflowError):
        raise ValueError("INVALID_PROCESS_DESCRIPTORS") from None
    env = {key: value for key, value in os.environ.items() if key.upper() in ENVIRONMENT_KEYS - {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"}}
    if environment is not None:
        if type(environment) is not dict or any(key not in ENVIRONMENT_KEYS or type(value) is not str or "\0" in value for key, value in environment.items()):
            raise ValueError("INVALID_PROCESS_ENVIRONMENT")
        env = environment.copy()
    started = time.monotonic()
    empty = hashlib.sha256(b"").hexdigest()
    job = None
    process = None
    try:
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 4} if os.name == "nt" else {"start_new_session": True} if os.name == "posix" else None
        if options is None:
            return ProcessResult(None, "PROCESS_BOUNDARY_UNAVAILABLE", b"", b"", empty, empty, 0, True)
        if pass_fds:
            options["pass_fds"] = pass_fds
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   close_fds=True, **options)
        if os.name == "nt":
            job = _WindowsJob(process)
    except OSError:
        if process is not None:
            process.kill()
            process.wait(timeout=5)
        return ProcessResult(None, "PROCESS_START_FAILED", b"", b"", empty, empty, time.monotonic() - started, True)
    buffers = [bytearray(), bytearray()]
    hashes = [hashlib.sha256(), hashlib.sha256()]
    lock = threading.Lock()
    overflow = threading.Event()
    total = 0
    def drain(stream, index):
        nonlocal total
        try:
            while chunk := os.read(stream.fileno(), 65536):
                hashes[index].update(chunk)
                with lock:
                    buffers[index].extend(chunk[:max(0, max_output_bytes - total)])
                    total += len(chunk)
                    if total > max_output_bytes:
                        overflow.set()
        except OSError:
            overflow.set()
        finally:
            stream.close()
    readers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for thread in readers:
        thread.start()
    def terminate():
        if job:
            job.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    reason = "PROCESS_COMPLETED"
    cleanup = True
    try:
        while process.poll() is None:
            report_size = output_file.lstat().st_size if output_file is not None and output_file.exists() else 0
            if overflow.is_set() or total + report_size > max_output_bytes:
                reason = "OUTPUT_LIMIT_EXCEEDED"
                break
            if time.monotonic() - started >= timeout_seconds:
                reason = "PROCESS_TIMEOUT"
                break
            time.sleep(0.01)
        # Stop descendants even after a normally exited leader. A cwd does not
        # confine files/network, and POSIX setsid escapes are not a sandbox.
        if reason == "PROCESS_COMPLETED":
            for thread in readers:
                thread.join(timeout=0.05)
            if any(thread.is_alive() for thread in readers):
                reason = "PROCESS_DESCENDANT_OPEN_PIPE"
        terminate()
        process.wait(timeout=5)
        for thread in readers:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in readers):
            reason, cleanup = "PROCESS_CLEANUP_INCOMPLETE", False
        elif overflow.is_set():
            reason = "OUTPUT_LIMIT_EXCEEDED"
        if output_file is not None and output_file.exists():
            if output_file.is_symlink() or total + output_file.lstat().st_size > max_output_bytes:
                reason = "OUTPUT_LIMIT_EXCEEDED"
    except (OSError, subprocess.TimeoutExpired):
        reason, cleanup = "PROCESS_CLEANUP_INCOMPLETE", False
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    except BaseException:
        terminate()
        process.wait(timeout=5)
        for thread in readers:
            thread.join(timeout=1)
        raise
    finally:
        if job:
            job.close()
    return ProcessResult(process.returncode, reason, bytes(buffers[0]), bytes(buffers[1]),
                         hashes[0].hexdigest(), hashes[1].hexdigest(), time.monotonic() - started, cleanup)
