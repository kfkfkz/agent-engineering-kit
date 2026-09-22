"""Execute exactly one preselected path and retain queryable commit receipts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from aek.core.dispatch import DispatchDecision, DispatchOutcome


@dataclass(frozen=True)
class DispatchReceipt:
    request_key: str
    selected_path: str
    state: str
    result: object | None = None
    error: str = ""


class ReceiptPort(Protocol):
    def get(self, request_key: str) -> DispatchReceipt | None: ...
    def put(self, receipt: DispatchReceipt) -> None: ...


class MemoryReceiptStore:
    def __init__(self) -> None:
        self._values: dict[str, DispatchReceipt] = {}

    def get(self, request_key: str) -> DispatchReceipt | None:
        return self._values.get(request_key)

    def put(self, receipt: DispatchReceipt) -> None:
        self._values[receipt.request_key] = receipt


class DispatchService:
    def __init__(self, receipts: ReceiptPort) -> None:
        self._receipts = receipts

    def receipt(self, request_key: str) -> DispatchReceipt | None:
        return self._receipts.get(request_key)

    def execute(
        self, request_key: str, decision: DispatchDecision, *, read_only: bool,
        service_ready: Callable[[], bool], service_call: Callable[[], object],
        legacy_call: Callable[[], object],
    ) -> DispatchOutcome:
        if not isinstance(request_key, str) or not request_key:
            raise ValueError("dispatch request key is required")
        prior = self._receipts.get(request_key)
        if prior is not None:
            if prior.state == "COMMITTED":
                return DispatchOutcome(prior.selected_path, True, True, False,
                                       prior.result, prior.error)
            return DispatchOutcome(prior.selected_path, True, False,
                                   bool(read_only), error=prior.error or
                                   "previous attempt started without a commit receipt")

        selected = decision.selected_path
        if selected not in {"service", "legacy"}:
            raise ValueError("unknown dispatch path")
        if selected == "service":
            try:
                ready = service_ready()
            except Exception as exc:  # pre-start capability probe
                ready = False
                readiness_error = str(exc)
            else:
                readiness_error = ""
            if not ready:
                selected = "legacy"
        else:
            readiness_error = ""
        call = service_call if selected == "service" else legacy_call
        self._receipts.put(DispatchReceipt(
            request_key, selected, "STARTED", error=readiness_error))
        try:
            result = call()
        except Exception as exc:  # execution started: never switch paths
            error = f"{type(exc).__name__}: {exc}"
            self._receipts.put(DispatchReceipt(
                request_key, selected, "STARTED", error=error))
            return DispatchOutcome(selected, True, False, bool(read_only),
                                   error=error)
        receipt = DispatchReceipt(request_key, selected, "COMMITTED", result=result)
        self._receipts.put(receipt)
        return DispatchOutcome(selected, True, True, False, result=result)
