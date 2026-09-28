from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..paypal_proxy import _stage_proxy_value
except ImportError:
    from paypal_proxy import _stage_proxy_value  # type: ignore
from typing import Any
from collections.abc import Mapping
from collections.abc import Sequence
import json
import os
import re
from .constants import DEFAULT_CONFIG_PATH


def _load_json(path: str) -> dict:
    """Load a JSON object from disk, accepting UTF-8 files with or without BOM."""
    if os.path.abspath(path) == os.path.abspath(DEFAULT_CONFIG_PATH):
        from ..config import load_merged_config

        return load_merged_config()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
def _method_cfg(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    section = cfg.get(method) if isinstance(cfg.get(method), dict) else {}
    return section if isinstance(section, dict) else {}
def _payment_stage_proxies_from_config(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    method_cfg = _method_cfg(cfg, method)
    _method_stage = method_cfg.get("stage_proxies")
    method_stage: dict[str, Any] = _method_stage if isinstance(_method_stage, dict) else {}
    _paypal_raw = cfg.get("paypal")
    paypal_cfg: dict[str, Any] = _paypal_raw if isinstance(_paypal_raw, dict) else {}
    _paypal_stage = paypal_cfg.get("stage_proxies")
    paypal_stage: dict[str, Any] = _paypal_stage if isinstance(_paypal_stage, dict) else {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""

    def pick(key: str, fallback: str = "") -> str:
        value = _stage_proxy_value(method_stage, key)
        if value:
            return value
        return _stage_proxy_value(paypal_stage, key, fallback)

    checkout = pick("checkout", proxy_default)
    provider = pick("provider") or pick("stripe_init") or proxy_default
    approve = pick("approve") or pick("confirm") or provider or proxy_default
    return {"checkout": checkout, "provider": provider, "approve": approve}
def _upi_first_string(data: Any, keys: Sequence[str]) -> str:
    """First non-empty string among ``keys``; a nested dict may carry the value."""
    if not isinstance(data, Mapping):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, Mapping):
            for inner in ("id", "client_secret", "value"):
                candidate = value.get(inner)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
def _upi_payment_intent_id(value: Any) -> str:
    """Extract ``pi_...`` from a bare id or a ``pi_..._secret_...`` client secret."""
    text = str(value or "").strip()
    if text.startswith("pi_"):
        return text.split("_secret_", 1)[0]
    match = re.search(r"(pi_[A-Za-z0-9]+)", text)
    return match.group(1) if match else ""
def _upi_retarget_region(proxy: Any, country: Any) -> str:
    """Rewrite a known region-tagged proxy to ``country``; unknown stays as-is.

    Kept as a thin wrapper so this module never imports ``proxy_entry`` at
    module scope (the payment flow is import-lazy by design) and a provider
    that does not use region tags is left untouched.
    """
    value = str(proxy or "").strip()
    target = str(country or "").strip().upper()
    if not value or not target or len(target) != 2:
        return value
    try:
        from ..proxy_entry import retarget_region

        return str(retarget_region(value, target) or value)
    except Exception:
        return value
