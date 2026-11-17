"""Document-stage projection, hard-check reports and review verdicts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable, ContextManager, Mapping, Protocol, Sequence, TypeVar

from aek.core.artifact.plan import (
    ArtifactPlan, MaterialStageBinding, bind_stage, load_binding_bytes,
    resolved_stage_prerequisites,
)
from aek.core.artifact.registry import ARTIFACT_REGISTRY, ArtifactRegistry
from aek.application.planning import DynamicPlanState
from aek.core.review.judge import JudgeInput, judge_gate
from aek.core.review.lifecycle import IssueState, LifecycleSnapshot


HARD_CHECK_REPORT_REF = "reviews/hard-checks.json"


class StageTransactionView(Protocol):
    def snapshot(self, *, for_material_recovery: bool = False) -> dict[str, bytes]: ...


_View = TypeVar("_View", bound=StageTransactionView)


def uses_material_bindings(snapshot: Mapping[str, bytes]) -> bool:
    return any(isinstance(load_binding_bytes(raw), MaterialStageBinding)
               for name, raw in snapshot.items() if name.endswith(".plan-binding.json"))


def run_stage_transaction(
    open_view: ContextManager[_View],
    load_plan: Callable[[dict[str, bytes]], DynamicPlanState | None],
    operate: Callable[[_View, DynamicPlanState | None, dict[str, bytes],
                       Callable[[str], None]], tuple[int, bool]],
    bind: Callable[[_View, DynamicPlanState, bool], int],
    *,
    emit_success: Callable[[str], None],
    recover: Callable[[_View, DynamicPlanState | None], None] | None = None,
) -> int:
    """Run recovery, stage publication and binding in one live lock scope."""
    success_messages: list[str] = []
    with open_view as view:
        if recover is not None:
            recover(view, load_plan(view.snapshot(for_material_recovery=True)))
        snapshot = view.snapshot()
        state = load_plan(snapshot)
        exit_code, material_mode = operate(view, state, snapshot, success_messages.append)
        if exit_code == 0 and state is not None:
            exit_code = bind(view, state, material_mode)
    if exit_code == 0:
        for message in success_messages:
            emit_success(message)
    return exit_code


class Checks:
    def __init__(self, stage: str):
        self.stage = stage
        self.items: list[dict] = []

    def add(self, group: str, name: str, ok: bool, detail: str = "") -> None:
        self.items.append({"group": group, "name": name,
                           "status": "PASS" if ok else "FAIL", "detail": detail})

    @property
    def fails(self) -> list[dict]:
        return [item for item in self.items if item["status"] == "FAIL"]

    def to_dict(self) -> dict:
        return {"stage": self.stage, "checks": self.items,
                "summary": {"pass": len(self.items) - len(self.fails),
                            "fail": len(self.fails)}}


@dataclass(frozen=True)
class HardCheckReport:
    stage: str
    counts: dict[str, int]
    failures: list[dict]
    raw: bytes
    signoff: dict[str, object]

    @classmethod
    def prepare(cls, checks: Checks, *, signoff: Mapping[str, object] | None = None) -> HardCheckReport:
        """Serialize the same report for the published bytes and their summary."""
        payload = checks.to_dict()
        fields = dict(signoff) if signoff is not None else {}
        payload.update(fields)
        return cls(checks.stage, payload["summary"], checks.fails,
                   json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), fields)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def summary_payload(self) -> dict:
        return {"schema_version": 1, "stage": self.stage,
                "status": "FAIL" if self.failures else "PASS",
                "summary": self.counts, "failures": self.failures,
                "full_result_ref": HARD_CHECK_REPORT_REF,
                "result_digest": hashlib.sha256(self.raw).hexdigest(), **self.signoff}


def publish_review_gate(gate, lifecycle_store, lifecycle, previous,
                        publish_pass: Callable[[bytes], None],
                        publish_failure: Callable[[bytes], None]) -> dict:
    """Preserve the lifecycle store's verified → gate → closed publication order."""
    passed = gate["status"] == "PASS"
    if lifecycle_store is None or lifecycle is None:
        publisher = publish_pass if passed else publish_failure
        publisher(json.dumps(gate, ensure_ascii=False, indent=2).encode("utf-8"))
        return gate
    expected = None if previous is None else previous.snapshot_digest
    if passed:
        _, published = lifecycle_store.publish_pass_gate(
            lifecycle, gate, expected_snapshot_digest=expected, gate_publisher=publish_pass)
        return published
    return lifecycle_store.publish_non_pass_gate(
        lifecycle, gate, expected_snapshot_digest=expected)


def require_binding_prerequisites(state: DynamicPlanState, stage: str,
                                  states: Mapping[str, str]) -> None:
    group = ARTIFACT_REGISTRY.get_group(stage)
    ancestors = {parent for artifact in group.artifact_ids
                 for parent in state.plan.by_id(artifact).resolved_prerequisites}
    if any(states[parent] != "BOUND" for parent in ancestors):
        raise ValueError("SOURCE_CHANGED: a required prerequisite is not bound")


def prepare_approved_binding(state: DynamicPlanState, stage: str, raw_approval: bytes | None,
                             digest: str, documents, fingerprints):
    approved = json.loads(raw_approval) if raw_approval is not None else None
    if not isinstance(approved, dict) or approved.get("upstream_fingerprints") != fingerprints:
        raise ValueError("SOURCE_CHANGED: current prerequisites differ from the published approval")
    return bind_stage(state.plan, state.credential, stage, gate_digest=digest,
                      doc_hashes=documents, prerequisite_fingerprints=fingerprints)


def prepare_human_approval(stage: str, approved_by: str, time: str,
                           sources: Mapping[str, bytes], upstream: Mapping[str, str | None]) -> dict:
    return {"stage": stage, "status": "PASS", "time": time,
            "approved_by": approved_by or "维护者",
            "doc_hashes": {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()},
            "upstream_fingerprints": upstream,
            "frozen_by": "doc-gate freeze（人工签核 + 哈希凭证）"}


def prepare_execution_closeout(time: str, gate_raw: bytes, *, projected: bool,
                               hashes: Mapping[str, str],
                               upstream: Mapping[str, str | None]) -> dict:
    return {"stage": "计划", "status": "PASS", "kind": "execution_closeout", "time": time,
            "plan_gate_sha256": hashlib.sha256(gate_raw).hexdigest(),
            "baseline_mode": "projected" if projected else "legacy_unavailable",
            "doc_hashes": hashes, "upstream_fingerprints": upstream}


def prepare_gate_verdict(
    checks: Checks,
    issues: Sequence[Mapping[str, object]],
    lifecycle: LifecycleSnapshot | None,
    policy: Mapping[str, int],
    *,
    consecutive: int,
    was_blocked: bool,
) -> dict:
    """Project verified observations into the existing gate verdict fields."""
    upstream = [issue for issue in issues if issue.get("upstream")]
    if lifecycle is None:
        counted = [issue for issue in issues if not issue.get("upstream")]
    else:
        upstream_ids = {str(issue.get("id")) for issue in upstream}
        unresolved = {IssueState.OPEN, IssueState.FIXED, IssueState.REOPENED, IssueState.BLOCKED}
        counted = [{"id": issue.occurrences[-1].issue_id,
                    "severity": issue.occurrences[-1].severity}
                   for issue in lifecycle.issues
                   if issue.state in unresolved and issue.occurrences[-1].issue_id not in upstream_ids]
    blocker, major, minor = (
        sum(1 for issue in counted if issue["severity"] == severity)
        for severity in ("blocker", "major", "minor")
    )
    hard_fail = checks.fails
    decision = judge_gate(JudgeInput(
        len(hard_fail), blocker, major, minor, len(upstream), consecutive, was_blocked,
        policy["max_blocker"], policy["max_major"], policy["max_minor"], policy["max_iterations"],
    ))
    status = decision.status
    reasons = []
    if hard_fail:
        reasons.append("hard checks FAIL: " + "；".join(item["name"] for item in hard_fail[:5]))
    if decision.over_threshold:
        reasons.append(f"severity 超阈值: blocker={blocker}/{policy['max_blocker']}, "
                       f"major={major}/{policy['max_major']}, minor={minor}/{policy['max_minor']}")
    if upstream:
        reasons.append(f"上游打回未决 {len(upstream)} 项（待维护者裁决）")
    if status == "PASS":
        next_step = "进入下一阶段（文档已冻结）"
    elif upstream and not hard_fail and not decision.over_threshold:
        next_step = "维护者裁决上游问题后对本阶段重新评审"
    elif status == "NEEDS_REVISION":
        next_step = "定向修改上述问题后重新提交"
    elif not was_blocked:
        next_step = f"已连续 {policy['max_iterations']} 轮不达标——升级人工介入"
    else:
        next_step = "BLOCKED 未解除：人工介入后需重新评审且判定 PASS"
    return {
        "status": status,
        "hard": {"pass": len(checks.items) - len(hard_fail), "fail": len(hard_fail),
                 "failed": [f"{item['group']}/{item['name']}" for item in hard_fail]},
        "review": {"blocker": blocker, "major": major, "minor": minor,
                   "upstream_pending": len(upstream),
                   "lifecycle_pending": len(counted) if lifecycle is not None else 0,
                   "fix_targets": [issue.get("id", "?") for issue in counted][:10] if status != "PASS" else []},
        "iteration": decision.current_attempt,
        "failed_because": reasons,
        "next": next_step,
    }


def participating_documents(
    plan: ArtifactPlan, stage: str,
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> tuple[str, ...]:
    group = registry.get_group(stage)
    return tuple(
        registry.get_artifact(artifact_id).filename
        for artifact_id in group.artifact_ids
        if plan.by_id(artifact_id).classification != "skipped")


def effective_stage_states(
    plan: ArtifactPlan, binding_states: Mapping[str, str],
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> dict[str, str]:
    """Expose plan-authorized whole-stage skips without forging a binding."""
    if set(binding_states) != set(registry.groups):
        raise ValueError("binding state map must cover every stage")
    return {
        stage: ("SKIPPED" if not participating_documents(plan, stage, registry)
                else binding_states[stage])
        for stage in registry.groups
    }


def participating_prerequisites(
    plan: ArtifactPlan, stage: str,
    registry: ArtifactRegistry = ARTIFACT_REGISTRY,
) -> tuple[str, ...]:
    return resolved_stage_prerequisites(plan, stage, registry)
