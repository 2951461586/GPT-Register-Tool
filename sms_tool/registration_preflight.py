"""Registration network preflight: proxy scheme detection and auth-edge checks."""

import logging
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
from . import endpoints
from .http_client import request_with_retry
from .accounts.account_liveness import CODEX_USAGE_URL
from .phone_proxy import normalize_proxy_url, redact_proxy_url, refresh_proxy_sid
from .proxy_edge_probe import BLOCKED, classify_edge_response
from .sentinel.bundle import sentinel_version

# ``auto`` keeps the historical mislabeled-provider correction; ``off`` pins the
# declared scheme so a transient socks5 outage cannot change the transport.
_SCHEME_FALLBACK_MODES = ("auto", "off")

logger = logging.getLogger(__name__)

# ``browser`` uses the entry page a real browser navigates to
# (``chatgpt.com/auth/login?next=%2F``).  ``legacy`` restores the historical
# probe of ``auth.openai.com/log-in``, which packet captures show a browser
# never requests outside an in-flow authorize redirect -- i.e. a pure protocol
# fingerprint (see ``wangshen233/core/openai_auth.py:154``).  Kept switchable so
# the endpoint change can be A/B'd on a live batch instead of assumed.
_PREFLIGHT_LOGIN_PAGES = ("browser", "legacy")
#: The stable machine-readable marker ``commands/registration`` keys its
#: skip-the-host guard on.  Defined here next to the classifier that raises it.
CLOUDFLARE_CHALLENGE_MARKER = "cloudflare_challenge"


def _sentinel_frame_version() -> str:
    """Compatibility seam backed by the pinned Sentinel bundle."""
    return sentinel_version()


def _chat_base() -> str:
    return str((CFG.get("chatgpt") or {}).get("chat_base_url") or endpoints.CHATGPT_BASE).rstrip("/")


def _proxy_scheme_fallback_mode(cfg=None) -> str:
    source = cfg if isinstance(cfg, Mapping) else CFG
    registration = source.get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    mode = str(registration.get("proxy_scheme_fallback") or "auto").strip().lower()
    return mode if mode in _SCHEME_FALLBACK_MODES else "auto"


def _preflight_login_page(cfg=None) -> str:
    """Which login entry the preflight probes: ``browser`` (default) or ``legacy``.

    ``registration.preflight_login_page``.  Default ``browser`` because a real
    browser only ever reaches ``auth.openai.com/log-in`` by following an
    in-flow ``authorize`` redirect; requesting it standalone is a protocol
    fingerprint.  The legacy endpoint stays reachable for the controlled A/B
    the change still owes (see ``docs/current/protocol-registration.md``).
    """
    source = cfg if isinstance(cfg, Mapping) else CFG
    registration = source.get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    mode = str(registration.get("preflight_login_page") or "browser").strip().lower()
    return mode if mode in _PREFLIGHT_LOGIN_PAGES else "browser"


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
        # One transient reset on an otherwise healthy socks5 endpoint used to
        # answer "unreachable" and trigger a scheme downgrade to http:// for the
        # whole run.  Retry the transport before declaring the scheme wrong.
        request_with_retry(session, "get", url, timeout=15, impersonate=auth_impersonate(), label="proxy scheme probe")
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
        print(
            f"[!] Proxy {label} failed the socks5 scheme check; keeping the declared "
            "scheme (registration.proxy_scheme_fallback=off)"
        )
        return candidate
    http_candidate = _with_proxy_scheme(candidate, "http")
    if http_candidate and _proxy_scheme_reachable(http_candidate, probe_url):
        print(
            f"[!] Proxy {label} does not answer as socks5 but does as an HTTP CONNECT "
            "proxy; downgrading the scheme to http:// for this run"
        )
        return http_candidate
    print(f"[!] Warning: proxy {label} failed the connectivity test as both socks5 and http")
    return candidate


def registration_network_preflight(proxy=None, *, proxy_attempts: int = 2):
    """Validate the auth edge nodes before claiming a mailbox.

    Probes the browser entry page, the Sentinel frame and the ChatGPT backend.
    A Cloudflare refusal on any mandatory check is raised with the
    :data:`CLOUDFLARE_CHALLENGE_MARKER` suffix so the caller can drop the exit
    instead of treating it as a transient failure.
    """
    capabilities = curl_cffi_capabilities()
    profile_capabilities = auth_fingerprint_capabilities()
    if not capabilities["version_ok"] and profile_capabilities["missing"]:
        raise RuntimeError("auth_fingerprint_unavailable:curl_cffi_requires_0.15.x_or_0.16.x")
    if profile_capabilities["missing"]:
        raise RuntimeError("auth_fingerprint_unavailable:" + ",".join(profile_capabilities["missing"]))
    chat_base = str((CFG.get("chatgpt") or {}).get("chat_base_url") or endpoints.CHATGPT_BASE).rstrip("/")
    auth_base = str((CFG.get("chatgpt") or {}).get("auth_base_url") or endpoints.AUTH_BASE).rstrip("/")
    sentinel_url = "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=" + _sentinel_frame_version()
    if _preflight_login_page() == "legacy":
        login_url = f"{auth_base}/log-in"
        login_referer = f"{chat_base}/login"
    else:
        # The entry page a real browser navigates to.  Do not "simplify" this
        # back to ``{auth_base}/log-in``: that endpoint is only ever reached by
        # an in-flow authorize redirect, never by a standalone navigation.
        login_url = f"{chat_base}/auth/login?next=%2F"
        login_referer = f"{chat_base}/"
    checks = (
        ("chatgpt-login", login_url, login_referer, False),
        ("sentinel-frame", sentinel_url, login_url, False),
        # The endpoint requires an AT, so an HTTP 401/403 is expected here.  A
        # transport failure is not: it would discard an already-created account
        # later when the registration AT is validated.
        ("chatgpt-backend", CODEX_USAGE_URL, f"{chat_base}/", True),
    )
    candidate = normalize_proxy_url(proxy or "") or None
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
                response = request_with_retry(
                    session,
                    "get",
                    url,
                    headers=headers,
                    timeout=15,
                    attempts=1,
                    impersonate=auth_impersonate(),
                    label=f"preflight {label}",
                )
                status = int(getattr(response, "status_code", 0) or 0)
                if not allow_http_error and status >= 400:
                    # An edge refusal and an ordinary 4xx are different failures:
                    # the first is the exit, the second may be the request.  Name
                    # the Cloudflare case so the caller can drop the whole host
                    # instead of burning the rest of its candidates on it.
                    body = str(getattr(response, "text", "") or "")
                    if classify_edge_response(status, body, getattr(response, "headers", None)) == BLOCKED:
                        logger.warning(
                            "registration preflight %s: exit challenged by Cloudflare (http_%s)",
                            label,
                            status,
                        )
                        raise RuntimeError(f"registration_preflight_failed:{label}:{CLOUDFLARE_CHALLENGE_MARKER}")
                    raise RuntimeError(f"registration_preflight_failed:{label}:http_{status}")
            result = {"ok": True, "profile": current_auth_fingerprint()["impersonate"]}
            original = normalize_proxy_url(proxy or "") or ""
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
