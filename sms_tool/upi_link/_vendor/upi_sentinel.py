"""Python wrapper around the vendored OpenAI Sentinel Node bridge.

Why this exists
---------------
The registration runner in :mod:`sms_tool.sentinel` mints the **main** Sentinel
token (``openai-sentinel-token``) but not the signed session-observer token
(``openai-sentinel-so-token``). The ChatGPT ``payments/checkout/approve`` gate
reads the SO header (per the HAR analysis recorded by the ``tilian`` upi-git5
rail: HAR #228 carries SO and its internal ``flow`` is still
``chatgpt_checkout`` — approve reuses the checkout-phase SO). Without it the
endpoint answers ``result=blocked`` forever.

The reference ``upi-zero-link`` project ships a self-contained Node bridge
(``sentinel_bridge.js`` + ``sentinel_bootstrap.js`` + a patched ``sentinel_sdk.js``
+ a ``curl_cffi`` transport helper) whose ``SentinelSDK.__proto2(flow)`` returns
``{t, c, so, ...}`` — both the turnstile proof *and* the SO. This module drives
that bridge over stdin/stdout and exposes a typed Python result.

Transport: when ``undici`` is absent (the vendored tree does not install it) the
bridge delegates HTTP to ``sentinel_curl_fetch.py`` via ``PYTHON_BIN``, which we
point at ``sys.executable`` so the project's ``curl_cffi`` is used. That path is
also what makes ``https://`` proxies work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ASSETS_DIR = Path(__file__).resolve().parent / "sentinel"
BRIDGE_JS = ASSETS_DIR / "sentinel_bridge.js"

#: Sentinel SDK version baked into the vendored ``sentinel_sdk.js``. Overridable
#: via ``UPI_SENTINEL_VERSION`` so a stale SDK can be refreshed without a code
#: change (the bridge uses it for the SDK/frame URL).
DEFAULT_SENTINEL_VERSION = "20260810913b"


def sentinel_version() -> str:
    configured = str(os.environ.get("UPI_SENTINEL_VERSION") or "").strip()
    if configured and all(char.isalnum() or char in {"-", "_"} for char in configured):
        return configured
    return DEFAULT_SENTINEL_VERSION


def mint_sentinel(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    cores: int = 16,
    page_url: str = "https://chatgpt.com/",
    language: str = "en-US",
    timezone: str = "UTC",
    platform: str = "Win32",
    platform_label: str = "Windows",
    screen_w: int = 1920,
    screen_h: int = 1080,
    max_touch_points: int = 0,
    cookie_header: str = "",
    sentinel_origin: str = "https://chatgpt.com",
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Mint a Sentinel token through the vendored bridge.

    Returns ``{"main": str, "so": str, "has_t": bool, "has_so": bool}`` on
    success, or ``{"error": "<code>"}`` on any failure. The caller degrades to
    "no Sentinel header" on error rather than aborting a valid Checkout.
    """
    if not BRIDGE_JS.is_file():
        return {"error": "sentinel_bridge_missing"}
    node = str(os.environ.get("SENTINEL_NODE") or "node").strip() or "node"
    payload = json.dumps(
        {
            "ua": str(user_agent or ""),
            "cores": int(cores or 16),
            "deviceId": str(device_id or ""),
            "flow": str(flow or ""),
            "proxy": str(proxy or ""),
            "version": sentinel_version(),
            "pageUrl": str(page_url or "https://chatgpt.com/"),
            "language": str(language or "en-US"),
            "timezone": str(timezone or "UTC"),
            "platform": str(platform or "Win32"),
            "platformLabel": str(platform_label or "Windows"),
            "screenW": int(screen_w or 1920),
            "screenH": int(screen_h or 1080),
            "availH": max(0, int(screen_h or 1080) - 48),
            "maxTouchPoints": int(max_touch_points or 0),
            "cookieHeader": str(cookie_header or ""),
            "sentinelOrigin": str(sentinel_origin or "https://chatgpt.com"),
        },
        separators=(",", ":"),
    ).encode("utf-8")

    env = dict(os.environ)
    env.setdefault("PYTHON_BIN", sys.executable)
    env.setdefault("SENTINEL_PYTHON", sys.executable)
    try:
        completed = subprocess.run(
            [node, str(BRIDGE_JS)],
            input=payload,
            capture_output=True,
            timeout=float(max(30.0, timeout_s)),
            cwd=str(ASSETS_DIR),
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return {"error": "sentinel_node_missing"}
    except subprocess.TimeoutExpired:
        return {"error": "sentinel_bridge_timeout"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"sentinel_bridge_spawn_failed:{type(exc).__name__}"}

    output = (completed.stdout or b"").decode("utf-8", "replace").strip()
    if not output:
        stderr = (completed.stderr or b"").decode("utf-8", "replace")[:200]
        return {"error": f"sentinel_bridge_empty:{stderr}"}
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return {"error": f"sentinel_bridge_bad_json:{output[:200]}"}
    if not isinstance(data, dict):
        return {"error": "sentinel_bridge_non_object"}
    if data.get("error"):
        return {"error": str(data["error"])[:300]}
    main = str(data.get("main") or "").strip()
    if not main:
        return {"error": "sentinel_bridge_no_main"}
    return {
        "main": main,
        "so": str(data.get("so") or "").strip(),
        "has_t": bool(data.get("hasT")),
        "has_so": bool(data.get("hasSo")),
    }


__all__ = ["mint_sentinel", "sentinel_version", "BRIDGE_JS", "ASSETS_DIR"]
