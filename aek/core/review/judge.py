"""Pure gate verdict over validated review counts and policy thresholds."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JudgeInput:
    hard_fail_count: int
    blocker: int
    major: int
    minor: int
    upstream_pending: int
    consecutive_failures: int
    was_blocked: bool
    max_blocker: int
    max_major: int
    max_minor: int
    max_iterations: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == "was_blocked":
                if type(value) is not bool:
                    raise ValueError("was_blocked must be boolean")
            elif type(value) is not int or value < (1 if name == "max_iterations" else 0):
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class GateDecision:
    status: str
    over_threshold: bool
    current_attempt: int


def judge_gate(item: JudgeInput) -> GateDecision:
    if not isinstance(item, JudgeInput):
        raise ValueError("judge input is invalid")
    over = (item.blocker > item.max_blocker or item.major > item.max_major
            or item.minor > item.max_minor)
    current_attempt = item.consecutive_failures + 1
    if item.hard_fail_count == 0 and not over and item.upstream_pending == 0:
        status = "PASS"
    elif item.upstream_pending and item.hard_fail_count == 0 and not over:
        status = "BLOCKED"
    elif item.was_blocked or current_attempt >= item.max_iterations:
        status = "BLOCKED"
    else:
        status = "NEEDS_REVISION"
    return GateDecision(status, over, current_attempt)
