"""Unified state machine for protocol payment-link extraction.

This module is now a thin backward-compatibility shell; all definitions
live in the `pay_link` subpackage and are re-exported verbatim so every
`from sms_tool.payment.link_manager import ...` / `sms_tool.payment.link_manager.X`
reference keeps working.

The public surface is **declared**, not incidental: ``__all__`` is re-imported
from ``pay_link`` (see ``sms_tool/pay_link/__init__.py``), and every name a
consumer imports through this shell must appear there.  A name that only
survives on one of the explicit pre-split imports below is exported by accident
and is reported as removable debt by ``scripts/unused_import_ratchet.py``.
``tests/payment/test_payment_link_manager.py::FacadeSurfaceTests`` pins the invariant.

Import laziness (2026-10-23)
----------------------------
The shell used ``from ..pay_link import *``, so importing ``payment.link_manager``
loaded the whole payment lane (461 ms / 329 modules) even for a caller that only
wanted ``__all__``. PEP 562 resolves each exported name on first access. The two
incidental imports kept below are not exports: tests patch
``link_manager.current_config_data`` and ``link_manager.subprocess``.

Import laziness (2026-10-23)
----------------------------
The shell used ``from ..pay_link import *``, so importing ``payment.link_manager``
loaded the whole payment lane (461 ms / 329 modules) even for a caller that only
wanted ``__all__``. PEP 562 resolves each exported name on first access. The two
incidental imports kept below are not exports: tests patch
``link_manager.current_config_data`` and ``link_manager.subprocess``.
"""

from __future__ import annotations

import importlib
import subprocess  # noqa: F401 - patch target (``link_manager.subprocess.run``)
from typing import TYPE_CHECKING, Any

from ..config import current_config_data  # noqa: F401 - patch target
from . import egress as payment_egress  # noqa: F401 - patch target (``manager.payment_egress``)

if TYPE_CHECKING:
    from ..pay_link import (  # noqa: F401
        _config_data,
        PaymentMethodSpec,
        _as_bool,
        _select_kwargs,
        _protocol_cfg,
        _reference_root,
        _state_path,
        _last_json_object,
        _tail,
        _blik_completion,
        _mask_ba_token,
        _redact_sensitive_text,
        _redact_sensitive_values,
        _run_extractor_subprocess,
        _run_protocol_script,
        _write_token_file,
        _run_wallet_adapter,
        _run_regional_wallet_adapter,
        _run_gcash_adapter,
        _run_direct_card,
        _run_momo,
        _normalize_result,
        _explicit_terminal_state,
        _canonical_terminal_state,
        _normalized_contract_value,
        _normalize_error_contract,
        _is_retryable_failure,
        _result_terminal_state,
        _classify_exception,
        _persist_run,
        _safe_persist_run,
        build_default_payment_registry,
        normalize_payment_method,
        payment_proxy_pools,
        payment_method_label,
        supported_payment_methods,
        register_payment_adapter,
        allowed_approve_countries,
        coerce_approve_country,
        parse_proxy_pool,
        _resolve_proxy_pool_routes,
        _enabled_methods,
        generate_payment_link,
        probe_payment_method,
        GOPAY_DEFAULT_APPROVE_COUNTRIES,
        PAYMENT_ADAPTERS,
        PAYMENT_METHODS,
        _BLIK_RESULT_RE,
        _DIRECT_CARD_CURRENCY,
        _LOGGER,
        _STATE_LOCK,
    )

_LAZY: dict[str, str] = {
    name: "pay_link"
    for name in (
        "_config_data",
        "PaymentMethodSpec",
        "_as_bool",
        "_select_kwargs",
        "_protocol_cfg",
        "_reference_root",
        "_state_path",
        "_last_json_object",
        "_tail",
        "_blik_completion",
        "_mask_ba_token",
        "_redact_sensitive_text",
        "_redact_sensitive_values",
        "_run_extractor_subprocess",
        "_run_protocol_script",
        "_write_token_file",
        "_run_wallet_adapter",
        "_run_regional_wallet_adapter",
        "_run_gcash_adapter",
        "_run_direct_card",
        "_run_momo",
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
        "GOPAY_DEFAULT_APPROVE_COUNTRIES",
        "PAYMENT_ADAPTERS",
        "PAYMENT_METHODS",
        "_BLIK_RESULT_RE",
        "_DIRECT_CARD_CURRENCY",
        "_LOGGER",
        "_STATE_LOCK",
    )
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
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # ``__package__`` is ``sms_tool.payment`` for this module, so level 2 from
    # there is ``sms_tool`` and the target is the sibling package
    # ``sms_tool.pay_link``. Using ``__name__`` would treat this module as the
    # package and look for ``sms_tool.payment.link_manager.pay_link``.
    module = importlib.import_module("..pay_link", __package__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
