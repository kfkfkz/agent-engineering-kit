#!/bin/sh
# agent-engineering-kit 接入/更新/卸载 —— 薄壳 wrapper（设计文档 v12 §20）：
# 只做 Python ≥ 3.10 检测（§19），随后调用 installer 包；全部生命周期逻辑在
# installer/（受管资源生命周期：Registry 权限边界 + 事务 + 崩溃恢复）。
#
# 用法（与旧版一致，由 installer 包解释）:
#   ./install.sh /path/to/your-project            # 首次接入（幂等）
#   ./install.sh --update /path/to/your-project    # 更新 kit 管辖文件
#   ./install.sh --repair /path/to/your-project    # 修复缺失/漂移的 kit 管辖文件
#   ./install.sh --doctor /path/to/your-project    # 只读检查安装健康度
#   ./install.sh --uninstall /path/to/your-project # 卸载 kit 管辖产物（不触碰用户数据）
#   ./install.sh --dry-run /path/to/your-project   # 只展示计划，不写入
#   ./install.sh --migrate-specify[=001-demo] [--sdd-layout --sdd-version V1.0] /path
#   ./install.sh --with-codebase-memory /path      # 同时安装/配置代码图谱 MCP
#   ./install.sh --codex-root /workspace /path     # Codex 从父级 workspace 启动时暴露仓库技能
#   ./install.sh --adopt-legacy /path              # 无 Manifest 的存量迁移（登记）
#   ./install.sh --recover=rollback|roll-forward /path   # needs_human 事务人工恢复

set -e

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    echo "错误: agent-engineering-kit 需要 Python >= 3.10（当前 $(python3 -V 2>&1)）" >&2
    exit 1
fi

PYTHONPATH="$KIT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# 注意不能用 `python3 -m installer`：-m 会把调用方 CWD 置于 sys.path 首位，
# 目标项目自带的 installer/ 包（常见目录名）会遮蔽 kit 的安装器包。
# 经 -c 把 KIT_DIR 显式插到 sys.path[0]，CWD 退居其后；main 收到原始参数。
exec python3 -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from installer import main; sys.exit(main(sys.argv[1:]))' "$KIT_DIR" "$@"
