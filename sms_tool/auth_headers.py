"""Shared browser-like headers for OpenAI auth protocol calls."""

from __future__ import annotations

import random
import threading
from urllib.parse import urlparse


AUTH_IMPERSONATE = "chrome124"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
DEFAULT_SEC_CH_UA = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
AUTH_FINGERPRINT_PROFILES = {
    f"chrome{version}": {
        "name": f"chrome{version}",
        "impersonate": f"chrome{version}",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not-A.Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    }
    for version in (124, 131, 136, 142)
}
_AUTH_FINGERPRINT_LOCAL = threading.local()


def _auth_fingerprint_config():
    try:
        from .config import CFG

        email_cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
        value = email_cfg.get("auth_fingerprint") or CFG.get("auth_fingerprint") or {}
        return value if isinstance(value, dict) else {"profile": value}
    except Exception:
        return {}


def _configured_auth_profiles():
    cfg = _auth_fingerprint_config()
    configured = cfg.get("profiles")
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.replace(";", ",").split(",") if item.strip()]
    if not isinstance(configured, (list, tuple)):
        configured = []
    names = [str(item or "").strip().lower() for item in configured]
    try:
        from curl_cffi.requests.impersonate import BrowserType

        supported = {item.value for item in BrowserType}
    except Exception:
        supported = {AUTH_IMPERSONATE}
    names = [name for name in names if name in AUTH_FINGERPRINT_PROFILES and name in supported]
    defaults = [name for name in AUTH_FINGERPRINT_PROFILES if name in supported]
    return names or defaults or [AUTH_IMPERSONATE]


def select_auth_fingerprint(rotate=False):
    cfg = _auth_fingerprint_config()
    mode = str(cfg.get("mode") or "fixed").strip().lower()
    names = _configured_auth_profiles()
    configured = str(cfg.get("profile") or AUTH_IMPERSONATE).strip().lower()
    if rotate and mode in {"rotate", "random", "per_account"}:
        previous = getattr(_AUTH_FINGERPRINT_LOCAL, "profile_name", "")
        choices = [name for name in names if name != previous] or names
        name = random.SystemRandom().choice(choices)
    else:
        name = configured if configured in names else names[0]
    _AUTH_FINGERPRINT_LOCAL.profile_name = name
    return dict(AUTH_FINGERPRINT_PROFILES[name])


def current_auth_fingerprint():
    name = getattr(_AUTH_FINGERPRINT_LOCAL, "profile_name", "")
    if name not in AUTH_FINGERPRINT_PROFILES:
        return select_auth_fingerprint(rotate=True)
    return dict(AUTH_FINGERPRINT_PROFILES[name])


def auth_impersonate():
    return current_auth_fingerprint()["impersonate"]


def auth_user_agent():
    return current_auth_fingerprint()["user_agent"]


def datadog_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def origin_from_referer(referer: str = "") -> str:
    try:
        parsed = urlparse(str(referer or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def _extra_header_value(extra: dict | None, name: str) -> str:
    if not isinstance(extra, dict):
        return ""
    target = name.lower()
    for key, value in extra.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def openai_auth_headers(
    did: str = "",
    *,
    referer: str = "",
    origin: str = "",
    accept: str = "application/json",
    sentinel: dict | None = None,
    sentinel_token: str = "",
    sentinel_so_token: str = "",
    extra: dict | None = None,
    include_trace: bool = True,
) -> dict[str, str]:
    referer = str(referer or "").strip() or _extra_header_value(extra, "referer")
    origin = str(origin or "").strip() or _extra_header_value(extra, "origin")
    fingerprint = current_auth_fingerprint()
    headers = {
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": fingerprint["user_agent"],
        "sec-ch-ua": fingerprint["sec_ch_ua"],
        "sec-ch-ua-mobile": fingerprint["sec_ch_ua_mobile"],
        "sec-ch-ua-platform": fingerprint["sec_ch_ua_platform"],
    }
    if referer:
        headers["Referer"] = str(referer)
    resolved_origin = str(origin or "").strip() or origin_from_referer(referer)
    if resolved_origin:
        headers["Origin"] = resolved_origin
    did = str(did or "").strip()
    if did:
        headers["oai-device-id"] = did
    if include_trace:
        headers.update(datadog_trace_headers())
    if sentinel or sentinel_token or sentinel_so_token:
        try:
            from .codex_sentinel import attach_sentinel

            sentinel_data = dict(sentinel or {})
            if sentinel_token:
                sentinel_data["sentinel_token"] = sentinel_token
            if sentinel_so_token:
                sentinel_data["sentinel_so_token"] = sentinel_so_token
            attach_sentinel(headers, sentinel_data)
        except Exception:
            pass
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return headers


def openai_auth_headers_lower(did: str = "", extra: dict | None = None, **kwargs) -> dict[str, str]:
    headers = openai_auth_headers(did, extra=extra, **kwargs)
    return {str(k).lower(): v for k, v in headers.items()}
