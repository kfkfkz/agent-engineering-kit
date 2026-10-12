#!/usr/bin/env python3
"""Observable v2 review contracts; v1 stays the default compatibility path."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aek.adapters.review_markdown import parse_review_documents
from aek.application import review


REQUIREMENTS = """# 需求分析：查询
## 背景
已有查询需要明确错误返回。
## 目标
1. 查询返回结果或明确错误。
## 非目标（边界）
- 不新增存储。
## 场景与验收依据
### 场景 1：查询
- **前置**：已有合法输入。
- **操作**：提交查询。
- **结果**：返回结果或明确错误。
## 验收项
- [ ] A1：查询结果与错误可区分。（场景 1）
## 非功能要求
- 兼容性：旧查询仍可调用。
## 待确认事项
无阻塞问题。
## 冻结记录
- 冻结状态：未冻结
"""

OUTLINE = """<!-- aek-review-boundary: declared-id-v1 -->
# 概要设计：查询
## 设计目标
覆盖 A1 的查询及错误返回。
## 现状分析
现有查询入口可复用。
## 总体方案
查询输入经校验后返回结果。
### 主流程
1. 读取输入并返回查询结果。
### 异常流程
输入非法时返回明确错误。
## 功能点设计
### F1：查询
- 覆盖验收项：A1
- 输入 → 处理 → 输出：查询输入、执行查询、返回结果。
- 异常行为：返回明确错误。
## 方案取舍
复用既有查询能力，不引入新依赖。
## 数据与接口影响
已有查询入口，无新增存储。
## 风险
需保留现有错误响应兼容性。
"""


class ReviewV2Tests(unittest.TestCase):
    def test_full_reviewer_locator_is_current_existing_and_covers_all_known_owners(self):
        documents = {"概要设计.md": "<!-- aek-review-boundary: declared-id-v1 -->\n"
            "# 概要设计\n## 功能点设计\n### F1：查询\n查询。\n### F2：消费者\n关联 F1。\n"}
        material = parse_review_documents("概要设计", documents, "a" * 64)
        request = review.full_review_request_v2(material.snapshot,
            documents=material.document_map(), section_texts=material.section_map())
        sections = [item for item in material.snapshot.sections if item.defines_ids]
        fingerprint = "f" * 64
        receipt = json.loads(review.reviewer_receipt(request))
        receipt["issue_locations"] = [{"fingerprint": fingerprint, "document": item.document,
            "heading": item.heading, "section_digest": item.digest} for item in sections]
        sensor = json.dumps({"reviewer": "synthetic-independent", "stage": request.stage,
                            "doc_hashes": dict(request.doc_hashes), "issues": []})
        arguments = dict(current_snapshot=material.snapshot,
                         known_issue_sections={fingerprint: tuple(item.key for item in sections)})
        checked = review.validate_review_result(request, sensor, json.dumps(receipt), **arguments)
        self.assertEqual(checked.state, "VALID", checked.reason)
        for invalid in (receipt["issue_locations"][:1],
                        [{**row, "section_digest": "0" * 64} for row in receipt["issue_locations"]],
                        [{**row, "fingerprint": "9" * 64} for row in receipt["issue_locations"]]):
            bad = {**receipt, "issue_locations": invalid}
            self.assertEqual(review.validate_review_result(request, sensor, json.dumps(bad),
                **arguments).state, "INVALID")

    def test_boundary_proof_binds_actual_upstream_bodies_not_only_their_names(self):
        material = parse_review_documents("概要设计", {"概要设计.md": OUTLINE}, "a" * 64)
        requests = [review.full_review_request_v2(material.snapshot,
            documents=material.document_map(), section_texts=material.section_map(),
            supporting_context={"upstream/需求分析/需求分析.md": text})
            for text in (REQUIREMENTS, REQUIREMENTS.replace("旧查询仍可调用", "旧查询必须保持错误码"))]
        self.assertNotEqual(requests[0].scope_proof["boundary_evidence_digest"],
                            requests[1].scope_proof["boundary_evidence_digest"])
        self.assertNotEqual(requests[0].input_digest, requests[1].input_digest)

    def test_new_design_templates_declare_scope_without_marking_old_documents(self):
        with tempfile.TemporaryDirectory(prefix="aek-review-v2-template-") as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            (repo / ".repo-memory-kit").mkdir()
            index = repo / "docs" / "01-需求" / "README.md"
            index.parent.mkdir(parents=True)
            shutil.copytree(ROOT / "templates" / "requirements", index.parent / "_模板")
            index.write_text("| 编号 | 需求名称 | 状态 |\n| --- | --- | --- |\n", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
            result = subprocess.run([sys.executable, str(ROOT / "doc-gate"), "init",
                "001-查询", "--repo", str(repo)], capture_output=True, text=True,
                encoding="utf-8", env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            directories = list((repo / "docs" / "03-SDD").iterdir())
            self.assertEqual(len(directories), 1)
            for name in ("概要设计.md", "详细设计.md", "API设计.md"):
                text = (directories[0] / name).read_text(encoding="utf-8")
                self.assertEqual(text.count("<!-- aek-review-boundary: declared-id-v1 -->"), 1)
                self.assertIn("全局", text)
                self.assertIn("不代表", text)

    def test_fenced_owner_example_cannot_hide_changed_global_constraints(self):
        documents = {"详细设计.md": "<!-- aek-review-boundary: declared-id-v1 -->\n"
            "# 详细设计\n\n## 全局约束\n\n所有查询都需要鉴权。\n"
            "```markdown\n### D9：仅为示例\n```\n"
            "\n### D1：查询\n\n当前处理。\n"}
        previous = parse_review_documents("详细设计", documents, "e" * 64)
        first = review.full_review_request_v2(previous.snapshot,
            documents=previous.document_map(), section_texts=previous.section_map())
        baseline = json.dumps({"schema_version": 2, "snapshot": asdict(previous.snapshot),
            "accepted_full_request_digest": first.request_digest,
            "accepted_receipt_digest": "1" * 64, "accepted_sensor_digest": "2" * 64,
            "dependency_fingerprint": first.dependency_fingerprint,
            "boundary_evidence_digest": first.scope_proof["boundary_evidence_digest"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        current = parse_review_documents("详细设计", {
            name: text.replace("所有查询都需要鉴权", "仅管理查询需要鉴权")
            for name, text in documents.items()}, "e" * 64)
        request = review.plan_review_v2(current.snapshot, baseline=baseline,
            documents=current.document_map(), section_texts=current.section_map())
        self.assertEqual(request.mode, "full")
        self.assertIn("GLOBAL_CONSTRAINT_CHANGED", request.scope_proof["reason_codes"])

    def test_unresolved_owner_without_new_edit_still_reviews_its_consumers(self):
        documents = {"概要设计.md": "<!-- aek-review-boundary: declared-id-v1 -->\n"
            "# 概要设计\n\n## 功能点设计\n\n### F1：查询\n\n查询规则。\n"
            "\n### F2：调用方\n\n关联 F1 的查询规则。\n"}
        material = parse_review_documents("概要设计", documents, "f" * 64)
        first = review.full_review_request_v2(material.snapshot,
            documents=material.document_map(), section_texts=material.section_map())
        baseline = json.dumps({"schema_version": 2, "snapshot": asdict(material.snapshot),
            "accepted_full_request_digest": first.request_digest,
            "accepted_receipt_digest": "1" * 64, "accepted_sensor_digest": "2" * 64,
            "dependency_fingerprint": first.dependency_fingerprint,
            "boundary_evidence_digest": first.scope_proof["boundary_evidence_digest"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = review.plan_review_v2(material.snapshot, baseline=baseline,
            documents=material.document_map(), section_texts=material.section_map(),
            unresolved_sections=(("概要设计.md", "概要设计 / 功能点设计 / F1：查询"),))
        self.assertEqual(request.mode, "incremental")
        self.assertEqual(set(request.selected_sections), {
            ("概要设计.md", "概要设计 / 功能点设计 / F1：查询"),
            ("概要设计.md", "概要设计 / 功能点设计 / F2：调用方"),
        })
        self.assertIn("UNRESOLVED_ISSUE", request.scope_proof["reason_codes"])
        reasons = {(row["document"], row["heading"]): row["reason_codes"]
                   for row in request.scope_proof["target_reasons"]}
        self.assertIn("DEPENDENT_CONTRACT", reasons[
            ("概要设计.md", "概要设计 / 功能点设计 / F2：调用方")])

    def test_global_change_and_unknown_format_cannot_use_scoped_input(self):
        marker = "<!-- aek-review-boundary: declared-id-v1 -->\n"
        documents = {"详细设计.md": marker + "# 详细设计\n\n## 全局约束\n\n安全限制不变。\n\n### D1：查询\n\n当前处理。\n"}
        previous = parse_review_documents("详细设计", documents, "e" * 64)
        first = review.full_review_request_v2(previous.snapshot,
            documents=previous.document_map(), section_texts=previous.section_map())
        baseline = json.dumps({"schema_version": 2, "snapshot": asdict(previous.snapshot),
            "accepted_full_request_digest": first.request_digest,
            "accepted_receipt_digest": "1" * 64, "accepted_sensor_digest": "2" * 64,
            "dependency_fingerprint": first.dependency_fingerprint,
            "boundary_evidence_digest": first.scope_proof["boundary_evidence_digest"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for text, expected in [
            (documents["详细设计.md"].replace("安全限制不变。", "安全限制改变。"), "GLOBAL_CONSTRAINT_CHANGED"),
            (documents["详细设计.md"].replace(marker, ""), "UNKNOWN_SCOPE"),
        ]:
            with self.subTest(reason=expected):
                current = parse_review_documents("详细设计", {"详细设计.md": text}, "e" * 64)
                request = review.plan_review_v2(current.snapshot, baseline=baseline,
                    documents=current.document_map(), section_texts=current.section_map())
                self.assertEqual(request.mode, "full")
                self.assertEqual(dict(request.full_documents), {"详细设计.md": text})
                self.assertIn(expected, request.scope_proof["reason_codes"])

    def test_scoped_v2_local_change_excludes_unrelated_owner_but_delivers_globals(self):
        marker = "<!-- aek-review-boundary: declared-id-v1 -->\n"
        before_documents = {
            "详细设计.md": marker + "# 详细设计\n\n## 业务逻辑\n\n### D1：查询\n\n#### 输入\n\n修改前。\n",
            "API设计.md": marker + "# API设计\n\n## 接口详细设计\n\n### D2：独立接口\n\n独立契约。\n",
        }
        before = parse_review_documents("详细设计", before_documents, "d" * 64)
        first = review.full_review_request_v2(before.snapshot,
            documents=before.document_map(), section_texts=before.section_map())
        # Synthetic accepted-material metadata, never an approval fixture.
        baseline = json.dumps({
            "schema_version": 2, "snapshot": asdict(before.snapshot),
            "accepted_full_request_digest": first.request_digest,
            "accepted_receipt_digest": "1" * 64, "accepted_sensor_digest": "2" * 64,
            "dependency_fingerprint": first.dependency_fingerprint,
            "boundary_evidence_digest": first.scope_proof["boundary_evidence_digest"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        after_documents = {**before_documents,
                           "详细设计.md": before_documents["详细设计.md"].replace("修改前。", "修改后。")}
        current = parse_review_documents("详细设计", after_documents, "d" * 64)
        factory = getattr(review, "plan_review_v2", None)
        self.assertTrue(callable(factory), "v2 scoped-review entry is missing")
        request = factory(current.snapshot, baseline=baseline,
                          documents=current.document_map(), section_texts=current.section_map())
        self.assertEqual(request.mode, "incremental")
        self.assertEqual(set(request.selected_sections), {
            ("详细设计.md", "详细设计 / 业务逻辑 / D1：查询"),
            ("详细设计.md", "详细设计 / 业务逻辑 / D1：查询 / 输入"),
        })
        self.assertNotIn("独立契约。", "".join(row[2] for row in request.section_documents))
        self.assertNotIn("独立契约。", "".join(row[2] for row in request.reference_sections))
        self.assertIn(("API设计.md", "API设计"), {row[:2] for row in request.reference_sections})
        self.assertIn("修改后。", "".join(row[2] for row in request.section_documents))
        self.assertEqual(review.load_versioned_review_request(review.review_request_bytes(request)), request)

    def test_explicit_v2_full_input_preserves_documents_and_scope_binding(self):
        documents = {
            "详细设计.md": "# 详细设计\n\n## 业务逻辑\n\n### D1：查询\n\n输入与查询结果。\n",
            "API设计.md": "# API设计\n\n## 接口详细设计\n\n### D2：查询接口\n\n错误与幂等。\n",
        }
        material = parse_review_documents("详细设计", documents, "a" * 64)
        factory = getattr(review, "full_review_request_v2", None)
        self.assertTrue(callable(factory), "explicit v2 full-review entry is missing")
        request = factory(
            material.snapshot, documents=material.document_map(),
            section_texts=material.section_map(),
            supporting_context={"upstream/需求分析.md": "已冻结的查询与错误约束。"},
        )
        self.assertEqual(request.schema_version, 2)
        self.assertEqual(request.mode, "full")
        self.assertEqual(dict(request.full_documents), documents)
        self.assertEqual(request.section_documents, ())
        self.assertEqual(request.reference_sections, ())
        self.assertEqual(dict(request.supporting_context),
                         {"upstream/需求分析.md": "已冻结的查询与错误约束。"})
        self.assertEqual(set(request.selected_sections), {
            ("详细设计.md", "详细设计"),
            ("详细设计.md", "详细设计 / 业务逻辑"),
            ("详细设计.md", "详细设计 / 业务逻辑 / D1：查询"),
            ("API设计.md", "API设计"),
            ("API设计.md", "API设计 / 接口详细设计"),
            ("API设计.md", "API设计 / 接口详细设计 / D2：查询接口"),
        })
        proof = request.scope_proof
        self.assertIsNone(proof["baseline_snapshot_digest"])
        self.assertIn("FULL_BASELINE_REQUIRED", proof["reason_codes"])
        self.assertEqual(proof["current_snapshot_digest"], hashlib.sha256(
            review.review_snapshot_bytes(material.snapshot)).hexdigest())
        self.assertEqual({(row["document"], row["heading"])
                          for row in proof["target_reasons"]},
                         set(request.selected_sections))

    def test_v2_canonical_round_trip_has_versioned_receipt_but_v1_rejects_it(self):
        documents = {"详细设计.md": "# 详细设计\n\n### D1：查询\n\n当前正文。\n"}
        material = parse_review_documents("详细设计", documents, "b" * 64)
        request = review.full_review_request_v2(
            material.snapshot, documents=material.document_map(),
            section_texts=material.section_map())
        raw = review.review_request_bytes(request)
        self.assertEqual(review.load_versioned_review_request(raw), request)
        with self.assertRaises(ValueError):
            review.load_review_request(raw)
        receipt = json.loads(review.reviewer_receipt(request))
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["input_digest"], request.input_digest)
        self.assertEqual(receipt["scope_proof_digest"], hashlib.sha256(json.dumps(
            request.scope_proof, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest())
        self.assertEqual(receipt["issue_locations"], [])

    def test_wrong_json_field_type_is_a_controlled_rejection(self):
        documents = {"详细设计.md": "# 详细设计\n\n### D1：查询\n\n当前正文。\n"}
        material = parse_review_documents("详细设计", documents, "c" * 64)
        request = review.full_review_request_v2(
            material.snapshot, documents=material.document_map(),
            section_texts=material.section_map())
        value = json.loads(review.review_request_bytes(request))
        value["mode"] = []
        with self.assertRaises(ValueError):
            review.load_versioned_review_request(json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8"))

    def test_cli_explicit_v2_emits_full_input_while_default_stays_v1(self):
        with tempfile.TemporaryDirectory(prefix="aek-review-v2-") as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            sdd = repo / "docs" / "03-SDD" / "001-review"
            sdd.mkdir(parents=True)
            (sdd / "需求分析.md").write_text(REQUIREMENTS, encoding="utf-8")
            command = [sys.executable, str(ROOT / "doc-gate"), "review-input",
                       str(sdd), "--stage", "需求分析", "--json"]
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
            legacy = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", env=env, check=False)
            self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
            self.assertEqual(json.loads(legacy.stdout)["request"]["schema_version"], 1)
            explicit = subprocess.run(command + ["--schema-version", "2"],
                                      capture_output=True, text=True,
                                      encoding="utf-8", env=env, check=False)
            self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
            output = json.loads(explicit.stdout)
            self.assertEqual(output["request"]["schema_version"], 2)
            self.assertEqual(output["request"]["mode"], "full")
            self.assertEqual(dict(output["request"]["full_documents"]),
                             {"需求分析.md": REQUIREMENTS})
            persisted = (sdd / "reviews" / "需求分析.review-request.json").read_bytes()
            self.assertEqual(review.load_versioned_review_request(persisted).schema_version, 2)
            self.assertEqual(output["receipt"]["schema_version"], 2)

    def test_full_v2_sensor_with_issue_keeps_material_baseline_without_approval(self):
        self._material_baseline_case("概要设计.md / F1", "incremental")

    def test_partly_unknown_issue_location_must_not_drop_its_unknown_region(self):
        self._material_baseline_case("概要设计.md / F1 / F99", "full")

    def test_upstream_change_requires_new_freeze_and_full_review_even_without_target_edit(self):
        self._material_baseline_case("概要设计.md / F1", "incremental", change_upstream=True)

    def _material_baseline_case(self, location, expected_mode, *, change_upstream=False):
        with tempfile.TemporaryDirectory(prefix="aek-review-v2-baseline-") as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            sdd = repo / "docs" / "03-SDD" / "001-review"
            sdd.mkdir(parents=True)
            (sdd / "需求分析.md").write_text(
                REQUIREMENTS.replace("冻结状态：未冻结", "冻结状态：已冻结"), encoding="utf-8")
            (sdd / "概要设计.md").write_text(OUTLINE, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}

            def invoke(*arguments):
                return subprocess.run([sys.executable, str(ROOT / "doc-gate"),
                                       arguments[0], str(sdd), *arguments[1:]],
                                      capture_output=True, text=True,
                                      encoding="utf-8", env=env, check=False)

            frozen = invoke("freeze", "--stage", "需求分析", "--by", "synthetic-k", "--json")
            self.assertEqual(frozen.returncode, 0, frozen.stdout + frozen.stderr)
            prepared = invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
            self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
            output = json.loads(prepared.stdout)
            reviews = sdd / "reviews"
            self.assertFalse((reviews / "概要设计.review-baseline.json").exists())
            issue = {"id": "REV-1", "severity": "major", "category": "verifiability",
                     "location": location, "problem": "重复查询处理未说明",
                     "evidence": "F1 没有重复请求的处理约束", "required_change": "说明F1重复查询处理",
                     "affected_ids": ["F1", "A1"], "upstream": False}
            (reviews / "概要设计.issues.json").write_text(json.dumps({
                "reviewer": "synthetic-independent", "stage": "概要设计",
                "doc_hashes": output["receipt"]["doc_hashes"], "issues": [issue],
            }, ensure_ascii=False), encoding="utf-8")
            (reviews / "概要设计.review-receipt.json").write_text(
                json.dumps(output["receipt"], ensure_ascii=False), encoding="utf-8")
            gated = invoke("gate", "--stage", "概要设计", "--json")
            self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
            result = json.loads(gated.stdout)
            self.assertEqual(result["status"], "NEEDS_REVISION")
            self.assertEqual(result["review"]["major"], 1)
            baseline = json.loads((reviews / "概要设计.review-baseline.json").read_text(encoding="utf-8"))
            self.assertEqual(baseline["schema_version"], 2)
            self.assertEqual(baseline["accepted_full_request_digest"], output["request"]["request_digest"])
            self.assertEqual(baseline["snapshot"]["doc_hashes"], output["request"]["doc_hashes"])
            lifecycle = json.loads((reviews / "概要设计.issue-lifecycle.json").read_text(encoding="utf-8"))
            self.assertEqual([row["state"] for row in lifecycle["issues"]], ["OPEN"])
            (sdd / "概要设计.md").write_text(
                OUTLINE.replace("执行查询", "校验后执行查询"), encoding="utf-8")
            repeated = invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            second = json.loads(repeated.stdout)["request"]
            self.assertEqual(second["mode"], expected_mode)
            if expected_mode == "full":
                self.assertIsNone(second["scope_proof"]["issue_bindings"][0]["section_keys"])
                self.assertIn("UNKNOWN_ISSUE_LOCATION", second["scope_proof"]["reason_codes"])
                self.assertTrue(second["full_documents"])
                return
            self.assertEqual(second["full_documents"], [])
            self.assertEqual(second["selected_sections"], [[
                "概要设计.md", "概要设计：查询 / 功能点设计 / F1：查询"]])
            self.assertEqual(second["scope_proof"]["issue_bindings"][0]["fingerprint"],
                             lifecycle["issues"][0]["fingerprint"])
            self.assertIn("UNRESOLVED_ISSUE", second["scope_proof"]["reason_codes"])
            if change_upstream:
                path = sdd / "需求分析.md"
                path.write_text(path.read_text(encoding="utf-8").replace(
                    "旧查询仍可调用", "旧查询必须保持错误码"), encoding="utf-8")
                refused = invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn("硬校验未过", refused.stdout)
                self.assertNotIn("Traceback", refused.stdout + refused.stderr)
                self.assertEqual(invoke("freeze", "--stage", "需求分析", "--by", "synthetic-k", "--json").returncode, 0)
                refreshed = invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
                self.assertEqual(refreshed.returncode, 0, refreshed.stdout + refreshed.stderr)
                third = json.loads(refreshed.stdout)["request"]
                self.assertEqual(third["mode"], "full")
                self.assertIn("DEPENDENCY_CHANGED", third["scope_proof"]["reason_codes"])
                self.assertNotEqual(third["dependency_fingerprint"], second["dependency_fingerprint"])
                self.assertIn("旧查询必须保持错误码", dict(third["supporting_context"])[
                    "upstream/需求分析/需求分析.md"])


if __name__ == "__main__":
    unittest.main()
