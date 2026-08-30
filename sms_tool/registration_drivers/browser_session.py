"""Native Playwright browser lifecycle used by the registration driver."""

from __future__ import annotations

import time
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from ..phone_proxy import normalize_proxy_url
from .stealth import apply_playwright_stealth


def _playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    value = normalize_proxy_url(proxy)
    if not value:
        return None
    parsed = urlsplit(value)
    # Chromium's proxy parser rejects the `socks5h://` scheme (hostnames must be
    # resolved through the proxy tunnel, which Chromium handles via the plain
    # `socks5` scheme). The local proxy bridge hands back a `socks5h://` URL, so
    # normalize it to `socks5` here. Firefox/camoufox accept `socks5h` directly,
    # but every Chromium-based driver routes through this helper.
    scheme = "socks5" if parsed.scheme == "socks5h" else (parsed.scheme or "http")
    server = f"{scheme}://{parsed.hostname or ''}"
    if parsed.port:
        server += f":{parsed.port}"
    result = {"server": server}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


class PlaywrightBrowserSession:
    """Small sync Playwright wrapper with deterministic cleanup and cookie export."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        headless: bool = True,
        timeout_ms: int = 45_000,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        user_data_dir: str = "",
        viewport: tuple[int, int] | None = None,
    ) -> None:
        self.proxy = proxy
        self.headless = bool(headless)
        self.timeout_ms = max(5_000, int(timeout_ms or 45_000))
        self.locale = str(locale or "en-US")
        self.timezone_id = str(timezone_id or "America/New_York")
        self.user_data_dir = str(user_data_dir or "").strip()
        # Rotated screen profile (browser fingerprint pool).  Defaults to the
        # historical 1440x900 so behavior is unchanged when no pool is drawn.
        if viewport and len(viewport) == 2 and viewport[0] > 0 and viewport[1] > 0:
            self.viewport: dict[str, int] = {"width": int(viewport[0]), "height": int(viewport[1])}
        else:
            self.viewport = {"width": 1440, "height": 900}
        self._persistent = bool(self.user_data_dir)
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.stealth_status: dict[str, Any] = {"playwright_stealth": False}

    def __enter__(self) -> "PlaywrightBrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("browser_dependency_missing:playwright") from exc
        try:
            self._playwright = sync_playwright().start()
            browser_proxy = _playwright_proxy(self.proxy)
            if self.user_data_dir:
                import pathlib
                pathlib.Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
                launch_kwargs: dict[str, Any] = {"headless": self.headless}
                if browser_proxy:
                    launch_kwargs["proxy"] = browser_proxy
                self.context = self._playwright.chromium.launch_persistent_context(
                    self.user_data_dir,
                    locale=self.locale,
                    timezone_id=self.timezone_id,
                    viewport=self.viewport,
                    **launch_kwargs,
                )
                self.browser = getattr(self.context, "browser", None) or self.context
                self._persistent = True
            else:
                launch_kwargs = {"headless": self.headless}
                if browser_proxy:
                    launch_kwargs["proxy"] = browser_proxy
                self.browser = self._playwright.chromium.launch(**launch_kwargs)
                self.context = self.browser.new_context(
                    locale=self.locale,
                    timezone_id=self.timezone_id,
                    viewport=self.viewport,
                )
            self.context.set_default_timeout(self.timeout_ms)
            pages = list(getattr(self.context, "pages", []) or [])
            self.page = pages[0] if pages else self.context.new_page()
            self.stealth_status = apply_playwright_stealth(
                self.context,
                self.page,
                label="playwright",
                provider_prefix="playwright",
            )
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        for item in (self.context, self.browser, self._playwright):
            if item is None:
                continue
            try:
                item.close() if item is not self._playwright else item.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None
        self.stealth_status = {"playwright_stealth": False}

    def release_account_context(self) -> None:
        """Close only the per-account context, keeping the browser process alive.

        Used by the browser process pool: after one registration finishes the
        browser is recycled for the next account, so only the isolated context
        (cookies / storage / sessions) must be torn down.  The browser process
        and the Playwright driver stay resident.
        """
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.stealth_status = {"playwright_stealth": False}

    def renew_account_context(
        self,
        *,
        locale: str | None = None,
        timezone_id: str | None = None,
        viewport: tuple[int, int] | None = None,
    ) -> "PlaywrightBrowserSession":
        """Create a fresh, isolated context+page on the resident browser.

        Mirrors ``__enter__``'s context setup but reuses the already-launched
        ``self.browser`` instead of spawning a new process.  Proxy is inherited
        from the launch (the pool binds one egress per resident browser), while
        locale / timezone / viewport can be re-supplied per account.
        """
        self.release_account_context()
        if self.browser is None or self._playwright is None:
            raise RuntimeError("browser_not_resident")
        loc = str(locale or self.locale or "en-US")
        tz = str(timezone_id or self.timezone_id or "America/New_York")
        if viewport and len(viewport) == 2 and viewport[0] > 0 and viewport[1] > 0:
            vp: dict[str, int] = {"width": int(viewport[0]), "height": int(viewport[1])}
        else:
            vp = dict(self.viewport)
        self.context = self.browser.new_context(locale=loc, timezone_id=tz, viewport=vp)
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.new_page()
        self.stealth_status = apply_playwright_stealth(
            self.context,
            self.page,
            label="playwright",
            provider_prefix="playwright",
        )
        return self

    def cookie_header(self) -> str:
        if self.context is None:
            return ""
        cookies = self.context.cookies()
        return "; ".join(
            f"{item.get('name')}={item.get('value')}"
            for item in cookies
            if item.get("name") and item.get("value")
        )

    def cookies(self) -> list[dict[str, Any]]:
        return list(self.context.cookies()) if self.context is not None else []

    @staticmethod
    def _page_is_open(page: Any) -> bool:
        try:
            closed = getattr(page, "is_closed", None)
            if not callable(closed):
                return True
            value = closed()
            return not value if isinstance(value, bool) else True
        except Exception:
            return False

    def select_live_page(self) -> Any:
        """Adopt the best live auth/callback target after SPA window churn."""
        pages = list(getattr(self.context, "pages", []) or []) if self.context is not None else []
        candidates = [page for page in pages if self._page_is_open(page)]
        if not candidates:
            return self.page

        def score(page: Any) -> int:
            try:
                parsed = urlsplit(str(page.url or ""))
                host = str(parsed.hostname or "").lower()
                path = str(parsed.path or "").lower()
            except Exception:
                return 0
            # Prefer the active auth step over a stale ChatGPT tab.  Cloud
            # providers commonly leave the callback page open while creating
            # a second page for the final session.
            if any(marker in path for marker in ("email-verification", "email_verification", "/verify", "/otp", "/code")):
                return 100
            if "password" in path:
                return 90
            if any(marker in path for marker in ("about-you", "profile", "create-account")):
                return 80
            if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
                return 70 if "/auth/" not in path else 60
            if host == "auth.openai.com" or host.endswith(".auth.openai.com"):
                return 50
            if host == "openai.com" or host.endswith(".openai.com"):
                return 40
            return 0

        self.page = max(reversed(candidates), key=score)
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        return self.page

    @staticmethod
    def _is_chatgpt_url(url: str) -> bool:
        host = str(urlsplit(str(url or "")).hostname or "").lower()
        return host == "chatgpt.com" or host.endswith(".chatgpt.com")

    def switch_to_chatgpt_page(self) -> bool:
        pages = list(getattr(self.context, "pages", []) or []) if self.context is not None else []
        for page in pages:
            try:
                if self._page_is_open(page) and self._is_chatgpt_url(str(page.url or "")):
                    self.page = page
                    return True
            except Exception:
                continue
        return False

    def ensure_chatgpt_context(self, *, auto_jump_wait: int = 15) -> bool:
        """Wait for OAuth's callback page/window before opening ChatGPT."""
        if self.page is None:
            return False
        deadline = time.monotonic() + max(0, int(auto_jump_wait or 0))
        while True:
            try:
                if self._is_chatgpt_url(str(self.page.url or "")):
                    return True
            except Exception:
                pass
            if self.switch_to_chatgpt_page():
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        try:
            self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            try:
                self.page.evaluate("window.stop()")
            except Exception:
                pass
        try:
            return self._is_chatgpt_url(str(self.page.url or ""))
        except Exception:
            return False

    def add_device_cookie(self, device_id: str, chat_base: str, auth_base: str) -> None:
        if self.context is None or not str(device_id or "").strip():
            return
        cookies = []
        for base in (chat_base, auth_base):
            parsed = urlsplit(str(base or ""))
            if parsed.scheme and parsed.netloc:
                cookies.append({"name": "oai-did", "value": str(device_id), "url": f"{parsed.scheme}://{parsed.netloc}/"})
        if cookies:
            self.context.add_cookies(cookies)

    def fetch_json(
        self,
        url: str,
        *,
        timeout_ms: int = 20_000,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.select_live_page()
        if self.page is None:
            raise RuntimeError("browser_session_not_started")
        parsed = urlsplit(str(url or ""))
        is_chatgpt_session = self._is_chatgpt_url(str(url or "")) and parsed.path.rstrip("/") == "/api/auth/session"
        request_context = getattr(self.context, "request", None) if self.context is not None else None
        request_payload: dict[str, Any] | None = None
        if is_chatgpt_session and request_context is not None:
            try:
                response = request_context.get(str(url), timeout=int(timeout_ms))
                text = response.text()
                try:
                    body = response.json()
                except Exception:
                    body = {"raw": str(text or "")[:500]}
                payload = {"status": int(response.status or 0), "body": body if isinstance(body, dict) else {}}
                request_payload = payload
                candidate = payload["body"].get("session") if isinstance(payload["body"].get("session"), dict) else payload["body"]
                if candidate.get("accessToken") or candidate.get("access_token"):
                    return payload
            except Exception:
                request_payload = None
        if request_payload is not None:
            body = request_payload.get("body") or {}
            marker = " ".join(str(body.get(key) or "").lower() for key in ("error", "code")) if isinstance(body, dict) else ""
            status = int(request_payload.get("status") or 0)
            # Preserve terminal/diagnostic responses for the caller.  Returning
            # them is important: a page fallback can otherwise turn a 401 or
            # NextAuth error into a misleading empty 200 response.
            if status >= 400 or marker.strip():
                return request_payload
        if is_chatgpt_session and not self.ensure_chatgpt_context():
            return {"status": 0, "body": {"error": "chatgpt_context_unavailable"}}
        # Same-origin requirement for browser-side API calls.  select_live_page()
        # above may have adopted a leftover OTP/verify tab (higher score than the
        # chatgpt.com tab), which makes Firefox-based drivers (camoufox) block the
        # cross-origin fetch and the probe collapses to status 0 / error.  Force
        # the evaluate onto a chatgpt.com page when the target host is chatgpt.
        if not is_chatgpt_session and self._is_chatgpt_url(str(url or "")):
            cur_host = str(urlsplit(str(getattr(self.page, "url", "") or "")).hostname or "").lower()
            if cur_host != "chatgpt.com":
                self.switch_to_chatgpt_page()
        target = "/api/auth/session" if is_chatgpt_session else str(url)
        request_headers = {"accept": "application/json"}
        if isinstance(headers, Mapping):
            request_headers.update({str(key): str(value) for key, value in headers.items() if value is not None})
        raw = self.page.evaluate(
            """async ({url, timeout, headers}) => {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), timeout);
              try {
                const response = await fetch(url, {
                  credentials: 'include', cache: 'no-store',
                  headers, signal: controller.signal
                });
                const text = await response.text();
                let body;
                try { body = JSON.parse(text); } catch (_) { body = {raw: text.slice(0, 500)}; }
                return {status: response.status, body};
              } finally { clearTimeout(timer); }
            }""",
            {"url": target, "timeout": int(timeout_ms), "headers": request_headers},
        )
        return raw if isinstance(raw, dict) else {"status": 0, "body": {}}


__all__ = ["PlaywrightBrowserSession", "_playwright_proxy"]
