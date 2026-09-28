"""gen_pp_link.py is now a thin backward-compatibility shell; all definitions
live in the `paypal_link` subpackage and are re-exported verbatim so every
`from sms_tool.gen_pp_link import ...` / `sms_tool.gen_pp_link.X` reference keeps working.
"""

from .paypal_link import *  # noqa: F401,F403
from .paypal_link import __all__ as _paypal_link_all

# UPI extraction used to reach this facade through the ``paypal_link`` package.
# It now lives with its single definition in ``upi_link``; this block is the
# one-version compatibility re-export for
# ``from sms_tool.gen_pp_link import generate_upi_qr_link`` (and the helpers that
# travelled with it). New call sites import ``sms_tool.upi_link`` directly.
from .upi_link import (  # noqa: F401
    STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
    STRIPE_PAYMENT_PAGE_GET_URL_T,
    STRIPE_PAYMENT_PAGE_INIT_URL_T,
    UPI_APPROVAL_MAX_ATTEMPTS,
    UPI_BILLING_IN,
    UPI_CHECKOUT_APPROVE_URL,
    UPI_CHECKOUT_CONFIRM_URL,
    UPI_CHECKOUT_URL,
    UPI_QR_POLL_INTERVAL,
    UPI_QR_POLL_MAX_ATTEMPTS,
    _default_qr_path,
    _method_cfg,
    _payment_stage_proxies_from_config,
    _upi_amount_minor,
    _upi_extract_next_action,
    _upi_extract_payment_amount,
    _upi_extract_qr_from_html,
    _upi_get_free_trial_status,
    _upi_get_payment_method_types,
    _upi_hydrate_qr_data,
    _upi_merge_qr_key,
    _upi_nested_get,
    _upi_scan_free_trial,
    _write_qr_png,
    generate_upi_qr_link,
)

__all__ = [
    *_paypal_link_all,
    "STRIPE_PAYMENT_PAGE_CONFIRM_URL_T",
    "STRIPE_PAYMENT_PAGE_GET_URL_T",
    "STRIPE_PAYMENT_PAGE_INIT_URL_T",
    "UPI_APPROVAL_MAX_ATTEMPTS",
    "UPI_BILLING_IN",
    "UPI_CHECKOUT_APPROVE_URL",
    "UPI_CHECKOUT_CONFIRM_URL",
    "UPI_CHECKOUT_URL",
    "UPI_QR_POLL_INTERVAL",
    "UPI_QR_POLL_MAX_ATTEMPTS",
    "_default_qr_path",
    "_method_cfg",
    "_payment_stage_proxies_from_config",
    "_upi_amount_minor",
    "_upi_extract_next_action",
    "_upi_extract_payment_amount",
    "_upi_extract_qr_from_html",
    "_upi_get_free_trial_status",
    "_upi_get_payment_method_types",
    "_upi_hydrate_qr_data",
    "_upi_merge_qr_key",
    "_upi_nested_get",
    "_upi_scan_free_trial",
    "_write_qr_png",
    "generate_upi_qr_link",
]
