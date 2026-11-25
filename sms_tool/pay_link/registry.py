"""Payment adapter registry backed by the external Paylink Extraction Server."""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..payment.adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from ..payment.catalog import (
    PAYMENT_METHODS as CATALOG_METHODS,
    normalize_payment_method as normalize_catalog_payment_method,
    validate_catalog_consistency,
)
from ..payment.routing import (
    PaymentRoutePlan,
    PaymentRoutePlanner,
    parse_proxy_pool,
    payment_proxy_pools as canonical_payment_proxy_pools,
)
from .base import (
    GOPAY_DEFAULT_APPROVE_COUNTRIES,
    PAYMENT_METHODS,
    _LOGGER,
    _config_data,
    _protocol_cfg,
)
from .extract_server import extract_link_via_server, resolve_extract_server_config
from . import team_trial as _team_trial


def probe_team_trial_eligibility(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Dispatch to ``team_trial.probe_team_trial_eligibility`` via the module seam."""
    return _team_trial.probe_team_trial_eligibility(*args, **kwargs)


def build_default_payment_registry() -> PaymentAdapterRegistry:
    """Build and validate the complete adapter composition for the catalog.

    Regional wallet / PayPal methods route through the external Paylink
    Extraction Server adapter, while ``team_trial`` executes natively in-process
    against ChatGPT's hosted Team Checkout endpoint.
    """
    registry = PaymentAdapterRegistry()

    def extract_server_runner(
        *,
        access_token: str,
        proxy: Any = None,
        auth_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        method = str(kwargs.pop("payment_method", "") or "").strip().lower()
        runtime_config = kwargs.pop("runtime_config", None)
        if method == "blik":
            blik_code = str(kwargs.get("blik_code") or "").strip()
            if not re.fullmatch(r"\d{6}", blik_code):
                return {
                    "ok": False,
                    "status": "failed",
                    "payment_method": "blik",
                    "error": "BLIK requires an explicit 6-digit code for this run",
                    "error_code": "blik_code_required",
                    "error_stage": "validation",
                    "retryable": False,
                }
        server_cfg = resolve_extract_server_config(runtime_config, options=kwargs)
        if server_cfg.mode == "off":
            return {
                "ok": False,
                "status": "failed",
                "payment_method": method,
                "source": "extract_link_server",
                "error": "extract link server is disabled (set EXTRACT_LINK_API_BASE and EXTRACT_LINK_CDK)",
                "error_code": "extract_server_disabled",
                "error_stage": "extract_server_config",
                "retryable": False,
            }
        if not server_cfg.applies_to(method):
            return {
                "ok": False,
                "status": "failed",
                "payment_method": method,
                "source": "extract_link_server",
                "error": f"payment method {method!r} is not enabled on extract_link_server.methods",
                "error_code": "extract_server_method_not_allowed",
                "error_stage": "extract_server_config",
                "retryable": False,
            }
        return extract_link_via_server(
            method,
            access_token,
            proxy=str(proxy or "").strip(),
            auth_context=auth_context,
            runtime_config=runtime_config,
            options=kwargs,
            config=server_cfg,
        )

    def team_trial_runner(
        *,
        access_token: str,
        proxy: Any = None,
        auth_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs.pop("payment_method", None)
        kwargs.pop("runtime_config", None)
        return _team_trial.extract_team_trial_link(
            access_token,
            proxy=str(proxy or "").strip(),
            auth_context=auth_context,
            timeout=kwargs.get("timeout", 30),
            workspace_name=str(kwargs.get("workspace_name") or _team_trial.TEAM_TRIAL_DEFAULT_WORKSPACE_NAME),
            seat_quantity=kwargs.get("seat_quantity", _team_trial.TEAM_TRIAL_DEFAULT_SEATS),
            target_country=str(kwargs.get("target_country") or ""),
            checkout_country=str(kwargs.get("checkout_country") or ""),
            currency=str(kwargs.get("currency") or ""),
            verify_eligibility=bool(kwargs.get("verify_eligibility", True)),
            checkout_ui_mode=str(kwargs.get("checkout_ui_mode") or "hosted"),
        )

    extract_server_methods = tuple(key for key, defn in CATALOG_METHODS.items() if defn.adapter == "extract_server")
    team_trial_methods = tuple(key for key, defn in CATALOG_METHODS.items() if defn.adapter == "team_trial_direct")
    registry.register(FunctionPaymentAdapter("extract_server", extract_server_methods, extract_server_runner))
    if team_trial_methods:
        registry.register(FunctionPaymentAdapter("team_trial_direct", team_trial_methods, team_trial_runner))
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
    registered = set(PAYMENT_ADAPTERS.methods())
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "country": spec.country,
            "currency": spec.currency,
            "adapter": spec.adapter,
            "available": spec.key in registered,
        }
        for spec in PAYMENT_METHODS.values()
    ]


def register_payment_adapter(adapter: Any) -> Any:
    """Register an adapter at the payment seam; useful for new methods/tests."""
    PAYMENT_ADAPTERS.register(adapter)
    return adapter


def allowed_approve_countries(payment_method: Any) -> tuple[str, ...]:
    """Return the approve-country allowlist for a method (empty = unconstrained)."""
    method = normalize_payment_method(payment_method)
    definition = CATALOG_METHODS.get(method)
    if definition is not None and definition.approve_countries:
        return tuple(definition.approve_countries)
    if method == "gopay":
        return GOPAY_DEFAULT_APPROVE_COUNTRIES
    return ()


def coerce_approve_country(payment_method: Any, country: Any) -> tuple[str, bool]:
    """Enforce the GoPay approve-country protocol rule."""
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
    approve_input = str(configured_countries.get("approve") or values.get("approve_country") or "").strip().upper()
    pre_coercions: list[dict[str, Any]] = []
    if approve_input:
        approve_country, changed = coerce_approve_country(method, approve_input)
        if changed:
            configured_countries["approve"] = approve_country
            values["stage_proxy_countries"] = configured_countries
            if str(values.get("approve_country") or "").strip():
                values["approve_country"] = approve_country
            pre_coercions.append(
                {
                    "field": "approve_country",
                    "original": approve_input,
                    "coerced": approve_country,
                }
            )
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
    raw = _protocol_cfg(runtime_config).get("enabled_methods")
    if isinstance(raw, str):
        values = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return set(PAYMENT_METHODS)
    return {method for value in values if (method := normalize_payment_method(value))}


PAYMENT_ADAPTERS = build_default_payment_registry()

__all__ = [
    "PAYMENT_ADAPTERS",
    "allowed_approve_countries",
    "build_default_payment_registry",
    "coerce_approve_country",
    "normalize_payment_method",
    "parse_proxy_pool",
    "payment_method_label",
    "payment_proxy_pools",
    "register_payment_adapter",
    "supported_payment_methods",
]
