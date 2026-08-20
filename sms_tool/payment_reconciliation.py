"""Method-neutral payment reconciliation facade."""
from __future__ import annotations

from typing import Any, Mapping

from .payment_catalog import PAYMENT_METHODS, normalize_payment_method


def reconcile_payment_result(method: str, source: Any, *, transport: Any = None) -> dict[str, Any]:
    key = normalize_payment_method(method, default_for_blank=False)
    definition = PAYMENT_METHODS.get(key)
    if definition is None:
        return {"classification": "failed", "outcome": "unknown", "retryable": False,
                "requires_reconciliation": True, "error_code": "unsupported_payment_method"}
    if definition.reconciliation_policy == "paypal_return":
        from .paypal_reconciliation import reconcile_paypal_return
        result = reconcile_paypal_return(source, transport=transport)
        payload = result.to_dict()
        payload["requires_reconciliation"] = payload.get("classification") == "unknown"
        return payload
    if isinstance(source, Mapping):
        status = str(source.get("status") or source.get("outcome") or "unknown").strip().lower()
        artifact = bool(source.get("url") or source.get("qr_data") or source.get("qr_path"))
        if status in {"succeeded", "completed", "success"} and artifact:
            return {"classification": "conclusive", "outcome": "succeeded", "retryable": False,
                    "requires_reconciliation": False, "final_stage": "artifact"}
        if status in {"failed", "cancelled", "canceled"}:
            return {"classification": "conclusive", "outcome": "cancelled" if "cancel" in status else "failed",
                    "retryable": False, "requires_reconciliation": False, "final_stage": str(source.get("stage") or "")}
    return {"classification": "unknown", "outcome": "unknown", "retryable": False,
            "requires_reconciliation": True, "error_code": "payment_outcome_unknown"}


__all__ = ["reconcile_payment_result"]
