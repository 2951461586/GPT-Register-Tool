"""Re-exports of the former sms_tool.payment.link_manager module (mechanical split).

Every name lives in a submodule. Importing them eagerly made ``import sms_tool.pay_link``
(and therefore any ``from sms_tool.pay_link import X``) load all seven submodules, which
in turn pulled ``payment.routing -> proxy.routing -> phone_proxy -> requests``. PEP 562
resolves each name on first access; ``TYPE_CHECKING`` keeps the symbols visible to pyright.

Only the names a consumer actually imports are declared in ``__all__``. The remaining
private helpers stay reachable as module attributes (``__getattr__``), so ``patch(...)``
targets and direct ``from sms_tool.pay_link import _helper`` calls keep working without
enlarging the star-export contract.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapters import (  # noqa: F401
        _run_direct_card,
        _run_extractor_subprocess,
        _run_gcash_adapter,
        _run_momo,
        _run_protocol_script,
        _run_regional_wallet_adapter,
        _run_wallet_adapter,
        _write_token_file,
    )
    from .base import (  # noqa: F401
        GOPAY_DEFAULT_APPROVE_COUNTRIES,
        PAYMENT_METHODS,
        PaymentMethodSpec,
        _BLIK_RESULT_RE,
        _DIRECT_CARD_CURRENCY,
        _LOGGER,
        _STATE_LOCK,
        _as_bool,
        _blik_completion,
        _config_data,
        _last_json_object,
        _mask_ba_token,
        _protocol_cfg,
        _redact_sensitive_text,
        _redact_sensitive_values,
        _reference_root,
        _select_kwargs,
        _state_path,
        _tail,
    )
    from .core import (  # noqa: F401
        generate_payment_link,
        probe_payment_method,
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
    # adapters
    "_run_direct_card": "adapters",
    "_run_extractor_subprocess": "adapters",
    "_run_gcash_adapter": "adapters",
    "_run_momo": "adapters",
    "_run_protocol_script": "adapters",
    "_run_regional_wallet_adapter": "adapters",
    "_run_wallet_adapter": "adapters",
    "_write_token_file": "adapters",
    # base
    "GOPAY_DEFAULT_APPROVE_COUNTRIES": "base",
    "PAYMENT_METHODS": "base",
    "PaymentMethodSpec": "base",
    "_BLIK_RESULT_RE": "base",
    "_DIRECT_CARD_CURRENCY": "base",
    "_LOGGER": "base",
    "_STATE_LOCK": "base",
    "_as_bool": "base",
    "_blik_completion": "base",
    "_config_data": "base",
    "_last_json_object": "base",
    "_mask_ba_token": "base",
    "_protocol_cfg": "base",
    "_redact_sensitive_text": "base",
    "_redact_sensitive_values": "base",
    "_reference_root": "base",
    "_select_kwargs": "base",
    "_state_path": "base",
    "_tail": "base",
    # core
    "generate_payment_link": "core",
    "probe_payment_method": "core",
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
    "PaymentMethodSpec",
    "_reference_root",
    "_run_protocol_script",
    "build_default_payment_registry",
    "normalize_payment_method",
    "payment_proxy_pools",
    "payment_method_label",
    "supported_payment_methods",
    "register_payment_adapter",
    "allowed_approve_countries",
    "coerce_approve_country",
    "parse_proxy_pool",
    "generate_payment_link",
    "probe_payment_method",
    "GOPAY_DEFAULT_APPROVE_COUNTRIES",
    "PAYMENT_ADAPTERS",
    "PAYMENT_METHODS",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # `module_name` comes from the `_LAZY` whitelist above, never from caller input.
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
