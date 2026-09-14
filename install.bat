@echo off
rem agent-engineering-kit installer - Windows entry point
rem Usage: install.bat [options] C:\path\to\your-project
setlocal EnableDelayedExpansion

set "KIT_DIR=%~dp0"

rem Python >= 3.10 check
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo 错误: agent-engineering-kit 需要 Python ^>= 3.10
    python --version
    exit /b 1
)

rem 调用 installer 包（sys.path 注入 KIT_DIR 避免包名遮蔽）
python -c "import sys; sys.path.insert(0, sys.argv.pop(1)); from installer import main; sys.exit(main(sys.argv[1:]))" "%KIT_DIR%" %*
endlocal
