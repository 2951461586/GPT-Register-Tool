"""Provider-neutral proxy-URL normalization shared by protocol-payment extractors.

**Process boundary (architecture.md Rule 10).**  This module must NOT import
``sms_tool``.  The services tree runs as a child process and talks to the host
through argv/env only; the host-side single authority is
``sms_tool/proxy_entry.py`` and the two are deliberately independent.

Why this module exists
----------------------
Until 2026-09-17 seven extractors each carried a private ``normalize_proxy_url``.
A differential scan (``runtime/tmp/p0_proxy_diff.py``, 37 inputs x 7 modules)
showed they disagreed on **25 of 37 inputs**, and the disagreements were not
cosmetic:

* **Two mutually exclusive four-field orders.**  ``proxy_entry.parse_proxy``
  (the host authority) reads the bare provider form as ``host:port:user:pass``.
  ``kakao`` / ``direct_card`` / ``ac_paylink_core`` agree; ``blik`` reads it as
  ``user:pass:host:port``.
* **Three failure policies.**  ``ideal`` / ``twint`` / ``blik`` pass the input
  through, ``kakao`` returns ``""``, ``ac_paylink_core`` raises.
* **Two userinfo policies.**  ``blik`` / ``ideal`` / ``twint`` / ``kakao``
  re-encode (percent-encode) the credentials; ``direct_card`` /
  ``ac_paylink_core`` return a scheme-bearing URL untouched.
* **Two default-scheme policies.**  ``blik`` / ``ideal`` / ``twint`` honour
  ``https`` from the environment; ``kakao`` collapses it to ``http``.

None of that was documented or tested.  This module makes each difference an
**explicit, named parameter** so that "these two extractors behave differently"
is an auditable decision rather than an accident of copy-paste.  It does not
attempt to unify behaviour: unifying is a separate, evidence-requiring change.

The two skeleton shapes
-----------------------
``normalize_proxy_url`` is the full skeleton used by ``blik`` / ``ideal`` /
``twint`` / ``kakao``: parse, refuse-or-return, then rebuild the netloc with
re-encoded userinfo and IPv6 bracketing.

``normalize_provider_form`` is the lighter skeleton used by ``direct_card`` and
``ac_paylink_core``: return a scheme-bearing URL untouched, otherwise accept
only the bare four-field provider form.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit


__all__ = [
    "default_scheme_from_env",
    "normalize_proxy_url",
    "normalize_provider_form",
]

# The four-field orders.  ``host_first`` is the ``proxy_entry`` authority
# (``host:port:user:pass``); ``user_first`` is blik's inverse convention.
HOST_FIRST = "host_first"
USER_FIRST = "user_first"
NO_FOUR_PART = "none"


def default_scheme_from_env(
    variable: str,
    fallback: str = "http",
    *,
    allow_https: bool = True,
) -> str:
    """Resolve a bare proxy's default scheme from ``variable``.

    Four extractors each had this function; they differed only in the
    environment variable name and in whether ``https`` survives.

    ``allow_https=False`` reproduces ``kakao``'s behaviour of collapsing
    ``https`` to ``http``.  That is *not* a typo to be "fixed" here: kakao's
    transport is plain HTTP CONNECT, and changing it silently would alter the
    egress scheme.  It stays a parameter until someone measures it.
    """
    raw = os.environ.get(variable, fallback).strip().lower()
    if raw.endswith("://"):
        raw = raw[:-3]
    if raw in ("socks5", "socks5h"):
        return "socks5h"
    if raw in ("http", "https"):
        return raw if (allow_https or raw == "http") else "http"
    return "http"


def _encode_userinfo(value: str, safe: str) -> str:
    """``quote(unquote(value))`` -- decode any existing escapes, then re-encode.

    Idempotent for the credentials this codebase sees, and the reason a
    password containing a literal ``@`` or a space survives a round trip.
    """
    return quote(unquote(value), safe=safe)


def _bracket_if_ipv6(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _apply_four_part(text: str, order: str, scheme: str) -> str:
    """Rewrite the bare four-field provider form into ``scheme://user:pass@host:port``.

    The two orders carry **different validity predicates**, copied verbatim from
    the extractors they came from, because those predicates are load-bearing:

    * ``host_first`` (kakao): ``text.count(":") == 3 and "@" not in text``.
    * ``user_first`` (blik): ``split(":")`` yields 4 parts where ``parts[3]``
      is all digits and ``parts[1]`` is not.

    The blik predicate misfires on IPv6 literals such as ``[::1]:8080``
    (``split(":")`` gives ``['[', '', '1]', '8080']``), which then raises from
    ``urlsplit``.  That behaviour is preserved on purpose: fixing it changes
    what a production payment extractor does with a real input, so it needs its
    own decision and its own evidence, not a drive-by edit inside a refactor.
    """
    if order == USER_FIRST:
        parts = text.split(":")
        if len(parts) == 4 and parts[3].isdigit() and not parts[1].isdigit():
            username, password, hostname, port = parts
            return (
                f"{scheme}://{_encode_userinfo(username, '-._~')}:"
                f"{_encode_userinfo(password, '-._~')}@{hostname}:{port}"
            )
        return f"{scheme}://{text}"

    if order == HOST_FIRST:
        if text.count(":") == 3 and "@" not in text:
            host, port, username, password = text.split(":", 3)
            return f"{scheme}://{username}:{password}@{host}:{port}"
        return f"{scheme}://{text}"

    return f"{scheme}://{text}"


def normalize_proxy_url(
    raw: Any,
    *,
    default_scheme: str = "http",
    four_part: str = NO_FOUR_PART,
    no_auth: str = "return_text",
    require_hostname: bool = False,
    on_error: str = "propagate",
) -> str:
    """Full-skeleton normalization: re-encode userinfo, bracket IPv6.

    Parameters
    ----------
    default_scheme:
        Scheme applied to a URL that carries none.
    four_part:
        ``HOST_FIRST`` / ``USER_FIRST`` to accept the bare provider form,
        ``NO_FOUR_PART`` to treat it as a plain host and just prefix a scheme.
    no_auth:
        ``"return_text"`` returns the text as soon as the parse shows no
        credentials (``blik`` / ``ideal`` / ``twint``).  ``"rebuild"`` always
        reconstructs the netloc (``kakao``).
    require_hostname:
        Return ``""`` when the parsed URL has no scheme or no host, instead of
        rebuilding a netloc from whatever was there (``kakao``).
    on_error:
        ``"propagate"`` lets ``urlsplit`` / ``.port`` failures escape
        (``blik`` / ``ideal`` / ``twint``); ``"empty"`` converts them to ``""``
        (``kakao``).
    """
    text = str(raw or "").strip()
    if not text:
        return ""

    try:
        if "://" not in text:
            text = _apply_four_part(text, four_part, default_scheme)

        parsed = urlsplit(text)

        if require_hostname and (not parsed.scheme or not parsed.hostname):
            return ""

        if no_auth == "return_text" and parsed.username is None and parsed.password is None:
            return text

        hostname = parsed.hostname or ""
        host = _bracket_if_ipv6(hostname)
        if parsed.port:
            host = f"{host}:{parsed.port}"

        username = _encode_userinfo(parsed.username or "", "-._~")
        auth = username
        if parsed.password is not None:
            auth = f"{auth}:{_encode_userinfo(parsed.password, '-._~')}"

        if no_auth == "rebuild":
            netloc = f"{auth}@{host}" if auth else host
        else:
            netloc = f"{auth}@{host}"

        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        if on_error == "empty":
            return ""
        raise


def normalize_provider_form(
    raw: Any,
    *,
    default_scheme: str = "http",
    require_numeric_port: bool = False,
    require_no_at: bool = False,
    on_unparseable: str = "prefix",
    unparseable_error: Any = None,
    quote_safe: str = "-._~",
) -> str:
    """Light-skeleton normalization for the ``direct_card`` / ``ac_paylink_core`` pair.

    Differences encoded as parameters rather than duplicated bodies:

    * ``require_numeric_port`` -- ``direct_card`` refuses to read a four-field
      value whose second field is not all digits; ``ac_paylink_core`` accepts
      anything.
    * ``require_no_at`` -- ``direct_card`` additionally requires the raw text to
      contain no ``@``.  Without it, ``"u@h:1:2:3"`` would be read as
      ``user=2, password=3, host=u@h, port=1``.  ``ac_paylink_core`` has no such
      guard.
    * ``on_unparseable`` -- ``"prefix"`` returns ``scheme://`` + the original
      text; ``"raise"`` raises.
    * ``unparseable_error`` -- zero-argument factory used when
      ``on_unparseable="raise"``.  ``ac_paylink_core`` raises its own
      ``PaylinkError`` with a fixed message; injecting the factory keeps this
      module free of any dependency on that provider's exception type.
    * ``quote_safe`` -- ``ac_paylink_core`` encodes with ``safe=""`` (a literal
      ``-`` or ``.`` in a credential gets percent-encoded), ``direct_card``
      keeps ``-._~`` literal.

    A scheme-bearing input is returned **untouched** by both -- no re-encoding.
    That is the observable difference from :func:`normalize_proxy_url`, and the
    reason the two skeletons are not merged.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text

    parts = text.split(":", 3)
    qualifies = (
        len(parts) == 4
        and (not require_numeric_port or parts[1].isdigit())
        and (not require_no_at or "@" not in text)
    )
    if qualifies:
        host, port, username, password = parts
        return (
            f"{default_scheme}://{quote(username, safe=quote_safe)}:"
            f"{quote(password, safe=quote_safe)}@{host}:{port}"
        )

    if on_unparseable == "raise":
        if unparseable_error is not None:
            raise unparseable_error()
        raise ValueError(f"proxy must be URL or host:port:user:pass, got {text!r}")
    return f"{default_scheme}://{text}"
