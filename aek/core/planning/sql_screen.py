"""Database-neutral, low-cost SQL/ORM performance screen evidence schema.

This records candidate scale risks, not query plans, timings or proof of runtime
performance. Dialect is descriptive and intentionally not a fixed allowlist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from aek.core.planning.facts import FactObservation


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"raw_sql", "orm", "migration", "batch"}
_DIMENSIONS = {
    "access_shape", "result_bounds", "round_trips", "predicate_selectivity",
    "transaction_scope", "ddl_migration", "batch_fanout",
}
_INDICATORS = {
    "n_plus_one", "unbounded_scan", "missing_index_candidate",
    "conversion_risk", "long_transaction", "write_lock_amplification",
    "batch_fanout", "unknown_access_path",
}


@dataclass(frozen=True)
class SqlPerformanceScreenResult:
    subject_digest: str
    dialect: str
    query_kind: str
    screened_dimensions: tuple[str, ...]
    risk_indicators: tuple[str, ...]
    scope_complete: bool
    evidence_digest: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported SQL screen schema")
        if not isinstance(self.subject_digest, str) or not _DIGEST.fullmatch(self.subject_digest):
            raise ValueError("subject digest must be SHA-256")
        if not isinstance(self.evidence_digest, str) or not _DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("evidence digest must be SHA-256")
        if (not isinstance(self.dialect, str) or not self.dialect.strip()
                or len(self.dialect) > 64
                or any(ord(char) < 32 for char in self.dialect)):
            raise ValueError("dialect must identify the target database")
        if not isinstance(self.query_kind, str) or self.query_kind not in _KINDS:
            raise ValueError("unknown SQL/ORM query kind")
        if (not isinstance(self.screened_dimensions, tuple)
                or any(not isinstance(item, str) or item not in _DIMENSIONS
                       for item in self.screened_dimensions)
                or len(set(self.screened_dimensions)) != len(self.screened_dimensions)):
            raise ValueError("unknown or duplicate screen dimension")
        if (not isinstance(self.risk_indicators, tuple)
                or any(not isinstance(item, str) or item not in _INDICATORS
                       for item in self.risk_indicators)
                or len(set(self.risk_indicators)) != len(self.risk_indicators)):
            raise ValueError("unknown or duplicate risk indicator")
        if type(self.scope_complete) is not bool:
            raise ValueError("scope_complete must be boolean")
        if (self.scope_complete or self.risk_indicators) and not self.screened_dimensions:
            raise ValueError("clean or risky screen requires an inspected dimension")

    @property
    def capacity_state(self) -> str:
        if self.risk_indicators:
            return "true"
        return "false" if self.scope_complete else "unknown"

    def to_fact_observation(self) -> FactObservation:
        return FactObservation(
            fact_id="performance_capacity",
            producer_id="sql_performance_screen",
            state=self.capacity_state,
            subject_digest=self.subject_digest,
            evidence_digest=self.evidence_digest,
            scope_complete=self.scope_complete,
        )
