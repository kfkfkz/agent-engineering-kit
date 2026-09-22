"""Pure dynamic document-stage projection over a frozen ArtifactPlan."""
from __future__ import annotations

from typing import Mapping

from aek.core.artifact.plan import ArtifactPlan, resolved_stage_prerequisites
from aek.core.artifact.registry import ARTIFACT_REGISTRY, ArtifactRegistry


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
