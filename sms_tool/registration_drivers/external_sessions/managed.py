"""Managed browser sessions for optional local and cloud registration drivers."""

from __future__ import annotations

import contextlib
import logging
import random
import shutil
import time
import warnings
from typing import Any, Mapping
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from curl_cffi import requests as curl_requests

from ...driver_env import driver_config as _driver_config
from ...phone_proxy import normalize_proxy_url
from ..base import BrowserRegistrationError
from ..browser_session import PlaywrightBrowserSession
from ..stealth import apply_playwright_stealth
from ..platform_patches import MOZ_DISABLE_CONTENT_SANDBOX, camoufox_launch_env

logger = logging.getLogger(__name__)


# `_driver_config` now lives in sms_tool/driver_env.py. `sms_tool.config` needed
# this same env-overlay logic for preflight and used to import it from here,
# which made config - imported by 64 modules - depend on the browser-driver
# package: a cycle. It now sits below both consumers. After the package split,
# the session classes in this module call this module-level binding, so patch
# `external_sessions.managed._driver_config`; the re-export on the
# `external_sessions` package is only a convenience for direct callers and
# patching it is a no-op.


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
            # keep_browser_open is an explicit operator/debug switch: the
            # locally launched browser and its playwright driver subprocess
            # stay alive and must be reaped by the operator. Say so instead of
            # leaking silently.
            logger.warning(
                "cloak keep_browser_open=True: browser left running for "
                "operator inspection; close it manually to free the process "
                "and driver handle"
            )
            self.page = None
            self.context = None
            self.browser = None
            self._playwright = None
            return
        super().close()


# Firefox (Camoufox) cannot spawn renderer/content processes on some Windows
# hosts while the content sandbox is enabled.  The failure mode is subtle: the
# browser process starts fine and Juggler reports "listening to the pipe", but
# every ``new_context().new_page()`` call then hangs forever (Playwright's
# ``launch_persistent_context`` times out for the same reason, because it also
# creates a page).  A GPU/compositor annotation ("RenderCompositorSWGL failed
# mapping default framebuffer, no dt") is the only hint in the browser log.
#
# The existing opt-out is passed only in the child's launch environment by
# platform_patches.camoufox_launch_env; the parent environment is untouched.


class CamoufoxBrowserSession(ConnectedPlaywrightSession):
    """Browser session backed by the Camoufox anti-detect engine.

    Camoufox provides a hardened Firefox with built-in fingerprint injection,
    GeoIP-aware locale/timezone, and humanized input.  This wrapper follows
    the same contract as ``CloakBrowserSession``: a thin ``__enter__`` that
    launches the browser, sets ``self.browser``/``self.context``/``self.page``,
    and applies stealth overlays so the shared registration flow works
    unchanged.
    """

    # Camoufox cannot do lightweight per-account context reuse the way the stock
    # Playwright driver can.  Its sync API owns one event loop per ``Camoufox``
    # object (see camoufox.sync_api.Camoufox.__exit__'s #82 note): tearing the
    # loop down requires ``Camoufox.__exit__`` -> ``PlaywrightContextManager.
    # __exit__``.  The ``release_account_context`` / ``renew_account_context``
    # methods inherited from ``PlaywrightBrowserSession`` only close
    # ``self.context`` and leave that loop running, which leaks a "running"
    # asyncio loop into the caller thread.  The next retry's
    # ``Camoufox(**options).__enter__()`` then dies with
    # "It looks like you are using Playwright Sync API inside the asyncio loop"
    # because the caller thread still has a live loop.
    #
    # Declaring these as unsupported (None) makes the browser process pool fall
    # back to a full ``close()`` + relaunch on every account, which is the only
    # safe reuse model for Camoufox and stops the loop from leaking between
    # retry attempts.
    release_account_context = None  # type: ignore[assignment]
    renew_account_context = None  # type: ignore[assignment]

    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "camoufox")
        self._persistent = False
        self._camoufox_ctx = None
        self._proxy_bridge_closer = None
        self._owned_profile: str | None = None
        self._launch_succeeded = False

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
            from ...proxy_bridge import proxy_for_browser, needs_bridge
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
            # user_data_dir is configured. We own this directory, not Camoufox:
            # its context manager closes the process but preserves profiles.
            import tempfile
            if not user_data_dir:
                self._owned_profile = tempfile.mkdtemp(prefix="camoufox_reg_")
            tmp_profile = user_data_dir or self._owned_profile
            options["persistent_context"] = True
            options["user_data_dir"] = tmp_profile
            # See the MOZ_DISABLE_CONTENT_SANDBOX note above: without it every
            # new_page() call hangs forever on affected hosts.
            options["env"] = camoufox_launch_env(self.driver_config)
            # GeoIP is deliberately disabled for bridged proxies: the bridge
            # is a local SOCKS5 endpoint, so geolocating the proxy IP is
            # meaningless (locale/timezone fall back to the configured values
            # instead). Camoufox still fires its "pass geoip=True" LeakWarning
            # for proxy+geoip=False, a known false positive for this design —
            # silence it here rather than in the global warnings filters.
            launch_warnings = (
                warnings.catch_warnings() if browser_proxy and not geoip else contextlib.nullcontext()
            )
            with launch_warnings:
                if browser_proxy and not geoip:
                    warnings.filterwarnings("ignore", message=".*geoip=True.*")
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
            self._launch_succeeded = True
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("camoufox_launch_failed", f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        keep_open = self._launch_succeeded and bool(self.driver_config.get("keep_browser_open", False))
        if keep_open:
            # Exiting the camoufox context manager would kill the browser the
            # operator explicitly asked to keep, so the process and its temp
            # profile directory stay alive by design -- but visibly.
            logger.warning(
                "camoufox keep_browser_open=True: browser and temp profile "
                "left running for operator inspection; close it manually to "
                "free the process, driver handle and profile directory"
            )
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
        self._launch_succeeded = False
        if self._owned_profile is not None:
            try:
                shutil.rmtree(self._owned_profile)
            except FileNotFoundError:
                self._owned_profile = None
            except OSError as exc:
                # Keep ownership so a later close can retry a Windows file lock.
                logger.warning("camoufox temporary profile cleanup failed: %s", type(exc).__name__)
            else:
                self._owned_profile = None


class RoxyBrowserSession(ConnectedPlaywrightSession):
    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "roxy")
        self.api_base = ""
        self.profile_id = ""
        self.created_profile = False
        self.debugger_address = ""

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
        # Default matches config.json's roxy.api_base (50000); 50100 was wrong
        # and would silently point at a dead port whenever api_base is absent.
        self.api_base = str(self.driver_config.get("api_base") or "http://127.0.0.1:50000").rstrip("/")
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
        try:
            opened = self._request(open_method, open_path, {
                "workspaceId": workspace_value,
                "dirId": profile_value,
                "args": [], "forceOpen": True, "headless": self.headless,
            })
        except Exception:
            # __enter__ 内 open 失败：清理已建 profile，避免孤儿占满 Roxy 3/3 额度
            try:
                self.close()
            except Exception:
                pass
            raise
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
        address = ws_address or self.debugger_address
        if not address:
            raise BrowserRegistrationError("roxy_debug_address_missing")
        address = _normalize_debugger_address(address)
        try:
            self._start_playwright()
            browser = self._playwright.chromium.connect_over_cdp(address, timeout=self.timeout_ms)
            self._adopt_browser(browser)
            return self
        except BrowserRegistrationError:
            # __enter__ 内失败：清理已建 profile（close() 按 created_profile +
            # delete_profile_after_run 删除），否则孤儿占满 Roxy 3/3 额度 → 死亡螺旋
            try:
                self.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                self.close()
            except Exception:
                pass
            raise BrowserRegistrationError("roxy_connect_failed", type(exc).__name__) from exc


    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        self._close_connection(keep_browser_open=keep_open)
        if not self.profile_id or not self.api_base or keep_open:
            return
        workspace_id = str(self.driver_config.get("workspace_id") or "").strip()
        common = {
            "workspaceId": int(workspace_id) if workspace_id.isdigit() else workspace_id,
            "dirId": int(self.profile_id) if self.profile_id.isdigit() else self.profile_id,
        }
        close_method = str(self.driver_config.get("close_method") or "POST").upper()
        close_path = self._path("close_path", "/browser/close")
        delete_method = str(self.driver_config.get("delete_method") or "POST").upper()
        delete_path = self._path("delete_path", "/browser/delete")
        # 关窗（幂等，失败忽略）。Roxy 关窗是异步的，紧接着删会 code:101 请先关闭窗口。
        try:
            self._request(close_method, close_path, common)
        except Exception:
            pass
        if self.created_profile and bool(self.driver_config.get("delete_profile_after_run", True)):
            payload = {"workspaceId": common["workspaceId"], "dirIds": [common["dirId"]]}
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    self._request(delete_method, delete_path, payload)
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    if attempt < 2:
                        # 窗口可能尚未完全关闭 → 先再发一次 close 确保关闭，退避后重试删
                        try:
                            self._request(close_method, close_path, common)
                        except Exception:
                            pass
                        time.sleep(2 ** attempt)
            # 3 次仍失败：不抛（delete_profile_after_run 仅做清理），但必须
            # 告警——孤儿 profile 会静默累积直到占满 Roxy 额度。
            if last_err is not None:
                logger.warning(
                    "roxy profile delete failed after 3 attempts; orphan "
                    "profile %s left behind (delete_profile_after_run=true): %s",
                    self.profile_id, type(last_err).__name__,
                )
        self.profile_id = ""


class AdsPowerBrowserSession(ConnectedPlaywrightSession):
    """Drive an AdsPower-managed Chromium over its local REST API.

    AdsPower owns the browser process, the fingerprint and the proxy for each
    environment (``user_id``).  This session only starts/stops the environment
    and attaches through CDP -- the environment must already exist in AdsPower's
    UI, because fingerprint/proxy configuration lives there, not in config.
    """

    def __init__(self, *, config: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_config = _driver_config(config, "adspower")
        self.api_base = ""
        self.user_id = ""
        self.debugger_address = ""

    def _api_get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.api_base = str(self.driver_config.get("api_base") or "http://127.0.0.1:50325").rstrip("/")
        url = urljoin(self.api_base + "/", path.lstrip("/"))
        timeout = min(60, max(10, self.timeout_ms // 1000))
        try:
            response = curl_requests.get(url, params=params, timeout=timeout)
            data = response.json()
        except Exception as exc:
            raise BrowserRegistrationError("adspower_api_error", type(exc).__name__) from exc
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise BrowserRegistrationError("adspower_api_error", f"http_{response.status_code}")
        if not isinstance(data, Mapping):
            raise BrowserRegistrationError("adspower_api_error", "non_json_response")
        if int(data.get("code", 0) or 0) != 0:
            raise BrowserRegistrationError(
                "adspower_api_error",
                str(data.get("msg") or data.get("message") or f"code_{data.get('code')}"),
            )
        return dict(data)

    def __enter__(self):
        self.api_base = str(self.driver_config.get("api_base") or "http://127.0.0.1:50325").rstrip("/")
        self.user_id = str(self.driver_config.get("user_id") or self.driver_config.get("profile_id") or "").strip()
        if not self.user_id:
            raise BrowserRegistrationError("adspower_user_id_missing")
        started = self._api_get("api/v1/browser/start", {
            "user_id": self.user_id,
            "headless": 1 if self.headless else 0,
            "ip_tab": 0,
        })
        data = started.get("data") or {}
        ws_blob = data.get("ws") if isinstance(data.get("ws"), Mapping) else {}
        ws_address = str(ws_blob.get("playwright") or ws_blob.get("puppeteer") or "").strip()
        if not ws_address:
            ws_address = str(data.get("debuggerAddress") or "").strip()
        if not ws_address:
            raise BrowserRegistrationError("adspower_debug_address_missing")
        ws_address = _normalize_debugger_address(ws_address)
        self.debugger_address = ws_address
        self._start_playwright()
        try:
            browser = self._playwright.chromium.connect_over_cdp(ws_address, timeout=self.timeout_ms)
            self._adopt_browser(browser)
            return self
        except BrowserRegistrationError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BrowserRegistrationError("adspower_connect_failed", type(exc).__name__) from exc

    def close(self) -> None:
        keep_open = bool(self.driver_config.get("keep_browser_open", False))
        self._close_connection(keep_browser_open=keep_open)
        if not self.user_id or not self.api_base or keep_open:
            return
        try:
            self._api_get("api/v1/browser/stop", {"user_id": self.user_id})
        except Exception:
            pass
        self.user_id = ""