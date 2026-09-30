from __future__ import annotations

from typing import Any
from collections.abc import Mapping
import json
import secrets
from urllib.parse import parse_qsl
import time
from urllib.parse import unquote
from urllib.parse import urlsplit
import uuid
from ._extract import (
    _upi_int_value,
)
from .session import _upi_find_attestation


def _upi_browser_available() -> bool:
    """True when the headless browser rail can run in this interpreter."""
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except Exception:
        return False
def _upi_browser_proxy(proxy: Any) -> dict[str, str] | None:
    """Convert a proxy URL to Playwright's launch proxy dict.

    Chromium rejects the ``socks5h`` scheme (remote DNS is implicit in
    Chromium's ``socks5``), so it is normalised here; credentials are
    percent-decoded for Playwright.
    """
    value = str(proxy or "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except Exception:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    result: dict[str, str] = {"server": server}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result
def _upi_browser_capture(
    *,
    access_token: Any,
    session_token: Any,
    device_id: Any,
    proxy: Any,
    fingerprint: Any,
    page_url: Any,
    sentinel_flow: Any = "",
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Headless browser rail: capture the browser-issued risk context.

    Mirrors the reference ``capture_browser_bootstrap``: launch a headless
    Chromium through the same egress, seed the account cookies, inject the
    access token onto ChatGPT backend requests, then read the headers the real
    frontend attaches — ``oai-web-deployment-attestation`` and, crucially,
    ``x-oai-is-client-observation``. The observation is a browser-signed value
    that the protocol path cannot synthesise, and it is what the ChatGPT
    ``checkout/approve`` risk gate validates.

    Read-only and evidence-only: it never fabricates a signature, and every
    failure degrades to an empty capture so the caller can fall back.
    """
    result: dict[str, Any] = {
        "attestation": "",
        "observation": "",
        "client_version": "",
        "client_build": "",
        "cookies": {},
        "stripe_ids": {},
        "sentinel_token": "",
        "sentinel_flow": "",
        "error": "",
    }
    if not _upi_browser_available():
        result["error"] = "playwright_unavailable"
        return result
    from playwright.sync_api import sync_playwright

    fp = fingerprint if isinstance(fingerprint, Mapping) else {}
    locale = str(fp.get("locale") or "en-US")
    timezone_id = str(fp.get("timezone") or "UTC")
    user_agent = str(fp.get("user_agent") or "").strip()
    target = str(page_url or "https://chatgpt.com/") or "https://chatgpt.com/"
    capture_ms = _upi_int_value(min(max(10.0, _upi_int_value(timeout_s)[0] or 30) * 1000, 40000))[0] or 30000
    cookies = {"oai-did": str(device_id or "")}
    if str(session_token or "").strip():
        cookies["__Secure-next-auth.session-token"] = str(session_token).strip()
    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {
                "headless": True,
                "timeout": capture_ms,
                "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
            }
            pw_proxy = _upi_browser_proxy(proxy)
            if pw_proxy:
                launch["proxy"] = pw_proxy
            browser = playwright.chromium.launch(**launch)
            context = browser.new_context(
                user_agent=user_agent or None,
                locale=locale,
                timezone_id=timezone_id,
            )
            try:
                context.add_cookies(
                    [
                        {"name": name, "value": value, "url": "https://chatgpt.com/"}
                        for name, value in cookies.items()
                        if value
                    ]
                )
            except Exception:
                pass

            def observe(request: Any) -> None:
                try:
                    raw_headers = request.all_headers()
                except Exception:
                    raw_headers = getattr(request, "headers", None)
                if not isinstance(raw_headers, Mapping):
                    raw_headers = {}
                headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
                if headers.get("oai-web-deployment-attestation"):
                    result["attestation"] = headers["oai-web-deployment-attestation"]
                if headers.get("x-oai-is-client-observation"):
                    result["observation"] = headers["x-oai-is-client-observation"]
                if headers.get("oai-client-version"):
                    result["client_version"] = headers["oai-client-version"]
                if headers.get("oai-client-build-number"):
                    result["client_build"] = headers["oai-client-build-number"]
                request_url = str(getattr(request, "url", "") or "")
                host = (urlsplit(request_url).hostname or "").lower()
                if host.endswith("stripe.com"):
                    encoded_values = [urlsplit(request_url).query]
                    post_data = getattr(request, "post_data", "")
                    if callable(post_data):
                        post_data = post_data()
                    if post_data:
                        encoded_values.append(str(post_data))
                    for encoded in encoded_values:
                        for key, value in parse_qsl(encoded, keep_blank_values=False):
                            if key in {"guid", "muid", "sid", "stripe_js_id"} and value:
                                result["stripe_ids"].setdefault(key, value)

            def route_handler(route: Any) -> None:
                request = route.request
                low = str(request.url or "").lower()
                if "chatgpt.com/backend-api/" in low and "/backend-api/sentinel/" not in low:
                    try:
                        headers = dict(request.all_headers())
                    except Exception:
                        fallback = request.headers
                        headers = dict(fallback) if isinstance(fallback, Mapping) else {}
                    if str(access_token or "").strip():
                        headers["authorization"] = f"Bearer {access_token}"
                    if str(device_id or "").strip():
                        headers["oai-device-id"] = str(device_id)
                    route.continue_(headers=headers)
                else:
                    route.continue_()

            page = context.new_page()
            context.on("request", observe)
            page.route("**/*", route_handler)
            page.goto(target, wait_until="domcontentloaded", timeout=capture_ms)
            page.wait_for_timeout(1200)
            try:
                page.evaluate(
                    """() => fetch('/backend-api/models?history_and_training_disabled=false',
                       {credentials: 'include', headers: {'accept': 'application/json'}})
                       .then(r => r.text()).catch(() => '')"""
                )
            except Exception:
                pass
            deadline = time.monotonic() + max(5.0, float(timeout_s) - 5.0)
            while time.monotonic() < deadline and not result["attestation"]:
                page.wait_for_timeout(500)
            if not result["attestation"]:
                try:
                    serialized = page.evaluate(
                        """() => { const values = [document.documentElement.outerHTML];
                           for (const key of ['__remixContext','__NEXT_DATA__','__reactRouterContext']) {
                             try { if (window[key]) values.push(JSON.stringify(window[key])); } catch (_) {} }
                           return values.join('\\n'); }"""
                    )
                    found = _upi_find_attestation(serialized)
                    if found:
                        result["attestation"] = found
                except Exception:
                    pass
            for cookie in context.cookies("https://chatgpt.com/"):
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if name and value:
                    result["cookies"][name] = value
            # A Sentinel minted inside the same page carries the browser's real
            # fingerprint; an out-of-band token is not interchangeable when the
            # risk engine correlates it with the observed browser session.
            flow = str(sentinel_flow or "").strip()
            if flow:
                try:
                    sdk_ms = _upi_int_value(min(max(5.0, _upi_int_value(timeout_s)[0] or 30) * 1000, 12000))[0] or 8000
                    if not page.evaluate("() => typeof globalThis.SentinelSDK === 'object'"):
                        page.add_script_tag(url="https://chatgpt.com/backend-api/sentinel/sdk.js")
                    page.wait_for_function(
                        "() => globalThis.SentinelSDK && typeof globalThis.SentinelSDK.token === 'function'",
                        timeout=sdk_ms,
                    )
                    token = page.evaluate(
                        """async ({flow, timeoutMs}) => {
                          const sdk = globalThis.SentinelSDK;
                          const value = await Promise.race([
                            (async () => {
                              if (typeof sdk.init === 'function') await sdk.init(flow);
                              return sdk.token(flow);
                            })(),
                            new Promise((_, reject) => setTimeout(
                              () => reject(new Error('Sentinel token timeout')), timeoutMs)),
                          ]);
                          return typeof value === 'string' ? value : JSON.stringify(value);
                        }""",
                        {"flow": flow, "timeoutMs": sdk_ms},
                    )
                    decoded = json.loads(str(token or ""))
                    if (
                        decoded.get("id") == str(device_id)
                        and decoded.get("flow") == flow
                        and decoded.get("c")
                        and decoded.get("p")
                    ):
                        result["sentinel_token"] = str(token)
                        result["sentinel_flow"] = flow
                except Exception as exc:
                    detail = f"browser_sentinel:{type(exc).__name__}"
                    result["error"] = ((result["error"] + "; ") if result["error"] else "") + detail
            try:
                stripe_cookies = [
                    cookie
                    for cookie in context.cookies()
                    if str(cookie.get("name") or "") in {"__stripe_mid", "__stripe_sid"}
                ]
            except Exception:
                stripe_cookies = []
            for cookie in stripe_cookies:
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if name == "__stripe_mid" and value:
                    result["stripe_ids"].setdefault("muid", value)
                elif name == "__stripe_sid" and value:
                    result["stripe_ids"].setdefault("sid", value)
            browser.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return result
def _upi_browser_approve(
    *,
    access_token: Any,
    session_token: Any,
    device_id: Any,
    proxy: Any,
    fingerprint: Any,
    processor_entity: Any,
    cs_id: Any,
    timeout_s: float = 40.0,
) -> dict[str, Any]:
    """Perform the ChatGPT ``checkout/approve`` call inside a real browser.

    Replaying a captured observation from curl_cffi is not always enough: the
    risk engine also correlates the request with the browser session that
    produced it. This launches a headless Chromium on the Checkout page, lets
    the page emit its own signed observation, then issues the approve POST from
    the page itself so the cookies, TLS and observation all belong to the same
    real session.
    """
    result: dict[str, Any] = {
        "ok": False,
        "status": 0,
        "result": "",
        "observation": "",
        "attestation": "",
        "error": "",
    }
    if not _upi_browser_available():
        result["error"] = "playwright_unavailable"
        return result
    from playwright.sync_api import sync_playwright

    fp = fingerprint if isinstance(fingerprint, Mapping) else {}
    locale = str(fp.get("locale") or "en-US")
    timezone_id = str(fp.get("timezone") or "UTC")
    user_agent = str(fp.get("user_agent") or "").strip()
    page_url = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
    page_ms = _upi_int_value(min(max(10.0, _upi_int_value(timeout_s)[0] or 40) * 1000, 60000))[0] or 40000
    seen: dict[str, str] = {}
    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {
                "headless": True,
                "timeout": page_ms,
                "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
            }
            pw_proxy = _upi_browser_proxy(proxy)
            if pw_proxy:
                launch["proxy"] = pw_proxy
            browser = playwright.chromium.launch(**launch)
            context = browser.new_context(user_agent=user_agent or None, locale=locale, timezone_id=timezone_id)
            context.add_cookies(
                [
                    {"name": "oai-did", "value": str(device_id or ""), "url": "https://chatgpt.com/"},
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": str(session_token or ""),
                        "url": "https://chatgpt.com/",
                    },
                ]
            )

            def observe(request: Any) -> None:
                try:
                    raw_headers = request.all_headers()
                except Exception:
                    raw_headers = getattr(request, "headers", None)
                headers = (
                    {str(k).lower(): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, Mapping) else {}
                )
                if headers.get("x-oai-is-client-observation"):
                    seen["observation"] = headers["x-oai-is-client-observation"]
                if headers.get("oai-web-deployment-attestation"):
                    seen["attestation"] = headers["oai-web-deployment-attestation"]

            def route_handler(route: Any) -> None:
                request = route.request
                low = str(request.url or "").lower()
                if "chatgpt.com/backend-api/" in low:
                    try:
                        headers = dict(request.all_headers())
                    except Exception:
                        fallback = request.headers
                        headers = dict(fallback) if isinstance(fallback, Mapping) else {}
                    headers["authorization"] = f"Bearer {access_token}"
                    headers["oai-device-id"] = str(device_id)
                    if seen.get("observation"):
                        headers["x-oai-is-client-observation"] = seen["observation"]
                    if seen.get("attestation"):
                        headers["oai-web-deployment-attestation"] = seen["attestation"]
                    route.continue_(headers=headers)
                else:
                    route.continue_()

            page = context.new_page()
            context.on("request", observe)
            page.route("**/*", route_handler)
            page.goto(page_url, wait_until="domcontentloaded", timeout=page_ms)
            page.wait_for_timeout(2500)
            if not seen.get("observation"):
                try:
                    page.evaluate(
                        """() => fetch('/backend-api/models?history_and_training_disabled=false',
                           {credentials: 'include'}).then(r => r.text()).catch(() => '')"""
                    )
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
            result["observation"] = seen.get("observation", "")
            result["attestation"] = seen.get("attestation", "")
            payload = page.evaluate(
                """async ({csId, processor, device}) => {
                  const r = await fetch('/backend-api/payments/checkout/approve', {
                    method: 'POST', credentials: 'include',
                    headers: {'content-type': 'application/json', 'accept': 'application/json', 'oai-device-id': device},
                    body: JSON.stringify({checkout_session_id: csId, processor_entity: processor}),
                  });
                  const text = await r.text();
                  return {status: r.status, body: text.slice(0, 4000)};
                }""",
                {"csId": str(cs_id), "processor": str(processor_entity), "device": str(device_id)},
            )
            result["status"] = int((payload or {}).get("status") or 0)
            try:
                data = json.loads(str((payload or {}).get("body") or "{}"))
            except Exception:
                data = {}
            result["result"] = str((data or {}).get("result") or "").strip().lower()
            result["ok"] = result["status"] < 400 and result["result"] == "approved"
            if not result["ok"]:
                result["error"] = f"status={result['status']} result={result['result'] or 'unknown'}"
            browser.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return result
def _upi_browser_id() -> str:
    """Stripe 的 guid / muid / sid 形态: ``randomUUID()`` 拼 6 位 hex = 42 字符。

    HAR 实测（blog.caowo.de《Stripe protocol payment automation deep dive 2026》
    §3.1 / §7）浏览器发的是 42 字符；旧实现只取 ``uuid4().hex[:16]``（16 字符），
    比「32 位纯 hex 也能用」的兼容下限还短一截，属于 Stripe Radar 能直接看出来的
    指纹偏差。
    """
    return f"{uuid.uuid4()}{secrets.token_hex(3)}"
