#!/usr/bin/env python3
"""Capsule cache is reusable, discardable and source-bound."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aek.adapters.capsule import CapsuleStore
from aek.application.context_capsule import get_or_rebuild_capsule
from aek.core.context.capsule import CapsuleClaim, CapsuleSource


class CapsuleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = CapsuleStore(Path(temp.name) / "capsules")

    def build(self, digest: str):
        source = CapsuleSource("docs/memory/orders/export.md", digest)
        return get_or_rebuild_capsule(
            self.store, subject_digest="b" * 64, stage="execution",
            profile_version=1, sources=(source,),
            claims=(CapsuleClaim("memory-1", "导出权限约束", (source.path,)),))

    def test_reuses_exact_entry_and_rebuilds_changed_source(self) -> None:
        first = self.build("a" * 64)
        self.assertFalse(first.reused)
        self.assertTrue(self.build("a" * 64).reused)
        changed = self.build("c" * 64)
        self.assertFalse(changed.reused)
        self.assertNotEqual(first.capsule.capsule_id, changed.capsule.capsule_id)

    def test_corrupt_expected_entry_is_rebuilt(self) -> None:
        first = self.build("a" * 64)
        path = self.store.root / f"{first.capsule.capsule_id}.json"
        path.write_text("{}", encoding="utf-8")
        rebuilt = self.build("a" * 64)
        self.assertFalse(rebuilt.reused)
        self.assertEqual(rebuilt.capsule, first.capsule)
        self.assertEqual(self.store.load(first.capsule.capsule_id), first.capsule)

    def test_current_source_set_and_claims_are_required_for_reuse(self) -> None:
        source = CapsuleSource("docs/memory/orders/export.md", "a" * 64)
        extra = CapsuleSource("docs/memory/orders/security.md", "c" * 64)
        claim = CapsuleClaim("memory-1", "导出权限约束", (source.path,))
        options = dict(subject_digest="b" * 64, stage="execution",
                       profile_version=1, sources=(source,), claims=(claim,))
        first = get_or_rebuild_capsule(self.store, **options)
        changed_set = get_or_rebuild_capsule(
            self.store, **{**options, "sources": (source, extra)})
        changed_claim = get_or_rebuild_capsule(
            self.store, **{**options, "claims": (
                CapsuleClaim("memory-1", "导出需要重新核验权限", (source.path,)),)})
        for rebuilt in (changed_set, changed_claim):
            self.assertFalse(rebuilt.reused)
            self.assertNotEqual(rebuilt.capsule.capsule_id,
                                first.capsule.capsule_id)
        self.assertTrue(get_or_rebuild_capsule(self.store, **options).reused)
        with self.assertRaises(ValueError):
            get_or_rebuild_capsule(self.store, **{**options, "sources": ()})

    def test_symlinked_cache_container_is_rejected(self) -> None:
        outside = self.store.root.parent / "outside"
        outside.mkdir()
        try:
            self.store.root.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable on this host")
        with self.assertRaises(ValueError):
            self.build("a" * 64)


if __name__ == "__main__":
    unittest.main()
