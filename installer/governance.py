"""Shared installation defaults and advisory comparison of team-owned rules."""
from __future__ import annotations

import json
import re

DEFAULTS_VERSION = "1.0.2"


def default_governance_policy() -> dict:
    """Return fresh defaults; the installer never updates an existing policy."""
    return {
        "version": 1,
        "rules": [
            {"id": "SEC-001",
             "require": ["receipt", "independent_review",
                         "required_check:security-review"],
             "match": {"paths": ["**/security/**", "**/auth/**"]}},
            {"id": "DB-001",
             "require": ["receipt", "independent_review",
                         "required_check:migration-plan",
                         "required_check:rollback-verification",
                         "required_check:sql-performance-screen"],
             "match": {
                 "paths": ["**/migration/**", "**/migrations/**", "**/schema/**"],
                 "files": ["*migration*", "*schema*", "*ddl*"]}},
            {"id": "DB-SQL-001",
             "require": ["required_check:sql-performance-screen"],
             "match": {"files": ["*.sql"]}},
        ],
    }


def _canonical_rule(rule: dict) -> str:
    # Matching and requirement lists are unordered sets. Preserve unknown
    # fields in the comparison rather than treating them as equivalent.
    normalized = dict(rule)
    required = normalized.get("require", [])
    match = normalized.get("match", {})
    if (not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or not isinstance(match, dict)):
        raise ValueError("unsupported rule structure")
    normalized["require"] = sorted(set(required))
    normalized["match"] = dict(match)
    for key in ("paths", "files", "added_lines_regex"):
        if key in match:
            items = match[key]
            if (not isinstance(items, list)
                    or any(not isinstance(item, str) for item in items)):
                raise ValueError("unsupported matcher structure")
            normalized["match"][key] = sorted(set(items))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def compare_governance_defaults(policy: object) -> tuple[str, str]:
    """Compare current defaults, not policy strength or historical provenance."""
    if (not isinstance(policy, dict) or type(policy.get("version")) is not int
            or policy["version"] != 1 or not isinstance(policy.get("rules"), list)):
        raise ValueError("unsupported governance schema")
    actual = {}
    for rule in policy["rules"]:
        if not isinstance(rule, dict):
            raise TypeError("unsupported rule structure")
        rule_id = rule.get("id")
        if (not isinstance(rule_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", rule_id)
                or rule_id in actual):
            raise ValueError("unsupported or duplicate rule identity")
        actual[rule_id] = _canonical_rule(rule)
    expected = {rule["id"]: _canonical_rule(rule)
                for rule in default_governance_policy()["rules"]}
    missing = sorted(expected.keys() - actual.keys())
    changed = sorted(key for key in expected.keys() & actual.keys()
                     if expected[key] != actual[key])
    extra = sorted(actual.keys() - expected.keys())
    status = "policy_update_available" if missing or changed else "managed"
    detail = (f"schema=1; baseline=unknown; current_defaults={DEFAULTS_VERSION}; "
              f"missing={','.join(missing) or 'none'}; "
              f"different={','.join(changed) or 'none'}; "
              f"additional={len(extra)}; "
              "只读差异提示，差异可能是团队有意定制，不代表策略更弱；不自动迁移")
    return status, detail
