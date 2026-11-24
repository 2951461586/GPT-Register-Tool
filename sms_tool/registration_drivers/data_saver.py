"""Browser data saver: optional resource blocking and cross-profile static asset cache.

Designed to reduce residential proxy traffic for local browser registration
drivers (Playwright, CloakBrowser, Camoufox, RoxyBrowser) without interfering
with authentication, OTP forms, Sentinel proofs, or CAPTCHA challenges.

Three composable layers, all off by default (opt in via
``registration.browser_data_saver.enabled = true`` or ``BROWSER_DATA_SAVER=1``):

1. **Proxy-bridge host blocking** (``blocked_proxy_hosts``) — rejects browser
   background downloads such as ``dl.google.com`` and ``gvt1.com`` inside
   :class:`sms_tool.proxy.bridge.LocalProxyBridge` *before* connecting to the
   upstream proxy.
2. **Page-level resource/telemetry filtering** — aborts configured resource
   types (default ``image``, ``media``) and third-party analytics/RUM URLs while
   strictly allowing CAPTCHA/challenge/Sentinel/auth endpoints.
3. **Cross-profile static asset cache** — atomically caches immutable static
   GET assets (JS/CSS/fonts/images on allowlisted static hosts, never APIs or
   auth endpoints) on disk so subsequent fresh profiles serve them locally with
   zero upstream proxy bytes.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)


def _load_native_data_saver() -> Any | None:
    mode = str(os.environ.get("RUST_ANYTHING_MODE") or "").strip().lower()
    unit_mode = str(os.environ.get("RUST_ANYTHING_UNIT_DATA_SAVER") or "").strip().lower()
    if mode in {"old", "original"} or unit_mode in {"old", "original"}:
        return None
    pkg_dir = Path(__file__).resolve().parents[2] / "rust" / "data-saver-rs" / "python"
    if pkg_dir.is_dir():
        pkg_str = str(pkg_dir)
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)
    try:
        return importlib.import_module("data_saver_rs")
    except (ImportError, OSError):
        if mode in {"new", "rust"} or unit_mode in {"new", "rust"}:
            raise
        return None


_NATIVE_DATA_SAVER = _load_native_data_saver()
_NATIVE_IS_CACHEABLE_STATIC_ASSET = getattr(_NATIVE_DATA_SAVER, "is_cacheable_static_asset", None)
_NATIVE_SCALAR_TYPES = (str, type(None), bool, int, float)

_CACHE_STORE_LOCK = threading.Lock()

DEFAULT_BLOCKED_PROXY_HOSTS: tuple[str, ...] = (
    "dl.google.com",
    "gvt1.com",
)

DEFAULT_BLOCKED_RESOURCE_TYPES: tuple[str, ...] = (
    "image",
    "media",
)

DEFAULT_BLOCKED_URL_PATTERNS: tuple[str, ...] = (
    "*://auth.openai.com/awe/api/v2/rum*",
    "*://chatgpt.com/awe/api/v2/rum*",
    "*://chatgpt.com/ces/statsc/flush*",
    "*://connect.facebook.net/*",
    "*://analytics.tiktok.com/*",
    "*://snap.licdn.com/*",
    "*://bat.bing.com/*",
    "*://accounts.google.com/gsi/client*",
)

_RESOURCE_TYPE_ALIASES: dict[str, str] = {
    "images": "image",
    "img": "image",
    "videos": "media",
    "video": "media",
    "audio": "media",
    "fonts": "font",
    "css": "stylesheet",
    "styles": "stylesheet",
}

#: URLs containing any of these keywords are never aborted and never cached.
_PROTECTED_KEYWORDS: tuple[str, ...] = (
    "captcha",
    "challenge",
    "turnstile",
    "arkose",
    "funcaptcha",
    "sentinel",
    "cdn-cgi",
)

_CACHEABLE_RESOURCE_TYPES: frozenset[str] = frozenset({"script", "stylesheet", "font", "image"})
_STATIC_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".js",
        ".mjs",
        ".css",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".ico",
    }
)
_CACHEABLE_HOST_SUFFIXES: tuple[str, ...] = (
    "chatgpt.com",
    "auth.openai.com",
    "oaistatic.com",
    "cdn.openai.com",
)
_DENIED_CACHE_PATH_PARTS: tuple[str, ...] = (
    "/api/",
    "/backend-api/",
    "/backend-anon/",
    "/ces/",
    "/cdn-cgi/",
    "/sentinel/",
    "/authorize",
    "/oauth",
    "/login",
    "/logout",
    "/email-verification",
    "/about-you",
    "/bazaar/",
    "/obi/",
)
_REPLAY_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "cache-control",
        "etag",
        "last-modified",
        "vary",
        "access-control-allow-origin",
        "access-control-expose-headers",
        "cross-origin-resource-policy",
        "timing-allow-origin",
    }
)


def _bool_flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def _string_tuple(value: Any, default: tuple[str, ...], *, lower: bool = False) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        if stripped == "[]":
            return ()
        items = [part.strip() for part in stripped.replace(",", "\n").splitlines()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part or "").strip() for part in value]
    else:
        return default
    out: list[str] = []
    for item in items:
        normalized = item.lower() if lower else item
        if normalized and normalized not in out:
            out.append(normalized)
    return tuple(out)


def _normalize_resource_types(value: Any) -> tuple[str, ...]:
    raw = _string_tuple(value, DEFAULT_BLOCKED_RESOURCE_TYPES, lower=True)
    out: list[str] = []
    for item in raw:
        canonical = _RESOURCE_TYPE_ALIASES.get(item, item)
        if canonical and canonical not in out:
            out.append(canonical)
    return tuple(out)


def _default_cache_dir() -> Path:
    base = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if base:
        return Path(base) / "GPTRegisterTool" / "browser_asset_cache"
    return Path(tempfile.gettempdir()) / "gpt_register_browser_asset_cache"


@dataclass(frozen=True)
class BrowserDataSaverConfig:
    enabled: bool = False
    blocked_proxy_hosts: tuple[str, ...] = DEFAULT_BLOCKED_PROXY_HOSTS
    blocked_resource_types: tuple[str, ...] = DEFAULT_BLOCKED_RESOURCE_TYPES
    blocked_url_patterns: tuple[str, ...] = DEFAULT_BLOCKED_URL_PATTERNS
    asset_cache_enabled: bool = True
    asset_cache_dir: str = ""
    asset_cache_max_age_seconds: int = 86400
    asset_cache_max_item_bytes: int = 25 * 1024 * 1024
    proxy_connect_retries: int = 3

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "BrowserDataSaverConfig":
        reg = config.get("registration") if isinstance(config, Mapping) else None
        section = reg.get("browser_data_saver") if isinstance(reg, Mapping) else None
        if not isinstance(section, Mapping):
            section = {}
        env_raw = str(os.environ.get("BROWSER_DATA_SAVER") or "").strip()
        enabled = _bool_flag(env_raw, False) if env_raw else _bool_flag(section.get("enabled"), False)
        try:
            max_age = max(0, int(section.get("asset_cache_max_age_seconds", 86400) or 0))
        except (TypeError, ValueError):
            max_age = 86400
        try:
            max_bytes = max(1024, int(section.get("asset_cache_max_item_bytes", 25 * 1024 * 1024) or 0))
        except (TypeError, ValueError):
            max_bytes = 25 * 1024 * 1024
        try:
            retries = max(0, min(3, int(section.get("proxy_connect_retries", 3) or 0)))
        except (TypeError, ValueError):
            retries = 3
        return cls(
            enabled=enabled,
            blocked_proxy_hosts=_string_tuple(
                section.get("blocked_proxy_hosts"), DEFAULT_BLOCKED_PROXY_HOSTS, lower=True
            ),
            blocked_resource_types=_normalize_resource_types(section.get("blocked_resource_types")),
            blocked_url_patterns=_string_tuple(section.get("blocked_url_patterns"), DEFAULT_BLOCKED_URL_PATTERNS),
            asset_cache_enabled=_bool_flag(section.get("asset_cache_enabled"), True),
            asset_cache_dir=str(section.get("asset_cache_dir") or "").strip(),
            asset_cache_max_age_seconds=max_age,
            asset_cache_max_item_bytes=max_bytes,
            proxy_connect_retries=retries,
        )

    def resolved_cache_dir(self) -> Path:
        if self.asset_cache_dir:
            return Path(self.asset_cache_dir).expanduser().resolve()
        return _default_cache_dir()


def is_protected_url(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    return any(keyword in lowered for keyword in _PROTECTED_KEYWORDS)


def should_abort_request(url: str, resource_type: str, config: BrowserDataSaverConfig) -> tuple[bool, str]:
    """Return ``(abort, reason)`` for a browser request under ``config``."""
    if not config.enabled:
        return False, ""
    raw_url = str(url or "").strip()
    if not raw_url or not raw_url.lower().startswith(("http://", "https://")):
        return False, ""
    if is_protected_url(raw_url):
        return False, ""
    rtype = str(resource_type or "").strip().lower()
    if rtype and rtype in config.blocked_resource_types:
        return True, f"type:{rtype}"
    lowered = raw_url.lower()
    for pattern in config.blocked_url_patterns:
        if fnmatchcase(lowered, pattern.lower()):
            return True, f"pattern:{pattern}"
    return False, ""


def is_cacheable_static_asset(url: str, resource_type: str = "", *, method: str = "GET") -> bool:
    """True only for exact GET immutable static assets on allowlisted hosts."""
    if (
        _NATIVE_IS_CACHEABLE_STATIC_ASSET is not None
        and isinstance(url, _NATIVE_SCALAR_TYPES)
        and isinstance(resource_type, _NATIVE_SCALAR_TYPES)
        and isinstance(method, _NATIVE_SCALAR_TYPES)
    ):
        return bool(_NATIVE_IS_CACHEABLE_STATIC_ASSET(url, resource_type, method=method))
    if str(method or "GET").upper() != "GET":
        return False
    raw_url = str(url or "").strip()
    if not raw_url or is_protected_url(raw_url):
        return False
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _CACHEABLE_HOST_SUFFIXES):
        return False
    path = parsed.path.lower()
    if any(part in path for part in _DENIED_CACHE_PATH_PARTS):
        return False
    rtype = str(resource_type or "").strip().lower()
    if rtype and rtype not in _CACHEABLE_RESOURCE_TYPES:
        return False
    suffix = Path(path).suffix.lower()
    return suffix in _STATIC_EXTENSIONS or "/_next/static/" in path or "/assets/" in path


class StaticAssetCache:
    """Thread-safe, atomic disk cache for browser static assets."""

    def __init__(self, cache_dir: Path, *, max_age_seconds: int = 86400, max_item_bytes: int = 25 * 1024 * 1024):
        self.cache_dir = Path(cache_dir)
        self.max_age_seconds = max(0, int(max_age_seconds))
        self.max_item_bytes = max(1024, int(max_item_bytes))
        self.hits = 0
        self.misses = 0
        self.recorded = 0
        self.bytes_saved = 0

    def _entry_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def get(self, url: str) -> dict[str, Any] | None:
        path = self._entry_path(url)
        try:
            stat = path.stat()
            if self.max_age_seconds and time.time() - stat.st_mtime > self.max_age_seconds:
                self.misses += 1
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("url") != url or not payload.get("body_b64"):
                self.misses += 1
                return None
            body = base64.b64decode(str(payload["body_b64"]))
        except (OSError, ValueError, TypeError):
            self.misses += 1
            return None
        self.hits += 1
        self.bytes_saved += len(body)
        return {
            "status": int(payload.get("status") or 200),
            "headers": dict(payload.get("headers") or {}),
            "body": body,
        }

    def put(
        self,
        url: str,
        *,
        status: int,
        headers: Mapping[str, Any],
        body: bytes,
        resource_type: str = "",
    ) -> bool:
        if status != 200 or not body or len(body) > self.max_item_bytes:
            return False
        if not is_cacheable_static_asset(url, resource_type, method="GET"):
            return False
        replay_headers = {
            str(key).lower(): str(value)
            for key, value in (headers or {}).items()
            if str(key).lower() in _REPLAY_HEADERS and value is not None
        }
        payload = {
            "version": 1,
            "url": url,
            "status": int(status),
            "resource_type": str(resource_type or ""),
            "headers": replay_headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "body_bytes": len(body),
            "stored_at": int(time.time()),
        }
        path = self._entry_path(url)
        try:
            with _CACHE_STORE_LOCK:
                if path.is_file():
                    stat = path.stat()
                    if not self.max_age_seconds or time.time() - stat.st_mtime <= self.max_age_seconds:
                        return False
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, path)
                self.recorded += 1
                return True
        except (OSError, ValueError, TypeError):
            return False


@dataclass
class BrowserDataSaverHandle:
    config: BrowserDataSaverConfig
    cache: StaticAssetCache | None = None
    bridge: Any = None
    aborted_requests: int = 0
    aborted_by_reason: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def record_abort(self, reason: str) -> None:
        with self._lock:
            self.aborted_requests += 1
            self.aborted_by_reason[reason] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            aborted = self.aborted_requests
            reasons = dict(self.aborted_by_reason)
        data: dict[str, Any] = {
            "enabled": self.config.enabled,
            "aborted_requests": aborted,
            "aborted_by_reason": reasons,
            "cache_hits": self.cache.hits if self.cache is not None else 0,
            "cache_misses": self.cache.misses if self.cache is not None else 0,
            "cache_recorded": self.cache.recorded if self.cache is not None else 0,
            "bytes_saved": self.cache.bytes_saved if self.cache is not None else 0,
        }
        if self.bridge is not None and callable(getattr(self.bridge, "traffic_snapshot", None)):
            data["proxy_transport"] = self.bridge.traffic_snapshot()
        return data

    def summary_line(self) -> str:
        snap = self.snapshot()
        parts = [
            f"aborted={snap['aborted_requests']}",
            f"cache_hits={snap['cache_hits']}",
            f"cache_saved={snap['bytes_saved']}B",
        ]
        transport = snap.get("proxy_transport")
        if isinstance(transport, dict):
            parts.append(f"proxy_total={transport.get('total_bytes', 0)}B")
            parts.append(f"proxy_blocked={transport.get('blocked_connections', 0)}")
            if transport.get("recovered_connection_count"):
                parts.append(f"proxy_recovered={transport['recovered_connection_count']}")
        return "[DataSaver] " + " ".join(parts)


def install_playwright_data_saver(
    context: Any,
    config: BrowserDataSaverConfig | None,
    *,
    bridge: Any = None,
) -> BrowserDataSaverHandle | None:
    """Attach optional resource filtering and static asset caching to a Playwright context."""
    if config is None or not config.enabled or context is None:
        return None
    route_fn = getattr(context, "route", None)
    if not callable(route_fn):
        return None
    cache = (
        StaticAssetCache(
            config.resolved_cache_dir(),
            max_age_seconds=config.asset_cache_max_age_seconds,
            max_item_bytes=config.asset_cache_max_item_bytes,
        )
        if config.asset_cache_enabled
        else None
    )
    handle = BrowserDataSaverHandle(config=config, cache=cache, bridge=bridge)

    def _on_route(route: Any) -> None:
        req = getattr(route, "request", None)
        url = str(getattr(req, "url", "") or "")
        rtype = str(getattr(req, "resource_type", "") or "")
        method = str(getattr(req, "method", "GET") or "GET").upper()
        abort, reason = should_abort_request(url, rtype, config)
        if abort:
            handle.record_abort(reason)
            route.abort()
            return
        if cache is not None and is_cacheable_static_asset(url, rtype, method=method):
            cached = cache.get(url)
            if cached is not None:
                route.fulfill(status=cached["status"], headers=cached["headers"], body=cached["body"])
                return
            fetch = getattr(route, "fetch", None)
            if callable(fetch):
                try:
                    fetched = fetch()
                    status = int(getattr(fetched, "status", 0) or 0)
                    body_fn = getattr(fetched, "body", None)
                    body = body_fn() if callable(body_fn) else b""
                    headers = dict(getattr(fetched, "headers", None) or {})
                    if status == 200 and isinstance(body, bytes):
                        cache.put(url, status=status, headers=headers, body=body, resource_type=rtype)
                    route.fulfill(response=fetched)
                    return
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    _LOGGER.debug("data_saver cache fetch fallback for %s: %s", rtype, type(exc).__name__)
        route.continue_()

    try:
        route_fn("**/*", _on_route)
    except (RuntimeError, ValueError, TypeError) as exc:
        _LOGGER.debug("data_saver install skipped: %s", type(exc).__name__)
        return None
    return handle


__all__ = [
    "BrowserDataSaverConfig",
    "BrowserDataSaverHandle",
    "DEFAULT_BLOCKED_PROXY_HOSTS",
    "DEFAULT_BLOCKED_RESOURCE_TYPES",
    "DEFAULT_BLOCKED_URL_PATTERNS",
    "StaticAssetCache",
    "install_playwright_data_saver",
    "is_cacheable_static_asset",
    "is_protected_url",
    "should_abort_request",
]
