"""Provider-neutral runtime helpers shared by the QR extractors.

These three functions were AST-identical in ``ideal`` / ``twint`` / ``blik``
(``scripts/extractor_parity_report.py``), and each is pure: every input arrives
as an argument, and the only module state they touch is the redaction registry
they are supposed to touch. That is the condition the consolidation plan
(``docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md``)
sets for moving a function out of a provider file — "AST-identical" alone is not
enough, because the *caller's* context can differ.

What is shared
--------------
``normalize_pre_proxy_url`` / ``proxy_chain_key`` plus the country-selector
regex they both use. ``proxy_chain_key`` deliberately
keeps its own copy of the regex instead of importing the extractors' one: the
key must stay stable across country rewrites, so the pattern is part of the
contract, not an implementation detail of the caller.

What is NOT shared
------------------
Anything that reads a provider-specific env var, locale, or constant
(``payment_accept_language``, ``saved_payment_value``, ``stripe_*``) stays in
its extractor: those functions look identical but resolve different values.
``set_proxy`` also stays: it must register the proxy in *that* extractor's own
redaction registry, so its inputs are not provider-neutral.

Rule 10: pure stdlib + ``common`` only, never ``sms_tool``.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote

from .proxy_url import normalize_proxy_url

#: ``country-NL`` / ``region=US,CA`` / ``geo_US`` selectors in a proxy username
#: or password. ``proxy_chain_key`` erases the value so one sticky seed keeps one
#: identity across country rewrites.
PROXY_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region|geo)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)


def normalize_pre_proxy_url(proxy: str) -> str:
    """Normalize a pre-proxy entry, defaulting a bare ``host:port`` to SOCKS5h."""
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"socks5h://{proxy}"
    return normalize_proxy_url(proxy)


def proxy_chain_key(proxy: str) -> str:
    """Return a redacted identity that stays stable across country rewrites."""
    proxy = unquote(normalize_proxy_url(proxy))
    normalized = PROXY_COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}*",
        proxy,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:10] if normalized else ""


__all__ = [
    "PROXY_COUNTRY_SELECTOR_RE",
    "normalize_pre_proxy_url",
    "proxy_chain_key",
]
