"""Managed browser sessions for optional local and cloud registration drivers."""

from __future__ import annotations

import random
import json
import os
import time
from typing import Any, Mapping
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from curl_cffi import requests as curl_requests

from ..env_loader import ensure_loaded
from ..phone_proxy import normalize_proxy_url
from .base import BrowserRegistrationError, normalize_registration_driver
from .browser_session import PlaywrightBrowserSession, _playwright_proxy
from .stealth import apply_playwright_stealth


def _driver_config(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    ensure_loaded()
    registration = config.get("registration")
    drivers = registration.get("drivers") if isinstance(registration, Mapping) else {}
    value = drivers.get(name) if isinstance(drivers, Mapping) else {}
    result = dict(value) if isinstance(value, Mapping) else {}
    # Deployment secrets override JSON values while remaining out of config
    # persistence and diagnostic payloads.
    env_overrides = {
        "roxy": {
            "api_token": ("ROXY_API_TOKEN", "str"),
            "api_base": ("ROXY_API_BASE", "str"),
            "profile_id": ("ROXY_PROFILE_ID", "str"),
            "workspace_id": ("ROXY_WORKSPACE_ID", "str"),
            "project_id": ("ROXY_PROJECT_ID", "str"),
            "workspace_list_path": ("ROXY_WORKSPACE_LIST_PATH", "str"),
            "open_path": ("ROXY_OPEN_PATH", "str"),
            "open_method": ("ROXY_OPEN_METHOD", "str"),
            "open_headless": ("ROXY_OPEN_HEADLESS", "bool"),
            "close_path": ("ROXY_CLOSE_PATH", "str"),
            "close_method": ("ROXY_CLOSE_METHOD", "str"),
            "delete_path": ("ROXY_DELETE_PATH", "str"),
            "delete_method": ("ROXY_DELETE_METHOD", "str"),
            "keep_browser_open": ("ROXY_KEEP_BROWSER_OPEN", "bool"),
            "delete_profile_after_run": ("ROXY_DELETE_PROFILE_AFTER_RUN", "bool"),
            "api_retries": ("ROXY_API_RETRIES", "int"),
            "api_retry_delay_seconds": ("ROXY_API_RETRY_DELAY", "float"),
            "backend": ("ROXY_BACKEND", "str"),
            "start_url": ("ROXY_START_URL", "str"),
            "headless": ("ROXY_HEADLESS", "bool"),
        },
        "browser_use": {
            "api_key": ("BROWSER_USE_API_KEY", "str"),
            "cdp_base": ("BROWSER_USE_CDP_BASE", "str"),
            "proxy_country_code": ("BROWSER_USE_PROXY_COUNTRY_CODE", "str"),
            "use_proxy": ("BROWSER_USE_USE_PROXY", "bool"),
            "profile_id": ("BROWSER_USE_PROFILE_ID", "str"),
            "session_timeout_minutes": ("BROWSER_USE_SESSION_TIMEOUT", "int"),
            "keep_browser_open": ("BROWSER_USE_KEEP_BROWSER_OPEN", "bool"),
            "start_url": ("BROWSER_USE_START_URL", "str"),
            "extra_query": ("BROWSER_USE_EXTRA_QUERY", "json"),
        },
        "skyvern": {
            "api_key": ("SKYVERN_API_KEY", "str"),
            "api_base": ("SKYVERN_API_BASE", "str"),
            "session_timeout_minutes": ("SKYVERN_BROWSER_SESSION_TIMEOUT", "int"),
            "profile_id": ("SKYVERN_BROWSER_PROFILE_ID", "str"),
            "proxy_location": ("SKYVERN_PROXY_LOCATION", "str"),
            "generate_browser_profile": ("SKYVERN_GENERATE_BROWSER_PROFILE", "bool"),
            "ad_blocker": ("SKYVERN_AD_BLOCKER", "bool"),
            "browser_type": ("SKYVERN_BROWSER_TYPE", "str"),
            "keep_browser_open": ("SKYVERN_KEEP_BROWSER_OPEN", "bool"),
            "start_url": ("SKYVERN_START_URL", "str"),
        },
        "cloak": {
            "license_key": ("CLOAK_LICENSE_KEY", "str"),
            "headless": ("CLOAK_HEADLESS", "bool"),
            "humanize": ("CLOAK_HUMANIZE", "bool"),
            "geoip": ("CLOAK_GEOIP", "bool"),
            "locale": ("CLOAK_LOCALE", "str"),
            "timezone": ("CLOAK_TIMEZONE", "str"),
            "use_proxy": ("CLOAK_USE_PROXY", "bool"),
            "fingerprint_seed": ("CLOAK_FINGERPRINT_SEED", "str"),
            "user_data_dir": ("CLOAK_USER_DATA_DIR", "str"),
            "keep_browser_open": ("CLOAK_KEEP_BROWSER_OPEN", "bool"),
            "start_url": ("CLOAK_START_URL", "str"),
        },
        "camoufox": {
            "headless": ("CAMOUFOX_HEADLESS", "bool"),
            "humanize": ("CAMOUFOX_HUMANIZE", "bool"),
            "geoip": ("CAMOUFOX_GEOIP", "bool"),
            "locale": ("CAMOUFOX_LOCALE", "str"),
            "timezone": ("CAMOUFOX_TIMEZONE", "str"),
            "use_proxy": ("CAMOUFOX_USE_PROXY", "bool"),
            "user_data_dir": ("CAMOUFOX_USER_DATA_DIR", "str"),
            "keep_browser_open": ("CAMOUFOX_KEEP_BROWSER_OPEN", "bool"),
            "start_url": ("CAMOUFOX_START_URL", "str"),
            "max_width": ("CAMOUFOX_MAX_WIDTH", "int"),
            "max_height": ("CAMOUFOX_MAX_HEIGHT", "int"),
        },
    }
    for key, (env_name, value_type) in env_overrides.get(name, {}).items():
        raw = os.getenv(env_name)
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        try:
            if value_type == "bool":
                normalized = text.lower()
                if normalized in {"1", "true", "yes", "on", "y"}:
                    result[key] = True
                elif normalized in {"0", "false", "no", "off", "n"}:
                    result[key] = False
                else:
                    continue
            elif value_type == "int":
                result[key] = int(text)
            elif value_type == "float":
                result[key] = float(text)
            elif value_type == "json":
                parsed = json.loads(text)
                if isinstance(parsed, Mapping):
                    result[key] = dict(parsed)
            else:
                result[key] = text
        except (TypeError, ValueError, json.JSONDecodeError):
            # Invalid optional environment values must not break importing the
            # module; the JSON/config value remains authoritative instead.
            continue
    return result


def _require(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BrowserRegistrationError(code)
    return text


def _first(payload: Any, *paths: tuple[str, ...]) -> str:
    for path in paths:
        current = payload
        for key in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        if current is not None and str(current).strip():
            return str(current).strip()
    return ""


def _normalize_skyvern_proxy_location(value: Any) -> str:
    """Normalize country aliases to Skyvern's residential location names."""
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper().replace("-", "_")
    aliases = {
        "JP": "RESIDENTIAL_JP",
        "JA": "RESIDENTIAL_JP",
        "JAPAN": "RESIDENTIAL_JP",
        "US": "RESIDENTIAL",
        "USA": "RESIDENTIAL",
        "GB": "RESIDENTIAL_GB",
        "UK": "RESIDENTIAL_GB",
        "IN": "RESIDENTIAL_IN",
        "DE": "RESIDENTIAL_DE",
        "FR": "RESIDENTIAL_FR",
        "AU": "RESIDENTIAL_AU",
        "CA": "RESIDENTIAL_CA",
        "KR": "RESIDENTIAL_KR",
        "NONE": "NONE",
    }
    if upper in aliases:
        return aliases[upper]
    if len(upper) == 2:
        return f"RESIDENTIAL_{upper}"
    return upper


def _normalize_skyvern_browser_type(value: Any) -> str:
    """Normalize Skyvern browser type aliases accepted by the reference client."""
    text = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": "stealth-chromium",
        "chromium": "stealth-chromium",
        "chromium-headful": "stealth-chromium",
        "headful": "stealth-chromium",
        "stealth": "stealth-chromium",
        "stealth-chrome": "stealth-chromium",
        "edge": "msedge",
        "microsoft-edge": "msedge",
    }
    return aliases.get(text, text)


def _normalize_debugger_address(value: Any) -> str:
    """Turn Roxy's port-only debugger forms into a CDP HTTP URL."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("ws://", "wss://", "http://", "https://")):
        return text
    if text.startswith(":") and text[1:].isdigit():
        return f"http://127.0.0.1{text}"
    if text.isdigit():
        return f"http://127.0.0.1:{text}"
    return f"http://{text}"


def _roxy_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "timeout", "timed out", "connection", "temporarily", "unavailable",
        "reset", "refused", "http_408", "http_409", "http_425", "http_429",
        "http_500", "http_502", "http_503", "http_504",
    ))


class _SeleniumLocator:
    """Small Playwright-like locator used by the shared registration flow."""

    def __init__(self, driver: Any, selector: str, index: int = 0) -> None:
        self.driver = driver
        self.selector = selector
        self.index = index

    @property
    def first(self) -> "_SeleniumLocator":
        return _SeleniumLocator(self.driver, self.selector, 0)

    def nth(self, index: int) -> "_SeleniumLocator":
        return _SeleniumLocator(self.driver, self.selector, index)

    def _elements(self) -> list[Any]:
        from selenium.webdriver.common.by import By

        selector = self.selector
        if ":has-text(" in selector:
            base, _, tail = selector.partition(":has-text(")
            wanted = tail.rstrip(")").strip("'\"").lower()
            return [item for item in self.driver.find_elements(By.CSS_SELECTOR, base) if wanted in str(item.text or "").lower()]
        return list(self.driver.find_elements(By.CSS_SELECTOR, selector))

    def _element(self) -> Any:
        items = self._elements()
        if self.index >= len(items):
            raise RuntimeError("selenium_locator_missing")
        return items[self.index]

    def count(self) -> int:
        return len(self._elements())

    def wait_for(self, *, state: str = "visible", timeout: int = 5_000) -> None:
        deadline = time.monotonic() + max(0.1, timeout / 1000)
        while time.monotonic() < deadline:
            try:
                item = self._element()
                if state != "visible" or (item.is_displayed() and item.is_enabled()):
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("selenium_locator_timeout")

    def is_visible(self, *, timeout: int | None = None) -> bool:
        if timeout:
            try:
                self.wait_for(timeout=timeout)
                return True
            except Exception:
                return False
        try:
            item = self._element()
            return bool(item.is_displayed() and item.is_enabled())
        except Exception:
            return False

    def fill(self, value: str) -> None:
        item = self._element()
        try:
            item.clear()
        except Exception:
            pass
        item.send_keys(str(value))

    def input_value(self) -> str:
        return str(self._element().get_attribute("value") or "")

    def click(self, **_kwargs: Any) -> None:
        item = self._element()
        try:
            item.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", item)

    def inner_text(self, *, timeout: int | None = None) -> str:
        if timeout:
            self.wait_for(timeout=timeout)
        return str(self._element().text or "")


class _SeleniumPage:
    def __init__(self, driver: Any, timeout_ms: int) -> None:
        self.driver = driver
        self.timeout_ms = timeout_ms

    @property
    def url(self) -> str:
        return str(self.driver.current_url or "")

    def title(self) -> str:
        return str(self.driver.title or "")

    def locator(self, selector: str) -> _SeleniumLocator:
        return _SeleniumLocator(self.driver, selector)

    def get_by_role(self, role: str, *, name: str, exact: bool = False) -> _SeleniumLocator:
        from selenium.webdriver.common.by import By

        tag = "button" if role == "button" else "[role='%s']" % role
        items = self.driver.find_elements(By.CSS_SELECTOR, tag)
        wanted = str(name or "").strip()
        for index, item in enumerate(items):
            text_value = str(item.text or item.get_attribute("aria-label") or "").strip()
            if (exact and text_value == wanted) or (not exact and wanted.lower() in text_value.lower()):
                return _SeleniumLocator(self.driver, tag, index)
        return _SeleniumLocator(self.driver, tag, len(items))

    def wait_for_timeout(self, timeout_ms: int) -> None:
        time.sleep(max(0, timeout_ms) / 1000)

    def evaluate(self, script: str, arg: Any = None) -> Any:
        source = str(script or "").strip()
        if source.startswith("(") or source.startswith("async"):
            # Selenium's execute_script does not await Promises. Convert the
            # async probe into a callback-based script so Roxy's WebDriver
            # backend observes the same country result as Playwright/CDP.
            if source.startswith("async"):
                source = f"var done = arguments[arguments.length - 1]; Promise.resolve(({source})(arguments[0])).then(done).catch(function () {{ done(null); }});"
                if arg is None:
                    return self.driver.execute_async_script(source)
                return self.driver.execute_async_script(source, arg)
            source = f"return ({source})(arguments[0]);"
        if arg is None:
            return self.driver.execute_script(source)
        return self.driver.execute_script(source, arg)

    def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int | None = None) -> None:
        del wait_until
        seconds = max(10, int((timeout or self.timeout_ms) / 1000))
        last_error: Exception | None = None
        accepted = {str(urlsplit(url).hostname or "").lower(), "chatgpt.com", "auth.openai.com"}
        for attempt in range(2):
            try:
                self.driver.set_page_load_timeout(seconds)
                self.driver.get(url)
                return
            except Exception as exc:
                last_error = exc
                try:
                    self.driver.execute_script("window.stop();")
                    current_host = str(urlsplit(self.url).hostname or "").lower()
                    has_body = bool(self.driver.execute_script("return !!document.body"))
                    if current_host in accepted and has_body:
                        return
                except Exception:
                    pass
                if attempt == 0:
                    time.sleep(1.5)
        raise last_error or RuntimeError("selenium_navigation_failed")


class ConnectedPlaywrightSession(PlaywrightBrowserSession):
    """Base for services exposing an existing browser through CDP."""

    def _start_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserRegistrationError("browser_dependency_missing", "playwright") from exc
        self._playwright = sync_playwright().start()

    def _adopt_browser(self, browser: Any) -> None:
        self.browser = browser
        contexts = list(getattr(browser, "contexts", []) or [])
        self.context = contexts[0] if contexts else browser.new_context(
            locale=self.locale,
            timezone_id=self.timezone_id,
            viewport={"width": 1440, "height": 900},
        )
        self.context.set_default_timeout(self.timeout_ms)
        pages = list(getattr(self.context, "pages", []) or [])
        self.page = pages[0] if pages else self.context.new_page()
        self.stealth_status = apply_playwright_stealth(
            self.context,
            self.page,
            label="connected-browser",
            provider_prefix=self.__class__.__name__.replace("BrowserSession", "").lower(),
        )

    def _close_connection(self, *, keep_browser_open: bool = False) -> None:
        if not keep_browser_open:
            for item in (self.context, self.browser):
                if item is None:
                    continue
                try:
                    item.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None


class CloakBrowserSession(ConnectedPlaywrightSession):
    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "cloak")
        self._persistent = False

    def __enter__(self):
        try:
            from cloakbrowser import launch, launch_persistent_context
        except ImportError as exc:
            raise BrowserRegistrationError("browser_dependency_missing", "cloakbrowser") from exc
        proxy = normalize_proxy_url(self.proxy) if bool(self.driver_config.get("use_proxy", True)) else ""
        geoip = bool(self.driver_config.get("geoip", True))
        options: dict[str, Any] = {
            "headless": self.headless,
            "humanize": bool(self.driver_config.get("humanize", True)),
            "geoip": geoip,
        }
        if proxy:
            options["proxy"] = proxy
        configured_locale = str(self.driver_config.get("locale") or "").strip()
        configured_timezone = str(self.driver_config.get("timezone") or "").strip()
        # With GeoIP enabled, leaving these unset lets Cloak align language,
        # timezone and WebRTC with the browser's actual exit. Applying the
        # global en-US/New_York defaults here would override that provider
        # behavior and create an avoidable country/environment mismatch.
        locale = configured_locale or ("" if geoip else self.locale)
        timezone = configured_timezone or ("" if geoip else self.timezone_id)
        if locale:
            options["locale"] = locale
        if timezone:
            options["timezone"] = timezone
        seed = str(self.driver_config.get("fingerprint_seed") or "").strip()
        if seed:
            options["args"] = [f"--fingerprint={seed}"]
        license_key = str(self.driver_config.get("license_key") or "").strip()
        if license_key:
            options["license_key"] = license_key
        user_data_dir = str(self.driver_config.get("user_data_dir") or "").strip()
        try:
            if user_data_dir:
                self.context = launch_persistent_context(user_data_dir, **options)
                self._persistent = True
                self.browser = getattr(self.context, "browser", None) or self.context
                pages = list(getattr(self.context, "pages", []) or [])
                self.page = pages[0] if pages else self.context.new_page()
            else:
                self.browser = launch(**options)
                context_options: dict[str, Any] = {}
                if locale:
                    context_options["locale"] = locale
                if timezone:
                    context_options["timezone_id"] = timezone
                self.context = self.browser.new_context(**context_options)
                self.page = self.context.new_page()
            self.context.set_default_timeout(self.timeout_ms)
            self.stealth_status = apply_playwright_stealth(
                self.context,
                self.page,
                label="cloak",
                provider_prefix="cloak",
            )
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("cloak_launch_failed", type(exc).__name__) from exc

    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        if keep_open:
            self.page = None
            self.context = None
            self.browser = None
            self._playwright = None
            return
        super().close()


class CamoufoxBrowserSession(ConnectedPlaywrightSession):
    """Browser session backed by the Camoufox anti-detect engine.

    Camoufox provides a hardened Firefox with built-in fingerprint injection,
    GeoIP-aware locale/timezone, and humanized input.  This wrapper follows
    the same contract as ``CloakBrowserSession``: a thin ``__enter__`` that
    launches the browser, sets ``self.browser``/``self.context``/``self.page``,
    and applies stealth overlays so the shared registration flow works
    unchanged.
    """

    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "camoufox")
        self._persistent = False
        self._camoufox_ctx = None
        self._proxy_bridge_closer = None

    def __enter__(self):
        try:
            from camoufox.sync_api import Camoufox
            from browserforge.fingerprints import Screen
        except ImportError as exc:
            raise BrowserRegistrationError("browser_dependency_missing", "camoufox") from exc
        raw_proxy = self.proxy if bool(self.driver_config.get("use_proxy", True)) else ""
        # Bridge the proxy for browser consumption.  Camoufox (Firefox) cannot
        # consume authenticated HTTP proxies directly; proxy_for_browser creates
        # a local SOCKS5 endpoint that the browser can use.  This matches the
        # pattern in paypal_auto._try_browser_pay_camoufox.
        browser_proxy = ""
        closer = None
        using_bridge = False
        if raw_proxy:
            from ..proxy_bridge import proxy_for_browser, needs_bridge
            using_bridge = needs_bridge(raw_proxy)
            browser_proxy, closer = proxy_for_browser(raw_proxy)
        self._proxy_bridge_closer = closer
        # Camoufox's GeoIP detection cannot work through a local SOCKS5 bridge
        # (it fails with "InvalidIP: Failed to get IP address").  When bridging,
        # disable GeoIP and fall back to the configured locale/timezone.
        configured_geoip = bool(self.driver_config.get("geoip", True))
        geoip = configured_geoip and not using_bridge
        max_width = int(self.driver_config.get("max_width") or 1280)
        max_height = int(self.driver_config.get("max_height") or 900)
        options: dict[str, Any] = {
            "headless": self.headless,
            "humanize": bool(self.driver_config.get("humanize", True)),
            "geoip": geoip,
            "screen": Screen(max_width=max_width, max_height=max_height),
        }
        if browser_proxy:
            from urllib.parse import urlsplit as _urlsplit
            pp = _urlsplit(browser_proxy)
            # Convert socks5h:// to socks5:// — Firefox's proxy parser does
            # not recognize the "h" suffix; remote DNS is handled by the
            # bridge itself, so socks5:// is correct here.
            scheme = "socks5" if pp.scheme == "socks5h" else pp.scheme
            proxy_dict: dict[str, Any] = {
                "server": f"{scheme}://{pp.hostname}:{pp.port}",
                "username": pp.username or "",
                "password": pp.password or "",
            }
            options["proxy"] = proxy_dict
        configured_locale = str(self.driver_config.get("locale") or "").strip()
        configured_timezone = str(self.driver_config.get("timezone") or "").strip()
        # With GeoIP enabled (direct proxy, no bridge), leaving these unset lets
        # Camoufox align language, timezone and WebRTC with the proxy exit.
        # When GeoIP is disabled (bridged proxy), fall back to configured or
        # global defaults so the browser has a consistent environment.
        locale = configured_locale or ("" if geoip else self.locale)
        timezone = configured_timezone or ("" if geoip else self.timezone_id)
        if locale:
            options["locale"] = locale
        # Note: timezone is not accepted by Camoufox's launch_persistent_context;
        # it is applied to the context after creation if geoip is disabled.
        user_data_dir = str(self.driver_config.get("user_data_dir") or "").strip()
        try:
            # Use persistent_context with a temp profile when no explicit
            # user_data_dir is configured.  This matches paypal_auto's pattern
            # and ensures proper cleanup via the Camoufox context manager.
            import tempfile
            tmp_profile = user_data_dir or tempfile.mkdtemp(prefix="camoufox_reg_")
            options["persistent_context"] = True
            options["user_data_dir"] = tmp_profile
            self._camoufox_ctx = Camoufox(**options)
            self.context = self._camoufox_ctx.__enter__()
            self._persistent = True
            self.browser = getattr(self.context, "browser", None) or self.context
            pages = list(getattr(self.context, "pages", []) or [])
            self.page = pages[0] if pages else self.context.new_page()
            self.context.set_default_timeout(self.timeout_ms)
            # Apply timezone to the context when geoip is disabled (bridged
            # proxy).  Camoufox's persistent_context doesn't accept timezone
            # as a launch parameter, so we set it via context timezone_id.
            if timezone and not geoip:
                try:
                    self.context.timezone_id = timezone
                except Exception:
                    pass
            self.stealth_status = apply_playwright_stealth(
                self.context,
                self.page,
                label="camoufox",
                provider_prefix="camoufox",
            )
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("camoufox_launch_failed", f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        if keep_open:
            self.page = None
            self.context = None
            self.browser = None
            self._playwright = None
            return
        # Properly exit the Camoufox context manager so the browser process
        # and temp profile are cleaned up.  Without this, residual processes
        # prevent subsequent launches within the same batch.
        if self._camoufox_ctx is not None:
            try:
                self._camoufox_ctx.__exit__(None, None, None)
            except Exception:
                pass
            self._camoufox_ctx = None
        # Close the proxy bridge if one was started.
        if self._proxy_bridge_closer is not None:
            try:
                self._proxy_bridge_closer()
            except Exception:
                pass
            self._proxy_bridge_closer = None
        super().close()


class BrowserUseSession(ConnectedPlaywrightSession):
    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "browser_use")

    def connect_url(self) -> str:
        api_key = _require(self.driver_config.get("api_key"), "browser_use_api_key_missing")
        base = str(self.driver_config.get("cdp_base") or "wss://connect.browser-use.com").rstrip("?&")
        query = {"apiKey": api_key}
        country = str(self.driver_config.get("proxy_country_code") or "").strip().lower()
        if bool(self.driver_config.get("use_proxy", True)) and country:
            query["proxyCountryCode"] = country
        profile_id = str(self.driver_config.get("profile_id") or "").strip()
        if profile_id:
            query["profileId"] = profile_id
        timeout = max(1, min(240, int(self.driver_config.get("session_timeout_minutes") or 120)))
        query["timeout"] = str(timeout)
        extra_query = self.driver_config.get("extra_query")
        if isinstance(extra_query, Mapping):
            # Keep provider-owned connection settings authoritative.  An
            # arbitrary extra query must not silently disable or replace the
            # configured residential proxy, profile, timeout, or API key.
            reserved = {
                "apikey", "api_key", "proxycountrycode", "proxy_country_code",
                "profileid", "profile_id", "timeout",
            }
            for key, value in extra_query.items():
                normalized_key = str(key).strip()
                if (
                    normalized_key
                    and normalized_key.lower() not in reserved
                    and value is not None
                ):
                    query[normalized_key] = str(value)
            # Never permit an arbitrary override to remove the credential used
            # to establish the cloud CDP connection.
            query["apiKey"] = api_key
        return f"{base}?{urlencode(query)}"

    def __enter__(self):
        self._start_playwright()
        try:
            browser = self._playwright.chromium.connect_over_cdp(self.connect_url(), timeout=self.timeout_ms)
            self._adopt_browser(browser)
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("browser_use_connect_failed", type(exc).__name__) from exc

    def close(self) -> None:
        self._close_connection(keep_browser_open=bool(self.driver_config.get("keep_browser_open", False)))


class SkyvernBrowserSession(ConnectedPlaywrightSession):
    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "skyvern")
        self.api_key = ""
        self.api_base = ""
        self.session_id = ""

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_method = str(method or "GET").upper()
        request_kwargs: dict[str, Any] = {
            "headers": self._headers(),
            "timeout": min(60, max(10, self.timeout_ms // 1000)),
        }
        if normalized_method == "GET":
            if body:
                request_kwargs["params"] = body
        else:
            request_kwargs["json"] = body
        response = curl_requests.request(
            normalized_method,
            urljoin(self.api_base.rstrip("/") + "/", path.lstrip("/")),
            **request_kwargs,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(response.text or "")[:500]}
        if int(response.status_code or 0) >= 400:
            raise BrowserRegistrationError("skyvern_api_error", f"http_{response.status_code}")
        return data if isinstance(data, dict) else {}

    def __enter__(self):
        self.api_key = _require(self.driver_config.get("api_key"), "skyvern_api_key_missing")
        self.api_base = str(self.driver_config.get("api_base") or "https://api.skyvern.com").rstrip("/")
        payload: dict[str, Any] = {
            "timeout": max(1, int(self.driver_config.get("session_timeout_minutes") or 60)),
            "generate_browser_profile": bool(self.driver_config.get("generate_browser_profile", False)),
            "ad_blocker": bool(self.driver_config.get("ad_blocker", True)),
            "browser_type": _normalize_skyvern_browser_type(self.driver_config.get("browser_type")),
        }
        for source, target in (("profile_id", "browser_profile_id"), ("proxy_location", "proxy_location")):
            value = str(self.driver_config.get(source) or "").strip()
            if value:
                payload[target] = (
                    _normalize_skyvern_proxy_location(value)
                    if source == "proxy_location" else value
                )
        data = self._request("POST", "/v1/browser_sessions", body=payload)
        self.session_id = _first(data, ("browser_session_id",), ("session_id",), ("id",))
        address = _first(data, ("browser_address",), ("cdp_url",), ("connect_url",), ("ws_endpoint",))
        for _ in range(10):
            if address or not self.session_id:
                break
            time.sleep(1)
            current = self._request("GET", f"/v1/browser_sessions/{self.session_id}")
            address = _first(current, ("browser_address",), ("cdp_url",), ("connect_url",), ("ws_endpoint",))
        if not address:
            self.close()
            raise BrowserRegistrationError("skyvern_browser_address_missing")
        self._start_playwright()
        try:
            browser = self._playwright.chromium.connect_over_cdp(address, headers=self._headers(), timeout=self.timeout_ms)
            self._adopt_browser(browser)
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("skyvern_connect_failed", type(exc).__name__) from exc

    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        self._close_connection(keep_browser_open=keep_open)
        if self.session_id and self.api_key and self.api_base and not keep_open:
            try:
                self._request("POST", f"/v1/browser_sessions/{self.session_id}/close", body={})
            except Exception:
                pass
        self.session_id = ""


class RoxyBrowserSession(ConnectedPlaywrightSession):
    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "roxy")
        self.api_base = ""
        self.profile_id = ""
        self.created_profile = False
        self.debugger_address = ""
        self.webdriver_url = ""
        self.driver_path = ""
        self.selenium = None

    def _headers(self) -> dict[str, str]:
        token = str(self.driver_config.get("api_token") or "").strip()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers.update({"token": token, "Authorization": f"Bearer {token}"})
        return headers

    def _path(self, key: str, default: str) -> str:
        raw = str(self.driver_config.get(key) or default)
        try:
            return raw.format(profile_id=self.profile_id, dir_id=self.profile_id)
        except (KeyError, IndexError, ValueError):
            return raw

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_method = str(method or "GET").upper()
        create_request = str(path or "").rstrip("/").endswith("/create")
        attempts = 1 if create_request else max(1, int(self.driver_config.get("api_retries") or 3))
        raw_delay = self.driver_config.get("api_retry_delay_seconds")
        retry_delay = max(0.0, float(raw_delay if raw_delay is not None else 1))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                request_kwargs: dict[str, Any] = {"headers": self._headers(), "timeout": min(60, max(10, self.timeout_ms // 1000))}
                if normalized_method == "GET":
                    if body:
                        request_kwargs["params"] = body
                else:
                    request_kwargs["json"] = body
                response = curl_requests.request(normalized_method, urljoin(self.api_base.rstrip("/") + "/", path.lstrip("/")), **request_kwargs)
                try:
                    data = response.json()
                except Exception:
                    data = {"raw": str(response.text or "")[:500]}
                if int(response.status_code or 0) >= 400:
                    raise BrowserRegistrationError("roxy_api_error", f"http_{response.status_code}")
                if isinstance(data, Mapping):
                    code = data.get("code")
                    normalized_code = str(code).strip().lower() if code is not None else ""
                    if normalized_code and normalized_code not in {"0", "200", "ok", "success"} and data.get("ok") is not True and data.get("success") is not True:
                        raise BrowserRegistrationError("roxy_api_error", "response_code_invalid")
                    if data.get("ok") is False and data.get("success") is not True:
                        raise BrowserRegistrationError("roxy_api_error", "response_not_ok")
                    if data.get("success") is False and data.get("ok") is not True:
                        raise BrowserRegistrationError("roxy_api_error", "response_not_success")
                    return dict(data)
                return {}
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts or not _roxy_retryable(exc):
                    raise
                time.sleep(retry_delay * (attempt + 1))
        raise last_error or BrowserRegistrationError("roxy_api_error", "request_failed")


    def _proxy_info(self) -> dict[str, Any] | None:
        value = normalize_proxy_url(self.proxy)
        if not value:
            return None
        parsed = urlsplit(value)
        if not parsed.hostname or not parsed.port:
            return None
        protocol = "SOCKS5" if parsed.scheme.startswith("socks5") else parsed.scheme.upper()
        result: dict[str, Any] = {
            "moduleId": 0, "proxyMethod": "custom", "proxyCategory": protocol,
            "ipType": "IPV4", "protocol": protocol, "host": parsed.hostname, "port": str(parsed.port),
        }
        if parsed.username:
            result["proxyUserName"] = unquote(parsed.username)
        if parsed.password:
            result["proxyPassword"] = unquote(parsed.password)
        return result

    def __enter__(self):
        self.api_base = str(self.driver_config.get("api_base") or "http://127.0.0.1:50100").rstrip("/")
        workspace_id = _require(self.driver_config.get("workspace_id"), "roxy_workspace_id_missing")
        self.profile_id = str(self.driver_config.get("profile_id") or "").strip()
        if not self.profile_id:
            payload: dict[str, Any] = {
                "workspaceId": int(workspace_id) if workspace_id.isdigit() else workspace_id,
                "name": f"gpt-register-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}",
                "os": random.choice(["Windows", "macOS"]),
            }
            project_id = str(self.driver_config.get("project_id") or "").strip()
            if project_id:
                payload["projectId"] = int(project_id) if project_id.isdigit() else project_id
            proxy_info = self._proxy_info()
            if proxy_info:
                payload["proxyInfo"] = proxy_info
            create_method = str(self.driver_config.get("create_method") or "POST").upper()
            create_path = self._path("create_path", "/browser/create")
            created = self._request(create_method, create_path, payload)
            self.profile_id = _first(
                created,
                ("id",), ("dirId",), ("dir_id",), ("profile_id",), ("profileId",), ("browser_id",),
                ("data", "id"), ("data", "dirId"), ("data", "dir_id"),
                ("data", "profile_id"), ("data", "profileId"), ("data", "browser_id"),
            )
            if not self.profile_id:
                raise BrowserRegistrationError("roxy_profile_create_failed")
            self.created_profile = True
        workspace_value = int(workspace_id) if workspace_id.isdigit() else workspace_id
        profile_value = int(self.profile_id) if self.profile_id.isdigit() else self.profile_id
        open_method = str(self.driver_config.get("open_method") or "POST").upper()
        open_path = self._path("open_path", "/browser/open")
        opened = self._request(open_method, open_path, {
            "workspaceId": workspace_value,
            "dirId": profile_value,
            "args": [], "forceOpen": True, "headless": self.headless,
        })
        ws_address = _first(
            opened, ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
            ("data", "ws"), ("data", "wsEndpoint"), ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
            ("result", "ws"), ("result", "wsEndpoint"), ("result", "ws_endpoint"),
        )
        self.debugger_address = _first(
            opened,
            ("http",), ("debuggerAddress",), ("debugger_address",), ("debugAddress",),
            ("debugHttp",), ("debug_http",), ("debuggingPortUrl",), ("debugging_port_url",),
            ("remoteDebuggingAddress",), ("remote_debugging_address",),
            ("data", "http"), ("data", "debuggerAddress"), ("data", "debugger_address"),
            ("data", "debugAddress"), ("data", "debugHttp"), ("data", "debug_http"),
            ("data", "debuggingPortUrl"), ("data", "debugging_port_url"),
            ("data", "remoteDebuggingAddress"), ("data", "remote_debugging_address"),
            ("result", "http"), ("result", "debugAddress"), ("result", "debugHttp"),
        )
        self.webdriver_url = _first(
            opened, ("webdriver",), ("webDriver",), ("webdriverUrl",), ("webdriver_url",),
            ("selenium",), ("selenium_url",), ("seleniumUrl",),
            ("data", "webdriver"), ("data", "webDriver"),
            ("data", "webdriverUrl"), ("data", "webdriver_url"), ("data", "selenium"),
            ("data", "selenium_url"), ("data", "seleniumUrl"),
            ("result", "webdriver"), ("result", "webdriverUrl"), ("result", "selenium"),
        )
        self.driver_path = _first(
            opened, ("driver",), ("driverPath",), ("driver_path",),
            ("data", "driver"), ("data", "driverPath"), ("data", "driver_path"),
            ("result", "driver"), ("result", "driverPath"),
        )
        address = ws_address or self.debugger_address
        backend = str(self.driver_config.get("backend") or self.driver_config.get("mode") or "").strip().lower()
        if not address and self.webdriver_url and backend in {"selenium", "webdriver"}:
            # The Selenium backend can attach through Roxy's WebDriver endpoint
            # without a CDP address.  Keep the profile lifecycle owned here.
            return self
        if not address:
            self.close()
            raise BrowserRegistrationError("roxy_debug_address_missing")
        address = _normalize_debugger_address(address)
        self._start_playwright()
        try:
            browser = self._playwright.chromium.connect_over_cdp(address, timeout=self.timeout_ms)
            self._adopt_browser(browser)
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("roxy_connect_failed", type(exc).__name__) from exc

    def selenium_driver(self) -> Any:
        """Attach Selenium to the Roxy profile using its own Chromedriver.

        This is intentionally lazy so installations that only use Playwright do
        not need Selenium installed. The returned driver is owned by the caller.
        """
        address = str(self.debugger_address or "").strip()
        webdriver_url = str(self.webdriver_url or "").strip()
        if not address and not webdriver_url:
            raise BrowserRegistrationError("roxy_selenium_debug_address_missing")
        if address.startswith(("http://", "https://")):
            address = urlsplit(address).netloc
        try:
            from .roxy_selenium import _build_driver
            self.selenium = _build_driver({
                "data": {
                    "http": address,
                    "webdriver": webdriver_url,
                    "driver": self.driver_path,
                }
            })
            return self.selenium
        except ImportError as exc:
            raise BrowserRegistrationError("browser_dependency_missing", "selenium") from exc
        except Exception as exc:
            raise BrowserRegistrationError("roxy_selenium_connect_failed", type(exc).__name__) from exc

    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        if self.selenium is not None and not keep_open:
            try:
                self.selenium.quit()
            except Exception:
                pass
            self.selenium = None
        self._close_connection(keep_browser_open=keep_open)
        if not self.profile_id or not self.api_base or keep_open:
            return
        workspace_id = str(self.driver_config.get("workspace_id") or "").strip()
        common = {
            "workspaceId": int(workspace_id) if workspace_id.isdigit() else workspace_id,
            "dirId": int(self.profile_id) if self.profile_id.isdigit() else self.profile_id,
        }
        try:
            close_method = str(self.driver_config.get("close_method") or "POST").upper()
            close_path = self._path("close_path", "/browser/close")
            self._request(close_method, close_path, common)
        except Exception:
            pass
        if self.created_profile and bool(self.driver_config.get("delete_profile_after_run", True)):
            try:
                delete_method = str(self.driver_config.get("delete_method") or "POST").upper()
                delete_path = self._path("delete_path", "/browser/delete")
                self._request(delete_method, delete_path, {
                    "workspaceId": common["workspaceId"], "dirIds": [common["dirId"]],
                })
            except Exception:
                pass
        self.profile_id = ""


def verify_browser_proxy_country(browser: Any, *, expected_country: str = "", timeout_seconds: int = 20) -> dict[str, Any]:
    """Probe the browser's own egress and return country-only audit data."""
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


class RoxySeleniumSession:
    """Roxy profile controlled by Roxy's own Chromedriver."""

    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        self.proxy = kwargs.get("proxy")
        self.headless = bool(kwargs.get("headless", True))
        self.timeout_ms = max(5_000, int(kwargs.get("timeout_ms", 45_000) or 45_000))
        self.locale = str(kwargs.get("locale") or "en-US")
        self.timezone_id = str(kwargs.get("timezone_id") or "America/New_York")
        self.config = config
        self.driver_config = _driver_config(config, "roxy")
        self._roxy: RoxyBrowserSession | None = None
        self.driver = None
        self.page = None

    def __enter__(self):
        self._roxy = RoxyBrowserSession(config=self.config, proxy=self.proxy, headless=self.headless, timeout_ms=self.timeout_ms, locale=self.locale, timezone_id=self.timezone_id)
        self._roxy.__enter__()
        try:
            self.driver = self._roxy.selenium_driver()
            self.page = _SeleniumPage(self.driver, self.timeout_ms)
            return self
        except Exception:
            self.close()
            raise

    def verify_proxy_country(self, expected_country: str = "", timeout_seconds: int = 20) -> dict[str, Any]:
        return verify_browser_proxy_country(self, expected_country=expected_country, timeout_seconds=timeout_seconds)

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def add_device_cookie(self, device_id: str, chat_base: str, auth_base: str) -> None:
        if not self.driver or not str(device_id or "").strip():
            return
        for base in (chat_base, auth_base):
            parsed = urlsplit(str(base or ""))
            if not parsed.netloc:
                continue
            try:
                self.driver.get(f"{parsed.scheme}://{parsed.netloc}/")
                self.driver.add_cookie({"name": "oai-did", "value": str(device_id), "path": "/"})
            except Exception:
                pass

    def cookie_header(self) -> str:
        try:
            return "; ".join(f"{c.get('name')}={c.get('value')}" for c in self.driver.get_cookies() if c.get("name") and c.get("value"))
        except Exception:
            return ""

    def session_cookie_state(self) -> dict[str, Any]:
        """Return cookie presence metadata without exposing cookie values."""
        names = set()
        try:
            names = {str(item.get("name") or "") for item in self.driver.get_cookies() if isinstance(item, Mapping)}
        except Exception:
            pass
        return {
            "session_cookie_present": any("session-token" in name or name.startswith("oai") for name in names),
            "cookie_count": len(names),
        }

    def context_state(self) -> dict[str, Any]:
        hosts: list[str] = []
        try:
            for handle in list(self.driver.window_handles or []):
                try:
                    self.driver.switch_to.window(handle)
                    host = str(urlsplit(str(self.driver.current_url or "")).hostname or "").lower()
                    if host and host not in hosts:
                        hosts.append(host)
                except Exception:
                    continue
        except Exception:
            pass
        return {"current_host": str(urlsplit(str(getattr(self.driver, "current_url", "") or "")).hostname or "").lower(), "window_hosts": hosts, **self.session_cookie_state()}

    @staticmethod
    def _is_chatgpt_url(url: str) -> bool:
        host = str(urlsplit(str(url or "")).hostname or "").lower()
        return host == "chatgpt.com" or host.endswith(".chatgpt.com")

    def _switch_to_chatgpt_window_if_any(self) -> bool:
        """Select a callback window which has already reached ChatGPT.

        Roxy may complete the OpenAI callback in a different WebDriver window.
        Preserve the current window when neither window is on ChatGPT.
        """
        if self.driver is None:
            return False
        try:
            current_handle = self.driver.current_window_handle
        except Exception:
            current_handle = None
        try:
            handles = list(self.driver.window_handles or [])
        except Exception:
            handles = []
        for handle in handles:
            try:
                self.driver.switch_to.window(handle)
                if self._is_chatgpt_url(str(self.driver.current_url or "")):
                    return True
            except Exception:
                continue
        if current_handle is not None:
            try:
                self.driver.switch_to.window(current_handle)
            except Exception:
                pass
        return False

    def _ensure_chatgpt_context(self, *, auto_jump_wait: int = 15) -> bool:
        """Wait for OAuth's natural return before explicitly visiting ChatGPT."""
        if self.driver is None:
            return False
        deadline = time.monotonic() + max(0, int(auto_jump_wait or 0))
        while True:
            try:
                if self._is_chatgpt_url(str(self.driver.current_url or "")):
                    return True
            except Exception:
                pass
            if self._switch_to_chatgpt_window_if_any():
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        try:
            self.driver.get("https://chatgpt.com/")
            return self._is_chatgpt_url(str(self.driver.current_url or ""))
        except Exception:
            return False

    def fetch_json(self, url: str, *, timeout_ms: int = 20_000) -> dict[str, Any]:
        is_chatgpt_session = self._is_chatgpt_url(url) and urlsplit(str(url or "")).path.rstrip("/") == "/api/auth/session"
        if is_chatgpt_session and not self._ensure_chatgpt_context():
            return {"status": 0, "body": {"error": "chatgpt_context_unavailable"}}
        # /api/auth/session must be fetched from a ChatGPT origin.  Fetching the
        # absolute URL while the driver remains on auth.openai.com can return a
        # harmless HTTP 200 without the newly written ChatGPT session.
        target = "/api/auth/session" if is_chatgpt_session else str(url)
        from .roxy_selenium import _fetch_json_with_window_recovery

        return _fetch_json_with_window_recovery(
            self.driver,
            target,
            timeout_ms=timeout_ms,
            proxy=self.proxy,
        )

    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        if self.driver is not None and not keep_open:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None
        if self._roxy is not None:
            try:
                self._roxy.selenium = None
                self._roxy.close()
            except Exception:
                pass
            self._roxy = None
        self.page = None


def _browser_profile_dir(driver: str, profile_id: str) -> str:
    """Derive a stable on-disk profile directory for a browser-registered account."""
    import pathlib

    safe_id = "".join(c if c.isalnum() or c in "-._" else "_" for c in str(profile_id or ""))
    if not safe_id:
        safe_id = "default"
    return str(pathlib.Path("runtime") / "browser_profiles" / driver / safe_id)


def _inject_browser_profile(
    config: Mapping[str, Any],
    driver: str,
    browser_identity: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Inject stable profile persistence into the driver config.

    When ``browser_identity`` carries a ``profile_id``, the config is patched
    so that local browser drivers (Camoufox, Cloak, Playwright) use a stable
    on-disk ``user_data_dir`` and Roxy retains its created profile instead of
    deleting it.  This ensures the same browser profile can be reopened for
    follow-up promotion, liveness, and recovery calls.
    """
    if not browser_identity or not browser_identity.get("profile_id"):
        return config
    profile_id = str(browser_identity["profile_id"])
    # Build a mutable copy of the config with the driver-specific overrides.
    mutable = dict(config)
    registration = dict(mutable.get("registration") or {})
    drivers = dict(registration.get("drivers") or {})
    driver_cfg = dict(drivers.get(driver) or {})
    if driver in {"camoufox", "cloak", "playwright"}:
        if not str(driver_cfg.get("user_data_dir") or "").strip():
            driver_cfg["user_data_dir"] = _browser_profile_dir(driver, profile_id)
    elif driver == "roxy":
        # Keep created profiles so they can be reopened later.  When an
        # explicit profile_id is already set in the env/config, it takes
        # precedence via _driver_config's env override layer.
        if "delete_profile_after_run" not in driver_cfg:
            driver_cfg["delete_profile_after_run"] = False
    drivers[driver] = driver_cfg
    registration["drivers"] = drivers
    mutable["registration"] = registration
    return mutable


def create_browser_session(
    driver: str,
    *,
    config: Mapping[str, Any],
    proxy: str | None,
    headless: bool,
    timeout_ms: int,
    locale: str,
    timezone_id: str,
    browser_identity: Mapping[str, Any] | None = None,
    viewport: tuple[int, int] | None = None,
) -> PlaywrightBrowserSession:
    try:
        driver = normalize_registration_driver(driver)
    except ValueError as exc:
        raise BrowserRegistrationError("unsupported_registration_driver") from exc
    if driver == "protocol":
        raise BrowserRegistrationError("unsupported_registration_driver", "protocol")
    config = _inject_browser_profile(config, driver, browser_identity)
    kwargs = {
        "proxy": proxy, "headless": headless, "timeout_ms": timeout_ms,
        "locale": locale, "timezone_id": timezone_id,
    }
    if driver == "playwright":
        # Only the local Playwright driver consumes the rotated screen profile;
        # external/anti-detect browsers manage their own viewport.
        kwargs["viewport"] = viewport
    if driver == "roxy":
        roxy_cfg = _driver_config(config, "roxy")
        if str(roxy_cfg.get("backend") or roxy_cfg.get("mode") or "").strip().lower() in {"selenium", "webdriver"}:
            return RoxySeleniumSession(config=config, **kwargs)
        return RoxyBrowserSession(config=config, **kwargs)
    if driver == "cloak":
        return CloakBrowserSession(config=config, **kwargs)
    if driver == "camoufox":
        return CamoufoxBrowserSession(config=config, **kwargs)
    if driver == "browser_use":
        return BrowserUseSession(config=config, **kwargs)
    if driver == "skyvern":
        return SkyvernBrowserSession(config=config, **kwargs)
    # Playwright: pass user_data_dir for persistent context when available.
    pw_user_data_dir = str(_driver_config(config, "playwright").get("user_data_dir") or "").strip()
    if pw_user_data_dir:
        kwargs["user_data_dir"] = pw_user_data_dir
    return PlaywrightBrowserSession(**kwargs)


__all__ = [
    "BrowserUseSession", "CamoufoxBrowserSession", "CloakBrowserSession",
    "RoxyBrowserSession", "RoxySeleniumSession", "SkyvernBrowserSession",
    "create_browser_session",
]
