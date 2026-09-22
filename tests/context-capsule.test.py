#!/usr/bin/env python3
"""Capsules are source-bound summaries, never independent facts."""
from __future__ import annotations

import unittest
from dataclasses import replace

from aek.core.context.capsule import (
    CapsuleClaim, CapsuleSource, build_capsule, capsule_is_current,
)


class CapsuleTests(unittest.TestCase):
    def test_determinism_and_stale_rebuild(self) -> None:
        source = CapsuleSource("docs/memory/orders/export.md", "a" * 64)
        claim = CapsuleClaim("c1", "导出沿用列表权限", (source.path,))
        first = build_capsule("b" * 64, "execution", 1, (source,), (claim,))
        self.assertEqual(first, build_capsule("b" * 64, "execution", 1,
                                              (source,), (claim,)))
        self.assertTrue(capsule_is_current(first, (source,)))
        self.assertFalse(capsule_is_current(replace(first, capsule_id="0" * 64),
                                            (source,)))
        changed = CapsuleSource(source.path, "c" * 64)
        self.assertFalse(capsule_is_current(first, (changed,)))
        rebuilt = build_capsule("b" * 64, "execution", 1, (changed,), (claim,))
        self.assertNotEqual(first.capsule_id, rebuilt.capsule_id)

    def test_unbound_claim_is_rejected(self) -> None:
        source = CapsuleSource("docs/memory/orders/export.md", "a" * 64)
        with self.assertRaises(ValueError):
            build_capsule("b" * 64, "execution", 1, (source,),
                          (CapsuleClaim("c1", "unbound", ("other.md",)),))
        with self.assertRaises(ValueError):
            build_capsule("b" * 64, "execution", 1, (),
                          (CapsuleClaim("c1", "unbound", ()),))


if __name__ == "__main__":
    unittest.main()
