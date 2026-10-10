"""Advisory workflow rules; absence of evidence is not evidence of absence."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HASH = re.compile(r"[a-f0-9]{64}")
_RULES = tuple(f"WA-00{index}" for index in range(1, 8))


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    run_id: str
    pair_id: str
    task_id: str
    kind: str
    stage: str | None
    purpose: str | None
    code_sha256: str | None
    material_sha256: str | None
    timestamp_seconds: float | None
    coverage: str
    data: tuple[tuple[str, str | bool | int | None], ...] = field(default=(), repr=False)
    manifest_sha256: str | None = None

    def __post_init__(self):
        for value in (self.event_id, self.run_id, self.pair_id, self.task_id, self.kind):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("WORKFLOW_EVENT_INVALID")
        for value in (self.stage, self.purpose):
            if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                raise ValueError("WORKFLOW_EVENT_INVALID")
        for value in (self.code_sha256, self.material_sha256, self.manifest_sha256):
            if value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)):
                raise ValueError("WORKFLOW_EVENT_INVALID")
        if (self.timestamp_seconds is not None and
                (type(self.timestamp_seconds) not in {int, float} or self.timestamp_seconds < 0
                 or self.timestamp_seconds > 2**53 or not math.isfinite(self.timestamp_seconds))):
            raise ValueError("WORKFLOW_EVENT_INVALID")
        if (not isinstance(self.coverage, str) or self.coverage not in {"COMPLETE", "PARTIAL", "UNKNOWN"}
                or not isinstance(self.data, tuple) or len(self.data) > 32
                or any(not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[0], str)
                       or not _ID.fullmatch(row[0]) or type(row[1]) not in {str, bool, int, type(None)}
                       or isinstance(row[1], str) and len(row[1]) > 128
                       or type(row[1]) is int and not -(2**53) <= row[1] <= 2**53 for row in self.data)
                or len(dict(self.data)) != len(self.data)):
            raise ValueError("WORKFLOW_EVENT_INVALID")
        object.__setattr__(self, "data", tuple(sorted(self.data)))

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def analyze_workflow(events: tuple[WorkflowEvent, ...], *, trusted_event_digests: frozenset[str] | None = None,
                     complete_capture: bool = False, repeat_window_seconds: float = 60,
                     trusted_hidden_isolation_digests: frozenset[str] | None = None) -> dict:
    report = {"schema_version": 1, "rules": {rule: {"status": "NOT_EVALUATED",
                                                     "reason": "SEMANTIC_EVIDENCE_MISSING"} for rule in _RULES},
              "findings": [], "source": "trusted_semantic_events", "modifies_source": False}
    if (not isinstance(events, tuple) or len(events) > 10_000 or type(complete_capture) is not bool
            or type(repeat_window_seconds) not in {int, float} or not 0 < repeat_window_seconds <= 3600):
        raise ValueError("WORKFLOW_INPUT_INVALID")
    if (not complete_capture or not events or any(not isinstance(event, WorkflowEvent) for event in events)
            or any(event.coverage != "COMPLETE" for event in events)
            or trusted_event_digests is None or any(event.digest not in trusted_event_digests for event in events)):
        return report
    if len({event.event_id for event in events}) != len(events):
        return report
    def finding(rule, first, second, component, observation, recommendation):
        report["rules"][rule]["status"] = "FINDINGS"
        identity = hashlib.sha256(f"{rule}:{first.digest}:{second.digest}".encode("ascii")).hexdigest()
        report["findings"].append({"finding_id": "FND-" + identity,
                                   "rule": rule, "severity": "advisory", "component": component,
                                   "evidence_runs": list(dict.fromkeys((first.run_id, second.run_id))),
                                   "evidence_events": list(dict.fromkeys((first.event_id, second.event_id))),
                                   "observation": observation, "recommendation": recommendation})

    def repeat(rule, kind, extra, component, observation, recommendation):
        relevant = [event for event in events if event.kind == kind]
        if not relevant or any(event.stage is None or event.purpose is None or event.code_sha256 is None
                               or event.material_sha256 is None or event.timestamp_seconds is None
                               or any(not isinstance(dict(event.data).get(key), str)
                                      or not _HASH.fullmatch(dict(event.data)[key]) for key in extra)
                               for event in relevant):
            return
        report["rules"][rule] = {"status": "NO_FINDING", "reason": "COMPLETE_APPLICABLE_EVIDENCE"}
        seen = {}
        for event in relevant:
            data = dict(event.data)
            key = (event.pair_id, event.run_id, event.task_id, event.manifest_sha256,
                   event.stage, event.purpose, event.code_sha256,
                   event.material_sha256, *(data[name] for name in extra))
            previous = seen.get(key)
            if previous is not None and 0 <= event.timestamp_seconds - previous.timestamp_seconds <= repeat_window_seconds:
                finding(rule, previous, event, component, observation, recommendation)
            seen[key] = event

    repeat("WA-001", "memory.retrieve", (), "context-capsule",
           "Same material/code/purpose was retrieved again within the configured window.",
           "Check context capsule reuse; retain safety-required expansions.")
    repeat("WA-002", "codebase.refresh", ("generation_sha256",), "codebase-freshness",
           "The same code and graph generation were refreshed again.", "Check freshness barrier reuse.")
    repeat("WA-004", "review.completed", ("review_scope_sha256", "reviewer_sha256"), "incremental-review",
           "The same reviewer repeated review of unchanged material and scope.", "Check issue-directed review convergence.")
    repeat("WA-005", "gate.failed", ("failure_sha256",), "gate-recovery",
           "An unchanged gate failed again with the same cause.", "Clarify the failure and repair strategy before retrying.")
    design = [event for event in events if event.kind == "design.generated"]
    if design and all(event.code_sha256 is not None and event.material_sha256 is not None
                      and event.stage is not None and event.purpose is not None
                      and dict(event.data).get("route") in {"direct", "bounded", "standard", "initiative"}
                      and dict(event.data).get("artifact") in {"full_sdd", "bounded_note"}
                      and dict(event.data).get("requirement") in {"required", "optional", "skipped"}
                      and type(dict(event.data).get("user_override")) is bool for event in design):
        report["rules"]["WA-003"] = {"status": "NO_FINDING", "reason": "COMPLETE_APPLICABLE_EVIDENCE"}
        for event in design:
            data = dict(event.data)
            if (data["route"] == "direct" and data["artifact"] == "full_sdd"
                    and data["requirement"] == "skipped" and not data["user_override"]):
                finding("WA-003", event, event, "route-artifact-plan",
                        "A Direct task produced a full design marked skipped by its trusted plan.",
                        "Check route/ArtifactPlan alignment; do not remove risk-required evidence.")
    executions = [event for event in events if event.kind == "stage.execute"]
    if executions and all(event.code_sha256 is not None and event.material_sha256 is not None
                          and event.stage is not None and event.purpose is not None
                          and type(dict(event.data).get("recovered")) is bool
                          and dict(event.data).get("prior_commit") in {"COMMITTED", "STARTED", "NONE"}
                          and dict(event.data).get("prior_identity") in {"VALID", "STALE", "INVALID"}
                          for event in executions):
        report["rules"]["WA-006"] = {"status": "NO_FINDING", "reason": "COMPLETE_APPLICABLE_EVIDENCE"}
        for event in executions:
            data = dict(event.data)
            if data["recovered"] and data["prior_commit"] == "COMMITTED" and data["prior_identity"] == "VALID":
                finding("WA-006", event, event, "work-unit-recovery",
                        "Recovery executed a stage whose prior committed identity was still valid.",
                        "Check WorkUnit/receipt reuse before executing again.")
    verifications = [event for event in events if event.kind == "verification.result"]
    if verifications and all(event.code_sha256 is not None
                             and dict(event.data).get("verification_scope") in {"public", "isolated_hidden"}
                             and dict(event.data).get("verification_status") in {"PASS", "FAIL", "INFRA_ERROR", "NOT_EVALUATED"}
                             and isinstance(dict(event.data).get("verification_suite_sha256"), str)
                             and _HASH.fullmatch(dict(event.data)["verification_suite_sha256"])
                             for event in verifications):
        hidden = [event for event in verifications if dict(event.data)["verification_scope"] == "isolated_hidden"]
        if hidden and trusted_hidden_isolation_digests is not None and all(
                dict(event.data).get("isolation_sha256") in trusted_hidden_isolation_digests for event in hidden):
            report["rules"]["WA-007"] = {"status": "NO_FINDING", "reason": "COMPLETE_APPLICABLE_EVIDENCE"}

            def verification_identity(event):
                return (event.pair_id, event.run_id, event.task_id, event.manifest_sha256,
                        event.code_sha256, dict(event.data)["verification_suite_sha256"])

            public_passes = {}
            for public in verifications:
                data = dict(public.data)
                if data["verification_scope"] == "public" and data["verification_status"] == "PASS":
                    public_passes[verification_identity(public)] = public
            for event in hidden:
                if dict(event.data)["verification_status"] != "FAIL":
                    continue
                public = public_passes.get(verification_identity(event))
                if public is not None:
                    finding("WA-007", public, event, "verification-coverage",
                            "Observed public checks passed while independently isolated hidden checks failed.",
                            "Review coverage and assumptions; retain the independent failure evidence.")
    return report
