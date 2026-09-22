"""Application boundary for deterministic governance evaluation."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aek.core.policy.evaluator import GovernanceResult, evaluate_policy


def evaluate_governance(
    diff_text: str, policy: dict[str, Any], *,
    changed_files: Callable[[str], set[str]],
    added_lines: Callable[[str], list[tuple[str, int, str]]],
    deleted_files: Callable[[str], set[str]],
) -> GovernanceResult:
    """Parse one immutable diff snapshot, then invoke the pure policy judge."""
    if not isinstance(diff_text, str) or not isinstance(policy, dict):
        raise ValueError("governance evaluation needs text and policy")
    return evaluate_policy(
        changed_files(diff_text), added_lines(diff_text),
        deleted_files(diff_text), policy)
