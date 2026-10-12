"""Closed security policy contract and bounded protection defaults."""
from __future__ import annotations

from dataclasses import dataclass, fields


class SecurityPolicyError(ValueError):
    """Security policy cannot be used safely by this runtime."""


SCANNERS = ("trivy", "semgrep", "gitleaks")


@dataclass(frozen=True)
class SecurityPolicy:
    schema_version: int = 1
    default_scope: str = "targeted"
    dynamic_verification: str = "disabled"
    required_scanners: tuple[str, ...] = ()
    optional_scanners: tuple[str, ...] = SCANNERS
    max_duration_seconds: int = 300
    max_output_bytes: int = 2000000
    max_items: int = 10000
    max_advisory_age_days: int = 7

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise SecurityPolicyError("unsupported security policy schema")
        if (not isinstance(self.default_scope, str)
                or self.default_scope not in {"targeted", "focused", "full"}):
            raise SecurityPolicyError("invalid security default scope")
        if self.dynamic_verification != "disabled":
            raise SecurityPolicyError("dynamic verification is unsupported")
        for name in ("required_scanners", "optional_scanners"):
            items = getattr(self, name)
            if (not isinstance(items, tuple)
                    or any(not isinstance(item, str) or item not in SCANNERS
                           for item in items)
                    or len(set(items)) != len(items)):
                raise SecurityPolicyError("invalid security scanner configuration")
        for name, limit in (("max_duration_seconds", 300),
                            ("max_output_bytes", 2000000),
                            ("max_items", 10000),
                            # Freshness is project policy, not execution budget.
                            # A year is a finite configuration protection bound;
                            # the default remains seven days.
                            ("max_advisory_age_days", 365)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= limit:
                raise SecurityPolicyError("security policy budget is out of bounds")

    @classmethod
    def from_dict(cls, value: object) -> SecurityPolicy:
        if not isinstance(value, dict) or set(value) != {
                item.name for item in fields(cls)}:
            raise SecurityPolicyError("security policy has unknown or missing fields")
        data = dict(value)
        for name in ("required_scanners", "optional_scanners"):
            if not isinstance(data[name], list):
                raise SecurityPolicyError("security scanners must be arrays")
            data[name] = tuple(data[name])
        return cls(**data)

    def to_dict(self) -> dict[str, object]:
        return {item.name: (list(value) if isinstance(value, tuple) else value)
                for item in fields(self)
                for value in (getattr(self, item.name),)}


def decode_capability_guard(value: object) -> list[str]:
    """Only this closed require object locks out legacy array-only loaders."""
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "capabilities", "actions"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["capabilities"] != ["security-review-v1"]
            or value["actions"] != []):
        raise SecurityPolicyError("unsupported security capability guard")
    return []
