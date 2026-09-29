"""Pure graph-first query routing and code-index freshness barriers."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import re


class QueryKind(str, Enum):
    STRUCTURAL = "STRUCTURAL"
    TEXTUAL = "TEXTUAL"


@dataclass(frozen=True)
class CodeIdentity:
    schema_version: int
    repo_id: str
    worktree_id: str
    vcs_kind: str
    revision: str
    tracked_diff_digest: str
    untracked_code_digest: str
    path_set_digest: str
    identity_digest: str


def build_code_identity(
    *, repo_id: str, worktree_id: str, vcs_kind: str, revision: str,
    tracked_diff_digest: str, untracked_code_digest: str,
    path_set_digest: str,
) -> CodeIdentity:
    if (not all(isinstance(item, str) and item.strip()
                for item in (repo_id, worktree_id, revision))
            or vcs_kind not in {"git", "svn", "none"}
            or any(re.fullmatch(r"[0-9a-f]{64}", item) is None
                   for item in (tracked_diff_digest, untracked_code_digest,
                                path_set_digest))):
        raise ValueError("code identity fields are invalid")
    payload = {
        "schema_version": 1, "repo_id": repo_id,
        "worktree_id": worktree_id, "vcs_kind": vcs_kind,
        "revision": revision, "tracked_diff_digest": tracked_diff_digest,
        "untracked_code_digest": untracked_code_digest,
        "path_set_digest": path_set_digest,
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return CodeIdentity(1, repo_id, worktree_id, vcs_kind, revision,
                        tracked_diff_digest, untracked_code_digest,
                        path_set_digest, digest)


@dataclass(frozen=True)
class CapabilityState:
    configured: bool
    visible: bool
    indexed: bool
    fresh: bool

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (
                self.configured, self.visible, self.indexed, self.fresh)):
            raise ValueError("codebase capability states must be boolean observations")


@dataclass(frozen=True)
class DirtyState:
    code_identity_digest: str
    dirty_epoch: int
    active_batch_id: str
    changed_paths: tuple[str, ...]
    reasons: tuple[str, ...]
    refresh_started_epoch: int

    def __post_init__(self) -> None:
        if (not isinstance(self.code_identity_digest, str)
                or not self.code_identity_digest
                or type(self.dirty_epoch) is not int or self.dirty_epoch < 0
                or not isinstance(self.active_batch_id, str)
                or not isinstance(self.changed_paths, tuple)
                or not isinstance(self.reasons, tuple)
                or type(self.refresh_started_epoch) is not int
                or self.refresh_started_epoch > self.dirty_epoch
                or any(_safe_path(path) != path for path in self.changed_paths)
                or any(not isinstance(item, str) or not item
                       for item in self.reasons)):
            raise ValueError("dirty state is invalid")

    @classmethod
    def clean(cls, identity_digest: str) -> "DirtyState":
        if not isinstance(identity_digest, str) or not identity_digest:
            raise ValueError("code identity digest is required")
        return cls(identity_digest, 0, "", (), (), -1)

    def with_refresh_started(self) -> "DirtyState":
        if self.dirty_epoch < 1:
            raise ValueError("clean codebase does not need refresh")
        return replace(self, refresh_started_epoch=self.dirty_epoch)


@dataclass(frozen=True)
class CodebaseBarrierPlan:
    required_action: str
    reason_code: str
    required_tools: tuple[str, ...]
    required_paths: tuple[str, ...]
    dirty_epoch: int
    confidence: str
    primary_tool: str


@dataclass(frozen=True)
class CodebaseObservation:
    canonical_root: str
    expected_root: str
    generation: str
    identity_digest: str
    covered_paths: tuple[str, ...]
    metadata_match_paths: tuple[str, ...]


_STRUCTURAL = re.compile(
    r"\b(callers?|callees?|calls?|symbols?|functions?|classes?|methods?|"
    r"dependencies|dependents?|impact|architecture|implements?|inherits?|"
    r"调用|被谁调用|符号|函数|类|方法|依赖|影响|架构)\b", re.I)
_TEXTUAL = re.compile(
    r"\b(exact|literal|error text|message|config|yaml|toml|json|dockerfile|"
    r"字面量|错误文案|配置|非代码)\b", re.I)
_NON_CODE_SUFFIXES = {".md", ".txt", ".rst", ".json", ".yaml", ".yml",
                      ".toml", ".ini", ".cfg", ".csv", ".tsv"}


def classify_query(query: str) -> QueryKind:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("codebase query is required")
    if _TEXTUAL.search(query):
        return QueryKind.TEXTUAL
    if _STRUCTURAL.search(query):
        return QueryKind.STRUCTURAL
    # Unknown intent is not silently treated as an exhaustive graph query.
    return QueryKind.TEXTUAL


def _safe_path(path: str) -> str:
    if (not isinstance(path, str) or not path or "\\" in path
            or path.startswith("/") or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("codebase evidence path is unsafe")
    return path


def _is_code_path(path: str) -> bool:
    normalized = _safe_path(path)
    if normalized.startswith("docs/"):
        return False
    name = normalized.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    suffix = "" if dot <= 0 else name[dot:].casefold()
    return suffix not in _NON_CODE_SUFFIXES


def mark_dirty(
    state: DirtyState, *, batch_id: str, identity_digest: str,
    paths: tuple[str, ...], reason: str,
) -> DirtyState:
    if not isinstance(state, DirtyState):
        raise ValueError("dirty state is required")
    if (not isinstance(batch_id, str) or not batch_id.strip()
            or not isinstance(identity_digest, str) or not identity_digest
            or not isinstance(reason, str) or not reason.strip()
            or not isinstance(paths, tuple)):
        raise ValueError("dirty change batch is invalid")
    code_paths = tuple(sorted({_safe_path(path) for path in paths
                               if _is_code_path(path)}))
    if not code_paths:
        return state
    same_batch = batch_id == state.active_batch_id
    merged_paths = tuple(sorted(set(state.changed_paths) | set(code_paths))) \
        if same_batch else code_paths
    merged_reasons = tuple(sorted(set(state.reasons) | {reason})) \
        if same_batch else (reason,)
    return DirtyState(
        identity_digest,
        state.dirty_epoch if same_batch else state.dirty_epoch + 1,
        batch_id, merged_paths, merged_reasons,
        state.refresh_started_epoch if same_batch else -1)


def _plan(
    action: str, reason: str, state: DirtyState, paths: tuple[str, ...], *,
    confidence: str, primary_tool: str, tools: tuple[str, ...],
) -> CodebaseBarrierPlan:
    return CodebaseBarrierPlan(
        action, reason, tools, paths, state.dirty_epoch, confidence, primary_tool)


def plan_barrier(
    kind: QueryKind, capabilities: CapabilityState, state: DirtyState, *,
    candidate_paths: tuple[str, ...],
) -> CodebaseBarrierPlan:
    if not isinstance(kind, QueryKind) or not isinstance(capabilities, CapabilityState) \
            or not isinstance(state, DirtyState):
        raise ValueError("codebase barrier inputs are invalid")
    candidates = tuple(sorted({_safe_path(path) for path in candidate_paths}))
    paths = tuple(sorted(set(state.changed_paths) | set(candidates)))
    if kind == QueryKind.TEXTUAL:
        return _plan("BOUNDED_SOURCE", "TEXTUAL_QUERY", state, paths,
                     confidence="high", primary_tool="rg", tools=("rg",))
    if not capabilities.visible:
        return _plan("BOUNDED_SOURCE", "MCP_NOT_VISIBLE", state, paths,
                     confidence="degraded", primary_tool="source",
                     tools=("bounded_source",))
    if not capabilities.indexed:
        return _plan("REFRESH_ONCE", "PROJECT_NOT_INDEXED", state, paths,
                     confidence="unknown", primary_tool="index_repository",
                     tools=("list_projects", "index_repository", "index_status"))
    if state.dirty_epoch > 0 or paths or not capabilities.fresh:
        return _plan("CHECK_STATUS", "FRESHNESS_PROOF_REQUIRED", state, paths,
                     confidence="unknown", primary_tool="index_status",
                     tools=("index_status", "check_index_coverage"))
    return _plan("USE_GRAPH", "GRAPH_PROVEN_FRESH", state, (),
                 confidence="high", primary_tool="search_graph",
                 tools=("search_graph", "trace_path", "get_code_snippet",
                        "check_index_coverage"))


def verify_observation(
    plan: CodebaseBarrierPlan, state: DirtyState,
    observation: CodebaseObservation,
) -> CodebaseBarrierPlan:
    if plan.required_action != "CHECK_STATUS":
        raise ValueError("only a status plan accepts a freshness observation")
    required = set(plan.required_paths)
    complete = (
        observation.canonical_root == observation.expected_root
        and bool(observation.generation)
        and observation.identity_digest == state.code_identity_digest
        and set(observation.covered_paths) == required
        and set(observation.metadata_match_paths) == required
    )
    if complete:
        return _plan("USE_GRAPH", "AUTO_REFRESH_CURRENT", state,
                     plan.required_paths, confidence="high",
                     primary_tool="search_graph",
                     tools=("search_graph", "trace_path", "get_code_snippet",
                            "check_index_coverage"))
    if state.refresh_started_epoch != state.dirty_epoch:
        return _plan("REFRESH_ONCE", "INDEX_STALE_OR_UNPROVEN", state,
                     plan.required_paths, confidence="unknown",
                     primary_tool="index_repository",
                     tools=("index_repository", "index_status",
                            "check_index_coverage"))
    return _plan("BOUNDED_SOURCE", "REFRESH_DID_NOT_PROVE_FRESH", state,
                 plan.required_paths, confidence="degraded",
                 primary_tool="source", tools=("bounded_source",))
