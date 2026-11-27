"""PayPal auto-payment package (PEP 562 lazy facade).

Split out of the monolithic ``sms_tool/paypal/auto.py`` (1.9k lines) so each
layer can be read, tested and changed in isolation. ``sms_tool.paypal`` is the
single public package entrypoint (the ``sms_tool.paypal.auto`` compatibility
shim was retired once all production and test callers migrated).

Dependency direction is strictly one-way (no cycles)::

    orchestrator   strategy selection: reverse protocol -> nodriver -> browser
        |
    flow_steps     step machine + human-verification / SMS gates
        |
    form_steps     semantic PayPal form fields (email, card, address, ...)
        |
    session        browser context helpers (cookies, waits, screenshots)
    dom_fields     generic locator / fill / read primitives
    config_picker  card / address selection and result persistence
    errors         ``_PayPalStepError`` (dependency-free leaf)

**Import laziness (2026-11-27):** this facade resolves re-exported symbols via
PEP 562 ``__getattr__`` rather than eagerly importing ``orchestrator`` (Layer 3)
from ``paypal/__init__.py`` (Layer 4). Importing a leaf submodule such as
``sms_tool.paypal.proxy`` no longer pulls the browser/nodriver orchestrator,
eliminating the ``paypal -> paypal.orchestrator`` layer-manifest exception.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config_picker import (
        _generate_alias_email as _generate_alias_email,
        _pick_card_and_address as _pick_card_and_address,
        _read_index as _read_index,
        _save_paypal_result as _save_paypal_result,
        _write_index as _write_index,
    )
    from .dom_fields import (
        _click_with_fallback as _click_with_fallback,
        _field_has_any_value as _field_has_any_value,
        _fill_by_label_fallback as _fill_by_label_fallback,
        _fill_by_visible_label_text as _fill_by_visible_label_text,
        _fill_dom_id as _fill_dom_id,
        _fill_dom_ids as _fill_dom_ids,
        _fill_visible_input as _fill_visible_input,
        _fill_with_fallback as _fill_with_fallback,
        _locator_has_value as _locator_has_value,
        _read_field_value as _read_field_value,
        _select_with_fallback as _select_with_fallback,
        _set_field_value as _set_field_value,
        _type_human as _type_human,
        _value_matches as _value_matches,
        _visible_field_has_value as _visible_field_has_value,
    )
    from .errors import _PayPalStepError as _PayPalStepError
    from .flow_steps import (
        _handle_human_verification_gate as _handle_human_verification_gate,
        _handle_sms_verification as _handle_sms_verification,
        _is_human_verification_page as _is_human_verification_page,
        _prepare_openai_checkout_paypal as _prepare_openai_checkout_paypal,
        _run_browser_steps as _run_browser_steps,
        _submit_payment as _submit_payment,
        _wait_for_stripe_redirect as _wait_for_stripe_redirect,
    )
    from .form_steps import (
        _accept_terms as _accept_terms,
        _click_create_account as _click_create_account,
        _click_openai_checkout_continue as _click_openai_checkout_continue,
        _ensure_country_us as _ensure_country_us,
        _fill_billing_address as _fill_billing_address,
        _fill_card as _fill_card,
        _fill_openai_checkout_billing as _fill_openai_checkout_billing,
        _fill_password as _fill_password,
        _fill_phone_if_present as _fill_phone_if_present,
        _fill_signup_email as _fill_signup_email,
        _fill_signup_name as _fill_signup_name,
        _select_openai_checkout_paypal as _select_openai_checkout_paypal,
        _verify_checkout_fields as _verify_checkout_fields,
    )
    from .orchestrator import (
        _try_browser_pay as _try_browser_pay,
        _try_browser_pay_camoufox as _try_browser_pay_camoufox,
        _try_browser_pay_cloakbrowser as _try_browser_pay_cloakbrowser,
        _try_nodriver_pay as _try_nodriver_pay,
        _try_reverse_pay as _try_reverse_pay,
        auto_pay as auto_pay,
    )
    from .session import (
        _dismiss_overlays as _dismiss_overlays,
        _inject_navigator_overrides as _inject_navigator_overrides,
        _is_openai_checkout_url as _is_openai_checkout_url,
        _is_paypal_url as _is_paypal_url,
        _safe_import_cookie_header as _safe_import_cookie_header,
        _screenshot as _screenshot,
        _wait_for_checkout_form_after_email as _wait_for_checkout_form_after_email,
        _wait_for_paypal_load as _wait_for_paypal_load,
    )

_LAZY_SUBMODULE_EXPORTS: dict[str, tuple[str, ...]] = {
    ".orchestrator": (
        "auto_pay",
        "_try_reverse_pay",
        "_try_nodriver_pay",
        "_try_browser_pay",
        "_try_browser_pay_camoufox",
        "_try_browser_pay_cloakbrowser",
    ),
    ".flow_steps": (
        "_run_browser_steps",
        "_is_human_verification_page",
        "_handle_human_verification_gate",
        "_handle_sms_verification",
        "_prepare_openai_checkout_paypal",
        "_submit_payment",
        "_wait_for_stripe_redirect",
    ),
    ".form_steps": (
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
    ),
    ".session": (
        "_dismiss_overlays",
        "_inject_navigator_overrides",
        "_is_openai_checkout_url",
        "_is_paypal_url",
        "_safe_import_cookie_header",
        "_screenshot",
        "_wait_for_checkout_form_after_email",
        "_wait_for_paypal_load",
    ),
    ".dom_fields": (
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
    ),
    ".config_picker": (
        "_generate_alias_email",
        "_pick_card_and_address",
        "_read_index",
        "_save_paypal_result",
        "_write_index",
    ),
    ".errors": ("_PayPalStepError",),
}

_LAZY_OWNER: dict[str, str] = {
    name: submodule for submodule, names in _LAZY_SUBMODULE_EXPORTS.items() for name in names
}

__all__ = [name for names in _LAZY_SUBMODULE_EXPORTS.values() for name in names]


def __getattr__(name: str) -> Any:
    """Resolve one PayPal facade re-export on first use (PEP 562)."""
    submodule = _LAZY_OWNER.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(submodule, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
