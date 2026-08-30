"""normalize submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

from __future__ import annotations
import sms_tool.payment_link_manager as _plm
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from ..config import ConfigError, current_config_data, resolve_runtime_config, validate_config
from ..paths import project_path, runtime_file
from ..payment_contracts import PaymentRequest, PaymentResult, payment_history_metadata
from ..payment_catalog import PAYMENT_METHODS as CATALOG_METHODS, normalize_payment_method as normalize_catalog_payment_method, validate_catalog_consistency
from ..payment_adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from ..payment_executor import PaymentExecutionRequest, PaymentFlowExecutor
from ..payment_operation import PaymentOperationConflict, PaymentOperationStore, conflict_result as payment_operation_conflict_result
from ..payment_routing import PaymentRoutePlan, PaymentRoutePlanner, coerce_approve_country as canonical_coerce_approve_country, parse_proxy_pool, payment_proxy_pools as canonical_payment_proxy_pools
from ..sanitizer import sanitize as _canonical_sanitize, sanitize_text as _canonical_sanitize_text
from .. import payment_egress

from .base import PaymentMethodSpec, _as_bool


def _normalize_result(spec: PaymentMethodSpec, result: Any) -> dict[str, Any]:
    is_mapping = isinstance(result, dict)
    data = dict(result) if is_mapping else {
        "ok": False,
        "error": str(result),
        "error_code": "invalid_adapter_result",
        "error_stage": "adapter_contract",
    }
    if is_mapping and not data and "ok" not in data:
        data.update({
            "ok": False,
            "error": f"{spec.label} extractor returned an invalid result contract",
            "error_code": "invalid_adapter_result",
            "error_stage": "adapter_contract",
        })
    data.setdefault("payment_method", spec.key)
    data.setdefault("method", spec.key)
    data.setdefault("target_country", spec.country)
    data.setdefault("currency", spec.currency)
    data.setdefault("link_type", f"{spec.key}_protocol")
    if not data.get("url"):
        data["url"] = data.get("long_url") or data.get("provider_redirect_url") or data.get("checkout_url") or data.get("upi_uri") or ""
    data.setdefault("operation", "extract_link")
    completed_payment = (
        spec.key == "blik"
        and str(data.get("status") or "").lower() == "completed"
        and data.get("operation") == "execute_payment"
        and data.get("link_type") == "blik_protocol_completed"
    )
    explicit_terminal = _explicit_terminal_state(data)
    if "ok" not in data:
        data["ok"] = False
        if not explicit_terminal:
            data.setdefault("error", f"{spec.label} extractor returned an invalid result contract")
            data.setdefault("error_code", "invalid_adapter_result")
            data.setdefault("error_stage", "adapter_contract")
    if explicit_terminal:
        data["ok"] = False
        data["status"] = explicit_terminal
        if explicit_terminal == "unknown":
            data.setdefault("requires_reconciliation", True)
        data.setdefault("error_code", {
            "cancelled": "payment_link_cancelled",
            "unknown": "payment_outcome_unknown",
            "timed_out": "payment_link_timed_out",
        }[explicit_terminal])
        data.setdefault("error", {
            "cancelled": f"{spec.label} extraction was cancelled",
            "unknown": f"{spec.label} extraction outcome is unknown",
            "timed_out": f"{spec.label} extraction timed out",
        }[explicit_terminal])
    capability_probe = data.get("operation") == "payment_method_capability_probe"
    validator = spec.artifact_validator
    artifact_ok = bool(data.get("url") or data.get("qr_data") or data.get("qr_path"))
    if validator in {"http_url", "paypal_ba_url", "provider_redirect", "checkout_url"} and data.get("url"):
        artifact_ok = str(data.get("url") or "").lower().startswith(("http://", "https://"))
    elif validator == "url_or_qr":
        artifact_ok = bool(data.get("url") or data.get("qr_data") or data.get("qr_path"))
    elif validator == "completion":
        artifact_ok = str(data.get("status") or "").lower() == "completed"
    if (
        data.get("ok")
        and not completed_payment
        and not capability_probe
        and not artifact_ok
    ):
        data["ok"] = False
        data["error"] = f"{spec.label} extractor returned no link or QR data"
        data["error_code"] = "adapter_result_missing_artifact"
        data["error_stage"] = "normalization"
    _normalize_error_contract(data)
    if explicit_terminal == "cancelled" and data.get("error_code") == "payment_link_extraction_failed":
        data["error_code"] = "payment_link_cancelled"
    elif explicit_terminal == "timed_out" and data.get("error_code") == "payment_link_extraction_failed":
        data["error_code"] = "payment_link_timed_out"
    return data



def _explicit_terminal_state(data: dict[str, Any]) -> str:
    """Return a non-success terminal state explicitly reported by an adapter."""
    if _as_bool(data.get("outcome_unknown")) is True or _as_bool(data.get("requires_reconciliation")) is True:
        return "unknown"

    for key in ("terminal_state", "state", "status", "outcome", "error_code", "error_type", "decision"):
        state = _canonical_terminal_state(data.get(key))
        if state:
            return state

    exit_code = data.get("exit_code")
    try:
        numeric_exit_code = int(exit_code)
    except (TypeError, ValueError):
        numeric_exit_code = 0
    if numeric_exit_code in {124}:
        return "timed_out"
    if numeric_exit_code in {-2, 130, -1073741510, 3221225786}:
        return "cancelled"

    status = _normalized_contract_value(data.get("status") or data.get("state"))
    has_artifact = bool(data.get("url") or data.get("qr_data") or data.get("qr_path"))
    if not data.get("ok") and not has_artifact and status in {
        "pending", "processing", "submitted", "requires_action", "awaiting_confirmation",
    }:
        return "unknown"
    return ""



def _canonical_terminal_state(value: Any) -> str:
    normalized = _normalized_contract_value(value)
    if normalized in {
        "cancelled", "canceled", "cancelled_by_user", "canceled_by_user", "interrupted",
        "keyboard_interrupt", "keyboardinterrupt",
    } or normalized.endswith("_cancelled") or normalized.endswith("_canceled"):
        return "cancelled"
    if normalized in {"timed_out", "timeout", "timeout_expired", "extractor_timeout"} or (
        normalized.endswith("_timed_out") or normalized.endswith("_timeout")
    ):
        return "timed_out"
    if normalized in {"unknown", "outcome_unknown", "payment_outcome_unknown", "indeterminate", "inconclusive"} or (
        normalized.endswith("_outcome_unknown")
    ):
        return "unknown"
    return ""



def _normalized_contract_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")



def _normalize_error_contract(data: dict[str, Any]) -> None:
    """Ensure every adapter result has stable retry and error-stage fields."""
    if data.get("ok"):
        data["retryable"] = False
        data["error_stage"] = ""
        return

    terminal_state = _explicit_terminal_state(data) or "failed"
    stage = data.get("error_stage") or data.get("stage") or data.get("failed_step")
    default_stage = "adapter_contract" if data.get("error_code") == "invalid_adapter_result" else "adapter"
    if data.get("error_code") in {"checkout_not_zero_due", "nonzero_offer", "paypal_payment_method_unavailable"}:
        default_stage = "eligibility"
    data["error_stage"] = str(stage or default_stage).strip() or default_stage
    data.setdefault("error", "payment-link extraction failed")
    data.setdefault("error_code", "payment_link_extraction_failed")

    explicit_retryable = _as_bool(data.get("retryable"))
    if explicit_retryable is None:
        explicit_retryable = _as_bool(data.get("retry_safe"))
    if terminal_state in {"cancelled", "unknown"}:
        data["retryable"] = False
    elif explicit_retryable is not None:
        data["retryable"] = explicit_retryable
    elif terminal_state == "timed_out":
        data["retryable"] = True
    else:
        data["retryable"] = _is_retryable_failure(data)



def _is_retryable_failure(data: dict[str, Any]) -> bool:
    try:
        status_code = int(data.get("status_code") or data.get("http_status") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code == 429 or 500 <= status_code <= 599:
        return True
    code = _normalized_contract_value(data.get("error_code") or data.get("error_type"))
    retryable_codes = {
        "connection_error", "connect_timeout", "read_timeout", "network_error",
        "proxy_error", "proxy_unavailable", "rate_limited", "service_unavailable",
    }
    return code in retryable_codes



def _result_terminal_state(data: dict[str, Any]) -> str:
    return "completed" if data.get("ok") else (_explicit_terminal_state(data) or "failed")



def _classify_exception(exc: Exception) -> tuple[str, str, bool]:
    explicit_state = _canonical_terminal_state(
        getattr(exc, "status", "") or getattr(exc, "terminal_state", "")
    )
    custom_code = str(
        getattr(exc, "error_code", "")
        or getattr(exc, "code", "")
        or ""
    )
    if explicit_state:
        default_code = {
            "cancelled": "payment_link_cancelled",
            "unknown": "payment_outcome_unknown",
            "timed_out": "payment_link_timed_out",
        }[explicit_state]
        explicit_retryable = _as_bool(getattr(exc, "retryable", None))
        if explicit_state in {"cancelled", "unknown"}:
            explicit_retryable = False
        elif explicit_retryable is None:
            explicit_retryable = explicit_state == "timed_out"
        return explicit_state, custom_code or default_code, bool(explicit_retryable)
    if _as_bool(getattr(exc, "outcome_unknown", None)) is True:
        return "unknown", custom_code or "payment_outcome_unknown", False
    names = {_normalized_contract_value(cls.__name__) for cls in type(exc).mro()}
    if names & {"cancellederror", "cancelled_error", "canceled_error"}:
        return "cancelled", "payment_link_cancelled", False
    if isinstance(exc, (_plm.subprocess.TimeoutExpired, TimeoutError)) or any("timeout" in name for name in names):
        return "timed_out", "payment_link_timed_out", True
    retryable = _as_bool(getattr(exc, "retryable", None)) is True
    return "failed", custom_code or "payment_link_manager_failed", retryable

