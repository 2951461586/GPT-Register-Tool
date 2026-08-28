"""PayPal checkout flow control: gates, SMS verification and the step runner.

Extracted from ``sms_tool.paypal_auto``. Owns the ordered step machine
(``_run_browser_steps``) plus the human-verification / SMS gates that can
interrupt it, and the OpenAI checkout pre-flight that runs before PayPal.
"""

from __future__ import annotations

import time
from typing import Any

from ..session_refresh import _poll_auth_session, _session_token
from ..sms_utils import _poll_sms_code, _sms_baseline
from .dom_fields import _click_with_fallback
from .errors import _PayPalStepError
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
from .session import (
    _is_openai_checkout_url,
    _is_paypal_url,
    _safe_import_cookie_header,
    _screenshot,
    _wait_for_paypal_load,
)

def _is_human_verification_page(page) -> bool:
    """Detect PayPal's visible human-verification challenge page."""
    selectors = [
        'text="Confirm you\'re human"',
        'text="Confirm you are human"',
        'text="Please enable JS and disable any ad blocker"',
        'text="Move the slider all the way to the right"',
        'iframe[src*="captcha"]',
        'iframe[title*="captcha" i]',
    ]
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    try:
        text = str(page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        text = ""
    return (
        "confirm you're human" in text
        or "confirm you are human" in text
        or "move the slider all the way to the right" in text
        or "please enable js and disable any ad blocker" in text
    )

def _handle_human_verification_gate(page, sms_cfg: dict, debug_dir: str, debug_enabled: bool, step: str) -> None:
    """Pause for manual PayPal human verification when allowed."""
    if not _is_human_verification_page(page):
        return

    _screenshot(page, debug_dir, "human_verification_required", debug_enabled)
    allow_manual = bool(sms_cfg.get("manual_human_verification", False))
    timeout = int(sms_cfg.get("human_verification_timeout", 300) or 300)
    if not allow_manual:
        raise _PayPalStepError(step, "paypal_human_verification_required")

    print(f"[*] PayPal human verification required; waiting up to {timeout}s for manual completion...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_human_verification_page(page):
            print("[*] PayPal human verification cleared")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            return
        time.sleep(2)

    raise _PayPalStepError(step, "paypal_human_verification_required")

def _handle_sms_verification(page, sms_cfg: dict, baseline: str) -> str | None:
    """Handle SMS verification if prompted."""
    code_selectors = [
        'input[name="code"]', 'input[name="smsCode"]',
        'input[name="otpCode"]', 'input[placeholder*="code"]',
        'input[placeholder*="Code"]', '#code', '#otp',
    ]
    needs_sms = False
    for selector in code_selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=3000):
                needs_sms = True
                break
        except Exception:
            continue

    if not needs_sms:
        _click_with_fallback(page, [
            'button:has-text("Send Code")',
            'button:has-text("Send")',
            'button:has-text("Text me")',
        ], timeout=3000)
        time.sleep(2)
        for selector in code_selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=3000):
                    needs_sms = True
                    break
            except Exception:
                continue

    if not needs_sms:
        return None

    print("[*] SMS verification required, polling for code...")
    code = _poll_sms_code(
        sms_cfg["api_url"], baseline,
        timeout=sms_cfg["timeout"],
        poll_interval=sms_cfg["poll_interval"],
    )
    if not code:
        raise _PayPalStepError("sms_verify", "sms_code_timeout")

    for selector in code_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=2000):
                el.fill(code)
                break
        except Exception:
            continue
    time.sleep(1)

    _click_with_fallback(page, [
        'button:has-text("Confirm")',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ], timeout=5000)
    time.sleep(2)
    return code

def _submit_payment(page):
    """Click the final payment/agree button."""
    selectors = [
        'button:has-text("Agree and Continue")',
        'button:has-text("Agree & Continue")',
        'button:has-text("Pay Now")',
        'button:has-text("Continue")',
        'button:has-text("Agree and Create Account")',
        'button:has-text("Agree")',
        'button[type="submit"]',
        '[data-testid="submit-button"]',
        '#payment-submit-btn',
    ]
    if _click_with_fallback(page, selectors, timeout=10000):
        print("[*] Payment submitted")
        time.sleep(3)
    else:
        raise _PayPalStepError("submit", "submit button not found")

def _wait_for_stripe_redirect(page, timeout: int = 60):
    """Wait for redirect back to Stripe or ChatGPT."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        if "checkout.stripe.com" in url or "chatgpt.com" in url:
            print(f"[*] Redirected to: {url[:80]}")
            return
        time.sleep(2)
    raise _PayPalStepError("wait_redirect", f"redirect timeout (current: {page.url[:80]})")

def _prepare_openai_checkout_paypal(
    page,
    *,
    address: dict,
    first_name: str,
    last_name: str,
    phone: str,
    debug_dir: str,
    debug_enabled: bool,
) -> bool:
    """Handle ChatGPT checkout links before continuing on PayPal."""
    if not _is_openai_checkout_url(page.url):
        return False

    print("[*] OpenAI checkout link detected; selecting PayPal in browser")
    deadline = time.time() + 45
    selected = False
    submitted = False
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        if _is_paypal_url(page.url):
            return True

        _fill_openai_checkout_billing(page, address, first_name, last_name, phone)
        if _select_openai_checkout_paypal(page):
            selected = True
            _screenshot(page, debug_dir, "02a_openai_paypal_selected", debug_enabled)
            time.sleep(1)

        if selected and _click_openai_checkout_continue(page):
            submitted = True
            _screenshot(page, debug_dir, "02b_openai_checkout_continue", debug_enabled)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            if _is_paypal_url(page.url):
                return True

        if submitted and not _is_openai_checkout_url(page.url):
            return _is_paypal_url(page.url)
        time.sleep(1)

    return _is_paypal_url(page.url)

def _run_browser_steps(
    page,
    ctx,
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    sms_cfg: dict,
    debug_dir: str,
    debug_enabled: bool,
    cookie_header: str,
    initial_step: str = "init",
) -> dict[str, Any]:
    """Run the shared PayPal browser automation steps.

    Used by both Camoufox and CloakBrowser paths.
    """
    step = initial_step
    result: dict[str, Any] = {"ok": False, "email": alias_email}

    baseline = _sms_baseline(sms_cfg["api_url"])

    if cookie_header:
        _safe_import_cookie_header(ctx, cookie_header)

    step = "navigate"
    _screenshot(page, debug_dir, "01_navigate_before", debug_enabled)
    page.goto(paypal_url, wait_until="domcontentloaded", timeout=60000)
    _prepare_openai_checkout_paypal(
        page,
        address=address,
        first_name=first_name,
        last_name=last_name,
        phone=sms_cfg.get("phone", ""),
        debug_dir=debug_dir,
        debug_enabled=debug_enabled,
    )
    _wait_for_paypal_load(page)
    _screenshot(page, debug_dir, "02_paypal_loaded", debug_enabled)
    _handle_human_verification_gate(page, sms_cfg, debug_dir, debug_enabled, "human_verification")

    step = "create_account"
    _click_create_account(page)
    _screenshot(page, debug_dir, "03_create_account", debug_enabled)
    _handle_human_verification_gate(page, sms_cfg, debug_dir, debug_enabled, "human_verification")

    step = "country"
    _ensure_country_us(page)
    _screenshot(page, debug_dir, "03_country_us", debug_enabled)
    _handle_human_verification_gate(page, sms_cfg, debug_dir, debug_enabled, "human_verification")

    step = "fill_email"
    _fill_signup_email(page, alias_email)
    _screenshot(page, debug_dir, "04_email_filled", debug_enabled)

    step = "fill_name"
    _fill_signup_name(page, first_name, last_name)
    _screenshot(page, debug_dir, "05_name_filled", debug_enabled)

    step = "phone"
    _fill_phone_if_present(page, sms_cfg["phone"])
    _screenshot(page, debug_dir, "06_phone_filled", debug_enabled)

    step = "password"
    _fill_password(page, password)
    _screenshot(page, debug_dir, "07_password_filled", debug_enabled)

    step = "card"
    _fill_card(page, card)
    _screenshot(page, debug_dir, "08_card_filled", debug_enabled)

    step = "address"
    billing_address = {**address, "first_name": first_name, "last_name": last_name}
    _fill_billing_address(page, billing_address)
    _screenshot(page, debug_dir, "09_address_filled", debug_enabled)

    step = "verify_fields"
    _verify_checkout_fields(page)

    step = "terms"
    _accept_terms(page)
    _screenshot(page, debug_dir, "10_terms_accepted", debug_enabled)

    step = "sms_verify"
    code = _handle_sms_verification(page, sms_cfg, baseline)
    _screenshot(page, debug_dir, "11_sms_verified", debug_enabled)

    step = "submit"
    _submit_payment(page)
    _screenshot(page, debug_dir, "12_payment_submitted", debug_enabled)

    step = "wait_redirect"
    _wait_for_stripe_redirect(page, timeout=60)
    _screenshot(page, debug_dir, "13_redirect_done", debug_enabled)

    step = "refresh_session"
    auth_body = _poll_auth_session(ctx, timeout=120)
    if not auth_body:
        raise _PayPalStepError("refresh_session", "auth_session_poll_timeout")

    new_access = _session_token(auth_body, "accessToken", "access_token")
    new_refresh = _session_token(auth_body, "refreshToken", "refresh_token")

    if not new_access:
        raise _PayPalStepError("refresh_session", "no_access_token_in_response")

    result = {
        "ok": True,
        "access_token": new_access,
        "oauth_refresh_token": new_refresh,
        "refresh_token_status": "oauth_present" if new_refresh else "no_rt",
        "paypal_status": "completed",
        "paypal_completed_at": int(time.time()),
        "card_last4": card["number"][-4:],
        "password": password,
        "alias_email": alias_email,
    }

    return result
