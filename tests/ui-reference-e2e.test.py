"""Installed Chinese requirement workflow, including current-byte UI freeze."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
NAME = "003-中文需求名称"
TIME = "2026-10-13T00:00:00+00:00"
FEATURE = "### F1：预览查询结果\n\n覆盖验收项：A1\n"
PAGE = ("### PAGE-1：查询列表（对应 F1 / A1）\n\n"
        "- 区域映射：AREA-1 → UI-EV P1\n"
        "- 页面状态：加载中 / 空态 / 错误 / 禁用 / 成功\n")
REQUIREMENTS = """# 需求分析：查询
## 背景
已有查询需要明确结果与错误。
## 目标
查询返回结果或明确错误。
## 非目标（边界）
不新增存储。
## 场景与验收依据
### 场景 1：查询
- **前置**：已有合法输入。
- **操作**：提交查询。
- **结果**：返回结果或明确错误。
## 验收项
- [x] A1：查询结果与错误可区分。（场景 1）
## 非功能要求
兼容性：旧查询仍可调用。
## 待确认事项
无阻塞问题。
## 冻结记录
- 冻结状态：已冻结
- 签核人：k
"""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> str:
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))


def image(pixel: bytes) -> bytes:
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + pixel)) + chunk(b"IEND", b""))


class InstalledChineseUiTests(unittest.TestCase):
    def test_installed_chinese_directory_to_human_confirmation_freeze_and_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "项目仓库"
            repo.mkdir()
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
                   "PYTHONIOENCODING": "utf-8", "XDG_CONFIG_HOME": str(base / "config")}

            def invoke(command, *args, payload=None, expected=0):
                result = subprocess.run(command + list(args), cwd=ROOT, env=env,
                                        input=None if payload is None else json.dumps(payload, ensure_ascii=False),
                                        capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result

            invoke([sys.executable, "-m", "installer"], "--link-strategy", "copy", str(repo))
            gate = [sys.executable, str(repo / ".repo-memory-kit/bin/doc-gate")]
            helper = [sys.executable, str(repo / ".agents/skills/design-pipeline/scripts/ui-reference.py")]
            invoke(gate, "init", NAME, "--repo", str(repo), "--json")
            req = repo / "docs/03-SDD" / NAME
            material = repo / "docs/01-需求" / NAME / "原始材料/ui-reference"
            request = {"schema_version": 1, "change_id": "CHG-003", "requirement_dir": NAME,
                       "design_system": {"name": "Ant Design", "source": "user", "evidence": []},
                       "coverage": [{"kind": "feature", "id": "F1", "screenshot_ids": ["P1"]},
                                    {"kind": "acceptance", "id": "A1", "screenshot_ids": ["P1"]}],
                       "screenshots": [{"id": "P1", "page": "查询列表", "state": "default",
                                        "purpose": "查看查询结果", "feature_ids": ["F1"],
                                        "acceptance_ids": ["A1"],
                                        "permissions": {"function_id": "FP01", "data_id": "DP01"}}]}
            invoke(helper, "scaffold", "--repo-root", str(repo), payload=request)
            index_path = material / "截图索引.json"
            screenshot = material / "function/FP01/data/DP01/P1.png"
            self.assertEqual(screenshot.read_bytes(), b"")
            self.assertFalse((repo / "docs/01-需求/CHG-003").exists())
            common = ("--repo-root", str(repo), "--change-id", "CHG-003", "--requirement-dir", NAME)
            invoke(helper, "validate", *common, expected=1)
            screenshot.write_bytes(image(b"\x11\x22\x33"))
            invoke(helper, "validate", *common, expected=1)
            row = json.loads(index_path.read_bytes())["screenshots"][0]
            self.assertEqual(row["validation"]["status"], "valid")
            self.assertEqual(row["applies_to"][0]["content_review"]["status"], "unverified")
            invoke(helper, "record-review", *common, "--expected-index-sha256", digest(index_path.read_bytes()),
                   payload={"schema_version": 1, "updates": [{"screenshot_id": "P1", "context_id": "P1-CAPTURED",
                       "content_review": {"status": "applicable", "reviewer_type": "maintainer",
                                          "reviewer": "k", "reviewed_at": TIME,
                                          "rationale": "已查看合成页面截图，符合目标权限上下文"},
                       "freshness": {"status": "confirmed_current", "confirmer_type": "maintainer",
                                     "confirmer": "k", "confirmed_at": TIME}}]})
            invoke(helper, "validate", *common)

            (req / "需求分析.md").write_bytes(REQUIREMENTS.encode("utf-8"))
            overview = ("# 概要设计\n## 设计目标\n支持查询。\n## 现状分析\n复用查询。\n"
                        "## 总体方案\n显示结果。\n## 功能点设计\n" + FEATURE
                        + "\n## 方案取舍\n沿用组件。\n## 数据与接口影响\n沿用接口。\n## 风险\n无新增风险。\n")
            (req / "概要设计.md").write_bytes(overview.encode("utf-8"))
            invoke(gate, "freeze", str(req), "--stage", "需求分析", "--by", "k")
            invoke(gate, "gate", str(req), "--stage", "概要设计", "--no-review")

            prototype = b"<!doctype html><html><head><style>body{color:#222}</style></head><body>List preview</body></html>"
            (req / "prototypes").mkdir()
            (req / "prototypes/page.html").write_bytes(prototype)
            ref = {"prototype_path": "prototypes/page.html", "prototype_sha256": digest(prototype),
                   "page_id": "PAGE-1", "area_id": "AREA-1", "feature_ids": ["F1"],
                   "acceptance_ids": ["A1"], "change_intent": "添加查询预览", "depends_on": ["P1"]}
            root_rel = req.relative_to(repo).as_posix()
            gate_hash = digest((req / "reviews/概要设计.gate.json").read_bytes())
            invoke(helper, "bind-prototype", *common, "--expected-index-sha256", digest(index_path.read_bytes()),
                   payload={"schema_version": 1, "artifact_plan_id": "legacy-static-003",
                            "gate_digest": gate_hash, "approved_root": root_rel, "prototype_refs": [ref]})
            design_hash = canonical(request["design_system"])
            dependency = canonical({"design_system_sha256": design_hash, "page_design_sha256": digest(PAGE.encode("utf-8")),
                "features": {"F1": FEATURE}, "acceptance": {"A1": "查询结果与错误可区分。（场景 1）"},
                "evidence": [{"id": "P1", "sha256": digest(screenshot.read_bytes()),
                    "semantic": {"page": "查询列表", "state": "default", "purpose": "查看查询结果",
                                 "feature_ids": ["F1"], "acceptance_ids": ["A1"]},
                    "contexts": [{"context_id": "P1-CAPTURED", "permissions": {"function_id": "FP01", "data_id": "DP01"},
                                  "system_version": None, "viewport": None, "relation": "captured"}]}]})
            record = {**ref, "artifact_plan_id": "legacy-static-003", "gate_digest": gate_hash,
                      "approved_root": root_rel, "status": "ACCEPTED", "reviewer": "k", "reviewed_at": TIME,
                      "confirmation_source": "maintainer_reply", "design_system_sha256": design_hash,
                      "dependency_sha256": dependency,
                      "evidence_contexts": [{"evidence_id": "P1", "context_id": "P1-CAPTURED"}]}
            ui = ("# UI设计\n## 界面目标\n显示查询结果。\n## 用户核心任务\n查看结果。\n"
                  "## 信息架构\n查询入口。\n## 页面清单\n" + PAGE
                  + "\n## 设计系统落地\nAnt Design。\n## 交互规则\n显示错误反馈。\n"
                  "## 响应式与兼容\n沿用断点。\n## 可访问性\n键盘可达。\n"
                  "## 验证清单\n已检查页面。\n## 原型确认记录\n<!-- aek-prototypes:required -->\n"
                  "```aek-prototypes\n" + json.dumps({"schema_version": 1, "prototypes": [record]}, ensure_ascii=False) + "\n```\n")
            (req / "UI设计.md").write_bytes(ui.encode("utf-8"))
            invoke(gate, "check", str(req), "--stage", "UI设计", "--json", expected=1)
            namespace = runpy.run_path(gate[1])
            joint = namespace["_prototype_confirmation_digest"]("PAGE-1", "AREA-1", digest(prototype), dependency)
            # Controlled terminal fixture exercises the actual interactive command;
            # production still refuses non-TTY calls and wrong confirmation phrases.
            with patch("sys.stdin") as terminal, patch("builtins.input", return_value="ACCEPT " + joint[:12]), redirect_stdout(io.StringIO()):
                terminal.isatty.return_value = True
                self.assertEqual(namespace["cmd_prototype_confirm"](req, "PAGE-1", "AREA-1", "k"), 0)
            invoke(gate, "check", str(req), "--stage", "UI设计", "--json")
            issues = {"stage": "UI设计", "doc_hashes": {"UI设计.md": digest((req / "UI设计.md").read_bytes())}, "issues": []}
            (req / "reviews/UI设计.issues.json").write_bytes(json.dumps(issues).encode("utf-8"))
            invoke(gate, "gate", str(req), "--stage", "UI设计", "--json")
            state = json.loads(invoke(gate, "status", str(req), "--json").stdout)
            self.assertEqual(state["UI设计"]["state"], "FROZEN")
            screenshot.write_bytes(image(b"\x44\x55\x66"))
            state = json.loads(invoke(gate, "status", str(req), "--json").stdout)
            self.assertEqual(state["UI设计"]["state"], "DRAFT")
            invoke(helper, "validate", *common, expected=1)
            changed = json.loads(index_path.read_bytes())["screenshots"][0]
            self.assertEqual(changed["applies_to"][0]["content_review"]["status"], "unverified")
            invoke(gate, "check", str(req), "--stage", "UI设计", "--json", expected=1)


if __name__ == "__main__":
    unittest.main()
