#!/usr/bin/env python3
"""Real document-gate paths preserve exact approved material, not inferred text."""
from __future__ import annotations

import hashlib
import base64
import json
import os
import shutil
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from aek.adapters.document_material_store import MaterialArchive
from aek.adapters.plan_transaction import PlanStore, PlanInvalid


ROOT = Path(__file__).resolve().parent.parent
DOC_GATE = ROOT / "doc-gate"

REQUIREMENTS = """# 需求分析：材料归档

## 背景

普通正文。

## 目标

保存实际已批准的原始材料。

## 非目标

不自动推测或迁移历史批准。

## 场景与验收依据

### 场景 1：冻结当前需求

- **前置**：维护者确认当前需求
- **操作**：冻结需求
- **结果**：当前批准与确切原字节一致

## 验收项

- [ ] A1：冻结材料保持准确（场景 1）

## 非功能要求

- 安全：保持原始字节和人工批准责任

## 待确认事项

无。

## 冻结记录

- 冻结状态：已冻结
- 确认人：fixture-maintainer
"""

OUTLINE = """# 概要设计：材料归档

## 设计目标

本设计覆盖的验收项：A1

## 现状分析

原入口负责当前阶段文档确认。

## 总体方案

保存实际原字节及发布凭证，不伪造历史材料。

### 主流程

先校验，再保存当前确认材料。

### 异常流程

材料变化则拒绝发布。

## 功能点设计

### F1：保存当前确认材料

- 覆盖验收项：A1
- 输入 → 处理 → 输出：当前材料校验后留存
- 异常行为：拒绝失配输入

## 方案取舍

### DEC-1：保持原批准

- 问题：避免反推旧材料
- 候选：猜测或精确保留
- 选择与原因：精确保留才可核验
- 代价：私有存储增加

## 数据与接口影响

| 对象 | 类型 | 变化 | 兼容性说明 |
| --- | --- | --- | --- |
| 材料 | 文件 | 保存 | 原入口不变 |

## 风险

| ID | 风险 | 触发条件 | 应对 |
| --- | --- | --- | --- |
| RISK-1 | 输入失配 | 并发修改 | 拒绝发布 |
"""


class MaterialGateE2ETests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repo"
        self.requirement = self.repository / "docs" / "03-SDD" / "001-中文材料归档"
        self.requirement.mkdir(parents=True)
        self.document = self.requirement / "需求分析.md"
        # Exact CRLF is deliberate; read_text() universal-newline conversion
        # must not become the original approved material.
        self.raw = REQUIREMENTS.replace("\n", "\r\n").encode("utf-8")
        self.document.write_bytes(self.raw)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-X", "utf8", str(DOC_GATE), *args],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def material_revision(self):
        return json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)["stages"]["需求分析"]["material"]["revision"]

    def freeze_material(self):
        return self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--schema-version", "2", "--json")

    def refresh_cli(self, revision, raw, *, expected=0):
        return self.cli("gate", str(self.requirement), "--stage", "需求分析", "--refresh-equivalent",
            "--expected-revision", revision, "--source-hash", "需求分析.md=" + hashlib.sha256(raw).hexdigest(),
            "--json", expected=expected)

    def review_files(self):
        root = self.requirement / "reviews"
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def test_human_freeze_archives_exact_document_and_published_gate_bytes(self):
        result = self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        gate = json.loads(result.stdout)
        self.assertEqual(gate["status"], "PASS")
        self.assertEqual(gate["approved_by"], "fixture-maintainer")
        reviews = self.requirement / "reviews"
        gate_raw = (reviews / "需求分析.gate.json").read_bytes()
        archive = MaterialArchive(reviews)
        self.assertEqual(archive.get(gate["doc_hashes"]["需求分析.md"]), self.raw)
        self.assertEqual(archive.get(hashlib.sha256(gate_raw).hexdigest()), gate_raw)
        self.assertEqual(self.document.read_bytes(), self.raw)

    def test_new_material_schema_is_explicit_and_existing_v2_cannot_downgrade(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        binding_path = self.requirement / "reviews/需求分析.plan-binding.json"
        self.assertFalse(binding_path.exists())
        self.assertIsNone(self.material_revision())
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--schema-version", "2", "--json")
        self.assertEqual(json.loads(binding_path.read_bytes())["schema_version"], 2)
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        self.assertEqual(json.loads(binding_path.read_bytes())["schema_version"], 2)

    def test_format_refresh_preserves_original_approval_and_freezes_current_bytes(self):
        self.freeze_material()
        reviews = self.requirement / "reviews"
        gate_path = reviews / "需求分析.gate.json"
        original_gate = gate_path.read_bytes()
        log_path = reviews / "需求分析.gate.log"
        original_log = log_path.read_bytes()
        prepared = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        revision = prepared["stages"]["需求分析"]["material"]["revision"]
        self.assertIsInstance(revision, str)
        changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
        self.document.write_bytes(changed)
        request = ("gate", str(self.requirement), "--stage", "需求分析", "--refresh-equivalent",
                   "--expected-revision", revision, "--source-hash",
                   "需求分析.md=" + hashlib.sha256(changed).hexdigest(), "--json")
        refreshed = json.loads(self.cli(*request).stdout)
        self.assertEqual(refreshed["status"], "REFRESHED")
        self.assertNotEqual(refreshed["revision"], revision)
        status = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(status["需求分析"]["state"], "FROZEN")
        current_report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(current_report["stages"]["需求分析"]["material"]["required_action"], "NONE")
        self.assertEqual(gate_path.read_bytes(), original_gate)
        self.assertEqual(log_path.read_bytes(), original_log)
        self.assertEqual(self.document.read_bytes(), changed)
        self.assertFalse((reviews / "需求分析.issues.json").exists())
        retry = json.loads(self.cli(*request).stdout)
        self.assertEqual(retry["status"], "UNCHANGED")
        self.assertEqual(retry["revision"], refreshed["revision"])

    def test_material_binding_preserves_the_actual_unicode_directory_spelling(self):
        renamed = self.requirement.with_name("001-e\u0301页面")
        self.requirement.rename(renamed)
        self.requirement = renamed
        self.document = renamed / "需求分析.md"
        self.freeze_material()
        binding = json.loads((renamed / "reviews/需求分析.plan-binding.json").read_bytes())
        self.assertEqual(binding["material_revision"]["context"]["package_ref"], renamed.as_posix())

    def test_refresh_rejects_content_changes_without_mutating_evidence(self):
        self.freeze_material()
        revision = self.material_revision()
        changed = self.raw.replace("普通正文。".encode(), "普通正文！".encode())
        self.document.write_bytes(changed)
        evidence = self.review_files()
        result = self.refresh_cli(revision, changed, expected=3)
        self.assertIn("NOT_EQUIVALENT", result.stdout)
        self.assertEqual(self.review_files(), evidence)
        self.assertEqual(self.document.read_bytes(), changed)

    def test_refresh_rejects_wrong_raw_hash_and_stale_revision_without_writes(self):
        self.freeze_material()
        revision = self.material_revision()
        changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
        self.document.write_bytes(changed)
        evidence = self.review_files()
        self.assertIn("SOURCE_CHANGED", self.refresh_cli(revision, self.raw, expected=3).stdout)
        self.assertIn("CAS_CONFLICT", self.refresh_cli("0" * 64, changed, expected=3).stdout)
        self.assertEqual(self.review_files(), evidence)

    def test_refresh_rejects_policy_changes_without_upgrading_the_old_approval(self):
        policy = self.repository / ".repo-memory-kit"
        policy.mkdir()
        self.freeze_material()
        revision = self.material_revision()
        (policy / "doc-policy.json").write_text('{"max_minor":1}', encoding="utf-8")
        changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
        self.document.write_bytes(changed)
        evidence = self.review_files()
        self.refresh_cli(revision, changed, expected=3)
        self.assertEqual(self.review_files(), evidence)
        status = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(status["需求分析"]["state"], "DRAFT")

    def test_refresh_does_not_adopt_an_approval_after_package_relocation(self):
        self.freeze_material()
        revision = self.material_revision()
        relocated = self.requirement.with_name("002-另一需求")
        self.requirement.rename(relocated)
        self.requirement = relocated
        self.document = relocated / "需求分析.md"
        evidence = self.review_files()
        self.refresh_cli(revision, self.raw, expected=3)
        self.assertEqual(self.review_files(), evidence)
        status = json.loads(self.cli("status", str(relocated), "--json").stdout)
        self.assertEqual(status["需求分析"]["state"], "DRAFT")

    def test_material_recovery_refuses_changed_source_and_resumes_the_original_epoch(self):
        self.freeze_material()
        reviews = self.requirement / "reviews"
        revision = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)["stages"]["需求分析"]["material"]["revision"]
        changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
        hashes = {"需求分析.md": hashlib.sha256(changed).hexdigest()}
        self.document.write_bytes(changed)
        namespace = runpy.run_path(str(DOC_GATE))
        store = PlanStore(reviews)
        actual_apply = store._apply
        def crash_after_target(*args, **kwargs):
            actual_apply(*args, **kwargs)
            raise RuntimeError("controlled interruption after material target")
        with store.locked_view(self.requirement) as view:
            with mock.patch.object(store, "_apply", crash_after_target), self.assertRaises(RuntimeError):
                namespace["_document_materials"](self.requirement, view).refresh("需求分析",
                    expected_revision=revision, source_hashes=hashes)
        journal = reviews / "artifact-plan.tx.json"
        self.assertTrue(journal.exists())
        self.document.write_bytes(changed.replace("普通正文。".encode(), "实质变更。".encode()))
        with self.assertRaises(PlanInvalid):
            store.recover()  # Generic plan recovery may not adopt a material epoch.
        bad_hash = hashlib.sha256(self.document.read_bytes()).hexdigest()
        self.cli("gate", str(self.requirement), "--stage", "需求分析", "--refresh-equivalent",
                 "--expected-revision", revision, "--source-hash", "需求分析.md=" + bad_hash, "--json", expected=3)
        self.assertTrue(journal.exists())
        self.document.write_bytes(changed)
        recovered = json.loads(self.cli("gate", str(self.requirement), "--stage", "需求分析",
            "--refresh-equivalent", "--expected-revision", revision, "--source-hash",
            "需求分析.md=" + hashes["需求分析.md"], "--json").stdout)
        self.assertEqual(recovered["status"], "UNCHANGED")
        self.assertFalse(journal.exists())
        self.assertEqual(json.loads(self.cli("status", str(self.requirement), "--json").stdout)["需求分析"]["state"], "FROZEN")

    def test_unchanged_refresh_cannot_return_success_after_the_live_source_changes(self):
        self.freeze_material()
        revision = self.material_revision()
        evidence = self.review_files()
        namespace = runpy.run_path(str(DOC_GATE))
        with PlanStore(self.requirement / "reviews").locked_view(self.requirement) as view:
            service = namespace["_document_materials"](self.requirement, view)
            actual_verify = service.verify
            def edit_after_actual_verification(*args, **kwargs):
                result = actual_verify(*args, **kwargs)
                self.document.write_bytes(self.raw.replace("普通正文。".encode(), "另一份语义。".encode()))
                return result
            with mock.patch.object(service, "verify", edit_after_actual_verification), \
                    self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
                service.refresh("需求分析", expected_revision=revision,
                    source_hashes={"需求分析.md": hashlib.sha256(self.raw).hexdigest()})
        self.assertEqual(self.review_files(), evidence)

    def test_two_real_processes_refresh_the_same_material_only_once(self):
        self.freeze_material()
        revision = self.material_revision()
        original_gate = (self.requirement / "reviews/需求分析.gate.json").read_bytes()
        changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
        self.document.write_bytes(changed)
        command = [sys.executable, "-X", "utf8", str(DOC_GATE), "gate", str(self.requirement),
            "--stage", "需求分析", "--refresh-equivalent", "--expected-revision", revision,
            "--source-hash", "需求分析.md=" + hashlib.sha256(changed).hexdigest(), "--json"]
        workers = [subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   text=True, encoding="utf-8") for _ in range(2)]
        try:
            results = []
            for worker in workers:
                out, err = worker.communicate(timeout=30)
                self.assertEqual(worker.returncode, 0, out + err)
                results.append(json.loads(out))
            self.assertEqual(sorted(item["status"] for item in results), ["REFRESHED", "UNCHANGED"])
            self.assertEqual(results[0]["revision"], results[1]["revision"])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()
        self.assertEqual((self.requirement / "reviews/需求分析.gate.json").read_bytes(), original_gate)

    def test_material_journal_recovers_at_each_publication_boundary(self):
        namespace = runpy.run_path(str(DOC_GATE))
        # Explicit schema2 recovery verifies the entire source epoch and
        # resumes prepared intent even before its first pointer publication.
        for cut, expected_status in ((0, "UNCHANGED"), (1, "UNCHANGED"), (2, "UNCHANGED"), (3, "UNCHANGED")):
            with self.subTest(cut=cut):
                self.requirement = self.repository / "docs/03-SDD" / ("001-事务切点-" + str(cut))
                self.requirement.mkdir(parents=True)
                self.document = self.requirement / "需求分析.md"
                self.document.write_bytes(self.raw)
                self.freeze_material()
                revision = self.material_revision()
                reviews = self.requirement / "reviews"
                before_gate = (reviews / "需求分析.gate.json").read_bytes()
                before_binding = (reviews / "需求分析.plan-binding.json").read_bytes()
                changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
                self.document.write_bytes(changed)
                with PlanStore(reviews).locked_view(self.requirement) as view:
                    actual_commit = view.commit_material_refresh
                    def interrupted_commit(*args, **kwargs):
                        return actual_commit(*args, **kwargs, fault_after=cut)
                    with mock.patch.object(view, "commit_material_refresh", interrupted_commit), self.assertRaises(RuntimeError):
                        namespace["_document_materials"](self.requirement, view).refresh("需求分析",
                            expected_revision=revision, source_hashes={"需求分析.md": hashlib.sha256(changed).hexdigest()})
                if cut < 3:
                    self.cli("status", str(self.requirement), "--json", expected=3)
                retry = json.loads(self.refresh_cli(revision, changed).stdout)
                self.assertEqual(retry["status"], expected_status)
                self.assertFalse((reviews / "artifact-plan.tx.json").exists())
                self.assertEqual((reviews / "需求分析.gate.json").read_bytes(), before_gate)
                current = (reviews / "需求分析.plan-binding.json").read_bytes()
                audit = json.loads(current)["material_revision"]
                self.assertEqual(audit["previous_revision"], hashlib.sha256(before_binding).hexdigest())
                self.assertEqual(MaterialArchive(reviews).get(hashlib.sha256(current).hexdigest()), current)

    def test_history_limit_refuses_new_revision_without_invalidating_current_approval(self):
        self.freeze_material()
        namespace = runpy.run_path(str(DOC_GATE))
        from aek.application import document_material
        with mock.patch.object(document_material, "MAX_MATERIAL_HISTORY", 3, create=True):
            with PlanStore(self.requirement / "reviews").locked_view(self.requirement) as view:
                service = namespace["_document_materials"](self.requirement, view)
                for number in range(2):
                    prior = service.verify("需求分析")
                    raw = (self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
                           if number % 2 == 0 else self.raw)
                    self.document.write_bytes(raw)
                    service.refresh("需求分析", expected_revision=prior.binding_digest,
                                    source_hashes={self.document.name: hashlib.sha256(raw).hexdigest()})
                current = service.verify("需求分析")
                binding_path = self.requirement / "reviews/需求分析.plan-binding.json"
                binding_raw = binding_path.read_bytes()
                accepted = self.document.read_bytes()
                raw = accepted.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
                self.document.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "history limit"):
                    service.refresh("需求分析", expected_revision=current.binding_digest,
                                    source_hashes={self.document.name: hashlib.sha256(raw).hexdigest()})
                self.assertEqual(binding_path.read_bytes(), binding_raw)
                self.assertFalse((self.requirement / "reviews/artifact-plan.tx.json").exists())
                self.document.write_bytes(accepted)
                self.assertEqual(service.verify("需求分析"), current)

    def test_recovery_keeps_the_journal_when_an_upstream_document_changes(self):
        self.freeze_material()
        outline = self.requirement / "概要设计.md"
        outline.write_bytes(OUTLINE.encode("utf-8"))
        self.cli("gate", str(self.requirement), "--stage", "概要设计", "--schema-version", "2", "--json")
        report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        revision = report["stages"]["概要设计"]["material"]["revision"]
        changed = outline.read_bytes().replace("原入口负责当前阶段文档确认。\n".encode(),
                                               "原入口负责当前阶段文档确认。\r\n".encode())
        outline.write_bytes(changed)
        namespace = runpy.run_path(str(DOC_GATE))
        with PlanStore(self.requirement / "reviews").locked_view(self.requirement) as view:
            actual_commit = view.commit_material_refresh
            def interrupted(*args, **kwargs):
                return actual_commit(*args, **kwargs, fault_after=1)
            with mock.patch.object(view, "commit_material_refresh", interrupted), self.assertRaises(RuntimeError):
                namespace["_document_materials"](self.requirement, view).refresh("概要设计",
                    expected_revision=revision, source_hashes={"概要设计.md": hashlib.sha256(changed).hexdigest()})
        self.document.write_bytes(self.raw.replace("普通正文。".encode(), "改变需求。".encode()))
        self.cli("gate", str(self.requirement), "--stage", "概要设计", "--refresh-equivalent",
            "--expected-revision", revision, "--source-hash", "概要设计.md=" + hashlib.sha256(changed).hexdigest(),
            "--json", expected=3)
        self.assertTrue((self.requirement / "reviews/artifact-plan.tx.json").exists())

    def prepare_real_planned_pipeline(self, *, declared_dependencies=False):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repository), *args], check=True,
                capture_output=True, text=True, encoding="utf-8").stdout.strip()
        git("init", "-q")
        (self.repository / "README.md").write_text("fixture baseline\n", encoding="utf-8")
        git("add", "README.md")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "fixture baseline")
        base = git("rev-parse", "HEAD")
        detail = """# 详细设计
## 设计范围
覆盖F1、A1。
## 业务逻辑
### D1：保存材料（对应 F1 / A1）
校验实际材料后保存。
## 异常处理
失败返回原因。
## 安全设计
拒绝不匹配材料。
## 可观测性与配置
记录有限结果。
## 发布与回滚
保留旧凭证。
"""
        tasks = """# 任务清单
## 任务
### T1：交付文档
- 对应设计：D1、TC-1
- 预计修改范围：docs/feature.md
- 验证方式：确认临时文件及测试结果
- 发布阻塞：是
- [ ] 实现
## 执行记录
| 任务 | 状态 | 实际改动范围 | 验证结果 |
| T1 | 进行中 | docs/feature.md | 尚未验证 |
## 回归范围确认
- [ ] 回归
## 完成标准
- [ ] 完成
"""
        tests = """# 测试方案
## 测试接缝
临时文档的可观察结果。
## 用例清单
| 编号 | 场景 | 验收项 | 类型 |
| TC-1 | 保存材料 | A1 | E2E |
## 边界与异常覆盖
正常与异常输入。
## 环境与数据
独立临时目录。
## 回归范围
原文档入口。
## 通过标准
- [ ] 测试通过
"""
        outline = OUTLINE
        if declared_dependencies:
            marker = "<!-- aek-review-boundary: declared-id-v1 -->\n"
            self.raw = marker.encode("utf-8") + self.raw
            self.document.write_bytes(self.raw)
            outline = marker + outline.replace("## 方案取舍", "### F2：另一项材料\n\n- 覆盖验收项：A1\n- 输入 → 处理 → 输出：独立导出材料\n- 异常行为：保留原文\n\n## 方案取舍")
            detail = marker + detail.replace("## 异常处理", "### D2：独立导出（对应 F2 / A1）\n独立导出材料。\n## 异常处理")
            tasks, tests = marker + tasks, marker + tests
        for name, text in {"概要设计.md": outline, "详细设计.md": detail, "任务清单.md": tasks, "测试方案.md": tests}.items():
            (self.requirement / name).write_text(text, encoding="utf-8")
        (self.requirement / "reviews").mkdir()
        self.cli("plan-prepare", str(self.requirement), "--base", base, "--route", "standard", "--json")
        self.cli("plan-freeze", str(self.requirement), "--base", base, "--json")
        self.freeze_material()
        self.cli("gate", str(self.requirement), "--stage", "概要设计", "--schema-version", "2", "--json")
        for stage, names in (("详细设计", ("详细设计.md",)), ("计划", ("任务清单.md", "测试方案.md"))):
            # Controlled independent sensor, not a real model/maintainer review.
            (self.requirement / "reviews" / (stage + ".issues.json")).write_text(json.dumps({
                "reviewer": "fixture-independent-reviewer", "stage": stage, "issues": [],
                "doc_hashes": {name: hashlib.sha256((self.requirement / name).read_bytes()).hexdigest()
                               for name in names}}, ensure_ascii=False), encoding="utf-8")
            self.cli("gate", str(self.requirement), "--stage", stage, "--schema-version", "2", "--json")
        (self.repository / "docs/feature.md").write_text("implemented fixture\n", encoding="utf-8")
        (self.requirement / "任务清单.md").write_text(tasks.replace("[ ]", "[x]")
            .replace("进行中", "完成").replace("尚未验证", "fixture tests passed"), encoding="utf-8")
        (self.requirement / "测试方案.md").write_text(tests.replace("[ ]", "[x]"), encoding="utf-8")
        before = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(before["计划"]["state"], "FROZEN")

    def test_real_planned_pipeline_closes_execution_and_then_binds_full_final_bytes(self):
        self.prepare_real_planned_pipeline()
        closed = json.loads(self.cli("closeout", str(self.requirement), "--json").stdout)
        self.assertEqual(closed["status"], "PASS")
        self.assertEqual(set(closed["upstream_fingerprints"]), {"详细设计"})
        self.assertRegex(closed["upstream_fingerprints"]["详细设计"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(closed["upstream_fingerprints"]["详细设计"], "0" * 64)
        after = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(after["计划"]["state"], "FROZEN")
        final_report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(final_report["stages"]["计划"]["material"]["classification"], "EXACT")
        binding = json.loads((self.requirement / "reviews/计划.plan-binding.json").read_bytes())
        self.assertEqual(binding["schema_version"], 2)
        final = (self.requirement / "任务清单.md").read_bytes()
        self.assertEqual(binding["material_revision"]["raw_after"]["任务清单.md"], hashlib.sha256(final).hexdigest())
        (self.requirement / "任务清单.md").write_bytes(final.replace(b"[x]", b"[ ]", 1))
        edited = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(edited["计划"]["state"], "DRAFT")

    def test_closeout_rejects_a_source_edit_after_the_real_completion_check(self):
        self.prepare_real_planned_pipeline()
        binding_path = self.requirement / "reviews/计划.plan-binding.json"
        before = binding_path.read_bytes()
        program = """import pathlib,runpy,sys
ns=runpy.run_path(sys.argv[1]); directory=pathlib.Path(sys.argv[2])
command=ns['cmd_closeout']; actual=command.__globals__['_plan_completion_failures']
def edit_after_check(*args,**kwargs):
    result=actual(*args,**kwargs)
    assert result==[], result
    path=directory/'任务清单.md'; path.write_bytes(path.read_bytes().replace(b'[x]',b'[ ]',1))
    return result
command.__globals__['_plan_completion_failures']=edit_after_check
raise SystemExit(command(directory,True))
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(DOC_GATE), str(self.requirement)], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertFalse((self.requirement / "reviews/计划.closeout.json").exists())
        self.assertEqual(binding_path.read_bytes(), before)

    def test_closeout_retries_after_material_storage_failure_without_downgrade(self):
        self.prepare_real_planned_pipeline()
        binding_path = self.requirement / "reviews/计划.plan-binding.json"
        before = binding_path.read_bytes()
        program = """import pathlib,runpy,sys
from unittest import mock
ns=runpy.run_path(sys.argv[1]); directory=pathlib.Path(sys.argv[2])
with mock.patch.object(ns['DocumentMaterials'],'baseline',side_effect=OSError('controlled archive failure')):
    raise SystemExit(ns['cmd_closeout'](directory,True))
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(DOC_GATE), str(self.requirement)], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(binding_path.read_bytes(), before)
        self.assertTrue((self.requirement / "reviews/计划.closeout.json").is_file())
        self.cli("closeout", str(self.requirement), "--json")
        report = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(report["计划"]["state"], "FROZEN")

    @unittest.skipIf(os.name == "nt", "POSIX link fixture; native reparse cases use shared filesystem tests")
    def test_closeout_never_writes_through_an_existing_symlink(self):
        self.prepare_real_planned_pipeline()
        outside = self.root / "outside-user-file.txt"
        outside.write_bytes(b"original user data")
        target = self.requirement / "reviews/计划.closeout.json"
        target.symlink_to(outside)
        self.cli("closeout", str(self.requirement), "--json", expected=3)
        self.assertEqual(outside.read_bytes(), b"original user data")
        self.assertTrue(target.is_symlink())

    def test_current_real_approval_is_retained_when_a_material_object_is_corrupt(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        reviews = self.requirement / "reviews"
        gate_path = reviews / "需求分析.gate.json"
        original_gate = gate_path.read_bytes()
        sha = hashlib.sha256(self.raw).hexdigest()
        blob = reviews / "materials" / sha
        blob.write_bytes(b"corrupt material")
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json", expected=3)
        self.assertEqual(gate_path.read_bytes(), original_gate)
        self.assertEqual(blob.read_bytes(), b"corrupt material")

    def test_new_explicit_approval_preserves_previous_gate_and_material_bytes(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        reviews = self.requirement / "reviews"
        gate_path = reviews / "需求分析.gate.json"
        original_gate = gate_path.read_bytes()
        changed = self.raw.replace("普通正文。".encode(), "当前新正文。".encode())
        self.document.write_bytes(changed)
        # This is a separate explicit fixture human-stage decision, not an
        # equivalent refresh, and must not erase the earlier approved bytes.
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        archive = MaterialArchive(reviews)
        self.assertEqual(archive.get(hashlib.sha256(original_gate).hexdigest()), original_gate)
        self.assertEqual(archive.get(hashlib.sha256(self.raw).hexdigest()), self.raw)
        self.assertEqual(archive.get(hashlib.sha256(changed).hexdigest()), changed)
        self.assertNotEqual(gate_path.read_bytes(), original_gate)

    def test_status_does_not_implicitly_archive_or_rebind_old_approval(self):
        reviews = self.requirement / "reviews"
        reviews.mkdir()
        old_gate = json.dumps({"stage": "需求分析", "status": "PASS",
            "approved_by": "legacy-fixture", "doc_hashes": {
                "需求分析.md": hashlib.sha256(self.raw).hexdigest()}}, ensure_ascii=False).encode()
        (reviews / "需求分析.gate.json").write_bytes(old_gate)
        result = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(result["需求分析"]["state"], "FROZEN")
        self.assertFalse((reviews / "materials").exists())
        self.assertEqual((reviews / "需求分析.gate.json").read_bytes(), old_gate)

    def test_copy_install_freezes_material_without_importing_source_checkout(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.root / "config"), "PYTHONPATH": str(ROOT)}
        installed = subprocess.run([sys.executable, "-m", "installer", "--codex-root", str(workspace),
            "--link-strategy", "copy", str(self.repository)], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        executable = self.repository / ".repo-memory-kit/bin/doc-gate"
        result = subprocess.run([sys.executable, "-I", str(executable), "freeze", str(self.requirement),
            "--by", "fixture-maintainer", "--json"], cwd=workspace, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        gate = json.loads(result.stdout)
        archived = self.requirement / "reviews/materials" / gate["doc_hashes"]["需求分析.md"]
        self.assertEqual(archived.read_bytes(), self.raw)

    def test_source_changed_after_checks_does_not_replace_existing_approval(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        gate_path = self.requirement / "reviews/需求分析.gate.json"
        original_gate = gate_path.read_bytes()
        program = """import pathlib,runpy,sys
namespace=runpy.run_path(sys.argv[1])
command=namespace['cmd_freeze']
actual=command.__globals__['run_checks']
directory=pathlib.Path(sys.argv[2])
def concurrent_edit(*args,**kwargs):
    checks=actual(*args,**kwargs)
    document=directory/'需求分析.md'
    document.write_bytes(document.read_bytes().replace('普通正文。'.encode(),'并发修改正文。'.encode()))
    return checks
command.__globals__['run_checks']=concurrent_edit
raise SystemExit(command(directory,'需求分析','fixture-maintainer',True))
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(DOC_GATE), str(self.requirement)], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(gate_path.read_bytes(), original_gate)
        self.assertIn("并发修改正文。".encode(), self.document.read_bytes())

    def test_optional_machine_gate_archives_exact_material_and_credential(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        outline = self.requirement / "概要设计.md"
        raw = OUTLINE.replace("\n", "\r\n").encode("utf-8")
        outline.write_bytes(raw)
        result = self.cli("gate", str(self.requirement), "--stage", "概要设计", "--json")
        gate = json.loads(result.stdout)
        archive = MaterialArchive(self.requirement / "reviews")
        self.assertEqual(archive.get(gate["doc_hashes"]["概要设计.md"]), raw)
        actual = (self.requirement / "reviews/概要设计.gate.json").read_bytes()
        self.assertEqual(archive.get(hashlib.sha256(actual).hexdigest()), actual)

    def test_reviewed_machine_gate_archives_the_actual_lifecycle_bound_credential(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        outline = self.requirement / "概要设计.md"
        raw = OUTLINE.replace("\n", "\r\n").encode("utf-8")
        outline.write_bytes(raw)
        prepared = json.loads(self.cli("review-input", str(self.requirement), "--stage", "概要设计",
            "--schema-version", "2", "--json").stdout)
        reviews = self.requirement / "reviews"
        (reviews / "概要设计.review-receipt.json").write_text(
            json.dumps(prepared["receipt"], ensure_ascii=False), encoding="utf-8")
        # A synthetic sensor tests the original deterministic protocol. It is
        # not an actual model review or a real maintainer's production signoff.
        (reviews / "概要设计.issues.json").write_text(json.dumps({
            "reviewer": "fixture-independent-reviewer", "stage": "概要设计",
            "doc_hashes": dict(prepared["request"]["doc_hashes"]), "issues": []},
            ensure_ascii=False), encoding="utf-8")
        gate = json.loads(self.cli("gate", str(self.requirement), "--stage", "概要设计", "--json").stdout)
        self.assertIn("lifecycle_snapshot_digest", gate)
        archive = MaterialArchive(reviews)
        self.assertEqual(archive.get(gate["doc_hashes"]["概要设计.md"]), raw)
        actual = (reviews / "概要设计.gate.json").read_bytes()
        self.assertEqual(archive.get(hashlib.sha256(actual).hexdigest()), actual)

    def aba_during_checks(self, stage, valid):
        program = """import pathlib,runpy,sys,base64
namespace=runpy.run_path(sys.argv[1])
directory=pathlib.Path(sys.argv[2]); stage=sys.argv[3]
valid=base64.b64decode(sys.argv[4]); document=directory/(stage+'.md')
invalid=document.read_bytes()
command=namespace['cmd_freeze' if stage=='需求分析' else 'cmd_gate']
actual=command.__globals__['run_checks']
def concurrent_aba(*args,**kwargs):
    document.write_bytes(valid)
    try:
        return actual(*args,**kwargs)
    finally:
        document.write_bytes(invalid)
command.__globals__['run_checks']=concurrent_aba
raise SystemExit(command(directory,stage,'fixture-maintainer',True) if stage=='需求分析'
                 else command(directory,stage,False,True))
"""
        return subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(DOC_GATE), str(self.requirement), stage, base64.b64encode(valid).decode("ascii")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_freeze_checks_the_captured_document_not_a_temporary_valid_replacement(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        gate_path = self.requirement / "reviews/需求分析.gate.json"
        old_gate = gate_path.read_bytes()
        invalid = self.raw.replace("## 背景\r\n\r\n普通正文。\r\n\r\n".encode(), b"")
        self.assertNotEqual(invalid, self.raw)
        self.document.write_bytes(invalid)
        self.cli("check", str(self.requirement), "--stage", "需求分析", expected=1)
        result = self.aba_during_checks("需求分析", self.raw)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.document.read_bytes(), invalid)
        self.assertEqual(gate_path.read_bytes(), old_gate)

    def test_machine_gate_checks_the_captured_document_not_a_temporary_valid_replacement(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        document = self.requirement / "概要设计.md"
        valid = OUTLINE.encode("utf-8")
        document.write_bytes(valid)
        self.cli("gate", str(self.requirement), "--stage", "概要设计", "--json")
        gate_path = self.requirement / "reviews/概要设计.gate.json"
        old_gate = gate_path.read_bytes()
        invalid = valid.replace("## 现状分析\n\n原入口负责当前阶段文档确认。\n\n".encode(), b"")
        self.assertNotEqual(invalid, valid)
        document.write_bytes(invalid)
        self.cli("check", str(self.requirement), "--stage", "概要设计", expected=1)
        result = self.aba_during_checks("概要设计", valid)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        gate = json.loads(result.stdout)
        self.assertEqual(gate["status"], "NEEDS_REVISION")
        self.assertGreater(gate["hard"]["fail"], 0)
        self.assertEqual(document.read_bytes(), invalid)
        archive = MaterialArchive(self.requirement / "reviews")
        self.assertEqual(archive.get(hashlib.sha256(old_gate).hexdigest()), old_gate)

    def test_machine_review_cannot_approve_a_different_temporary_document(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        document = self.requirement / "概要设计.md"
        current = OUTLINE.encode("utf-8")
        reviewed = current.replace("保存实际原字节及发布凭证，不伪造历史材料。".encode(),
                                   "另一份待评审设计内容。".encode())
        self.assertNotEqual(current, reviewed)
        document.write_bytes(current)
        reviews = self.requirement / "reviews"
        (reviews / "概要设计.issues.json").write_text(json.dumps({
            "reviewer": "fixture-independent-reviewer", "stage": "概要设计",
            "doc_hashes": {document.name: hashlib.sha256(reviewed).hexdigest()},
            "issues": []}, ensure_ascii=False), encoding="utf-8")
        program = """import pathlib,runpy,sys,base64
namespace=runpy.run_path(sys.argv[1]); directory=pathlib.Path(sys.argv[2])
document=directory/'概要设计.md'; current=document.read_bytes()
reviewed=base64.b64decode(sys.argv[3]); command=namespace['cmd_gate']
actual=command.__globals__['_validate_issues']
def concurrent_aba(*args,**kwargs):
    document.write_bytes(reviewed)
    try:
        return actual(*args,**kwargs)
    finally:
        document.write_bytes(current)
command.__globals__['_validate_issues']=concurrent_aba
raise SystemExit(command(directory,'概要设计',False,True))
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(DOC_GATE), str(self.requirement), base64.b64encode(reviewed).decode("ascii")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(document.read_bytes(), current)
        self.assertFalse((reviews / "概要设计.gate.json").exists())
        self.assertFalse((reviews / "概要设计.issue-lifecycle.json").exists())

    def test_sensor_change_after_validation_does_not_publish_pass(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        document = self.requirement / "概要设计.md"
        document.write_bytes(OUTLINE.encode("utf-8"))
        reviews = self.requirement / "reviews"
        sensor = reviews / "概要设计.issues.json"
        sensor.write_text(json.dumps({"reviewer": "fixture-independent-reviewer",
            "stage": "概要设计", "doc_hashes": {document.name:
                hashlib.sha256(document.read_bytes()).hexdigest()}, "issues": []},
            ensure_ascii=False), encoding="utf-8")
        program = """import pathlib,runpy,sys
namespace=runpy.run_path(sys.argv[1]); directory=pathlib.Path(sys.argv[2])
command=namespace['cmd_gate']; actual=command.__globals__['_validate_issues']
def concurrent_edit(*args,**kwargs):
    result=actual(*args,**kwargs)
    (directory/'reviews/概要设计.issues.json').write_bytes(b'changed sensor')
    return result
command.__globals__['_validate_issues']=concurrent_edit
raise SystemExit(command(directory,'概要设计',False,True))
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
            str(DOC_GATE), str(self.requirement)], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertFalse((reviews / "概要设计.gate.json").exists())
        self.assertEqual(sensor.read_bytes(), b"changed sensor")

    def test_review_request_and_gate_bind_actual_crlf_source_bytes(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        document = self.requirement / "概要设计.md"
        raw = OUTLINE.replace("\n", "\r\n").encode("utf-8")
        document.write_bytes(raw)
        self.cli("check", str(self.requirement), "--stage", "概要设计", "--json")
        prepared = json.loads(self.cli("review-input", str(self.requirement), "--stage", "概要设计",
            "--schema-version", "2", "--json").stdout)
        self.assertEqual(dict(prepared["request"]["full_documents"])[document.name], raw.decode("utf-8"))
        self.assertEqual(dict(prepared["request"]["doc_hashes"])[document.name], hashlib.sha256(raw).hexdigest())
        reviews = self.requirement / "reviews"
        (reviews / "概要设计.review-receipt.json").write_text(
            json.dumps(prepared["receipt"], ensure_ascii=False), encoding="utf-8")
        (reviews / "概要设计.issues.json").write_text(json.dumps({
            "reviewer": "fixture-independent-reviewer", "stage": "概要设计",
            "doc_hashes": dict(prepared["request"]["doc_hashes"]), "issues": []},
            ensure_ascii=False), encoding="utf-8")
        gate = json.loads(self.cli("gate", str(self.requirement), "--stage", "概要设计", "--json").stdout)
        self.assertEqual(gate["doc_hashes"][document.name], hashlib.sha256(raw).hexdigest())
        self.assertEqual(MaterialArchive(reviews).get(hashlib.sha256(raw).hexdigest()), raw)

    def test_explicit_material_status_explains_equivalence_without_approving_or_writing(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        changed = self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode())
        self.assertNotEqual(changed, self.raw)
        self.document.write_bytes(changed)
        before = {str(p.relative_to(self.requirement)): p.read_bytes()
                  for p in self.requirement.rglob("*") if p.is_file()}
        report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(report["schema_version"], 2)
        material = report["stages"]["需求分析"]["material"]
        self.assertEqual(material["classification"], "EQUIVALENT")
        self.assertEqual(material["reason_code"], "SUPPORTED_FORMAT_ONLY")
        self.assertEqual(material["required_action"], "REFRESH_EQUIVALENT")
        self.assertEqual(material["raw_before"], {"需求分析.md": hashlib.sha256(self.raw).hexdigest()})
        self.assertEqual(material["raw_after"], {"需求分析.md": hashlib.sha256(changed).hexdigest()})
        self.assertEqual(report["stages"]["需求分析"]["state"], "DRAFT")
        after = {str(p.relative_to(self.requirement)): p.read_bytes()
                 for p in self.requirement.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        legacy = json.loads(self.cli("status", str(self.requirement), "--json").stdout)
        self.assertEqual(legacy["需求分析"]["state"], "DRAFT")
        self.assertNotIn("schema_version", legacy)

    def test_material_status_does_not_infer_a_changed_legacy_baseline(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        materials = self.requirement / "reviews/materials"
        shutil.rmtree(materials)  # Model a legacy Gate without archived bytes.
        (self.requirement / "reviews/需求分析.plan-binding.json").unlink(missing_ok=True)
        exact = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(exact["stages"]["需求分析"]["material"]["classification"], "EXACT")
        self.assertFalse(materials.exists())
        self.document.write_bytes(self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode()))
        changed = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(changed["stages"]["需求分析"]["material"]["classification"], "UNKNOWN")
        self.assertEqual(changed["stages"]["需求分析"]["material"]["reason_code"], "BASELINE_MISSING")
        self.assertFalse(materials.exists())

    def test_material_status_keeps_punctuation_changes_for_review(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        self.document.write_bytes(self.raw.replace("普通正文。".encode(), "普通正文！".encode()))
        report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(report["stages"]["需求分析"]["material"]["classification"], "REVIEW_REQUIRED")
        self.assertEqual(report["stages"]["需求分析"]["material"]["required_action"], "REVIEW")
        self.assertEqual(report["stages"]["需求分析"]["state"], "DRAFT")

    def test_material_status_preserves_corrupted_baseline_and_fails_closed(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        blob = self.requirement / "reviews/materials" / hashlib.sha256(self.raw).hexdigest()
        blob.write_bytes(b"user replacement")
        self.document.write_bytes(self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode()))
        report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        self.assertEqual(report["stages"]["需求分析"]["material"]["classification"], "UNKNOWN")
        self.assertEqual(report["stages"]["需求分析"]["material"]["required_action"], "REVIEW")
        self.assertEqual(blob.read_bytes(), b"user replacement")

    def test_material_status_does_not_suggest_refresh_under_an_unknown_plan(self):
        self.cli("freeze", str(self.requirement), "--by", "fixture-maintainer", "--json")
        self.document.write_bytes(self.raw.replace("普通正文。\r\n".encode(), "普通正文。\n".encode()))
        (self.requirement / "reviews/artifact-plan.json").write_bytes(b'{"schema_version":999}')
        report = json.loads(self.cli("status", str(self.requirement), "--schema-version", "2", "--json").stdout)
        stage = report["stages"]["需求分析"]
        self.assertEqual(stage["state"], "INVALID")
        self.assertEqual(stage["material"]["classification"], "UNKNOWN")
        self.assertEqual(stage["material"]["reason_code"], "PLAN_UNVERIFIABLE")


if __name__ == "__main__":
    unittest.main()
