"""python3 -m installer 入口 shim。"""
import sys


def _configure_utf8_stdio() -> None:
    """CLI 输出固定 UTF-8；窄系统编码/重定向不得把安装变成事务失败。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


_configure_utf8_stdio()

from installer import main

if __name__ == "__main__":
    sys.exit(main())
