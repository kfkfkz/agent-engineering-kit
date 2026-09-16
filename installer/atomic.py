"""atomic.py — 原子 no-replace rename 原语（re-export 自 platform/）。

- Linux: `renameat2(2)` + `RENAME_NOREPLACE`（glibc 2.28+ / kernel 3.15+）
- macOS: `renameatx_np(3)` + `RENAME_EXCL`（fd 版本，五参数）
- 不支持时抛 AtomicRenameNotSupported——**绝不静默降级到检查+rename**
  （那只是 TOCTOU 的另一个名字，安全原语必须 fail closed）
"""
from __future__ import annotations

import ctypes
import errno
import os


class AtomicRenameNotSupported(OSError):
    """平台不支持原子 no-replace rename（极老的 kernel/glibc）。"""


_libc: ctypes.CDLL | None = None
_checked = False
_renameat2 = None      # Linux: (src_fd, src, dst_fd, dst, flags) -> int
_renameatx_np = None   # macOS: (src_fd, src, dst_fd, dst, flags) -> int


def _init() -> None:
    global _libc, _checked, _renameat2, _renameatx_np
    if _checked:
        return
    _checked = True
    try:
        _libc = ctypes.CDLL(None, use_errno=True)
        if hasattr(_libc, "renameat2"):
            _renameat2 = _libc.renameat2
        if hasattr(_libc, "renameatx_np"):
            _renameatx_np = _libc.renameatx_np
    except (OSError, AttributeError):
        _libc = None


def rename_noreplace(src_dir_fd: int, src_name: str,
                     dst_dir_fd: int, dst_name: str) -> None:
    """原子 rename：目标已存在则失败（FileExistsError），绝不覆盖。

    fail-closed：平台不支持时抛 AtomicRenameNotSupported（不降级到
    检查+rename——那是 TOCTOU 的另一个名字）。"""
    _init()
    src_b = src_name.encode("utf-8")
    dst_b = dst_name.encode("utf-8")

    if _renameat2 is not None:
        # Linux: renameat2 + RENAME_NOREPLACE
        RENAME_NOREPLACE = 1
        result = _renameat2(
            ctypes.c_int(src_dir_fd), ctypes.c_char_p(src_b),
            ctypes.c_int(dst_dir_fd), ctypes.c_char_p(dst_b),
            ctypes.c_uint(RENAME_NOREPLACE))
        if result == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, "目标已存在（no-replace）", dst_name)
        if err == errno.ENOSYS:
            raise AtomicRenameNotSupported(
                "kernel < 3.15 不支持 renameat2(RENAME_NOREPLACE)")
        raise OSError(err, os.strerror(err), dst_name)

    if _renameatx_np is not None:
        # macOS: renameatx_np + RENAME_EXCL（五参数 fd 版本）
        RENAME_EXCL = 0x04
        result = _renameatx_np(
            ctypes.c_int(src_dir_fd), ctypes.c_char_p(src_b),
            ctypes.c_int(dst_dir_fd), ctypes.c_char_p(dst_b),
            ctypes.c_uint(RENAME_EXCL))
        if result == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, "目标已存在（no-replace）", dst_name)
        raise OSError(err, os.strerror(err), dst_name)

    raise AtomicRenameNotSupported(
        "平台不支持原子 no-replace rename（需 Linux renameat2 或 macOS renameatx_np）")


def supported() -> bool:
    """返回当前平台是否有原生 no-replace 原语。"""
    _init()
    return _renameat2 is not None or _renameatx_np is not None
