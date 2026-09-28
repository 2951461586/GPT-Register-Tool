"""UPI protocol-link extraction package (facade).

Implementation is split by dependency layer; this package re-exports every
name so ``sms_tool.upi_link.<name>`` keeps resolving exactly as it did when
the whole pipeline lived in ``__init__.py``. ``generate_upi_qr_link`` is
defined in :mod:`sms_tool.upi_link.pipeline` (single definition, guarded by
``tests/test_upi_link_entrypoint_unique.py``).
"""
from __future__ import annotations

from .constants import (  # noqa: F401
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    SCRIPT_DIR,
    STRIPE_INTENT_URL_T,
    STRIPE_PAYMENT_METHODS_URL,
    STRIPE_PAYMENT_PAGE_CONFIRM_URL_T,
    STRIPE_PAYMENT_PAGE_GET_URL_T,
    STRIPE_PAYMENT_PAGE_INIT_URL_T,
    UPI_APPROVAL_MAX_ATTEMPTS,
    UPI_ATTESTATION_ENV_KEYS,
    UPI_BILLING_ADDRESSES,
    UPI_BILLING_IN,
    UPI_BILLING_NAMES,
    UPI_CALL_OPTIONS,
    UPI_CHATGPT_CLIENT_BUILD_NUMBER,
    UPI_CHATGPT_CLIENT_VERSION,
    UPI_CHECKOUT_APPROVE_URL,
    UPI_CHECKOUT_CONFIRM_URL,
    UPI_CHECKOUT_SNAPSHOT_URL,
    UPI_CHECKOUT_URL,
    UPI_CONFIRMATION_TOKENS_URL,
    UPI_CPMT_CONFIRM_URL,
    UPI_CPMT_START_URL,
    UPI_DEFAULT_FINGERPRINT,
    UPI_DUMP_DEFAULT_DIR,
    UPI_EMAIL_DOMAINS,
    UPI_FINGERPRINT_TEMPLATES,
    UPI_PAYMENT_INTENT_CONFIRM_URL_T,
    UPI_PAYMENT_INTENT_GET_URL_T,
    UPI_QR_POLL_INTERVAL,
    UPI_QR_POLL_MAX_ATTEMPTS,
    UPI_SECOND_CONFIRM_MARKERS,
    UPI_SENTINEL_APPROVAL_FLOW,
    UPI_SENTINEL_CHECKOUT_FLOW,
    UPI_SENTINEL_PING_URL,
    UPI_WARMUP_TIMEOUT,
    _UPI_ATTESTATION_ANY_RE,
    _UPI_ATTESTATION_RE,
    _UPI_PAGE_BUILD_ANY_RE,
    _UPI_PAGE_BUILD_RE,
    _UPI_PAGE_SEQ_ANY_RE,
    _UPI_PAGE_SEQ_JSON_RE,
    _UPI_PAGE_SEQ_RE,
)

from .env import (  # noqa: F401
    _emit,
    _env_bool,
    _env_int,
    _env_str,
    _float_env,
)

from .dump import (  # noqa: F401
    _approve_backoff,
    _dump_counter,
    _dump_lock,
    _upi_dump_dir,
    _upi_dump_http,
    _upi_redact_for_dump,
)

from .config import (  # noqa: F401
    _load_json,
    _method_cfg,
    _payment_stage_proxies_from_config,
    _upi_first_string,
    _upi_payment_intent_id,
    _upi_retarget_region,
)

from .session import (  # noqa: F401
    _default_qr_path,
    _normalize_hosted_checkout_url,
    _upi_accept_language_for,
    _upi_account_id_from_token,
    _upi_apply_chatgpt_identity,
    _upi_apply_fingerprint,
    _upi_attestation_deploy_id,
    _upi_billing_profile,
    _upi_classify_failure,
    _upi_elements_session,
    _upi_ensure_checkout_flow,
    _upi_env_attestation,
    _upi_find_attestation,
    _upi_fingerprint,
    _upi_new_chatgpt_session,
    _upi_promo_page_url,
    _upi_record_zero_result,
    _upi_runtime_version,
    _upi_scrape_page_identity,
    _upi_server_mandate,
    _upi_session_is_live,
    _upi_warmup_session,
    _write_qr_png,
)

from .browser import (  # noqa: F401
    _upi_browser_approve,
    _upi_browser_available,
    _upi_browser_capture,
    _upi_browser_id,
    _upi_browser_proxy,
)

from .stripe import (  # noqa: F401
    _upi_build_confirm_body,
    _upi_build_confirmation_token_body,
    _upi_build_ctx,
    _upi_build_init_body,
    _upi_create_upi_pm,
    _upi_degraded_template,
    _upi_elements_session_params,
    _upi_format_summary,
    _upi_hosted_fallback_result,
    _upi_intent_redirect_url,
    _upi_is_403,
    _upi_passive_captcha_config,
    _upi_passive_captcha_fields,
    _upi_payload_intent_redirect_url,
    _upi_payment_page_summary,
    _upi_poll_payment_page,
    _upi_post_with_degrade,
    _upi_should_retry_second_confirm,
    _upi_stripe_init,
)

from .sentinel import (  # noqa: F401
    _UpiRiskContext,
    _upi_apply_approve_risk,
    _upi_capture_risk_context,
    _upi_fetch_oaics_state,
    _upi_sentinel_headers,
    _upi_sentinel_ping,
    _upi_wait_paid,
)

from .extract import (  # noqa: F401
    _upi_hydrate_qr_data,
    _upi_resolve_external_redirect,
)

from .flows import (  # noqa: F401
    _upi_run_cpmt_flow,
    _upi_run_oaics_flow,
)

from .pipeline import (  # noqa: F401
    _resolve_upi_runtime,
    generate_upi_qr_link,
    upi_invocation,
)

from ._extract import (  # noqa: F401
    UPI_CODE_RESOURCE_SUFFIXES,
    UPI_PROVIDER_DECLINE_MARKER,
    UPI_STATIC_HOSTS,
    _UPI_DATA_IMAGE_RE,
    _UPI_QR_HINT_RE,
    _UPI_URL_RE,
    _upi_amount_minor,
    _upi_bool_value,
    _upi_collect_urls,
    _upi_confirm_amounts,
    _upi_copyable_link,
    _upi_custom_payment_method_id,
    _upi_decode_base64url_json,
    _upi_display_amounts,
    _upi_extract_next_action,
    _upi_extract_payment_amount,
    _upi_extract_qr_candidates,
    _upi_extract_qr_from_html,
    _upi_extract_redirect_url,
    _upi_find_submission_attempt,
    _upi_first_non_empty,
    _upi_first_value_by_key,
    _upi_get_free_trial_status,
    _upi_get_payment_method_types,
    _upi_int_value,
    _upi_is_code_resource_url,
    _upi_is_instructions_url,
    _upi_is_provider_decline_text,
    _upi_is_qr_candidate,
    _upi_is_static_resource_url,
    _upi_merge_qr_key,
    _upi_nested_get,
    _upi_provider_decline_message,
    _upi_qr_image_kind,
    _upi_raise_if_setup_intent_blocked,
    _upi_scan_free_trial,
    _upi_setup_intent_last_error,
    _upi_url_path_extension,
)

