"""posix_fs.py — POSIX 高安全后端（Linux/macOS）。

现有 dir_fd + O_NOFOLLOW + O_DIRECTORY 实现的语义化封装。
安全等级：strict——检查与打开在同一原子域内，无 TOCTOU 窗口。
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import stat as stat_module
import sys
from pathlib import Path

BACKEND_NAME = "posix"
SECURITY_LEVEL = "strict"

from installer.atomic import rename_noreplace as _atomic_rename_noreplace
from installer.atomic import AtomicRenameNotSupported


def read_bytes(root: Path, rel: str) -> bytes | None:
    """读取文件内容；不存在 → None。"""
    try:
        fd = os.open(root / rel, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def create_exclusive(root: Path, rel: str, data: bytes, *, mode: int = 0o600) -> int:
    """O_CREAT|O_EXCL 创建。返回 fd。"""
    fd = os.open(root / rel,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd  # 调用方 close


def write_exclusive(root: Path, rel: str, data: bytes, *, mode: int = 0o600,
                    truncate: bool = False) -> int:
    """打开写入（O_CREAT，可选 O_TRUNC）。返回 fd。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    if truncate:
        flags |= os.O_TRUNC
    fd = os.open(root / rel, flags, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def atomic_replace(root: Path, src_rel: str, dst_rel: str) -> None:
    """原子替换（同目录 rename → fsync 父目录）。"""
    src = root / src_rel
    dst = root / dst_rel
    os.replace(src, dst)
    # fsync 父目录
    _fsync_dir(dst.parent)


def unlink(root: Path, rel: str) -> None:
    """删除文件。"""
    os.unlink(root / rel)
    _fsync_dir((root / rel).parent)


def rmdir(root: Path, rel: str) -> None:
    """删除空目录。"""
    os.rmdir(root / rel)


def mkdirs_checked(root: Path, rel: str) -> None:
    """逐级创建目录（幂等；符号链接/文件占位组件拒绝）。"""
    current = root
    parts = Path(rel).parts
    for part in parts:
        current = current / part
        if current.exists():
            if current.is_symlink():
                raise PermissionError(f"符号链接组件: {current}")
            if not current.is_dir():
                raise PermissionError(f"文件占位: {current}")
        else:
            current.mkdir(mode=0o755)


def check_path_safe(root: Path, rel: str) -> bool:
    """检查路径组件无符号链接。"""
    current = root
    for part in Path(rel).parts:
        current = current / part
        try:
            st = current.lstat()
            if stat_module.S_ISLNK(st.st_mode):
                return False
        except FileNotFoundError:
            return True  # 后续组件不存在
    return True


def fsync_directory(root: Path, rel: str) -> None:
    """目录 fsync（POSIX 独有）。"""
    _fsync_dir(root / rel)


# ══════════════════════════ 锁 ══════════════════════════

@contextlib.contextmanager
def exclusive_lock(root: Path, rel: str):
    """排他非阻塞锁。冲突时 sys.exit。"""
    lock_path = root / rel
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("✗ 另一个 kit 生命周期进程正在运行（install.lock 被持有），退出")
        yield fd
    finally:
        os.close(fd)


def try_shared_lock(root: Path, rel: str):
    """尝试共享锁（doctor）。返回锁对象或 None 或 False。"""
    lock_path = root / rel
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return False

    class _SharedLock:
        def release(self):
            os.close(fd)

    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    return _SharedLock()


# ══════════════════════════ fd 级锁原语 ══════════════════════════

def lock_exclusive_nb(fd: int) -> None:
    """排他非阻塞锁（fcntl.flock）。冲突抛 BlockingIOError。"""
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def lock_shared_nb(fd: int) -> None:
    """共享非阻塞锁。冲突抛 BlockingIOError。"""
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)


def unlock_fd(fd: int) -> None:
    """释放锁（flock 随 fd 关闭释放；显式解锁尽力）。"""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


# ══════════════════════════ secure_* 家族（POSIX dir_fd 实现） ══════════════════════════
# 路径校验（validate_relative_path）由 registry.py 调用方完成后再委托到这里。

def _validate_rel(rel: str) -> Path:
    """轻量路径校验——委托 platform/pathcheck 唯一校验器。"""
    from .pathcheck import validate_relative_rel
    return validate_relative_rel(rel)


def secure_walk_dir_fd(target: Path, dir_rel: str) -> int:
    """逐级 O_NOFOLLOW|O_DIRECTORY 打开到 dir_rel。"""
    if dir_rel in ("", "."):
        return os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    parts = _validate_rel(dir_rel).parts
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def secure_open(target: Path, rel: str, flags: int, mode: int = 0o600) -> int:
    """dir_fd 版打开（O_NOFOLLOW）。"""
    p = _validate_rel(rel)
    dir_fd = secure_walk_dir_fd(target, str(p.parent))
    try:
        return os.open(p.name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def secure_replace(target: Path, src_rel: str, dst_rel: str) -> None:
    """双 dir_fd 原子替换 + fsync。"""
    src = _validate_rel(src_rel)
    dst = _validate_rel(dst_rel)
    src_dir_fd = secure_walk_dir_fd(target, str(src.parent))
    try:
        dst_dir_fd = secure_walk_dir_fd(target, str(dst.parent))
        try:
            os.replace(src.name, dst.name,
                       src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            os.fsync(dst_dir_fd)
        finally:
            os.close(dst_dir_fd)
    finally:
        os.close(src_dir_fd)


def secure_unlink(target: Path, rel: str) -> None:
    """dir_fd 版 unlink + fsync。"""
    p = _validate_rel(rel)
    dir_fd = secure_walk_dir_fd(target, str(p.parent))
    try:
        os.unlink(p.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def secure_rmdir(target: Path, rel: str) -> None:
    """dir_fd 版 rmdir + fsync。"""
    p = _validate_rel(rel)
    dir_fd = secure_walk_dir_fd(target, str(p.parent))
    try:
        os.rmdir(p.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def secure_mkdir(target: Path, rel: str) -> None:
    """逐级创建目录（幂等；符号链接/文件占位拒绝）。"""
    if rel in ("", "."):
        return
    parts = _validate_rel(rel).parts
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            except OSError as e:
                raise PermissionError(f"目录组件已存在但异常（{e}）: {part}")
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)


def rename_noreplace(src_dir_fd: int, src_name: str,
                     dst_dir_fd: int, dst_name: str) -> None:
    """原子 no-replace rename（来自 atomic.py）。"""
    _atomic_rename_noreplace(src_dir_fd, src_name, dst_dir_fd, dst_name)


# ══════════════════════════ 内部 ══════════════════════════

def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("os.write returned 0")
        remaining = remaining[written:]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
