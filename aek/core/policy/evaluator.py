"""Pure rule evaluation over an already normalized Git diff.

The CLI adapter owns Git's quoting/hunk format and policy-file I/O. This Core
module owns path/line matching and action aggregation, with no file writes.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any


class PolicyError(Exception):
    """Policy is invalid; callers must fail closed."""


@dataclass
class RuleMatch:
    rule_id: str
    matched_files: list[str] = field(default_factory=list)
    matched_lines: list[tuple[str, int, str]] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)


@dataclass
class GovernanceResult:
    profile: str
    matched: list[RuleMatch] = field(default_factory=list)
    default_actions: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
    all_actions: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @property
    def is_inline_only(self) -> bool:
        non_inline = [action for action in self.required_actions
                      if action != "inline_review"]
        return not non_inline and "inline_review" in self.all_actions

    @property
    def needs_formal_receipt(self) -> bool:
        merged = set(self.required_actions) | set(self.default_actions)
        return "receipt" in merged or "independent_review" in merged

    @property
    def needs_independent_review(self) -> bool:
        return "independent_review" in self.required_actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "matched_rules": [
                {"rule_id": item.rule_id, "files": item.matched_files,
                 "required": item.required_actions} for item in self.matched],
            "required_actions": self.required_actions,
            "all_actions": self.all_actions,
            "changed_files": self.changed_files,
            "is_inline_only": self.is_inline_only,
            "needs_formal_receipt": self.needs_formal_receipt,
            "needs_independent_review": self.needs_independent_review,
        }

    def summary(self) -> str:
        if not self.matched:
            return (f"Governance: {self.profile} | "
                    f"无规则命中 | 默认动作: {', '.join(self.default_actions) or 'none'}")
        lines = [f"Governance: {self.profile}"]
        for item in self.matched:
            lines.append(f"  命中规则: {item.rule_id}")
            lines.append(f"    required: {', '.join(item.required_actions)}")
            for path in item.matched_files[:5]:
                lines.append(f"    file: {path}")
            for path, lineno, value in item.matched_lines[:3]:
                lines.append(f"    {path}:{lineno}: {value[:80]}")
        if self.required_actions:
            lines.append(f"  总需: {', '.join(self.required_actions)}")
        return "\n".join(lines)


def _glob_to_regex(pattern: str) -> str:
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*/", "(?:[^/]*/)*")
    escaped = escaped.replace(r"\*\*", ".*")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace(r"\?", "[^/]")
    return escaped


def _file_matches_pattern(filepath: str, pattern: str) -> bool:
    if "**" in pattern:
        return re.fullmatch(_glob_to_regex(pattern), filepath) is not None
    return fnmatch.fnmatchcase(filepath, pattern)


def evaluate_policy(
    changed_files: set[str], added_lines: list[tuple[str, int, str]],
    deleted_files: set[str], policy: dict[str, Any],
) -> GovernanceResult:
    """Match validated rules and return actions in declared rule order."""
    result = GovernanceResult(profile=policy.get("profile", "strict"))
    default = policy.get("default", {})
    result.default_actions = list(default.get("require", []))
    result.all_actions = list(result.default_actions)
    result.changed_files = sorted(changed_files)

    for rule in policy.get("rules", []):
        match = RuleMatch(rule_id=rule.get("id", "unknown"),
                          required_actions=rule.get("require", []))
        match_spec = rule.get("match", {})
        for pattern in match_spec.get("paths", []):
            for path in changed_files | deleted_files:
                if _file_matches_pattern(path, pattern):
                    match.matched_files.append(path)
        for pattern in match_spec.get("files", []):
            for path in changed_files | deleted_files:
                if fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern):
                    match.matched_files.append(path)
        for regex_str in match_spec.get("added_lines_regex", []):
            try:
                regex = re.compile(regex_str)
            except re.error as exc:
                raise PolicyError(
                    f"规则 {rule.get('id', 'unknown')} 的正则非法: {regex_str!r}: {exc}") from exc
            for path, lineno, value in added_lines:
                if regex.search(value):
                    match.matched_lines.append((path, lineno, value.strip()))
        if match.matched_files or match.matched_lines:
            match.matched_files = sorted(set(match.matched_files))
            result.matched.append(match)
            for action in match.required_actions:
                if action not in result.required_actions:
                    result.required_actions.append(action)
                if action not in result.all_actions:
                    result.all_actions.append(action)
    for action in result.default_actions:
        if action not in result.all_actions:
            result.all_actions.append(action)
    return result
