"""platform/ — 平台能力抽象层。

事务层只知道"原子写入""按 hash 删除""取得排他锁"等语义操作，
不知道 fcntl、fd、O_NOFOLLOW 或 Win32 HANDLE。

后端选择：
- Linux/macOS → posix_fs（dir_fd + O_NOFOLLOW，现有高安全实现）
- Windows    → windows_fs（path-based + islink 预检，compatible 档）

架构约束（来自外部架构审查）：
1. POSIX 后端的安全等级不因 Windows 兼容而下降
2. Windows 分 compatible/strict 两档——compatible 明确声明 TOCTOU 保护弱于 POSIX
3. 锁是 context manager 对象，不泄漏 fd
4. Windows 不默认依赖 symlink——--link-strategy copy 替代
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# ── 平台条件标志：Windows 无 O_NOFOLLOW/O_DIRECTORY，取 0（路径安全由
#    windows_fs 的 islink 预检承担——compatible 档语义）──
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_BINARY = getattr(os, "O_BINARY", 0)

# ── 延迟导入：避免在非目标平台上加载不必要的模块 ──

if IS_WINDOWS:
    from . import windows_fs as _fs
else:
    from . import posix_fs as _fs


# ══════════════════════════ 语义化文件操作接口 ══════════════════════════

def read_bytes(root: Path, rel: str) -> bytes | None:
    """读取文件内容；不存在 → None。调用方保证 rel 已通过路径校验。"""
    return _fs.read_bytes(root, rel)


def create_exclusive(root: Path, rel: str, data: bytes, *, mode: int = 0o600) -> int:
    """O_CREAT|O_EXCL 创建新文件并写入。返回 fd（调用方负责 close）。"""
    return _fs.create_exclusive(root, rel, data, mode=mode)


def write_exclusive(root: Path, rel: str, data: bytes, *, mode: int = 0o600,
                    truncate: bool = False) -> int:
    """打开文件写入（O_CREAT，可选 O_TRUNC）。返回 fd。"""
    return _fs.write_exclusive(root, rel, data, mode=mode, truncate=truncate)


def atomic_replace(root: Path, src_rel: str, dst_rel: str) -> None:
    """原子替换（同目录内 rename，保证原子性）。"""
    _fs.atomic_replace(root, src_rel, dst_rel)


def unlink(root: Path, rel: str) -> None:
    """删除文件（含最终组件 islink 拒绝）。"""
    _fs.unlink(root, rel)


def rmdir(root: Path, rel: str) -> None:
    """删除空目录。"""
    _fs.rmdir(root, rel)


def mkdirs_checked(root: Path, rel: str) -> None:
    """逐级创建目录（幂等；符号链接/junction 组件拒绝）。"""
    _fs.mkdirs_checked(root, rel)


def check_path_safe(root: Path, rel: str) -> bool:
    """检查路径组件无符号链接/junction（preflight 只读）。"""
    return _fs.check_path_safe(root, rel)


def fsync_directory(root: Path, rel: str) -> None:
    """目录项持久化（Windows 上 no-op）。"""
    _fs.fsync_directory(root, rel)


# ══════════════════════════ 锁（context manager，不泄漏 fd） ══════════════════════════

@contextmanager
def exclusive_lock(root: Path, rel: str):
    """排他非阻塞锁——生命周期操作（install/uninstall/recover）。
    冲突时 sys.exit（与现有行为一致）。"""
    with _fs.exclusive_lock(root, rel):
        yield


def try_shared_lock(root: Path, rel: str) -> Any:
    """尝试获取共享锁（doctor 用）。
    返回：锁对象（有 .release()）或 None 或 False（被排他持有）。"""
    return _fs.try_shared_lock(root, rel)


# ══════════════════════════ fd 级锁原语（transaction/zvec 等已持有 fd 的调用方） ══════════════════════════

def lock_exclusive_nb(fd: int) -> None:
    """对已打开的 fd 加排他非阻塞锁。冲突统一抛 BlockingIOError。"""
    _fs.lock_exclusive_nb(fd)


def lock_shared_nb(fd: int) -> None:
    """对已打开的 fd 加共享非阻塞锁。冲突统一抛 BlockingIOError。"""
    _fs.lock_shared_nb(fd)


def unlock_fd(fd: int) -> None:
    """释放 fd 上的锁（尽力，失败忽略——进程退出即释放）。"""
    _fs.unlock_fd(fd)


# ══════════════════════════ secure_* 家族（registry.py 委托入口） ══════════════════════════

def secure_walk_dir_fd(target, dir_rel: str) -> int:
    """打开到指定目录，返回 fd。POSIX: O_NOFOLLOW|O_DIRECTORY；Windows: islink 预检。"""
    return _fs.secure_walk_dir_fd(target, dir_rel)


def secure_open(target, rel: str, flags: int, mode: int = 0o600) -> int:
    """安全打开文件，返回 fd。路径校验由调用方（registry.validate_relative_path）完成。"""
    return _fs.secure_open(target, rel, flags, mode)


def secure_replace(target, src_rel: str, dst_rel: str) -> None:
    """原子替换。"""
    _fs.secure_replace(target, src_rel, dst_rel)


def secure_unlink(target, rel: str) -> None:
    """删除文件。"""
    _fs.secure_unlink(target, rel)


def secure_rmdir(target, rel: str) -> None:
    """删除空目录。"""
    _fs.secure_rmdir(target, rel)


def secure_mkdir(target, rel: str) -> None:
    """逐级创建目录（幂等）。"""
    _fs.secure_mkdir(target, rel)


# ══════════════════════════ 原子 no-replace rename ══════════════════════════

def rename_noreplace(src_dir_fd: int, src_name: str,
                     dst_dir_fd: int, dst_name: str) -> None:
    """原子 no-replace rename——目标已存在则 FileExistsError。
    fail-closed：不支持时抛 AtomicRenameNotSupported。"""
    _fs.rename_noreplace(src_dir_fd, src_name, dst_dir_fd, dst_name)


# ══════════════════════════ 平台信息 ══════════════════════════

def backend_name() -> str:
    """当前后端名称（诊断/doctor 显示用）。"""
    return _fs.BACKEND_NAME


def security_level() -> str:
    """安全等级声明：
    - POSIX: 'strict'（dir_fd + O_NOFOLLOW 原子防 TOCTOU）
    - Windows compatible: 'compatible'（path-based + islink 预检，
      检查与打开之间存在理论竞窗——不能完全抵御恶意本机进程）"""
    return _fs.SECURITY_LEVEL
