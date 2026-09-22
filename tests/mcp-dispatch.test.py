#!/usr/bin/env python3
"""MCP migration seam never falls back after a selected path starts."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
