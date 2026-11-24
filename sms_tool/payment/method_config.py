"""Pure payment-method config merger (Layer 5 foundation leaf).

Extracted from ``sms_tool.payment.routing`` so ``sms_tool.paypal.proxy`` can
resolve ``paypal`` config without forming an import cycle back into the Layer 4
routing planner.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .catalog import normalize_payment_method


def method_payment_config(config: Mapping[str, Any], payment_method: Any) -> dict[str, Any]:
    method = normalize_payment_method(payment_method, default_for_blank=False)
    _raw_legacy: object = config.get(method)
    legacy: Mapping = _raw_legacy if isinstance(_raw_legacy, Mapping) else {}
    _raw_protocol: object = config.get("protocol_payments")
    protocol: Mapping = _raw_protocol if isinstance(_raw_protocol, Mapping) else {}
    _raw_methods: object = protocol.get("methods")
    methods: Mapping = _raw_methods if isinstance(_raw_methods, Mapping) else {}
    _raw_canonical: object = methods.get(method)
    canonical: Mapping = _raw_canonical if isinstance(_raw_canonical, Mapping) else {}
    merged = {**dict(legacy), **dict(canonical)}
    routing_keys = ("stage_proxies", "stage_proxy_pools", "stage_routes", "stage_proxy_countries")
    canonical_owns_routing = any(isinstance(canonical.get(key), Mapping) for key in routing_keys)
    for key in ("stage_proxies", "stage_proxy_pools", "stage_routes"):
        _raw_old: object = legacy.get(key)
        old: Mapping = _raw_old if isinstance(_raw_old, Mapping) else {}
        _raw_new: object = canonical.get(key)
        new: Mapping = _raw_new if isinstance(_raw_new, Mapping) else {}
        if canonical_owns_routing:
            merged[key] = dict(new)
        elif old:
            merged[key] = dict(old)
    legacy_countries = legacy.get("stage_proxy_countries")
    canonical_countries = canonical.get("stage_proxy_countries")
    if canonical_owns_routing:
        merged["stage_proxy_countries"] = dict(canonical_countries) if isinstance(canonical_countries, Mapping) else {}
    elif isinstance(legacy_countries, Mapping):
        merged["stage_proxy_countries"] = dict(legacy_countries)
    return merged


__all__ = ["method_payment_config"]
