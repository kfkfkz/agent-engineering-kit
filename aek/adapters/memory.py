"""Safe source expansion and deterministic no-zvec metadata fallback."""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import stat
from dataclasses import replace
from pathlib import Path

from aek.adapters.memory_scope import MemoryScope, resolve_memory_scope
from aek.core.context.memory import CandidateRef, CandidateSet, is_memory_markdown


TYPE_BY_PREFIX = (
    ("docs/memory/pitfalls/", "pitfall"),
    ("docs/memory/decisions/", "decision"),
    ("docs/memory/playbooks/", "playbook"),
    ("docs/02-业务域地图/", "domain"),
    ("docs/03-SDD/", "sdd"),
    ("docs/需求/", "sdd"),
    ("docs/delivery-receipts/", "receipt"),
)
DOMAIN_STATUS = {"已确认": "confirmed", "草稿": "draft", "待确认": "draft", "已废弃": "deprecated"}


def parse_frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm, key = {}, None
    for ln in text[4:end].splitlines():
        # 支持缩进列表项(anchors: 的 "- class:xxx" 行)
        if re.match(r"^\s+-\s+", ln) and key:
            if not isinstance(fm.get(key), list):
                fm[key] = []
            fm[key].append(ln.strip()[2:].strip())
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if m:
            key = m.group(1)
            val = m.group(2).split(" #")[0].strip()
            fm[key] = [] if val == "[]" else val
    return fm, text[end + 5:]


def derive_type(rel, fm, body):
    # v4：类型以 frontmatter type 为准（模块分目录后类型不再由路径承载）；
    # 存量类型目录前缀仅作回退。domain/sdd/receipt 等文档类型仍按路径前缀判定。
    t = str(fm.get("type") or "").strip()
    if t in ("pitfall", "decision", "playbook"):
        return t
    for prefix, typ in TYPE_BY_PREFIX:
        if rel.startswith(prefix):
            return typ
    return "doc"


def derive_status(rel, fm, body):
    s = str(fm.get("status") or "").strip()
    if s:
        return s
    if rel.startswith("docs/02-业务域地图/"):
        m = re.search(r"^>\s*状态[：:]\s*([^\s（(，,]+)", body, re.M)
        if m:
            return DOMAIN_STATUS.get(m.group(1), m.group(1))
    return ""


class StaleCandidate(ValueError):
    """A candidate no longer identifies the current source/generation."""


def _safe_path(repo: Path, rel: str) -> Path:
    root = repo.resolve()
    candidate = root / rel
    current = root
    for part in rel.split("/"):
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0)
                                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise StaleCandidate("memory path contains a symlink")
    try:
        candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise StaleCandidate("memory path escaped repository") from exc
    return candidate


def _read_source(repo: Path, rel: str, *, prefix_bytes: int | None = None) -> bytes:
    if not is_memory_markdown(rel):
        raise StaleCandidate("memory source is outside the Markdown corpus")
    path = _safe_path(repo, rel)
    if (os.open in os.supports_dir_fd and getattr(os, "O_NOFOLLOW", 0)
            and getattr(os, "O_DIRECTORY", 0)):
        # POSIX: pin every component. A final-component O_NOFOLLOW alone does
        # not protect against a concurrent replacement of a parent directory.
        parent_fd = os.open(repo.resolve(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            parts = rel.split("/")
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    else:
        # Windows compatible mode: reparse-point precheck, not a hostile
        # concurrent-process TOCTOU guarantee. Never describe it as strict.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or status.st_size > 1_000_000:
            raise StaleCandidate("memory source is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = status.st_size if prefix_bytes is None else min(status.st_size, prefix_bytes)
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise StaleCandidate("memory source changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _scoped_generation(repo: Path, scope: MemoryScope) -> str:
    raw = json.loads((repo / ".repo-memory-kit/zvec/current.json").read_text(encoding="utf-8"))
    generation = raw.get("generation") if isinstance(raw, dict) else None
    if (not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", generation)
            or raw.get("memory_scope_digest") not in ((scope.digest,) if scope.git else (None, scope.digest))):
        raise StaleCandidate("memory index is unavailable for the current scope")
    return generation


def _source_metadata(text: str):
    return parse_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n"))


def read_candidate(repo: Path, ref: CandidateRef, generation: str, *, scope: MemoryScope | None = None) -> str:
    if not isinstance(ref, CandidateRef) or ref.generation != generation:
        raise StaleCandidate("candidate generation mismatch")
    scope = scope or resolve_memory_scope(repo)
    source_root = repo
    if ref.origin_ref is not None:
        source_root = dict(scope.parent_worktrees).get(ref.origin_ref)
        if source_root is None:
            raise StaleCandidate("memory parent source is no longer available")
    if generation != "fallback":
        try:
            current = _scoped_generation(repo, scope)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StaleCandidate("memory generation pointer unavailable") from exc
        if current != generation:
            raise StaleCandidate("memory index generation changed")
    try:
        content = _read_source(source_root, ref.path)
    except (OSError, UnicodeError) as exc:
        raise StaleCandidate("memory source unavailable") from exc
    if hashlib.sha256(content).hexdigest() != ref.content_digest:
        raise StaleCandidate("memory source digest changed")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StaleCandidate("memory source is not UTF-8") from exc
    if not scope.allows(ref.path, _source_metadata(text)[0], ref.origin_ref):
        raise StaleCandidate("memory source does not apply to the current scope")
    return text


def _matches_filters(rel: str, content: bytes, types: tuple[str, ...],
                     status: str | None, module: str | None, scope: MemoryScope,
                     origin_ref: str | None = None) -> bool:
    frontmatter, body = _source_metadata(content.decode("utf-8"))
    return (scope.allows(rel, frontmatter, origin_ref)
            and (not types or derive_type(rel, frontmatter, body) in types)
            and (not status or derive_status(rel, frontmatter, body) == status)
            and (not module or module in str(frontmatter.get("module") or "")))


def zvec_filter(types: tuple[str, ...], status: str | None) -> str | None:
    # Only simple values enter the index expression. All filters, including
    # values outside this grammar, are checked against the current source.
    clauses = []
    if types and all(re.fullmatch(r"[\w.-]+", value) for value in types):
        values = ", ".join(f'"{value}"' for value in types)
        clauses.append(f"type in ({values})")
    if status and re.fullmatch(r"[\w.-]+", status):
        clauses.append(f'status = "{status}"')
    return " and ".join(clauses) or None


def _metadata_rows(repo: Path, query: str, types: tuple[str, ...], status: str | None,
                   module: str | None, scope: MemoryScope, origin_ref: str | None = None) -> list[CandidateRef]:
    tokens = tuple(token.casefold() for token in query.split() if token)
    rows: list[CandidateRef] = []
    root = repo / ("docs/memory" if origin_ref else "docs")
    if not root.is_dir():
        return rows
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(repo).as_posix()
        if not is_memory_markdown(rel):
            continue
        try:
            prefix = _read_source(repo, rel, prefix_bytes=4096)
            head = codecs.getincrementaldecoder("utf-8")().decode(prefix, final=False)
        except (OSError, UnicodeError, StaleCandidate):
            continue
        metadata = "\n".join(
            line for line in head.splitlines()[:30]
            if line.startswith(("# ", "title:", "summary:", "module:", "anchors:")))
        haystack = (rel + "\n" + metadata).casefold()
        hits = sum(token in haystack for token in tokens)
        if hits == 0:
            continue
        try:
            content = _read_source(repo, rel)
            if not content.startswith(prefix) or not _matches_filters(
                    rel, content, types, status, module, scope, origin_ref):
                continue
        except (OSError, UnicodeError, StaleCandidate):
            continue
        anchor = next((line[2:].strip() for line in metadata.splitlines()
                       if line.startswith("# ")), path.stem)
        rows.append(CandidateRef(
            path=rel, score=hits / len(tokens), generation="fallback",
            content_digest=hashlib.sha256(content).hexdigest(), anchor=anchor[:200], origin_ref=origin_ref,
        ))
    rows.sort(key=lambda item: (-item.score, item.path))
    return rows


def _with_parents(rows: list[CandidateRef], query: str, limit: int, types: tuple[str, ...],
                  status: str | None, module: str | None, scope: MemoryScope,
                  generation: str) -> list[CandidateRef]:
    seen = {row.path for row in rows}
    # Give current-checkout matches priority. Only query ancestors for capacity
    # the local corpus has not already filled.
    score_factor = min((row.score for row in rows), default=1.0) / 2
    for origin_ref, parent in scope.parent_worktrees:
        if len(rows) > limit:
            break
        for ref in _metadata_rows(parent, query, types, status, module, scope, origin_ref):
            if ref.path in seen:
                continue
            seen.add(ref.path)
            rows.append(replace(ref, generation=generation, score=ref.score * score_factor))
            if len(rows) > limit:
                break
        score_factor /= 2
    return rows


def fallback_candidates(repo: Path, query: str, *, limit: int, types: tuple[str, ...] = (),
                        status: str | None = None, module: str | None = None,
                        scope: MemoryScope | None = None) -> CandidateSet:
    """Search bounded metadata from the checkout and explicitly shared ancestors."""
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or limit < 0:
        raise ValueError("fallback needs a non-empty query and non-negative limit")
    if limit == 0:
        return CandidateSet("fallback", (), False, "fallback")
    scope = scope or resolve_memory_scope(repo)
    rows = _metadata_rows(repo, query, types, status, module, scope)
    rows = _with_parents(rows, query, limit, types, status, module, scope, "fallback")
    return CandidateSet("fallback", tuple(rows[:limit]),
                        len(rows) > limit, "fallback")


def search_candidates(repo: Path, query: str, *, limit: int, types: tuple[str, ...] = (),
                      status: str | None = None, module: str | None = None,
                      scope: MemoryScope | None = None, open_generation=None) -> CandidateSet:
    """Return metadata-only candidates from the current index or fallback.

    The semantic index is an optimization.  A missing dependency/index or an
    unreadable generation falls back to bounded metadata, never to whole-body
    scanning/output.
    """
    if not isinstance(query, str) or not query.strip() or type(limit) is not int \
            or limit < 0:
        raise ValueError("candidate search needs a query and non-negative limit")
    if limit == 0:
        return CandidateSet("fallback", (), False, "fallback")
    scope = scope or resolve_memory_scope(repo)
    lock_fd = None
    try:
        import zvec
        from zvec import Query
        from zvec.extension.multi_vector_reranker import RrfReRanker
        from zvec.model.param.query import Fts

        generation = _scoped_generation(repo, scope)
        directory = repo / ".repo-memory-kit/zvec/generations" / generation
        if open_generation is not None:
            opened, lock_fd = open_generation(repo)
            if opened != directory:
                raise StaleCandidate("memory generation changed before lookup")
        collection = zvec.open(str(directory))
        fetch_limit = max(limit * 4, 20)
        results = collection.query(
            queries=[
                Query(field_name="meta", fts=Fts(match_string=query)),
                Query(field_name="content", fts=Fts(match_string=query)),
            ], topk=fetch_limit,
            reranker=RrfReRanker(rank_constant=60),
            filter=zvec_filter(types, status))
        rows: list[CandidateRef] = []
        for result in results:
            path = result.fields.get("path")
            if not is_memory_markdown(path):
                continue
            try:
                content = _read_source(repo, path)
                if not _matches_filters(path, content, types, status, module, scope):
                    continue
            except (OSError, UnicodeError, StaleCandidate):
                continue
            anchor = result.fields.get("title") or Path(path).stem
            rows.append(CandidateRef(
                path=path, score=max(float(result.score), 0.0),
                generation=generation,
                content_digest=hashlib.sha256(content).hexdigest(),
                anchor=str(anchor)[:200]))
            if len(rows) > limit:
                break
        rows = _with_parents(rows, query, limit, types, status, module, scope, generation)
        return CandidateSet(generation, tuple(rows[:limit]),
                            len(rows) > limit or len(results) >= fetch_limit, "zvec")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        return fallback_candidates(repo, query, limit=limit, types=types, status=status, module=module, scope=scope)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
