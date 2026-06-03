"""Midtrans Snap request signing helpers.

The Snap web client signs selected API calls with a permuted HMAC-SHA256
signature.  The signing key is runtime data captured from Snap JS and may be
provided by config or by the MIDTRANS_SNAP_SIGNING_KEY environment variable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

ROOT_PATH = "/snap"
DEFAULT_MIDTRANS_CLIENT_KEY = os.environ.get("MIDTRANS_CLIENT_KEY", "")


def permute_signature(signature: str) -> str:
    chars = list(signature)
    i = 0
    while i + 3 < len(chars):
        chars[i], chars[i + 1], chars[i + 2], chars[i + 3] = (
            chars[i + 2],
            chars[i + 3],
            chars[i],
            chars[i + 1],
        )
        i += 4
    return "".join(chars)


def snap_json_body(body: Any) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def _body_string(body: Any) -> str:
    if body is None or body == "":
        return ""
    if isinstance(body, (dict, list)):
        return snap_json_body(body)
    return str(body)


def sign_snap_request(
    path: str,
    body: Any = None,
    *,
    signing_key: str = "",
    timestamp: int | None = None,
) -> dict[str, str]:
    key = (signing_key or os.environ.get("MIDTRANS_SNAP_SIGNING_KEY") or "").strip()
    if not key or key.startswith("CHANGE_ME"):
        return {}
    if not path.startswith("/snap"):
        path = f"{ROOT_PATH}{path}" if path.startswith("/") else f"{ROOT_PATH}/{path}"
    ts = str(int(time.time()) if timestamp is None else timestamp)
    msg = f"{path}:{ts}:{_body_string(body)}"
    raw_sig = hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"X-Snap-Signature": permute_signature(raw_sig), "X-Timestamp": ts}


def midtrans_basic_auth(client_key: str = "") -> dict[str, str]:
    key = (client_key or DEFAULT_MIDTRANS_CLIENT_KEY or "").strip()
    if not key or key.startswith("CHANGE_ME"):
        return {}
    token = base64.b64encode(f"{key}:".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}
