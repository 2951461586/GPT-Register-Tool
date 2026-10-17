"""Pure setup helpers for payment batch orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .payment_link_manager import parse_proxy_pool


def apply_configured_stage_pools(
    base_kwargs: dict[str, Any],
    *,
    protocol_config: Mapping[str, Any],
    legacy_config: Mapping[str, Any] | None = None,
    method: str,
    proxy: Any = None,
) -> dict[str, Any]:
    """Return batch kwargs with method-owned checkout/approve pools applied."""
    result = dict(base_kwargs)
    if result.get("transport") is not None or proxy:
        return result
    _raw_methods: object = protocol_config.get("methods")
    methods: Mapping = _raw_methods if isinstance(_raw_methods, Mapping) else {}
    _raw_method_config: object = methods.get(method)
    method_config: Mapping = _raw_method_config if isinstance(_raw_method_config, Mapping) else {}
    _raw_legacy_value: object = legacy_config.get(method) if isinstance(legacy_config, Mapping) else None
    legacy: Mapping = _raw_legacy_value if isinstance(_raw_legacy_value, Mapping) else {}
    for stage in ("checkout", "approve"):
        pool_key = f"{stage}_proxy_pool"
        if parse_proxy_pool(result.get(pool_key)):
            continue
        configured = method_config.get(pool_key) or legacy.get(pool_key)
        if parse_proxy_pool(configured):
            result[pool_key] = configured
            result.pop(f"{stage}_proxy", None)
    return result
