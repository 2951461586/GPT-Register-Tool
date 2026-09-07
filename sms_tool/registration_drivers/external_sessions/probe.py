"""Browser egress audit with no credential-bearing output."""

from typing import Any, Mapping


def verify_browser_proxy_country(browser: Any, *, expected_country: str = "", timeout_seconds: int = 20) -> dict[str, Any]:
    page = getattr(browser, "page", None)
    if page is None:
        selector = getattr(browser, "select_live_page", None)
        page = selector() if callable(selector) else None
    if page is None:
        return {"ok": False, "error": "browser_page_unavailable", "actual_country": ""}
    script = """
        async () => {
          for (const url of ['https://ipwho.is/', 'https://ipapi.co/json/']) {
            try {
              const response = await fetch(url, { credentials: 'omit' });
              const body = await response.json();
              const country = String(body.country_code || body.countryCode || '').toUpperCase();
              if (country) return { country, status: response.status };
            } catch (_) {}
          }
          return { country: '', status: 0 };
        }
    """
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
