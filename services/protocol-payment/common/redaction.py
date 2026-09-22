"""Log/proxy redaction shared by the protocol-payment extractors.

Batch 2 of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared and what is not
------------------------------
`redact_log_text` and `register_proxy_for_redaction` are byte-identical in all
four extractors and are extracted for all four.

`redact_text` (the extractors' `_redact_text`) is extracted for **ideal and
twint only**.  blik's version additionally strips ``IDEAL_BLIK_CODE`` from the
environment and redacts six-digit ``blik_code`` fields, so it is a different
function with a different contract, not a copy.

Why a registry object
---------------------
Both shared functions read and write the same per-extractor mutable state
(``_proxy_redaction_values`` + ``_proxy_redaction_lock``).  A module-level
globals pair cannot live in a shared module: every extractor would then see and
redact every other extractor's proxies, and worse, one extractor's log output
would start depending on what a different extractor had registered.

So each extractor owns a ``RedactionRegistry`` and passes it in.  The lock
stays inside the registry, mirroring ``common/http_dump.DumpCounter``.

Injected knobs
--------------
``proxy_label``  per-extractor (each delegates to ``common/proxy_bookkeeping``
                 with its own normaliser).
``normalize``    per-extractor proxy URL normaliser.
``limit_env``    ``IDEAL_DUMP_LIMIT`` vs ``TWINT_DUMP_LIMIT``.  No default --
                 see ``file_loading.load_token(env_names=...)`` for the same
                 reasoning.
``env_int``      injected so callers keep their own env parsing.

Rule 10: pure stdlib, no ``sms_tool`` import.
"""

from __future__ import annotations

import hashlib
import re
from threading import RLock
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

__all__ = [
    "RedactionRegistry",
    "redact_log_text",
    "register_proxy_for_redaction",
    "redact_text",
]

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._=-]+")
_SESSION_COOKIE_RE = re.compile(r"(__Secure-next-auth\.session-token=)[^;\\s]+")
_TOKEN_FIELD_RE = re.compile(
    r"(accessToken|access_token|sessionToken|token)(['\"]?\s*[:=]\s*['\"])[^'\"]+"
)


class RedactionRegistry:
    """Per-extractor set of proxy strings to hide from logs.

    Must NOT be shared between extractors: registration is per-extractor and
    the redaction set is what keeps credentials out of that extractor's own
    log/dump output.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = RLock()

    def register(self, values: Iterable[str]) -> None:
        with self._lock:
            self._values.update(values)

    def snapshot(self) -> list[str]:
        """Longest first, so a shorter value cannot shadow a longer one that
        contains it (``host`` vs ``host:port``)."""
        with self._lock:
            return sorted(self._values, key=len, reverse=True)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._values)


def _fallback_label(value: str) -> str:
    return f"proxy#{hashlib.sha256(value.encode()).hexdigest()[:10]}"


def redact_log_text(
    text: str,
    *,
    registry: RedactionRegistry,
    proxy_label: Callable[[str], str],
) -> str:
    """Replace every registered proxy string in ``text`` with a stable label.

    Body copied from the extractors unchanged, including the ``direct``
    special case: ``proxy_label`` returns ``"direct"`` for a proxy-less entry,
    and a literal ``"direct"`` in the output would be both useless and
    confusable with the word itself, so it falls back to a hashed label.
    """
    text = str(text or "")
    values = registry.snapshot()
    for value in values:
        if value:
            try:
                label = proxy_label(value)
            except (TypeError, ValueError):
                label = _fallback_label(value)
            if label == "direct":
                label = _fallback_label(value)
            text = text.replace(value, label)
    return text


def register_proxy_for_redaction(
    proxy: str,
    *,
    registry: RedactionRegistry,
    normalize: Callable[[str], str],
) -> None:
    """Register every spelling of ``proxy`` that could appear in output.

    The raw line, the normalised URL, its percent-decoded form, the netloc and
    the ``host:port`` form are all registered because any of them can show up
    in a log or a dump depending on which layer produced the message.
    """
    raw = str(proxy or "").strip()
    if not raw:
        return
    normalized = normalize(raw)
    values = {raw}
    if normalized:
        values.add(normalized)
        decoded = unquote(normalized)
        values.add(decoded)
        parsed = urlsplit(decoded)
        if parsed.netloc:
            values.add(parsed.netloc)
        if parsed.hostname:
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = parsed.port
            except ValueError:
                port = None
            values.add(f"{host}:{port}" if port else host)
    registry.register(values)


def redact_text(
    text: str,
    limit: int | None = None,
    *,
    registry: RedactionRegistry,
    proxy_label: Callable[[str], str],
    limit_env: str,
    env_int: Callable[[str, int, int], int],
) -> str:
    """Scrub secrets from a dump payload and cap its length.

    ideal/twint shape only -- see the module docstring for why blik is not
    included.  ``limit_env`` is injected because the two read different env
    vars (``IDEAL_DUMP_LIMIT`` / ``TWINT_DUMP_LIMIT``).
    """
    text = text or ""
    text = _BEARER_RE.sub(r"\1***", text)
    text = _SESSION_COOKIE_RE.sub(r"\1***", text)
    text = _TOKEN_FIELD_RE.sub(r"\1\2***", text)
    text = redact_log_text(text, registry=registry, proxy_label=proxy_label)
    if limit is None:
        limit = env_int(limit_env, 6000, minimum=500)
    return text[:limit]
