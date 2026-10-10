#!/usr/bin/env python3
"""MCP migration seam never falls back after a selected path starts."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class McpDispatchTests(unittest.TestCase):
    def module(self):
        name = "aek_mcp_dispatch_test"
        loader = importlib.machinery.SourceFileLoader(
            name, str(ROOT / "agent-engineering-mcp"))
        spec = importlib.util.spec_from_loader(name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        return module

    def test_timeout_after_start_does_not_invoke_legacy(self) -> None:
        module = self.module()
        calls = []

        def fail(_args):
            calls.append("service")
            raise TimeoutError("injected")

        module.SERVICE_TOOL_HANDLERS["memory_build"] = fail
        module.LEGACY_TOOL_HANDLERS["memory_build"] = (
            lambda _args: calls.append("legacy") or {"returncode": 0})
        result = module._execute_tool(41, "memory_build", {"mode": "check"})
        self.assertEqual(calls, ["service"])
        self.assertFalse(result["dispatch"]["committed"])
        self.assertFalse(result["dispatch"]["retryable"])

    def test_response_retry_returns_commit_receipt_without_reexecution(self) -> None:
        module = self.module()
        calls = []

        def commit(_args):
            calls.append("service")
            return {"returncode": 0, "output": "done", "error": ""}

        module.SERVICE_TOOL_HANDLERS["memory_build"] = commit
        first = module._execute_tool(42, "memory_build", {"mode": "build"})
        second = module._execute_tool(42, "memory_build", {"mode": "build"})
        self.assertEqual(calls, ["service"])
        self.assertEqual(first, second)

    def test_memory_purpose_contract_rejects_freeform_before_cli_start(self) -> None:
        module = self.module()
        context = {"schema_version": 1, "route": "standard", "stage": "design",
                   "purpose": "依赖迁移的影响分析与历史约束核查", "subject_digest": "d" * 64,
                   "session_id": "purpose-test"}
        with patch.object(module, "run_cli", return_value=(0, "{}", "")) as process:
            result = module.tool_memory_recall({"query": "dependency migration", "context": context})
        process.assert_not_called()
        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["reason_code"], "INVALID_CONTEXT_PURPOSE")
        self.assertFalse(result["lookup_started"])
        self.assertNotIn(context["purpose"], result["error"])
        self.assertIn("target_evidence", result["allowed_purposes"])
        tool = next(tool for tool in module.TOOLS if tool["name"] == "memory_recall")
        schema = tool["inputSchema"]["properties"]["context"]["properties"]["purpose"]
        self.assertEqual(set(schema["enum"]), set(result["allowed_purposes"]))
        self.assertEqual(schema["default"], "target_evidence")

    def test_memory_identity_validation_precedes_cli_and_accepts_uuid_format(self) -> None:
        module = self.module()
        context = {"schema_version": 1, "route": "standard", "stage": "design", "purpose": "target_evidence",
                   "subject_digest": "d" * 64,
                   "session_id": "12345678-1234-1234-1234-123456789abc-123456abcdef"}
        for change in ({"session_id": "../../../outside"}, {"session_id": "a" * 65},
                       {"subject_digest": "../../outside"}, {"schema_version": True},
                       {"route": []}, {"stage": []}):
            with self.subTest(change=change), patch.object(module, "run_cli") as process:
                result = module.tool_memory_recall({"query": "migration", "context": {**context, **change}})
                process.assert_not_called()
                self.assertEqual(result["returncode"], 2)
                self.assertFalse(result["lookup_started"])
        with patch.object(module, "run_cli", return_value=(0, "{}", "")) as process:
            result = module.tool_memory_recall({"query": "migration", "context": context})
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(process.call_args.args[1][-1], context["session_id"])


if __name__ == "__main__":
    unittest.main()
