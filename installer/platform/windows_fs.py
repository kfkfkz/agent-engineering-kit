"""windows_fs.py — Windows compatible 后端。

安全等级：compatible——path-based + islink 预检。
不能完全抵御恶意本机进程制造的 TOCTOU 竞争（检查与打开之间存在理论竞窗）。
适合普通本地开发机，不适用于对抗性环境。

关键差异（vs POSIX strict）：
- dir_fd 不可用 → 路径拼接 + islink 预检
- O_NOFOLLOW 不可用 → os.path.islink() 检查
- fcntl.flock → msvcrt.locking（LK_NBLCK 排他，锁 1 字节足够互斥）
- 目录 fsync 不可用 → no-op（NTFS 日志保证元数据一致性）
- os.rename 默认不覆盖（等价 RENAME_NOREPLACE）→ 直接用
- O_DIRECTORY 不可用 → 直接 os.open（Windows 允许打开目录读取）
- 符号链接/junction → path_is_symlink 统一检查
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

BACKEND_NAME = "windows_compatible"
SECURITY_LEVEL = "compatible"


def _is_junction(path: Path) -> bool:
    """检测 NTFS junction（Python 3.12+ 有 os.path.isjunction）。"""
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    return False


def path_is_symlink(path: Path) -> bool:
    """检查是否为符号链接或 junction。"""
    if path.is_symlink():
        return True
    return _is_junction(path)


def _check_no_reparse(root: Path, rel: str) -> Path:
    """逐级检查路径组件无符号链接/junction。返回完整路径。"""
    full = root / rel
    current = root
    for part in Path(rel).parts:
        current = current / part
        if path_is_symlink(current):
            raise PermissionError(f"路径包含符号链接/junction: {current}")
    return full


# ══════════════════════════ 语义化文件操作 ══════════════════════════

def read_bytes(root: Path, rel: str) -> bytes | None:
    """读取文件；不存在 → None。"""
    full = _check_no_reparse(root, rel)
    try:
        return full.read_bytes()
    except FileNotFoundError:
        return None


def create_exclusive(root: Path, rel: str, data: bytes, *, mode: int = 0o600) -> int:
    """O_CREAT|O_EXCL 创建。返回 fd。"""
    full = _check_no_reparse(root, str(Path(rel).parent))
    fd = os.open(full / Path(rel).name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_exclusive(root: Path, rel: str, data: bytes, *, mode: int = 0o600,
                    truncate: bool = False) -> int:
    """打开写入。返回 fd。"""
    full = _check_no_reparse(root, str(Path(rel).parent))
    flags = os.O_WRONLY | os.O_CREAT
    if truncate:
        flags |= os.O_TRUNC
    fd = os.open(full / Path(rel).name, flags, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def atomic_replace(root: Path, src_rel: str, dst_rel: str) -> None:
    """原子替换（Windows os.replace → MoveFileEx 覆盖语义）。"""
    _check_no_reparse(root, str(Path(src_rel).parent))
    _check_no_reparse(root, str(Path(dst_rel).parent))
    os.replace(root / src_rel, root / dst_rel)
    # 目录 fsync 在 Windows 上无操作（NTFS 日志保证一致性）


def unlink(root: Path, rel: str) -> None:
    """删除文件。"""
    _check_no_reparse(root, str(Path(rel).parent))
    os.unlink(root / rel)


def rmdir(root: Path, rel: str) -> None:
    """删除空目录。"""
    _check_no_reparse(root, str(Path(rel).parent))
    os.rmdir(root / rel)


def mkdirs_checked(root: Path, rel: str) -> None:
    """逐级创建目录（幂等；符号链接/junction/文件占位拒绝）。"""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.exists():
            if path_is_symlink(current):
                raise PermissionError(f"符号链接/junction 组件: {current}")
            if not current.is_dir():
                raise PermissionError(f"文件占位: {current}")
        else:
            current.mkdir(mode=0o755)


def check_path_safe(root: Path, rel: str) -> bool:
    """检查路径组件无符号链接/junction。"""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if path_is_symlink(current):
            return False
    return True


def fsync_directory(root: Path, rel: str) -> None:
    """Windows 无目录 fsync——no-op。"""
    pass  # NTFS 日志保证元数据一致性


# ══════════════════════════ 锁 ══════════════════════════

@contextlib.contextmanager
def exclusive_lock(root: Path, rel: str):
    """排他非阻塞锁（msvcrt.locking）。冲突时 sys.exit。"""
    import msvcrt
    lock_path = root / rel
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            sys.exit("✗ 另一个 kit 生命周期进程正在运行（install.lock 被持有），退出")
        yield fd
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(fd)


def try_shared_lock(root: Path, rel: str):
    """尝试共享锁（doctor 用）。"""
    import msvcrt
    lock_path = root / rel
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError:
        return False

    class _SharedLock:
        def release(self):
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)

    try:
        msvcrt.locking(fd, msvcrt.LK_NBRLCK, 1)
    except OSError:
        os.close(fd)
        return False
    return _SharedLock()


def rename_noreplace(src_dir_fd: int, src_name: str,
                     dst_dir_fd: int, dst_name: str) -> None:
    """Windows os.rename 默认不覆盖——直接用。
    注意：参数中的 fd 在 Windows 上被忽略（不可用），改用路径。"""
    # Windows 的 os.rename 不带 dir_fd（会报错），用路径替代
    # fd 在这里实际上是路径（由调用方适配）或不可用
    os.rename(src_name, dst_name)  # 路径形式；Windows 默认 no-replace


# ══════════════════════════ 内部 ══════════════════════════

def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("os.write returned 0")
        remaining = remaining[written:]
