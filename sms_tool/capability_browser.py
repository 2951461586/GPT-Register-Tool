"""Real-browser fallback for the ChatGPT Checkout capability probe.

The protocol ``payments/checkout`` create gate answers ``400 unusual activity``
for some accounts regardless of egress (see ``docs/current/account-health.md``).
A browser session that has passed the Cloudflare challenge can still open the
session, so this transport performs the create **inside the page context** —
same cookies, TLS, headers and JS environment — and delegates every later stage
(Stripe init, custom-checkout read, elements) back to the protocol transport.

It is deliberately a plug-in seam, not a default: ``payment_method_capability_probe``
accepts ``fallback_transport=`` and only falls through on a risk-block or
transport failure. Build one with :func:`browser_fallback_transport`, which
returns ``None`` when the runtime cannot run a browser.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, cast

from . import endpoints
from .checkout_contract import (
    CheckoutContractError,
    CheckoutRequestContract,
    CheckoutSessionContract,
)
from .payment_capability import (
    CapabilityProbeError,
    ChatGPTStripeCapabilityTransport,
)


#: Route fetched from the page. Relative on purpose: it inherits the real
#: browser origin so the request is same-origin.
CHECKOUT_ROUTE = "/backend-api/payments/checkout"
_CHATGPT_ORIGIN = endpoints.CHATGPT_ORIGIN


def _load_sync_playwright() -> Any:
    """Import Playwright lazily (optional dependency)."""
    from playwright.sync_api import sync_playwright

    return sync_playwright


def browser_available() -> bool:
    """True when a headless browser rail can run in this interpreter."""
    try:
        _load_sync_playwright()
        return True
    except Exception:
        return False


def _cookies_from_header(cookie_header: str, device_id: str) -> list[dict[str, str]]:
    """Split an account ``Cookie`` header into Playwright cookie records."""
    cookies: list[dict[str, str]] = []
    seen: set[str] = set()
    for part in str(cookie_header or "").split(";"):
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name and value and name not in seen:
            seen.add(name)
            cookies.append({"name": name, "value": value, "url": _CHATGPT_ORIGIN})
    if device_id and "oai-did" not in seen:
        cookies.append({"name": "oai-did", "value": device_id, "url": _CHATGPT_ORIGIN})
    return cookies


class BrowserCapabilityTransport:
    """Open the Checkout in a real browser session; delegate the rest.

    ``fingerprint`` is the frozen browser profile (locale/timezone/user-agent).
    ``timeout_ms`` bounds the page load and the in-page fetch.
    """

    def __init__(self, *, fingerprint: Mapping[str, Any] | None = None, timeout_ms: int = 45000) -> None:
        self.fingerprint = dict(fingerprint or {})
        try:
            resolved_timeout = int(timeout_ms or 45000)
        except (TypeError, ValueError):
            resolved_timeout = 45000
        self.timeout_ms = max(10_000, min(resolved_timeout, 120_000))
        self._protocol = ChatGPTStripeCapabilityTransport()

    # ── later stages are unchanged; delegate to the protocol transport ──
    def stripe_init(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._protocol.stripe_init(*args, **kwargs)

    def custom_checkout_session(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._protocol.custom_checkout_session(*args, **kwargs)

    def stripe_elements(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._protocol.stripe_elements(*args, **kwargs)

    def payment_methods_signal(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._protocol.payment_methods_signal(*args, **kwargs)

    def create_checkout(
        self,
        contract: CheckoutRequestContract,
        *,
        access_token: str,
        auth_context: dict[str, Any],
        proxy: str,
        timeout: int,
    ) -> CheckoutSessionContract:
        try:
            sync_playwright = _load_sync_playwright()
        except Exception as exc:  # pragma: no cover - exercised via browser_available()
            raise CapabilityProbeError(
                "browser fallback unavailable: playwright not installed",
                error_code="checkout_browser_unavailable",
                error_stage="checkout_create",
                retryable=False,
                status="unknown",
            ) from exc

        from .upi_link.browser import _upi_browser_proxy

        context = auth_context if isinstance(auth_context, dict) else {}
        cookie_header = str(context.get("cookie_header") or "")
        device_id = str(context.get("device_id") or context.get("oai_did") or "").strip()
        fingerprint = self.fingerprint
        locale = str(fingerprint.get("locale") or "en-US")
        timezone_id = str(fingerprint.get("timezone") or "UTC")
        user_agent = str(fingerprint.get("user_agent") or "").strip()
        payload = contract.checkout_payload()
        status_code = 0
        parsed_body: dict[str, Any] = {}
        try:
            with sync_playwright() as playwright:
                launch: dict[str, Any] = {
                    "headless": True,
                    "timeout": self.timeout_ms,
                    "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
                }
                pw_proxy = _upi_browser_proxy(proxy)
                if pw_proxy:
                    launch["proxy"] = pw_proxy
                browser = playwright.chromium.launch(**launch)
                try:
                    browser_context = browser.new_context(
                        user_agent=user_agent or None,
                        locale=locale,
                        timezone_id=timezone_id,
                    )
                    cookies = _cookies_from_header(cookie_header, device_id)
                    if cookies:
                        browser_context.add_cookies(cast(Any, cookies))

                    def route_handler(route: Any) -> None:
                        request = route.request
                        low = str(request.url or "").lower()
                        if "chatgpt.com/backend-api/" not in low:
                            route.continue_()
                            return
                        try:
                            headers = dict(request.all_headers())
                        except Exception:
                            raw_headers = request.headers
                            headers = dict(raw_headers) if isinstance(raw_headers, Mapping) else {}
                        if str(access_token or "").strip():
                            headers["authorization"] = f"Bearer {access_token}"
                        if device_id:
                            headers["oai-device-id"] = device_id
                        route.continue_(headers=headers)

                    page = browser_context.new_page()
                    page.route("**/*", route_handler)
                    page.goto(_CHATGPT_ORIGIN, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    page.wait_for_timeout(1500)
                    fetched = page.evaluate(
                        """async ({route, body, device}) => {
                          const r = await fetch(route, {
                            method: 'POST', credentials: 'include',
                            headers: {
                              'content-type': 'application/json',
                              'accept': 'application/json',
                              'oai-device-id': device,
                            },
                            body: JSON.stringify(body),
                          });
                          const text = await r.text();
                          return {status: r.status, body: text.slice(0, 20000)};
                        }""",
                        {"route": CHECKOUT_ROUTE, "body": payload, "device": device_id},
                    )
                    status_code = int((fetched or {}).get("status") or 0)
                    try:
                        candidate = json.loads(str((fetched or {}).get("body") or "{}"))
                    except Exception:
                        candidate = {}
                    if isinstance(candidate, Mapping):
                        parsed_body = dict(candidate)
                finally:
                    browser.close()
        except CapabilityProbeError:
            raise
        except Exception as exc:
            raise CapabilityProbeError(
                f"browser checkout failed: {type(exc).__name__}",
                error_code="checkout_browser_failed",
                error_stage="checkout_create",
                retryable=True,
                status="unknown",
            ) from exc

        if status_code >= 400:
            raise CapabilityProbeError(
                f"browser checkout returned HTTP {status_code}",
                error_code="checkout_risk_blocked" if status_code == 400 else "checkout_failed",
                error_stage="checkout_create",
                retryable=status_code in {408, 425, 429} or status_code >= 500,
                status="unknown",
                http_status=status_code,
            )
        try:
            return CheckoutSessionContract.from_payload(parsed_body, billing_country=contract.billing_country)
        except CheckoutContractError as exc:
            raise CapabilityProbeError(
                str(exc),
                error_code=exc.error_code,
                error_stage=exc.error_stage,
                retryable=exc.retryable,
                status="unknown",
            ) from exc


def browser_fallback_transport(**kwargs: Any) -> BrowserCapabilityTransport | None:
    """Return a browser fallback when one can actually run, else ``None``."""
    if not browser_available():
        return None
    return BrowserCapabilityTransport(**kwargs)


__all__ = [
    "BrowserCapabilityTransport",
    "browser_available",
    "browser_fallback_transport",
]
