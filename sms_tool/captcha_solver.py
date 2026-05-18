"""Passive CAPTCHA solver using Playwright + postMessage bridge.

Solves invisible hCaptcha challenges by loading the vendor's iframe in a
controlled browser context, injecting fraud signals (mouse, pointer, keyboard),
and extracting the response token via a local HTTP bridge server.

Reference: byte-v-forge/register gopay-payment/gopay-flow/gopay.py
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import re
import socketserver
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

# ──────────────────────────── constants ────────────────────────────

# Stripe's hCaptcha asset version (used in the wrapper iframe URL)
_DEFAULT_HCAPTCHA_ASSET_VERSION = "v32.5"

# Fallback hCaptcha site key (Stripe's default)
_HCAPTCHA_SITE_KEY_FALLBACK = "c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a"

# PayPal reCAPTCHA site key (commonly used)
_PAYPAL_RECAPTCHA_SITE_KEY = "6LdkHAAAAAF3Mv0gfj0r2j1w8bDM8r2V4Q5"


# ──────────────────────────── public API ────────────────────────────


def extract_captcha_config(html: str) -> dict[str, str]:
    """Extract CAPTCHA site_key and rqdata from page HTML.

    Searches for hCaptcha and reCAPTCHA data attributes and script variables.
    Returns dict with keys: type (hcaptcha|recaptcha), site_key, rqdata.
    """
    # hCaptcha data-sitekey
    m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html, re.I)
    if m:
        site_key = m.group(1)
        rqdata_m = re.search(r'data-rqdata=["\']([^"\']*)["\']', html, re.I)
        rqdata = rqdata_m.group(1) if rqdata_m else ""
        return {"type": "hcaptcha", "site_key": site_key, "rqdata": rqdata}

    # reCAPTCHA data-sitekey
    m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html, re.I)
    if m:
        return {"type": "recaptcha", "site_key": m.group(1), "rqdata": ""}

    # JavaScript variable patterns
    for pattern, captcha_type in [
        (r'"hcaptcha_site_key"\s*:\s*"([^"]+)"', "hcaptcha"),
        (r'"site_key"\s*:\s*"([^"]+)"', "hcaptcha"),
        (r'"recaptchaSiteKey"\s*:\s*"([^"]+)"', "recaptcha"),
        (r'"recaptcha_site_key"\s*:\s*"([^"]+)"', "recaptcha"),
    ]:
        m = re.search(pattern, html, re.I)
        if m:
            site_key = m.group(1)
            rqdata_m = re.search(r'"(?:hcaptcha_)?rqdata"\s*:\s*"([^"]*)"', html, re.I)
            rqdata = rqdata_m.group(1) if rqdata_m else ""
            return {"type": captcha_type, "site_key": site_key, "rqdata": rqdata}

    return {"type": "", "site_key": "", "rqdata": ""}


def solve_captcha(
    html: str,
    *,
    proxy: str = "",
    headless: bool = True,
    timeout_ms: int = 120000,
    locale: str = "en-US",
    log: Callable[[str], None] = print,
) -> tuple[str, str]:
    """Extract CAPTCHA config from HTML and attempt to solve it.

    Returns (token, ekey) on success. Raises CaptchaError on failure.
    """
    config = extract_captcha_config(html)
    captcha_type = config.get("type", "")
    site_key = config.get("site_key", "")

    if not captcha_type or not site_key:
        raise CaptchaError("no CAPTCHA found in page HTML")

    log(f"[captcha] detected {captcha_type} site_key={site_key[:16]}...")

    if captcha_type == "hcaptcha":
        return _solve_hcaptcha(
            site_key=site_key,
            rqdata=config.get("rqdata", ""),
            proxy=proxy,
            headless=headless,
            timeout_ms=timeout_ms,
            locale=locale,
            log=log,
        )
    elif captcha_type == "recaptcha":
        return _solve_recaptcha(
            site_key=site_key,
            proxy=proxy,
            headless=headless,
            timeout_ms=timeout_ms,
            locale=locale,
            log=log,
        )
    else:
        raise CaptchaError(f"unsupported CAPTCHA type: {captcha_type}")


# ──────────────────────────── hCaptcha solver ────────────────────────────


def _build_hcaptcha_bridge_url(
    invisible: bool = True,
    frame_id: str = "",
    origin: str = "https://js.stripe.com",
) -> str:
    """Build the Stripe hCaptcha wrapper iframe URL."""
    frame = frame_id or str(uuid.uuid4())
    page_name = "HCaptchaInvisible.html" if invisible else "HCaptcha.html"
    return (
        "https://b.stripecdn.com/stripethirdparty-srv/assets/"
        f"{_DEFAULT_HCAPTCHA_ASSET_VERSION}/{page_name}"
        f"?id={frame}&origin={quote(origin, safe='')}"
    )


def _build_hcaptcha_bridge_html(
    frame_id: str,
    wrapper_url: str,
    site_key: str,
    rqdata: str,
    merchant_id: str = "",
    locale: str = "en-US",
) -> str:
    """Build the parent HTML page that communicates with the hCaptcha iframe.

    The parent page:
    1. Listens for the iframe's 'frame-ready' message
    2. Sends INITIALIZE_HCAPTCHA_INVISIBLE to set up the site key
    3. Injects fraud signals (mouse, pointer, keyboard events)
    4. Sends EXECUTE_HCAPTCHA_INVISIBLE to trigger solving
    5. Captures the RESPONSE_HCAPTCHA_INVISIBLE token
    """
    init_payload = {
        "tag": "INITIALIZE_HCAPTCHA_INVISIBLE",
        "message": {"sitekey": site_key},
    }
    execute_payload = {
        "tag": "EXECUTE_HCAPTCHA_INVISIBLE",
        "message": {
            "sitekey": site_key,
            "rqdata": rqdata,
            "data": {
                "merchant_id": merchant_id or "",
                "locale": locale or "",
                "flow": "passive_captcha",
                "captcha_vendor": "hcaptcha",
            },
        },
    }
    signal_payloads = [
        {
            "tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
            "message": {"type": "mouse", "eventName": "mousemove", "coordinates": {"x": 168, "y": 132}},
        },
        {
            "tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
            "message": {"type": "pointer", "eventName": "pointermove", "coordinates": {"x": 214, "y": 176}},
        },
        {
            "tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
            "message": {"type": "keyboard", "eventName": "keydown"},
        },
    ]
    return f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>hCaptcha Bridge</title></head>
  <body>
    <iframe id="captchaFrame" src="{wrapper_url}" style="width:420px;height:720px;border:0"></iframe>
    <script>
      const frameID = {json.dumps(frame_id)};
      const initPayload = {json.dumps(init_payload, ensure_ascii=False)};
      const executePayload = {json.dumps(execute_payload, ensure_ascii=False)};
      const signalPayloads = {json.dumps(signal_payloads, ensure_ascii=False)};
      let initialized = false;
      let executed = false;

      function postToBridge(path, payload) {{
        fetch(path, {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify(payload || {{}}),
          keepalive: true,
        }}).catch(() => {{}});
      }}

      function postToChild(source, origin, payload) {{
        source.postMessage({{
          type: "stripe-third-party-parent-to-child",
          frameID,
          payload,
        }}, origin);
      }}

      function initialize(source, origin) {{
        if (initialized) return;
        initialized = true;
        postToBridge("/event", {{type: "invisible_initialize"}});
        postToChild(source, origin, initPayload);
      }}

      function execute(source, origin) {{
        if (executed) return;
        executed = true;
        signalPayloads.forEach((payload, idx) => setTimeout(() => postToChild(source, origin, payload), 50 * idx));
        setTimeout(() => {{
          postToBridge("/event", {{type: "invisible_execute"}});
          postToChild(source, origin, executePayload);
        }}, 180);
      }}

      window.addEventListener("message", (event) => {{
        const data = event.data || {{}};
        if (data.type === "stripe-third-party-frame-ready" && data.frameID === frameID) {{
          postToBridge("/event", {{type: "frame_ready", origin: event.origin}});
          initialize(event.source, event.origin);
          return;
        }}
        if (data.type !== "stripe-third-party-child-to-parent" || data.frameID !== frameID) return;
        const payload = data.payload || {{}};
        postToBridge("/event", {{type: "child_payload", tag: payload.tag || ""}});
        if (payload.tag === "LOAD_HCAPTCHA_INVISIBLE") {{
          execute(event.source, event.origin);
          return;
        }}
        if (payload.tag === "RESPONSE_HCAPTCHA_INVISIBLE") {{
          const value = payload.value || {{}};
          postToBridge("/result", {{
            response: value.response || "",
            ekey: value.key || "",
            duration: value.duration || 0,
            raw: payload,
          }});
          return;
        }}
        if (payload.tag === "ERROR_HCAPTCHA_INVISIBLE") {{
          postToBridge("/error", {{error: (payload.value || {{}}).error || "unknown_error", raw: payload}});
        }}
      }});
    </script>
  </body>
</html>
"""


def _playwright_proxy(proxy_url: str) -> Optional[dict]:
    """Convert a proxy URL to Playwright proxy config."""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return None
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        if not host:
            return None
        server = f"{parsed.scheme or 'http'}://{host}"
        if parsed.port:
            server += f":{parsed.port}"
        proxy = {"server": server, "bypass": "127.0.0.1,localhost"}
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy
    except Exception:
        return None


def _accept_language_for_locale(locale_value: str | None) -> str:
    locale = (locale_value or "").strip().lower()
    if locale.startswith("zh"):
        return "zh-CN,zh;q=0.9,en;q=0.8"
    if locale.startswith("id"):
        return "id-ID,id;q=0.9,en;q=0.8"
    return "en-US,en;q=0.9"


def _solve_hcaptcha(
    *,
    site_key: str,
    rqdata: str = "",
    proxy: str = "",
    headless: bool = True,
    timeout_ms: int = 120000,
    locale: str = "en-US",
    log: Callable[[str], None] = print,
) -> tuple[str, str]:
    """Solve an invisible hCaptcha using Playwright + postMessage bridge.

    1. Build a bridge HTML page that embeds the Stripe hCaptcha wrapper iframe
    2. Start a local HTTP server to relay messages between parent and child frames
    3. Launch Playwright, load the bridge page
    4. The bridge injects fraud signals then triggers hCaptcha execution
    5. Extract the response token from the bridge server's /result endpoint
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CaptchaError("playwright is required for hCaptcha solving: pip install playwright")

    with tempfile.TemporaryDirectory(prefix="hcaptcha-bridge-") as tmpdir:
        bridge_state: dict[str, Any] = {"events": [], "result": None, "error": None}
        result_event = threading.Event()
        error_event = threading.Event()

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, directory=tmpdir, **kwargs)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _write_json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except Exception:
                    payload = {}
                if self.path == "/event":
                    bridge_state["events"].append(payload)
                    self._write_json(200, {"ok": True})
                    return
                if self.path == "/result":
                    bridge_state["result"] = payload
                    result_event.set()
                    self._write_json(200, {"ok": True})
                    return
                if self.path == "/error":
                    bridge_state["error"] = payload
                    error_event.set()
                    self._write_json(200, {"ok": True})
                    return
                self._write_json(404, {"error": "not found"})

        class _BridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        httpd = _BridgeServer(("127.0.0.1", 0), _QuietHandler)
        port = httpd.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        frame_id = str(uuid.uuid4())
        wrapper_url = _build_hcaptcha_bridge_url(invisible=True, frame_id=frame_id, origin=origin)
        html = _build_hcaptcha_bridge_html(
            frame_id=frame_id,
            wrapper_url=wrapper_url,
            site_key=site_key,
            rqdata=rqdata,
            locale=locale,
        )
        with open(os.path.join(tmpdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)

        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        playwright_ctx = None
        browser = None
        page = None
        try:
            log(f"[captcha] hCaptcha solver site_key={site_key[:16]} headless={headless}")
            playwright_ctx = sync_playwright().start()
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            pw_proxy = _playwright_proxy(proxy)
            if pw_proxy:
                launch_kwargs["proxy"] = pw_proxy
            browser = playwright_ctx.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1280, "height": 960},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                locale=locale or "en-US",
                timezone_id="America/New_York",
                extra_http_headers={"Accept-Language": _accept_language_for_locale(locale)},
            )
            page = context.new_page()
            page.goto(f"{origin}/index.html", wait_until="domcontentloaded", timeout=60000)

            deadline = time.time() + timeout_ms / 1000
            logged_events = 0
            while time.time() < deadline:
                events = bridge_state["events"]
                while logged_events < len(events):
                    event = events[logged_events]
                    logged_events += 1
                    event_type = event.get("type") or "event"
                    tag = event.get("tag") or ""
                    if tag:
                        log(f"[captcha] invisible payload: {tag}")
                    elif event_type in ("frame_ready", "invisible_initialize", "invisible_execute"):
                        log(f"[captcha] invisible event: {event_type}")
                if result_event.wait(timeout=1):
                    result = bridge_state.get("result") or {}
                    token = str(result.get("response") or "")
                    ekey = str(result.get("ekey") or "")
                    if token:
                        log(f"[captcha] solved token_len={len(token)} ekey_len={len(ekey)}")
                        return token, ekey
                    raise CaptchaError("hCaptcha returned empty token")
                if error_event.is_set():
                    err = bridge_state.get("error") or {}
                    raise CaptchaError(f"hCaptcha error: {str(err)[:240]}")
            raise CaptchaError(f"hCaptcha timeout ({timeout_ms // 1000}s)")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright_ctx is not None:
                try:
                    playwright_ctx.stop()
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()


# ──────────────────────────── reCAPTCHA solver ────────────────────────────


def _solve_recaptcha(
    *,
    site_key: str,
    proxy: str = "",
    headless: bool = True,
    timeout_ms: int = 120000,
    locale: str = "en-US",
    log: Callable[[str], None] = print,
) -> tuple[str, str]:
    """Solve reCAPTCHA v2/v3 using Playwright.

    Loads the page with the reCAPTCHA widget, solves it via the browser,
    and extracts the response token. This is a simplified solver that works
    for invisible reCAPTCHA v3 and some v2 variants.

    For more complex reCAPTCHA challenges (image selection), consider using
    a CAPTCHA solving service API instead.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CaptchaError("playwright is required for reCAPTCHA solving: pip install playwright")

    log(f"[captcha] reCAPTCHA solver site_key={site_key[:16]} (basic mode)")

    # Build a minimal page with the reCAPTCHA widget
    html_content = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>reCAPTCHA Bridge</title>
    <script src="https://www.google.com/recaptcha/api.js?render={site_key}"></script>
  </head>
  <body>
    <div id="status">loading</div>
    <script>
      grecaptcha.ready(function() {{
        document.getElementById('status').textContent = 'ready';
        grecaptcha.execute('{site_key}', {{action: 'submit'}}).then(function(token) {{
          document.getElementById('status').textContent = 'solved';
          window._captchaToken = token;
        }}).catch(function(err) {{
          document.getElementById('status').textContent = 'error: ' + err.message;
        }});
      }});
    </script>
  </body>
</html>"""

    with tempfile.TemporaryDirectory(prefix="recaptcha-bridge-") as tmpdir:
        html_path = os.path.join(tmpdir, "index.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        playwright_ctx = None
        browser = None
        page = None
        try:
            playwright_ctx = sync_playwright().start()
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            pw_proxy = _playwright_proxy(proxy)
            if pw_proxy:
                launch_kwargs["proxy"] = pw_proxy
            browser = playwright_ctx.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1280, "height": 960},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                locale=locale or "en-US",
            )
            page = context.new_page()

            # Navigate to a data URI with the bridge HTML
            page.set_content(html_content, wait_until="domcontentloaded")

            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline:
                status = page.evaluate("document.getElementById('status').textContent")
                if status == "solved":
                    token = page.evaluate("window._captchaToken || ''")
                    if token:
                        log(f"[captcha] reCAPTCHA solved token_len={len(token)}")
                        return token, ""
                    raise CaptchaError("reCAPTCHA returned empty token")
                if status.startswith("error"):
                    raise CaptchaError(f"reCAPTCHA error: {status}")
                time.sleep(0.5)
            raise CaptchaError(f"reCAPTCHA timeout ({timeout_ms // 1000}s)")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright_ctx is not None:
                try:
                    playwright_ctx.stop()
                except Exception:
                    pass


# ──────────────────────────── exceptions ────────────────────────────


class CaptchaError(Exception):
    """Raised when CAPTCHA solving fails."""
