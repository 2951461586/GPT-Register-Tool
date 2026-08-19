"""Token-free timing and correlation metadata shared across workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any


def access_token_telemetry(token: str, *, acquired_at: int = 0) -> dict[str, Any]:
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
    now = int(time.time())
    iat = _as_int(payload.get("iat"))
    exp = _as_int(payload.get("exp"))
    acquired = _as_int(acquired_at) or iat
    return {
        "token_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else "",
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
