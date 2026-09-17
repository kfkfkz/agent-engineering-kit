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
    "由 Agent 写入签核记录并完成内部冻结",
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
safe_review_prompt = (
    "请确认本轮业务流程设计是否通过，并提供评审人姓名；"
    "确认后我会继续当前 design-pipeline 流程并进入详细设计。"
)
if pipeline.count(safe_review_prompt) < 2:
    errors.append("业务流程阶段没有复用零 CLI 泄漏的固定评审话术")
if re.search(
    r"评审人明确通过当前版本后.{0,160}doc-gate freeze",
    pipeline,
    flags=re.DOTALL,
):
    errors.append("业务流程评审叙述仍把人工确认与 freeze CLI 相邻，容易诱导提示泄漏")

business_flow_template = (
    ROOT / "templates/requirements/业务流程设计.md"
).read_text(encoding="utf-8")
review_note = business_flow_template.split("## 5. 评审记录", 1)[-1]
if "doc-gate" in review_note:
    errors.append("业务流程模板的人工评审记录仍向读者暴露内部 CLI")
if "完成内部冻结" not in review_note:
    errors.append("业务流程模板没有说明评审后由 Agent 自动续跑")

requirements_template = (
    ROOT / "templates/requirements/需求分析.md"
).read_text(encoding="utf-8")
freeze_note = requirements_template.split("## 冻结记录", 1)[-1]
if "doc-gate" in freeze_note:
    errors.append("需求分析模板的人工冻结记录仍向读者暴露内部 CLI")
if "完成内部冻结" not in freeze_note:
    errors.append("需求分析模板没有说明确认后由 Agent 自动续跑")

if "本地 CLI 的执行责任" not in delivery:
    errors.append("repo-delivery 未声明本地 CLI 应由 Agent 自动执行")
human_gate_section = delivery.split("**本地 CLI 的执行责任在 Agent**", 1)[-1]
human_gate_section = human_gate_section.split("## 定位目标仓库", 1)[0]
if "doc-gate freeze" in human_gate_section:
    errors.append("repo-delivery 的人工门禁说明仍把确认与 freeze CLI 相邻")
if "完成内部冻结" not in human_gate_section:
    errors.append("repo-delivery 的人工门禁说明没有抽象为内部冻结")

task_template = (ROOT / "templates/requirements/任务清单.md").read_text(encoding="utf-8")
for phrase in ("执行前计划基线", "执行记录与勾选状态", "完整终态哈希"):
    if phrase not in task_template:
        errors.append(f"任务清单模板缺少执行期冻结协议: {phrase}")

delivery_gate = (ROOT / "skills/delivery-gate/SKILL.md").read_text(encoding="utf-8")
for phrase in ("统一收口", "doc-gate closeout", "计划.closeout.json"):
    if phrase not in delivery_gate:
        errors.append(f"delivery-gate 缺少计划收口步骤: {phrase}")
for phrase in ("执行前计划基线", "doc-gate closeout"):
    if phrase not in delivery:
        errors.append(f"repo-delivery 缺少执行期计划协议: {phrase}")
if "执行前计划基线" not in pipeline:
    errors.append("design-pipeline 未说明计划门禁建立执行前基线")

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
