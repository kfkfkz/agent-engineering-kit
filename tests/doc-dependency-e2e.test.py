#!/usr/bin/env python3
"""Original gates preserve only previously approved unaffected consumers."""
import json
import hashlib
import contextlib
import io
import runpy
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DependencyGateTests(unittest.TestCase):
    def test_refresh_cannot_publish_a_new_revision_using_an_inner_dependency_epoch(self):
        from aek.application.document_material import DocumentMaterials
        from aek.adapters.plan_transaction import PlanStore
        fixture = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_real_planned_pipeline(declared_dependencies=True)
        directory = fixture.requirement
        provider = directory / "详细设计.md"
        approved = provider.read_bytes()
        changed = approved.replace("校验实际材料后保存。".encode(), "无需校验直接保存。".encode())
        provider.write_bytes(changed)
        namespace = runpy.run_path(str(ROOT / "doc-gate"))
        actual = DocumentMaterials.verify
        def inner_epoch(service, *args, **kwargs):
            provider.write_bytes(approved)
            try:
                return actual(service, *args, **kwargs)
            finally:
                provider.write_bytes(changed)
        with PlanStore(directory / "reviews").locked_view(directory) as view:
            state = namespace["_dynamic_state_from_snapshot"](view.snapshot())
            service = namespace["_document_materials"](directory, view, state)
            before = view.snapshot()["计划.plan-binding.json"]
            prior = namespace["load_binding_bytes"](before)
            hashes = {name: hashlib.sha256(view.read_source(name)).hexdigest() for name in service._gate("计划")[1]["doc_hashes"]}
            with mock.patch.object(DocumentMaterials, "verify", inner_epoch), self.assertRaises(ValueError):
                service.refresh("计划", expected_revision=prior.binding_digest, source_hashes=hashes)
            self.assertEqual(view.snapshot()["计划.plan-binding.json"], before)
        self.assertEqual(provider.read_bytes(), changed)

    def test_dependency_aba_cannot_validate_approved_bytes_between_unapproved_hashes(self):
        from aek.application.document_material import DocumentMaterials
        fixture = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_real_planned_pipeline(declared_dependencies=True)
        path = fixture.requirement / "详细设计.md"
        approved = path.read_bytes()
        changed = approved.replace("校验实际材料后保存。".encode(), "无需校验直接保存。".encode())
        path.write_bytes(changed)
        actual, calls = DocumentMaterials.capture_dependencies, 0
        def concurrent_aba(service, stage):
            nonlocal calls
            if stage != "计划":
                return actual(service, stage)
            calls += 1
            if calls % 2:
                result = actual(service, stage)
                path.write_bytes(approved)
                return result
            path.write_bytes(changed)
            return actual(service, stage)
        namespace = runpy.run_path(str(ROOT / "doc-gate"))
        output = io.StringIO()
        with mock.patch.object(DocumentMaterials, "capture_dependencies", concurrent_aba), contextlib.redirect_stdout(output):
            result = namespace["cmd_status"](fixture.requirement, True)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["计划"]["state"], "DRAFT")
        self.assertEqual(path.read_bytes(), changed)

    def test_material_only_new_approval_cannot_skip_a_draft_transitive_ancestor(self):
        module = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))
        fixture = module["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        directory = fixture.requirement
        marker = "<!-- aek-review-boundary: declared-id-v1 -->\n"
        fixture.document.write_bytes(marker.encode() + fixture.raw)
        outline = marker + module["OUTLINE"].replace("## 方案取舍", "### F2：独立导出\n覆盖验收项：A1\n独立导出材料。\n\n## 方案取舍")
        (directory / "概要设计.md").write_text(outline, encoding="utf-8")
        shell = (ROOT / "tests/doc-gate.test.sh").read_text(encoding="utf-8")
        flow = shell.split('cat > "$D/业务流程设计.md" <<\'EOF\'\n', 1)[1].split('\nEOF', 1)[0]
        flow = marker + "> 图表模式：Mermaid 降级\n> 降级原因：Node.js <18，archify doctor: DEGRADED\n" + flow
        (directory / "业务流程设计.md").write_text(flow, encoding="utf-8")
        fixture.freeze_material()
        fixture.cli("gate", str(directory), "--stage", "概要设计", "--schema-version", "2", "--json")
        fixture.cli("freeze", str(directory), "--stage", "业务流程设计", "--by", "fixture-maintainer", "--schema-version", "2", "--json")
        (directory / "概要设计.md").write_text(outline.replace("独立导出材料。", "独立导出两个材料。"), encoding="utf-8")
        report = json.loads(fixture.cli("status", str(directory), "--json").stdout)
        self.assertEqual(report["概要设计"]["state"], "DRAFT")
        self.assertEqual(report["业务流程设计"]["state"], "FROZEN")
        documents = {"详细设计.md": marker + "# 详细设计\n## 设计范围\n覆盖本次材料。\n## 业务逻辑\n### D1：保存（对应 F1 / A1）\n校验实际材料后保存。\n### D2：导出（对应 F2 / A1）\n导出材料。\n## 异常处理\n失败返回原因。\n## 安全设计\n拒绝不匹配材料。\n## 可观测性与配置\n记录结果。\n## 发布与回滚\n保留旧凭证。\n",
            "API设计.md": marker + "# API设计\n## 接口清单\n无新增接口。\n## 接口详细设计\n沿用当前实现。\n",
            "数据库设计.md": marker + "# 数据库设计\n## 表变更清单\n无变更。\n## 详细变更\n沿用当前模型。\n"}
        for name, text in documents.items():
            (directory / name).write_text(text, encoding="utf-8")
        (directory / "reviews/详细设计.issues.json").write_text(json.dumps({"reviewer": "fixture-independent-reviewer",
            "stage": "详细设计", "issues": [], "doc_hashes": {name: hashlib.sha256(text.encode()).hexdigest()
                                                                    for name, text in documents.items()}}), encoding="utf-8")
        result = json.loads(fixture.cli("gate", str(directory), "--stage", "详细设计", "--schema-version", "2", "--json", expected=1).stdout)
        self.assertTrue(any(name.endswith("上游已冻结：概要设计") for name in result["hard"]["failed"]))
        report = json.loads(fixture.cli("status", str(directory), "--json").stdout)
        self.assertNotEqual(report["详细设计"]["state"], "FROZEN")

    def test_global_boundary_unknown_reference_and_missing_proof_invalidate_consumer(self):
        fixture = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_real_planned_pipeline(declared_dependencies=True)
        detail = fixture.requirement / "详细设计.md"
        approved = detail.read_bytes()
        binding = fixture.requirement / "reviews/计划.plan-binding.json"
        original_binding = binding.read_bytes()
        for before, after in (("拒绝不匹配材料。", "允许不匹配材料。"),
                              ("D2：独立导出", "D2：改名导出"),
                              ("独立导出材料。", "独立导出材料并调用 D99。"),
                              ("<!-- aek-review-boundary: declared-id-v1 -->", "")):
            with self.subTest(before=before):
                detail.write_bytes(approved.replace(before.encode(), after.encode()))
                report = json.loads(fixture.cli("status", str(fixture.requirement), "--json").stdout)
                self.assertEqual(report["计划"]["state"], "DRAFT")
                self.assertEqual(binding.read_bytes(), original_binding)
        detail.write_bytes(approved)
        proof = json.loads(original_binding)["material_revision"]["dependency_identity"]
        archived = fixture.requirement / "reviews/materials" / proof["approved_sources"]["详细设计"]["gate_ref"]
        archived.write_bytes(b"corrupt approved source")
        report = json.loads(fixture.cli("status", str(fixture.requirement), "--json").stdout)
        self.assertEqual(report["计划"]["state"], "DRAFT")
        self.assertEqual(archived.read_bytes(), b"corrupt approved source")

    def test_incremental_review_sends_only_current_target_and_necessary_upstream_bodies(self):
        fixture = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_real_planned_pipeline(declared_dependencies=True)
        reviews = fixture.requirement / "reviews"
        def prepare():
            return json.loads(fixture.cli("review-input", str(fixture.requirement), "--stage", "详细设计",
                "--schema-version", "2", "--json").stdout)
        def observe(output):
            (reviews / "详细设计.review-receipt.json").write_text(json.dumps(output["receipt"], ensure_ascii=False), encoding="utf-8")
            (reviews / "详细设计.issues.json").write_text(json.dumps({"reviewer": "fixture-independent-reviewer",
                "stage": "详细设计", "doc_hashes": output["receipt"]["doc_hashes"], "issues": []}), encoding="utf-8")
            fixture.cli("gate", str(fixture.requirement), "--stage", "详细设计", "--schema-version", "2", "--json")
        initial = prepare()
        self.assertEqual(initial["request"]["mode"], "full")
        observe(initial)
        path = fixture.requirement / "详细设计.md"
        path.write_bytes(path.read_bytes().replace("校验实际材料后保存。".encode(), "校验实际材料并记录后保存。".encode()))
        # An old receipt cannot validate the new target bytes.
        fixture.cli("gate", str(fixture.requirement), "--stage", "详细设计", "--schema-version", "2", "--json", expected=3)
        current = prepare()
        request = current["request"]
        self.assertEqual(request["mode"], "incremental", request["scope_proof"])
        self.assertEqual(request["full_documents"], [])
        sent = json.dumps({key: request[key] for key in ("section_documents", "reference_sections", "supporting_context")}, ensure_ascii=False)
        self.assertIn("校验实际材料并记录后保存。", sent)
        self.assertNotIn("独立导出材料", sent)
        self.assertLess(len(sent.encode()), len(json.dumps(initial["request"], ensure_ascii=False).encode()))
        observe(current)

    def test_real_review_receipt_binds_selected_current_text_and_validates_at_original_gate(self):
        fixture = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_real_planned_pipeline(declared_dependencies=True)
        prepared = json.loads(fixture.cli("review-input", str(fixture.requirement), "--stage", "计划",
                                          "--schema-version", "2", "--json").stdout)
        sent = "".join(text for _name, text in prepared["request"]["supporting_context"])
        self.assertIn("校验实际材料后保存", sent)
        self.assertNotIn("独立导出材料", sent)
        reviews = fixture.requirement / "reviews"
        (reviews / "计划.review-receipt.json").write_text(json.dumps(prepared["receipt"], ensure_ascii=False), encoding="utf-8")
        (reviews / "计划.issues.json").write_text(json.dumps({"reviewer": "fixture-independent-reviewer",
            "stage": "计划", "doc_hashes": prepared["receipt"]["doc_hashes"], "issues": []}), encoding="utf-8")
        fixture.cli("gate", str(fixture.requirement), "--stage", "计划", "--schema-version", "2", "--json")

    def test_unrelated_provider_draft_preserves_consumer_but_blocks_new_closeout(self):
        fixture = runpy.run_path(str(ROOT / "tests/doc-material-e2e.test.py"))["MaterialGateE2ETests"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_real_planned_pipeline(declared_dependencies=True)
        consumer_binding = fixture.requirement / "reviews/计划.plan-binding.json"
        original_binding = consumer_binding.read_bytes()
        self.assertIn("dependency_identity", json.loads(original_binding)["material_revision"])
        detail = fixture.requirement / "详细设计.md"
        approved = detail.read_bytes()
        detail.write_bytes(approved.replace("独立导出材料".encode(), "独立导出两个材料".encode()))
        report = json.loads(fixture.cli("status", str(fixture.requirement), "--json").stdout)
        self.assertEqual(report["详细设计"]["state"], "DRAFT")
        self.assertEqual(report["计划"]["state"], "FROZEN")
        self.assertEqual(consumer_binding.read_bytes(), original_binding)
        fixture.cli("closeout", str(fixture.requirement), "--json", expected=1)
        self.assertFalse((fixture.requirement / "reviews/计划.closeout.json").exists())
        (fixture.requirement / "reviews/详细设计.issues.json").write_text(json.dumps({
            "reviewer": "fixture-independent-reviewer", "stage": "详细设计", "issues": [],
            "doc_hashes": {detail.name: hashlib.sha256(detail.read_bytes()).hexdigest()}}), encoding="utf-8")
        fixture.cli("gate", str(fixture.requirement), "--stage", "详细设计", "--schema-version", "2", "--json")
        report = json.loads(fixture.cli("status", str(fixture.requirement), "--json").stdout)
        self.assertEqual(report["计划"]["state"], "FROZEN")
        self.assertEqual(consumer_binding.read_bytes(), original_binding)
        detail.write_bytes(detail.read_bytes().replace("校验实际材料后保存".encode(), "无需校验直接保存".encode()))
        report = json.loads(fixture.cli("status", str(fixture.requirement), "--json").stdout)
        self.assertEqual(report["计划"]["state"], "DRAFT")


if __name__ == "__main__":
    unittest.main()
