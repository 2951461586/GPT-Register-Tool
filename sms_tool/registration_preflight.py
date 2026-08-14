"""Registration network preflight: proxy scheme detection and auth-edge checks."""

from dataclasses import replace
from typing import Mapping

from curl_cffi import requests as curl_requests

from .auth_headers import (
    auth_fingerprint_capabilities,
    auth_impersonate,
    curl_cffi_capabilities,
    current_auth_fingerprint,
    openai_auth_headers,
)
from .config import CFG
from .account_liveness import CODEX_USAGE_URL
from .phone_proxy import normalize_proxy_url, redact_proxy_url, refresh_proxy_sid
from .sentinel_tokens import _sentinel_frame_version

# ``auto`` keeps the historical mislabeled-provider correction; ``off`` pins the
# declared scheme so a transient socks5 outage cannot change the transport.
_SCHEME_FALLBACK_MODES = ("auto", "off")


def _chat_base() -> str:
    return str((CFG.get("chatgpt") or {}).get("chat_base_url") or "https://chatgpt.com").rstrip("/")


def _proxy_scheme_fallback_mode(cfg=None) -> str:
    source = cfg if isinstance(cfg, Mapping) else CFG
    registration = source.get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    mode = str(registration.get("proxy_scheme_fallback") or "auto").strip().lower()
    return mode if mode in _SCHEME_FALLBACK_MODES else "auto"


def _with_proxy_scheme(proxy: str, scheme: str) -> str:
    """Re-render a proxy URL under a different scheme without string surgery."""
    from .proxy_entry import parse_proxy, proxy_to_url

    entry = parse_proxy(proxy)
    return proxy_to_url(replace(entry, scheme=scheme)) if entry is not None else ""


def _proxy_scheme_reachable(candidate: str, url: str) -> bool:
    session = curl_requests.Session()
    try:
        session.trust_env = False
    except Exception:
        pass
    session.proxies = {"http": candidate, "https": candidate}
    try:
        session.get(url, timeout=15, impersonate=auth_impersonate())
        return True
    except Exception:
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


def _resolve_proxy_scheme(proxy, *, cfg=None):
    """Confirm the declared proxy scheme, correcting a mislabeled one only when allowed.

    Providers routinely hand out ``socks5h://`` URLs that are really HTTP
    CONNECT endpoints, so the correction stays available. But swapping the
    transport because of a transient socks5 outage is not the same thing as
    fixing a mislabeled endpoint: the swap is announced, it is verified against
    the real auth edge over TLS rather than a plaintext geo lookup, and
    ``registration.proxy_scheme_fallback=off`` pins the declared scheme.
    """
    candidate = normalize_proxy_url(proxy)
    if not candidate or not candidate.startswith(("socks5h://", "socks5://")):
        return candidate
    probe_url = f"{_chat_base()}/robots.txt"
    if _proxy_scheme_reachable(candidate, probe_url):
        return candidate
    label = redact_proxy_url(candidate)
    if _proxy_scheme_fallback_mode(cfg) == "off":
        print(f"[!] Proxy {label} failed the socks5 scheme check; keeping the declared "
              "scheme (registration.proxy_scheme_fallback=off)")
        return candidate
    http_candidate = _with_proxy_scheme(candidate, "http")
    if http_candidate and _proxy_scheme_reachable(http_candidate, probe_url):
        print(f"[!] Proxy {label} does not answer as socks5 but does as an HTTP CONNECT "
              "proxy; downgrading the scheme to http:// for this run")
        return http_candidate
    print(f"[!] Warning: proxy {label} failed the connectivity test as both socks5 and http")
    return candidate


def registration_network_preflight(proxy=None, *, proxy_attempts: int = 2):
    """Validate the three auth edge nodes before claiming a mailbox."""
    capabilities = curl_cffi_capabilities()
    profile_capabilities = auth_fingerprint_capabilities()
    if not capabilities["version_ok"] and profile_capabilities["missing"]:
        raise RuntimeError(
            "auth_fingerprint_unavailable:curl_cffi_requires_0.15.x_or_0.16.x"
        )
    if profile_capabilities["missing"]:
        raise RuntimeError(
            "auth_fingerprint_unavailable:" + ",".join(profile_capabilities["missing"])
        )
    chat_base = str((CFG.get("chatgpt") or {}).get("chat_base_url") or "https://chatgpt.com").rstrip("/")
    auth_base = str((CFG.get("chatgpt") or {}).get("auth_base_url") or "https://auth.openai.com").rstrip("/")
    sentinel_url = "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=" + _sentinel_frame_version()
    checks = (
        ("chatgpt-login", f"{chat_base}/login", f"{chat_base}/", False),
        ("auth-login", f"{auth_base}/log-in", f"{chat_base}/login", False),
        ("sentinel-frame", sentinel_url, f"{auth_base}/log-in", False),
        # The endpoint requires an AT, so an HTTP 401/403 is expected here.  A
        # transport failure is not: it would discard an already-created account
        # later when the registration AT is validated.
        ("chatgpt-backend", CODEX_USAGE_URL, f"{chat_base}/", True),
    )
    candidate = normalize_proxy_url(proxy) or None
    last_error = None
    for attempt in range(max(1, min(int(proxy_attempts or 1), 3))):
        session = curl_requests.Session()
        try:
            session.trust_env = False
        except Exception:
            pass
        session.proxies = {"http": candidate, "https": candidate} if candidate else {"http": "", "https": ""}
        try:
            for label, url, referer, allow_http_error in checks:
                headers = openai_auth_headers(
                    referer=referer,
                    origin=url.split("/", 3)[0] + "//" + url.split("/", 3)[2],
                    accept="text/html,application/xhtml+xml",
                    include_trace=True,
                    extra={
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "same-site",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                response = session.get(url, headers=headers, timeout=15, impersonate=auth_impersonate())
                if not allow_http_error and int(getattr(response, "status_code", 0) or 0) >= 400:
                    raise RuntimeError(f"registration_preflight_failed:{label}:http_{response.status_code}")
            result = {"ok": True, "profile": current_auth_fingerprint()["impersonate"]}
            original = normalize_proxy_url(proxy) or ""
            if candidate and candidate != original:
                result["proxy"] = candidate
            return result
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= max(1, min(int(proxy_attempts or 1), 3)) or not candidate:
                break
            candidate = refresh_proxy_sid(candidate)
        finally:
            try:
                session.close()
            except Exception:
                pass
    raise RuntimeError(str(last_error or "registration_preflight_failed"))
