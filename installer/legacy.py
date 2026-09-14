"""legacy.py — §18 历史产物识别与无 Manifest 迁移。

--adopt-legacy：唯一**不修改任何内容、仅凭 legacy 匹配登记** Manifest 条目的
流程（用于 Manifest 丢失后的重建）。普通 install 遇到 legacy 片段是**原地升级**
（install.py），不走本模块。

legacy_hashes.json（installer/ 侧，随 kit 发布、目标仓库不可篡改）由
--generate 从 kit 的 git 历史生成（不依赖浅克隆不可用的时间线；无 git 时只含
确定性的旧格式条目）。不依赖 git history 的浅克隆也能用已发布的文件。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from .manifest import ManifestEntry, build_manifest, write_manifest_atomic
from .registry import (
    FRAGMENT_ABSENT,
    GITIGNORE_START,
    GITIGNORE_END,
    REGISTRY,
    KIT_DIR,
    ContainerUnreadableError,
    compute_fragment_hashes,
    current_kit_version,
    read_fragment,
    trusted_canonical_hashes,
)

# 旧版 .gitignore 行组（v6 前：有 start 行、无 end 标记，位于文件末尾）——
# 提取形态恰为 start 至 EOF，canonical 无绝对路径可替换
LEGACY_GITIGNORE_V1 = f"{GITIGNORE_START}\n.repo-memory-kit/zvec/\n"

# v4 中期的带 end 标记形态（只 ignore zvec/）——内容升级后成为历史版本，
# 存量安装靠它保持血统（managed → 可更新），否则会被误判 conflict
LEGACY_GITIGNORE_V2 = f"{GITIGNORE_START}\n.repo-memory-kit/zvec/\n{GITIGNORE_END}"
LEGACY_GITIGNORE_V3 = (f"{GITIGNORE_START}\n.repo-memory-kit/zvec/\n"
                       f".repo-memory-kit/memory-index.md\n"
                       f".repo-memory-kit/.anchors.json\n{GITIGNORE_END}")

# 旧版 .codex/config.toml 无标记 TOML 节（v1 前 install.sh 追加形态；canonical
# 用 ${TARGET} 占位符——与运行时对实际绝对路径的规范化结果一致）
LEGACY_CODEX_TOML_V1 = ('[mcp_servers.agent-engineering]\n'
                        'command = "${TARGET}/.repo-memory-kit/bin/agent-engineering-mcp"\n')


def adopt_legacy(target: Path, *, codex_root_cli: Path | None = None) -> int:
    """§18 显式迁移：对 Registry 每个 spec 做内容血统比对，canonical ∈
    trusted（当前 ∪ legacy）→ 登记 ManifestEntry（action=adopted_legacy，
    before_state=unknown——纯审计字段）。FRAGMENT_ABSENT 或不匹配 → 报告列出。
    不修改任何文件内容。"""
    target = target.resolve()
    entries: list[ManifestEntry] = []
    adopted: list[str] = []
    unmatched: list[str] = []
    skipped: list[str] = []

    for spec in REGISTRY:
        if spec.resource_type == "codex_link":
            # 链接无内容血统可验（§5 独立判定）；有 --codex-root 也只核验不登记
            skipped.append(f"{spec.id}: codex_link 无内容血统，不参与采纳")
            continue
        try:
            fragment = read_fragment(target / spec.destination_path, spec)
        except ContainerUnreadableError as e:
            unmatched.append(f"{spec.id}: 容器不可读: {e}")
            continue
        if fragment is FRAGMENT_ABSENT:
            unmatched.append(f"{spec.id}: 片段缺失")
            continue
        installed, canonical = compute_fragment_hashes(fragment, target)
        if canonical in trusted_canonical_hashes(spec, target):
            entries.append(ManifestEntry(
                spec_id=spec.id, action="adopted_legacy", before_state="unknown",
                installed_hash=installed, canonical_hash=canonical,
                codex_root=None))
            adopted.append(spec.id)
        else:
            unmatched.append(f"{spec.id}: 非 kit 当前或历史版本产物")

    if not entries:
        print("✗ 没有任何资源匹配 kit 当前或历史版本产物——不生成清单。")
        for line in unmatched:
            print(f"  - {line}")
        for line in skipped:
            print(f"  • {line}")
        return 1

    write_manifest_atomic(target, build_manifest(target, current_kit_version(), entries))
    print(f"✓ 已登记 {len(entries)} 项（adopted_legacy，未修改任何内容）——"
          f"此后 doctor / uninstall 走正常流程。")
    for line in skipped:
        print(f"  • {line}")
    if unmatched:
        print(f"• 以下 {len(unmatched)} 项未入清单（不入清单 = 不获得删除授权，§18）:")
        for line in unmatched:
            print(f"  - {line}")
    return 0


# ══════════════════════════ legacy_hashes.json 生成（CI / --generate） ══════════════════════════

def _git(kit_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(kit_dir), *args],
                          capture_output=True, text=True, timeout=60)


def _path_history_blobs(kit_dir: Path, rel: str) -> list[tuple[str, bytes]]:
    """(commit_short, blob_bytes) 列表——该路径在 git 全历史中的每个版本。"""
    proc = _git(kit_dir, "log", "--all", "--format=%H", "--", rel)
    if proc.returncode != 0:
        return []
    out: list[tuple[str, bytes]] = []
    for rev in proc.stdout.split():
        check = _git(kit_dir, "cat-file", "-e", f"{rev}:{rel}")
        if check.returncode != 0:
            continue                     # blob 不存在的提交必须跳过（空内容陷阱）
        show = _git(kit_dir, "show", f"{rev}:{rel}")
        if show.returncode != 0:
            continue
        short = _git(kit_dir, "rev-parse", "--short", rev)
        out.append((short.stdout.strip() if short.returncode == 0 else rev[:7],
                    show.stdout.encode("utf-8")))
    return out


def generate_legacy_hashes(kit_dir: Path | None = None) -> dict:
    """生成 legacy_hashes.json 内容：
    - owned_file / seed_file：源文件 git 全历史各版本的内容 hash
      （canonical == 内容——kit 源不含目标绝对路径）
    - 模板型 managed_block（CLAUDE.md / AGENTS.md）：模板历史版本 →
      区块形态（start + "\n" + 模板内容 + end）的 hash（与 install.sh 追加
      时的提取形态一致）
    - .gitignore 旧行组（无 end 标记）：确定性旧格式（LEGACY_GITIGNORE_V1）
    不含：.mcp.json / hook（形状恒定，无历史差异）；.codex/config.toml 旧
    无标记段落（extract_block 无法提取，不伪造血统）。"""
    kit_dir = (kit_dir or KIT_DIR).resolve()
    kit_versions: dict[str, dict[str, str]] = {}

    def add(version: str, spec_id: str, digest: str) -> None:
        kit_versions.setdefault(version, {})[spec_id] = f"canonical_sha256:{digest}"

    for spec in REGISTRY:
        if spec.resource_type == "codex_link":
            continue
        if spec.resource_type in ("json_fragment", "hook"):
            continue
        if spec.id == "gitignore-block":
            for tag, form in (("legacy-gitignore-v1", LEGACY_GITIGNORE_V1),
                              ("legacy-gitignore-v2", LEGACY_GITIGNORE_V2),
                              ("legacy-gitignore-v3", LEGACY_GITIGNORE_V3)):
                add(tag, spec.id, hashlib.sha256(form.encode()).hexdigest())
            continue
        if spec.id == "mcp-codex":
            # 无标记 TOML 节（canonical 形态含 ${TARGET}）
            add("legacy-codex-toml-v1", spec.id,
                hashlib.sha256(LEGACY_CODEX_TOML_V1.encode()).hexdigest())
            continue
        if spec.source_path is None:
            continue
        if spec.resource_type in ("owned_file", "seed_file"):
            for version, blob in _path_history_blobs(kit_dir, spec.source_path):
                add(version, spec.id, hashlib.sha256(blob).hexdigest())
        elif spec.resource_type == "managed_block":
            # 模板历史 → 标记区块形态（start\n<模板>end）
            for version, blob in _path_history_blobs(kit_dir, spec.source_path):
                text = blob.decode("utf-8", errors="replace")
                if not text.endswith("\n"):
                    text += "\n"
                block = f"{spec.block_start}\n{text}{spec.block_end}"
                add(version, spec.id, hashlib.sha256(block.encode()).hexdigest())
                # CLAUDE/AGENTS 旧格式：裸标题段（无标记，提取形态 = 模板原文）
                if spec.id in ("claude-md-block", "agents-md-block"):
                    add(f"{version}:legacy-section", spec.id,
                        hashlib.sha256(blob).hexdigest())

    return {"kit_versions": kit_versions}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--generate":
        out = KIT_DIR / "installer" / "legacy_hashes.json"
        data = generate_legacy_hashes()
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        versions = len(data["kit_versions"])
        specs = len({s for entries in data["kit_versions"].values() for s in entries})
        print(f"✓ 已生成 {out.relative_to(KIT_DIR)}（{versions} 个版本 / {specs} 个 spec）")
        return 0
    if len(args) == 1:
        return adopt_legacy(Path(args[0]))
    print("用法: python3 -m installer.legacy <target> | --generate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
