"""registry submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

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

from .adapters import _run_direct_card, _run_gcash_adapter, _run_momo, _run_protocol_script, _run_regional_wallet_adapter
from .base import GOPAY_DEFAULT_APPROVE_COUNTRIES, PAYMENT_METHODS, _LOGGER, _config_data, _reference_root, _select_kwargs


def build_default_payment_registry() -> PaymentAdapterRegistry:
    """Build and validate the complete adapter composition for the catalog."""
    registry = PaymentAdapterRegistry()

    def methods_for(adapter_key: str) -> tuple[str, ...]:
        return tuple(
            key for key, definition in _plm.CATALOG_METHODS.items()
            if definition.adapter == adapter_key
        )

    def paypal_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        from ..gen_pp_link import generate_pp_link
        runtime_config = kwargs.pop("runtime_config", None)
        kwargs.pop("payment_method", None)
        return generate_pp_link(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            paypal_generation_type=kwargs.pop("paypal_generation_type", None),
            runtime_config=runtime_config,
            **_select_kwargs(kwargs, {
                "checkout_proxy", "provider_proxy", "stripe_init_proxy", "payment_method_proxy",
                "confirm_proxy", "approve_proxy", "promotion_proxy", "target_country",
                "checkout_country", "require_zero", "require_ba_token", "stage_proxy_countries",
                "max_checkout_retries", "max_stage_retries",
            }),
        )

    def upi_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        from ..gen_pp_link import generate_upi_qr_link
        runtime_config = kwargs.pop("runtime_config", None)
        kwargs.pop("payment_method", None)
        return generate_upi_qr_link(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            runtime_config=runtime_config,
            **_select_kwargs(kwargs, {
                "checkout_proxy", "provider_proxy", "approve_proxy", "target_country",
                "checkout_country", "payment_country", "require_zero", "qr_path",
            }),
        )

    def wallet_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return _plm._run_wallet_adapter(PAYMENT_METHODS[str(kwargs.pop("payment_method"))], access_token, proxy=proxy, auth_context=auth_context, **kwargs)

    def gcash_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("payment_method", None)
        return _run_gcash_adapter(PAYMENT_METHODS["gcash"], access_token, proxy=proxy, auth_context=auth_context, **kwargs)

    def script_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        spec = PAYMENT_METHODS[str(kwargs.pop("payment_method"))]
        return _run_protocol_script(spec, access_token, proxy=proxy, **kwargs)

    def direct_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return _run_direct_card(PAYMENT_METHODS["direct_card"], access_token, proxy=proxy, **kwargs)

    def momo_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return _run_momo(PAYMENT_METHODS["momo"], access_token, proxy=proxy, **kwargs)

    def regional_wallet_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        method = str(kwargs.pop("payment_method"))
        return _run_regional_wallet_adapter(
            PAYMENT_METHODS[method],
            access_token,
            proxy=proxy,
            auth_context=auth_context,
            **kwargs,
        )

    registry.register(FunctionPaymentAdapter("native_paypal", methods_for("native_paypal"), paypal_runner))
    registry.register(FunctionPaymentAdapter("native_upi", methods_for("native_upi"), upi_runner))
    registry.register(FunctionPaymentAdapter("wallet", methods_for("wallet"), wallet_runner))
    registry.register(FunctionPaymentAdapter("gcash_custom", methods_for("gcash_custom"), gcash_runner))
    registry.register(FunctionPaymentAdapter("script", methods_for("script"), script_runner))
    registry.register(FunctionPaymentAdapter("direct_card", methods_for("direct_card"), direct_runner))
    registry.register(FunctionPaymentAdapter("momo", methods_for("momo"), momo_runner))
    registry.register(FunctionPaymentAdapter("regional_wallet", methods_for("regional_wallet"), regional_wallet_runner))
    registry.validate_methods(set(PAYMENT_METHODS))
    validate_catalog_consistency(adapter_methods=set(registry.methods()))
    return registry



def normalize_payment_method(value: Any) -> str:
    method = normalize_catalog_payment_method(value)
    return method if method in PAYMENT_METHODS else ""



def payment_proxy_pools(
    payment_method: Any,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Read method-owned Checkout and Approve proxy pools from configuration."""
    return canonical_payment_proxy_pools(_config_data(runtime_config), payment_method)



def payment_method_label(value: Any) -> str:
    method = normalize_payment_method(value)
    return PAYMENT_METHODS[method].label if method else str(value or "")



def supported_payment_methods() -> list[dict[str, Any]]:
    root = _reference_root()
    registered = set(PAYMENT_ADAPTERS.methods())
    output = []
    for spec in PAYMENT_METHODS.values():
        available = spec.key in registered and (not spec.script or (root / spec.script).is_file())
        output.append({
            "key": spec.key,
            "label": spec.label,
            "country": spec.country,
            "currency": spec.currency,
            "adapter": spec.adapter,
            "available": available,
        })
    return output



def register_payment_adapter(adapter: Any) -> Any:
    """Register an adapter at the payment seam; useful for new methods/tests."""
    PAYMENT_ADAPTERS.register(adapter)
    return adapter



def allowed_approve_countries(payment_method: Any) -> tuple[str, ...]:
    """Return the approve-country allowlist for a method (empty = unconstrained).

    The catalog ``approve_countries`` value wins; GoPay falls back to the
    historical JP/TR default when the catalog does not constrain it.
    """
    method = normalize_payment_method(payment_method)
    definition = _plm.CATALOG_METHODS.get(method)
    if definition is not None and definition.approve_countries:
        return tuple(definition.approve_countries)
    if method == "gopay":
        return GOPAY_DEFAULT_APPROVE_COUNTRIES
    return ()



def coerce_approve_country(payment_method: Any, country: Any) -> tuple[str, bool]:
    """Enforce the GoPay approve-country protocol rule.

    Returns ``(effective_country, coerced)``.  Only GoPay is constrained: an
    explicit approve country outside the allowlist (catalog
    ``approve_countries``, falling back to JP/TR) is forced to JP, or to the
    first allowed entry when JP itself is not allowed.  Other methods and
    blank values pass through unchanged so their existing defaults apply.
    """
    method = normalize_payment_method(payment_method)
    value = str(country or "").strip().upper()
    if method != "gopay" or not value:
        return value, False
    allowed = allowed_approve_countries(method)
    if not allowed or value in allowed:
        return value, False
    coerced = "JP" if "JP" in allowed else allowed[0]
    _LOGGER.warning(
        "payment method %s approve country %s is not in the allowed set (%s); coerced to %s",
        method,
        value,
        ",".join(allowed),
        coerced,
    )
    return coerced, True



def _resolve_proxy_pool_routes(
    method: str,
    proxy: Any,
    kwargs: Mapping[str, Any],
    runtime_config: Mapping[str, Any] | None = None,
    *,
    coercion_records: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Compatibility wrapper around the canonical payment route planner."""
    values = dict(kwargs)
    source = _config_data(runtime_config)
    configured_countries = values.get("stage_proxy_countries")
    configured_countries = dict(configured_countries) if isinstance(configured_countries, Mapping) else {}
    approve_input = str(
        configured_countries.get("approve") or values.get("approve_country") or ""
    ).strip().upper()
    pre_coercions: list[dict[str, Any]] = []
    if approve_input:
        approve_country, changed = coerce_approve_country(method, approve_input)
        if changed:
            configured_countries["approve"] = approve_country
            values["stage_proxy_countries"] = configured_countries
            if str(values.get("approve_country") or "").strip():
                values["approve_country"] = approve_country
            pre_coercions.append({
                "field": "approve_country",
                "original": approve_input,
                "coerced": approve_country,
            })
    supplied = values.get("payment_route_plan")
    if isinstance(supplied, PaymentRoutePlan):
        plan = supplied
    else:
        plan = PaymentRoutePlanner(source).plan(
            method,
            options=values,
            default_proxy=proxy,
        )
    if plan.payment_method != method:
        raise ValueError(f"payment route plan method mismatch: {plan.payment_method} != {method}")

    countries_supplied = isinstance(values.get("stage_proxy_countries"), Mapping)
    routed = {**values, **plan.to_adapter_options()}
    routed.pop("checkout_proxy_pool", None)
    routed.pop("approve_proxy_pool", None)
    routed.pop("stage_proxy_pools", None)
    routed.pop("stage_routes", None)
    if not countries_supplied and not plan.coercions:
        routed.pop("stage_proxy_countries", None)

    records = [*pre_coercions, *plan.coercions]
    for record in records:
        if coercion_records is not None:
            coercion_records.append(dict(record))
        original = str(record.get("original") or "")
        coerced = str(record.get("coerced") or "")
        if record not in pre_coercions:
            _LOGGER.warning(
                "payment method %s approve country %s is not in the allowed set; coerced to %s",
                method,
                original,
                coerced,
            )
        countries = dict(routed.get("stage_proxy_countries") or {})
        countries["approve"] = coerced
        routed["stage_proxy_countries"] = countries
        if str(values.get("approve_country") or "").strip():
            routed["approve_country"] = coerced
    return plan.checkout_proxy or proxy, routed



def _enabled_methods(runtime_config: Mapping[str, Any] | None = None) -> set[str]:
    raw = _plm._protocol_cfg(runtime_config).get("enabled_methods")
    if isinstance(raw, str):
        values = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return set(PAYMENT_METHODS)
    return {method for value in values if (method := normalize_payment_method(value))}


PAYMENT_ADAPTERS = build_default_payment_registry()

