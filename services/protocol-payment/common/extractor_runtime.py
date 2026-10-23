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
import os
import re
from collections.abc import Callable, Mapping, Sequence
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


#: Values that mean "no pre-proxy" in any provider's ``*_PRE_PROXY`` variable.
#: One set, so a new disabled spelling cannot be honoured by one extractor and
#: ignored by another.
PRE_PROXY_DISABLED_VALUES = frozenset({"", "0", "off", "none", "direct", "disabled"})


def resolve_pre_proxy_url(
    env_names: Sequence[str],
    *,
    normalize: Callable[[str], str],
    register: Callable[[str], None],
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the local pre-proxy from the first present provider variable.

    ``pre_proxy_url`` was AST-identical in ideal / twint / blik apart from the
    env-var prefix (and blik's dead ``raw = ""`` tail, which could only ever
    return ``""``). The resolver is shared; ``normalize`` and ``register`` stay
    injected because each extractor owns its own normaliser and its own
    redaction registry -- ``register`` writes into *that* module's set, which is
    exactly the "caller context" the consolidation plan requires to stay local.

    ``environ`` is injectable so the differential test can drive the frozen
    pre-merge body and this resolver with the same mapping.
    """
    source = os.environ if environ is None else environ
    for name in env_names:
        if name not in source:
            continue
        raw = str(source.get(name, "")).strip()
        if raw.lower() in PRE_PROXY_DISABLED_VALUES:
            return ""
        proxy = normalize(raw)
        register(proxy)
        return proxy
    return ""


def proxy_chain_key(proxy: str) -> str:
    """Return a redacted identity that stays stable across country rewrites."""
    proxy = unquote(normalize_proxy_url(proxy))
    normalized = PROXY_COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}*",
        proxy,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:10] if normalized else ""


__all__ = [
    "PRE_PROXY_DISABLED_VALUES",
    "PROXY_COUNTRY_SELECTOR_RE",
    "normalize_pre_proxy_url",
    "proxy_chain_key",
    "resolve_pre_proxy_url",
]
