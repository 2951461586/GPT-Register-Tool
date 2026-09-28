"""Read-only OpenAI-edge reachability probe for a single proxy.

Why this exists
---------------
Two different questions were being answered by one signal:

* **"can this proxy carry traffic?"** — ``Socks5Server``'s health probe opens a
  TCP/TLS tunnel to ``cloudflare.com:443`` and closes it.  It never sends an
  HTTP request, so an exit that Cloudflare answers with a **403 challenge for
  ``chatgpt.com``** is still reported healthy.
* **"will ChatGPT actually serve this exit?"** — only
  ``registration_preflight.registration_network_preflight`` answers it, and only
  per account, so a batch discovers a blocked pool entry by burning a preflight
  slot rather than before the run.

This module extracts that second question into one read-only probe an operator
tool can run over a whole pool first.  It performs a single anonymous ``GET`` to
the ChatGPT login edge, classifies the reply, and **never raises and never
mutates state** -- no mailbox, no account, no checkout.

Verdict vocabulary
------------------
``clean``     the edge served the request (200/3xx), or reached the origin and
              answered 401/429.  This is the only status a registration pool
              should be built from.
``blocked``   the edge refused with 403, or returned an HTML challenge
              (``cf-chl`` / "Just a moment" / ``cf-mitigated: challenge``).
              Reachable but useless for registration.
``degraded``  reached something, but neither clean nor an explicit refusal
              (e.g. 5xx).  Treat as unusable until re-measured.
``dead``      the transport failed (connect/TLS/timeout); the proxy cannot
              serve anything.

Credentials never leave this module: an :class:`EdgeVerdict` carries the
*redacted* proxy URL, never the raw one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .phone_proxy import normalize_proxy_url, redact_proxy_url

CLEAN = "clean"
BLOCKED = "blocked"
DEGRADED = "degraded"
DEAD = "dead"

STATUSES = (CLEAN, BLOCKED, DEGRADED, DEAD)

#: The registration entry page.  It is Cloudflare-fronted, so an exit that can
#: reach it is a plausible registration exit; a CF block shows up as 403/challenge
#: here rather than only at account creation.
CHATGPT_LOGIN_PATH = "/login"
DEFAULT_CHAT_BASE = "https://chatgpt.com"
DEFAULT_TIMEOUT = 15.0
PROBE_IMPERSONATE = "chrome146"

#: Markers of a Cloudflare interstitial.  Lower-cased; matched against a bounded
#: slice of the body and the response headers.
_CHALLENGE_MARKERS = (
    "cf-chl",
    "cf_chl",
    "cf-mitigated",
    "challenge-platform",
    "just a moment",
    "attention required",
    "enable javascript and cookies to continue",
)

#: Only this many body bytes are inspected; a CF challenge is in the <head>.
_BODY_LIMIT = 65536


@dataclass(frozen=True)
class EdgeVerdict:
    """One proxy's anonymous reachability of the ChatGPT edge.

    ``proxy`` is always redacted (``***:***`` credentials) so the verdict is
    safe to log, print or persist.
    """

    proxy: str
    status: str
    http_status: int = 0
    blocked_by_cloudflare: bool = False
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == CLEAN

    def to_dict(self) -> dict[str, Any]:
        return {
            "proxy": self.proxy,
            "status": self.status,
            "http_status": self.http_status,
            "blocked_by_cloudflare": self.blocked_by_cloudflare,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce to int, falling back on anything malformed.

    Keeps :func:`classify_edge_response` total and keeps a non-int
    ``response.status_code`` (a mock, or an unusual transport) from turning a
    probe into a crash.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _header_text(headers: Mapping[str, Any] | Sequence[tuple[str, str]] | None) -> str:
    if not headers:
        return ""
    items = headers.items() if isinstance(headers, Mapping) else headers
    try:
        return " ".join(f"{name}:{value}" for name, value in items).lower()
    except (AttributeError, TypeError, ValueError):
        return ""


def classify_edge_response(
    status_code: int,
    body: str = "",
    headers: Mapping[str, Any] | Sequence[tuple[str, str]] | None = None,
) -> str:
    """Map an HTTP reply from the ChatGPT edge to a verdict status.

    Pure and total: any integer status maps to exactly one of
    :data:`STATUSES`.  The only statuses that mean "usable for registration" are
    :data:`CLEAN`.
    """
    code = _as_int(status_code)
    if code <= 0:
        return DEAD
    text = str(body or "")[:_BODY_LIMIT].lower()
    headers_text = _header_text(headers)
    challenge = any(marker in text for marker in _CHALLENGE_MARKERS) or any(
        marker in headers_text for marker in _CHALLENGE_MARKERS
    )
    if code == 403:
        # Any 403 at the login edge is a refusal; an HTML/challenge body is the
        # Cloudflare interstitial, a bare 403 is still the edge declining this exit.
        return BLOCKED
    if challenge and code >= 400:
        return BLOCKED
    if 200 <= code < 400:
        return CLEAN
    if code in (401, 429):
        # Reached the origin: an anonymous request has no token (401) and the
        # origin can rate-limit (429).  Both mean the exit is not CF-blocked.
        return CLEAN
    if 500 <= code < 600:
        return DEGRADED
    if 400 <= code < 500:
        return DEGRADED
    return DEGRADED


def probe_openai_edge(
    proxy: str,
    *,
    chat_base: str = DEFAULT_CHAT_BASE,
    timeout: float = DEFAULT_TIMEOUT,
    impersonate: str = PROBE_IMPERSONATE,
) -> EdgeVerdict:
    """Send one anonymous GET to the ChatGPT login edge through ``proxy``.

    Never raises; a transport failure becomes :data:`DEAD`.  ``proxy=""`` probes
    the direct egress.  The returned verdict carries a redacted URL.
    """
    value = normalize_proxy_url(proxy)
    target = str(chat_base or DEFAULT_CHAT_BASE).rstrip("/") + CHATGPT_LOGIN_PATH
    redacted = redact_proxy_url(value) if value else "DIRECT"
    started = time.monotonic()

    def _verdict(status: str, *, http_status: int = 0, blocked: bool = False, error: str = "") -> EdgeVerdict:
        return EdgeVerdict(
            proxy=redacted,
            status=status,
            http_status=http_status,
            blocked_by_cloudflare=blocked,
            error=error,
            elapsed_ms=_as_int((time.monotonic() - started) * 1000),
        )

    try:
        from curl_cffi import requests as curl_requests
    except Exception:
        return _verdict(DEAD, error="curl_cffi_unavailable")

    session = None
    try:
        session = curl_requests.Session(impersonate=impersonate)
        session.trust_env = False
        if value:
            session.proxies = {"http": value, "https": value}
        response = session.get(
            target,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=max(1.0, float(timeout or DEFAULT_TIMEOUT)),
            allow_redirects=False,
        )
    except Exception as exc:
        return _verdict(DEAD, error=f"{type(exc).__name__}")
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass

    status_code = _as_int(getattr(response, "status_code", 0))
    try:
        body = str(getattr(response, "text", "") or "")
    except Exception:
        body = ""
    try:
        headers: Any = dict(getattr(response, "headers", {}) or {})
    except Exception:
        headers = {}
    status = classify_edge_response(status_code, body, headers)
    return _verdict(status, http_status=status_code, blocked=status == BLOCKED)


def edge_health(proxy: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Adapt :func:`probe_openai_edge` to the SOCKS5 pool's health contract.

    The pool treats an upstream as healthy only when the edge serves it; a
    Cloudflare block, a 5xx or a dead transport is a failure, with a short,
    credential-free detail string for the shared tracker's ``last_error``.
    Formatting the detail lives here so ``proxy_pool`` needs no import of this
    module (it keeps its zero-runtime-import design and takes the callable by
    injection instead).
    """
    verdict = probe_openai_edge(proxy, timeout=timeout)
    if verdict.status == CLEAN:
        return True, ""
    detail = f"edge:{verdict.status}"
    if verdict.http_status:
        detail += f":http_{verdict.http_status}"
    if verdict.blocked_by_cloudflare:
        detail += ":cloudflare"
    return False, detail


__all__ = [
    "BLOCKED",
    "CHATGPT_LOGIN_PATH",
    "CLEAN",
    "DEAD",
    "DEFAULT_CHAT_BASE",
    "DEFAULT_TIMEOUT",
    "DEGRADED",
    "EdgeVerdict",
    "STATUSES",
    "classify_edge_response",
    "edge_health",
    "probe_openai_edge",
]
