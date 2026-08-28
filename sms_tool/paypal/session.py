"""Browser session helpers shared by the Camoufox and CloakBrowser engines.

Extracted from ``sms_tool.paypal_auto``. Covers cookie import, fingerprint
override injection, page-load waits, overlay dismissal and debug screenshots.
"""

from __future__ import annotations

import time
from pathlib import Path

def _safe_import_cookie_header(ctx, cookie_header):
    """Safely import cookies into browser context."""
    if not cookie_header:
        return

    cookies = []
    for item in str(cookie_header).split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        if name.startswith("__Host-"):
            continue
        cookie = {
            "name": name,
            "value": value,
            "domain": ".chatgpt.com",
            "path": "/",
        }
        if name.startswith("__Secure-"):
            cookie["secure"] = True
            cookie["httpOnly"] = True
            cookie["sameSite"] = "Lax"
        cookies.append(cookie)

    if cookies:
        try:
            ctx.add_cookies(cookies)
        except Exception as e:
            print(f"[!] Cookie import warning: {e}")

def _inject_navigator_overrides(ctx) -> None:
    """Inject Navigator property overrides for fingerprint consistency.

    Ensures navigator.language and navigator.languages match the expected
    locale, even if the browser's default differs from the proxy's GeoIP.
    """
    script = """
(() => {
  const language = 'en-US';
  const languages = ['en-US', 'en'];
  const define = (object, property, value) => {
    try {
      Object.defineProperty(object, property, {
        get: () => value,
        configurable: true,
      });
    } catch (_) {}
  };
  define(Navigator.prototype, 'language', language);
  define(Navigator.prototype, 'languages', languages);
})();
"""
    try:
        ctx.add_init_script(script)
    except Exception as e:
        print(f"[*] Navigator override injection failed (non-fatal): {e}")

def _wait_for_paypal_load(page, timeout: int = 30000):
    """Wait for PayPal page to finish loading."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    time.sleep(2)

def _screenshot(page, debug_dir: str, name: str, enabled: bool = True):
    if not enabled:
        return
    try:
        p = Path(debug_dir)
        p.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(p / f"{name}.png"), full_page=True)
    except Exception:
        pass

def _dismiss_overlays(page):
    """Dismiss autocomplete/cookie overlays that can steal focus from the next field."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for selector in [
        'button:has-text("Close")',
        'button:has-text("Accept")',
        '[aria-label="Close"]',
        '.AddressAutocomplete-results',
    ]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=500):
                if "AddressAutocomplete-results" in selector:
                    continue
                el.click(timeout=1000)
                time.sleep(0.3)
        except Exception:
            continue

def _wait_for_checkout_form_after_email(page, timeout: int = 12000) -> bool:
    """Wait until the full PayPal checkoutweb form appears after the email gate."""
    deadline = time.time() + (timeout / 1000)
    selectors = [
        '#cardNumber',
        'input[id="cardNumber"]',
        'input[autocomplete="cc-number"]',
        'text="Pay with debit or credit card"',
        'text="Billing address"',
    ]
    while time.time() < deadline:
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        time.sleep(0.5)
    return False

def _is_openai_checkout_url(url: str) -> bool:
    value = str(url or "").lower()
    return (
        "chatgpt.com/checkout/" in value
        or "pay.openai.com" in value
        or "checkout.stripe.com/c/pay/" in value
    )

def _is_paypal_url(url: str) -> bool:
    return "paypal.com" in str(url or "").lower()
