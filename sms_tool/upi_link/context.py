"""Mutable per-run state shared by the UPI stage functions (2026-10-03).

`stages.run_upi_qr_link_once` builds one of these from its arguments and the
resolved runtime, then hands the same object to every `_stage_*` function.  Field
names are the original local names, so the moved stage bodies read `state.cs_id`
exactly where they used to read the local `cs_id`.

Unset slots raise ``AttributeError`` on read, matching the old
``UnboundLocalError`` for a variable read before assignment.
"""

from __future__ import annotations

#: Every field the stage bodies touch (one slot each).
UPI_STAGE_FIELDS: tuple[str, ...] = (
    "_oaics_state",
    "_rc",
    "access_token",
    "amount",
    "approval_blocked",
    "approval_data",
    "approval_ok",
    "approve_backoff_cap",
    "approve_proxy",
    "approve_shape",
    "billing",
    "browser_rail",
    "checkout_country",
    "checkout_data",
    "checkout_proxy",
    "checkout_sentinel_headers",
    "checkout_ui_mode",
    "confirm_data",
    "cs",
    "cs_id",
    "ctx",
    "currency",
    "device_id",
    "emit",
    "expires_at",
    "fingerprint",
    "ft_status",
    "hosted_url",
    "init",
    "inline_pm",
    "is_oaics",
    "link_type",
    "local_mandate_enabled",
    "mandate",
    "mandate_ok",
    "max_approve_attempts",
    "paid_state",
    "paid_timeout",
    "payment_country",
    "payment_currency",
    "payment_method_selection_flow",
    "pm_id",
    "pm_types",
    "poll_max_attempts",
    "processor_entity",
    "provider_proxy",
    "proxy",
    "proxy_state",
    "qr_data",
    "qr_data_str",
    "qr_path",
    "redirect_url",
    "refreshed",
    "refreshed_init",
    "require_server_upi_mandate",
    "require_zero",
    "return_url",
    "risk",
    "runtime_config",
    "session_token",
    "source",
    "stripe",
    "stripe_js_id",
    "stripe_pk",
    "target_country",
    "tax_body",
    "update_customer_data",
    "update_tax_region",
    "upi_cfg",
    "upi_uri",
    "wait_paid",
    "written_qr_path",
)


class UpiStageContext:
    """Attribute bag with a fixed slot set; unset fields raise on read."""

    __slots__ = UPI_STAGE_FIELDS


