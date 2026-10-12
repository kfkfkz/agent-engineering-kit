"""Shared installation defaults and advisory comparison of team-owned rules."""
from __future__ import annotations

import json
import re

from aek.core.security.policy import SecurityPolicy, decode_capability_guard

DEFAULTS_VERSION = "1.0.2"
SECURITY_DEFAULTS_VERSION = "1.0.4"


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


def default_security_governance_policy() -> dict:
    """Return opt-in v2 defaults, never seed or overwrite team policy implicitly."""
    policy = default_governance_policy()
    policy["version"] = 2
    policy["security_review"] = SecurityPolicy().to_dict()
    policy["rules"].insert(0, {
        "id": "SECURITY-CAPABILITY", "require": {
            "schema_version": 1, "capabilities": ["security-review-v1"],
            "actions": []}})
    return policy


def _canonical_rule(rule: dict) -> str:
    # Matching and requirement lists are unordered sets. Preserve unknown
    # fields in the comparison rather than treating them as equivalent.
    normalized = dict(rule)
    required = normalized.get("require", [])
    match = normalized.get("match", {})
    if isinstance(required, dict):
        decode_capability_guard(required)
        normalized["require"] = required
    elif (not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or not isinstance(match, dict)):
        raise ValueError("unsupported rule structure")
    else:
        normalized["require"] = sorted(set(required))
    if not isinstance(match, dict):
        raise ValueError("unsupported matcher structure")
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
            or policy["version"] not in {1, 2}
            or not isinstance(policy.get("rules"), list)):
        raise ValueError("unsupported governance schema")
    version = policy["version"]
    if version == 2:
        if set(policy) != {"version", "rules", "security_review"}:
            raise ValueError("unsupported governance v2 schema")
        SecurityPolicy.from_dict(policy["security_review"])
        guards = [rule["require"] for rule in policy["rules"]
                  if isinstance(rule, dict) and isinstance(rule.get("require"), dict)]
        if not guards:
            raise ValueError("missing security capability guard")
        for guard in guards:
            decode_capability_guard(guard)
    actual = {}
    for rule in policy["rules"]:
        if not isinstance(rule, dict):
            raise TypeError("unsupported rule structure")
        if version == 1 and not isinstance(rule.get("require", []), list):
            raise ValueError("unsupported v1 requirement structure")
        rule_id = rule.get("id")
        if (not isinstance(rule_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", rule_id)
                or rule_id in actual):
            raise ValueError("unsupported or duplicate rule identity")
        actual[rule_id] = _canonical_rule(rule)
    baseline = (default_security_governance_policy() if version == 2
                else default_governance_policy())
    expected = {rule["id"]: _canonical_rule(rule) for rule in baseline["rules"]}
    missing = sorted(expected.keys() - actual.keys())
    changed = sorted(key for key in expected.keys() & actual.keys()
                     if expected[key] != actual[key])
    extra = sorted(actual.keys() - expected.keys())
    if version == 2 and policy["security_review"] != baseline["security_review"]:
        changed.append("security_review")
    status = "policy_update_available" if missing or changed else "managed"
    defaults_version = SECURITY_DEFAULTS_VERSION if version == 2 else DEFAULTS_VERSION
    detail = (f"schema={version}; baseline=unknown; current_defaults={defaults_version}; "
              f"missing={','.join(missing) or 'none'}; "
              f"different={','.join(changed) or 'none'}; "
              f"additional={len(extra)}; "
              "只读差异提示，差异可能是团队有意定制，不代表策略更弱；不自动迁移；"
              "security_policy_v2=explicit_opt_in")
    return status, detail
