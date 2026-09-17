#!/usr/bin/env python3
"""Validate the user-visible skill routing contract.

The public seam is the prose installed into a target repository: every name that
looks invokable must resolve to an installed skill, platform-specific examples
must not silently prefer one client, and an orchestration skill must hand control
back to the orchestrator instead of asking users to guess the next specialist.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from installer.registry import KIT_SKILLS, KIT_TOOLS  # noqa: E402


def frontmatter_name(path: Path) -> str | None:
    match = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)[\"']?\s*$", path.read_text())
    return match.group(1).strip() if match else None


errors: list[str] = []
skill_dirs = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
registered = set(KIT_SKILLS)
invokable = registered | {"archify"}

if skill_dirs != registered:
    errors.append(
        "installer.registry.KIT_SKILLS 与 skills/*/SKILL.md 不一致: "
        f"only_dirs={sorted(skill_dirs - registered)}, "
        f"only_registry={sorted(registered - skill_dirs)}"
    )

for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
    if frontmatter_name(path) != path.parent.name:
        errors.append(f"{path.relative_to(ROOT)} frontmatter name 与目录名不一致")

archify = ROOT / "vendor/archify/SKILL.md"
if frontmatter_name(archify) != "archify":
    errors.append("vendor/archify/SKILL.md 未声明 canonical name: archify")

# Explicit /foo or $foo examples are user-facing invocations, not prose labels.
documents = [ROOT / "README.md", *sorted((ROOT / "skills").glob("*/SKILL.md")),
             *sorted((ROOT / "templates").rglob("*.md"))]
for path in documents:
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"`([/$])([a-z][a-z0-9-]*)(?:\s+[^`]*)?`", text):
        name = match.group(2)
        if name not in invokable:
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{path.relative_to(ROOT)}:{line} 引用了未安装 skill: {match.group(0)}")

# The installed memory rules are shared by Claude Code and Codex, so an explicit
# invocation must show both spellings rather than teaching only slash commands.
memory_rules = (ROOT / "templates/memory-RULES.md").read_text(encoding="utf-8")
if "`/memory-check <符号>`" not in memory_rules or "`$memory-check <符号>`" not in memory_rules:
    errors.append("templates/memory-RULES.md 必须同时给出 /memory-check 与 $memory-check")

# These are CLI tools. Calling them "skills" is what made generated next-step
# prompts invent aliases, so the routing contract must classify them explicitly.
delivery = (ROOT / "skills/repo-delivery/SKILL.md").read_text(encoding="utf-8")
if "下游 skill 的 canonical name" not in delivery:
    errors.append("repo-delivery 缺少 canonical skill 路由约束")
else:
    routing_section = delivery.split("### 路由名称约束", 1)[1].split("\n## ", 1)[0]
    routing_names = set(re.findall(r"`([a-z][a-z0-9-]*)`", routing_section))
    missing_routes = (registered - {"repo-delivery"}) - routing_names
    unknown_routes = routing_names - invokable - set(KIT_TOOLS)
    if missing_routes:
        errors.append(f"repo-delivery canonical 路由清单缺失: {sorted(missing_routes)}")
    if unknown_routes:
        errors.append(f"repo-delivery 路由区含未知名称: {sorted(unknown_routes)}")
for tool in ("memory-recall", "memory-build", "doc-gate", "route-eval", "governance-eval", "domain-check"):
    if tool not in KIT_TOOLS:
        errors.append(f"测试配置错误：{tool} 不在 KIT_TOOLS")
    if f"`{tool}` 是 CLI" not in delivery:
        errors.append(f"repo-delivery 未把 {tool} 明确标为 CLI")

# design-pipeline is a specialist inside repo-delivery. After design it must
# return to the orchestrator; tdd and delivery-gate are not alternative entry
# names for the next user action.
pipeline = (ROOT / "skills/design-pipeline/SKILL.md").read_text(encoding="utf-8")
if "返回 `repo-delivery`" not in pipeline:
    errors.append("design-pipeline 完成后没有明确返回 repo-delivery")

# Human approval is a decision gate, not a reason to delegate local CLI work.
# Once approval and signer identity are explicit, the orchestrating Agent must
# persist the record and run doc-gate itself. Otherwise generated responses keep
# ending with copy/paste commands labelled as the user's next step.
for phrase in (
    "人工决策与机械执行分离",
    "只询问是否通过和签核人",
    "由 Agent 写入签核记录并执行",
    "不得把 `doc-gate` 命令列为“你的下一步”",
    "用户可见的继续入口只能是 canonical skill",
    "`/design-pipeline <SDD目录>`",
    "`$design-pipeline <SDD目录>`",
    "当前任务可继续时自动续跑",
    "用户提示零 CLI 泄漏",
    "请确认本轮业务流程设计是否通过，并提供评审人姓名；确认后我会继续当前 design-pipeline 流程并进入详细设计。",
    "不得出现 `doc-gate`、`.repo-memory-kit/bin/`、`--stage` 或 `--by`",
):
    if phrase not in pipeline:
        errors.append(f"design-pipeline 缺少自动续跑约束: {phrase}")
if re.search(r"提醒\s*维护者运行\s*`?\.repo-memory-kit/bin/doc-gate freeze", pipeline):
    errors.append("design-pipeline 仍要求维护者手工运行 doc-gate freeze")

if "本地 CLI 的执行责任" not in delivery:
    errors.append("repo-delivery 未声明本地 CLI 应由 Agent 自动执行")

# An upstream source title previously looked exactly like an invokable skill.
tdd = (ROOT / "skills/tdd/SKILL.md").read_text(encoding="utf-8")
if "`improve-codebase-architecture`" in tdd:
    errors.append("tdd 把上游资料名 improve-codebase-architecture 伪装成可调用 skill")

if errors:
    print("skill routing contract: FAIL")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)

print("skill routing contract: PASS")
