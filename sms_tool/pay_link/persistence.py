"""persistence submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

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

from .base import _STATE_LOCK, _redact_sensitive_values


def _persist_run(result: dict[str, Any]) -> None:
    path = _plm._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {}
    for key, value in result.items():
        lowered = key.lower()
        # Key 黑名单：原始子进程输出、token、proxy 已知不能落盘；
        # card_* / card_last4 / pan 同样是敏感凭据 —— 浏览器支付路径
        # 会把卡号末四位塞进返回 dict，这里按 key 名拦截，避免进 jsonl。
        if (
            lowered in {"raw_output", "raw_output_tail"}
            or "token" in lowered
            or "proxy" in lowered
            or lowered.startswith("card_")
            or lowered in {"card", "pan", "cardnumber", "card_number"}
        ):
            continue
        record[key] = value
    record = payment_history_metadata(record)
    record = _redact_sensitive_values(record)
    with _STATE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")



def _safe_persist_run(result: dict[str, Any]) -> None:
    try:
        _plm._persist_run(result)
    except (OSError, TypeError, ValueError) as exc:
        result["persistence_warning"] = f"payment run state was not persisted: {type(exc).__name__}"

