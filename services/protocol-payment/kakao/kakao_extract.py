"""
Kakao Pay / Nicepay 跳转链接提取。

本文件独立维护，不依赖主 iDEAL 或 BLIK 脚本：
  proxy_seeds.txt 中每行一条 sticky Seed
  -> 同一 Seed 派生 Checkout / Bootstrap Stripe init 地区
  -> checkout/update 地区
  -> Stripe refresh / taxes / Kakao / approve / redirect 地区

同一次任务中失败 Seed 不会再次尝试；跨任务状态保存在本目录的
proxy_state.json。成功会清零失败计数；明确代理错误会移除该 Seed。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path
from threading import Event, RLock
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests

try:
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:
    CurlCffiSession = None


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sms_tool.account_liveness import probe_account_liveness


LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = max(5, min(120, int(os.environ.get("KAKAO_PAY_TIMEOUT", "30") or "30")))
POLL_TIMEOUT = max(30, min(300, int(os.environ.get("KAKAO_POLL_TIMEOUT", "120") or "120")))
APPROVE_RETRY_MAX = max(1, min(10, int(os.environ.get("KAKAO_APPROVE_RETRY_MAX", "1") or "1")))
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_RUNTIME = "c00af4ce81"
STRIPE_PAYMENT_UA = f"stripe.js/{STRIPE_RUNTIME}; stripe-js-v3/{STRIPE_RUNTIME}; checkout"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

def configured_country(name: str, default: str) -> str:
    value = str(os.environ.get(name, default) or default).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise RuntimeError(f"{name} 必须是两位国家代码")
    return value


CHECKOUT_COUNTRY = configured_country("KAKAO_BOOTSTRAP_COUNTRY", "KR")
PROMOTION_COUNTRY = configured_country("KAKAO_PROMOTION_COUNTRY", "VN")
PROVIDER_COUNTRY = configured_country("KAKAO_PROVIDER_COUNTRY", "KR")
IP_CHECK_SOURCES = (
    ("ipinfo", "https://ipinfo.io/json"),
    ("ipapi", "https://ipapi.co/json/"),
    ("ipwho", "https://ipwho.is/"),
    ("myip", "https://api.myip.com/"),
)
_PROXY_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)
# Sticky 代理（如 cliproxy）由 sid 会话标识决定出口 IP：同一 sid 在 TTL 内粘同一个
# IP，会把不同 region 的派生粘在同一个出口上。为让 checkout/provider(KR) 与
# promotion(VN) 各自拿到本地区出口，Kakao 给每个地区派生独立 sid（追加国家后缀）。
_PROXY_SID_RE = re.compile(r"(?i)(?P<name>sid)(?P<separator>[-_=])(?P<value>[A-Za-z0-9]+)")
_state_lock = RLock()
_file_lock = RLock()
_proxy_redaction_lock = RLock()
_proxy_state: dict[str, Any] | None = None
_proxy_redaction_values: set[str] = set()

KOREAN_FAMILY_NAMES = (
    "김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황",
)
KOREAN_GIVEN_NAMES = (
    "민준", "서준", "도윤", "예준", "시우", "주원", "하준", "지호", "지후", "준서", "서연", "서윤",
    "지우", "서현", "하은", "하윤", "민서", "지유", "윤서", "채원",
)
SEOUL_ADDRESS_SEEDS = (
    {"district": "강남구", "road": "테헤란로", "postal": "06164", "base": 87, "span": 40},
    {"district": "강남구", "road": "봉은사로", "postal": "06097", "base": 524, "span": 32},
    {"district": "서초구", "road": "서초대로", "postal": "06611", "base": 396, "span": 36},
    {"district": "송파구", "road": "올림픽로", "postal": "05510", "base": 300, "span": 36},
    {"district": "마포구", "road": "월드컵북로", "postal": "03925", "base": 396, "span": 36},
)
EMAIL_DOMAINS = ("gmail.com", "naver.com", "daum.net", "kakao.com")


class TaskStopped(RuntimeError):
    pass


def redact_log_text(text: str) -> str:
    text = str(text or "")
    with _proxy_redaction_lock:
        values = sorted(_proxy_redaction_values, key=len, reverse=True)
    for value in values:
        if value:
            try:
                label = proxy_label(value)
            except (TypeError, ValueError):
                label = f"proxy#{hashlib.sha256(value.encode()).hexdigest()[:10]}"
            if label == "direct":
                label = f"proxy#{hashlib.sha256(value.encode()).hexdigest()[:10]}"
            text = text.replace(value, label)
    return text


def log(message: str, prefix: str = "") -> None:
    line = redact_log_text(f"{prefix}{message}")
    print(line, flush=True)
    try:
        with (LOG_DIR / "kakao_extract.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


PREFLIGHT_TIMEOUT = env_int(
    "KAKAO_PROXY_PREFLIGHT_TIMEOUT", 12, minimum=3, maximum=TIMEOUT
)


def default_proxy_scheme() -> str:
    raw = os.environ.get("KAKAO_PROXY_DEFAULT_SCHEME", "http").strip().lower().removesuffix("://")
    return "socks5h" if raw in {"socks5", "socks5h"} else "http"


def normalize_proxy_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        if text.count(":") == 3 and "@" not in text:
            host, port, username, password = text.split(":", 3)
            text = f"{default_proxy_scheme()}://{username}:{password}@{host}:{port}"
        else:
            text = f"{default_proxy_scheme()}://{text}"
    try:
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        username = quote(unquote(parsed.username or ""), safe="-._~")
        auth = username
        if parsed.password is not None:
            auth = f"{auth}:{quote(unquote(parsed.password), safe='-._~')}"
        netloc = f"{auth}@{host}" if auth else host
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        return ""


def register_proxy_for_redaction(proxy: str) -> None:
    raw = str(proxy or "").strip()
    if not raw:
        return
    normalized = normalize_proxy_url(raw)
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
    with _proxy_redaction_lock:
        _proxy_redaction_values.update(values)


def proxy_short(proxy: str) -> str:
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return "direct"
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:10]
    return f"proxy#{digest}"


def proxy_label(proxy: str) -> str:
    return proxy_short(proxy)


def proxy_chain_key(proxy: str) -> str:
    normalized = unquote(normalize_proxy_url(proxy))
    without_country = _PROXY_COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}*", normalized
    )
    # 只去掉 sid 尾部的地区后缀（proxy_for_country 追加的 2 位大写国家码），保留
    # base sid。这样同一 Seed 的三地区派生（base+KR/base+VN）归一化到同一 base →
    # 同 chain_key（sticky 校验通过）；而多条只有 base sid 不同的冗余 Seed 仍是不同
    # chain_key，不会被 load_proxy_seeds 去重。假设 base sid 不以 2 位连续大写结尾。
    def _normalize_sid_base(match: re.Match[str]) -> str:
        base = re.sub(r"[A-Z]{2}$", "", match.group("value"))
        return f"{match.group('name')}{match.group('separator')}{base}"

    without_country = _PROXY_SID_RE.sub(_normalize_sid_base, without_country)
    return hashlib.sha256(without_country.encode()).hexdigest()[:16] if without_country else ""


def proxy_for_country(proxy: str, country: str) -> str:
    """Only change country/region while preserving the sticky session fields."""
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        raise RuntimeError("代理为空，无法派生地区链路")
    parsed = urlsplit(normalized)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    target_country = str(country or "").strip().lower()
    replacements = 0

    def replace_country(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        current = match.group("value")
        value = target_country.upper() if current.isupper() else target_country
        return f"{match.group('name')}{match.group('separator')}{value}"

    username = _PROXY_COUNTRY_SELECTOR_RE.sub(replace_country, username)
    password = _PROXY_COUNTRY_SELECTOR_RE.sub(replace_country, password)
    if not replacements:
        raise RuntimeError(f"代理未包含可改写的 country/region 选择器: {proxy_label(proxy)}")
    # 给 sid 追加地区后缀，使 sticky 代理为每个地区分配独立出口 IP。始终从原始 Seed
    # 调用（kakao_proxy_chain 即如此），避免后缀累积。不含 sid 的代理不受影响。
    country_tag = target_country.upper()

    def tag_sid(match: re.Match[str]) -> str:
        return f"{match.group('name')}{match.group('separator')}{match.group('value')}{country_tag}"

    username = _PROXY_SID_RE.sub(tag_sid, username)
    password = _PROXY_SID_RE.sub(tag_sid, password)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = quote(username, safe="-._~")
    if parsed.password is not None:
        auth = f"{auth}:{quote(password, safe='-._~')}"
    derived = urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))
    register_proxy_for_redaction(derived)
    return derived


def kakao_proxy_chain(proxy_seed: str) -> tuple[str, str, str]:
    checkout_proxy = proxy_for_country(proxy_seed, CHECKOUT_COUNTRY)
    promotion_proxy = proxy_for_country(proxy_seed, PROMOTION_COUNTRY)
    provider_proxy = proxy_for_country(proxy_seed, PROVIDER_COUNTRY)
    key = proxy_chain_key(proxy_seed)
    if not key or any(
        proxy_chain_key(proxy) != key
        for proxy in (checkout_proxy, promotion_proxy, provider_proxy)
    ):
        raise RuntimeError("代理地区改写改变了 sticky Seed，已拒绝混用代理链")
    return checkout_proxy, promotion_proxy, provider_proxy


def role_country(role: str) -> str:
    if role == "promotion":
        return PROMOTION_COUNTRY
    return CHECKOUT_COUNTRY if role == "checkout" else PROVIDER_COUNTRY


def role_label(role: str) -> str:
    if role == "promotion":
        return f"{PROMOTION_COUNTRY} promotion"
    return (
        f"{CHECKOUT_COUNTRY} checkout"
        if role == "checkout"
        else f"{PROVIDER_COUNTRY} provider/approve"
    )


def proxy_seed_file() -> Path:
    raw = os.environ.get("KAKAO_PROXY_SEED_FILE", "").strip()
    return Path(raw).expanduser() if raw else SCRIPT_DIR / "proxy_seeds.txt"


def proxy_state_file() -> Path:
    raw = os.environ.get("KAKAO_PROXY_STATE_FILE", "").strip()
    return Path(raw).expanduser() if raw else SCRIPT_DIR / "proxy_state.json"


def load_proxy_state() -> dict[str, Any]:
    global _proxy_state
    with _state_lock:
        if _proxy_state is not None:
            return _proxy_state
        path = proxy_state_file()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        _proxy_state = payload if isinstance(payload, dict) else {}
        _proxy_state.setdefault("seed", {})
        return _proxy_state


def save_proxy_state() -> None:
    with _state_lock:
        state = load_proxy_state()
        path = proxy_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)


def seed_record(proxy_seed: str) -> dict[str, Any]:
    key = proxy_chain_key(proxy_seed)
    if not key:
        return {}
    state = load_proxy_state()
    seeds = state.setdefault("seed", {})
    record = seeds.setdefault(key, {})
    if not isinstance(record, dict):
        seeds[key] = {}
        return seeds[key]
    return record


def record_in_cooldown(record: dict[str, Any], now: int) -> bool:
    fail = int(record.get("fail") or 0)
    last_fail = int(record.get("last_fail") or 0)
    cooldown = env_int("KAKAO_PROXY_FAIL_COOLDOWN", 180, minimum=0, maximum=86_400)
    return fail > 0 and (cooldown == 0 or not last_fail or now - last_fail <= cooldown)


def remove_seed(proxy_seed: str, reason: str) -> bool:
    if not env_bool("KAKAO_PROXY_REMOVE_FAILED", True):
        return False
    path = proxy_seed_file()
    key = proxy_chain_key(proxy_seed)
    if not key or not path.is_file():
        return False
    with _file_lock:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        removed = [line for line in lines if proxy_chain_key(line) == key]
        if not removed:
            return False
        kept = [line for line in lines if proxy_chain_key(line) != key]
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text("".join(kept), encoding="utf-8")
        os.replace(temp, path)
        audit = SCRIPT_DIR / "removed_proxies.jsonl"
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "time": int(time.time()),
                        "proxy": proxy_label(proxy_seed),
                        "reason": redact_log_text(str(reason or ""))[:300],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return True


def is_direct_proxy_error(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "proxy authentication",
            "proxy auth",
            "invalid proxy",
            "malformed proxy",
            "unsupported proxy",
            "could not resolve proxy",
            "http 407",
            "status 407",
        )
    )


def is_proxy_health_error(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection refused",
            "connection aborted",
            "proxy connect",
            "proxy tunnel",
            "proxy handshake",
            "ssl",
            "tls",
            "curl: (",
            "http 502",
            "http 503",
            "http 504",
        )
    )


def is_account_error(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "invalid access token",
            "token_invalidated",
            "authentication token has been invalidated",
            "chatgpt /me failed 401",
            "wham/usage failed 401",
            "wham/usage token_invalid",
            "checkout failed 401",
            "checkout/update failed 401",
            "checkout/taxes failed 401",
            "approve failed 401",
            "token expired",
            "already paid",
            "already subscribed",
            "already has plus",
            "active subscription",
            "no trial",
            "not trial",
        )
    )


def is_checkout_shape_error(reason: str) -> bool:
    return "checkout_not_kakao_trial" in str(reason or "").lower()


def record_seed_success(proxy_seed: str) -> None:
    if not proxy_chain_key(proxy_seed):
        return
    record = seed_record(proxy_seed)
    record["success"] = int(record.get("success") or 0) + 1
    record["fail"] = 0
    record["last_success"] = int(time.time())
    record["last_reason"] = "success"
    save_proxy_state()


def record_seed_failure(proxy_seed: str, reason: str) -> str:
    """Persist a failure without treating account or checkout-shape errors as proxy faults."""
    if not proxy_chain_key(proxy_seed) or is_account_error(reason) or is_checkout_shape_error(reason):
        return "kept"
    record = seed_record(proxy_seed)
    record["fail"] = int(record.get("fail") or 0) + 1
    record["last_fail"] = int(time.time())
    record["last_reason"] = redact_log_text(str(reason or "failed"))[:240]
    if is_direct_proxy_error(reason) or "出口国家" in reason:
        record["removed"] = True
        save_proxy_state()
        return "removed" if remove_seed(proxy_seed, reason) else "kept"
    remove_after = env_int("KAKAO_PROXY_REMOVE_AFTER_FAILS", 3, minimum=1, maximum=100)
    if is_proxy_health_error(reason) and int(record.get("fail") or 0) >= remove_after:
        record["removed"] = True
        save_proxy_state()
        return "removed" if remove_seed(proxy_seed, reason) else "kept"
    save_proxy_state()
    return "cooling"


def load_proxy_seeds() -> list[str]:
    path = proxy_seed_file()
    if not path.is_file():
        raise RuntimeError("代理 Seed 文件不存在")
    unique: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        register_proxy_for_redaction(line)
        proxy = normalize_proxy_url(line)
        key = proxy_chain_key(proxy)
        if proxy and key and key not in seen:
            seen.add(key)
            unique.append(proxy)
    if not unique:
        raise RuntimeError("代理 Seed 为空")

    now = int(time.time())
    usable: list[str] = []
    skipped = 0
    for proxy in unique:
        record = seed_record(proxy)
        if record.get("removed") or record_in_cooldown(record, now):
            skipped += 1
            continue
        usable.append(proxy)
    if not usable:
        raise RuntimeError("代理 Seed 已全部处于失败冷却或已移除")
    random.shuffle(usable)
    usable.sort(
        key=lambda proxy: (
            int(seed_record(proxy).get("success") or 0),
            int(seed_record(proxy).get("last_success") or 0),
        ),
        reverse=True,
    )
    log(f"加载代理 Seed {len(usable)} 条，冷却/移除跳过 {skipped} 条")
    log(
        "地区链路: 一份 Seed 派生 "
        f"{CHECKOUT_COUNTRY} checkout/Bootstrap -> {PROMOTION_COUNTRY} checkout/update -> "
        f"{PROVIDER_COUNTRY} Stripe/Kakao/approve"
    )
    return usable


def extract_access_token(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if value.startswith("{") or value.startswith("["):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for name in ("accessToken", "access_token", "token", "bearerToken"):
                candidate = str(payload.get(name) or "").strip()
                if candidate:
                    return candidate.removeprefix("Bearer ").strip()
    first = value.splitlines()[0].strip()
    return first.removeprefix("Bearer ").strip()


def load_token() -> str:
    for name in ("KAKAO_TOKEN", "PP_TOKEN", "IDEAL_TOKEN"):
        token = extract_access_token(os.environ.get(name, ""))
        if token:
            return token
    path = SCRIPT_DIR / "token.txt"
    if path.is_file():
        return extract_access_token(path.read_text(encoding="utf-8", errors="ignore"))
    return ""


def token_account(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:10]
    return f"token#{digest}"


def random_kakao_billing(token: str) -> dict[str, str]:
    seed = hashlib.sha256(f"{token}:{uuid.uuid4()}".encode()).digest()
    rng = random.Random(seed)
    address = rng.choice(SEOUL_ADDRESS_SEEDS)
    name = f"{rng.choice(KOREAN_FAMILY_NAMES)}{rng.choice(KOREAN_GIVEN_NAMES)}"
    local_name = hashlib.sha256(name.encode()).hexdigest()[:10]
    return {
        "name": name,
        "email": f"{local_name}@{rng.choice(EMAIL_DOMAINS)}",
        "line1": f"{address['road']} {address['base'] + rng.randrange(address['span'])}",
        "line2": "",
        "city": "서울특별시",
        "state": str(address["district"]),
        "postal_code": str(address["postal"]),
        "country": PROVIDER_COUNTRY,
    }


def new_session(proxy: str) -> Any:
    register_proxy_for_redaction(proxy)
    if CurlCffiSession is not None:
        session: Any = CurlCffiSession(impersonate="chrome136")
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy}
    return session


def extract_ip_country(source: str, payload: dict[str, Any]) -> tuple[str, str]:
    if source == "ipinfo":
        return str(payload.get("ip") or ""), str(payload.get("country") or "").upper()
    if source == "ipapi":
        return (
            str(payload.get("ip") or ""),
            str(payload.get("country_code") or payload.get("country") or "").upper(),
        )
    if source == "ipwho":
        if payload.get("success") is False:
            return str(payload.get("ip") or ""), ""
        return str(payload.get("ip") or ""), str(payload.get("country_code") or "").upper()
    if source == "myip":
        return str(payload.get("ip") or ""), str(payload.get("cc") or payload.get("country") or "").upper()
    return "", ""


def ip_info(proxy: str) -> dict[str, str]:
    session = new_session(proxy)
    failures: list[str] = []
    for source, url in IP_CHECK_SOURCES:
        try:
            response = session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=PREFLIGHT_TIMEOUT,
            )
            if int(getattr(response, "status_code", 599)) >= 400:
                failures.append(f"{source} HTTP {getattr(response, 'status_code', 599)}")
                continue
            payload = response.json() or {}
            if not isinstance(payload, dict):
                failures.append(f"{source} invalid response")
                continue
            ip, country = extract_ip_country(source, payload)
            if country:
                return {"ip": ip, "country": country}
            failures.append(f"{source} no country")
        except Exception as exc:
            failures.append(f"{source} {str(exc)[:80]}")
    raise RuntimeError("出口 IP 查询失败：" + "；".join(failures[:4]))


def preflight_proxy(proxy: str, role: str) -> tuple[bool, str]:
    expected = role_country(role)
    try:
        country = str(ip_info(proxy).get("country") or "").upper()
    except Exception as exc:
        return False, str(exc)[:180]
    if country != expected:
        return False, f"出口国家 {country or 'UNKNOWN'}，要求 {expected}"
    return True, country


def select_verified_seed(
    proxy_seeds: list[str],
    attempted_keys: set[str],
) -> tuple[str, str, str, str] | None:
    """Select one Seed for the configured checkout -> promotion -> provider chain."""
    while True:
        now = int(time.time())
        candidates = [
            seed
            for seed in proxy_seeds
            if proxy_chain_key(seed) not in attempted_keys
            and not seed_record(seed).get("removed")
            and not record_in_cooldown(seed_record(seed), now)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda seed: (
                int(seed_record(seed).get("success") or 0),
                int(seed_record(seed).get("last_success") or 0),
            ),
            reverse=True,
        )
        proxy_seed = candidates[0]
        attempted_keys.add(proxy_chain_key(proxy_seed))
        try:
            checkout_proxy, promotion_proxy, provider_proxy = kakao_proxy_chain(proxy_seed)
        except Exception as exc:
            reason = str(exc)
            state = record_seed_failure(proxy_seed, reason)
            state_text = "已移除" if state == "removed" else ("进入冷却" if state == "cooling" else "保留")
            log(f"Kakao Seed {proxy_label(proxy_seed)} 无法派生，{state_text}: {reason[:180]}", "[WARN] ")
            continue

        checked: set[str] = set()
        preflight_error = ""
        for role, proxy in (
            ("checkout", checkout_proxy),
            ("promotion", promotion_proxy),
            ("provider", provider_proxy),
        ):
            if proxy in checked:
                continue
            checked.add(proxy)
            ok, detail = preflight_proxy(proxy, role)
            if ok:
                log(f"{role_label(role)} {proxy_label(proxy)} 出口预检通过：{detail}")
                continue
            preflight_error = f"{role_label(role)} 出口预检失败: {detail}"
            break
        if not preflight_error:
            return proxy_seed, checkout_proxy, promotion_proxy, provider_proxy

        state = record_seed_failure(proxy_seed, preflight_error)
        state_text = "已移除" if state == "removed" else ("进入冷却" if state == "cooling" else "保留")
        log(f"Kakao Seed {proxy_label(proxy_seed)} {state_text}: {preflight_error[:180]}", "[WARN] ")


def response_error(response: Any, limit: int = 800) -> str:
    try:
        return redact_log_text(str(response.text or ""))[:limit]
    except Exception:
        return ""


def stripe_headers(publishable_key: str, referer: str) -> dict[str, str]:
    origin = "https://checkout.stripe.com" if "checkout.stripe.com" in referer else "https://pay.openai.com"
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Origin": origin,
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Sec-Fetch-Site": "same-site" if origin == "https://checkout.stripe.com" else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }


def elements_params(stripe_js_id: str, session_id: str = "") -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "ko",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def create_checkout(session: Any, token: str) -> tuple[str, str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": CHECKOUT_COUNTRY, "currency": "KRW"},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
    }
    promo_mode = os.environ.get("KAKAO_PROMO_MODE", "campaign").strip().lower()
    promo_id = os.environ.get("KAKAO_PROMO_ID", "plus-1-month-free").strip()
    if promo_mode != "off" and promo_id:
        payload["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": False,
        }
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "oai-language": "ko-KR",
            "User-Agent": USER_AGENT,
        },
        json=payload,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"checkout failed {response.status_code}: {response_error(response)}")
    checkout = response.json() or {}
    checkout_session = str(checkout.get("checkout_session_id") or "")
    publishable_key = str(checkout.get("publishable_key") or "")
    if not checkout_session or not publishable_key:
        raise RuntimeError(f"checkout missing cs/pk: {list(checkout.keys())}")
    return checkout_session, publishable_key, checkout


def checkout_processor_entity(checkout: dict[str, Any]) -> str:
    return str(checkout.get("processor_entity") or "openai_llc")


def checkout_page_url(checkout_id: str, checkout: dict[str, Any]) -> str:
    return f"https://chatgpt.com/checkout/{checkout_processor_entity(checkout)}/{checkout_id}"


def checkout_api_headers(token: str, referer: str, target_path: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "oai-language": "ko-KR",
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }


def update_checkout_promotion(session: Any, token: str, checkout_id: str, checkout: dict[str, Any]) -> None:
    promo_mode = os.environ.get("KAKAO_PROMO_MODE", "campaign").strip().lower()
    promo_id = os.environ.get("KAKAO_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "checkout_session_id": checkout_id,
        "processor_entity": checkout_processor_entity(checkout),
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
    }
    if promo_mode != "off" and promo_id:
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": False,
        }
    target_path = "/backend-api/payments/checkout/update"
    response = session.post(
        f"https://chatgpt.com{target_path}",
        headers=checkout_api_headers(token, checkout_page_url(checkout_id, checkout), target_path),
        json=body,
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout/update failed {response.status_code}: {response_error(response)}")
    try:
        payload = response.json() or {}
    except (TypeError, ValueError):
        payload = {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"checkout/update rejected: {str(payload)[:500]}")
    log(f"{PROMOTION_COUNTRY} checkout/update 成功: promo={promo_id if 'promo_campaign' in body else 'off'}")


def update_kakao_checkout_taxes(
    session: Any,
    token: str,
    checkout_id: str,
    checkout: dict[str, Any],
    billing: dict[str, str],
) -> None:
    target_path = "/backend-api/payments/checkout/taxes"
    body = {
        "checkout_session_id": checkout_id,
        "checkout_email": billing["email"],
        "billing_country": PROVIDER_COUNTRY,
        "billing_name": billing["name"],
        "currency": "KRW",
        "tax_id": None,
        "processor_entity": checkout_processor_entity(checkout),
        "billing_address": {
            "line1": billing["line1"],
            "city": billing["city"],
            "country": PROVIDER_COUNTRY,
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    response = session.post(
        f"https://chatgpt.com{target_path}",
        headers=checkout_api_headers(token, checkout_page_url(checkout_id, checkout), target_path),
        json=body,
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout/taxes failed {response.status_code}: {response_error(response)}")
    log(f"{PROVIDER_COUNTRY} checkout/taxes 同步成功")


def expected_amount(payload: dict[str, Any]) -> str:
    options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    if options.get("amount") is not None:
        return str(int(options["amount"]))
    total_summary = payload.get("total_summary") if isinstance(payload.get("total_summary"), dict) else {}
    if total_summary.get("due") is not None:
        return str(int(total_summary["due"]))
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    for name in ("amount_due", "total"):
        if invoice.get(name) is not None:
            return str(int(invoice[name]))
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        amounts = [item.get("amount") for item in line_items if isinstance(item, dict) and item.get("amount") is not None]
        if amounts:
            return str(sum(int(value) for value in amounts))
    return "unknown"


def activate_stripe_checkout(session: Any, checkout_id: str) -> str:
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    for url in (f"https://pay.openai.com/c/pay/{checkout_id}", checkout_page):
        session.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,*/*",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                "Referer": "https://chatgpt.com/",
            },
            timeout=TIMEOUT,
        )
    return checkout_page


def stripe_init(
    session: Any,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
) -> tuple[dict[str, Any], str]:
    stripe_js_id = str(uuid.uuid4())
    init_body = {
        "key": publishable_key,
        "eid": "NA",
        "browser_locale": "ko-KR",
        "browser_timezone": "Asia/Seoul",
        "redirect_type": "url",
        "_stripe_version": STRIPE_VERSION,
        **elements_params(stripe_js_id),
    }
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        data=init_body,
        headers=stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"stripe init failed {response.status_code}: {response_error(response)}")
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise RuntimeError("stripe init returned invalid payload")
    return payload, stripe_js_id


def inspect_kakao_init(
    payload: dict[str, Any],
    stage: str,
    *,
    require_zero: bool,
    require_kakao: bool = True,
) -> str:
    amount = expected_amount(payload)
    currency = str(payload.get("currency") or "").lower()
    methods = [str(item).lower() for item in (payload.get("payment_method_types") or [])]
    log(f"{stage} Stripe init: amount={amount}; currency={currency}; methods={','.join(methods) or 'none'}")
    if (require_kakao and "kakao_pay" not in methods) or (
        require_zero and (amount != "0" or currency != "krw")
    ):
        raise RuntimeError(
            f"checkout_not_kakao_trial: stage={stage} amount={amount} currency={currency} methods={methods}"
        )
    return amount


def stripe_update_kakao_tax_region(
    session: Any,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    stripe_js_id: str,
    elements_session_id: str,
    billing: dict[str, str],
) -> None:
    body = {
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
        **elements_params(stripe_js_id, elements_session_id),
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
        "tax_region[state]": billing["state"],
    }
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
        data=body,
        headers=stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"tax_region failed {response.status_code}: {response_error(response)}")
    log(f"{PROVIDER_COUNTRY} Stripe tax_region 同步成功: {billing['city']} {billing['postal_code']}")


def extract_redirect(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    action = payload.get("next_action")
    if isinstance(action, dict) and action.get("type") == "redirect_to_url":
        redirect = action.get("redirect_to_url") or {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    for name in ("setup_intent", "payment_intent"):
        redirect = extract_redirect(payload.get(name))
        if redirect:
            return redirect
    return ""


def ensure_running(stop_event: Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise TaskStopped("任务已停止")


def probe_kakao_access_token(token: str, proxy: str) -> dict[str, Any]:
    return probe_account_liveness(
        {"access_token": token},
        proxy=proxy,
        timeout=TIMEOUT,
    )


def validate_kakao_access_token(token: str, proxy: str) -> dict[str, Any]:
    probe = probe_kakao_access_token(token, proxy)
    probe_status = int(probe.get("status_code") or 0)
    detail = str(probe.get("error") or probe.get("quota_status") or "unknown")
    if probe_status == 401:
        raise RuntimeError(f"wham/usage failed 401: {detail}")
    if str(probe.get("status") or "") == "token_invalid":
        raise RuntimeError(f"wham/usage token_invalid: {detail}")
    if not 200 <= probe_status < 300:
        # 403, rate limits, and network failures are inconclusive. Let the real
        # checkout operation decide whether the account/payment path can proceed.
        log(f"[WARN] wham/usage probe inconclusive ({probe_status or 'network'}): {detail}; continuing checkout")
    return probe


def kakao_link(
    token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    *,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Keep one sticky Seed across configured bootstrap, promotion, and provider stages."""
    ensure_running(stop_event)
    checkout_session = new_session(checkout_proxy)
    promotion_session = new_session(promotion_proxy)
    provider_session = new_session(provider_proxy)

    log("校验 ChatGPT Token")
    validate_kakao_access_token(token, checkout_proxy)

    ensure_running(stop_event)
    log(f"{CHECKOUT_COUNTRY} 创建 KRW Kakao trial checkout")
    checkout_id, publishable_key, checkout = create_checkout(checkout_session, token)
    checkout_page = activate_stripe_checkout(checkout_session, checkout_id)

    log(f"{CHECKOUT_COUNTRY} Bootstrap Stripe init")
    bootstrap_payload, _ = stripe_init(checkout_session, checkout_id, publishable_key, checkout_page)
    inspect_kakao_init(
        bootstrap_payload,
        f"{CHECKOUT_COUNTRY} Bootstrap",
        require_zero=False,
        require_kakao=CHECKOUT_COUNTRY == PROVIDER_COUNTRY,
    )

    ensure_running(stop_event)
    log(f"{PROMOTION_COUNTRY} checkout/update")
    update_checkout_promotion(promotion_session, token, checkout_id, checkout)

    ensure_running(stop_event)
    log(f"{PROMOTION_COUNTRY} checkout/update 后通过 {PROVIDER_COUNTRY} 刷新 Stripe")
    init_payload, stripe_js_id = stripe_init(provider_session, checkout_id, publishable_key, checkout_page)
    amount = inspect_kakao_init(
        init_payload, f"{PROMOTION_COUNTRY} 更新后 {PROVIDER_COUNTRY}", require_zero=True
    )

    billing = random_kakao_billing(token)
    tax_elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    ensure_running(stop_event)
    log(f"同步 {PROVIDER_COUNTRY} checkout/taxes 与 Stripe tax_region")
    update_kakao_checkout_taxes(provider_session, token, checkout_id, checkout, billing)
    stripe_update_kakao_tax_region(
        provider_session,
        checkout_id,
        publishable_key,
        checkout_page,
        stripe_js_id,
        tax_elements_session_id,
        billing,
    )

    ensure_running(stop_event)
    log(f"{PROVIDER_COUNTRY} 税务同步后刷新 Stripe")
    init_payload, stripe_js_id = stripe_init(provider_session, checkout_id, publishable_key, checkout_page)
    amount = inspect_kakao_init(init_payload, f"{PROVIDER_COUNTRY} 税务同步", require_zero=True)
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"

    ensure_running(stop_event)
    log(f"{PROVIDER_COUNTRY} Stripe pre_confirm Kakao")
    pre_confirm = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/pre_confirm",
        data={
            "eid": str(uuid.uuid4()),
            "payment_method_type": "kakao_pay",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
        },
        headers=stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if pre_confirm.status_code != 200:
        raise RuntimeError(f"pre_confirm failed {pre_confirm.status_code}: {response_error(pre_confirm)}")

    ensure_running(stop_event)
    log(f"{PROVIDER_COUNTRY} 创建 Kakao payment_method")
    client_session_id = str(uuid.uuid4())
    guid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    muid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    sid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    payment_method_body = {
        "type": "kakao_pay",
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": PROVIDER_COUNTRY,
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": billing["line2"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": STRIPE_VERSION,
        "key": publishable_key,
        "payment_user_agent": STRIPE_PAYMENT_UA,
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        payment_method_body["client_attribution_metadata[checkout_config_id]"] = config_id
    payment_method_response = provider_session.post(
        "https://api.stripe.com/v1/payment_methods",
        data=payment_method_body,
        headers=stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if payment_method_response.status_code != 200:
        raise RuntimeError(
            f"payment method failed {payment_method_response.status_code}: {response_error(payment_method_response, 1000)}"
        )
    payment_method_id = str((payment_method_response.json() or {}).get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise RuntimeError(f"payment method no id: {response_error(payment_method_response, 500)}")

    ensure_running(stop_event)
    log(f"{PROVIDER_COUNTRY} Stripe confirm")
    processor_entity = str(checkout.get("processor_entity") or "openai_llc")
    success_url = (
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{checkout_id}/success?"
        f"billing_country={PROVIDER_COUNTRY}"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}?returned_from_redirect=true&ui_mode=custom&"
        f"return_url={quote(success_url, safe='')}"
    )
    confirm_body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": amount,
        "tax_id_collection[purchasing_as_business]": "false",
        "expected_payment_method_type": "kakao_pay",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": STRIPE_RUNTIME,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "link_brand": "link",
        **elements_params(stripe_js_id, elements_session_id),
    }
    if config_id:
        confirm_body["client_attribution_metadata[checkout_config_id]"] = config_id
    confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
        data=confirm_body,
        headers=stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if confirm_response.status_code != 200:
        raise RuntimeError(f"confirm failed {confirm_response.status_code}: {response_error(confirm_response, 1000)}")
    confirm_payload = confirm_response.json() or {}
    redirect = extract_redirect(confirm_payload)
    submission = (
        confirm_payload.get("submission_attempt")
        if isinstance(confirm_payload.get("submission_attempt"), dict)
        else {}
    )

    if not redirect and (
        submission.get("state") == "requires_approval" or checkout.get("requires_manual_approval")
    ):
        log(f"{PROVIDER_COUNTRY} OpenAI approve（最多 {APPROVE_RETRY_MAX} 次）")
        last_error = ""
        for index in range(1, APPROVE_RETRY_MAX + 1):
            ensure_running(stop_event)
            approval_response = provider_session.post(
                "https://chatgpt.com/backend-api/payments/checkout/approve",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "oai-language": "ko-KR",
                    "User-Agent": USER_AGENT,
                    "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}",
                },
                json={"checkout_session_id": checkout_id, "processor_entity": processor_entity},
                timeout=TIMEOUT,
            )
            if approval_response.status_code == 200:
                try:
                    if (approval_response.json() or {}).get("result") == "approved":
                        log(f"{PROVIDER_COUNTRY} approve 第 {index} 次成功")
                        last_error = ""
                        break
                except (TypeError, ValueError):
                    pass
            last_error = f"approve failed {approval_response.status_code}: {response_error(approval_response, 500)}"
            if index < APPROVE_RETRY_MAX:
                time.sleep(1)
        if last_error:
            raise RuntimeError(last_error)

    log(f"{PROVIDER_COUNTRY} 轮询 Stripe redirect（最长 {POLL_TIMEOUT}s）")
    poll_params = {"key": publishable_key, **elements_params(stripe_js_id, elements_session_id)}
    deadline = time.time() + POLL_TIMEOUT
    while not redirect and time.time() < deadline:
        ensure_running(stop_event)
        poll_response = provider_session.get(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            params=poll_params,
            headers=stripe_headers(publishable_key, checkout_page),
            timeout=8,
        )
        if poll_response.status_code == 200:
            redirect = extract_redirect(poll_response.json() or {})
        if not redirect:
            time.sleep(1)
    if not redirect:
        raise RuntimeError("redirect url timeout")

    current = redirect
    for _ in range(6):
        ensure_running(stop_event)
        host = urlsplit(current).netloc.lower()
        if "nicepay" in host or "kakao" in host:
            break
        response = provider_session.get(current, allow_redirects=False, timeout=TIMEOUT)
        location = str(response.headers.get("Location") or "")
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            break
        current = urljoin(current, location)
    return {
        "checkout_session_id": checkout_id,
        "payment_method_id": payment_method_id,
        "stripe_redirect_url": redirect,
        "provider_redirect_url": current,
    }


def no_kakao_method_error(reason: str) -> bool:
    text = str(reason or "")
    return (
        "checkout_not_kakao_trial" in text.lower()
        and "amount=0" in text
        and "currency=krw" in text.lower()
        and "kakao_pay" not in text.lower()
    )


def terminal_checkout_shape_error(reason: str) -> bool:
    return is_checkout_shape_error(reason) and not no_kakao_method_error(reason)


def checkout_retry_error(reason: str) -> bool:
    text = str(reason or "").lower()
    if "chatgpt /me failed" in text:
        return not is_account_error(reason)
    return "checkout failed" in text and not is_account_error(reason)


def kakao_result_contract(
    *,
    ok: bool,
    attempts: int,
    error: str = "",
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable, token-free manager contract for every Kakao run."""
    result = dict(result or {})
    text = str(error or "")
    low = text.lower()
    amount_match = re.search(r"amount=(\d+|none)", low)
    currency_match = re.search(r"currency=([a-z]{3}|none)", low)
    methods_match = re.search(r"methods=(\[[^\]]*\]|[^\s]+)", text, re.IGNORECASE)
    amount = None if not amount_match or amount_match.group(1) == "none" else int(amount_match.group(1))
    currency = currency_match.group(1).upper() if currency_match else "KRW"
    methods_text = methods_match.group(1).lower() if methods_match else ""
    has_kakao = True if ok else (
        False if "kakao_pay" not in methods_text and "checkout_not_kakao_trial" in low else None
    )
    if ok:
        decision, stage = "ready", "redirect"
    elif is_account_error(text):
        decision = "account_deactivated" if "deactivat" in low else "credential_invalid"
        stage = "credential"
    elif is_checkout_shape_error(text):
        if amount not in (None, 0):
            decision = "nonzero_offer"
        elif currency != "KRW":
            decision = "wrong_currency"
        else:
            decision = "kakao_not_enabled"
        stage = "stripe_init"
    elif is_proxy_health_error(text) or is_direct_proxy_error(text) or any(x in low for x in ("tls", "timeout", "407")):
        decision, stage = "proxy_or_network_failed", "proxy"
    elif "approve" in low:
        decision, stage = "approve_result_blocked", "approve"
    elif "redirect" in low:
        decision, stage = "redirect_missing", "redirect"
    elif "confirm" in low:
        decision, stage = "confirm_failed", "confirm"
    else:
        decision, stage = "provider_failed", "provider"
    final_url = str(result.get("provider_redirect_url") or "")
    return {
        "ok": bool(ok),
        "payment_method": "kakao",
        "decision": decision,
        "stage": stage,
        "credential_valid": decision not in {"credential_invalid", "account_deactivated"},
        "amount_due": 0 if ok else amount,
        "currency": currency,
        "methods": ["kakao_pay"] if ok else [],
        "has_kakao": has_kakao,
        "url": final_url,
        "provider_redirect_url": final_url,
        "link_type": "kakao_protocol_redirect" if ok else "kakao_protocol",
        "attempts": max(0, int(attempts or 0)),
        "error": "" if ok else text[:600],
    }


def print_kakao_result(contract: dict[str, Any]) -> None:
    print(json.dumps(contract, ensure_ascii=False, separators=(",", ":")), flush=True)


def run_single_seed_mode(token: str, proxy_seeds: list[str]) -> int:
    seeds_per_round = env_int(
        "KAKAO_SEEDS_PER_ROUND",
        env_int("IDEAL_CHECKOUT_RETRY_MAX", 5, minimum=1, maximum=100),
        minimum=1,
        maximum=100,
    )
    max_rounds = env_int(
        "KAKAO_MAX_RETRY",
        env_int("IDEAL_MAX_RETRY", 5, minimum=1, maximum=100),
        minimum=1,
        maximum=100,
    )
    max_attempts = seeds_per_round * max_rounds
    attempted_keys: set[str] = set()
    stop_event = Event()
    last_error = ""
    attempt = 0

    log(
        "开始执行 Kakao 单 Seed 链路："
        f"{CHECKOUT_COUNTRY} checkout/Bootstrap Stripe init -> {PROMOTION_COUNTRY} checkout/update -> "
        f"{PROVIDER_COUNTRY} Stripe refresh/taxes/Kakao/approve/redirect；"
        f"每轮 Seed 尝试数={seeds_per_round}，重试轮数={max_rounds}，"
        f"最多完整链路={max_attempts}（{seeds_per_round} × {max_rounds}）。"
    )
    while attempt < max_attempts:
        candidate = select_verified_seed(proxy_seeds, attempted_keys)
        if candidate is None:
            last_error = (
                f"没有可用的 {CHECKOUT_COUNTRY} -> {PROMOTION_COUNTRY} -> "
                f"{PROVIDER_COUNTRY} 代理 Seed"
            )
            break
        proxy_seed, checkout_proxy, promotion_proxy, provider_proxy = candidate
        attempt += 1
        log(
            f"完整链路 {attempt}/{max_attempts}："
            f"{CHECKOUT_COUNTRY} checkout={proxy_label(checkout_proxy)}；"
            f"{PROMOTION_COUNTRY} promotion={proxy_label(promotion_proxy)}；"
            f"{PROVIDER_COUNTRY} provider/approve={proxy_label(provider_proxy)}"
        )
        try:
            result = kakao_link(
                token,
                checkout_proxy,
                promotion_proxy,
                provider_proxy,
                stop_event=stop_event,
            )
            final_url = str(result.get("provider_redirect_url") or "")
            host = urlsplit(final_url).netloc.lower()
            if "nicepay" not in host and "kakao" not in host:
                raise RuntimeError(f"not kakao/nicepay redirect: {final_url[:180]}")
            record_seed_success(proxy_seed)
            log("Kakao/Nicepay 跳转链接已获取")
            print_kakao_result(kakao_result_contract(ok=True, attempts=attempt, result=result))
            return 0
        except TaskStopped:
            log("任务已停止", "[WARN] ")
            print_kakao_result(kakao_result_contract(ok=False, attempts=attempt, error="task_stopped"))
            return 1
        except Exception as exc:
            error = str(exc)
            last_error = error
            if is_account_error(error):
                log(f"账号不可继续：{error[:240]}", "[ERROR] ")
                print_kakao_result(kakao_result_contract(ok=False, attempts=attempt, error=error))
                return 1
            if is_checkout_shape_error(error):
                print_kakao_result(kakao_result_contract(ok=False, attempts=attempt, error=error))
                return 3
            state = record_seed_failure(proxy_seed, error)
            state_text = "已移除" if state == "removed" else ("进入冷却" if state == "cooling" else "保留")
            if is_checkout_shape_error(error):
                log(
                    "当前 Seed 的 Kakao checkout 未保持支付方式或 0 KRW；"
                    "已废弃本次 Checkout 与 Seed，不计为代理故障，"
                    f"下一个 Seed 将重建完整 {CHECKOUT_COUNTRY} -> {PROMOTION_COUNTRY} -> "
                    f"{PROVIDER_COUNTRY} 链：{error[:260]}",
                    "[WARN] ",
                )
            else:
                log(
                    f"Kakao 单 Seed 链路失败，{state_text}；本任务不会重抽该 Seed: {error[:260]}",
                    "[WARN] ",
                )

    log(f"全部失败: {last_error or '未获取 Kakao/Nicepay 跳转链接'}", "[ERROR] ")
    print_kakao_result(kakao_result_contract(
        ok=False,
        attempts=attempt,
        error=last_error or "kakao_nicepay_redirect_missing",
    ))
    return 1


def main() -> int:
    token = load_token()
    if not token:
        log("access_token 为空", "[ERROR] ")
        print_kakao_result(kakao_result_contract(ok=False, attempts=0, error="missing_access_token"))
        return 1
    log(f"使用 {token_account(token)}")
    try:
        proxy_seeds = load_proxy_seeds()
    except Exception as exc:
        log(str(exc), "[ERROR] ")
        print_kakao_result(kakao_result_contract(ok=False, attempts=0, error=str(exc)))
        return 1
    return run_single_seed_mode(token, proxy_seeds)


if __name__ == "__main__":
    raise SystemExit(main())
