"""atomic.py — 平台原子文件操作原语。

提供 POSIX `rename(2)` 无法保证的 no-overwrite 语义：
- Linux: `renameat2(2)` + `RENAME_NOREPLACE`（glibc 2.28+ / kernel 3.15+）
- macOS: `renamex_np(3)` + `RENAME_EXCL`
- 兜底: 检查 + rename（有理论竞窗，但比裸 rename 多一层保护；平台声明
  中已标注）

这些原语是外部链接安全删除的基石——隔离移入和恢复都使用 no-overwrite
语义，绝不能覆盖竞争者创建的文件。
"""
from __future__ import annotations

import ctypes
import errno
import os


class AtomicRenameNotSupported(Exception):
    """平台不支持原子 no-replace rename（极老的 kernel/glibc）。"""


_libc: ctypes.CDLL | None = None
_checked = False
_has_renameat2 = False
_has_renamex_np = False


def _init() -> None:
    global _libc, _checked, _has_renameat2, _has_renamex_np
    if _checked:
        return
    _checked = True
    try:
        _libc = ctypes.CDLL(None, use_errno=True)
        _has_renameat2 = hasattr(_libc, "renameat2")
        _has_renamex_np = hasattr(_libc, "renamex_np")
    except (OSError, AttributeError):
        _libc = None


def rename_noreplace(src_dir_fd: int, src_name: str,
                     dst_dir_fd: int, dst_name: str) -> None:
    """原子 rename：目标已存在则失败（FileExistsError），绝不覆盖。

    优先使用平台原生的 no-replace 原语；不可用时降级为检查+rename
    （有理论竞窗，但覆盖前的 lstat 至少能挡住已存在的文件）。
    """
    _init()
    src_b = src_name.encode("utf-8")
    dst_b = dst_name.encode("utf-8")

    # Linux: renameat2 + RENAME_NOREPLACE
    if _libc and _has_renameat2:
        RENAME_NOREPLACE = 1
        result = _libc.renameat2(
            ctypes.c_int(src_dir_fd), ctypes.c_char_p(src_b),
            ctypes.c_int(dst_dir_fd), ctypes.c_char_p(dst_b),
            ctypes.c_uint(RENAME_NOREPLACE))
        if result == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, "目标已存在（no-replace）", dst_name)
        if err == errno.ENOSYS:
            pass  # kernel 太老，降级
        else:
            raise OSError(err, os.strerror(err), dst_name)

    # macOS: renamex_np + RENAME_EXCL
    if _libc and _has_renamex_np:
        RENAME_EXCL = 0x04  # #define RENAME_EXCL 0x00000004
        # renamex_np 签名与 renameat 相似但多了 flags
        try:
            result = _libc.renamex_np(
                ctypes.c_int(src_dir_fd), ctypes.c_char_p(src_b),
                ctypes.c_int(dst_dir_fd), ctypes.c_char_p(dst_b),
                ctypes.c_uint(RENAME_EXCL))
            if result == 0:
                return
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                raise FileExistsError(err, "目标已存在（no-replace）", dst_name)
            raise OSError(err, os.strerror(err), dst_name)
        except AttributeError:
            pass

    # 兜底：检查 + rename（理论竞窗——但比裸 rename 多一层检查）
    try:
        os.lstat(dst_name, dir_fd=dst_dir_fd)
        raise FileExistsError(f"目标已存在: {dst_name}")
    except FileNotFoundError:
        pass
    os.rename(src_name, dst_name,
              src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def supported() -> bool:
    """返回当前平台是否有原生 no-replace 原语（非兜底路径）。"""
    _init()
    return _has_renameat2 or _has_renamex_np
