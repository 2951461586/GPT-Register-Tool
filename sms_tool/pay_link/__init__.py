"""Re-exports of the former sms_tool.payment.link_manager module (PEP 562 lazy)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import (  # noqa: F401
        GOPAY_DEFAULT_APPROVE_COUNTRIES,
        PAYMENT_METHODS,
        PaymentMethodSpec,
        _LOGGER,
        _STATE_LOCK,
        _as_bool,
        _config_data,
        _mask_ba_token,
        _protocol_cfg,
        _redact_sensitive_text,
        _redact_sensitive_values,
        _select_kwargs,
        _state_path,
        _tail,
    )
    from .core import (  # noqa: F401
        generate_payment_link,
        probe_payment_method,
    )
    from .extract_server import (  # noqa: F401
        ExtractLinkServerConfig,
        extract_link_via_server,
        resolve_extract_server_config,
    )
    from .team_trial import (  # noqa: F401
        extract_team_trial_link,
        probe_team_trial_eligibility,
    )
    from .normalize import (  # noqa: F401
        _canonical_terminal_state,
        _classify_exception,
        _explicit_terminal_state,
        _is_retryable_failure,
        _normalize_error_contract,
        _normalize_result,
        _normalized_contract_value,
        _result_terminal_state,
    )
    from .persistence import (  # noqa: F401
        _persist_run,
        _safe_persist_run,
    )
    from .registry import (  # noqa: F401
        PAYMENT_ADAPTERS,
        _enabled_methods,
        _resolve_proxy_pool_routes,
        allowed_approve_countries,
        build_default_payment_registry,
        coerce_approve_country,
        normalize_payment_method,
        parse_proxy_pool,
        payment_method_label,
        payment_proxy_pools,
        register_payment_adapter,
        supported_payment_methods,
    )

_LAZY: dict[str, str] = {
    # base
    "GOPAY_DEFAULT_APPROVE_COUNTRIES": "base",
    "PAYMENT_METHODS": "base",
    "PaymentMethodSpec": "base",
    "_LOGGER": "base",
    "_STATE_LOCK": "base",
    "_as_bool": "base",
    "_config_data": "base",
    "_mask_ba_token": "base",
    "_protocol_cfg": "base",
    "_redact_sensitive_text": "base",
    "_redact_sensitive_values": "base",
    "_select_kwargs": "base",
    "_state_path": "base",
    "_tail": "base",
    # core
    "generate_payment_link": "core",
    "probe_payment_method": "core",
    # extract_server
    "ExtractLinkServerConfig": "extract_server",
    "extract_link_via_server": "extract_server",
    "resolve_extract_server_config": "extract_server",
    # team_trial
    "extract_team_trial_link": "team_trial",
    "probe_team_trial_eligibility": "team_trial",
    # normalize
    "_canonical_terminal_state": "normalize",
    "_classify_exception": "normalize",
    "_explicit_terminal_state": "normalize",
    "_is_retryable_failure": "normalize",
    "_normalize_error_contract": "normalize",
    "_normalize_result": "normalize",
    "_normalized_contract_value": "normalize",
    "_result_terminal_state": "normalize",
    # persistence
    "_persist_run": "persistence",
    "_safe_persist_run": "persistence",
    # registry
    "PAYMENT_ADAPTERS": "registry",
    "_enabled_methods": "registry",
    "_resolve_proxy_pool_routes": "registry",
    "allowed_approve_countries": "registry",
    "build_default_payment_registry": "registry",
    "coerce_approve_country": "registry",
    "normalize_payment_method": "registry",
    "parse_proxy_pool": "registry",
    "payment_method_label": "registry",
    "payment_proxy_pools": "registry",
    "register_payment_adapter": "registry",
    "supported_payment_methods": "registry",
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
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
