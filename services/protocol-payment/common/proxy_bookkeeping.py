"""Proxy and token bookkeeping shared by the protocol-payment extractors.

Why this module exists
----------------------
The provider extractors under `services/protocol-payment/` were grown by
copy-paste.  The 2026-09-17 parity audit (`scripts/extractor_parity_report.py`)
found 108 of ideal's 134 top-level functions AST-identical to twint's once
provider naming is normalised away.

This module holds the subset that is **provably provider-free**: no provider
name, no provider-specific environment variable, no provider-specific data
table.  Those were verified mechanically -- a function whose body references a
module-level constant that differs between files, or an env var whose name
embeds the provider, was rejected from this batch.

    ``_redact_text`` was REJECTED for exactly that reason: it reads
    ``IDEAL_DUMP_LIMIT`` in ideal, ``TWINT_DUMP_LIMIT`` in twint (and
    ``IDEAL_DUMP_LIMIT`` in blik, which looks like a copy-paste bug).  The AST
    comparison called it "identical" only because the normaliser blanks
    provider tokens -- including the one inside the env var name.  Extracting it
    here would have silently unified three different knobs.

    (``_redact_text`` now lives in ``common/protocol_core.py``, which is
    provider-free by construction.  Its correct home is there, not here.)

Rule 10 compliance
------------------
This package must not import `sms_tool`.  Nothing here does; it is pure stdlib.

Design note: bodies are copied, not improved
--------------------------------------------
Phase 1 of the consolidation plan requires differential verification that
behaviour is unchanged, and an "improvement" made during extraction is exactly
the thing that invalidates that proof.  So every body below is the extractors'
own, character for character, including their chosen idioms.

That rule caught a real transcription bug on the first pass of this module:
`token_key_name` had been rewritten as "drop ``_`` and ``-``", which is *not*
the original -- the original is ``re.sub(r"[^a-z0-9]+", "", ...)`` and drops
**every** non-alphanumeric, so ``api.key`` and ``xsrf token`` normalise
differently under the two versions.  The rewrite looked tidier and was wrong.
It is restored verbatim below.

``proxy_short`` / ``proxy_key`` take the normaliser as a parameter instead of
calling a module-local `normalize_proxy_url`, because the extractors carry
*different* four-field conventions -- see `proxy_short`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable
from urllib.parse import urlparse

__all__ = [
    "proxy_short",
    "proxy_label",
    "proxy_key",
    "is_known_static_host",
    "find_named_token",
    "token_key_name",
]


def proxy_short(proxy: str, normalize: Callable[[str], str]) -> str:
    """Opaque short label for a proxy URL, used in logs.

    ``normalize`` is injected rather than imported.  The extractors each carry
    their own `normalize_proxy_url` and they do **not** agree: blik reads the
    bare provider form as ``user:pass:host:port`` while the others read
    ``host:port:user:pass`` (see `common/proxy_url.py`).  Importing one caller's
    normaliser here would silently change the rest, so the dependency stays
    visible at the call site.

    Every caller passes its own module-local `normalize_proxy_url`, so the
    observable result is exactly what the pre-extraction body produced.
    """
    proxy = normalize(proxy)
    if not proxy:
        return "direct"
    digest = hashlib.sha256(proxy.encode()).hexdigest()[:10]
    return f"proxy#{digest}"


def proxy_label(proxy: str, normalize: Callable[[str], str]) -> str:
    """Alias for `proxy_short`.

    Historically a distinct name, but in all four extractors that define it the
    body is literally ``return proxy_short(proxy)`` -- a pure alias.  It is kept
    because call sites use both spellings (20+ uses of `proxy_label`, 2 of
    `proxy_short`) and because it is what the log-redaction path calls.
    """
    return proxy_short(proxy, normalize)


def proxy_key(proxy: str, normalize: Callable[[str], str]) -> str:
    """Stable dict key for a proxy URL: full SHA256, or ``""`` for direct.

    Unlike `proxy_short` this is used for state lookup, so it must not truncate.
    """
    proxy = normalize(proxy)
    return hashlib.sha256(proxy.encode()).hexdigest() if proxy else ""


# Hosts whose URLs are noise in a redirect trace.  Shared because the set is
# provider-independent: Stripe's CDN is Stripe's CDN in every flow.
_KNOWN_STATIC_HOSTS = frozenset({
    "stripe-camo.global.ssl.fastly.net",
    "files.stripe.com",
    "js.stripe.com",
    "m.stripe.network",
    "q.stripe.com",
})


def is_known_static_host(url: str) -> bool:
    """True when ``url``'s netloc is a static-asset host not worth following.

    The extractors spell the host set as a fresh ``set`` literal inside the
    function; hoisting it to a module-level ``frozenset`` changes the container
    type but not the membership test, which is the only thing the function does.
    """
    host = (urlparse(url).netloc or "").lower()
    return host in _KNOWN_STATIC_HOSTS


def token_key_name(value: Any) -> str:
    """Normalise a token/cookie key for comparison.

    Drops **every** non-alphanumeric character after lowercasing, so
    ``accessToken``, ``access_token``, ``access-token`` and ``api.key`` all
    collapse onto their alphanumeric core.

    Restored verbatim from the extractors.  A hand-tidied version that only
    stripped ``_`` and ``-`` was briefly in this module and is wrong: it leaves
    ``.`` and spaces in place, so ``api.key`` would not match ``apikey``.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def find_named_token(payload: Any, aliases: tuple[str, ...]) -> str:
    """Depth-first search for the first token-ish value under any of ``aliases``.

    ``aliases`` are compared through `token_key_name`, so ``accessToken``,
    ``access_token`` and ``accesstoken`` all match.  Returns ``""`` when absent.
    """
    wanted = {token_key_name(item) for item in aliases}
    if isinstance(payload, dict):
        cookie_name = token_key_name(payload.get("name") or payload.get("key"))
        if cookie_name in wanted:
            for value_key in ("value", "token", "content"):
                value = str(payload.get(value_key) or "").strip()
                if value:
                    return value
        for key, value in payload.items():
            if token_key_name(key) in wanted and isinstance(value, (str, int, float)):
                found = str(value).strip()
                if found:
                    return found
        for value in payload.values():
            found = find_named_token(value, aliases)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_named_token(item, aliases)
            if found:
                return found
    return ""
