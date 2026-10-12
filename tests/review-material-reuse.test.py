#!/usr/bin/env python3
"""Operation-local Markdown material reuse keeps exact source identities."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import unittest
from dataclasses import FrozenInstanceError, asdict
from unittest.mock import patch

from aek.adapters import review_markdown
from aek.application import review


class ReviewMaterialReuseTests(unittest.TestCase):
    def test_review_input_preparation_reuses_stage_and_upstream_material(self) -> None:
        root = Path(__file__).resolve().parents[1]
        fixture = runpy.run_path(str(root / "tests" / "review-convergence.test.py"))
        test_case = fixture["ReviewConvergenceTests"]()
        test_case.setUp()
        self.addCleanup(test_case.doCleanups)
        prepared = test_case.prepare()
        self.assertEqual(prepared["request"]["schema_version"], 2)

        commands = runpy.run_path(str(root / "doc-gate"))
        parser = review_markdown.ReviewMaterialParser()
        with patch.object(review_markdown, "parse_review_documents",
                          wraps=review_markdown.parse_review_documents) as parse:
            request, material = commands["_build_review_request"](
                test_case.sdd, "概要设计", schema_version=2,
                material_parser=parser)
            commands["_v2_progress"](
                test_case.sdd, "概要设计", request, material,
                material_parser=parser)
        self.assertEqual(parse.call_count, 2)  # current stage + frozen upstream

    def test_unsupported_global_headings_cannot_hide_security_change_in_owner(self) -> None:
        for heading in (" ## 全局安全\n", "  ## 全局安全\n",
                        "   ## 全局安全\n", "全局安全\n----------\n",
                        "全局安全\n==========\n"):
            with self.subTest(heading=heading):
                parser = review_markdown.ReviewMaterialParser()
                document = (
                    "<!-- aek-review-boundary: declared-id-v1 -->\n"
                    "# 详细设计\n\n## 业务逻辑\n\n### D1：查询\n\n查询规则。\n\n"
                    + heading + "\n所有操作必须鉴权。\n\n"
                    "### D2：导出\n\n独立导出也受安全规则约束。\n")
                before = parser.parse("详细设计", {"详细设计.md": document}, "a" * 64)
                first = review.full_review_request_v2(
                    before.snapshot, documents=before.document_map(),
                    section_texts=before.section_map())
                baseline = json.dumps({
                    "schema_version": 2, "snapshot": asdict(before.snapshot),
                    "accepted_full_request_digest": first.request_digest,
                    "accepted_receipt_digest": "1" * 64,
                    "accepted_sensor_digest": "2" * 64,
                    "dependency_fingerprint": first.dependency_fingerprint,
                    "boundary_evidence_digest": first.scope_proof["boundary_evidence_digest"],
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                changed = document.replace("所有操作必须鉴权。", "仅查询需要鉴权。")
                current = parser.parse("详细设计", {"详细设计.md": changed}, "a" * 64)
                request = review.plan_review_v2(
                    current.snapshot, baseline=baseline,
                    documents=current.document_map(), section_texts=current.section_map())
                self.assertEqual(request.mode, "full")
                self.assertEqual(dict(request.full_documents), {"详细设计.md": changed})

    def test_same_sources_share_one_immutable_material_in_an_operation(self) -> None:
        parser = review_markdown.ReviewMaterialParser()
        documents = {
            "需求分析.md": "# 需求\n## 验收\n- [ ] A1：导出保持权限\n",
            "概要设计.md": "# 概要\n## F1：导出\n覆盖 A1\n",
        }
        first = parser.parse("概要设计", documents, "a" * 64)
        repeated = parser.parse(
            "概要设计", dict(reversed(tuple(documents.items()))), "a" * 64)
        self.assertIs(repeated, first)
        self.assertEqual(first.document_map(), documents)
        returned_map = repeated.document_map()
        returned_map["需求分析.md"] = "caller mutation"
        self.assertEqual(first.document_map(), documents)
        with self.assertRaises(FrozenInstanceError):
            first.documents = ()

    def test_exact_source_bytes_and_membership_invalidate_material(self) -> None:
        parser = review_markdown.ReviewMaterialParser()
        documents = {"详细设计.md": "# 设计\n## D1：权限\n保留权限\n"}
        first = parser.parse("详细设计", documents, "a" * 64)
        variants = (
            {"详细设计.md": "# 设计\n## D1：权限\n保留权限 \n"},
            {"详细设计.md": "# 设计\r\n## D1：权限\r\n保留权限\r\n"},
            {"重命名设计.md": documents["详细设计.md"]},
            {**documents, "API设计.md": "# 接口\n## A\n"},
        )
        for sources in variants:
            with self.subTest(sources=sources):
                changed = parser.parse("详细设计", sources, "a" * 64)
                self.assertIsNot(changed, first)
                self.assertNotEqual(changed.snapshot.doc_hashes,
                                    first.snapshot.doc_hashes)
                self.assertEqual(changed.document_map(), sources)
                self.assertIs(parser.parse("详细设计", sources, "a" * 64),
                              changed)
        self.assertIs(parser.parse("详细设计", documents, "a" * 64), first)

    def test_stage_and_dependency_are_part_of_material_identity(self) -> None:
        parser = review_markdown.ReviewMaterialParser()
        documents = {"设计.md": "# 设计\n## D1：权限\nA1\n"}
        first = parser.parse("详细设计", documents, "a" * 64)
        new_stage = parser.parse("概要设计", documents, "a" * 64)
        new_dependency = parser.parse("详细设计", documents, "b" * 64)
        self.assertIsNot(new_stage, first)
        self.assertEqual(new_stage.snapshot.stage, "概要设计")
        self.assertIsNot(new_dependency, first)
        self.assertEqual(new_dependency.snapshot.dependency_fingerprint,
                         "b" * 64)

    def test_each_operation_owns_its_material_and_failed_parse_is_not_cached(self) -> None:
        first_operation = review_markdown.ReviewMaterialParser()
        documents = {"设计.md": "# 设计\n## D1：权限\nA1\n"}
        first = first_operation.parse("详细设计", documents, "a" * 64)
        second = review_markdown.ReviewMaterialParser().parse(
            "详细设计", documents, "a" * 64)
        self.assertIsNot(second, first)
        self.assertEqual(second, first)
        for invalid in ({}, {"设计.md": None}, {1: "# 设计\n"}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError,
                                            "invalid Markdown review source"):
                    first_operation.parse("详细设计", invalid, "a" * 64)
        self.assertIs(first_operation.parse("详细设计", documents, "a" * 64),
                      first)

    def test_caller_source_mutation_is_checked_without_changing_prior_material(self) -> None:
        parser = review_markdown.ReviewMaterialParser()
        documents = {"设计.md": "# 设计\n## D1：权限\n原文\n"}
        first = parser.parse("详细设计", documents, "a" * 64)
        documents["设计.md"] = "# 设计\n## D1：权限\n新文\n"
        changed = parser.parse("详细设计", documents, "a" * 64)
        self.assertIsNot(changed, first)
        self.assertEqual(first.document_map()["设计.md"],
                         "# 设计\n## D1：权限\n原文\n")
        self.assertEqual(changed.document_map(), documents)


if __name__ == "__main__":
    unittest.main()
