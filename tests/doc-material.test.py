#!/usr/bin/env python3
"""Observable finite-format comparison rules, never an approval shortcut."""
from __future__ import annotations

import unittest

from aek.core.artifact.document_material import compare_material


class DocumentMaterialTests(unittest.TestCase):
    def test_ordinary_prose_line_endings_have_equal_content_but_distinct_raw_identity(self):
        result = compare_material("普通正文。\n后续说明。\n".encode(),
                                  "普通正文。\r\n后续说明。\r\n".encode())
        self.assertEqual(result.classification, "EQUIVALENT")
        self.assertEqual(result.rule_version, "markdown-format-v1")
        self.assertNotEqual(result.before_sha256, result.after_sha256)
        self.assertIsNotNone(result.content_sha256)

    def test_redundant_terminal_empty_lines_after_top_level_prose_are_equivalent(self):
        result = compare_material("普通正文。\n".encode(), "普通正文。\n\n\n".encode())
        self.assertEqual(result.classification, "EQUIVALENT")
        self.assertNotEqual(result.before_sha256, result.after_sha256)

    def test_setext_heading_is_protected_even_when_its_text_looks_like_prose(self):
        result = compare_material("标题\n====\n".encode(), "标题\r\n====\n".encode())
        self.assertNotEqual(result.classification, "EQUIVALENT")

    def test_unchanged_fenced_code_does_not_disable_separate_ordinary_prose(self):
        code = "# 设计\n\n```text\n代码内容\n```\n\n"
        result = compare_material((code + "普通正文。\n").encode(),
                                  (code + "普通正文。\r\n").encode())
        self.assertEqual(result.classification, "EQUIVALENT")

    def test_real_review_marker_and_separate_list_do_not_disable_ordinary_prose(self):
        prefix = "<!-- aek-review-boundary: declared-id-v1 -->\n# 设计\n\n- 原有条目\n  条目续行\n\n"
        result = compare_material((prefix + "普通正文。\n").encode(),
                                  (prefix + "普通正文。\r\n").encode())
        self.assertEqual(result.classification, "EQUIVALENT")

    def test_protected_regions_are_never_normalized_as_prose(self):
        pairs = (
            ("```text\n代码内容\n```\n", "```text\n代码内容\r\n```\n"),
            ("- 条目\n懒续行\n", "- 条目\n懒续行\r\n"),
            ("> 引用\n懒续行\n", "> 引用\n懒续行\r\n"),
            ("    缩进代码\n", "    缩进代码\r\n"),
            ("标题\n---\n", "标题\r\n---\n"),
            ("含有 `内联代码` 的正文。\n", "含有 `内联代码` 的正文。\r\n"),
            ("地址 https://example.invalid/a\n", "地址 https://example.invalid/a\r\n"),
            ("记录 A1 的数量为 3\n", "记录 A1 的数量为 3\r\n"),
            ("评审人：k\n", "评审人：k\r\n"),
            ("普通正文。  \n", "普通正文。  \r\n"),
            ("普通正文。\\\n", "普通正文。\\\r\n"),
            ("| 表头 |\n| --- |\n| 内容 |\n", "| 表头 |\n| --- |\n| 内容 |\r\n"),
            ("<div>\n内部正文\n</div>\n", "<div>\n内部正文\r\n</div>\n"),
        )
        for before, after in pairs:
            with self.subTest(before=before):
                result = compare_material(before.encode(), after.encode())
                self.assertIn(result.classification, ("REVIEW_REQUIRED", "UNKNOWN"))
                self.assertIsNone(result.content_sha256)

    def test_punctuation_words_numbers_and_unicode_changes_require_review(self):
        pairs = (
            ("允许访问\n", "允许访问。\n"),
            ("允许访问\n", "禁止访问\n"),
            ("最多一次\n", "最多两次\n"),
            ("范围内\n", "范围外\n"),
            ("普通正文。\n", "普通正文！\r\n"),
            ("café\n", "cafe\u0301\n"),
            ("Name\n", "name\n"),
            ("正文\n", "正文 \n"),
            ("普通正文。\n", "普通正文。"),
            ("第一段\n\n第二段\n", "第一段\n第二段\n"),
        )
        for before, after in pairs:
            with self.subTest(before=before, after=after):
                result = compare_material(before.encode(), after.encode())
                self.assertIn(result.classification, ("REVIEW_REQUIRED", "UNKNOWN"))

    def test_ambiguous_or_unclosed_structure_is_unknown_not_equivalent(self):
        for text in ("```text\n普通正文。\n", "`跨行\n普通正文。\n结束`\n",
                     "<div>\n普通正文。\n", "---\n普通正文。\n"):
            with self.subTest(text=text):
                result = compare_material(text.encode(), text.replace("普通正文。\n", "普通正文。\r\n").encode())
                self.assertEqual(result.classification, "UNKNOWN")

    def test_exact_identity_is_not_an_approval_and_unknown_rule_fails_closed(self):
        exact = compare_material(b"unchanged\n", b"unchanged\n")
        self.assertEqual(exact.classification, "EXACT")
        self.assertEqual(exact.before_sha256, exact.after_sha256)
        self.assertFalse(hasattr(exact, "approved_by"))
        unknown = compare_material(b"unchanged\n", b"unchanged\n", rule_version="future-v9")
        self.assertEqual((unknown.classification, unknown.reason_code), ("UNKNOWN", "UNKNOWN_RULE"))

    def test_invalid_transport_is_rejected_and_undecodable_content_is_unknown(self):
        for bad in (b"", bytearray(b"hello"), "hello", None, b"x" * (2 * 1024 * 1024 + 1)):
            with self.subTest(type=type(bad)):
                with self.assertRaises(ValueError):
                    compare_material(bad, b"hello\n")
        result = compare_material(b"\xff\n", b"\xff\r\n")
        self.assertEqual((result.classification, result.reason_code), ("UNKNOWN", "INVALID_UTF8"))
        for before, after in ((b"word\r", b"word\n"), (b"word\x00\n", b"word\x00\r\n")):
            with self.subTest(before=before):
                self.assertEqual(compare_material(before, after).classification, "UNKNOWN")

    def test_unchanged_inline_code_and_links_do_not_force_unrelated_prose_unknown(self):
        prefix = "使用 `example` 示例；链接见 [说明](https://example.invalid)。\n\n"
        result = compare_material((prefix + "普通正文。\n").encode(),
                                  (prefix + "普通正文。\r\n").encode())
        self.assertEqual(result.classification, "EQUIVALENT")

    def test_multiline_inline_spans_and_protected_terminal_containers_stay_protected(self):
        samples = ("[跨行\n普通正文。\n](target)\n", "*跨行\n普通正文。\n结束*\n",
                   "``跨行\n普通正文。\n结束``\n", "- 条目\n懒续行\n\n",
                   "```text\n代码内容\n```\n\n", "评审人：k\n\n")
        for before in samples:
            after = before.replace("普通正文。\n", "普通正文。\r\n") if "普通正文。" in before else before + "\n"
            with self.subTest(before=before):
                self.assertIn(compare_material(before.encode(), after.encode()).classification,
                              ("REVIEW_REQUIRED", "UNKNOWN"))

    def test_multiline_reference_link_title_cannot_expose_ordinary_prose(self):
        for quote in ("\"", "'"):
            for separator in (" ", "\n"):
                before = f"[ref]: /target{separator}{quote}title\n普通正文。\nend{quote}\n\n[ref]\n"
                after = before.replace("普通正文。\n", "普通正文。\r\n")
                with self.subTest(quote=quote, separator=separator):
                    result = compare_material(before.encode(), after.encode())
                    self.assertIn(result.classification, ("UNKNOWN", "REVIEW_REQUIRED"))

    def test_content_identity_is_stable_when_equivalent_material_is_read_again(self):
        before, after = "普通正文。\n".encode(), "普通正文。\r\n".encode()
        changed = compare_material(before, after)
        unchanged = compare_material(after, after)
        self.assertEqual(changed.classification, "EQUIVALENT")
        self.assertEqual(unchanged.classification, "EXACT")
        self.assertEqual(changed.content_sha256, unchanged.content_sha256)


if __name__ == "__main__":
    unittest.main()
