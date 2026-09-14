"""agent-engineering-kit installer 包——受管资源生命周期（设计文档 v12·冻结版）。

CLI 入口（python3 -m installer）：
  python3 -m installer <target>                            # 安装/更新（幂等）
  python3 -m installer --repair <target>                  # 修复缺失/漂移
  python3 -m installer --doctor [--codex-root DIR] <target>
  python3 -m installer --uninstall [--codex-root DIR] <target>
  python3 -m installer --adopt-legacy <target>             # 无 Manifest 的显式迁移（§18）
  python3 -m installer --recover=rollback|roll-forward <target>   # 人工恢复（§11.9）
  python3 -m installer --dry-run <target>                  # 只读计划预览
  python3 -m installer --codex-root /workspace <target>    # Codex workspace 链接
"""
from __future__ import annotations

import sys

__all__ = ["main"]

USAGE = ("用法: python3 -m installer [--update|--repair|--doctor|--uninstall|--dry-run] "
         "[--adopt-legacy] [--recover=rollback|roll-forward] [--codex-root DIR] "
         "[--migrate-specify[=FEATURE,...]] [--sdd-layout --sdd-version V] "
         "[--with-codebase-memory] [--svn] /path/to/your-project")


def _check_python() -> None:
    if sys.version_info < (3, 10):
        sys.exit(f"✗ 需要 Python ≥ 3.10（当前 {sys.version.split()[0]}）")


def main(argv: list[str] | None = None) -> int:
    _check_python()
    from pathlib import Path
    args = list(sys.argv[1:] if argv is None else argv)

    mode = "install"      # install | repair | doctor | uninstall | adopt-legacy | recover
    recover_strategy: str | None = None
    dry_run = False
    codex_root: str | None = None
    migrate_features: str | None = None       # None = 未要求迁移
    sdd_layout = False
    sdd_version: str | None = None
    with_codebase_memory = False
    with_svn = False
    target: str | None = None

    def set_mode(requested: str) -> None:
        nonlocal mode
        if mode != "install" and mode != requested:
            sys.exit(f"错误: --{requested} 不能与 --{mode} 同时使用")
        mode = requested

    while args:
        arg = args.pop(0)
        if arg == "--update":
            set_mode("install")            # 更新 = 幂等重装 kit 管辖文件
        elif arg == "--repair":
            set_mode("repair")
        elif arg == "--doctor":
            set_mode("doctor")
        elif arg == "--uninstall":
            set_mode("uninstall")
        elif arg == "--adopt-legacy":
            set_mode("adopt-legacy")
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--recover=rollback":
            set_mode("recover")
            recover_strategy = "rollback"
        elif arg == "--recover=roll-forward":
            set_mode("recover")
            recover_strategy = "roll-forward"
        elif arg == "--recover":
            set_mode("recover")
            if not args:
                sys.exit("错误: --recover 需要 rollback 或 roll-forward")
            recover_strategy = args.pop(0)
            if recover_strategy not in ("rollback", "roll-forward"):
                sys.exit(f"错误: --recover 只接受 rollback|roll-forward（收到 {recover_strategy!r}）")
        elif arg == "--codex-root":
            if not args:
                sys.exit("错误: --codex-root 需要目录参数")
            codex_root = args.pop(0)
        elif arg == "--migrate-specify":
            if migrate_features is None:
                migrate_features = ""
        elif arg.startswith("--migrate-specify="):
            migrate_features = arg.split("=", 1)[1]
            if not migrate_features:
                sys.exit("错误: --migrate-specify= 需要至少一个 feature 名（逗号分隔）")
        elif arg == "--sdd-layout":
            sdd_layout = True
        elif arg == "--sdd-version":
            if not args:
                sys.exit("错误: --sdd-version 需要版本目录名参数")
            sdd_version = args.pop(0)
        elif arg == "--with-codebase-memory":
            with_codebase_memory = True
        elif arg == "--svn":
            with_svn = True
        elif arg.startswith("-"):
            sys.exit(f"错误: 未知选项 {arg}\n{USAGE}")
        else:
            if target is not None:
                sys.exit(f"错误: 只能指定一个目标仓库\n{USAGE}")
            target = arg

    if target is None:
        sys.exit(USAGE)
    if dry_run and mode not in ("install", "repair"):
        sys.exit("错误: --dry-run 仅用于安装方向")
    if mode == "recover" and recover_strategy is None:
        sys.exit("错误: --recover 需要 rollback 或 roll-forward")
    if sdd_layout and not sdd_version:
        sys.exit("错误: --sdd-layout 需要 --sdd-version <版本目录名>（如 V1.0）")
    if sdd_version and not sdd_layout:
        sys.exit("错误: --sdd-version 仅在 --sdd-layout 时使用")
    if sdd_layout and migrate_features is None:
        sys.exit("错误: --sdd-layout 仅在 --migrate-specify 时使用")
    if with_codebase_memory and mode in ("uninstall", "doctor"):
        sys.exit("错误: --with-codebase-memory 不能与 --uninstall/--doctor 同时使用")
    if with_svn and mode not in ("install", "repair"):
        sys.exit("错误: --svn 仅用于安装方向（治理初始化）")
    if migrate_features is not None and mode != "install":
        sys.exit("错误: --migrate-specify 仅用于安装方向")
    if migrate_features is not None and not (Path(target) / ".specify" / "specs").is_dir():
        sys.exit("错误: --migrate-specify 要求目标存在 .specify/specs")

    repo = Path(target).expanduser()
    if not repo.is_dir():
        sys.exit(f"错误: 目标目录不存在: {repo}")
    repo = repo.resolve()
    codex = (Path(codex_root).expanduser().resolve()
             if codex_root is not None else None)

    if mode == "doctor":
        from .doctor import run_doctor
        return run_doctor(repo, codex_root_cli=codex)
    if mode == "uninstall":
        from .uninstall import run_uninstall
        return run_uninstall(repo, codex_root_cli=codex)
    if mode == "adopt-legacy":
        from .legacy import adopt_legacy
        return adopt_legacy(repo, codex_root_cli=codex)
    if mode == "recover":
        from .transaction import acquire_install_lock, recover_manual
        import os
        lock_fd = acquire_install_lock(repo)
        try:
            result = recover_manual(repo, recover_strategy)
        finally:
            os.close(lock_fd)
        print(f"恢复结果: {result.status}"
              + (f" — {result.detail}" if result.detail else ""))
        return 0 if result.status in ("no_action", "rolled_back",
                                       "rolled_back_to_clean",
                                       "roll_forward_completed") else 1

    from .install import run_install
    rc = run_install(repo, repair=(mode == "repair"), codex_root=codex,
                     dry_run=dry_run)
    if rc == 0 and not dry_run:
        _post_install_extras(repo, migrate_features, sdd_layout, sdd_version,
                             with_codebase_memory)
    if rc == 0 and not dry_run and with_svn:
        rc = _svn_setup(repo)
    return rc


def _svn_setup(repo) -> int:
    """--svn 团队治理初始化（SVN 多人协作，幂等；详见设计文档 svn-multiuser-governance）：
    1. svn:ignore 轻提交清单（合并追加，不覆盖既有项如 .idea）
    2. 需求目录种子（docs/01-需求/README.md——seed 式：存在即不动）
    3. svn add 内容确定文件（尊重 ignore；--parents 登记缺失父目录）
    4. 对已版本化的 kit 文本文件设 svn:eol-style=LF（防 Windows CRLF 化脱钩）
    5. 对"已版本化但本应 ignore"的文件给出整改提示（每机文件入库会永久冲突）"""
    import os
    import shutil
    import subprocess

    if shutil.which("svn") is None:
        print("✗ --svn 需要 svn 命令行客户端")
        return 1
    if not (repo / ".svn").is_dir():
        print(f"✗ --svn 要求目标是 SVN 工作副本: {repo}")
        return 1

    def svn(*args):
        return subprocess.run(["svn", *args], capture_output=True, text=True)

    def ignore_merge(rel, entries, reports):
        d = repo / rel
        get = svn("propget", "svn:ignore", str(d))
        current = [ln for ln in get.stdout.splitlines() if ln.strip()]
        missing = [e for e in entries if e not in current]
        if not missing:
            return
        result = svn("propset", "svn:ignore", "\n".join(current + missing) + "\n", str(d))
        if result.returncode != 0:
            reports.append(f"✗ svn:ignore({rel}) 设置失败: {result.stderr.strip()}")
        else:
            reports.append(f"✓ svn:ignore({rel}) += {' '.join(missing)}")

    reports: list[str] = []

    # 1. 根 ignore（工作副本根本必已版本化）
    ignore_merge(".", [".repo-memory-kit", ".mcp.json", ".codex"], reports)

    # 2. 子目录 ignore：未版本化则先 --parents --depth empty 登记再设
    #    （此后递归 add 尊重 ignore；settings.json 才不会被误加入）
    #    注：docs/memory 无需 ignore——v4 起记忆索引/锚点在 .repo-memory-kit/（整目录
    #    已 ignore），README.md 回归纯 seed（内容确定，可提交）
    for rel, entries in ((".claude", ["settings.json"]),):
        if not (repo / rel).is_dir():
            continue
        info = svn("info", str(repo / rel))
        if info.returncode != 0:
            add = svn("add", "--parents", "--depth", "empty", str(repo / rel))
            if add.returncode != 0:
                reports.append(f"✗ 无法登记 {rel}: {add.stderr.strip()}")
                continue
        ignore_merge(rel, entries, reports)

    # 3. 需求目录种子（seed 式：存在即不动）
    from .registry import KIT_DIR, secure_mkdir, secure_open, write_all
    req_rel = "docs/01-需求/README.md"
    if not (repo / req_rel).is_file():
        secure_mkdir(repo, str(_posix_parent(req_rel)))
        fd = secure_open(repo, req_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            write_all(fd, (KIT_DIR / "templates" / "requirements-index.md").read_bytes())
            os.fsync(fd)
        finally:
            os.close(fd)
        reports.append("✓ 已创建 docs/01-需求/README.md（需求指派索引，seed 式不再改写）")

    # 4. svn add 内容确定文件（A 类；ignore 已就位，--force 递归进已登记目录并跳过每机文件）
    add_targets = [".claude", ".agents", "docs/memory", "docs/01-需求",
                   ".cbmignore", "CLAUDE.md", "AGENTS.md"]
    added = []
    for rel in add_targets:
        if not (repo / rel).exists():
            continue
        result = svn("add", "--force", "--parents", str(repo / rel))
        if result.returncode == 0:
            added.append(rel)
    if added:
        reports.append(f"✓ svn add 已登记: {' '.join(added)}（按 svn status 复核后提交）")
    reports.append("• .specify 等自有内容如需入库请自行 svn add（不代管）")

    # 5. eol-style：已版本化的 kit 文本文件
    from .registry import REGISTRY
    a_class = [s.destination_path for s in REGISTRY
               if s.resource_type in ("owned_file", "seed_file")
               and not s.destination_path.startswith(".repo-memory-kit")
               and s.destination_path != "docs/memory/README.md"]
    a_class += ["CLAUDE.md", "AGENTS.md", ".cbmignore"]
    eol_set = 0
    for rel in dict.fromkeys(a_class):
        p = repo / rel
        if not p.is_file():
            continue
        get = svn("propget", "svn:eol-style", str(p))
        if get.returncode == 0 and get.stdout.strip() == "LF":
            continue
        if svn("propset", "svn:eol-style", "LF", str(p)).returncode == 0:
            eol_set += 1
    if eol_set:
        reports.append(f"✓ svn:eol-style=LF 已设于 {eol_set} 个 kit 文本文件")

    # 6. 已版本化但本应 ignore 的文件 → 整改提示
    for rel in (".mcp.json", ".codex", ".repo-memory-kit", ".claude/settings.json",
                "docs/memory/.anchors.json", ".repo-memory-kit/memory-index.md"):
        p = repo / rel
        if p.exists() and svn("info", str(p)).returncode == 0:
            reports.append(f"⚠ {rel} 已被版本控制（含每机内容，会永久冲突）——"
                           f"建议 svn rm --keep-local 后提交")

    print("SVN 治理初始化：")
    for line in reports:
        print(f"  {line}")
    print("  提交策略：仅提交内容确定文件（技能/模板/RULES/托管区块/需求与记忆条目）；"
          ".repo-memory-kit、.mcp.json、settings.json、.codex 为每机文件不入库。")
    return 0


def _posix_parent(rel: str) -> str:
    from pathlib import PurePosixPath
    parent = str(PurePosixPath(rel).parent)
    return "" if parent == "/" else parent


def _post_install_extras(repo, migrate_features, sdd_layout, sdd_version,
                         with_codebase_memory) -> None:
    """安装后的旁路步骤（不在事务内，沿用旧 install.sh 行为）：
    - Spec Kit 迁移：默认只检查报告；--migrate-specify 才执行保留原文的增量迁移
    - codebase-memory MCP：本地已有则配置，否则下载官方安装器（联网）"""
    import subprocess

    if with_codebase_memory:
        import shutil
        if shutil.which("codebase-memory-mcp"):
            print("正在使用已安装的 codebase-memory-mcp 配置当前 Agent 环境...")
            subprocess.run(["codebase-memory-mcp", "install"])
        elif shutil.which("curl"):
            import tempfile
            from pathlib import Path as _P
            with tempfile.TemporaryDirectory() as tmp:
                installer = _P(tmp) / "install.sh"
                print("正在下载并执行 DeusData/codebase-memory-mcp 官方安装器...")
                import urllib.request
                try:
                    urllib.request.urlretrieve(
                        "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh",
                        installer)
                except Exception as e:
                    print(f"✗ codebase-memory-mcp 安装器下载失败: {e}")
                else:
                    rc = subprocess.run(["sh", str(installer)]).returncode
                    if rc != 0:
                        print("✗ codebase-memory-mcp 官方安装器执行失败")
        else:
            print("✗ --with-codebase-memory 需要 curl 下载官方安装器")

    if (repo / ".specify" / "specs").is_dir():
        script = repo / ".repo-memory-kit" / "bin" / "spec-migrate"
        if not script.is_file():
            print("• 未发现 spec-migrate 工具（本次安装未包含？），跳过 Spec Kit 检查")
            return
        print(f"发现 Spec Kit: {repo / '.specify' / 'specs'}")
        cmd = [sys.executable, str(script),
               "--apply" if migrate_features is not None else "--check"]
        if migrate_features:
            for feature in migrate_features.split(","):
                if feature.strip():
                    cmd += ["--feature", feature.strip()]
        if sdd_layout:
            cmd += ["--layout", "sdd", "--sdd-version", sdd_version]
        cmd.append(str(repo))
        proc = subprocess.run(cmd)
        if migrate_features is None and proc.returncode == 0:
            print("提示: 当前仅检查未修改文档；确认后可使用 --migrate-specify[=FEATURE,...] 增量迁移")
        elif migrate_features is not None and proc.returncode != 0:
            print("✗ --migrate-specify 迁移失败（详见上方输出）")
