"""Proxy selection shared by the protocol-payment extractors.

Batch 5 of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared, and what is deliberately not
--------------------------------------------
``proxy_for_country``   ideal / twint / blik.  kakao's version is a different
                        contract, not a copy: it also appends the country to
                        the sticky ``sid`` (so each region gets its own exit
                        IP) and derives the target country with
                        ``str(country or "").strip().lower()`` rather than the
                        per-extractor ``normalize_country``, whose fallback
                        differs (NL / CH / ``default_payment_country()``).

``pick_random_proxies`` ideal / twint / blik -- byte-identical.

``is_preferred_proxy``  ideal / twint only.  blik's reads its records through
                        ``proxy_state_key(group, proxy)`` instead of
                        ``proxy_key(proxy)`` and adds a ``zero_ok`` escape
                        hatch for the ``checkout``/``seed`` groups, so its
                        notion of "preferred" is a different rule.

Injected knobs (never defaulted)
--------------------------------
``proxy_for_country``
    normalize           per-extractor proxy URL normaliser
    country_normalizer  per-extractor country normaliser (different fallback)
    selector            the country/region selector pattern; identical text in
                        all four extractors today, but each also uses it for
                        ``proxy_chain_key``, so it stays theirs and is passed
                        in rather than re-defined here
    label               per-extractor redaction label used in the error text
    register            per-extractor redaction registrar; the derived proxy
                        is a new credential and must be scrubbed like the seed
``is_preferred_proxy``
    score_env           ``IDEAL_PROXY_SCORE`` vs ``TWINT_PROXY_SCORE``
    env_bool            per-extractor env parsing
    load_state          per-extractor proxy-score state (its own file)
    key                 per-extractor record key
``pick_random_proxies``
    order_group         per-extractor group ordering
    is_preferred        per-extractor preference rule

``random`` is used directly (not injected): ``random.shuffle`` is a bound
method of a hidden module-level ``Random`` instance, so it must never appear
as a *default argument* -- resolved in the body it is still patchable by
swapping the ``random`` module object, which is what the tests do.

Rule 10: pure stdlib, no ``sms_tool`` import.
"""

from __future__ import annotations

import random
import re
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

__all__ = [
    "is_preferred_proxy",
    "pick_random_proxies",
    "proxy_for_country",
]


def proxy_for_country(
    proxy: str,
    country: str,
    *,
    normalize: Callable[[str], str],
    country_normalizer: Callable[[str], str],
    selector: re.Pattern[str],
    label: Callable[[str], str],
    register: Callable[[str], None],
) -> str:
    """Rewrite only the country selector, keeping the sticky session intact.

    The username and password are what carry the sticky session, so they are
    rewritten in place instead of being rebuilt; only the ``country=`` /
    ``region=`` selector changes.  The derived proxy is a new credential and is
    registered for redaction before it is returned -- otherwise the first log
    line mentioning it would leak it.
    """
    proxy = normalize(proxy)
    target_country = country_normalizer(country).lower()
    if not proxy:
        raise RuntimeError("代理为空，无法派生地区链路")

    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    replacements = 0

    def replace_country(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        current = match.group("value")
        value = target_country.upper() if current.isupper() else target_country
        return f"{match.group('name')}{match.group('separator')}{value}"

    username = selector.sub(replace_country, username)
    password = selector.sub(replace_country, password)
    if not replacements:
        raise RuntimeError(
            f"代理未包含可改写的 country/region 选择器: {label(proxy)}"
        )

    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = quote(username, safe="-._~")
    if parsed.password is not None:
        auth = f"{auth}:{quote(password, safe='-._~')}"
    derived = urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))
    register(derived)
    return derived


def is_preferred_proxy(
    group: str,
    proxy: str,
    *,
    score_env: str,
    env_bool: Callable[[str, bool], bool],
    load_state: Callable[[], dict[str, Any]],
    key: Callable[[str], str],
) -> bool:
    """True when this proxy has already succeeded in this group.

    Returns False -- not "unknown" -- when scoring is switched off or the group
    is empty, so callers can treat it as a plain filter.
    """
    if not group or not env_bool(score_env, True):
        return False
    state = load_state().get(group, {})
    if not isinstance(state, dict):
        return False
    record = state.get(key(proxy), {})
    if not isinstance(record, dict):
        return False
    return int(record.get("success") or 0) > 0


def pick_random_proxies(
    proxies: list[str],
    limit: int,
    group: str = "",
    *,
    order_group: Callable[[str, list[str]], list[str]],
    is_preferred: Callable[[str, str], bool],
) -> list[str]:
    """Pick ``limit`` proxies, already-successful ones first.

    Preferred proxies are always taken ahead of the random tail and are never
    shuffled away, so a proxy that worked keeps its place.  Only the tail is
    randomised.
    """
    if group:
        proxies = order_group(group, proxies)
    preferred = [proxy for proxy in proxies if is_preferred(group, proxy)]
    preferred_set = set(preferred)
    rest = [proxy for proxy in proxies if proxy not in preferred_set]
    if limit >= len(proxies):
        random.shuffle(rest)
        return preferred + rest
    selected = preferred[:limit]
    remain_count = limit - len(selected)
    if remain_count > 0:
        selected.extend(random.sample(rest, min(remain_count, len(rest))))
    return selected
