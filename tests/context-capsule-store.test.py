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

    def test_symlinked_cache_container_is_rejected(self) -> None:
        outside = self.store.root.parent / "outside"
        outside.mkdir()
        self.store.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.build("a" * 64)


if __name__ == "__main__":
    unittest.main()
