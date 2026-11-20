"""HTTP session construction and redirect-following shared by the QR extractors.

Stage 1 (batch 10) of
`docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared, and why it is safe
----------------------------------
Two functions were AST-identical across the extractors once the provider token is
normalised away, and neither branches on the provider:

* ``new_session`` (19 lines) -- ideal / twint identical; blik differs on **one**
  line (its pre-proxy is additionally gated behind
  ``IDEAL_USE_LOCAL_PROXY_ONLY``). The shared body owns the curl_cffi ->
  ``requests`` fallback, the ``trust_env`` guard and the pre-proxy error
  contract; *how* ``pre_proxy`` is resolved is injected.
* ``fetch_redirect_page`` (14 lines) -- identical in all three; the only
  difference is the ``*_FOLLOW_REDIRECT`` env-var *name*.

What stays with the caller, and why it must
-------------------------------------------
* ``resolve_pre_proxy(use_pre_proxy)`` -- blik's extra env gate is provider
  behaviour. Injecting the resolution (not just the boolean) keeps that gate in
  blik, where an operator reads it.
* ``set_proxy`` / ``register_proxy_for_redaction`` -- each extractor owns its own
  redaction registry, so the side effect must run against *that* module's set.
  This is the same reason ``common/extractor_runtime.py`` keeps ``set_proxy``
  local.
* ``build_chatgpt_session`` is deliberately **not** here: its header table is
  exactly where the providers differ (ideal/twint send the Safari/macOS set plus
  ``oai-client-*``; blik sends the Chrome/Windows set and omits the client
  headers). Sharing the body would only relocate provider data and change header
  ordering on the wire.
* ``log`` stays: it writes through each module's own log file, lock and durable
  logger.

The two error strings below are the pre-merge literals, moved verbatim: operators
match on them, so a reworded message would be a behaviour change.

Rule 10: pure stdlib + ``common`` only, never ``sms_tool``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

try:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:  # pragma: no cover - exercised by the batch-10 differential test
    CurlOpt = None
    CurlCffiSession = None

from .timeouts import DEFAULT_TIMEOUT

__all__ = [
    "MAX_REDIRECT_HOPS",
    "RedirectHooks",
    "fetch_redirect_page",
    "new_session",
]

#: Pre-merge literals, unchanged: the operator-facing contract for a missing
#: curl_cffi when a local pre-proxy is configured.
CURL_PRE_PROXY_MISSING = "本机前置代理需要 curl_cffi 支持"
CURL_MISSING = "本机前置代理需要 curl_cffi：python3 -m pip install curl_cffi"

#: Redirect hops :func:`fetch_redirect_page` walks before giving up (the
#: pre-merge loop was ``range(1, 6)``).
MAX_REDIRECT_HOPS = 5


def new_session(
    proxy: str = "",
    use_pre_proxy: bool = True,
    *,
    resolve_pre_proxy: Callable[[bool], str],
    set_proxy: Callable[[Any, str], None],
    register_proxy_for_redaction: Callable[[str], None],
) -> Any:
    """Build one browser-impersonating session, falling back to ``requests``."""
    pre_proxy = resolve_pre_proxy(use_pre_proxy)
    register_proxy_for_redaction(pre_proxy)
    if CurlCffiSession is not None:
        kwargs: dict[str, Any] = {"impersonate": "chrome136"}
        if pre_proxy:
            if CurlOpt is None:
                raise RuntimeError(CURL_PRE_PROXY_MISSING)
            kwargs["curl_options"] = {CurlOpt.PRE_PROXY: pre_proxy}
        session = CurlCffiSession(**kwargs)
    else:
        if pre_proxy:
            raise RuntimeError(CURL_MISSING)
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    if proxy:
        set_proxy(session, proxy)
    return session


@dataclass(frozen=True)
class RedirectHooks:
    """Per-extractor seams for :func:`fetch_redirect_page`.

    ``follow_flag`` is the env-var *name* (``IDEAL_FOLLOW_REDIRECT`` /
    ``TWINT_FOLLOW_REDIRECT``); passing the name keeps each module's literal in
    its own file, where an operator reads it.
    """

    env_bool: Callable[..., bool]
    dump_http: Callable[..., Any]
    extract_qr_candidates: Callable[[str], list[str]]
    follow_flag: str
    timeout: float = DEFAULT_TIMEOUT


def fetch_redirect_page(stripe: Any, start_url: str, hooks: RedirectHooks) -> list[str]:
    """Walk up to :data:`MAX_REDIRECT_HOPS` redirects and collect QR candidates."""
    if not start_url or not hooks.env_bool(hooks.follow_flag, True):
        return []
    current = start_url
    qr_urls: list[str] = []
    for hop in range(1, MAX_REDIRECT_HOPS + 1):
        resp = stripe.get(current, timeout=hooks.timeout, allow_redirects=False)
        hooks.dump_http(resp, f"redirect_hop_{hop}", None, "GET", current, force=True)
        qr_urls.extend(hooks.extract_qr_candidates(resp.text))
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            break
        current = urljoin(current, location)
    return list(dict.fromkeys(qr_urls))
