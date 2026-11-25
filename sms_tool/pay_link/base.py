"""base submodule of the former payment/link_manager.py (mechanical split, bodies unchanged)."""

from __future__ import annotations
import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping
from pathlib import Path
from ..config import current_config_data, resolve_runtime_config
from ..kernel.paths import project_path, runtime_file
from ..payment.catalog import (
    PAYMENT_METHODS as CATALOG_METHODS,
    offline_method_error,
)
from ..kernel.sanitizer import sanitize as _canonical_sanitize, sanitize_text as _canonical_sanitize_text


def _offline_refusal(method: str, *, probe_only: bool = False) -> ValueError | None:
    return offline_method_error(method, probe_only=probe_only)


def _config_data(runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if runtime_config is not None:
        return resolve_runtime_config(runtime_config).data
    return current_config_data()


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
    """Delegate to :func:`sms_tool.payment.contracts.as_bool`.

    The two-sided spelling parser used to be copied here and in
    ``payment.contracts``; the copies agreed, but only because nobody had edited
    one. The private name stays because ``pay_link.__all__`` exports it and
    ``normalize`` / ``adapters`` import it.

    The import is function-local on purpose: ``pay_link -> payment`` is an
    already-frozen facade edge, and a module-level import of one more
    ``payment`` module would grow the layer ratchet for a one-line helper. The
    delayed-import ratchet records this edge with its reason instead.
    """
    from ..payment.contracts import as_bool

    return as_bool(value)


def safe_int(value: Any, default: int = 0) -> int:
    """``int(value)`` that never raises (missing/garbage -> ``default``).

    Adapter options come from CLI/store/config and can be absent or
    non-numeric; raising there loses the whole run to a ValueError.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """``float(value)`` that never raises (missing/garbage -> ``default``)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_true(value: Any) -> bool:
    """True only when ``value`` is the bool ``True`` (not 1/"yes")."""
    return isinstance(value, bool) and value


def is_false(value: Any) -> bool:
    """True only when ``value`` is the bool ``False`` (not 0/None/"")."""
    return isinstance(value, bool) and not value


def _select_kwargs(values: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in allowed and value is not None}


def _protocol_cfg(runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    source = _config_data(runtime_config)
    value = source.get("protocol_payments")
    return value if isinstance(value, Mapping) else {}


def _state_path() -> Path:
    configured = str(_protocol_cfg().get("state_file") or "").strip()
    return project_path(configured) if configured else runtime_file(_config_data(), "payment_link_runs.jsonl")


def _tail(text: str, limit: int = 1200) -> str:
    value = str(text or "").strip()
    return value[-limit:]


def _mask_ba_token(token: str) -> str:
    return "[REDACTED]" if token else ""


def _redact_sensitive_text(value: str) -> str:
    return _canonical_sanitize_text(value)


def _redact_sensitive_values(value: Any) -> Any:
    """Mask credentials anywhere inside a persisted payment-run value.

    ``ba_token`` 键本身已被 :func:`_persist_run` 的键名过滤丢弃，但 approve URL
    （如 ``.../agreements/approve?ba_token=BA-...``）会以 ``url``/``fallback_url`` 字段
    保留，需按值脱敏后再落盘。日志和错误文本还可能包含 Bearer/JWT、代理认证或
    其他命名凭据，因此统一递归清洗。仅影响持久化记录，不改动返回给调用方的结果。
    """
    return _canonical_sanitize(value)


_LOGGER = logging.getLogger("sms_tool.payment.link_manager")


GOPAY_DEFAULT_APPROVE_COUNTRIES = ("JP", "TR")


PAYMENT_METHODS = {
    key: PaymentMethodSpec(
        key,
        definition.label,
        definition.country,
        definition.currency,
        definition.adapter,
        definition.script,
        definition.artifact_validator,
    )
    for key, definition in CATALOG_METHODS.items()
}


_STATE_LOCK = threading.Lock()
