"""base submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

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


def _config_data(runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if runtime_config is not None:
        return resolve_runtime_config(runtime_config).data
    if CFG:
        merged = dict(_plm.current_config_data())
        merged.update(CFG)
        return merged
    return _plm.current_config_data()



@dataclass(frozen=True)
class PaymentMethodSpec:
    key: str
    label: str
    country: str
    currency: str
    adapter: str
    script: str = ""
    artifact_validator: str = "http_url"



def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return None



def _manager_error_stage(state: str) -> str:
    return {
        "created": "validation",
        "validating": "validation",
        "preparing_proxy": "proxy_setup",
        "running": "adapter",
        "extracting": "normalization",
    }.get(state, "manager")



def _select_kwargs(values: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in allowed and value is not None}



def _protocol_cfg(runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    source = _config_data(runtime_config)
    value = source.get("protocol_payments")
    return value if isinstance(value, Mapping) else {}



def _reference_root(runtime_config: Mapping[str, Any] | None = None) -> Path:
    configured = _plm._protocol_cfg(runtime_config).get("reference_root") or "services/protocol-payment"
    return project_path(configured)



def _state_path() -> Path:
    configured = str(_plm._protocol_cfg().get("state_file") or "").strip()
    return project_path(configured) if configured else runtime_file(_config_data(), "payment_link_runs.jsonl")



def _last_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index in reversed([i for i, char in enumerate(text) if char == "{"]):
        try:
            value, end = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(value, dict) and not text[index + end :].strip():
            return value
    return {}



def _tail(text: str, limit: int = 1200) -> str:
    value = str(text or "").strip()
    return value[-limit:]



def _blik_completion(stdout: str) -> dict[str, Any]:
    """Parse the BLIK auto-submit completion sentinel from stdout.

    BLIK 自动提交模式完成支付后没有可分享 URL，成功信号是 ``print_result_url`` 打印的
    ``BLIK_RESULT:{...}`` 结构化行（status=completed）。返回最后一个完成哨兵，否则空 dict。
    """
    for raw in reversed(_BLIK_RESULT_RE.findall(stdout or "")):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if (
            isinstance(value, dict)
            and value.get("ok") is True
            and str(value.get("payment_method") or "").lower() == "blik"
            and str(value.get("status") or "").lower() == "completed"
            and value.get("link_type") == "blik_protocol_completed"
        ):
            return value
    return {}



def _mask_ba_token(token: str) -> str:
    return "[REDACTED]" if token else ""



def _redact_sensitive_text(value: str) -> str:
    return _canonical_sanitize_text(value)



def _redact_sensitive_values(value: Any) -> Any:
    """Mask credentials anywhere inside a persisted payment-run value.

    ``ba_token`` 键本身已被 :func:`_plm._persist_run` 的键名过滤丢弃，但 approve URL
    （如 ``.../agreements/approve?ba_token=BA-...``）会以 ``url``/``fallback_url`` 字段
    保留，需按值脱敏后再落盘。日志和错误文本还可能包含 Bearer/JWT、代理认证或
    其他命名凭据，因此统一递归清洗。仅影响持久化记录，不改动返回给调用方的结果。
    """
    return _canonical_sanitize(value)


CFG: dict[str, Any] = {}



_LOGGER = logging.getLogger("sms_tool.payment_link_manager")



GOPAY_DEFAULT_APPROVE_COUNTRIES = ("JP", "TR")



PAYMENT_METHODS = {
    key: PaymentMethodSpec(
        key,
        definition.label,
        definition.country,
        definition.currency,
        {"native_paypal": "native", "native_upi": "native"}.get(definition.adapter, definition.adapter),
        definition.script,
        definition.artifact_validator,
    )
    for key, definition in CATALOG_METHODS.items()
}



_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "unknown", "timed_out"})



_NON_SUCCESS_TERMINAL_STATES = _TERMINAL_STATES - {"completed"}



_TRANSITIONS = {
    "created": {"validating"} | _NON_SUCCESS_TERMINAL_STATES,
    "validating": {"preparing_proxy"} | _NON_SUCCESS_TERMINAL_STATES,
    "preparing_proxy": {"running"} | _NON_SUCCESS_TERMINAL_STATES,
    "running": {"extracting"} | _NON_SUCCESS_TERMINAL_STATES,
    "extracting": set(_TERMINAL_STATES),
    **{state: set() for state in _TERMINAL_STATES},
}



_STATE_LOCK = threading.Lock()



_BLIK_RESULT_RE = re.compile(r"BLIK_RESULT:(\{.*\})")



_DIRECT_CARD_CURRENCY = {
    "PH": "PHP", "US": "USD", "GB": "GBP", "JP": "JPY", "DE": "EUR", "FR": "EUR",
    "IE": "EUR", "NL": "EUR", "AU": "AUD", "CA": "CAD", "SG": "SGD", "IN": "INR",
    "TR": "TRY", "BR": "BRL", "KR": "KRW", "PL": "PLN", "CH": "CHF", "VN": "VND",
    "NZ": "NZD",
}

