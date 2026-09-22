#!/usr/bin/env python3
"""Two-level memory: candidate metadata first, digest-checked expansion second."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aek.adapters.memory import StaleCandidate, fallback_candidates, read_candidate
from aek.core.context.memory import CandidateRef, CandidateSet, select_expansions


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.memory = self.repo / "docs/memory/orders"
        self.memory.mkdir(parents=True)
        self.doc = self.memory / "export.md"
        self.doc.write_text("# 订单导出\nsummary: 注意筛选权限\n具体规则", encoding="utf-8")

    def test_fallback_returns_metadata_and_expands_by_digest(self) -> None:
        self.assertEqual(fallback_candidates(self.repo, "订单", limit=0).candidates, ())
        candidates = fallback_candidates(self.repo, "订单 导出", limit=5)
        self.assertEqual(candidates.source, "fallback")
        self.assertEqual(len(candidates.candidates), 1)
        ref = candidates.candidates[0]
        self.assertEqual(ref.path, "docs/memory/orders/export.md")
        self.assertNotIn("具体规则", str(ref))
        self.assertEqual(
            read_candidate(self.repo, ref, "fallback"),
            self.doc.read_bytes().decode("utf-8"),
        )
        self.doc.write_text("# changed", encoding="utf-8")
        with self.assertRaises(StaleCandidate):
            read_candidate(self.repo, ref, "fallback")

    def test_generation_handshake_and_bound_selection(self) -> None:
        digest = hashlib.sha256(self.doc.read_bytes()).hexdigest()
        refs = (
            CandidateRef("docs/memory/orders/export.md", 0.9, "g1", digest, "订单导出"),
            CandidateRef("docs/memory/orders/other.md", 0.8, "g1", digest, "其他"),
        )
        batch = CandidateSet("g1", refs, True, "zvec")
        chosen = select_expansions(batch, limit=1, risk_required=True)
        self.assertEqual(tuple(x.path for x in chosen.selected), (refs[0].path,))
        self.assertTrue(chosen.needs_more_candidates)
        pointer = self.repo / ".repo-memory-kit/zvec/current.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({"generation": "g2"}), encoding="utf-8")
        with self.assertRaises(StaleCandidate):
            read_candidate(self.repo, refs[0], "g1")
        self.assertEqual(select_expansions(CandidateSet("g1", (), False, "zvec"),
                                           limit=2, risk_required=False).selected, ())

    def test_unsafe_path_and_symlink_are_rejected(self) -> None:
        digest = hashlib.sha256(self.doc.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            CandidateRef("../outside.md", 1.0, "fallback", digest, "x")
        linked = self.memory / "link.md"
        try:
            linked.symlink_to(self.doc)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        ref = CandidateRef("docs/memory/orders/link.md", 1.0, "fallback", digest, "x")
        with self.assertRaises(StaleCandidate):
            read_candidate(self.repo, ref, "fallback")


if __name__ == "__main__":
    unittest.main()
