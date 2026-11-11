#!/usr/bin/env python3
"""Current declared consumption, never an approval inferred from prose."""
import unittest

from aek.adapters.review_markdown import parse_review_documents
from aek.application.document_material_dependency import consumption_identity, consumed_review_context


PROVIDER = """<!-- aek-review-boundary: declared-id-v1 -->
# 概要
## 公共约束
保持审计记录。
### F1：查询
返回查询结果。
#### 错误
错误须明确。
### F2：导出
返回导出文件。
"""
CONSUMER = """<!-- aek-review-boundary: declared-id-v1 -->
# 详细
## 实现
### D1：查询实现
对应 F1，保留校验。
"""


class ConsumptionTests(unittest.TestCase):
    def identity(self, provider=PROVIDER, consumer=CONSUMER):
        current = parse_review_documents("详细设计", {"详细设计.md": consumer}, "a" * 64)
        upstream = parse_review_documents("概要设计", {"概要设计.md": provider}, "a" * 64)
        return consumption_identity(current, {"概要设计": upstream})

    def test_unconsumed_owner_change_keeps_content_identity_but_retains_raw_integrity(self):
        first = self.identity()
        changed = self.identity(PROVIDER.replace("返回导出文件。", "返回两个导出文件。"))
        self.assertEqual(first["status"], "SCOPED")
        self.assertEqual(first["content_digest"], changed["content_digest"])
        self.assertNotEqual(first["source_hashes"], changed["source_hashes"])
        selected = [row["heading"] for row in first["consumed_sections"]]
        self.assertTrue(any("F1：查询 / 错误" in heading for heading in selected))
        self.assertFalse(any("F2" in heading for heading in selected))

    def test_consumed_or_global_change_alters_identity(self):
        original = self.identity()["content_digest"]
        for before, after in (("返回查询结果。", "隐藏查询结果。"),
                              ("错误须明确。", "错误可以忽略。"),
                              ("保持审计记录。", "删除审计记录。")):
            with self.subTest(before=before):
                self.assertNotEqual(original, self.identity(PROVIDER.replace(before, after))["content_digest"])

    def test_unknown_edges_missing_marker_and_owner_renames_cannot_reuse_scope(self):
        original = self.identity()["content_digest"]
        for provider in (PROVIDER.replace("F1：查询", "F3：查询"),
                         PROVIDER.replace("返回查询结果。", "调用 F99。"),
                         PROVIDER.replace("<!-- aek-review-boundary: declared-id-v1 -->", ""),
                         PROVIDER + "### F1：重复\n冲突。\n"):
            with self.subTest(provider=provider):
                result = self.identity(provider)
                self.assertEqual(result["status"], "FULL")
                self.assertIsNone(result["content_digest"])
        renamed = self.identity(PROVIDER.replace("F2：导出", "F2：导出结果"))
        self.assertNotEqual(original, renamed["content_digest"])

    def test_actual_selected_text_and_global_context_are_preserved(self):
        result = self.identity()
        payload = "".join(row["text"] for row in result["consumed_sections"])
        self.assertIn("返回查询结果。", payload)
        self.assertIn("保持审计记录。", payload)
        self.assertNotIn("返回导出文件。", payload)
        self.assertNotIn("approved", result)

    def test_review_payload_contains_current_consumed_text_without_unrelated_full_source(self):
        current = parse_review_documents("详细设计", {"详细设计.md": CONSUMER}, "a" * 64)
        provider = parse_review_documents("概要设计", {"概要设计.md": PROVIDER}, "a" * 64)
        proof, context = consumed_review_context(current, {"概要设计": provider}, {
            "upstream/概要设计/概要设计.md": PROVIDER, "security/context": "必须鉴权。"})
        self.assertEqual(proof["status"], "SCOPED")
        self.assertNotIn("upstream/概要设计/概要设计.md", context)
        self.assertIn("必须鉴权。", context.values())
        self.assertIn("返回查询结果。", "".join(context.values()))
        self.assertNotIn("返回导出文件。", "".join(context.values()))
        self.assertLess(sum(len(text.encode()) for text in context.values()), len(PROVIDER.encode()) + len("必须鉴权。".encode()))


if __name__ == "__main__":
    unittest.main()
