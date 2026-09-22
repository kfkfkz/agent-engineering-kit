#!/usr/bin/env python3
"""Markdown review snapshots reject ambiguous structure."""
from __future__ import annotations

import unittest

from aek.adapters.review_markdown import parse_review_documents
from aek.application.review import plan_review


class ReviewMarkdownTests(unittest.TestCase):
    def test_real_markdown_change_produces_bound_reverse_closure(self) -> None:
        before = {
            "需求分析.md": "# 需求\n## 验收\n- [ ] A1：旧目标\n",
            "概要设计.md": "# 概要\n## 功能\n### F1：方案\n覆盖 A1\n",
        }
        after = dict(before)
        after["需求分析.md"] = "# 需求\n## 验收\n- [ ] A1：新目标\n"
        old = parse_review_documents("概要设计", before, "f" * 64)
        current = parse_review_documents("概要设计", after, "f" * 64)
        request = plan_review(
            old.snapshot, current.snapshot, prior_review_was_full=True,
            reviewed_doc_hashes=old.snapshot.doc_hashes,
            documents=current.document_map(), section_texts=current.section_map())
        self.assertEqual(request.mode, "incremental")
        selected = set(request.selected_sections)
        self.assertIn(("需求分析.md", "需求 / 验收"), selected)
        self.assertIn(("概要设计.md", "概要 / 功能 / F1：方案"), selected)

    def test_fenced_heading_is_content_not_a_section_boundary(self) -> None:
        material = parse_review_documents(
            "详细设计", {"详细设计.md": (
                "# 设计\n## 正文\n```md\n## 伪标题\nD99\n```\n")}, "f" * 64)
        headings = {item.heading for item in material.snapshot.sections}
        self.assertNotIn("设计 / 伪标题", headings)
        self.assertTrue(material.snapshot.parse_complete)

    def test_unclosed_fence_and_duplicate_definition_force_full(self) -> None:
        old = parse_review_documents(
            "详细设计", {"详细设计.md": "# 设计\n## A\n### D1：一\n"},
            "f" * 64)
        ambiguous = parse_review_documents(
            "详细设计", {"详细设计.md": (
                "# 设计\n## A\n### D1：一\n## B\n### D1：二\n```\n")},
            "f" * 64)
        self.assertFalse(ambiguous.snapshot.parse_complete)
        request = plan_review(
            old.snapshot, ambiguous.snapshot, prior_review_was_full=True,
            reviewed_doc_hashes=old.snapshot.doc_hashes,
            documents=ambiguous.document_map(),
            section_texts=ambiguous.section_map())
        self.assertEqual(request.mode, "full")


if __name__ == "__main__":
    unittest.main()
