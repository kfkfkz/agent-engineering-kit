"""Safe source expansion and deterministic no-zvec metadata fallback."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from aek.core.context.memory import CandidateRef, CandidateSet


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
        fd = os.open(path, os.O_RDONLY)
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


def fallback_candidates(repo: Path, query: str, *, limit: int) -> CandidateSet:
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


def search_candidates(repo: Path, query: str, *, limit: int) -> CandidateSet:
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
        results = collection.query(
            queries=[
                Query(field_name="meta", fts=Fts(match_string=query)),
                Query(field_name="content", fts=Fts(match_string=query)),
            ], topk=max(limit * 4, 20),
            reranker=RrfReRanker(rank_constant=60))
        rows: list[CandidateRef] = []
        for result in results:
            path = result.fields.get("path")
            if not isinstance(path, str) or not path.startswith("docs/memory/"):
                continue
            content = _read_source(repo, path)
            anchor = result.fields.get("title") or Path(path).stem
            rows.append(CandidateRef(
                path=path, score=max(float(result.score), 0.0),
                generation=generation,
                content_digest=hashlib.sha256(content).hexdigest(),
                anchor=str(anchor)[:200]))
            if len(rows) > limit:
                break
        return CandidateSet(generation, tuple(rows[:limit]),
                            len(rows) > limit, "zvec")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        return fallback_candidates(repo, query, limit=limit)
