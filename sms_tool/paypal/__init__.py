"""PayPal auto-payment package.

Split out of the monolithic ``sms_tool/paypal_auto.py`` (1.9k lines) so each
layer can be read, tested and changed in isolation. ``sms_tool.paypal_auto``
remains as a thin compatibility shim re-exporting everything below.

Dependency direction is strictly one-way (no cycles)::

    orchestrator   strategy selection: reverse protocol -> nodriver -> browser
        |
    flow_steps     step machine + human-verification / SMS gates
        |
    form_steps     semantic PayPal form fields (email, card, address, ...)
        |
    session        browser context helpers (cookies, waits, screenshots)
    dom_fields     generic locator / fill / read primitives
    config_picker  card / address / phone selection and result persistence
    errors         ``_PayPalStepError`` (dependency-free leaf)
"""

from .config_picker import (
    _generate_alias_email,
    _pick_card_and_address,
    _pick_phone_and_sms,
    _read_index,
    _save_paypal_result,
    _write_index,
)
from .dom_fields import (
    _click_with_fallback,
    _field_has_any_value,
    _fill_by_label_fallback,
    _fill_by_visible_label_text,
    _fill_dom_id,
    _fill_dom_ids,
    _fill_visible_input,
    _fill_with_fallback,
    _locator_has_value,
    _read_field_value,
    _select_with_fallback,
    _set_field_value,
    _type_human,
    _value_matches,
    _visible_field_has_value,
)
from .errors import _PayPalStepError
from .flow_steps import (
    _handle_human_verification_gate,
    _handle_sms_verification,
    _is_human_verification_page,
    _prepare_openai_checkout_paypal,
    _run_browser_steps,
    _submit_payment,
    _wait_for_stripe_redirect,
)
from .form_steps import (
    _accept_terms,
    _click_create_account,
    _click_openai_checkout_continue,
    _ensure_country_us,
    _fill_billing_address,
    _fill_card,
    _fill_openai_checkout_billing,
    _fill_password,
    _fill_phone_if_present,
    _fill_signup_email,
    _fill_signup_name,
    _select_openai_checkout_paypal,
    _verify_checkout_fields,
)
from .orchestrator import (
    _try_browser_pay,
    _try_browser_pay_camoufox,
    _try_browser_pay_cloakbrowser,
    _try_nodriver_pay,
    _try_reverse_pay,
    auto_pay,
)
from .session import (
    _dismiss_overlays,
    _inject_navigator_overrides,
    _is_openai_checkout_url,
    _is_paypal_url,
    _safe_import_cookie_header,
    _screenshot,
    _wait_for_checkout_form_after_email,
    _wait_for_paypal_load,
)

__all__ = [
    # public entry point
    "auto_pay",
    # strategy attempts
    "_try_reverse_pay",
    "_try_nodriver_pay",
    "_try_browser_pay",
    "_try_browser_pay_camoufox",
    "_try_browser_pay_cloakbrowser",
    # flow
    "_run_browser_steps",
    "_is_human_verification_page",
    "_handle_human_verification_gate",
    "_handle_sms_verification",
    "_prepare_openai_checkout_paypal",
    "_submit_payment",
    "_wait_for_stripe_redirect",
    # form
    "_accept_terms",
    "_click_create_account",
    "_click_openai_checkout_continue",
    "_ensure_country_us",
    "_fill_billing_address",
    "_fill_card",
    "_fill_openai_checkout_billing",
    "_fill_password",
    "_fill_phone_if_present",
    "_fill_signup_email",
    "_fill_signup_name",
    "_select_openai_checkout_paypal",
    "_verify_checkout_fields",
    # session
    "_dismiss_overlays",
    "_inject_navigator_overrides",
    "_is_openai_checkout_url",
    "_is_paypal_url",
    "_safe_import_cookie_header",
    "_screenshot",
    "_wait_for_checkout_form_after_email",
    "_wait_for_paypal_load",
    # dom primitives
    "_click_with_fallback",
    "_field_has_any_value",
    "_fill_by_label_fallback",
    "_fill_by_visible_label_text",
    "_fill_dom_id",
    "_fill_dom_ids",
    "_fill_visible_input",
    "_fill_with_fallback",
    "_locator_has_value",
    "_read_field_value",
    "_select_with_fallback",
    "_set_field_value",
    "_type_human",
    "_value_matches",
    "_visible_field_has_value",
    # config / persistence
    "_generate_alias_email",
    "_pick_card_and_address",
    "_pick_phone_and_sms",
    "_read_index",
    "_save_paypal_result",
    "_write_index",
    # errors
    "_PayPalStepError",
]
