"""Unified state machine facade for payment-link extraction.

Thin PEP 562 lazy compatibility shell re-exporting ``sms_tool.pay_link``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from ..config import current_config_data  # noqa: F401 - patch target
from . import egress as payment_egress  # noqa: F401 - patch target (``manager.payment_egress``)

if TYPE_CHECKING:
    from ..pay_link import (  # noqa: F401
        GOPAY_DEFAULT_APPROVE_COUNTRIES,
        PAYMENT_ADAPTERS,
        PAYMENT_METHODS,
        ExtractLinkServerConfig,
        PaymentMethodSpec,
        _LOGGER,
        _STATE_LOCK,
        _as_bool,
        _canonical_terminal_state,
        _classify_exception,
        _config_data,
        _enabled_methods,
        _explicit_terminal_state,
        _is_retryable_failure,
        _mask_ba_token,
        _normalize_error_contract,
        _normalize_result,
        _normalized_contract_value,
        _persist_run,
        _protocol_cfg,
        _redact_sensitive_text,
        _redact_sensitive_values,
        _resolve_proxy_pool_routes,
        _result_terminal_state,
        _safe_persist_run,
        _select_kwargs,
        _state_path,
        _tail,
        allowed_approve_countries,
        build_default_payment_registry,
        coerce_approve_country,
        extract_link_via_server,
        extract_team_trial_link,
        generate_payment_link,
        normalize_payment_method,
        parse_proxy_pool,
        payment_method_label,
        payment_proxy_pools,
        probe_payment_method,
        probe_team_trial_eligibility,
        register_payment_adapter,
        resolve_extract_server_config,
        supported_payment_methods,
    )

_LAZY: dict[str, str] = {
    name: "pay_link"
    for name in (
        "_config_data",
        "PaymentMethodSpec",
        "_as_bool",
        "_select_kwargs",
        "_protocol_cfg",
        "_state_path",
        "_tail",
        "_mask_ba_token",
        "_redact_sensitive_text",
        "_redact_sensitive_values",
        "_normalize_result",
        "_explicit_terminal_state",
        "_canonical_terminal_state",
        "_normalized_contract_value",
        "_normalize_error_contract",
        "_is_retryable_failure",
        "_result_terminal_state",
        "_classify_exception",
        "_persist_run",
        "_safe_persist_run",
        "build_default_payment_registry",
        "normalize_payment_method",
        "payment_proxy_pools",
        "payment_method_label",
        "supported_payment_methods",
        "register_payment_adapter",
        "allowed_approve_countries",
        "coerce_approve_country",
        "parse_proxy_pool",
        "_resolve_proxy_pool_routes",
        "_enabled_methods",
        "generate_payment_link",
        "probe_payment_method",
        "probe_team_trial_eligibility",
        "ExtractLinkServerConfig",
        "extract_link_via_server",
        "extract_team_trial_link",
        "resolve_extract_server_config",
        "GOPAY_DEFAULT_APPROVE_COUNTRIES",
        "PAYMENT_ADAPTERS",
        "PAYMENT_METHODS",
        "_LOGGER",
        "_STATE_LOCK",
    )
}

__all__ = [
    "ExtractLinkServerConfig",
    "PaymentMethodSpec",
    "build_default_payment_registry",
    "extract_link_via_server",
    "extract_team_trial_link",
    "normalize_payment_method",
    "payment_proxy_pools",
    "payment_method_label",
    "supported_payment_methods",
    "register_payment_adapter",
    "resolve_extract_server_config",
    "allowed_approve_countries",
    "coerce_approve_country",
    "parse_proxy_pool",
    "generate_payment_link",
    "probe_payment_method",
    "probe_team_trial_eligibility",
    "GOPAY_DEFAULT_APPROVE_COUNTRIES",
    "PAYMENT_ADAPTERS",
    "PAYMENT_METHODS",
]


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module("..pay_link", __package__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
