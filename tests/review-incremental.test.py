#!/usr/bin/env python3
"""A reviewer gets only a proven closure, or the whole current stage."""
from __future__ import annotations

import unittest
from dataclasses import replace

from aek.core.review.incremental import (
    ReviewSnapshot, SectionSnapshot, plan_incremental_review,
)


def section(doc: str, heading: str, digest: str, defines=(), uses=()):
    return SectionSnapshot(doc, heading, digest * 64, tuple(defines), tuple(uses))


def snapshot(*, changed=False, headings=False, unknown=False, fingerprint="f"):
    sections = (
        section("需求分析.md", "目标", "2" if changed else "1", ("A1",)),
        section("概要设计.md", "实现" if headings else "方案", "3", ("F1",), ("A1",)),
        section("详细设计.md", "落地", "4", ("D1",), ("F1",)),
        section("详细设计.md", "独立", "5", ("D2",)),
    )
    return ReviewSnapshot(
        stage="详细设计", doc_hashes=(("需求分析.md", ("d" if changed else "a") * 64),
                                  ("概要设计.md", "b" * 64),
                                  ("详细设计.md", "c" * 64)),
        sections=sections, dependency_fingerprint=fingerprint * 64,
        parse_complete=not unknown, unknown_edges=unknown,
    )


class IncrementalReviewTests(unittest.TestCase):
    def test_changed_section_closes_over_reverse_dependencies(self):
        previous = snapshot()
        current = snapshot(changed=True)
        plan = plan_incremental_review(
            previous, current, prior_review_was_full=True,
            reviewed_doc_hashes=previous.doc_hashes)
        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.selected_sections, (
            ("需求分析.md", "目标"), ("概要设计.md", "方案"),
            ("详细设计.md", "落地")))
        self.assertNotIn(("详细设计.md", "独立"), plan.selected_sections)
        self.assertEqual(plan.doc_hashes, current.doc_hashes)
        self.assertEqual(len(plan.receipt_digest), 64)

    def test_incomplete_proof_falls_back_to_full(self):
        old, current = snapshot(), snapshot(changed=True)
        cases = (
            dict(prior_review_was_full=False, reviewed_doc_hashes=old.doc_hashes),
            dict(prior_review_was_full=True, reviewed_doc_hashes=()),
        )
        for kwargs in cases:
            self.assertEqual(plan_incremental_review(old, current, **kwargs).mode,
                             "full")
        for unsafe in (snapshot(changed=True, headings=True),
                       snapshot(changed=True, unknown=True),
                       snapshot(changed=True, fingerprint="e")):
            self.assertEqual(plan_incremental_review(
                old, unsafe, prior_review_was_full=True,
                reviewed_doc_hashes=old.doc_hashes).mode, "full")
        broken = replace(current, sections=current.sections + (
            section("详细设计.md", "bad", "6", ("D3",), ("D99",)),))
        self.assertEqual(plan_incremental_review(
            old, broken, prior_review_was_full=True,
            reviewed_doc_hashes=old.doc_hashes).mode, "full")

    def test_removed_reference_keeps_previous_dependency_in_closure(self):
        old = snapshot()
        changed = list(old.sections)
        changed[1] = section("概要设计.md", "方案", "6", ("F1",), ())
        current = replace(old, doc_hashes=(old.doc_hashes[0],
                                           ("概要设计.md", "e" * 64),
                                           old.doc_hashes[2]),
                          sections=tuple(changed))
        plan = plan_incremental_review(
            old, current, prior_review_was_full=True,
            reviewed_doc_hashes=old.doc_hashes)
        self.assertEqual(plan.mode, "incremental")
        self.assertIn(("需求分析.md", "目标"), plan.selected_sections)
        self.assertIn(("详细设计.md", "落地"), plan.selected_sections)

    def test_same_group_companion_document_is_included(self):
        old, current = snapshot(), snapshot(changed=True)
        plan = plan_incremental_review(
            old, current, prior_review_was_full=True,
            reviewed_doc_hashes=old.doc_hashes,
            same_group_documents=("需求分析.md", "详细设计.md"))
        self.assertEqual(plan.mode, "incremental")
        self.assertIn(("详细设计.md", "独立"), plan.selected_sections)


if __name__ == "__main__":
    unittest.main()
