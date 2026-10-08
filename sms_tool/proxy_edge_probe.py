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
#: Checkout admission path an anonymous probe can hit without a session.  A real
#: frontend reaches it during the payment flow; a non-CF <500 reply means the
#: exit can enter that flow.
CHATGPT_CHECKOUT_PATH = "/backend-api/payments/checkout"
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


#: In-flow (post-preflight) challenge judgement vocabulary.  Deliberately
#: three-valued, per ``plan-2026-10-05-inflow-challenge-handoff.md`` §3.2: the
#: cost asymmetry this exists for -- a false negative burns a mailbox slot and
#: its OTP forever, a false positive costs one extra request -- is only
#: expressible if "we could not tell" stays a value instead of collapsing into
#: a guess.
EDGE_CHALLENGE = "challenge"
EDGE_NOT_CHALLENGE = "not_challenge"
EDGE_UNKNOWN = "unknown"
EDGE_CHALLENGE_VERDICTS = (EDGE_CHALLENGE, EDGE_NOT_CHALLENGE, EDGE_UNKNOWN)


def _response_status_code(response: Any) -> int:
    try:
        return _as_int(getattr(response, "status_code", 0))
    except Exception:
        return 0


def _response_body_text(response: Any, limit: int) -> str:
    """Bounded body text; never raises and never consumes a stream twice.

    ``content`` (bytes) is preferred over ``text`` so a large HTML interstitial
    is truncated *before* it is decoded, not after.  A response object that
    carries neither (a mock, or an adapter that already dropped the body) reads
    as empty rather than raising -- the verdict must stay total.
    """
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content[:limit]).decode("utf-8", "replace")
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text[:limit]
    return ""


def _response_headers(response: Any):
    headers = getattr(response, "headers", None)
    return headers if isinstance(headers, (Mapping, list, tuple)) else None


def edge_challenge_verdict(response: Any, *, body_limit: int = 4096) -> str:
    """Decide whether an in-flow ``403``/``429`` is an exit-level CF challenge.

    Returns one of :data:`EDGE_CHALLENGE` / :data:`EDGE_NOT_CHALLENGE` /
    :data:`EDGE_UNKNOWN`.  Pure and total: it reads the response, never raises,
    and never touches session or circuit state.

    Three rules, all from ``plan-2026-10-05-inflow-challenge-handoff.md`` §3.2:

    1. **Only 403/429 get a yes/no answer.**  Any other status is
       :data:`EDGE_UNKNOWN`: the body of a successful OTP reply legitimately
       contains the word "challenge" (``auth_flow/otp.py``'s
       ``login_challenge`` transaction arm), so a text match on a 2xx would
       manufacture an exit-level verdict out of an unrelated OTP concept.
    2. **The judgement is** :func:`classify_edge_response`'s, not a second set
       of host/header matchers.  A reply the probe calls :data:`BLOCKED` -- "the
       edge refused this exit, reachable but useless for registration" -- is
       exactly the rotate-the-exit trigger.  Note the probe's own contract
       reads *any* 403 at this edge as :data:`BLOCKED`; the two ways a 403
       reads :data:`EDGE_NOT_CHALLENGE` here are an account-deactivated body
       (rule 3) and nothing else, which is honest about what a bare 403 can
       tell us.
    3. **An already-dead account is not a challenge.**  Rotating the egress
       cannot change an ``account_deactivated`` answer, so that vocabulary is
       excluded first -- the same order SunnyRegister's
       ``_is_challenge_response`` uses (``protocol_auth.py:609``).

    ``429`` needs no special case: a plain rate-limit reply classifies
    :data:`CLEAN` (the origin answered), so it reads :data:`EDGE_NOT_CHALLENGE`
    and stays with the ``rate_limit`` handler; a 429 that *does* carry CF
    challenge markers reads :data:`EDGE_CHALLENGE`, which is observation only --
    §3.5 forbids the control flow from rotating on a ``rate_limit`` class.
    """
    status = _response_status_code(response)
    if status not in (403, 429):
        return EDGE_UNKNOWN
    body = _response_body_text(response, max(0, int(body_limit or 0)))
    # ``accounts/account_terminal`` is the single owner of the deactivation
    # vocabulary.  A module-level import would add a ``sms_tool ->
    # sms_tool/accounts`` edge that ``scripts/import_layer_ratchet.py`` freezes
    # (9 pairs); that ratchet's own failure text names a function-local import
    # as the remedy, which is also how ``payment_auth`` reaches the package.
    from .accounts.account_terminal import text_has_account_deactivated

    if text_has_account_deactivated(body):
        return EDGE_NOT_CHALLENGE
    if classify_edge_response(status, body, _response_headers(response)) == BLOCKED:
        return EDGE_CHALLENGE
    return EDGE_NOT_CHALLENGE


def probe_openai_edge(
    proxy: str,
    *,
    chat_base: str = DEFAULT_CHAT_BASE,
    timeout: float = DEFAULT_TIMEOUT,
    impersonate: str = PROBE_IMPERSONATE,
    path: str = CHATGPT_LOGIN_PATH,
) -> EdgeVerdict:
    """Send one anonymous GET to a ChatGPT/OpenAI edge path through ``proxy``.

    ``path`` defaults to the login page; callers probing *checkout admission*
    pass ``/backend-api/payments/checkout`` so the verdict answers "can this
    exit enter the payment flow" rather than only "is the login edge up".

    Never raises; a transport failure becomes :data:`DEAD`.  ``proxy=""`` probes
    the direct egress.  The returned verdict carries a redacted URL.
    """
    value = normalize_proxy_url(proxy)
    target = str(chat_base or DEFAULT_CHAT_BASE).rstrip("/") + (str(path or "").strip() or CHATGPT_LOGIN_PATH)
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

    # Function-local on purpose (counted by ``scripts/delayed_import_ratchet.py``):
    # ``promotion_batch`` imports this module at top level and curl_cffi is a heavy
    # optional dependency.  Deferring it keeps that importer chain free of curl_cffi
    # and lets the probe degrade to ``dead`` with a named reason instead of failing
    # at import time.
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
