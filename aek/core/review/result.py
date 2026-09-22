"""Validate a review sensor result against the exact document snapshot.

This module does not decide whether findings pass the gate. It only establishes
that the input is well-formed and bound to the documents being judged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping


VALID_SEVERITIES = ("blocker", "major", "minor")
ISSUE_FIELDS = ("id", "severity", "location", "problem", "required_change")


@dataclass(frozen=True)
class ReviewResult:
    stage: str
    doc_hashes: tuple[tuple[str, str], ...]
    issues: tuple[dict, ...]


def validate_review_payload(
    text: str, stage: str, expected_doc_hashes: Mapping[str, str | None]
) -> tuple[ReviewResult | None, str]:
    """Return a normalized result or the legacy doc-gate validation error."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "issues.json 不是合法 JSON"
    if not isinstance(data, dict):
        return None, "issues.json 顶层必须是对象"
    if data.get("stage") != stage:
        return None, f"issues.json 的 stage={data.get('stage')!r} 与门禁阶段 {stage!r} 不符"
    recorded = data.get("doc_hashes")
    if not isinstance(recorded, dict) or not recorded:
        return None, ("issues.json 缺 doc_hashes（评审必须记录被评审文档的 "
                      "sha256——防旧评审冻结新版本）")
    for doc, current_hash in expected_doc_hashes.items():
        wanted_hash = recorded.get(doc)
        if wanted_hash is None:
            return None, f"issues.json 的 doc_hashes 未覆盖 {doc}"
        if wanted_hash != current_hash:
            return None, (f"{doc} 在评审后有改动（哈希失配）——评审已过期，"
                          f"需对当前版本重新评审")
    issues = data.get("issues")
    if not isinstance(issues, list):
        return None, "issues 必须是数组"
    seen_ids: set[str] = set()
    normalized: list[dict] = []
    for i, issue in enumerate(issues):
        if not isinstance(issue, dict):
            return None, f"issues[{i}] 不是对象"
        for field in ISSUE_FIELDS:
            if not isinstance(issue.get(field), str) or not issue[field].strip():
                return None, f"issues[{i}] 缺必填字段 {field}（id/severity/location/problem/required_change）"
        severity = str(issue.get("severity", "")).strip().lower()
        if severity not in VALID_SEVERITIES:
            return None, (f"issues[{i}].severity={severity!r} 非法"
                          f"（只允许 blocker/major/minor）")
        normalized_issue = {**issue, "severity": severity}
        if normalized_issue["id"] in seen_ids:
            return None, f"issues[{i}].id 重复: {normalized_issue['id']}"
        seen_ids.add(normalized_issue["id"])
        normalized.append(normalized_issue)
    return ReviewResult(stage, tuple((doc, recorded[doc]) for doc in expected_doc_hashes),
                        tuple(normalized)), ""
