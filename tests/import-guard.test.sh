#!/bin/sh
# shellcheck disable=SC2015  # 断言惯用法
# Windows 可导入性守卫：installer/（platform/ 除外）不得顶层 import fcntl/msvcrt，
# 不得直用 os.O_NOFOLLOW / os.O_DIRECTORY（Windows 上 AttributeError——
# Codex P1：Windows 平台抽象未接管核心事务层的直接症状）。
set -e
pass=0; fail=0

ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }

VIOLATIONS="$(python3 - <<'EOF'
import ast, pathlib, sys
bad = []
for p in sorted(pathlib.Path(".").rglob("*.py")):
    parts = p.parts
    # 只检查 installer 包（工具脚本 doc-gate/governance-eval 是纯文本处理，无平台依赖）
    if len(parts) < 2 or parts[0] != "installer":
        continue
    if "platform" in parts:
        continue
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        bad.append(f"{p}: 语法错误 {e}")
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in ("fcntl", "msvcrt"):
                    bad.append(f"{p}:{node.lineno} import {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.module in ("fcntl", "msvcrt"):
            bad.append(f"{p}:{node.lineno} from {node.module} import")
        elif (isinstance(node, ast.Attribute)
              and isinstance(node.value, ast.Name) and node.value.id == "os"
              and node.attr in ("O_NOFOLLOW", "O_DIRECTORY")):
            bad.append(f"{p}:{node.lineno} os.{node.attr}（应使用 platform 常量）")
if bad:
    print("\n".join(bad))
EOF
)"

if [ -z "$VIOLATIONS" ]; then
    ok "installer/ 无 POSIX 专属导入与标志直用"
else
    bad "发现违规（Windows 导入即崩）:"
    echo "$VIOLATIONS"
fi

# platform/__init__.py 的 contextmanager 必须真正 yield（P1：exclusive_lock 委托缺 with/yield）
if python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
import inspect
from installer import platform as p
src = inspect.getsource(p.exclusive_lock)
assert "yield" in src, "exclusive_lock 未 yield——锁从未被持有"
EOF
then
    ok "platform.exclusive_lock 正确 yield（委托不是空壳）"
else
    bad "platform.exclusive_lock 缺 yield——锁从未被持有"
fi

# 事务记录必须携带 link_strategy（P1：copy 安装崩溃恢复曾变成 symlink）
if python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from installer.transaction import TransactionRecord
tx = TransactionRecord(tx_id="t", kind="install", status="planning")
assert hasattr(tx, "link_strategy"), "TransactionRecord 缺 link_strategy 字段"
d = tx.to_dict()
assert "link_strategy" in d, "link_strategy 未进 HMAC 保护负载"
EOF
then
    ok "事务记录携带 link_strategy（HMAC 保护）"
else
    bad "事务记录缺 link_strategy"
fi

# PowerShell 不会为原生命令可靠展开 *.py；CI 必须使用 Python 自己递归编译。
if grep -q 'python -m compileall -q installer' .github/workflows/ci.yml \
   && ! grep -q 'python -m py_compile installer/\*\.py' .github/workflows/ci.yml; then
    ok "Windows CI 由 compileall 展开 installer 源文件"
else
    bad "Windows CI 仍把 installer/*.py 字面量交给 py_compile"
fi

# archify 的供应链锁按原始字节计算；Windows checkout 也必须保留 LF。
ATTRS="$(git check-attr text eol -- vendor/archify/SKILL.md)"
if printf '%s\n' "$ATTRS" | grep -q 'text: set' \
   && printf '%s\n' "$ATTRS" | grep -q 'eol: lf'; then
    ok "archify vendor 快照跨平台固定为 LF"
else
    bad "archify vendor 未固定 LF——Windows checkout 会破坏供应链哈希"
fi

if python3 - <<'EOF'
from pathlib import PureWindowsPath
from installer.registry import _snapshot_rel_bytes, _sort_snapshot_files

assert _snapshot_rel_bytes(PureWindowsPath(r"renderers\shared\utils.mjs")) == (
    b"renderers/shared/utils.mjs"
)
root = PureWindowsPath(r"C:\vendor")
files = [root / "assets" / "a.txt", root / "LICENSE", root / "SKILL.md"]
assert [p.relative_to(root).as_posix() for p in _sort_snapshot_files(root, files)] == [
    "LICENSE", "SKILL.md", "assets/a.txt"
]
EOF
then
    ok "archify 快照路径哈希跨平台统一为 POSIX 分隔符"
else
    bad "archify 快照路径哈希仍依赖宿主路径分隔符"
fi

if grep -q '^from pathlib import Path$' memory-build; then
    ok "memory-build 的 Path 注解兼容 Python 3.10–3.13"
else
    bad "memory-build 使用 Path 注解但未在模块级导入"
fi

ENCODING_TMP="$(mktemp -d)"
mkdir "$ENCODING_TMP/repo"
if XDG_CONFIG_HOME="$ENCODING_TMP/config" PYTHONIOENCODING=cp1252 \
   python3 -m installer --link-strategy copy "$ENCODING_TMP/repo" >/dev/null 2>&1; then
    ok "安装器在窄编码重定向下不会因 Unicode 输出崩溃"
else
    bad "安装器输出依赖系统编码，窄编码重定向会崩溃"
fi
rm -rf "$ENCODING_TMP"

echo
echo "import-guard 测试: $pass 通过, $fail 失败"
[ "$fail" -eq 0 ] || exit 1
