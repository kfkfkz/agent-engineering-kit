#!/usr/bin/env python3
"""Single-path dispatch and receipt semantics for MCP migration."""
from __future__ import annotations

import unittest

from aek.application.dispatch import DispatchService, MemoryReceiptStore
from aek.core.dispatch import select_dispatch


class DispatchServiceTests(unittest.TestCase):
    def test_equivalent_zero_padded_versions_select_service(self) -> None:
        decision = select_dispatch(
            service_version="1.0", minimum_version="1.0.0",
            available_capabilities=frozenset({"memory_build"}),
            required_capability="memory_build", parity_passed=True)
        self.assertEqual(decision.selected_path, "service")

    def test_selection_requires_version_capability_and_parity(self) -> None:
        service = select_dispatch(
            service_version="1.0.1", minimum_version="1.0.1",
            available_capabilities=frozenset({"memory_build"}),
            required_capability="memory_build", parity_passed=True)
        self.assertEqual(service.selected_path, "service")
        for version, capabilities, parity in (
            ("1.0.0", frozenset({"memory_build"}), True),
            ("1.0.1", frozenset(), True),
            ("1.0.1", frozenset({"memory_build"}), False),
        ):
            with self.subTest(version=version, capabilities=capabilities,
                              parity=parity):
                decision = select_dispatch(
                    service_version=version, minimum_version="1.0.1",
                    available_capabilities=capabilities,
                    required_capability="memory_build", parity_passed=parity)
                self.assertEqual(decision.selected_path, "legacy")

    def test_unavailable_before_start_falls_back_once(self) -> None:
        calls = []
        service = DispatchService(MemoryReceiptStore())
        outcome = service.execute(
            "request-1", select_dispatch(
                service_version="1.0.1", minimum_version="1.0.1",
                available_capabilities=frozenset({"x"}),
                required_capability="x", parity_passed=True),
            read_only=False, service_ready=lambda: False,
            service_call=lambda: calls.append("service"),
            legacy_call=lambda: calls.append("legacy") or {"ok": True})
        self.assertEqual(calls, ["legacy"])
        self.assertEqual((outcome.selected_path, outcome.started,
                          outcome.committed), ("legacy", True, True))

    def test_failure_after_start_never_falls_back(self) -> None:
        calls = []

        def fail() -> object:
            calls.append("service")
            raise TimeoutError("lost response")

        service = DispatchService(MemoryReceiptStore())
        outcome = service.execute(
            "request-2", select_dispatch(
                service_version="1.0.1", minimum_version="1.0.1",
                available_capabilities=frozenset({"x"}),
                required_capability="x", parity_passed=True),
            read_only=False, service_ready=lambda: True,
            service_call=fail,
            legacy_call=lambda: calls.append("legacy"))
        self.assertEqual(calls, ["service"])
        self.assertTrue(outcome.started)
        self.assertFalse(outcome.committed)
        self.assertFalse(outcome.retryable)

    def test_committed_receipt_answers_retry_without_reexecution(self) -> None:
        calls = []
        store = MemoryReceiptStore()
        service = DispatchService(store)
        decision = select_dispatch(
            service_version="1.0.1", minimum_version="1.0.1",
            available_capabilities=frozenset({"x"}),
            required_capability="x", parity_passed=True)

        def execute() -> dict[str, int]:
            calls.append("service")
            return {"count": len(calls)}

        first = service.execute(
            "request-3", decision, read_only=False,
            service_ready=lambda: True, service_call=execute,
            legacy_call=lambda: calls.append("legacy"))
        second = service.execute(
            "request-3", decision, read_only=False,
            service_ready=lambda: True, service_call=execute,
            legacy_call=lambda: calls.append("legacy"))
        self.assertEqual(calls, ["service"])
        self.assertTrue(first.committed and second.committed)
        self.assertEqual(second.result, {"count": 1})
        self.assertEqual(service.receipt("request-3").state, "COMMITTED")


if __name__ == "__main__":
    unittest.main()
