"""Read-only dependency bundle for the UPI protocol stages (2026-10-03).

`stages.run_upi_qr_link_once` resolves every module-level callable through
``ops.<name>``.  The bundle is built by ``pipeline._build_upi_operations()`` from
**`pipeline`'s own globals at call time**, so a test that patches
``sms_tool.upi_link.pipeline.<name>`` is still read by the stage body -- the
property that let the split land with no test edits.

`UPI_OPERATION_NAMES` keeps the original names verbatim: the copy is then a
name-for-name read with no mapping table to drift, and a missing name fails
loudly at build time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Names `pipeline` must expose for the stage body to run.
UPI_OPERATION_NAMES: tuple[str, ...] = (
    "_approve_backoff",
    "_emit",
    "_env_bool",
    "_new_session",
    "_normalize_hosted_checkout_url",
    "_resolve_upi_runtime",
    "_upi_account_id_from_token",
    "_upi_apply_approve_risk",
    "_upi_apply_fingerprint",
    "_upi_assert_egress_contract",
    "_upi_browser_approve",
    "_upi_build_confirm_body",
    "_upi_build_ctx",
    "_upi_capture_risk_context",
    "_upi_classify_failure",
    "_upi_confirm_local_mandate",
    "_upi_create_upi_pm",
    "_upi_custom_payment_method_id",
    "_upi_dump_http",
    "_upi_elements_session",
    "_upi_elements_session_params",
    "_upi_ensure_checkout_flow",
    "_upi_extract_next_action",
    "_upi_extract_qr_candidates",
    "_upi_extract_redirect_url",
    "_upi_fetch_oaics_state",
    "_upi_find_submission_attempt",
    "_upi_first_value_by_key",
    "_upi_get_free_trial_status",
    "_upi_hosted_fallback_result",
    "_upi_hydrate_qr_data",
    "_upi_india_exit_attempts",
    "_upi_india_exit_probe_enabled",
    "_upi_india_exit_timeout",
    "_upi_is_403",
    "_upi_is_instructions_url",
    "_upi_link_unverified_contract",
    "_upi_new_chatgpt_session",
    "_upi_passive_captcha_fields",
    "_upi_poll_payment_page",
    "_upi_post_with_degrade",
    "_upi_promo_page_url",
    "_upi_qr_image_kind",
    "_upi_raise_if_setup_intent_blocked",
    "_upi_rebuild_ctx",
    "_upi_record_zero_result",
    "_upi_register_stripe_fingerprint",
    "_upi_repeat_tax_region",
    "_upi_resolve_external_redirect",
    "_upi_run_cpmt_flow",
    "_upi_run_oaics_flow",
    "_upi_select_india_exit",
    "_upi_sentinel_headers",
    "_upi_sentinel_ping",
    "_upi_server_mandate",
    "_upi_should_retry_second_confirm",
    "_upi_stripe_init",
    "_upi_verify_instructions_verdict",
    "_upi_wait_paid",
    "_write_qr_png",
    "redact_proxy_text",
)


class UpiOperations:
    """Attribute bundle; one slot per name in :data:`UPI_OPERATION_NAMES`."""

    __slots__ = UPI_OPERATION_NAMES

    def __init__(self, **values: Any) -> None:
        missing = [name for name in UPI_OPERATION_NAMES if name not in values]
        if missing:
            raise TypeError(f"UpiOperations missing operation(s): {missing}")
        for name in UPI_OPERATION_NAMES:
            object.__setattr__(self, name, values[name])


def build_upi_operations(namespace: Mapping[str, Any]) -> UpiOperations:
    """Assemble the bundle from a module namespace (call-time read)."""
    return UpiOperations(**{name: namespace[name] for name in UPI_OPERATION_NAMES})
