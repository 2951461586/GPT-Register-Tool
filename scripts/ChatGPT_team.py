#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT_team.py
================

Standalone single-file script for:

1. registering/logging into ChatGPT Web through the current enterprise SSO flow
2. completing Codex OAuth
3. saving the first usable Codex refresh token

This file is intentionally self-contained:

- no dependency on `chatgpt_register.py`
- no dependency on `codex_login_extract.py`
- no dependency on `hero_sms.py`
- no stock-upload / merchant-sync logic

Install
-------

```bash
python -m pip install --upgrade pip
python -m pip install curl_cffi
```

Minimal config
--------------

Create `ChatGPT_team.config.local.json` next to this file:

```json
{
  "proxy": "http://127.0.0.1:7890"
}
```

If you do not need a proxy, you can omit the file and run directly.

Usage
-----

Register and save RT:

```bash
python ChatGPT_team.py --total 1 --workers 1
```

Check existing `codex_tokens/*.json` plan type using current `access_token`:

```bash
python ChatGPT_team.py --check-tokens
```

When `--check-tokens` sees a `401`, it will:

1. load the matching local session from `chatgpt_sessions/`
2. run Codex OAuth again
3. overwrite the original token file with the fresh RT/AT/ID token

Outputs
-------

- `registered_only.txt`
- `register_only_failed.txt`
- `chatgpt_sessions/`
- `codex_tokens/`
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import random
import re
import secrets
import string
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from curl_cffi import requests as curl_requests

try:
    from curl_cffi.const import CurlHttpVersion
except Exception:
    CurlHttpVersion = None


CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
OAUTH_ORIGINATOR = "codex_vscode"
EMAIL_DOMAIN = "@gpt.edu.sixoner.com"
REGISTERED_OUTPUT_FILE = "registered_only.txt"
FAILED_OUTPUT_FILE = "register_only_failed.txt"
CHATGPT_SESSION_DIR = "chatgpt_sessions"
CODEX_TOKEN_DIR = "codex_tokens"
DEFAULT_ONBOARDING_ROLE = "engineering"

_CONFIG_CANDIDATES = (
    "ChatGPT_team.config.local.json",
    "ChatGPT_team.config.json",
)

_print_lock = threading.Lock()
_file_lock = threading.Lock()


def _load_proxy_from_config() -> str:
    root = Path(__file__).resolve().parent
    for candidate in _CONFIG_CANDIDATES:
        path = root / candidate
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        proxy = str(data.get("proxy") or "").strip()
        if proxy:
            return proxy
    return ""


DEFAULT_PROXY = os.environ.get("PROXY", _load_proxy_from_config()).strip()


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _short(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _print_pipe(level: str, tag: str, message: str) -> None:
    with _print_lock:
        print(f"{_ts()} | {level:<6} | {tag:<12} | {message}", flush=True)


def _set_progress_title(*, total: int, submitted: int, success: int, fail: int, active: int, waiting: bool = False) -> None:
    title = f"ChatGPT_team 运行:{active} 成功:{success} 失败:{fail} 进度:{submitted}/{total}"
    if waiting:
        title += " [等待结束]"
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _safe_cache_name(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", str(email or "").strip().lower())


def _cwd_path(name: str) -> Path:
    path = Path(str(name or "").strip())
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _chatgpt_session_path(email: str) -> Path:
    return _cwd_path(CHATGPT_SESSION_DIR) / f"{_safe_cache_name(email)}.json"


def _codex_token_path(email: str) -> Path:
    return _cwd_path(CODEX_TOKEN_DIR) / f"{email}.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock, path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def _make_trace_headers() -> dict[str, str]:
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    traceparent = f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01"
    return {
        "traceparent": traceparent,
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id),
        "x-datadog-parent-id": str(parent_id),
    }


def generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def extract_code_from_url(url: str | None) -> str | None:
    if not url or "code=" not in url:
        return None
    try:
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


def _decode_auth_session_cookie(raw_value: str) -> dict[str, Any]:
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return {}
    for candidate in (raw_value, unquote(raw_value)):
        try:
            if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
                candidate = candidate[1:-1]
            payload = candidate.split(".", 1)[0]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _extract_first_form(html: str) -> tuple[str, dict[str, str]]:
    html = str(html or "")
    match = re.search(r"<form[^>]+action=\"([^\"]+)\"[^>]*>(.*?)</form>", html, re.I | re.S)
    if not match:
        return "", {}
    action = match.group(1)
    inner = match.group(2)
    fields: dict[str, str] = {}
    for name, value in re.findall(r'name=\"([^\"]+)\"(?:[^>]*value=\"([^\"]*)\")?', inner, re.I | re.S):
        fields[str(name)] = str(value or "")
    return action, fields


def _extract_plan_label(value: object) -> str:
    labels: list[str] = []

    def walk(obj: object, path: str = "") -> None:
        if len(labels) >= 6:
            return
        if isinstance(obj, dict):
            for key, item in obj.items():
                key_s = str(key or "")
                low = key_s.lower()
                next_path = f"{path}.{key_s}" if path else key_s
                if any(marker in low for marker in ("plan", "tier", "subscription", "account_type", "sku")):
                    if not isinstance(item, (dict, list)):
                        text = str(item or "").strip()
                        if text:
                            labels.append(f"{next_path}={text}")
                walk(item, next_path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:20]):
                walk(item, f"{path}[{idx}]")

    walk(value)
    return "; ".join(labels[:6]) or "-"


def _random_name() -> str:
    first_names = [
        "Ava", "Mia", "Ethan", "James", "Lucas", "Noah", "Grace", "Emma",
        "Olivia", "Mason", "Liam", "Sophia", "Amelia", "Harper", "Evelyn",
    ]
    last_names = [
        "Smith", "Johnson", "Taylor", "Martin", "Brown", "Garcia", "Young",
        "Hall", "Allen", "King", "Scott", "Lee", "Wright", "Walker", "Hill",
    ]
    return f"{random.choice(first_names)} {random.choice(last_names)}"


def _random_birthdate() -> str:
    year = random.randint(1988, 2003)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(random.choice(alphabet) for _ in range(13))
        if any(ch.islower() for ch in password) and any(ch.isupper() for ch in password) and any(ch.isdigit() for ch in password):
            return password


_SCREEN_SIZES = ["1366x768", "1440x900", "1536x864", "1920x1080", "1920x1200"]
_HARDWARE_CONCURRENCY = [8, 12, 16]


def _navigator_languages_from_accept(accept_language: str) -> str:
    langs = [part.split(";", 1)[0].strip() for part in accept_language.split(",") if part.strip()]
    return ",".join(langs[:3]) or "en-US,en"


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(
        self,
        device_id: str | None = None,
        user_agent: str | None = None,
        *,
        screen_size: str | None = None,
        primary_language: str | None = None,
        accept_language: str | None = None,
        hardware_concurrency: int | None = None,
    ):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        )
        self.screen_size = screen_size or random.choice(_SCREEN_SIZES)
        self.primary_language = primary_language or "en-US"
        self.accept_language = _navigator_languages_from_accept(accept_language or "en-US,en;q=0.9")
        self.hardware_concurrency = hardware_concurrency or random.choice(_HARDWARE_CONCURRENCY)
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list[Any]:
        now_str = time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice([
            "vendorSub", "productSub", "vendor", "maxTouchPoints", "scheduling",
            "userActivation", "doNotTrack", "geolocation", "connection", "plugins",
            "mimeTypes", "pdfViewerEnabled", "webkitTemporaryStorage",
            "webkitPersistentStorage", "hardwareConcurrency", "cookieEnabled",
            "credentials", "mediaDevices", "permissions", "locks", "ink",
        ])
        return [
            self.screen_size,
            now_str,
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            self.primary_language,
            self.accept_language,
            random.random(),
            f"{nav_prop}-undefined",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            self.hardware_concurrency,
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data: Any) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time: float, seed: str, difficulty: str, config: list[Any], nonce: int) -> str | None:
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        if hash_hex[: len(difficulty)] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, seed: str | None = None, difficulty: str | None = None) -> str:
        seed = seed if seed is not None else self.requirements_seed
        difficulty = str(difficulty or "0")
        start_time = time.time()
        config = self._get_config()
        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start_time, seed, difficulty, config, i)
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def fetch_sentinel_challenge(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
    screen_size: str | None = None,
    primary_language: str | None = None,
    accept_language: str | None = None,
    hardware_concurrency: int | None = None,
) -> dict[str, Any] | None:
    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        screen_size=screen_size,
        primary_language=primary_language,
        accept_language=accept_language,
        hardware_concurrency=hardware_concurrency,
    )
    body = {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
    }
    if not impersonate:
        headers["User-Agent"] = user_agent or "Mozilla/5.0"
        if sec_ch_ua:
            headers["sec-ch-ua"] = sec_ch_ua
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = '"Windows"'
    kwargs: dict[str, Any] = {"data": json.dumps(body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        resp = session.post("https://sentinel.openai.com/backend-api/sentinel/req", **kwargs)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def build_sentinel_token(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
    screen_size: str | None = None,
    primary_language: str | None = None,
    accept_language: str | None = None,
    hardware_concurrency: int | None = None,
) -> str | None:
    challenge = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
        screen_size=screen_size,
        primary_language=primary_language,
        accept_language=accept_language,
        hardware_concurrency=hardware_concurrency,
    )
    if not challenge:
        return None
    c_value = challenge.get("token", "")
    if not c_value:
        return None
    pow_data = challenge.get("proofofwork") or {}
    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        screen_size=screen_size,
        primary_language=primary_language,
        accept_language=accept_language,
        hardware_concurrency=hardware_concurrency,
    )
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(seed=pow_data.get("seed"), difficulty=pow_data.get("difficulty", "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )


@dataclass
class FingerprintProfile:
    impersonate: str
    user_agent: str
    sec_ch_ua: str
    accept_language: str
    primary_language: str
    screen_size: str
    hardware_concurrency: int


_FINGERPRINT_PROFILES = [
    FingerprintProfile(
        impersonate="chrome142",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="99"',
        accept_language="en-US,en;q=0.9",
        primary_language="en-US",
        screen_size="1440x900",
        hardware_concurrency=8,
    ),
    FingerprintProfile(
        impersonate="chrome131",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="99"',
        accept_language="en-GB,en;q=0.9",
        primary_language="en-GB",
        screen_size="1920x1080",
        hardware_concurrency=12,
    ),
    FingerprintProfile(
        impersonate="chrome",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="99"',
        accept_language="en-US,en;q=0.9",
        primary_language="en-US",
        screen_size="1536x864",
        hardware_concurrency=8,
    ),
]


class ChatGPTRegister:
    BASE = CHATGPT_BASE
    AUTH = AUTH_BASE

    def __init__(self, proxy: str | None = None, tag: str = ""):
        self.tag = str(tag or "")
        self.proxy = str(proxy or "").strip() or None
        self.device_id = str(uuid.uuid4())
        self.auth_session_logging_id = str(uuid.uuid4())
        self._callback_url = ""
        self._codex_code_verifier = ""
        self._codex_state = ""

        self.fingerprint = random.choice(_FINGERPRINT_PROFILES)
        self.impersonate = self.fingerprint.impersonate
        self.ua = self.fingerprint.user_agent
        self.sec_ch_ua = self.fingerprint.sec_ch_ua
        self.accept_language = self.fingerprint.accept_language
        self.primary_language = self.fingerprint.primary_language
        self.screen_size = self.fingerprint.screen_size
        self.hardware_concurrency = self.fingerprint.hardware_concurrency

        session_kwargs: dict[str, Any] = {"impersonate": self.impersonate}
        if CurlHttpVersion is not None:
            session_kwargs["http_version"] = CurlHttpVersion.V1_1
        self.session = curl_requests.Session(**session_kwargs)
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        self.session.headers.update(
            {
                "User-Agent": self.ua,
                "Accept-Language": self.accept_language,
                "sec-ch-ua": self.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )
        self.session.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
        self._print(
            f"[Fingerprint] impersonate={self.impersonate} lang={self.accept_language} "
            f"screen={self.screen_size} hw={self.hardware_concurrency} proxy={self.proxy or 'DIRECT'}"
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _print(self, message: str) -> None:
        tag = "Run"
        body = str(message or "")
        match = re.match(r"^\[([A-Za-z0-9_]+)\]\s*(.*)$", body)
        if match:
            tag = match.group(1)
            body = match.group(2)
        level = "INFO"
        if any(word in body for word in ("失败", "异常", "跳过", "未", "error", "Error")):
            level = "WARN"
        _print_pipe(level, tag, f"账号={self.tag} {body}" if self.tag else body)

    def _log(self, step: str, method: str, url: str, status: int, body: object | None = None) -> None:
        host = (urlparse(url).hostname or "").lower()
        module = "OpenAI" if "openai.com" in host else "ChatGPT" if "chatgpt.com" in host else "Run"
        _print_pipe("INFO" if int(status or 0) < 400 else "WARN", module, f"账号={self.tag} 步骤={step} 方法={method} 地址={host}{urlparse(url).path} 状态={status}")
        if body:
            summary = _short(body, 200)
            if summary:
                _print_pipe("INFO" if int(status or 0) < 400 else "WARN", module, f"账号={self.tag} {summary}")

    def _json_or_raise(self, step: str, method: str, url: str, response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            text = _short(getattr(response, "text", "") or "", 300)
            raise RuntimeError(f"{step} failed: HTTP {getattr(response, 'status_code', '-')} non_json={text}")
        if not isinstance(data, dict):
            raise RuntimeError(f"{step} failed: unexpected JSON type {type(data).__name__}")
        return data

    def _get_cookie_value(self, name: str, domain_hint: str = "") -> str:
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            return ""
        for cookie in list(jar):
            if getattr(cookie, "name", "") != name:
                continue
            domain = str(getattr(cookie, "domain", "") or "")
            if domain_hint and domain_hint not in domain:
                continue
            return str(getattr(cookie, "value", "") or "")
        return ""

    def import_cookie_jar(self, cookies: list[dict[str, Any]]) -> int:
        added = 0
        for item in cookies or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            domain = str(item.get("domain") or "")
            path = str(item.get("path") or "/")
            if not name or not domain:
                continue
            with contextlib.suppress(Exception):
                self.session.cookies.set(name, value, domain=domain, path=path)
                added += 1
        return added

    def export_cookie_jar(self) -> list[dict[str, Any]]:
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            return []
        out: list[dict[str, Any]] = []
        for cookie in list(jar):
            out.append(
                {
                    "name": str(getattr(cookie, "name", "") or ""),
                    "value": str(getattr(cookie, "value", "") or ""),
                    "domain": str(getattr(cookie, "domain", "") or ""),
                    "path": str(getattr(cookie, "path", "") or "/"),
                }
            )
        return out

    def visit_homepage(self) -> None:
        url = f"{self.BASE}/"
        resp = self.session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=self.impersonate,
        )
        self._log("0. Visit homepage", "GET", url, int(resp.status_code or 0), {"cookies": len(self.export_cookie_jar())})

    def get_csrf(self) -> str:
        url = f"{self.BASE}/api/auth/csrf"
        resp = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.BASE}/"}, timeout=30, impersonate=self.impersonate)
        data = self._json_or_raise("1. Get CSRF", "GET", url, resp)
        token = str(data.get("csrfToken") or "")
        self._log("1. Get CSRF", "GET", url, int(resp.status_code or 0), data)
        if not token:
            raise RuntimeError("missing csrfToken")
        return token

    def signin(self, email: str, csrf: str) -> str:
        url = f"{self.BASE}/api/auth/signin/openai"
        params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.auth_session_logging_id,
            "ext-passkey-client-capabilities": "1111",
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        form = {"callbackUrl": f"{self.BASE}/", "csrfToken": csrf, "json": "true"}
        resp = self.session.post(
            url,
            params=params,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "Referer": f"{self.BASE}/", "Origin": self.BASE},
            timeout=30,
            impersonate=self.impersonate,
        )
        data = self._json_or_raise("2. Signin", "POST", url, resp)
        auth_url = str(data.get("url") or "")
        self._log("2. Signin", "POST", url, int(resp.status_code or 0), data)
        if not auth_url:
            raise RuntimeError("signin did not return authorize url")
        return auth_url

    def authorize(self, url: str) -> str:
        resp = self.session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=self.impersonate,
        )
        final_url = str(resp.url)
        self._log("3. Authorize", "GET", url, int(resp.status_code or 0), {"final_url": final_url})
        if int(resp.status_code or 0) >= 400:
            raise RuntimeError(f"authorize failed status={resp.status_code} final={final_url}")
        auth_did = self._get_cookie_value("oai-did", "auth.openai.com") or self._get_cookie_value("oai-did", ".auth.openai.com")
        if auth_did and auth_did != self.device_id:
            self.device_id = auth_did
            self._print(f"[Auth] synchronized oai-did -> {self.device_id[:8]}…")
        return final_url

    def client_auth_session_dump(self) -> tuple[int, dict[str, Any]]:
        url = f"{self.AUTH}/api/accounts/client_auth_session_dump"
        resp = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.AUTH}/email-verification"}, timeout=30, impersonate=self.impersonate)
        try:
            data = resp.json()
        except Exception:
            data = {"text": _short(getattr(resp, "text", "") or "", 400)}
        if not isinstance(data, dict):
            data = {"data": data}
        self._log("client_auth_session_dump", "GET", url, int(resp.status_code or 0), data)
        return int(resp.status_code or 0), data

    def get_chatgpt_session_access_token(self) -> tuple[bool, dict[str, Any] | str]:
        url = f"{self.BASE}/api/auth/session"
        last_error = ""
        for _ in range(1, 6):
            try:
                resp = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.BASE}/"}, timeout=30, impersonate=self.impersonate)
                if resp.status_code == 200:
                    data = resp.json()
                    access_token = str((data or {}).get("accessToken") or "").strip()
                    if access_token:
                        return True, {
                            "access_token": access_token,
                            "session_token": str((data or {}).get("sessionToken") or "").strip(),
                            "raw_session": data,
                        }
                    last_error = "api/auth/session missing accessToken"
                else:
                    last_error = f"api/auth/session HTTP {resp.status_code}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                self.session.get(f"{self.BASE}/", headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1"}, allow_redirects=True, timeout=30, impersonate=self.impersonate)
            time.sleep(1.2)
        return False, last_error

    def create_account(self, name: str, birthdate: str) -> tuple[int, dict[str, Any]]:
        url = f"{self.AUTH}/api/accounts/create_account"
        sentinel = build_sentinel_token(
            self.session,
            self.device_id,
            flow="create_account",
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            impersonate=self.impersonate,
            screen_size=self.screen_size,
            primary_language=self.primary_language,
            accept_language=self.accept_language,
            hardware_concurrency=self.hardware_concurrency,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": f"{self.AUTH}/about-you",
            "Origin": self.AUTH,
            "oai-device-id": self.device_id,
        }
        headers.update(_make_trace_headers())
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        resp = self.session.post(url, json={"name": name, "birthdate": birthdate}, headers=headers, timeout=30, impersonate=self.impersonate)
        try:
            data = resp.json()
        except Exception:
            data = {"text": _short(getattr(resp, "text", "") or "", 500)}
        if isinstance(data, dict):
            cb = str(data.get("continue_url") or data.get("url") or data.get("redirect_url") or "")
            if cb:
                self._callback_url = cb
        self._log("7. Create Account", "POST", url, int(resp.status_code or 0), data)
        return int(resp.status_code or 0), data if isinstance(data, dict) else {"data": data}

    def callback(self, url: str | None = None) -> tuple[int, dict[str, Any]]:
        final_url = url or self._callback_url
        if not final_url:
            raise RuntimeError("missing callback url")
        resp = self.session.get(final_url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"}, allow_redirects=True, timeout=30, impersonate=self.impersonate)
        data = {"final_url": str(resp.url)}
        self._log("8. Callback", "GET", final_url, int(resp.status_code or 0), data)
        return int(resp.status_code or 0), data

    def oauth_authorize_codex(self) -> str:
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(24)
        self._codex_code_verifier = verifier
        self._codex_state = state
        params = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
            "originator": OAUTH_ORIGINATOR,
            "state": state,
        }
        url = f"{AUTH_BASE}/oauth/authorize?{urlencode(params)}"
        self._print("[Codex] GET /oauth/authorize")
        resp = self.session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{CHATGPT_BASE}/", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=self.impersonate,
        )
        final_url = str(resp.url)
        self._print(f"[Codex] /oauth/authorize -> {resp.status_code} final={final_url[:120]}")
        return final_url

    def exchange_codex_code(self, code: str) -> dict[str, Any] | None:
        url = f"{AUTH_BASE}/oauth/token"
        self._print("[Codex] POST /oauth/token")
        resp = self.session.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": self._codex_code_verifier,
            },
            timeout=60,
            impersonate=self.impersonate,
        )
        self._print(f"[Codex] /oauth/token -> {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _follow_url_for_code(self, start_url: str, referer: str) -> str:
        current_url = str(start_url or "").strip()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
        }
        for _ in range(12):
            if not current_url:
                return ""
            code = extract_code_from_url(current_url)
            if code:
                return code
            try:
                resp = self.session.get(
                    current_url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=30,
                    impersonate=self.impersonate,
                )
            except Exception as exc:
                maybe = re.search(r"(https?://localhost[^\s'\"]+)", str(exc))
                if maybe:
                    return extract_code_from_url(maybe.group(1)) or ""
                self._print(f"[Codex] follow url exception: {exc}")
                return ""
            current_url = str(resp.url)
            code = extract_code_from_url(current_url)
            if code:
                return code
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = str(resp.headers.get("Location") or "")
                if loc.startswith("/"):
                    loc = f"{AUTH_BASE}{loc}"
                code = extract_code_from_url(loc)
                if code:
                    return code
                if loc:
                    headers["Referer"] = current_url
                    current_url = loc
                    continue
            body = str(getattr(resp, "text", "") or "")
            if body:
                hrefs = re.findall(r'href=["\']([^"\']+)["\']', body, flags=re.I)
                moved = False
                for href in hrefs:
                    if href.startswith("/"):
                        href = f"{AUTH_BASE}{href}"
                    code = extract_code_from_url(href)
                    if code:
                        return code
                    if href.startswith("http://localhost") or href.startswith("https://localhost"):
                        return extract_code_from_url(href) or ""
                    if href.startswith(AUTH_BASE) and any(marker in href for marker in ("/api/accounts/consent", "/api/oauth/oauth2/auth", "/sign-in-with-chatgpt/")):
                        headers["Referer"] = current_url
                        current_url = href
                        moved = True
                        break
                if moved:
                    continue
            return ""
        return ""

    def _follow_codex_consent_workspace(self) -> str:
        consent_url = f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent"
        workspace_id = ""

        try:
            resp = self.session.get(
                consent_url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent", "Upgrade-Insecure-Requests": "1"},
                allow_redirects=False,
                timeout=30,
                impersonate=self.impersonate,
            )
            _, fields = _extract_first_form(str(getattr(resp, "text", "") or ""))
            workspace_id = str(fields.get("workspace_id") or "").strip()
        except Exception:
            pass

        if not workspace_id:
            try:
                data_resp = self.session.get(
                    consent_url + ".data",
                    headers={"Accept": "application/json", "Referer": consent_url},
                    timeout=30,
                    impersonate=self.impersonate,
                )
                data = data_resp.json()
                if isinstance(data, dict):
                    workspaces = data.get("workspaces") or data.get("organizations") or []
                    if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], dict):
                        workspace_id = str(workspaces[0].get("id") or workspaces[0].get("workspace_id") or "").strip()
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and (item.get("id") or item.get("workspace_id")):
                            workspace_id = str(item.get("id") or item.get("workspace_id") or "").strip()
                            break
            except Exception:
                pass

        if not workspace_id:
            st, dump = self.client_auth_session_dump()
            if st == 200:
                session_data = dump.get("client_auth_session") if isinstance(dump.get("client_auth_session"), dict) else dump
                workspaces = session_data.get("workspaces") if isinstance(session_data, dict) else []
                if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], dict):
                    workspace_id = str(workspaces[0].get("id") or workspaces[0].get("workspace_id") or "").strip()

        if not workspace_id:
            raise RuntimeError("workspace_id not found on consent page")

        self._print(f"[Codex] workspace/select id={workspace_id[:16]}...")
        url = f"{AUTH_BASE}/api/accounts/workspace/select"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": consent_url,
            "Origin": AUTH_BASE,
            "oai-device-id": self.device_id,
        }
        headers.update(_make_trace_headers())
        resp = self.session.post(url, json={"workspace_id": workspace_id}, headers=headers, allow_redirects=False, timeout=30, impersonate=self.impersonate)
        try:
            data = resp.json()
        except Exception:
            data = {"text": _short(getattr(resp, "text", "") or "", 500)}
        self._log("Codex workspace/select", "POST", url, int(resp.status_code or 0), data)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = str(resp.headers.get("Location") or "")
            if loc.startswith("/"):
                loc = f"{AUTH_BASE}{loc}"
            return self._follow_url_for_code(loc, consent_url)

        if int(resp.status_code or 0) != 200 or not isinstance(data, dict):
            raise RuntimeError(f"workspace/select failed: {_short(data, 220)}")

        continue_url = str(data.get("continue_url") or "")
        if continue_url:
            follow_url = continue_url if continue_url.startswith("http") else f"{AUTH_BASE}{continue_url}" if continue_url.startswith("/") else continue_url
            code = self._follow_url_for_code(follow_url, consent_url)
            if code:
                return code

        page = data.get("page") or {}
        if isinstance(page, dict):
            payload = page.get("payload") or {}
            redirect = str(payload.get("url") or "") if isinstance(payload, dict) else ""
            if redirect:
                code = self._follow_url_for_code(redirect, consent_url)
                if code:
                    return code

        orgs = data.get("data", {}).get("orgs", []) if isinstance(data.get("data"), dict) else []
        if orgs and isinstance(orgs[0], dict):
            org_id = str(orgs[0].get("id") or "").strip()
            if org_id:
                org_url = f"{AUTH_BASE}/api/accounts/organization/select"
                org_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": consent_url,
                    "Origin": AUTH_BASE,
                    "oai-device-id": self.device_id,
                }
                org_headers.update(_make_trace_headers())
                org_resp = self.session.post(org_url, json={"org_id": org_id}, headers=org_headers, allow_redirects=False, timeout=30, impersonate=self.impersonate)
                try:
                    org_data = org_resp.json()
                except Exception:
                    org_data = {}
                org_next = str(org_data.get("continue_url") or "") if isinstance(org_data, dict) else ""
                if org_next:
                    follow_url = org_next if org_next.startswith("http") else f"{AUTH_BASE}{org_next}" if org_next.startswith("/") else org_next
                    code = self._follow_url_for_code(follow_url, consent_url)
                    if code:
                        return code

        code = self._follow_url_for_code(consent_url, consent_url)
        if code:
            return code
        raise RuntimeError("could not extract authorization code after workspace/select")


def _save_chatgpt_session_cache(*, email: str, reg: ChatGPTRegister, access_result: dict[str, Any], password: str, name: str, birthdate: str) -> None:
    access_token = str((access_result or {}).get("access_token") or "")
    data = {
        "email": email,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "chatgpt_password": password,
        "name": name,
        "birthdate": birthdate,
        "access_token": access_token,
        "session_token": str((access_result or {}).get("session_token") or ""),
        "cookies": reg.export_cookie_jar(),
    }
    path = _chatgpt_session_path(email)
    _save_json(path, data)
    reg._print(f"[Session] ChatGPT 会话已保存: {path}")


def _try_reuse_runtime_chatgpt_session(reg: ChatGPTRegister, email: str) -> tuple[bool, dict[str, Any] | str]:
    path = _chatgpt_session_path(email)
    data = _load_json(path)
    if not data:
        return False, f"missing session cache: {path}"
    count = reg.import_cookie_jar(data.get("cookies") or [])
    reg._print(f"[Session] 加载本地 ChatGPT 会话: cookies={count}")
    if count:
        ok, access_result = reg.get_chatgpt_session_access_token()
        if ok:
            return True, access_result
    cached_token = str(data.get("access_token") or "").strip()
    if cached_token:
        return True, {"access_token": cached_token, "session_token": str(data.get("session_token") or "").strip()}
    return False, f"session invalid for {email}"


def _chatgpt_json(reg: ChatGPTRegister, method: str, path: str, *, access_token: str = "", json_body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    url = f"{reg.BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Origin": reg.BASE,
        "Referer": f"{reg.BASE}/",
        "oai-device-id": reg.device_id,
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    headers.update(_make_trace_headers())
    func = getattr(reg.session, method.lower())
    resp = func(url, json=json_body, headers=headers, timeout=30, impersonate=reg.impersonate)
    try:
        data = resp.json()
    except Exception:
        data = {"text": _short(getattr(resp, "text", "") or "", 800)}
    return int(resp.status_code or 0), data if isinstance(data, dict) else {"data": data}


def _patch_onboarding(reg: ChatGPTRegister, access_token: str) -> None:
    st_me, me = _chatgpt_json(reg, "GET", "/backend-api/me", access_token=access_token)
    if st_me != 200:
        raise RuntimeError(f"backend-api/me failed http={st_me} body={_short(me, 300)}")
    user_id = str(me.get("id") or "").strip()
    if not user_id:
        raise RuntimeError("backend-api/me missing user id")
    st_chk, chk = _chatgpt_json(reg, "GET", "/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480", access_token=access_token)
    if st_chk != 200:
        raise RuntimeError(f"accounts/check failed http={st_chk} body={_short(chk, 300)}")
    accounts = chk.get("accounts") or {}
    if not isinstance(accounts, dict) or not accounts:
        raise RuntimeError("accounts/check missing accounts")
    account_id = next(iter(accounts.keys()))
    path = f"/backend-api/accounts/{account_id}/users/{user_id}"
    payload = {"onboarding_information": {"role": DEFAULT_ONBOARDING_ROLE, "departments": []}}
    st_patch, patched = _chatgpt_json(reg, "PATCH", path, access_token=access_token, json_body=payload)
    if st_patch != 200:
        raise RuntimeError(f"onboarding patch failed http={st_patch} body={_short(patched, 300)}")
    reg._print(f"[HAR] onboarding PATCH success account={account_id} user={user_id}")


def _extract_sso_connection(reg: ChatGPTRegister) -> tuple[str, int]:
    cookie_val = reg._get_cookie_value("oai-client-auth-session", "auth.openai.com") or reg._get_cookie_value("oai-client-auth-session", "openai.com")
    session_data = _decode_auth_session_cookie(cookie_val)
    sso = session_data.get("sso") if isinstance(session_data.get("sso"), dict) else {}
    conns = sso.get("connections") if isinstance(sso, dict) else []
    if isinstance(conns, list):
        for item in conns:
            if isinstance(item, dict):
                name = str(item.get("connection_name") or "").strip()
                provider = int(item.get("connection_provider") or 0)
                if name and provider:
                    return name, provider
    st, dump = reg.client_auth_session_dump()
    if st == 200:
        client_auth = dump.get("client_auth_session") if isinstance(dump.get("client_auth_session"), dict) else dump
        sso = client_auth.get("sso") if isinstance(client_auth, dict) and isinstance(client_auth.get("sso"), dict) else {}
        conns = sso.get("connections") if isinstance(sso, dict) else []
        if isinstance(conns, list):
            for item in conns:
                if isinstance(item, dict):
                    name = str(item.get("connection_name") or "").strip()
                    provider = int(item.get("connection_provider") or 0)
                    if name and provider:
                        return name, provider
    raise RuntimeError("enterprise SSO connection not found")


def _build_authorize_continue_sentinel(reg: ChatGPTRegister, page_url: str) -> str:
    token = build_sentinel_token(
        reg.session,
        reg.device_id,
        flow="authorize_continue",
        user_agent=reg.ua,
        sec_ch_ua=reg.sec_ch_ua,
        impersonate=reg.impersonate,
        screen_size=reg.screen_size,
        primary_language=reg.primary_language,
        accept_language=reg.accept_language,
        hardware_concurrency=reg.hardware_concurrency,
    )
    if not token:
        raise RuntimeError(f"authorize_continue sentinel failed at {page_url}")
    return token


def _complete_sixoner_external_flow(reg: ChatGPTRegister, *, email: str, continue_url: str, referer: str) -> str:
    page_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
    }
    resp = reg.session.get(continue_url, headers=page_headers, allow_redirects=True, timeout=30, impersonate=reg.impersonate)
    sso_url = str(resp.url)
    reg._print(f"[HAR] SSO page -> {sso_url[:160]}")
    approve_action, approve_fields = _extract_first_form(str(getattr(resp, "text", "") or ""))
    challenge = str(approve_fields.get("challenge") or "").strip()
    if not approve_action or not challenge:
        raise RuntimeError(f"SSO approve form missing challenge: {sso_url}")
    approve_resp = reg.session.post(
        urljoin(sso_url, approve_action),
        data={"email": email, "challenge": challenge},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": sso_url,
        },
        allow_redirects=False,
        timeout=30,
        impersonate=reg.impersonate,
    )
    approve_loc = str(approve_resp.headers.get("Location") or "")
    reg._print(f"[HAR] SSO approve -> {approve_resp.status_code} next={approve_loc[:160] or '-'}")
    if approve_resp.status_code not in (301, 302, 303, 307, 308) or not approve_loc:
        raise RuntimeError(f"SSO approve failed ({approve_resp.status_code})")

    consent_resp = reg.session.get(
        approve_loc,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": sso_url, "Upgrade-Insecure-Requests": "1"},
        allow_redirects=True,
        timeout=30,
        impersonate=reg.impersonate,
    )
    consent_url = str(consent_resp.url)
    reg._print(f"[HAR] interstitial page -> {consent_url[:160]}")
    if "/sign-in-with-chatgpt/codex/consent" in consent_url:
        return consent_url

    interstitial_action, interstitial_fields = _extract_first_form(str(getattr(consent_resp, "text", "") or ""))
    interstitial_token = str(interstitial_fields.get("interstitial_token") or "").strip()
    csrf_token = str(interstitial_fields.get("csrf_token") or "").strip()
    action_value = str(interstitial_fields.get("action") or "confirm").strip() or "confirm"
    if not interstitial_action or not interstitial_token or not csrf_token:
        raise RuntimeError(f"interstitial form missing fields: {consent_url}")

    final_resp = reg.session.post(
        urljoin(consent_url, interstitial_action),
        data={"interstitial_token": interstitial_token, "action": action_value, "csrf_token": csrf_token},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Cache-Control": "max-age=0",
            "Origin": "null",
            "Referer": consent_url,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Upgrade-Insecure-Requests": "1",
        },
        allow_redirects=False,
        timeout=30,
        impersonate=reg.impersonate,
    )
    loc = str(final_resp.headers.get("Location") or "")
    reg._print(f"[HAR] interstitial confirm -> {final_resp.status_code} next={loc[:160] or '-'}")
    if final_resp.status_code in (301, 302, 303, 307, 308) and loc:
        callback_resp = reg.session.get(
            loc,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": consent_url, "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
            timeout=30,
            impersonate=reg.impersonate,
        )
        final_url = str(callback_resp.url)
        reg._print(f"[HAR] callback page -> {final_url[:160]}")
        return final_url
    return str(final_resp.url)


def _complete_chatgpt_web_sso(reg: ChatGPTRegister, *, email: str, sso_url: str) -> str:
    conn_name, conn_provider = _extract_sso_connection(reg)
    sentinel = _build_authorize_continue_sentinel(reg, sso_url)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": sso_url,
        "Origin": reg.AUTH,
        "oai-device-id": reg.device_id,
        "openai-sentinel-token": sentinel,
    }
    headers.update(_make_trace_headers())
    resp = reg.session.post(
        f"{reg.AUTH}/api/accounts/authorize/continue",
        json={"connection": conn_name, "connection_provider": conn_provider},
        headers=headers,
        allow_redirects=False,
        timeout=30,
        impersonate=reg.impersonate,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"text": _short(getattr(resp, "text", "") or "", 500)}
    reg._log("HAR register authorize_continue", "POST", f"{reg.AUTH}/api/accounts/authorize/continue", int(resp.status_code or 0), data)
    if int(resp.status_code or 0) != 200:
        raise RuntimeError(f"register authorize/continue failed ({resp.status_code}): {_short(data, 220)}")
    continue_url = str(data.get("continue_url") or ((data.get("page") or {}).get("payload") or {}).get("url") or "")
    if not continue_url:
        raise RuntimeError("register authorize/continue did not return continue_url")
    return _complete_sixoner_external_flow(reg, email=email, continue_url=continue_url, referer=sso_url)


def _complete_codex_oauth(reg: ChatGPTRegister, email: str) -> str:
    authorize_final = reg.oauth_authorize_codex()
    reg._print(f"[HAR] Codex authorize -> {authorize_final[:120]}")
    code = extract_code_from_url(authorize_final)
    if code:
        return code
    continue_referer = authorize_final if authorize_final.startswith(AUTH_BASE) else f"{AUTH_BASE}/log-in"
    sentinel = _build_authorize_continue_sentinel(reg, continue_referer)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": continue_referer,
        "Origin": reg.AUTH,
        "oai-device-id": reg.device_id,
        "openai-sentinel-token": sentinel,
    }
    headers.update(_make_trace_headers())
    resp = reg.session.post(
        f"{reg.AUTH}/api/accounts/authorize/continue",
        json={"username": {"kind": "email", "value": email}},
        headers=headers,
        timeout=30,
        allow_redirects=False,
        impersonate=reg.impersonate,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"text": _short(getattr(resp, "text", "") or "", 500)}
    reg._log("HAR codex authorize_continue", "POST", f"{reg.AUTH}/api/accounts/authorize/continue", int(resp.status_code or 0), data)
    if int(resp.status_code or 0) != 200:
        raise RuntimeError(f"Codex authorize/continue failed ({resp.status_code}): {_short(data, 220)}")
    next_url = str(data.get("continue_url") or data.get("url") or ((data.get("page") or {}).get("payload") or {}).get("url") or "")
    page = data.get("page") or {}
    page_type = str(page.get("type") or "") if isinstance(page, dict) else ""
    reg._print(f"[HAR] codex continue -> page={page_type or '-'} next={next_url[:120]}")
    if page_type == "external_url" or "external.auth.openai.com/sso/authorize" in next_url:
        next_url = _complete_sixoner_external_flow(reg, email=email, continue_url=next_url, referer=continue_referer)
    code = extract_code_from_url(next_url)
    if code:
        return code
    if next_url:
        with contextlib.suppress(Exception):
            reg.session.get(
                next_url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": continue_referer, "Upgrade-Insecure-Requests": "1"},
                allow_redirects=True,
                timeout=30,
                impersonate=reg.impersonate,
            )
    return reg._follow_codex_consent_workspace()


def _debug_auth_cookies(reg: ChatGPTRegister, stage: str) -> None:
    items = []
    for cookie in reg.export_cookie_jar():
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        if "chatgpt.com" in domain or "openai.com" in domain:
            items.append(f"{name}@{domain}")
    reg._print(f"[HAR] {stage} cookies={items[:20]}")


def _get_access_token_after_callback(reg: ChatGPTRegister) -> tuple[bool, dict[str, Any] | str]:
    return reg.get_chatgpt_session_access_token()


def _query_plan_with_access_token(reg: ChatGPTRegister, access_token: str) -> tuple[int, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {str(access_token or '').strip()}",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    resp = reg.session.get(WHAM_USAGE_URL, headers=headers, timeout=30, impersonate=reg.impersonate)
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "")
    if status != 200:
        return status, _short(text, 220)
    try:
        data = resp.json()
    except Exception:
        return status, f"non_json: {_short(text, 220)}"
    return status, _extract_plan_label(data)


def _refresh_token_file_on_401(token_path: Path, token_data: dict[str, Any], proxy: str | None) -> tuple[bool, str]:
    email = str((token_data or {}).get("email") or token_path.stem).strip()
    reg = None
    try:
        reg = ChatGPTRegister(proxy=proxy, tag=f"refresh-{email}")
        ok, access_result = _try_reuse_runtime_chatgpt_session(reg, email)
        if not ok:
            return False, str(access_result)
        code = _complete_codex_oauth(reg, email)
        if not code:
            return False, "failed to get authorization code during refresh"
        tokens = reg.exchange_codex_code(code)
        if not tokens or not isinstance(tokens, dict):
            return False, "/oauth/token failed during refresh"
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            return False, "missing refresh_token during refresh"
        updated = dict(token_data or {})
        updated["type"] = "codex"
        updated["email"] = email
        updated["token_source"] = "ChatGPT_team_refresh_401"
        updated["refresh_token"] = refresh_token
        updated["access_token"] = str(tokens.get("access_token") or "")
        updated["id_token"] = str(tokens.get("id_token") or "")
        updated["saved_at"] = datetime.now(timezone.utc).isoformat()
        _save_json(token_path, updated)
        return True, refresh_token
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if reg is not None:
            reg.close()


def _check_single_token_file(token_path: Path, proxy: str | None) -> tuple[str, bool, bool]:
    token_data = _load_json(token_path)
    email = str(token_data.get("email") or token_path.stem).strip()
    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        _print_pipe("WARN", "Check", f"email={email} 缺少 access_token，跳过")
        return email, False, False

    reg = None
    try:
        reg = ChatGPTRegister(proxy=proxy, tag=f"check-{email}")
        status, detail = _query_plan_with_access_token(reg, access_token)
        if status == 200:
            _print_pipe("OK", "Check", f"email={email} plan={detail}")
            return email, True, False

        if status == 429:
            _print_pipe("WARN", "Check", f"email={email} plan_check=429，重试一次")
            time.sleep(1.0)
            status, detail = _query_plan_with_access_token(reg, access_token)
            if status == 200:
                _print_pipe("OK", "Check", f"email={email} retry plan={detail}")
                return email, True, False
            if status == 429:
                _print_pipe("WARN", "Check", f"email={email} 二次 429，开始 OAuth 刷新")
                ok, refresh_detail = _refresh_token_file_on_401(token_path, token_data, proxy)
                if not ok:
                    _print_pipe("ERR", "Check", f"email={email} 429 后刷新失败: {refresh_detail}")
                    return email, False, False
                reg2 = None
                try:
                    reg2 = ChatGPTRegister(proxy=proxy, tag=f"recheck-{email}")
                    refreshed_data = _load_json(token_path)
                    new_at = str(refreshed_data.get("access_token") or "").strip()
                    status2, detail2 = _query_plan_with_access_token(reg2, new_at)
                    if status2 == 200:
                        _print_pipe("OK", "Check", f"email={email} refreshed plan={detail2}")
                        return email, True, True
                    _print_pipe("WARN", "Check", f"email={email} 刷新后套餐查询失败 status={status2} detail={detail2}")
                    return email, False, True
                finally:
                    if reg2 is not None:
                        reg2.close()

        if status == 401:
            _print_pipe("WARN", "Check", f"email={email} access_token=401，开始 OAuth 刷新")
            ok, refresh_detail = _refresh_token_file_on_401(token_path, token_data, proxy)
            if not ok:
                _print_pipe("ERR", "Check", f"email={email} 刷新失败: {refresh_detail}")
                return email, False, False
            reg2 = None
            try:
                reg2 = ChatGPTRegister(proxy=proxy, tag=f"recheck-{email}")
                refreshed_data = _load_json(token_path)
                new_at = str(refreshed_data.get("access_token") or "").strip()
                status2, detail2 = _query_plan_with_access_token(reg2, new_at)
                if status2 == 200:
                    _print_pipe("OK", "Check", f"email={email} refreshed plan={detail2}")
                    return email, True, True
                _print_pipe("WARN", "Check", f"email={email} 刷新后套餐查询失败 status={status2} detail={detail2}")
                return email, False, True
            finally:
                if reg2 is not None:
                    reg2.close()

        _print_pipe("WARN", "Check", f"email={email} plan_check status={status} detail={detail}")
        return email, False, False
    except Exception as exc:
        _print_pipe("ERR", "Check", f"email={email} 检查异常: {type(exc).__name__}: {exc}")
        return email, False, False
    finally:
        if reg is not None:
            reg.close()


def run_token_check(proxy: str | None, workers: int = 100) -> int:
    token_dir = _cwd_path(CODEX_TOKEN_DIR)
    token_files = sorted([p for p in token_dir.glob("*.json") if p.is_file()])
    if not token_files:
        _print_pipe("WARN", "Check", "未找到 codex_tokens/*.json")
        return 1
    workers = max(1, int(workers or 100))
    _print_pipe("INFO", "Check", f"开始检查本地 token 文件: {len(token_files)} threads={workers}")
    checked = 0
    refreshed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(token_files))), thread_name_prefix="team-check-") as executor:
        futures = {executor.submit(_check_single_token_file, token_path, proxy): token_path for token_path in token_files}
        while futures:
            done, _ = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for fut in done:
                futures.pop(fut, None)
                _email, ok, did_refresh = fut.result()
                if ok:
                    checked += 1
                else:
                    failed += 1
                if did_refresh:
                    refreshed += 1
    _print_pipe("INFO", "Check", f"完成: checked={checked} refreshed={refreshed} failed={failed}")
    return 0 if (checked or refreshed) else 1


def _register_one(idx: int, total: int, proxy: str | None, output_file: str) -> tuple[bool, str]:
    tag = f"r{idx}"
    reg = None
    email = ""
    try:
        reg = ChatGPTRegister(proxy=proxy, tag=tag)
        name = _random_name()
        birthdate = _random_birthdate()
        password = _generate_password()
        email = f"{_random_local(name)}{EMAIL_DOMAIN}"
        _print_pipe("INFO", tag, f"[HAR] 随机邮箱: {email}")

        reg.visit_homepage()
        time.sleep(random.uniform(0.2, 0.5))
        csrf = reg.get_csrf()
        auth_url = reg.signin(email, csrf)
        final_url = reg.authorize(auth_url)
        final_path = urlparse(final_url).path
        final_host = (urlparse(final_url).hostname or "").lower()
        reg._print(f"[HAR] authorize -> {final_path}")

        needs_about_you = False
        if final_path == "/sso":
            final_url = _complete_chatgpt_web_sso(reg, email=email, sso_url=final_url)
            final_path = urlparse(final_url).path
            final_host = (urlparse(final_url).hostname or "").lower()
            reg._print(f"[HAR] register SSO 完成 -> {final_url[:160]}")
        elif "about-you" in final_path:
            needs_about_you = True
        elif final_host == "chatgpt.com" or final_host.endswith(".chatgpt.com"):
            reg._print("[HAR] ChatGPT Web 会话已建立")
        else:
            raise RuntimeError(f"unexpected authorize destination: {final_url}")

        if needs_about_you:
            st, data = reg.create_account(name, birthdate)
            if st != 200:
                raise RuntimeError(f"create_account failed ({st}): {_short(data, 220)}")
            reg.callback(str(data.get("continue_url") or ""))
            reg._print("[HAR] callback 完成")

        _debug_auth_cookies(reg, "callback后")
        ok_at, at_result = _get_access_token_after_callback(reg)
        if not ok_at:
            raise RuntimeError(f"failed to get access token: {at_result}")
        access_token = str((at_result or {}).get("access_token") or "").strip()
        _save_chatgpt_session_cache(
            email=email,
            reg=reg,
            access_result=at_result if isinstance(at_result, dict) else {},
            password=password,
            name=name,
            birthdate=birthdate,
        )
        _patch_onboarding(reg, access_token)

        code = _complete_codex_oauth(reg, email)
        if not code:
            raise RuntimeError("missing authorization code")
        tokens = reg.exchange_codex_code(code)
        if not tokens or not isinstance(tokens, dict):
            raise RuntimeError("/oauth/token failed")
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("missing refresh_token")
        token_path = _codex_token_path(email)
        _save_json(
            token_path,
            {
                "type": "codex",
                "email": email,
                "token_source": "ChatGPT_team",
                "refresh_token": refresh_token,
                "access_token": str(tokens.get("access_token") or ""),
                "id_token": str(tokens.get("id_token") or ""),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _append_line(_cwd_path(output_file), f"{email}----{password}----rt----{refresh_token}")
        _print_pipe("OK", "Summary", f"[{idx}/{total}] 注册成功 email={email} rt_saved=Y first_rt=Y")
        return True, email
    except Exception as exc:
        reason = f"[{idx}/{total}] 注册失败 email={email or '-'} err={type(exc).__name__}: {exc}"
        _append_line(_cwd_path(FAILED_OUTPUT_FILE), reason)
        _print_pipe("ERR", "Summary", reason)
        return False, email
    finally:
        if reg is not None:
            reg.close()


def run_batch(total_accounts: int, max_workers: int, proxy: str | None, output_file: str) -> int:
    total_accounts = max(1, int(total_accounts))
    max_workers = max(1, int(max_workers))
    _print_pipe("INFO", "Run", f"HAR注册模式启动：数量={total_accounts} 并发={max_workers} 代理={proxy or '无'}")
    success = 0
    fail = 0
    submitted = 0
    stop_waiting = False
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="team-reg-") as executor:
        futures: dict[Any, int] = {}

        def _submit_next() -> bool:
            nonlocal submitted
            if stop_waiting or submitted >= total_accounts:
                return False
            submitted += 1
            fut = executor.submit(_register_one, submitted, total_accounts, proxy, output_file)
            futures[fut] = submitted
            return True

        for _ in range(min(max_workers, total_accounts)):
            _submit_next()

        _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures))
        try:
            while futures:
                done, _ = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=stop_waiting)
                    continue
                for fut in done:
                    futures.pop(fut, None)
                    ok, _email = fut.result()
                    if ok:
                        success += 1
                    else:
                        fail += 1
                    if not stop_waiting:
                        _submit_next()
                _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=stop_waiting)
        except KeyboardInterrupt:
            stop_waiting = True
            _print_pipe("WARN", "Run", "收到 Ctrl+C，不再投递新任务，等待当前注册完成…（再按一次 Ctrl+C 强制退出）")
            _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=True)
            try:
                while futures:
                    done, _ = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                    if not done:
                        _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=True)
                        continue
                    for fut in done:
                        futures.pop(fut, None)
                        ok, _email = fut.result()
                        if ok:
                            success += 1
                        else:
                            fail += 1
                    _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=len(futures), waiting=True)
            except KeyboardInterrupt:
                _print_pipe("WARN", "Run", "再次收到 Ctrl+C，立即退出")
                raise SystemExit(130) from None
        finally:
            _set_progress_title(total=total_accounts, submitted=submitted, success=success, fail=fail, active=0)

    _print_pipe("INFO", "Summary", f"HAR注册模式完成：成功={success}/{total_accounts} 输出={output_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone pure RT flow for ChatGPT/Codex team accounts")
    parser.add_argument("-p", "--proxy", default=None)
    parser.add_argument("-n", "--total", type=int, default=1)
    parser.add_argument("-w", "--workers", type=int, default=1)
    parser.add_argument("-o", "--output", default=REGISTERED_OUTPUT_FILE)
    parser.add_argument("--check-tokens", action="store_true", help="check existing codex_tokens access_token plan type; refresh on 401 and overwrite file")
    args = parser.parse_args()
    proxy = str(args.proxy or DEFAULT_PROXY or "").strip() or None
    if args.check_tokens:
        workers = args.workers if args.workers and args.workers > 0 else 100
        return run_token_check(proxy, workers=workers)
    return run_batch(args.total, args.workers, proxy, args.output)

def _random_local(name: str) -> str:
    """
    修复缺失的 _random_local 函数
    生成基于名字首字母的随机邮箱前缀
    """
    if not name:
        return "user" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    
    first_letter = name.strip().split()[0][0].lower()
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{first_letter}{random_suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
