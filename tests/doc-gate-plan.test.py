#!/usr/bin/env python3
"""Dynamic plan CLI lifecycle, skip bypass and transitive invalidation."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DOC_GATE = ROOT / "doc-gate"


class DynamicPlanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.req = self.repo / "docs" / "03-SDD" / "001-plan"
        (self.req / "reviews").mkdir(parents=True)
        self.docs = {
            "需求分析.md": "# requirements\n",
            "概要设计.md": "# outline\n",
            "详细设计.md": "# detail\n",
            "任务清单.md": "# tasks\n",
            "测试方案.md": "# tests\n",
        }
        for name, text in self.docs.items():
            (self.req / name).write_text(text, encoding="utf-8")

    def write_valid_requirements(self) -> None:
        (self.req / "需求分析.md").write_text("""# requirements

## 背景

现有流程需要一次安全的小幅调整。

## 目标

完成明确且可验证的行为变更。

## 非目标

不改变接口、数据和安全边界。

## 场景与验收依据

### 场景 1：执行变更

- **前置**：维护者已确认范围
- **操作**：执行目标修改
- **结果**：目标行为可观察且回归通过

## 验收项

- [ ] A1：目标行为满足场景 1

## 非功能要求

- 性能：N/A-无热点路径变化
- 安全：N/A-无边界变化

## 待确认事项

- 无。

## 冻结记录

- 冻结状态：已冻结
- 确认人：ll
""", encoding="utf-8")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True, encoding="utf-8").stdout

    def run_gate(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(DOC_GATE), *args], cwd=self.repo,
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        return proc

    def write_gate(self, stage: str, documents: tuple[str, ...]) -> None:
        hashes = {name: hashlib.sha256(
            (self.req / name).read_bytes()).hexdigest() for name in documents}
        gate = {"stage": stage, "status": "PASS", "doc_hashes": hashes}
        if stage == "计划":
            baseline = {}
            for name in documents:
                text = (self.req / name).read_text(encoding="utf-8")
                projected = re.sub(
                    r"(?m)^(\s*[-*]\s*)\[[ xX]\]", r"\1[ ]", text)
                if name == "任务清单.md":
                    projected = re.sub(
                        r"(?ms)(^##\s+执行记录\s*\n).*?(?=^##\s+|\Z)",
                        r"\1<AEK-EXECUTION-RECORD>\n\n", projected)
                baseline[name] = hashlib.sha256(projected.encode()).hexdigest()
            gate["execution_baseline_hashes"] = baseline
        (self.req / "reviews" / f"{stage}.gate.json").write_text(json.dumps(
            gate, ensure_ascii=False), encoding="utf-8")

    def status(self) -> dict:
        proc = self.run_gate(
            "plan-status", str(self.req), "--base", self.base, "--json")
        return json.loads(proc.stdout)

    def test_prepare_freeze_bind_skip_invalidate_and_rollback(self) -> None:
        (self.req / "任务清单.md").write_text("""# tasks
## 任务
- [ ] T1
## 执行记录
| T1 | 进行中 |
## 回归范围确认
- [ ] regression
## 完成标准
- [ ] done
""", encoding="utf-8")
        self.run_gate(
            "plan-prepare", str(self.req), "--base", self.base,
            "--route", "standard", "--json")
        self.assertTrue((self.req / "reviews" / "artifact-plan.json").is_file())
        self.run_gate(
            "plan-freeze", str(self.req), "--base", self.base, "--json")
        self.assertEqual(self.status()["state"], "FROZEN")

        stages = (
            ("需求分析", ("需求分析.md",)),
            ("概要设计", ("概要设计.md",)),
            ("详细设计", ("详细设计.md",)),
            ("计划", ("任务清单.md", "测试方案.md")),
        )
        for stage, documents in stages:
            self.write_gate(stage, documents)
            self.run_gate(
                "plan-bind", str(self.req), "--base", self.base,
                "--stage", stage, "--json")
        current = self.status()["effective_stage_states"]
        self.assertEqual(current["需求分析"], "BOUND")
        self.assertEqual(current["概要设计"], "BOUND")
        self.assertEqual(current["业务流程设计"], "SKIPPED")
        self.assertEqual(current["UI设计"], "SKIPPED")
        self.assertEqual(current["详细设计"], "BOUND")
        self.assertEqual(current["计划"], "BOUND")

        tasks = self.req / "任务清单.md"
        tasks.write_text(tasks.read_text(encoding="utf-8").replace(
            "| T1 | 进行中 |", "| T1 | 完成 | actual | tests green |").replace(
            "- [ ] T1", "- [x] T1"), encoding="utf-8")
        self.assertEqual(self.status()["effective_stage_states"]["计划"], "BOUND")

        (self.req / "需求分析.md").write_text(
            "# changed requirements\n", encoding="utf-8")
        stale = self.status()["effective_stage_states"]
        self.assertEqual(stale["需求分析"], "STALE")
        self.assertEqual(stale["概要设计"], "STALE")
        self.assertEqual(stale["详细设计"], "STALE")
        self.assertEqual(stale["计划"], "STALE")

        self.run_gate("plan-rollback", str(self.req), "--json")
        self.assertFalse((self.req / "reviews" / "artifact-plan.json").exists())
        self.assertTrue((self.req / "reviews" / "需求分析.gate.json").exists())

    def test_existing_freeze_entry_binds_frozen_dynamic_plan(self) -> None:
        self.write_valid_requirements()
        self.run_gate(
            "plan-prepare", str(self.req), "--base", self.base,
            "--route", "standard", "--json")
        self.run_gate(
            "plan-freeze", str(self.req), "--base", self.base, "--json")

        frozen = self.run_gate(
            "freeze", str(self.req), "--stage", "需求分析", "--by", "ll",
            "--json")
        gate = json.loads(frozen.stdout)
        self.assertEqual(gate["status"], "PASS")
        self.assertTrue(
            (self.req / "reviews" / "需求分析.plan-binding.json").is_file())

        dynamic = self.status()
        self.assertEqual(dynamic["effective_stage_states"]["需求分析"], "BOUND")
        self.assertEqual(dynamic["effective_stage_states"]["业务流程设计"], "SKIPPED")
        public_status = json.loads(self.run_gate(
            "status", str(self.req), "--json").stdout)
        self.assertEqual(public_status["需求分析"]["state"], "FROZEN")
        self.assertEqual(public_status["业务流程设计"]["state"], "SKIPPED")


if __name__ == "__main__":
    unittest.main()
