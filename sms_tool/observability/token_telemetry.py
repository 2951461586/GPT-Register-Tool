"""Token-free timing and correlation metadata shared across workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from .diagnostics import load_native_module

_NATIVE_TOKEN_TELEMETRY = load_native_module(
    "token-telemetry-rs",
    "token_telemetry_rs",
    "RUST_ANYTHING_UNIT_TOKEN_TELEMETRY",
)
_NATIVE_PARSE_TOKEN_CLAIMS_AND_HASH = getattr(_NATIVE_TOKEN_TELEMETRY, "_parse_token_claims_and_hash", None)


def _parse_token_claims_and_hash(token: str | None) -> tuple[str, int, int]:
    if _NATIVE_PARSE_TOKEN_CLAIMS_AND_HASH is not None and isinstance(token, (str, type(None))):
        token_hash, iat, exp = _NATIVE_PARSE_TOKEN_CLAIMS_AND_HASH(token)
        return str(token_hash), int(iat), int(exp)
    text = str(token or "").strip()
    payload: dict[str, Any] = {}
    parts = text.split(".")
    if len(parts) >= 2:
        try:
            raw = parts[1] + "=" * (-len(parts[1]) % 4)
            parsed = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
            if isinstance(parsed, dict):
                payload = parsed
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            payload = {}
    iat = _as_int(payload.get("iat"))
    exp = _as_int(payload.get("exp"))
    token_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""
    return token_hash, iat, exp


def access_token_telemetry(token: str | None, *, acquired_at: int = 0) -> dict[str, Any]:
    """``token`` is ``str | None``: the body already normalises with ``str(token or "")``
    and ``tests/accounts/test_token_telemetry_pure.py`` pins that ``None`` and ``""``
    both yield an empty ``token_hash``. The narrower ``str`` annotation only made
    that test untypeable."""
    token_hash, iat, exp = _parse_token_claims_and_hash(token)
    now = int(time.time())
    acquired = _as_int(acquired_at) or iat
    return {
        "token_hash": token_hash,
        "iat": iat,
        "exp": exp,
        "lifetime_seconds": max(0, exp - iat) if iat and exp else 0,
        "age_seconds": max(0, now - acquired) if acquired else 0,
        "expires_in_seconds": exp - now if exp else 0,
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
