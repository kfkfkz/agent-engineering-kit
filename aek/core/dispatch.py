"""Pure pre-dispatch decision and immutable outcome contracts."""
from __future__ import annotations

from dataclasses import dataclass
def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise ValueError("dispatch version must be a string")
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise ValueError("dispatch version must be numeric dotted form") from exc
    if not parts or any(part < 0 for part in parts):
        raise ValueError("dispatch version must be numeric dotted form")
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


@dataclass(frozen=True)
class DispatchDecision:
    selected_path: str
    reason: str


@dataclass(frozen=True)
class DispatchOutcome:
    selected_path: str
    started: bool
    committed: bool
    retryable: bool
    result: object | None = None
    error: str = ""


def select_dispatch(*, service_version: str, minimum_version: str,
                    available_capabilities: frozenset[str],
                    required_capability: str,
                    parity_passed: bool) -> DispatchDecision:
    if (not isinstance(available_capabilities, frozenset)
            or any(not isinstance(item, str) or not item
                   for item in available_capabilities)
            or not isinstance(required_capability, str)
            or not required_capability or type(parity_passed) is not bool):
        raise ValueError("invalid dispatch capability evidence")
    if _version(service_version) < _version(minimum_version):
        return DispatchDecision("legacy", "service version is below minimum")
    if required_capability not in available_capabilities:
        return DispatchDecision("legacy", "required capability is unavailable")
    if not parity_passed:
        return DispatchDecision("legacy", "parity evidence is not current")
    return DispatchDecision("service", "version, capability and parity verified")
