"""governance.py — 确定性治理策略引擎。

读取 governance.json 中的结构化规则，对 git diff 做机械匹配。
Skill 可以建议风险等级，但本引擎是**唯一裁决者**——只认 diff + 规则 + 证据。

核心原则：
1. 扫描 diff 的新增/修改行（不扫全文件——存量代码不应触发）
2. 删除的文件也参与匹配（删掉 security/ 同样触发）
3. 每条规则独立判定，输出命中的规则 ID 和需要补的 action
4. Agent 拿到的是"哪条规则命中、还缺什么"，不是模糊的"gate failed"
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RuleMatch:
    """一条规则的命中详情。"""
    rule_id: str
    matched_files: list[str] = field(default_factory=list)
    matched_lines: list[tuple[str, int, str]] = field(default_factory=list)  # (file, line_no, line_text)
    required_actions: list[str] = field(default_factory=list)


@dataclass
class GovernanceResult:
    """治理评估结果。"""
    profile: str                        # "strict" | "lightweight"
    matched: list[RuleMatch] = field(default_factory=list)
    default_actions: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)  # 合并（matched ∪ default 中的非 inline）
    all_actions: list[str] = field(default_factory=list)       # 全部（含 inline_review）

    @property
    def is_inline_only(self) -> bool:
        """True = 只需要 inline_review（commit message 内嵌验证结论）。"""
        non_inline = [a for a in self.required_actions if a != "inline_review"]
        return not non_inline and "inline_review" in self.all_actions

    @property
    def needs_formal_receipt(self) -> bool:
        """True = 需要正式回执文件。"""
        return "receipt" in self.required_actions or "independent_review" in self.required_actions

    @property
    def needs_independent_review(self) -> bool:
        """True = 需要独立审查（对抗复核）。"""
        return "independent_review" in self.required_actions

    def summary(self) -> str:
        """人类可读的治理摘要（delivery-gate 收口输出）。"""
        if not self.matched:
            return (f"Governance: {self.profile} | "
                    f"无规则命中 | 默认动作: {', '.join(self.default_actions) or 'none'}")
        lines = [f"Governance: {self.profile}"]
        for m in self.matched:
            lines.append(f"  命中规则: {m.rule_id}")
            lines.append(f"    required: {', '.join(m.required_actions)}")
            for f in m.matched_files[:5]:
                lines.append(f"    file: {f}")
            for fname, lineno, text in m.matched_lines[:3]:
                lines.append(f"    {fname}:{lineno}: {text[:80]}")
        if self.required_actions:
            lines.append(f"  总需: {', '.join(self.required_actions)}")
        return "\n".join(lines)


def load_policy(governance_path: Path) -> dict[str, Any]:
    """读取治理策略 JSON。缺失 → 返回 strict 默认。"""
    if not governance_path.is_file():
        return {"version": 1, "profile": "strict",
                "rules": [], "default": {"require": ["receipt"]}}
    try:
        return json.loads(governance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "profile": "strict",
                "rules": [], "default": {"require": ["receipt", "independent_review"]}}


def _glob_to_regex(pattern: str) -> str:
    """glob → regex（** 跨目录，* 单层，? 单字符）。
    先 re.escape 全部，再按优先级替换 token（防 * 干扰已替换的 .*）。"""
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*/", "(?:[^/]*/)*")   # **/ → 任意层目录
    escaped = escaped.replace(r"\*\*", ".*")              # **  → 任意
    escaped = escaped.replace(r"\*", "[^/]*")              # *   → 单层
    escaped = escaped.replace(r"\?", "[^/]")                # ?   → 单字符
    return escaped


def _file_matches_pattern(filepath: str, pattern: str) -> bool:
    """glob 模式匹配（** 支持跨目录）。"""
    if "**" in pattern:
        return re.fullmatch(_glob_to_regex(pattern), filepath) is not None
    return fnmatch.fnmatch(filepath, pattern)


def _diff_files(diff_text: str) -> set[str]:
    """从 git diff 输出中提取变更的文件路径。"""
    files = set()
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[6:] if line.startswith("+++ b/") else line[6:]
            if path != "/dev/null":
                files.add(path)
        elif line.startswith("diff --git"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.add(parts[1])
    return files


def _added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """从 git diff 中提取新增/修改的行（带文件名和行号）。"""
    results = []
    current_file = ""
    line_no = 0
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
            line_no = 0
        elif line.startswith("@@"):
            # 解析 @@ -old,count +new,count @@
            m = re.search(r"\+(\d+)", line)
            if m:
                line_no = int(m.group(1)) - 1
        elif line.startswith("+") and not line.startswith("+++"):
            line_no += 1
            content = line[1:]  # 去掉 +
            results.append((current_file, line_no, content))
        elif line.startswith("-") and not line.startswith("---"):
            pass  # 删除的行不计数
        else:
            line_no += 1
    return results


def _deleted_files(diff_text: str) -> set[str]:
    """从 git diff 中提取被删除的文件。"""
    deleted = set()
    for line in diff_text.split("\n"):
        if line.startswith("diff --git") and " b/" in line:
            pass  # 在后面检测 /dev/null
    # 更简单的方法：--- a/file 后面跟 +++ /dev/null
    lines = diff_text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("--- a/"):
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ /dev/null"):
                deleted.add(line[6:])
    return deleted


def evaluate_diff(diff_text: str, policy: dict[str, Any]) -> GovernanceResult:
    """对 git diff 应用治理策略——**唯一裁决入口**。

    参数：
    - diff_text: `git diff` 的完整输出（unified format）
    - policy: load_policy() 返回的策略 JSON

    返回 GovernanceResult，包含命中的规则和需要的动作。
    """
    result = GovernanceResult(profile=policy.get("profile", "strict"))
    default = policy.get("default", {})
    result.default_actions = list(default.get("require", []))
    result.all_actions = list(result.default_actions)

    changed_files = _diff_files(diff_text)
    added = _added_lines(diff_text)
    deleted = _deleted_files(diff_text)

    for rule in policy.get("rules", []):
        match = RuleMatch(
            rule_id=rule.get("id", "unknown"),
            required_actions=rule.get("require", []))
        match_spec = rule.get("match", {})

        # 路径匹配（新增/修改/删除的文件）
        for pattern in match_spec.get("paths", []):
            for f in changed_files | deleted:
                if _file_matches_pattern(f, pattern):
                    match.matched_files.append(f)

        # 文件名匹配（精确文件名，非路径 glob）
        for pattern in match_spec.get("files", []):
            for f in changed_files | deleted:
                if fnmatch.fnmatch(Path(f).name, pattern):
                    match.matched_files.append(f)

        # 新增行内容匹配（正则，只扫描 diff 新增行）
        for regex_str in match_spec.get("added_lines_regex", []):
            try:
                regex = re.compile(regex_str)
            except re.error:
                continue  # 无效正则跳过（策略文件问题，不崩溃）
            for fname, lineno, text in added:
                if regex.search(text):
                    match.matched_lines.append((fname, lineno, text.strip()))

        if match.matched_files or match.matched_lines:
            match.matched_files = sorted(set(match.matched_files))
            result.matched.append(match)
            # 合并 required_actions（去重）
            for action in match.required_actions:
                if action not in result.required_actions:
                    result.required_actions.append(action)
                if action not in result.all_actions:
                    result.all_actions.append(action)

    # default 的动作加入 all_actions 但不加入 required_actions
    for action in result.default_actions:
        if action not in result.all_actions:
            result.all_actions.append(action)

    return result


def evaluate(target: Path, diff_text: str) -> GovernanceResult:
    """便捷入口：读取 governance 文件并评估 diff。"""
    policy = load_policy(target / ".repo-memory-kit" / "governance.json")
    return evaluate_diff(diff_text, policy)
