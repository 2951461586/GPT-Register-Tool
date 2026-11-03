"""Account identity plumbing for the UPI lane.

Why this module exists
----------------------
``generate_upi_qr_link(..., auth_context=...)`` has been part of
``UPI_CALL_OPTIONS`` and forwarded by every entry point (CLI, ``pay_link``
``native_upi`` adapter, payment batch) since the UPI lane was introduced, but no
stage ever read it: ``pipeline._resolve_upi_runtime`` minted ``uuid.uuid4()`` for
the device id and left ``session_token`` empty.  Every run therefore presented a
**brand-new device** plus a synthetic ``Cookie: oai-did=<uuid>``.

That is a measured contradiction, not a cosmetic one:

* ``payment/qualification.py:_device_id_and_cookie`` records the 2026-10-27
  finding -- sending only ``oai-did=<id>`` while the access token belongs to a
  real browser session answers ``400 Our systems have detected unusual
  activity``.  A Checkout is a same-site action: it needs the session cookies
  the token was issued with.
* The Sentinel pair is device-bound, so the ``Cookie`` header and
  ``OAI-Device-Id`` must agree (``session._upi_apply_chatgpt_identity`` writes
  both from the same value).

The canonical ``auth_context`` shape is the one
``payment/qualification.py:_auth_context`` already produces::

    {"device_id": "...", "oai_did": "...", "cookie_header": "..."}

This module only **reads** that shape (plus the account/session keys the callers
happen to carry).  It never invents credentials: an absent cookie stays absent
so the caller's behaviour is unchanged, and the caller keeps the documented
``uuid4`` fallback until a sticky identity store exists.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any, NamedTuple

#: ``auth_context`` keys that carry a device id, most authoritative first.
#: ``device_id``/``oai_did`` are what ``payment/qualification.py:_auth_context``
#: writes; the remaining spellings are what account payloads use.
UPI_DEVICE_ID_KEYS: tuple[str, ...] = (
    "device_id",
    "oai_did",
    "oai_device_id",
    "oai-device-id",
)

#: ``auth_context`` keys that carry the ChatGPT session credential.
UPI_SESSION_TOKEN_KEYS: tuple[str, ...] = (
    "session_token",
    "sessionToken",
    "next_auth_session_token",
    "__Secure-next-auth.session-token",
)

#: ``auth_context`` keys that carry a full cookie header (string or mapping).
UPI_COOKIE_HEADER_KEYS: tuple[str, ...] = (
    "cookie_header",
    "cookie",
    "cookies",
)

#: ``auth_context`` keys that identify the account (used for logging + the
#: sticky identity store added later; never a credential).
UPI_ACCOUNT_ID_KEYS: tuple[str, ...] = (
    "account_id",
    "account_ref",
    "email",
)

#: Cookie names the UPI lane must agree on.
COOKIE_DEVICE_ID = "oai-did"
COOKIE_SESSION_TOKEN = "__Secure-next-auth.session-token"


class AccountIdentity(NamedTuple):
    """The identity one UPI run presents to ChatGPT / Stripe."""

    device_id: str = ""
    session_token: str = ""
    cookie_header: str = ""
    account_id: str = ""

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie_header)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def cookie_pairs(value: Any) -> list[tuple[str, str]]:
    """Normalise a cookie header string **or** mapping into ``(name, value)``.

    A ``requests``/``curl_cffi`` cookie jar is a mapping, an account payload
    stores a header string, and both reach this function, so neither shape may
    raise.
    """
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [(_text(k), _text(v)) for k, v in value.items() if _text(k)]
    if isinstance(value, (list, tuple)):
        pairs: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, Mapping):
                name = _text(item.get("name"))
                if name:
                    pairs.append((name, _text(item.get("value"))))
            else:
                pairs.extend(cookie_pairs(item))
        return pairs
    pairs = []
    for part in _text(value).split(";"):
        name, _, cookie_value = part.partition("=")
        name = name.strip()
        if name:
            pairs.append((name, cookie_value.strip().strip('"')))
    return pairs


def cookie_value(value: Any, name: str) -> str:
    """Return one cookie's value from a header string or mapping (``""`` if absent)."""
    wanted = _text(name).lower()
    if not wanted:
        return ""
    for key, item in cookie_pairs(value):
        if key.lower() == wanted:
            return item
    return ""


def cookie_header_text(value: Any) -> str:
    """Serialise either cookie shape back to a ``; ``-joined header."""
    return "; ".join(f"{name}={item}" for name, item in cookie_pairs(value) if name)


def session_token_from_cookie(value: Any) -> str:
    """Read the ChatGPT session token from a cookie header, chunk-aware.

    next-auth splits a >4096-byte session token into
    ``__Secure-next-auth.session-token.0`` / ``.1`` / ... (measured on real
    accounts: two chunks).  Matching only the unchunked name finds nothing and
    made the lane believe it had no session at all, so the parts are joined in
    numeric order.
    """
    exact = cookie_value(value, COOKIE_SESSION_TOKEN)
    if exact:
        return exact
    parts: list[tuple[int, str]] = []
    prefix = f"{COOKIE_SESSION_TOKEN}."
    for name, item in cookie_pairs(value):
        if not name.startswith(prefix):
            continue
        index = name[len(prefix) :]
        if not index.isdigit():
            continue
        parts.append((int(index), item))
    if not parts:
        return ""
    parts.sort(key=lambda pair: pair[0])
    return "".join(item for _index, item in parts)


def cookie_carries_session_token(value: Any) -> bool:
    """True when ``value`` already holds the session token (plain or chunked)."""
    return bool(session_token_from_cookie(value))


def with_cookie(value: Any, name: str, cookie_value_: str) -> str:
    """Return a header with ``name`` set to ``cookie_value_``, keeping the rest.

    Order is preserved and an existing entry is replaced in place, so the
    resulting header reads like the account's own rather than a rebuilt one.
    """
    wanted = _text(name)
    replacement = _text(cookie_value_)
    if not wanted or not replacement:
        return cookie_header_text(value)
    pairs = cookie_pairs(value)
    lowered = wanted.lower()
    out: list[tuple[str, str]] = []
    replaced = False
    for key, item in pairs:
        if key.lower() == lowered:
            if not replaced:
                out.append((key, replacement))
                replaced = True
            continue
        out.append((key, item))
    if not replaced:
        out.append((wanted, replacement))
    return "; ".join(f"{key}={item}" for key, item in out)


def _first_text(*candidates: Any) -> str:
    for candidate in candidates:
        text = _text(candidate)
        if text:
            return text
    return ""


def _from_context(context: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text(context.get(key))
        if text:
            return text
    return ""


def token_claims(token: Any) -> dict[str, Any]:
    """Decode a ChatGPT access token's payload **without** verifying it.

    The token is ours and already trusted by the caller; this only reads the
    claims to derive a *stable* account key, so a run can find the identity it
    used last time.  Any malformed input returns ``{}`` -- identity resolution
    must never be the reason a run dies.
    """
    parts = _text(token).split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, UnicodeDecodeError):
        # A truncated/hand-edited token: identity resolution must never raise.
        return {}
    return decoded if isinstance(decoded, dict) else {}


def token_account_id(token: Any) -> str:
    """``chatgpt_account_id`` from the token, or ``""``.

    Kept identical to ``session._upi_account_id_from_token`` (which now
    delegates here) because the value is also sent in risk headers -- adding
    fallbacks here would change what those headers carry.
    """
    claims = token_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    nested = auth if isinstance(auth, Mapping) else {}
    return _text(nested.get("chatgpt_account_id") or claims.get("chatgpt_account_id"))


def resolve_account_identity(
    auth_context: Mapping[str, Any] | None = None,
    *,
    device_id: Any = "",
    session_token: Any = "",
    cookie_header: Any = "",
) -> AccountIdentity:
    """Merge explicit arguments with ``auth_context`` into one identity.

    Precedence is *cookie evidence → explicit argument → ``auth_context`` key →
    cookie*, mirroring ``payment/qualification.py:_device_id_and_cookie`` (which
    reads ``oai-did`` out of the account's own cookie *before* the context).  The
    cookie wins because the Sentinel pair is bound to it: a caller passing a
    different ``device_id`` next to a real browser cookie is describing a stale
    account payload, and inventing a header state the account never had is the
    one thing that cannot be recovered from.

    Invariants kept on the way out:

    * ``device_id`` equals the cookie's ``oai-did`` when both exist (the Sentinel
      pair is device-bound; a mismatch is a self-inflicted 400).
    * ``session_token`` is empty when the cookie already carries one (plain or
      chunked), because callers append it to that same header.
    * no credential is invented -- absent stays absent.
    """
    context: Mapping[str, Any] = auth_context if isinstance(auth_context, Mapping) else {}
    context_cookie = _from_context(context, UPI_COOKIE_HEADER_KEYS)

    raw_cookie = _first_text(cookie_header, context_cookie)
    cookie_device = cookie_value(raw_cookie, COOKIE_DEVICE_ID)
    device = _first_text(
        cookie_device,
        device_id,
        _from_context(context, UPI_DEVICE_ID_KEYS),
    )

    # ``session_token`` is only populated when the cookie does **not** already
    # carry it: callers append it to the Cookie header, and appending a second
    # copy next to the chunked parts (``...session-token.0/.1``) would present a
    # duplicate, oversized credential.
    cookie_token = session_token_from_cookie(raw_cookie)
    token = _first_text(
        session_token,
        _from_context(context, UPI_SESSION_TOKEN_KEYS),
    )

    if device and raw_cookie:
        # Keep the header and the device id in agreement. ``with_cookie`` is a
        # no-op when they already match, so a verbatim account header survives.
        raw_cookie = with_cookie(raw_cookie, COOKIE_DEVICE_ID, device)
    if token and raw_cookie and not cookie_token:
        raw_cookie = with_cookie(raw_cookie, COOKIE_SESSION_TOKEN, token)

    return AccountIdentity(
        device_id=device,
        session_token=token,
        cookie_header=raw_cookie,
        account_id=_from_context(context, UPI_ACCOUNT_ID_KEYS),
    )


__all__ = [
    "AccountIdentity",
    "COOKIE_DEVICE_ID",
    "COOKIE_SESSION_TOKEN",
    "UPI_ACCOUNT_ID_KEYS",
    "UPI_COOKIE_HEADER_KEYS",
    "UPI_DEVICE_ID_KEYS",
    "UPI_SESSION_TOKEN_KEYS",
    "cookie_carries_session_token",
    "cookie_header_text",
    "cookie_pairs",
    "cookie_value",
    "resolve_account_identity",
    "session_token_from_cookie",
    "token_account_id",
    "token_claims",
    "with_cookie",
]
