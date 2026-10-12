#!/usr/bin/env python3
"""Precheck summaries retain complete, current, accessible evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
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


class ReviewSummaryTests(unittest.TestCase):
    def test_failed_summary_retains_every_failure_and_current_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            sdd = Path(directory)
            path = sdd / "需求分析.md"
            path.write_text(REQUIREMENTS, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
            command = [sys.executable, str(ROOT / "doc-gate"), "check", str(sdd),
                       "--stage", "需求分析", "--summary"]
            original = subprocess.run(command, capture_output=True, text=True,
                                      encoding="utf-8", env=env)
            self.assertEqual(original.returncode, 0, original.stdout + original.stderr)
            original_digest = json.loads(original.stdout)["result_digest"]
            path.write_text("# 需求分析\n## 背景\n输入不完整。\n", encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", env=env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            raw = (sdd / "reviews" / "hard-checks.json").read_bytes()
            complete = json.loads(raw)
            self.assertEqual(summary["status"], "FAIL")
            self.assertEqual(summary["failures"], [row for row in complete["checks"]
                if row["status"] == "FAIL"])
            self.assertTrue(summary["failures"])
            self.assertTrue(all(row["group"] and row["name"] and row["detail"]
                                for row in summary["failures"]))
            self.assertEqual(summary["summary"]["fail"], len(summary["failures"]))
            self.assertEqual(summary["result_digest"], hashlib.sha256(raw).hexdigest())
            self.assertNotEqual(summary["result_digest"], original_digest)

    def test_success_summary_binds_accessible_full_report_and_keeps_default_output(self):
        with tempfile.TemporaryDirectory() as directory:
            sdd = Path(directory)
            (sdd / "需求分析.md").write_text(REQUIREMENTS, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
            command = [sys.executable, str(ROOT / "doc-gate"), "check", str(sdd),
                       "--stage", "需求分析"]
            legacy = subprocess.run(command + ["--json"], capture_output=True,
                                    text=True, encoding="utf-8", env=env)
            self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
            original = json.loads(legacy.stdout)
            result = subprocess.run(command + ["--summary", "--json"], capture_output=True,
                                    text=True, encoding="utf-8", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(set(summary), {"schema_version", "stage", "status", "summary",
                "failures", "full_result_ref", "result_digest"})
            self.assertEqual(summary["schema_version"], 1)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(summary["stage"], "需求分析")
            self.assertEqual(summary["failures"], [])
            self.assertEqual(summary["summary"], original["summary"])
            self.assertEqual(summary["full_result_ref"], "reviews/hard-checks.json")
            raw = (sdd / summary["full_result_ref"]).read_bytes()
            self.assertEqual(summary["result_digest"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(json.loads(raw), original)
            self.assertLess(len(result.stdout.encode("utf-8")), len(legacy.stdout.encode("utf-8")))


if __name__ == "__main__":
    unittest.main()
