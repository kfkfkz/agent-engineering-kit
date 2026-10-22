"""Strict filesystem boundary for user-owned UI material.

Directory handles are opaque to callers. POSIX uses *at operations; Windows
pins the entire validated ancestor chain without write/delete sharing, opens
leaves with OPEN_REPARSE_POINT, and mutates opened leaves by handle. No check
then follow fallback is permitted. File handles remain ordinary binary CRT fds.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import math
import os
import re
import stat
import sys
import time
from pathlib import Path


def _component(name: str) -> str:
    if (not isinstance(name, str) or name in {"", ".", ".."}
            or any(c in name for c in '/\\\0:<>"|?*')
            or any(ord(c) < 32 for c in name) or name[-1:] in {".", " "}
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name)):
        raise ValueError("unsafe filesystem component")
    return name


def _posix_supported() -> None:
    if (not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
            or any(f not in os.supports_dir_fd for f in (os.open, os.mkdir, os.stat, os.unlink, os.rename))):
        raise OSError("secure handle-relative filesystem operations unavailable")


class _Rename(ctypes.Structure):
    # Windows ABI widths are explicit so the syscall payload can also be
    # checked on POSIX, where ctypes.c_wchar and c_ulong have different widths.
    _fields_ = [("replace", ctypes.c_ubyte), ("root", ctypes.c_void_p),
                ("length", ctypes.c_uint32), ("name", ctypes.c_uint16 * 1)]


class _StatusValue(ctypes.Union):
    _fields_ = [("status", ctypes.c_int32), ("pointer", ctypes.c_void_p)]


class _IOStatusBlock(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("value", _StatusValue), ("information", ctypes.c_size_t)]


def _windows_rename_buffer(destination: str, overwrite: bool):
    destination = _component(destination)
    encoded = destination.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_Rename) + len(encoded))
    info = _Rename.from_buffer(buffer)
    info.replace = overwrite
    info.root = None
    info.length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + _Rename.name.offset, encoded, len(encoded))
    return buffer


def _windows_set_rename(raw, destination: str, overwrite: bool):
    buffer = _windows_rename_buffer(destination, overwrite)
    completion = _IOStatusBlock()
    status = _native.NtSetInformationFile(raw, ctypes.byref(completion), buffer, len(buffer), 10)
    if status == 0x103:  # STATUS_PENDING: completion status is not final yet.
        if _kernel.WaitForSingleObject(raw, 0xFFFFFFFF) != 0:
            _win_error(destination)
        status = completion.status
    if ctypes.c_int32(status).value < 0:
        _win_error(destination, error=_native.RtlNtStatusToDosError(status))


if os.name == "nt":
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _native = ctypes.WinDLL("ntdll", use_last_error=True)
    _HANDLE = wintypes.HANDLE
    _INVALID = ctypes.c_void_p(-1).value
    _READ = 0x80000000
    _WRITE = 0x40000000
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _ATTR = 0x80
    _REPARSE = 0x400
    _DIRECTORY = 0x10
    _BACKUP = 0x02000000
    _OPEN_REPARSE = 0x00200000

    class _Info(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_hi", wintypes.DWORD),
                    ("size_lo", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_hi", wintypes.DWORD), ("index_lo", wintypes.DWORD)]

    class _Disposition(ctypes.Structure):
        _fields_ = [("delete", wintypes.BOOLEAN)]

    # Explicit prototypes are necessary: untyped ctypes silently truncates
    # 64-bit HANDLE arguments/results on 64-bit Windows.
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, _HANDLE]
    _kernel.CreateFileW.restype = _HANDLE
    _kernel.CloseHandle.argtypes = [_HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.GetFileInformationByHandle.argtypes = [_HANDLE, ctypes.POINTER(_Info)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFinalPathNameByHandleW.argtypes = [_HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel.SetFileInformationByHandle.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel.CreateMutexW.restype = _HANDLE
    _kernel.WaitForSingleObject.argtypes = [_HANDLE, wintypes.DWORD]
    _kernel.WaitForSingleObject.restype = wintypes.DWORD
    _kernel.ReleaseMutex.argtypes = [_HANDLE]
    _kernel.ReleaseMutex.restype = wintypes.BOOL
    _native.NtSetInformationFile.argtypes = [_HANDLE, ctypes.POINTER(_IOStatusBlock),
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    _native.NtSetInformationFile.restype = ctypes.c_int32
    _native.RtlNtStatusToDosError.argtypes = [ctypes.c_int32]
    _native.RtlNtStatusToDosError.restype = ctypes.c_uint32

    def _win_error(path=None, *, error=None):
        if error is None:
            error = ctypes.get_last_error()
        # WinError maps ERROR_FILE_EXISTS/ALREADY_EXISTS to FileExistsError.
        exception = ctypes.WinError(error)
        if path is not None:
            exception.filename = str(path)
        raise exception

    def _info(handle):
        result = _Info()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(result)):
            _win_error()
        return result

    def _win_open(path, access, disposition=3, *, directory=None, allow_reparse=False):
        handle = _kernel.CreateFileW(str(path), access, 1, None, disposition,
                                    _BACKUP | _OPEN_REPARSE, None)
        if handle == _INVALID:
            _win_error(path)
        try:
            info = _info(handle)
            if not allow_reparse and info.attributes & _REPARSE:
                raise OSError(errno.ELOOP, "reparse point refused", str(path))
            if directory is True and not info.attributes & _DIRECTORY:
                raise NotADirectoryError(errno.ENOTDIR, "expected directory", str(path))
            if directory is False and info.attributes & _DIRECTORY:
                raise IsADirectoryError(errno.EISDIR, "expected regular file", str(path))
            return handle
        except BaseException:
            _kernel.CloseHandle(handle)
            raise

    class _Directory:
        def __init__(self, path, handle, parent=None):
            self.path, self.handle, self.parent = path, handle, parent
            self.references = 1
            if parent is not None:
                parent.references += 1

        def require_open(self):
            if not self.references:
                raise OSError(errno.EBADF, "closed directory handle")
            return self

    def _win_anchor(anchor):
        original = _win_open(anchor, _READ, directory=True)
        current = None
        try:
            # Use the opened volume's GUID, rather than a mutable DOS drive
            # letter. UNC volumes have no GUID and retain their UNC anchor.
            flags = 0 if str(anchor).startswith("\\\\") else 1
            capacity = _kernel.GetFinalPathNameByHandleW(original, None, 0, flags)
            if not capacity:
                _win_error(anchor)
            buffer = ctypes.create_unicode_buffer(capacity + 1)
            result = _kernel.GetFinalPathNameByHandleW(original, buffer, len(buffer), flags)
            if not result or result >= len(buffer):
                _win_error(anchor)
            physical = Path(buffer.value)
            root = Path(physical.anchor)
            current = _Directory(root, _win_open(root, _READ, directory=True))
            # A SUBST drive may map to a nested directory. Pin its physical
            # ancestors too, then compare identities to close the mapping race.
            for part in physical.parts[1:]:
                child = open_child_dir(current, part)
                close_dir(current)
                current = child
            first, second = _info(original), _info(current.handle)
            if ((first.volume, first.index_hi, first.index_lo)
                    != (second.volume, second.index_hi, second.index_lo)):
                raise OSError("volume mapping changed while opening directory")
            return current
        except BaseException:
            if current is not None:
                close_dir(current)
            raise
        finally:
            _kernel.CloseHandle(original)

    def _win_stat(info):
        def seconds(value):
            return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10**7 - 11644473600
        mode = stat.S_IFLNK if info.attributes & _REPARSE else (
            stat.S_IFDIR if info.attributes & _DIRECTORY else stat.S_IFREG)
        return os.stat_result((mode | 0o600, (info.index_hi << 32) | info.index_lo,
                               info.volume, info.links, 0, 0, (info.size_hi << 32) | info.size_lo,
                               seconds(info.accessed), seconds(info.written), seconds(info.created)))


def open_absolute_dir(path: Path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory must be absolute with no parent traversal")
    if os.name == "nt":
        # Drive and UNC share roots are accepted; device namespaces bypass
        # ordinary path semantics and are deliberately refused.
        if str(path).startswith(("\\\\?\\", "\\\\.\\")):
            raise ValueError("device namespace paths refused")
        anchor = Path(path.anchor)
        current = _win_anchor(anchor)
    else:
        _posix_supported()
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = open_child_dir(current, part)
            close_dir(current)
            current = child
        return current
    except BaseException:
        close_dir(current)
        raise


def dup_dir(handle):
    if os.name == "nt":
        handle.require_open().references += 1
        return handle
    return os.dup(handle)


def close_dir(handle) -> None:
    if os.name == "nt":
        handle.require_open().references -= 1
        if not handle.references:
            _kernel.CloseHandle(handle.handle)
            if handle.parent is not None:
                close_dir(handle.parent)
    else:
        os.close(handle)


def open_child_dir(handle, name: str, *, create: bool = False):
    name = _component(name)
    if create:
        try:
            mkdir(handle, name)
        except FileExistsError:
            pass
    if os.name == "nt":
        handle.require_open()
        path = handle.path / name
        return _Directory(path, _win_open(path, _READ, directory=True), handle)
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)


def open_file(handle, name: str, flags: int, mode: int = 0o600) -> int:
    name = _component(name)
    if os.name != "nt":
        # O_NONBLOCK prevents FIFO/device placeholders from blocking before
        # the regular-file check. It does not affect regular-file IO.
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=handle)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "expected regular file", name)
            return fd
        except BaseException:
            os.close(fd)
            raise
    handle.require_open()
    access = _READ if flags & os.O_RDWR or not flags & os.O_WRONLY else 0
    if flags & (os.O_WRONLY | os.O_RDWR):
        access |= _WRITE
    disposition = 1 if flags & os.O_CREAT and flags & os.O_EXCL else (4 if flags & os.O_CREAT else 3)
    raw = _win_open(handle.path / name, access, disposition, directory=False)
    try:
        # Never truncate until the opened handle has passed the reparse check.
        fd = msvcrt.open_osfhandle(raw, (flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND)) | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(raw)
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "expected regular file", name)
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        return fd
    except BaseException:
        os.close(fd)
        raise


def mkdir(handle, name: str, mode: int = 0o700) -> None:
    name = _component(name)
    if os.name == "nt":
        os.mkdir(handle.require_open().path / name, mode)
    else:
        os.mkdir(name, mode, dir_fd=handle)


def listdir(handle) -> list[str]:
    return os.listdir(handle.require_open().path if os.name == "nt" else handle)


def stat_at(handle, name: str):
    name = _component(name)
    if os.name != "nt":
        return os.stat(name, dir_fd=handle, follow_symlinks=False)
    raw = _win_open(handle.require_open().path / name, _ATTR, allow_reparse=True)
    try:
        return _win_stat(_info(raw))
    finally:
        _kernel.CloseHandle(raw)


def _delete(handle, name, directory):
    name = _component(name)
    if os.name != "nt":
        (os.rmdir if directory else os.unlink)(name, dir_fd=handle)
        return
    raw = _win_open(handle.require_open().path / name, _DELETE | _ATTR, allow_reparse=True)
    try:
        attributes = _info(raw).attributes
        # unlink also removes directory symlinks/junctions themselves, as on
        # POSIX. A real directory must still go through rmdir.
        if not attributes & _REPARSE and bool(attributes & _DIRECTORY) != directory:
            error = errno.ENOTDIR if directory else errno.EISDIR
            raise OSError(error, "incorrect entry type", name)
        info = _Disposition(True)
        if not _kernel.SetFileInformationByHandle(raw, 4, ctypes.byref(info), ctypes.sizeof(info)):
            _win_error(name)
    finally:
        _kernel.CloseHandle(raw)


def unlink(handle, name: str) -> None:
    _delete(handle, name, False)


def rmdir(handle, name: str) -> None:
    _delete(handle, name, True)


def _rename(src_handle, src, dst_handle, dst, overwrite):
    src, dst = _component(src), _component(dst)
    if os.name == "nt":
        source = _info(src_handle.require_open().handle)
        target = _info(dst_handle.require_open().handle)
        if ((source.volume, source.index_hi, source.index_lo)
                != (target.volume, target.index_hi, target.index_lo)):
            raise OSError(errno.ENOTSUP, "secure cross-directory rename unsupported on Windows")
        raw = _win_open(src_handle.path / src, _DELETE | _ATTR | _SYNCHRONIZE)
        try:
            # Native NULL-root + basename renames within Open.Link.ParentFile.
            # Absolute names and non-NULL roots reopen the destination parent
            # writable, conflicting with our deliberate no-write ancestor pins.
            # Win32's wrapper converts a basename to an absolute path, so use
            # the native syscall directly. UI publications all share a parent.
            _windows_set_rename(raw, dst, overwrite)
        finally:
            _kernel.CloseHandle(raw)
        return
    if overwrite:
        os.replace(src, dst, src_dir_fd=src_handle, dst_dir_fd=dst_handle)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function, flag = libc.renameat2, 1
    elif sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function, flag = libc.renameatx_np, 4
    else:
        raise OSError("atomic no-replace publication unsupported")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(src_handle, os.fsencode(src), dst_handle, os.fsencode(dst), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), dst)


def replace(src_handle, src: str, dst_handle, dst: str) -> None:
    _rename(src_handle, src, dst_handle, dst, True)


def rename_noreplace(src_handle, src: str, dst_handle, dst: str) -> None:
    _rename(src_handle, src, dst_handle, dst, False)


def fsync_dir(handle) -> None:
    if os.name == "nt":
        # Windows has no supported directory fsync equivalent. File writes
        # are fsynced by callers; handle rename is atomic, not power-loss proof.
        handle.require_open()
    else:
        os.fsync(handle)


@contextlib.contextmanager
def exclusive_lock(handle, *, timeout: float | None = None):
    """Lock material identity; legacy callers block, explicit deadlines do not."""
    deadline = None
    if timeout is not None:
        if type(timeout) not in (int, float):
            raise ValueError("lock timeout must be a finite non-negative number")
        try:
            seconds = float(timeout)
        except OverflowError as exc:
            raise ValueError("lock timeout must be finite") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("lock timeout must be a finite non-negative number")
        deadline = time.monotonic() + seconds
    if os.name != "nt":
        import fcntl
        if deadline is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(errno.ETIMEDOUT, "UI material lock timed out") from None
                    time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
        return
    info = _info(handle.require_open().handle)
    identity = f"{info.volume}:{info.index_hi}:{info.index_lo}"
    mutex = _kernel.CreateMutexW(None, False, "Global\\AEK.UI." + hashlib.sha256(identity.encode()).hexdigest())
    if not mutex:
        _win_error()
    acquired = False
    try:
        while True:
            milliseconds = (0xFFFFFFFF if deadline is None else
                math.ceil(max(0.0, min(deadline - time.monotonic(), 0xFFFFFFFE / 1000)) * 1000))
            result = _kernel.WaitForSingleObject(mutex, milliseconds)
            if result != 0x102:  # WAIT_TIMEOUT
                break
            if deadline is None or time.monotonic() >= deadline:
                raise TimeoutError(errno.ETIMEDOUT, "UI material lock timed out")
        if result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            _win_error()
        acquired = True
        yield
    finally:
        try:
            if acquired and not _kernel.ReleaseMutex(mutex):
                _win_error()
        finally:
            _kernel.CloseHandle(mutex)


def read_fd(fd: int, limit: int) -> bytes:
    if limit < 0:
        raise ValueError("negative file size limit")
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("expected regular file within size limit")
    chunks, total = [], 0
    while True:
        data = os.read(fd, min(1024 * 1024, limit + 1 - total))
        if not data:
            break
        total += len(data)
        if total > limit:
            raise ValueError("file exceeds size limit")
        chunks.append(data)
    after = os.fstat(fd)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or total != before.st_size):
        raise ValueError("file changed while reading")
    return b"".join(chunks)


def read_at(handle, name: str, limit: int) -> bytes:
    fd = open_file(handle, name, os.O_RDONLY)
    try:
        return read_fd(fd, limit)
    finally:
        os.close(fd)
