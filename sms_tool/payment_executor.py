"""Common protocol-payment execution state machine."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .payment_contracts import PaymentResult
from .payment_flow import PaymentStage
from .payment_routing import PaymentRoutePlan


class PaymentExecutionState(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    ROUTING = "preparing_proxy"
    RUNNING = "running"
    NORMALIZING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed_out"


_TERMINAL = {
    PaymentExecutionState.COMPLETED.value,
    PaymentExecutionState.FAILED.value,
    PaymentExecutionState.CANCELLED.value,
    PaymentExecutionState.UNKNOWN.value,
    PaymentExecutionState.TIMED_OUT.value,
}


@dataclass(frozen=True)
class PaymentExecutionRequest:
    payment_method: str
    access_token: str
    route_plan: PaymentRoutePlan
    auth_context: Mapping[str, Any]
    runtime_config: Mapping[str, Any]
    options: Mapping[str, Any]
    operation: str = "extract_link"
    operation_id: str = ""
    idempotency_key_hash: str = ""


class PaymentFlowExecutor:
    def __init__(
        self,
        adapter_runner: Callable[[PaymentExecutionRequest], Mapping[str, Any]],
        *,
        normalizer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        exception_classifier: Callable[[Exception], tuple[str, str, bool]] | None = None,
        error_sanitizer: Callable[[str], str] | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.adapter_runner = adapter_runner
        self.normalizer = normalizer or (lambda value: value)
        self.exception_classifier = exception_classifier
        self.error_sanitizer = error_sanitizer or (lambda value: value)
        self.progress = progress

    def run(self, request: PaymentExecutionRequest) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        operation_id = str(request.operation_id or run_id).strip()
        history: list[dict[str, Any]] = []

        def move(state: str, stage: str, message: str = "") -> None:
            event = {
                "operation_id": operation_id,
                "state": state,
                "stage": stage,
                "at": int(time.time()),
                "message": message,
            }
            history.append(event)
            if self.progress:
                self.progress({**event, "run_id": run_id, "method": request.payment_method})

        move(PaymentExecutionState.CREATED.value, "created", "payment run created")
        try:
            move(PaymentExecutionState.VALIDATING.value, "validation", "validating request")
            if not request.payment_method:
                raise ValueError("payment_method is required")
            if not str(request.access_token or "").strip():
                raise ValueError("access_token is required")
            move(PaymentExecutionState.ROUTING.value, "routing", "payment routes prepared")
            move(PaymentExecutionState.RUNNING.value, "adapter", "adapter execution started")
            raw = dict(self.adapter_runner(request) or {})
            move(PaymentExecutionState.NORMALIZING.value, PaymentStage.ARTIFACT.value, "normalizing adapter result")
            normalized = dict(self.normalizer(raw) or {})
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            normalized = {
                "ok": False,
                "status": "cancelled",
                "error": self.error_sanitizer(str(exc)) or "payment execution cancelled",
                "error_code": "payment_link_cancelled",
                "error_stage": history[-1]["stage"] if history else "executor",
                "retryable": False,
            }
        except Exception as exc:
            classified = self.exception_classifier(exc) if self.exception_classifier else None
            status = str(
                getattr(exc, "status", "")
                or (classified[0] if classified else "failed")
            ).strip().lower()
            if status not in {"failed", "cancelled", "unknown", "timed_out"}:
                status = "failed"
            normalized = {
                "ok": False,
                "status": status,
                "error": self.error_sanitizer(str(exc)) or type(exc).__name__,
                "error_code": str(
                    getattr(exc, "error_code", "")
                    or (classified[1] if classified else "payment_link_extraction_failed")
                ),
                "error_stage": str(
                    getattr(exc, "error_stage", "")
                    or getattr(exc, "stage", "")
                    or (history[-1]["stage"] if history else "executor")
                ),
                "retryable": bool(
                    getattr(exc, "retryable", classified[2] if classified else False)
                ),
            }
            if status == "unknown":
                normalized["requires_reconciliation"] = True

        contract = PaymentResult.from_mapping(normalized, payment_method=request.payment_method)
        result = contract.to_dict()
        terminal = contract.outcome.status
        if terminal not in _TERMINAL:
            terminal = "completed" if contract.ok else "failed"
            if not result.get("status"):
                result["status"] = terminal
        move(terminal, str(result.get("error_stage") or PaymentStage.ARTIFACT.value), "payment run finished")
        result.update({
            "run_id": run_id,
            "operation_id": operation_id,
            "idempotency_key_hash": request.idempotency_key_hash,
            "manager_state": terminal,
            "state_history": history,
            "flow_profile": request.route_plan.flow_profile,
            "route_plan": request.route_plan.public_dict(),
        })
        for record in request.route_plan.coercions:
            field = str(record.get("field") or "").strip()
            if field:
                result[field] = record.get("coerced")
                result[f"{field}_original"] = record.get("original")
                result[f"{field}_coerced"] = True
        return result


__all__ = [
    "PaymentExecutionRequest",
    "PaymentExecutionState",
    "PaymentFlowExecutor",
]
