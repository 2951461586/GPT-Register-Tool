"""QuickJS-driven Sentinel token generator.

Adapted from
https://github.com/zc-zhangchen/any-auto-register
platforms/chatgpt/sentinel_browser.py:`_get_sentinel_token_via_quickjs`
+ scripts/js/openai_sentinel_quickjs.js (MIT License).

Why this exists:
  Pure-Python `sentinel.py` computes a synthetic PoW that *passes* OpenAI's
  surface validation (200 OK on `/sentinel/req`, `/authorize/continue`, etc.)
  but the OTP-dispatch service runs the actual sentinel SDK JS server-side
  to verify the token. Our synthetic token fails the deeper check → email
  silent-drop. To pass, we must run OpenAI's real `sdk.js` (downloaded from
  `sentinel.openai.com/sentinel/<ver>/sdk.js`) inside a JS VM and emit the
  same token the real browser would.

Implementation:
  - Spawn `node -e <wrapper>` per token request
  - Wrapper loads OpenAI's sdk.js + `openai_sentinel_quickjs.js` (a thin
    adapter that exposes `requirements`/`solve` actions over stdin/stdout)
  - Two passes: action=requirements → `request_p`, then `/sentinel/req` →
    challenge, then action=solve → `final_p` + `t`
  - Returns the same JSON-string shape `{p, t, c, id, flow}` as our
    pure-Python `build_sentinel_token`, so callers don't need to change

Public API:
  - `get_sentinel_token_via_quickjs(session, device_id, flow, ...) -> str | None`
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .auth_headers import auth_impersonate
from .http_client import request_with_retry

logger = logging.getLogger(__name__)


SENTINEL_VERSION = "20260219f9f6"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
# Kept as a public compatibility constant for callers that used the original
# standalone implementation.  Runtime URL construction still goes through
# ``sentinel_version()`` so SDK rotation can be configured without a code edit.
SENTINEL_SDK_URL = f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"


def sentinel_version() -> str:
    configured = str(os.getenv("OPENAI_SENTINEL_VERSION", "") or "").strip()
    if not configured:
        try:
            from .config import CFG

            email_cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
            configured = str(email_cfg.get("sentinel_version") or CFG.get("sentinel_version") or "").strip()
        except Exception:
            configured = ""
    if configured and all(char.isalnum() or char in {"-", "_"} for char in configured):
        return configured
    return SENTINEL_VERSION


def _resolve_node_binary() -> str:
    return (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"


def _quickjs_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_quickjs.js"


def sentinel_sdk_url() -> str:
    return f"https://sentinel.openai.com/sentinel/{sentinel_version()}/sdk.js"


def _ensure_sdk_file(session: Any, timeout_ms: int) -> Path:
    """Download OpenAI's actual sdk.js to /tmp cache (one-shot per version)."""
    version = sentinel_version()
    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / version
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    if sdk_file.exists() and sdk_file.stat().st_size > 0:
        try:
            cached = sdk_file.read_bytes()
            if b"SentinelSDK" in cached:
                return sdk_file
        except OSError:
            pass

    resp = request_with_retry(
        session,
        "get",
        sentinel_sdk_url(),
        label="sentinel-sdk",
        headers={
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": "https://auth.openai.com/",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        },
        timeout=max(10, int(timeout_ms / 1000)),
        impersonate=auth_impersonate(),
    )
    status = getattr(resp, "status_code", 0)
    if status != 200:
        hint = ""
        if status in (403, 404):
            hint = (
                f"（Sentinel 版本 {version} 可能已被 OpenAI 轮换失效，"
                "请更新环境变量 OPENAI_SENTINEL_VERSION 或 config 的 sentinel_version）"
            )
        raise RuntimeError(f"下载 sdk.js 失败: HTTP {status}{hint}")
    content = getattr(resp, "content", b"") or (resp.text or "").encode()
    if not content:
        raise RuntimeError("下载 sdk.js 失败: 响应为空")
    if b"SentinelSDK" not in content:
        raise RuntimeError("下载 sdk.js 失败: 响应不是 Sentinel SDK")
    # A batch can start several workers at once.  Publish only a complete SDK
    # file so a reader never evaluates a partially-written JavaScript bundle.
    tmp_file = sdk_file.with_name(f"{sdk_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_file.write_bytes(content)
        tmp_file.replace(sdk_file)
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass
    return sdk_file


_WRAPPER_JS = """
const fs = require('fs');
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || '10000');
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const scriptFile = process.env.OPENAI_SENTINEL_QUICKJS_SCRIPT;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const payload = JSON.parse(input || '{}');
    if (payload.timezone_name) process.env.TZ = String(payload.timezone_name);
    globalThis.__payload_json = JSON.stringify(payload);
    globalThis.__sdk_source = fs.readFileSync(sdkFile, 'utf8');
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';
    const script = fs.readFileSync(scriptFile, 'utf8');
    eval(script);

    const started = Date.now();
    while (!globalThis.__vm_done) {
      if ((Date.now() - started) > timeoutMs) {
        throw new Error('QuickJS script timeout');
      }
      await new Promise((resolve) => setTimeout(resolve, 1));
    }

    if (String(globalThis.__vm_error || '').trim()) {
      throw new Error(String(globalThis.__vm_error));
    }

    process.stdout.write(String(globalThis.__vm_output_json || ''));
  } catch (err) {
    const msg = err && err.stack ? String(err.stack) : String(err);
    process.stderr.write(msg);
    process.exit(1);
  }
});
""".strip()


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    quickjs_script: Path,
    payload: dict,
    timeout_ms: int,
) -> dict:
    body = dict(payload)
    body["action"] = action
    if not sdk_file.exists() or sdk_file.stat().st_size <= 0:
        raise RuntimeError("QuickJS SDK 文件不存在或为空")
    if not quickjs_script.exists() or quickjs_script.stat().st_size <= 0:
        raise RuntimeError("QuickJS wrapper 文件不存在或为空")
    proc = subprocess.run(
        [_resolve_node_binary(), "-e", _WRAPPER_JS],
        input=json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=max(10, int(timeout_ms / 1000) + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            "OPENAI_SENTINEL_QUICKJS_SCRIPT": str(quickjs_script),
            "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(min(timeout_ms, 30000)),
        },
    )
    if proc.returncode != 0:
        raise RuntimeError(f"QuickJS 执行失败: {(proc.stderr or proc.stdout or 'unknown').strip()[:300]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("QuickJS 返回空输出")
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("QuickJS 输出不是 JSON 对象")
    return data


def _fetch_sentinel_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    timeout_ms: int,
) -> dict:
    body = {"p": request_p, "id": device_id, "flow": flow}
    resp = request_with_retry(
        session,
        "post",
        SENTINEL_REQ_URL,
        label="sentinel-req",
        data=json.dumps(body, separators=(",", ":")),
        headers={
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={sentinel_version()}",
            "content-type": "text/plain;charset=UTF-8",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "zh-CN,zh;q=0.9",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        },
        timeout=max(10, int(timeout_ms / 1000)),
        impersonate=auth_impersonate(),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"/sentinel/req HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Sentinel challenge 响应不是 JSON 对象")
    return payload


