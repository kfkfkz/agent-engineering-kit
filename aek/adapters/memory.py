"""Safe source expansion and deterministic no-zvec metadata fallback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from aek.core.context.memory import CandidateRef, CandidateSet


TYPE_BY_PREFIX = (
    ("docs/memory/pitfalls/", "pitfall"),
    ("docs/memory/decisions/", "decision"),
    ("docs/memory/playbooks/", "playbook"),
    ("docs/02-业务域地图/", "domain"),
    ("docs/03-SDD/", "sdd"),
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
        if current.is_symlink():
            raise StaleCandidate("memory path contains a symlink")
    try:
        candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise StaleCandidate("memory path escaped repository") from exc
    return candidate


def _read_source(repo: Path, rel: str) -> bytes:
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
        remaining = status.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise StaleCandidate("memory source changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_candidate(repo: Path, ref: CandidateRef, generation: str) -> str:
    if not isinstance(ref, CandidateRef) or ref.generation != generation:
        raise StaleCandidate("candidate generation mismatch")
    if generation != "fallback":
        pointer = repo / ".repo-memory-kit/zvec/current.json"
        try:
            raw = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StaleCandidate("memory generation pointer unavailable") from exc
        if not isinstance(raw, dict) or raw.get("generation") != generation:
            raise StaleCandidate("memory index generation changed")
    try:
        content = _read_source(repo, ref.path)
    except (OSError, UnicodeError) as exc:
        raise StaleCandidate("memory source unavailable") from exc
    if hashlib.sha256(content).hexdigest() != ref.content_digest:
        raise StaleCandidate("memory source digest changed")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StaleCandidate("memory source is not UTF-8") from exc


def _matches_filters(rel: str, content: bytes, types: tuple[str, ...],
                     status: str | None, module: str | None) -> bool:
    if not (types or status or module):
        return True
    text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, body = parse_frontmatter(text)
    return ((not types or derive_type(rel, frontmatter, body) in types)
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


def fallback_candidates(repo: Path, query: str, *, limit: int, types: tuple[str, ...] = (),
                        status: str | None = None, module: str | None = None) -> CandidateSet:
    """No semantic index: search only bounded metadata, never return bodies."""
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or limit < 0:
        raise ValueError("fallback needs a non-empty query and non-negative limit")
    if limit == 0:
        return CandidateSet("fallback", (), False, "fallback")
    tokens = tuple(token.casefold() for token in query.split() if token)
    rows: list[CandidateRef] = []
    root = repo / "docs/memory"
    if not root.is_dir():
        return CandidateSet("fallback", (), False, "fallback")
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(repo).as_posix()
        try:
            content = _read_source(repo, rel)
            if not _matches_filters(rel, content, types, status, module):
                continue
            head = content[:4096].decode("utf-8", errors="strict")
        except (OSError, UnicodeError, StaleCandidate):
            continue
        metadata = "\n".join(
            line for line in head.splitlines()[:30]
            if line.startswith(("# ", "title:", "summary:", "module:", "anchors:")))
        haystack = (rel + "\n" + metadata).casefold()
        hits = sum(token in haystack for token in tokens)
        if hits == 0:
            continue
        anchor = next((line[2:].strip() for line in metadata.splitlines()
                       if line.startswith("# ")), path.stem)
        rows.append(CandidateRef(
            path=rel, score=hits / len(tokens), generation="fallback",
            content_digest=hashlib.sha256(content).hexdigest(), anchor=anchor[:200],
        ))
    rows.sort(key=lambda item: (-item.score, item.path))
    return CandidateSet("fallback", tuple(rows[:limit]),
                        len(rows) > limit, "fallback")


def search_candidates(repo: Path, query: str, *, limit: int, types: tuple[str, ...] = (),
                      status: str | None = None, module: str | None = None) -> CandidateSet:
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
    try:
        import zvec
        from zvec import Query
        from zvec.extension.multi_vector_reranker import RrfReRanker
        from zvec.model.param.query import Fts

        pointer = repo / ".repo-memory-kit/zvec/current.json"
        raw = json.loads(pointer.read_text(encoding="utf-8"))
        generation = raw.get("generation") if isinstance(raw, dict) else None
        if not isinstance(generation, str):
            raise ValueError("memory generation is absent")
        directory = repo / ".repo-memory-kit/zvec/generations" / generation
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
            if not isinstance(path, str) or not path.startswith("docs/memory/"):
                continue
            content = _read_source(repo, path)
            if not _matches_filters(path, content, types, status, module):
                continue
            anchor = result.fields.get("title") or Path(path).stem
            rows.append(CandidateRef(
                path=path, score=max(float(result.score), 0.0),
                generation=generation,
                content_digest=hashlib.sha256(content).hexdigest(),
                anchor=str(anchor)[:200]))
            if len(rows) > limit:
                break
        return CandidateSet(generation, tuple(rows[:limit]),
                            len(rows) > limit or len(results) >= fetch_limit, "zvec")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        return fallback_candidates(repo, query, limit=limit, types=types, status=status, module=module)
