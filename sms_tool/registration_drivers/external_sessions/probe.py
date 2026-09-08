"""Browser egress audit with no credential-bearing output."""

from typing import Any, Mapping


# ``__BUDGET_MS__`` is substituted per call -- an f-string would force every
# brace in the script to be doubled, which is how the last two attempts to
# parameterise this ended up malformed.
_COUNTRY_PROBE_SCRIPT = """
    async () => {
      const deadline = Date.now() + __BUDGET_MS__;
      for (const url of ['https://ipwho.is/', 'https://ipapi.co/json/']) {
        const remaining = deadline - Date.now();
        if (remaining <= 0) break;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), remaining);
        try {
          const response = await fetch(url, { credentials: 'omit', signal: controller.signal });
          const body = await response.json();
          const country = String(body.country_code || body.countryCode || '').toUpperCase();
          if (country) return { country, status: response.status };
        } catch (_) {}
        finally {
          clearTimeout(timer);
        }
      }
      return { country: '', status: 0 };
    }
"""


def verify_browser_proxy_country(browser: Any, *, expected_country: str = "", timeout_seconds: int = 20) -> dict[str, Any]:
    """Ask the live page which country it egresses from.

    ``timeout_seconds`` is enforced *inside* the page via an ``AbortController``
    deadline. It used to be accepted and then ignored, which was survivable when
    only roxy/cloak ran this -- but P2-3 runs it for every browser driver, and
    ``page.evaluate`` is not governed by Playwright's default timeout, so an
    unresponsive geo endpoint would have blocked a registration indefinitely.
    """
    page = getattr(browser, "page", None)
    if page is None:
        selector = getattr(browser, "select_live_page", None)
        page = selector() if callable(selector) else None
    if page is None:
        return {"ok": False, "error": "browser_page_unavailable", "actual_country": ""}
    try:
        budget_ms = max(1, int(timeout_seconds or 20)) * 1000
    except (TypeError, ValueError):
        budget_ms = 20_000
    script = _COUNTRY_PROBE_SCRIPT.replace("__BUDGET_MS__", str(budget_ms))
    try:
        result = page.evaluate(script)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "actual_country": ""}
    actual = str((result or {}).get("country") or "").strip().upper() if isinstance(result, Mapping) else ""
    expected = str(expected_country or "").strip().upper()
    if not actual:
        return {"ok": False, "error": "browser_proxy_country_unavailable", "actual_country": ""}
    if expected and actual != expected:
        return {"ok": False, "error": f"country_mismatch:{actual}", "actual_country": actual}
    return {"ok": True, "actual_country": actual}
