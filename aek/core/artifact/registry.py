"""Document artifacts and the v1.0.0-compatible stage view.

This module is the only authority for static document topology. It performs no
filesystem I/O; callers receive fresh legacy dictionaries so they cannot mutate
the registry through the compatibility interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


ReviewPolicy = bool | str


@dataclass(frozen=True)
class ArtifactSpec:
    id: str
    filename: str
    group_id: str
    sections: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactGroup:
    id: str
    artifact_ids: tuple[str, ...]
    prerequisites: tuple[str, ...]
    review_policy: ReviewPolicy


class ArtifactRegistry:
    def __init__(
        self, artifacts: tuple[ArtifactSpec, ...], groups: tuple[ArtifactGroup, ...],
        *, schema_version: int = 1,
    ) -> None:
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("unsupported artifact registry schema version")
        self.schema_version = schema_version
        self._artifacts = MappingProxyType({item.id: item for item in artifacts})
        self._groups = MappingProxyType({item.id: item for item in groups})
        if len(self._artifacts) != len(artifacts):
            raise ValueError("duplicate artifact ID")
        if len(self._groups) != len(groups):
            raise ValueError("duplicate group ID")
        self.validate()

    @property
    def artifacts(self) -> Mapping[str, ArtifactSpec]:
        return self._artifacts

    @property
    def groups(self) -> Mapping[str, ArtifactGroup]:
        return self._groups

    def get_artifact(self, artifact_id: str) -> ArtifactSpec:
        return self._artifacts[artifact_id]

    def get_group(self, group_id: str) -> ArtifactGroup:
        return self._groups[group_id]

    def validate(self) -> None:
        filenames: set[str] = set()
        assigned: set[str] = set()
        for artifact in self._artifacts.values():
            if not artifact.id or not artifact.filename or not artifact.group_id:
                raise ValueError("artifact identity must be non-empty")
            filename_key = artifact.filename.casefold()
            if (filename_key in filenames or "/" in artifact.filename
                    or "\\" in artifact.filename or artifact.filename in {".", ".."}):
                raise ValueError("duplicate or unsafe artifact filename")
            if not artifact.filename.endswith(".md"):
                raise ValueError("artifact filename must be Markdown")
            if (not artifact.sections or any(not section for section in artifact.sections)
                    or len(set(artifact.sections)) != len(artifact.sections)):
                raise ValueError("artifact sections must be unique and non-empty")
            filenames.add(filename_key)
        for group in self._groups.values():
            if not group.id or not group.artifact_ids:
                raise ValueError("group identity/artifacts must be non-empty")
            if type(group.review_policy) is not bool and group.review_policy != "optional":
                raise ValueError("unknown review policy")
            if len(set(group.artifact_ids)) != len(group.artifact_ids):
                raise ValueError("duplicate artifact in group")
            for artifact_id in group.artifact_ids:
                artifact = self._artifacts.get(artifact_id)
                if artifact is None or artifact.group_id != group.id or artifact_id in assigned:
                    raise ValueError("unknown or multiply assigned artifact")
                assigned.add(artifact_id)
            if len(set(group.prerequisites)) != len(group.prerequisites):
                raise ValueError("duplicate prerequisite")
            if any(parent not in self._groups for parent in group.prerequisites):
                raise ValueError("unknown prerequisite")
        if assigned != set(self._artifacts):
            raise ValueError("unassigned artifact")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(group_id: str) -> None:
            if group_id in visiting:
                raise ValueError("cyclic artifact group prerequisites")
            if group_id in visited:
                return
            visiting.add(group_id)
            for prerequisite in self._groups[group_id].prerequisites:
                visit(prerequisite)
            visiting.remove(group_id)
            visited.add(group_id)

        for group_id in self._groups:
            visit(group_id)

    def resolve_prerequisites(self, group_id: str) -> tuple[str, ...]:
        """Return transitive upstream groups in dependency-first order."""
        self.get_group(group_id)
        seen: set[str] = set()
        order: list[str] = []

        def collect(current: str) -> None:
            for parent in self._groups[current].prerequisites:
                if parent not in seen:
                    collect(parent)
                    seen.add(parent)
                    order.append(parent)

        collect(group_id)
        return tuple(order)

    def legacy_stage_view(self) -> dict[str, dict[str, object]]:
        """Produce fresh mutable dictionaries matching doc-gate's historical STAGES."""
        view: dict[str, dict[str, object]] = {}
        for group in self._groups.values():
            artifacts = [self._artifacts[item] for item in group.artifact_ids]
            view[group.id] = {
                "docs": [item.filename for item in artifacts],
                "upstream": list(group.prerequisites),
                "review": group.review_policy,
                "sections": {item.filename: list(item.sections) for item in artifacts},
            }
        return view


ARTIFACT_REGISTRY = ArtifactRegistry(
    (
        ArtifactSpec("requirements-analysis", "需求分析.md", "需求分析",
                     ("背景", "目标", "非目标", "场景与验收依据", "验收项", "非功能要求", "待确认事项", "冻结记录")),
        ArtifactSpec("outline-design", "概要设计.md", "概要设计",
                     ("设计目标", "现状分析", "总体方案", "功能点设计", "方案取舍", "数据与接口影响", "风险")),
        ArtifactSpec("business-flow", "业务流程设计.md", "业务流程设计",
                     ("修订记录", "简介", "业务流程总览", "功能设计", "数据与接口概览", "评审记录")),
        ArtifactSpec("ui-design", "UI设计.md", "UI设计",
                     ("界面目标", "用户核心任务", "信息架构", "页面清单", "设计系统落地", "交互规则", "响应式与兼容", "可访问性", "验证清单")),
        ArtifactSpec("detail-design", "详细设计.md", "详细设计",
                     ("设计范围", "业务逻辑", "异常处理", "安全设计", "可观测性与配置", "发布与回滚")),
        ArtifactSpec("api-design", "API设计.md", "详细设计",
                     ("接口清单", "接口详细设计")),
        ArtifactSpec("database-design", "数据库设计.md", "详细设计",
                     ("表变更清单", "详细变更")),
        ArtifactSpec("tasks", "任务清单.md", "计划",
                     ("任务", "执行记录", "回归范围确认", "完成标准")),
        ArtifactSpec("test-plan", "测试方案.md", "计划",
                     ("测试接缝", "用例清单", "边界与异常覆盖", "环境与数据", "回归范围", "通过标准")),
    ),
    (
        ArtifactGroup("需求分析", ("requirements-analysis",), (), False),
        ArtifactGroup("概要设计", ("outline-design",), ("需求分析",), "optional"),
        ArtifactGroup("业务流程设计", ("business-flow",), ("概要设计",), False),
        ArtifactGroup("UI设计", ("ui-design",), ("概要设计",), True),
        ArtifactGroup("详细设计", ("detail-design", "api-design", "database-design"),
                      ("业务流程设计",), True),
        ArtifactGroup("计划", ("tasks", "test-plan"), ("详细设计",), "optional"),
    ),
)
