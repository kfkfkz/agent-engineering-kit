"""pathcheck.py — 平台感知的相对路径校验（**唯一**校验器）。

第七轮审计修复（P1）：registry.validate_relative_path 此前固定 PurePosixPath——
Windows 的 C:\\absolute、UNC（\\\\server\\share）、..\\outside 都不会被识别为
绝对路径或父级跳转（PurePosixPath 把 "..\\outside" 当作单个组件名；拼接后
WindowsPath 会把它解析成两级，逃逸 target）。

规则（按目标平台选用 PureWindowsPath/PurePosixPath 语义）：
- 拒绝空路径与含 NUL 字节的路径
- 拒绝绝对路径 / 盘符 / 根 / UNC
- 拒绝任何 ".." 组件（含最后组件）
- POSIX 上反斜杠是合法文件名字符（无逃逸语义），按 PurePosixPath 放行

windows 参数供测试在任意平台显式选择语义（校验逻辑本身不因平台分叉）。
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

IS_WINDOWS = sys.platform == "win32"


def validate_relative_rel(rel: str, *, windows: bool | None = None) -> Path:
    """校验非空相对路径，返回组件化 Path。非法 → ValueError。"""
    if not rel or "\x00" in rel:
        raise ValueError(f"路径为空或含 NUL 字节: {rel!r}")
    use_windows = IS_WINDOWS if windows is None else windows
    p = PureWindowsPath(rel) if use_windows else PurePosixPath(rel)
    if p.is_absolute() or p.drive or p.root:
        raise ValueError(f"只接受非空相对路径（拒绝绝对路径/盘符/根/UNC）: {rel!r}")
    parts = p.parts
    if not parts:
        raise ValueError(f"路径为空: {rel!r}")
    if any(part == ".." for part in parts):
        raise ValueError(f"路径含 .. 组件: {rel!r}")
    return Path(*parts)
