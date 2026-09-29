"""Pure Memory lookup-channel planning with an explicit fallback chain."""
from __future__ import annotations

from dataclasses import dataclass


_PATHS = ("mcp", "cli", "bounded_text")


@dataclass(frozen=True)
class CapabilitySnapshot:
    """Host-observed capabilities; no state is inferred from another state."""

    mcp_configured: bool
    mcp_visible: bool
    mcp_compatible: bool
    cli_available: bool
    bounded_text_available: bool
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.mcp_configured, self.mcp_visible, self.mcp_compatible,
            self.cli_available, self.bounded_text_available,
        )
        if any(type(value) is not bool for value in values):
            raise ValueError("capability states must be boolean observations")
        if (not isinstance(self.evidence, tuple)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.evidence)):
            raise ValueError("capability evidence must be non-empty strings")

    def as_dict(self) -> dict[str, object]:
        return {
            "mcp": {"configured": self.mcp_configured,
                    "visible": self.mcp_visible,
                    "compatible": self.mcp_compatible},
            "cli": {"available": self.cli_available},
            "bounded_text": {"available": self.bounded_text_available},
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class LookupAttempt:
    path: str
    failure_phase: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.path not in _PATHS:
            raise ValueError("unknown lookup path")
        if self.failure_phase not in {"before_start", "after_start"}:
            raise ValueError("lookup failure phase is invalid")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("lookup attempt needs a reason code")


@dataclass(frozen=True)
class KnowledgeLookupPlan:
    selected_path: str
    reason_code: str
    attempted_paths: tuple[str, ...]
    uncertainty: str
    capability_snapshot: CapabilitySnapshot

    def __post_init__(self) -> None:
        if self.selected_path not in {*_PATHS, "stop"}:
            raise ValueError("lookup plan selected path is invalid")
        if self.uncertainty not in {"normal", "degraded", "outcome_unknown"}:
            raise ValueError("lookup uncertainty is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "selected_path": self.selected_path,
            "reason_code": self.reason_code,
            "attempted_paths": list(self.attempted_paths),
            "uncertainty": self.uncertainty,
            "capability_snapshot": self.capability_snapshot.as_dict(),
        }


def _available_paths(snapshot: CapabilitySnapshot) -> tuple[str, ...]:
    paths: list[str] = []
    if snapshot.mcp_visible and snapshot.mcp_compatible:
        paths.append("mcp")
    if snapshot.cli_available:
        paths.append("cli")
    if snapshot.bounded_text_available:
        paths.append("bounded_text")
    return tuple(paths)


def plan_knowledge_lookup(
    capabilities: CapabilitySnapshot, *,
    attempts: tuple[LookupAttempt, ...] = (),
) -> KnowledgeLookupPlan:
    """Choose one channel; only a pre-start failure permits the next channel."""
    if not isinstance(capabilities, CapabilitySnapshot):
        raise ValueError("capability snapshot is required")
    if (not isinstance(attempts, tuple)
            or any(not isinstance(item, LookupAttempt) for item in attempts)):
        raise ValueError("lookup attempts must be a tuple")
    available = _available_paths(capabilities)
    if not available:
        if attempts:
            raise ValueError("attempts cannot exist without an available channel")
        return KnowledgeLookupPlan(
            "stop", "NO_LOOKUP_CHANNEL", (), "degraded", capabilities)
    if len({item.path for item in attempts}) != len(attempts):
        raise ValueError("a lookup channel may be attempted only once")
    if tuple(item.path for item in attempts) != available[:len(attempts)]:
        raise ValueError("lookup attempts violate MCP to CLI to text order")
    if attempts and attempts[-1].failure_phase == "after_start":
        path = attempts[-1].path.upper()
        return KnowledgeLookupPlan(
            "stop", f"{path}_OUTCOME_UNKNOWN",
            tuple(item.path for item in attempts), "outcome_unknown", capabilities)
    if any(item.failure_phase != "before_start" for item in attempts):
        raise ValueError("only the final attempt may have an unknown outcome")
    if len(attempts) == len(available):
        return KnowledgeLookupPlan(
            "stop", "ALL_CHANNELS_PRESTART_FAILED",
            tuple(item.path for item in attempts), "degraded", capabilities)
    selected = available[len(attempts)]
    if not attempts:
        reason = {
            "mcp": "MCP_AVAILABLE",
            "cli": "MCP_UNAVAILABLE",
            "bounded_text": "MCP_AND_CLI_UNAVAILABLE",
        }[selected]
        uncertainty = "normal" if selected == "mcp" else "degraded"
    else:
        reason = f"{attempts[-1].path.upper()}_PRESTART_FAILED"
        uncertainty = "degraded"
    return KnowledgeLookupPlan(
        selected, reason, tuple(item.path for item in attempts),
        uncertainty, capabilities)
