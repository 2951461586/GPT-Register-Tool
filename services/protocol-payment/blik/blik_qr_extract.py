"""
	BLIK 接口提交 / iDEAL 支付页 URL 提取脚本。

说明：
- BLIK 模式通过 Stripe 接口提交 6 位 BLIK Code 和波兰账单资料，不打开支付页面。
- iDEAL 在 Stripe 里通常是银行授权跳转，不一定有二维码指令页。
- 本脚本会提取 iDEAL redirect URL；成功时该 URL 就是最终扫码/授权界面。
- 默认只读取当前项目的 proxy_seeds.txt；每条 Seed 会派生为 PL checkout、Stripe 和 approve 出口。

用法：
  1) 环境变量方式：
     PP_TOKEN="..." python blik_qr_extract.py

  2) 文件方式：
     把 token 放到当前目录 token.txt
     python blik_qr_extract.py

常用环境变量：
  IDEAL_PAYMENT_METHOD=blik   # blik 或 ideal；默认 blik
  IDEAL_BLIK_CODE=            # BLIK 必填：6 位银行码；不填会直接结束任务
  IDEAL_BANK=ing              # iDEAL 可选：指定银行；不填则走银行选择页
  IDEAL_CONFIRM_INLINE_PM=0   # 默认按 gpthel 流程：先创建 PM，再 confirm 引用 PM
  IDEAL_UPDATE_TAX_REGION=0   # 默认不额外提交 tax_region
  IDEAL_CHECKOUT_SNAPSHOT=0   # 默认不额外提交 checkout snapshot
  IDEAL_CHECKOUT_COUNTRY=PL   # BLIK 默认 PL；iDEAL 可设 NL
  IDEAL_MAX_RETRY=900         # 可选；不设置时默认每批 30 轮、最多 30 批
  IDEAL_PROVIDER_PER_CHECKOUT=30
  IDEAL_MAX_ACCOUNT_ATTEMPTS=0   # 显式设为 0 时按代理池总容量跑
  IDEAL_MAX_APPROVE_BLOCKED=900
  IDEAL_WORKERS=30            # 正式流程并发数；默认每批 30 个 provider 同时跑
  IDEAL_WORKERS_MAX=30
  IDEAL_APPROVE_RETRY_MAX=10 # approve 阶段最大重试次数
  IDEAL_APPROVE_STICKY=0     # approve 默认走代理池换出口
  IDEAL_FOLLOW_REDIRECT=1
  IDEAL_REQUIRE_ZERO=1        # 默认强制 0 元，不生成非 0 元链
  IDEAL_DUMP_LIMIT=6000       # 抓包响应保存长度
  IDEAL_PROXY_SKIP_FAILED=1   # 普通流程失败下次软跳过
  IDEAL_PROXY_REMOVE_FAILED=1 # 明确的代理失败会从原代理文件移除
  IDEAL_PROXY_GEO_CHECK=1     # 默认检测代理出口国家
  IDEAL_PROXY_PRECHECK=1      # 默认启动时先预筛代理出口国家
  IDEAL_PROXY_PRECHECK_WORKERS=50
  IDEAL_PROXY_PRECHECK_WORKERS_MAX=50
  IDEAL_PROXY_PRECHECK_TIMEOUT=20
  IDEAL_PROXY_DEFAULT_SCHEME=http # 裸代理默认协议；Mars SOCKS5 可设 socks5h
  IDEAL_PROXY_GEO_USE_PRE_PROXY=0 # 国家检测默认不走本机前置，避免检测到 Clash 出口国家
  IDEAL_PROXY_TARGET_CHECK=1  # 预筛/运行中确认代理能访问 chatgpt.com/api.stripe.com
  IDEAL_PROXY_TARGET_USE_PRE_PROXY=1 # 目标站检测默认走本机前置，匹配正式访问链路
  IDEAL_PROXY_FAIL_COOLDOWN=180 # 失败代理冷却秒数，0 表示按旧逻辑一直跳过
  IDEAL_PROXY_REMOVE_AFTER_FAILS=3 # 已复用代理健康类失败累计 3 次移除；普通代理失败 1 次移除
  IDEAL_ZERO_CACHE=1          # 缓存 checkout 0 元资格结果，用于后续优先调度
  IDEAL_PROXY_SEED_FILE=proxy_seeds.txt
  PP_PROMO_MODE=campaign      # 默认直接走 promo_campaign，避免 coupon 再 fallback 多耗时
  PP_TRIAL_DAYS=30            # 仅 PP_PROMO_MODE=trial/free_trial 时使用
"""

from __future__ import annotations

import json
import hashlib
import os
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, RLock, local
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests

try:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:
    CurlOpt = None
    CurlCffiSession = None


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
DUMP_DIR = SCRIPT_DIR / "dumps"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
DEFAULT_STRIPE_PK = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

COUNTRY_CURRENCY = {
    "NL": "EUR",
    "BE": "EUR",
    "DE": "EUR",
    "FR": "EUR",
    "PL": "PLN",
    "US": "USD",
    "IN": "INR",
    "JP": "JPY",
}

BLIK_PROXY_COUNTRY = "PL"

DEFAULT_IDEAL_BILLING = {
    "email": "redacted@example.invalid",
    "name": "Daan de Vries",
    "country": "NL",
    "line1": "Damrak 1",
    "line2": "",
    "city": "Amsterdam",
    "postal_code": "1012 LG",
    "state": "",
}

DEFAULT_BLIK_BILLING = {
    "email": "redacted@example.invalid",
    "name": "Jan Kowalski",
    "country": "PL",
    "line1": "Marszalkowska 1",
    "line2": "",
    "city": "Warszawa",
    "postal_code": "00-001",
    "state": "",
}

NL_BILLING_NAMES = [
    ("Daan", "de Vries"),
    ("Sem", "Jansen"),
    ("Milan", "Bakker"),
    ("Lars", "Visser"),
    ("Sophie", "Smit"),
    ("Emma", "Meijer"),
    ("Tess", "Mulder"),
    ("Nina", "de Boer"),
]

NL_BILLING_ADDRESSES = [
    ("Damrak 1", "Amsterdam", "1012 LG"),
    ("Kalverstraat 92", "Amsterdam", "1012 PH"),
    ("Coolsingel 40", "Rotterdam", "3011 AD"),
    ("Lijnbaan 50", "Rotterdam", "3012 EP"),
    ("Grote Marktstraat 43", "Den Haag", "2511 BH"),
    ("Oudegracht 120", "Utrecht", "3511 AW"),
    ("Stationsplein 1", "Eindhoven", "5611 AB"),
    ("Vismarkt 10", "Groningen", "9711 KV"),
]

PL_BILLING_NAMES = [
    ("Jan", "Kowalski"),
    ("Piotr", "Nowak"),
    ("Anna", "Wisniewska"),
    ("Katarzyna", "Wojcik"),
    ("Tomasz", "Kaminski"),
    ("Marta", "Lewandowska"),
]

PL_BILLING_ADDRESSES = [
    ("Marszalkowska 1", "Warszawa", "00-001"),
    ("Nowy Swiat 15", "Warszawa", "00-029"),
    ("Florianska 10", "Krakow", "31-021"),
    ("Dluga 5", "Gdansk", "80-827"),
    ("Piotrkowska 20", "Lodz", "90-269"),
    ("Rynek 1", "Wroclaw", "50-101"),
]

EMAIL_DOMAINS = ("gmail.com", "outlook.com", "icloud.com", "hotmail.com")

_log_file = LOG_DIR / f"blik_{time.strftime('%Y%m%d-%H%M%S')}.log"
_dump_counter = 0
_proxy_state: dict[str, Any] | None = None
_proxy_state_lock = RLock()
_log_lock = RLock()
_dump_lock = RLock()
_proxy_file_lock = RLock()
_proxy_redaction_lock = RLock()
_proxy_redaction_values: set[str] = set()
_log_context = local()


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
    context = getattr(_log_context, "prefix", "")
    line = redact_log_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{context}{message}")
    with _log_lock:
        print(line, flush=True)
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def is_checkout_not_active_error(value: Any) -> bool:
    return "checkout_not_active_session" in str(value)


def is_user_already_paid_error(value: Any) -> bool:
    return "user is already paid" in str(value or "").lower()


def random_user_agent() -> str:
    major = random.randint(122, 149)
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def random_runtime_version() -> str:
    return os.environ.get("PP_RUNTIME_VERSION", "").strip() or DEFAULT_STRIPE_RUNTIME_VERSION


def stripe_browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def build_email(first_name: str, last_name: str) -> str:
    first = re.sub(r"[^a-z]", "", first_name.lower())
    last = re.sub(r"[^a-z]", "", last_name.lower())
    suffix = random.randint(10000, 999999)
    domain = random.choice(EMAIL_DOMAINS)
    if random.random() < 0.5:
        local = f"{first}.{last}{suffix}"
    else:
        local = f"{first}{last}{suffix}"
    return f"{local}@{domain}"


def normalize_country(country: str) -> str:
    value = str(country or "").strip().upper()
    return value if value in COUNTRY_CURRENCY else default_payment_country()


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(normalize_country(country), "EUR")


def payment_browser_locale() -> str:
    default_locale = "pl-PL" if payment_method_type() == "blik" else "en-US"
    return os.environ.get("IDEAL_BROWSER_LOCALE", default_locale).strip() or default_locale


def payment_elements_locale() -> str:
    return os.environ.get("IDEAL_ELEMENTS_LOCALE", payment_browser_locale()).strip() or payment_browser_locale()


def payment_browser_timezone() -> str:
    default_timezone = "Europe/Warsaw" if payment_method_type() == "blik" else "Europe/Amsterdam"
    return os.environ.get("IDEAL_BROWSER_TIMEZONE", default_timezone).strip() or default_timezone


def saved_payment_value() -> str:
    return os.environ.get("IDEAL_SAVED_PAYMENT_VALUE", "never").strip() or "never"


def payment_method_type() -> str:
    value = os.environ.get("IDEAL_PAYMENT_METHOD", "blik").strip().lower()
    return value if value in {"ideal", "blik"} else "ideal"


def payment_method_label() -> str:
    return "BLIK" if payment_method_type() == "blik" else "iDEAL"


def default_payment_country() -> str:
    return "PL" if payment_method_type() == "blik" else "NL"


def default_checkout_proxy_country() -> str:
    return BLIK_PROXY_COUNTRY if payment_method_type() == "blik" else "JP"


def default_provider_proxy_countries() -> str:
    return "PL" if payment_method_type() == "blik" else "NL,BE,DE,FR,IE,ES,IT,AT,LU"


def provider_country_label() -> str:
    label = os.environ.get("IDEAL_PROVIDER_COUNTRY_LABEL", "").strip()
    if label:
        return label
    raw = os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRIES", "").strip()
    if raw:
        return raw
    raw = os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRY", "").strip()
    if raw:
        return raw
    return default_provider_proxy_countries()


def print_result_url(url: str) -> None:
    if payment_method_type() == "blik" and os.environ.get("IDEAL_BLIK_CODE", "").strip():
        print("BLIK 自动提交完成")
        return
    print(f"{payment_method_label()} 支付页 URL:\n{url}")


def validate_blik_code_before_start() -> bool:
    if payment_method_type() != "blik":
        return True
    code = os.environ.get("IDEAL_BLIK_CODE", "").strip()
    if not code:
        log("BLIK Code 为空，任务已结束；请先填写 6 位 BLIK Code", "[ERROR] ")
        return False
    if not re.fullmatch(r"\d{6}", code):
        log("BLIK Code 必须是6位数字，任务已结束", "[ERROR] ")
        return False
    return True


def payment_accept_language() -> str:
    locale = payment_browser_locale()
    if locale.lower().startswith("en"):
        return "en-US,en;q=0.9"
    return f"{locale},{locale.split('-', 1)[0]};q=0.9,en;q=0.8"


def normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        parts = proxy.split(":")
        if len(parts) == 4 and parts[3].isdigit() and not parts[1].isdigit():
            username, password, hostname, port = parts
            username = quote(unquote(username), safe="-._~")
            password = quote(unquote(password), safe="-._~")
            proxy = f"{default_proxy_scheme()}://{username}:{password}@{hostname}:{port}"
        else:
            proxy = f"{default_proxy_scheme()}://{proxy}"

    parsed = urlsplit(proxy)
    if parsed.username is None and parsed.password is None:
        return proxy

    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    username = quote(unquote(parsed.username or ""), safe="-._~")
    auth = username
    if parsed.password is not None:
        auth = f"{auth}:{quote(unquote(parsed.password), safe='-._~')}"
    return urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))


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


def default_proxy_scheme() -> str:
    raw = os.environ.get("IDEAL_PROXY_DEFAULT_SCHEME", "http").strip().lower()
    raw = raw[:-3] if raw.endswith("://") else raw
    if raw in ("socks5", "socks5h"):
        return "socks5h"
    if raw in ("http", "https"):
        return raw
    return "http"


def proxy_short(proxy: str) -> str:
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return "direct"
    digest = hashlib.sha256(proxy.encode()).hexdigest()[:10]
    return f"proxy#{digest}"


def proxy_label(proxy: str) -> str:
    return proxy_short(proxy)


def proxy_key(proxy: str) -> str:
    proxy = normalize_proxy_url(proxy)
    return hashlib.sha256(proxy.encode()).hexdigest() if proxy else ""


_PROXY_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)


def proxy_chain_key(proxy: str) -> str:
    """Return one stable identity for every country derived from a sticky seed."""
    proxy = unquote(normalize_proxy_url(proxy))
    normalized = _PROXY_COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}*",
        proxy,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:10] if normalized else ""


def proxy_for_country(proxy: str, country: str) -> str:
    """Rewrite only the country selector and retain the source sticky session."""
    proxy = normalize_proxy_url(proxy)
    target_country = normalize_country(country).lower()
    if not proxy:
        raise RuntimeError("代理为空，无法派生地区链路")

    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
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

    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = quote(username, safe="-._~")
    if parsed.password is not None:
        auth = f"{auth}:{quote(password, safe='-._~')}"
    derived = urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))
    register_proxy_for_redaction(derived)
    return derived


def blik_proxy_chain(proxy_seed: str) -> tuple[str, str]:
    """Use one sticky seed for all BLIK stages, with a PL country selector."""
    checkout_proxy = proxy_for_country(proxy_seed, BLIK_PROXY_COUNTRY)
    provider_proxy = proxy_for_country(proxy_seed, BLIK_PROXY_COUNTRY)
    chain_key = proxy_chain_key(proxy_seed)
    if not chain_key or any(proxy_chain_key(proxy) != chain_key for proxy in (checkout_proxy, provider_proxy)):
        raise RuntimeError("代理地区改写改变了 sticky seed，已拒绝混用代理链")
    return checkout_proxy, provider_proxy


def log_blik_proxy_chain(proxy_seed: str, checkout_proxy: str, provider_proxy: str) -> None:
    log(
        "派生代理链: "
        f"chain={proxy_chain_key(proxy_seed)}; seed={proxy_label(proxy_seed)}; "
        f"PL checkout={proxy_label(checkout_proxy)}; "
        f"PL Stripe/approve={proxy_label(provider_proxy)}"
    )


def normalize_pre_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"socks5h://{proxy}"
    return normalize_proxy_url(proxy)


def proxy_state_path() -> Path:
    raw = os.environ.get("IDEAL_PROXY_STATE_FILE", "").strip()
    return Path(raw) if raw else SCRIPT_DIR / "proxy_state.json"


def load_proxy_state() -> dict[str, Any]:
    global _proxy_state
    with _proxy_state_lock:
        if _proxy_state is not None:
            return _proxy_state
        path = proxy_state_path()
        if not path.exists():
            _proxy_state = {"seed": {}, "checkout": {}, "provider": {}, "pair": {}}
            return _proxy_state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("seed", {})
        data.setdefault("checkout", {})
        data.setdefault("provider", {})
        data.setdefault("pair", {})
        _proxy_state = data
        return _proxy_state


def save_proxy_state() -> None:
    with _proxy_state_lock:
        if _proxy_state is None:
            return
        path = proxy_state_path()
        path.write_text(json.dumps(_proxy_state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def proxy_state_key(group: str, proxy: str) -> str:
    return proxy_chain_key(proxy) if group == "seed" else proxy_key(proxy)


def prune_proxy_state(checkout_proxies: list[str], provider_proxies: list[str]) -> None:
    removed_counts: dict[str, int] = {}
    with _proxy_state_lock:
        state = load_proxy_state()
        active_keys_by_group: dict[str, set[str]] = {}
        for group, proxies in (("checkout", checkout_proxies), ("provider", provider_proxies)):
            group_state = state.get(group)
            if not isinstance(group_state, dict):
                continue
            active_keys = {proxy_key(proxy) for proxy in proxies if proxy}
            active_keys_by_group[group] = active_keys
            stale_keys = [key for key in group_state if key not in active_keys]
            for key in stale_keys:
                del group_state[key]
            if stale_keys:
                removed_counts[group] = len(stale_keys)
        pair_state = state.get("pair")
        if isinstance(pair_state, dict):
            active_checkout = active_keys_by_group.get("checkout", set())
            active_provider = active_keys_by_group.get("provider", set())
            stale_pair_keys = [
                key
                for key, record in pair_state.items()
                if not isinstance(record, dict)
                or record.get("checkout") not in active_checkout
                or record.get("provider") not in active_provider
            ]
            for key in stale_pair_keys:
                del pair_state[key]
            if stale_pair_keys:
                removed_counts["pair"] = len(stale_pair_keys)
        if removed_counts:
            save_proxy_state()
    if removed_counts:
        summary = ", ".join(f"{group}={count}" for group, count in removed_counts.items())
        log(f"代理状态清理完成: {summary}")


def prune_proxy_seed_state(proxy_seeds: list[str]) -> None:
    with _proxy_state_lock:
        state = load_proxy_state()
        seed_state = state.setdefault("seed", {})
        active_keys = {proxy_chain_key(proxy) for proxy in proxy_seeds if proxy_chain_key(proxy)}
        stale_keys = [key for key in seed_state if key not in active_keys]
        for key in stale_keys:
            del seed_state[key]
        if stale_keys:
            save_proxy_state()
    if stale_keys:
        log(f"Seed 代理状态清理完成: {len(stale_keys)}")


def proxy_record(group: str, proxy: str) -> dict[str, Any]:
    with _proxy_state_lock:
        state = load_proxy_state()
        group_state = state.setdefault(group, {})
        key = proxy_state_key(group, proxy)
        if not key:
            return {}
        record = group_state.setdefault(key, {})
        record.setdefault("success", 0)
        record.setdefault("fail", 0)
        return record


def proxy_pair_key(checkout_proxy: str, provider_proxy: str) -> str:
    checkout_key = proxy_key(checkout_proxy)
    provider_key = proxy_key(provider_proxy)
    return f"{checkout_key}:{provider_key}" if checkout_key and provider_key else ""


def clean_country_code(value: str) -> str:
    return re.sub(r"[^A-Z]", "", str(value or "").upper())[:2]


def proxy_country_cache_ttl() -> int:
    return env_int("IDEAL_PROXY_GEO_CACHE_TTL", 3600)


def geo_lookup_urls() -> list[tuple[str, str]]:
    return [
        ("ip-api", "http://ip-api.com/json/?fields=status,countryCode,as,message"),
        ("ipwho", "https://ipwho.is/?fields=success,country_code,connection,message"),
        ("ipapi", "https://ipapi.co/json/"),
    ]


def parse_geo_country(source: str, payload: dict[str, Any]) -> tuple[str, str]:
    if source == "ip-api":
        if payload.get("status") != "success":
            return "", str(payload.get("message") or "")
        return clean_country_code(str(payload.get("countryCode") or "")), str(payload.get("as") or "")
    if source == "ipwho":
        if payload.get("success") is False:
            return "", str(payload.get("message") or "")
        connection = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}
        return clean_country_code(str(payload.get("country_code") or "")), str(connection.get("asn") or "")
    if source == "ipapi":
        error = payload.get("error")
        if error:
            return "", str(payload.get("reason") or payload.get("message") or error)
        return clean_country_code(str(payload.get("country_code") or "")), str(payload.get("org") or "")
    return "", ""


def lookup_proxy_country(group: str, proxy: str, timeout: int | None = None) -> tuple[str, str, str]:
    record = proxy_record(group, proxy)
    now = int(time.time())
    use_pre_proxy = env_bool("IDEAL_PROXY_GEO_USE_PRE_PROXY", False)
    cached_country = clean_country_code(str(record.get("country") or ""))
    checked_at = int(record.get("country_checked_at") or 0)
    cache_matches = "country_pre_proxy" in record and bool(record.get("country_pre_proxy")) == use_pre_proxy
    if cached_country and cache_matches and now - checked_at <= proxy_country_cache_ttl():
        return cached_country, str(record.get("country_as") or ""), "cache"

    last_error = ""
    request_timeout = timeout or env_int("IDEAL_PROXY_GEO_TIMEOUT", 15)
    session = new_session(proxy, use_pre_proxy=use_pre_proxy)
    for source, url in geo_lookup_urls():
        try:
            resp = session.get(url, timeout=request_timeout)
            if resp.status_code != 200:
                last_error = f"{source}:HTTP_{resp.status_code}"
                continue
            payload = resp.json() or {}
            country, asn = parse_geo_country(source, payload)
            if country:
                record["country"] = country
                record["country_as"] = asn
                record["country_source"] = source
                record["country_checked_at"] = now
                record["country_pre_proxy"] = use_pre_proxy
                save_proxy_state()
                return country, asn, source
            last_error = f"{source}:{asn or 'no_country'}"
        except Exception as exc:
            last_error = f"{source}:{str(exc)[:80]}"

    record["country"] = ""
    record["country_error"] = redact_log_text(last_error)
    record["country_checked_at"] = now
    record["country_pre_proxy"] = use_pre_proxy
    save_proxy_state()
    return "", last_error, "error"


def proxy_target_cache_ttl() -> int:
    return env_int("IDEAL_PROXY_TARGET_CACHE_TTL", 1800, minimum=0)


def target_probe_urls(group: str) -> list[tuple[str, str]]:
    if group == "checkout":
        return [("chatgpt", "https://chatgpt.com/")]
    return [
        ("chatgpt", "https://chatgpt.com/"),
        ("stripe", "https://api.stripe.com/"),
    ]


def target_response_error(resp: Any) -> str:
    headers = getattr(resp, "headers", {}) or {}
    status_code = int(getattr(resp, "status_code", 0) or 0)
    origin = str(headers.get("x-response-origin") or headers.get("X-Response-Origin") or "").lower()
    proxy_auth = str(headers.get("proxy-authenticate") or headers.get("Proxy-Authenticate") or "").lower()
    if "proxy-server" in origin:
        return f"HTTP_{status_code}:proxy-server"
    if status_code == 407 or proxy_auth:
        return f"HTTP_{status_code}:proxy-auth"
    if status_code >= 500:
        return f"HTTP_{status_code}"
    return ""


def lookup_proxy_targets(group: str, proxy: str, timeout: int | None = None) -> tuple[bool, str]:
    record = proxy_record(group, proxy)
    now = int(time.time())
    use_pre_proxy = env_bool("IDEAL_PROXY_TARGET_USE_PRE_PROXY", True)
    checked_at = int(record.get("target_checked_at") or 0)
    cached_ok = record.get("target_ok")
    ttl = proxy_target_cache_ttl()
    cache_matches = "target_pre_proxy" in record and bool(record.get("target_pre_proxy")) == use_pre_proxy
    if isinstance(cached_ok, bool) and cache_matches and checked_at and (ttl <= 0 or now - checked_at <= ttl):
        return cached_ok, "cache" if cached_ok else str(record.get("target_error") or "cache_failed")

    request_timeout = timeout or env_int("IDEAL_PROXY_TARGET_TIMEOUT", env_int("IDEAL_PROXY_PRECHECK_TIMEOUT", 20))
    session = new_session(proxy, use_pre_proxy=use_pre_proxy)
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"})
    last_error = ""
    for name, url in target_probe_urls(group):
        try:
            resp = session.get(url, timeout=request_timeout, allow_redirects=False)
            response_error = target_response_error(resp)
            if response_error:
                last_error = f"{name}:{response_error}"
                break
        except Exception as exc:
            last_error = f"{name}:{str(exc)[:120]}"
            break

    ok = not last_error
    record["target_ok"] = ok
    record["target_error"] = "" if ok else redact_log_text(last_error)
    record["target_checked_at"] = now
    record["target_pre_proxy"] = use_pre_proxy
    save_proxy_state()
    return ok, "ok" if ok else last_error


def expected_proxy_country(group: str) -> str:
    if group == "checkout":
        raw = os.environ.get("IDEAL_CHECKOUT_PROXY_COUNTRY", default_checkout_proxy_country())
    else:
        raw = os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRY", default_payment_country())
    return clean_country_code(raw)


def expected_proxy_countries(group: str) -> set[str]:
    if group == "checkout":
        return {expected_proxy_country(group)}
    raw = os.environ.get(
        "IDEAL_PROVIDER_PROXY_COUNTRIES",
        os.environ.get("IDEAL_PROVIDER_PROXY_COUNTRY", default_provider_proxy_countries()),
    )
    countries = {clean_country_code(item) for item in re.split(r"[,;\s]+", raw) if clean_country_code(item)}
    return countries or {expected_proxy_country(group)}


def format_expected_countries(countries: set[str]) -> str:
    return ",".join(sorted(countries))


def ensure_proxy_country(group: str, proxy: str) -> None:
    if not env_bool("IDEAL_PROXY_GEO_CHECK", True):
        return
    expected = expected_proxy_countries(group)
    if not expected:
        return
    country, asn, source = lookup_proxy_country(group, proxy)
    log(
        f"{group} 出口检测: {proxy_label(proxy)} country={country or 'UNKNOWN'} "
        f"expected={format_expected_countries(expected)} source={source}"
    )
    if not country:
        return
    if country not in expected:
        reason = f"{group} 代理出口国家不符: actual={country}, expected={format_expected_countries(expected)}"
        remove_failed_proxy(group, proxy, reason)
        raise RuntimeError(reason)


def ensure_proxy_targets(group: str, proxy: str) -> None:
    if not env_bool("IDEAL_PROXY_TARGET_CHECK", True):
        return
    ok, reason = lookup_proxy_targets(group, proxy)
    log(f"{group} 目标站检测: {proxy_label(proxy)} reachable={ok} source={reason}")
    if not ok:
        raise RuntimeError(f"{group} 代理目标站不可达: {reason}")


def precheck_proxy_group(group: str, proxies: list[str]) -> list[str]:
    if not env_bool("IDEAL_PROXY_PRECHECK", True):
        return proxies
    geo_enabled = env_bool("IDEAL_PROXY_GEO_CHECK", True)
    target_enabled = env_bool("IDEAL_PROXY_TARGET_CHECK", True) and env_bool("IDEAL_PROXY_TARGET_PRECHECK", True)
    if not geo_enabled and not target_enabled:
        return proxies
    expected = expected_proxy_countries(group)
    if geo_enabled and not expected:
        return proxies

    total = len(proxies)
    requested_workers = env_int("IDEAL_PROXY_PRECHECK_WORKERS", 50)
    worker_limit = env_int("IDEAL_PROXY_PRECHECK_WORKERS_MAX", 50)
    workers = min(requested_workers, worker_limit, total)
    timeout = env_int("IDEAL_PROXY_PRECHECK_TIMEOUT", 20)
    if requested_workers > workers:
        log(f"{group} 代理预筛并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(
        f"{group} 代理预筛开始: total={total}, "
        f"expected={format_expected_countries(expected) if geo_enabled else 'SKIP'}, "
        f"target={'on' if target_enabled else 'off'}, workers={workers}, timeout={timeout}s"
    )

    kept_set: set[str] = set()
    country_failures: list[tuple[str, str]] = []
    target_failures: list[tuple[str, str]] = []
    failed = 0
    unknown = 0

    def check(proxy: str) -> tuple[str, str, str, bool, str]:
        country = ""
        source = "skip"
        if geo_enabled:
            country, _asn, source = lookup_proxy_country(group, proxy, timeout=timeout)
        target_ok = True
        target_reason = "skip"
        if target_enabled and (not geo_enabled or country in expected):
            target_ok, target_reason = lookup_proxy_targets(group, proxy, timeout=timeout)
        return proxy, country, source, target_ok, target_reason

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check, proxy) for proxy in proxies]
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                proxy, country, source, target_ok, target_reason = future.result()
            except Exception:
                failed += 1
                continue
            country_ok = (not geo_enabled) or country in expected
            if country_ok and target_ok:
                kept_set.add(proxy)
            else:
                failed += 1
                if not country_ok and not country:
                    unknown += 1
                elif not country_ok:
                    country_failures.append(
                        (
                            proxy,
                            f"预筛出口国家不符: actual={country}, expected={format_expected_countries(expected)}, source={source}",
                        )
                    )
                else:
                    reason = f"预筛目标站不可达: {target_reason}"
                    target_failures.append((proxy, reason))
                    record_proxy_health_failure(group, proxy, reason)
            if index % 50 == 0 or index == total:
                log(f"{group} 代理预筛进度: {index}/{total}, kept={len(kept_set)}, failed={failed}, unknown={unknown}")

    remove_failed_proxies(group, country_failures)
    if not kept_set:
        failed_set = {proxy for proxy, _reason in country_failures}
        failed_set.update(proxy for proxy, _reason in target_failures)
        remaining = [proxy for proxy in proxies if proxy not in failed_set]
        if remaining:
            log(f"{group} 代理预筛无明确可用结果，仅保留 {len(remaining)} 条出口未知代理继续跑", "[WARN] ")
        return remaining
    kept = [proxy for proxy in proxies if proxy in kept_set]
    log(f"{group} 代理预筛完成: kept={len(kept)}/{total}, removed={total - len(kept)}, unknown={unknown}")
    return kept


def record_proxy_result(group: str, proxy: str, success: bool, reason: str = "") -> dict[str, Any]:
    if not proxy or not env_bool("IDEAL_PROXY_SCORE", True):
        return {}
    record = proxy_record(group, proxy)
    if not record:
        return {}
    now = int(time.time())
    if success:
        record["success"] = int(record.get("success") or 0) + 1
        record["fail"] = 0
        record["last_success"] = now
        record["last_reason"] = "success"
    else:
        record["fail"] = int(record.get("fail") or 0) + 1
        record["last_fail"] = now
        record["last_reason"] = redact_log_text(str(reason or "failed"))[:160]
    save_proxy_state()
    return record


def proxy_remove_after_fails() -> int:
    return env_int("IDEAL_PROXY_REMOVE_AFTER_FAILS", 3)


def is_reused_proxy_record(group: str, record: dict[str, Any]) -> bool:
    if int(record.get("success") or 0) > 0:
        return True
    return group in {"checkout", "seed"} and record.get("zero_ok") is True


def record_proxy_health_failure(group: str, proxy: str, reason: str) -> None:
    record = record_proxy_result(group, proxy, False, reason)
    fail_count = int(record.get("fail") or 0)
    remove_after = proxy_remove_after_fails() if is_reused_proxy_record(group, record) else 1
    if fail_count >= remove_after:
        remove_failed_proxy(group, proxy, reason)


def checkout_zero_cache_ttl() -> int:
    return env_int("IDEAL_ZERO_CACHE_TTL", 86400, minimum=0)


def checkout_zero_cache_status(proxy: str, country: str) -> tuple[str, int, int]:
    if not proxy or not env_bool("IDEAL_ZERO_CACHE", True):
        return "", 0, 0
    record = proxy_record("seed", proxy)
    if not record:
        return "", 0, 0
    checked_at = int(record.get("zero_checked_at") or 0)
    if not checked_at:
        return "", 0, 0
    ttl = checkout_zero_cache_ttl()
    if ttl > 0 and int(time.time()) - checked_at > ttl:
        return "", 0, checked_at
    if normalize_country(str(record.get("zero_country") or country)) != normalize_country(country):
        return "", 0, checked_at
    amount = int(record.get("zero_amount") or 0)
    if record.get("zero_ok") is True:
        return "ok", amount, checked_at
    if record.get("zero_ok") is False:
        return "bad", amount, checked_at
    return "", amount, checked_at


def record_checkout_zero_result(proxy: str, country: str, amount: int) -> None:
    if not proxy or not env_bool("IDEAL_ZERO_CACHE", True):
        return
    record = proxy_record("seed", proxy)
    if not record:
        return
    amount = int(amount or 0)
    record["zero_ok"] = amount == 0
    record["zero_amount"] = amount
    record["zero_country"] = normalize_country(country)
    record["zero_checked_at"] = int(time.time())
    if amount == 0:
        record["zero_success"] = int(record.get("zero_success") or 0) + 1
    save_proxy_state()


def record_proxy_pair_result(checkout_proxy: str, provider_proxy: str, success: bool, reason: str = "") -> None:
    record_proxy_result("checkout", checkout_proxy, success, reason)
    record_proxy_result("provider", provider_proxy, success, reason)
    if not checkout_proxy or not provider_proxy or not env_bool("IDEAL_PROXY_SCORE", True):
        return
    key = proxy_pair_key(checkout_proxy, provider_proxy)
    if not key:
        return
    with _proxy_state_lock:
        state = load_proxy_state()
        pair_state = state.setdefault("pair", {})
        record = pair_state.setdefault(
            key,
            {"checkout": proxy_key(checkout_proxy), "provider": proxy_key(provider_proxy)},
        )
        now = int(time.time())
        if success:
            record["success"] = int(record.get("success") or 0) + 1
            record["last_success"] = now
            record["last_reason"] = "success"
        else:
            record["fail"] = int(record.get("fail") or 0) + 1
            record["last_fail"] = now
            record["last_reason"] = redact_log_text(str(reason or "failed"))[:160]
        save_proxy_state()


def record_proxy_pair_approve_success(checkout_proxy: str, provider_proxy: str, approve_proxy: str) -> None:
    if not checkout_proxy or not provider_proxy or not approve_proxy or not env_bool("IDEAL_PROXY_SCORE", True):
        return
    key = proxy_pair_key(checkout_proxy, provider_proxy)
    approve_key = proxy_key(approve_proxy)
    if not key or not approve_key:
        return
    record_proxy_result("checkout", approve_proxy, True, "approve_success")
    with _proxy_state_lock:
        state = load_proxy_state()
        pair_state = state.setdefault("pair", {})
        record = pair_state.setdefault(
            key,
            {"checkout": proxy_key(checkout_proxy), "provider": proxy_key(provider_proxy)},
        )
        now = int(time.time())
        record["approve"] = approve_key
        record["approve_success"] = int(record.get("approve_success") or 0) + 1
        record["approve_last_success"] = now
        record["approve_last_reason"] = "success"
        save_proxy_state()


def successful_approve_preferences(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    if not env_bool("IDEAL_PROXY_SCORE", True):
        return []
    pair_state = load_proxy_state().get("pair", {})
    if not isinstance(pair_state, dict):
        return []
    record = pair_state.get(proxy_pair_key(checkout_proxy, provider_proxy))
    if not isinstance(record, dict):
        return []
    approve_key = str(record.get("approve") or "")
    if not approve_key:
        return []
    approve_by_key = {proxy_key(proxy): proxy for proxy in approve_pool}
    approve_proxy = approve_by_key.get(approve_key)
    return [approve_proxy] if approve_proxy else []


def record_failure_by_stage(reason: str, checkout_proxy: str, provider_proxy: str) -> None:
    if "checkout 代理出口国家不符" in reason:
        if "actual=UNKNOWN" not in reason:
            remove_failed_proxy("checkout", checkout_proxy, reason)
        else:
            record_proxy_result("checkout", checkout_proxy, False, reason)
        return
    if "checkout 代理目标站不可达" in reason:
        record_proxy_health_failure("checkout", checkout_proxy, reason)
        return
    if "provider 代理出口国家不符" in reason:
        if "actual=UNKNOWN" not in reason:
            remove_failed_proxy("provider", provider_proxy, reason)
        else:
            record_proxy_result("provider", provider_proxy, False, reason)
        return
    if "provider 代理目标站不可达" in reason:
        record_proxy_health_failure("provider", provider_proxy, reason)
        return
    if "checkout 阶段失败" in reason or "checkout 创建失败" in reason:
        if is_direct_remove_proxy_error(reason):
            remove_failed_proxy("checkout", checkout_proxy, reason)
            record_proxy_result("checkout", checkout_proxy, False, reason)
        elif is_proxy_health_failure(reason):
            record_proxy_health_failure("checkout", checkout_proxy, reason)
        return
    if "当前 checkout 不支持" in reason:
        return
    if "0 元优惠未生效" in reason:
        return
    if "approve blocked" in reason:
        return
    if is_direct_remove_proxy_error(reason):
        remove_failed_proxy("provider", provider_proxy, reason)
        record_proxy_result("provider", provider_proxy, False, reason)
    elif is_proxy_health_failure(reason):
        record_proxy_health_failure("provider", provider_proxy, reason)
    else:
        record_proxy_result("provider", provider_proxy, False, reason)


def record_seed_failure(proxy_seed: str, reason: str) -> None:
    """Persist one BLIK chain result against its original sticky seed."""
    if not proxy_seed:
        return
    if is_checkout_not_active_error(reason) or "当前 checkout 不支持" in reason:
        return
    if "0 元优惠未生效" in reason or "approve blocked" in reason:
        return
    if "代理出口国家不符" in reason:
        if "actual=UNKNOWN" not in reason:
            remove_failed_proxy("seed", proxy_seed, reason)
        record_proxy_result("seed", proxy_seed, False, reason)
        return
    if is_direct_remove_proxy_error(reason):
        remove_failed_proxy("seed", proxy_seed, reason)
        record_proxy_result("seed", proxy_seed, False, reason)
        return
    if is_proxy_health_failure(reason):
        record_proxy_health_failure("seed", proxy_seed, reason)
        return
    record_proxy_result("seed", proxy_seed, False, reason)


def order_proxy_group(group: str, proxies: list[str]) -> list[str]:
    if not env_bool("IDEAL_PROXY_SCORE", True):
        return proxies
    state = load_proxy_state().get(group, {})
    skip_failed = env_bool("IDEAL_PROXY_SKIP_FAILED", True)
    fail_threshold = env_int("IDEAL_PROXY_FAIL_SKIP_AFTER", 1)
    fail_cooldown = env_int("IDEAL_PROXY_FAIL_COOLDOWN", 180, minimum=0)
    zero_ttl = checkout_zero_cache_ttl()
    now = int(time.time())
    kept: list[str] = []
    cooldown_skipped = 0
    zero_skipped = 0
    zero_seen = 0
    success_seen = 0
    for proxy in proxies:
        record = state.get(proxy_state_key(group, proxy), {}) if isinstance(state, dict) else {}
        success_count = int(record.get("success") or 0)
        fail_count = int(record.get("fail") or 0)
        last_fail = int(record.get("last_fail") or 0)
        if success_count > 0:
            success_seen += 1
        zero_checked_at = int(record.get("zero_checked_at") or 0)
        zero_cache_valid = zero_checked_at and (zero_ttl <= 0 or now - zero_checked_at <= zero_ttl)
        if group in {"checkout", "seed"} and zero_cache_valid and record.get("zero_ok") is True:
            zero_seen += 1
        if (
            group in {"checkout", "seed"}
            and env_bool("IDEAL_ZERO_CACHE_SKIP_BAD", True)
            and zero_cache_valid
            and record.get("zero_ok") is False
        ):
            zero_skipped += 1
            continue
        if skip_failed and fail_count >= fail_threshold:
            in_cooldown = fail_cooldown <= 0 or not last_fail or now - last_fail <= fail_cooldown
            if in_cooldown:
                cooldown_skipped += 1
                continue
        kept.append(proxy)

    if not kept and proxies:
        log(f"{group} 代理状态过滤后为空，已全部跳过", "[WARN] ")

    def rank(proxy: str) -> tuple[int, int, int, int, int]:
        record = state.get(proxy_state_key(group, proxy), {}) if isinstance(state, dict) else {}
        zero_checked_at = int(record.get("zero_checked_at") or 0)
        zero_cache_valid = zero_checked_at and (zero_ttl <= 0 or now - zero_checked_at <= zero_ttl)
        zero_rank = 1 if group in {"checkout", "seed"} and zero_cache_valid and record.get("zero_ok") is True else 0
        return (
            zero_rank,
            int(record.get("success") or 0),
            int(record.get("last_success") or 0),
            -int(record.get("fail") or 0),
            -int(record.get("last_fail") or 0),
        )

    ordered = sorted(kept, key=rank, reverse=True)
    if cooldown_skipped or success_seen or zero_seen or zero_skipped:
        log(
            f"{group} 代理状态: 成功优先={success_seen}，0元命中={zero_seen}，"
            f"冷却跳过={cooldown_skipped}，0元失败跳过={zero_skipped}"
        )
    return ordered


def set_proxy(session: Any, proxy: str) -> None:
    register_proxy_for_redaction(proxy)
    proxy = normalize_proxy_url(proxy)
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}


def pre_proxy_url() -> str:
    """本机前置代理：本机代理 -> 文件代理 -> 目标站。"""
    for name in ("IDEAL_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY"):
        if name in os.environ:
            raw = os.environ.get(name, "").strip()
            if raw.lower() in {"", "0", "off", "none", "direct", "disabled"}:
                return ""
            proxy = normalize_pre_proxy_url(raw)
            register_proxy_for_redaction(proxy)
            return proxy
    return ""


def load_proxy_file(path: Path) -> list[str]:
    proxies: list[str] = []
    if not path.exists():
        return proxies
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            register_proxy_for_redaction(line)
            proxy = normalize_proxy_url(line)
            if proxy:
                proxies.append(proxy)
    random.shuffle(proxies)
    return proxies


def proxy_seed_file() -> Path:
    raw = (
        os.environ.get("IDEAL_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    )
    return Path(raw).expanduser() if raw else SCRIPT_DIR / "proxy_seeds.txt"


def unique_proxy_seeds(proxy_seeds: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    duplicates = 0
    for proxy_seed in proxy_seeds:
        chain_key = proxy_chain_key(proxy_seed)
        if not chain_key or chain_key in seen:
            duplicates += 1
            continue
        seen.add(chain_key)
        unique.append(proxy_seed)
    if duplicates:
        log(f"代理 Seed 去重: 忽略相同 sticky session {duplicates} 条", "[WARN] ")
    return unique


def load_proxy_seeds() -> list[str]:
    if env_bool("IDEAL_USE_LOCAL_PROXY_ONLY", False):
        local_proxy = pre_proxy_url()
        if not local_proxy:
            raise RuntimeError("本地代理直连模式需要设置 IDEAL_PRE_PROXY")
        log("本地代理直连模式: 单一出口用于 BLIK 全流程")
        return [local_proxy]

    path = proxy_seed_file()
    if not path.is_file():
        raise RuntimeError("代理 Seed 文件不存在")
    proxy_seeds = unique_proxy_seeds(load_proxy_file(path))
    if not proxy_seeds:
        raise RuntimeError("代理 Seed 为空")
    prune_proxy_seed_state(proxy_seeds)
    proxy_seeds = order_proxy_group("seed", proxy_seeds)
    if not proxy_seeds:
        raise RuntimeError("代理 Seed 已全部处于失败冷却")
    log(f"加载代理 Seed {len(proxy_seeds)} 条")
    log("严格代理策略: 每轮取一条 seed，派生 PL Checkout → PL Stripe/BLIK → PL approve")
    log(f"裸代理默认协议: {default_proxy_scheme()}://")
    log(f"本机前置代理: {proxy_label(pre_proxy_url())}")
    return proxy_seeds


def first_existing_file(paths: list[str | Path]) -> Path:
    for item in paths:
        if not item:
            continue
        path = Path(item)
        if path.exists():
            return path
    return Path("")


def proxy_file_for_group(group: str) -> Path:
    if (
        group == "seed"
        or os.environ.get("IDEAL_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    ):
        return proxy_seed_file()
    if group == "checkout":
        candidates = [
            os.environ.get("IDEAL_CHECKOUT_PROXY_FILE", "").strip(),
            os.environ.get("PP_CHECKOUT_PROXY_FILE", "").strip(),
            SCRIPT_DIR / "checkout.json",
        ]
    else:
        candidates = [
            os.environ.get("IDEAL_PROVIDER_PROXY_FILE", "").strip(),
            os.environ.get("PP_PROVIDER_PROXY_FILE", "").strip(),
            SCRIPT_DIR / "provider.json",
        ]
    return first_existing_file(candidates)


def is_direct_remove_proxy_error(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "proxy authentication",
            "proxy auth",
            "resolve proxy",
            "could not resolve proxy",
            "invalid proxy",
            "malformed proxy",
            "unsupported proxy",
            "http 407",
            "status 407",
        )
    )


def is_proxy_health_failure(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "目标站不可达",
            "proxy-server",
            "connection reset",
            "recv failure",
            "timed out",
            "timeout",
            "connect tunnel failed",
            "proxy connect aborted",
            "proxy tunneling",
            "proxy handshake",
            "connection refused",
            "ssl connect",
            "tls connect",
            "curl: (28)",
            "curl: (35)",
            "curl: (56)",
            "http_502",
            "http_503",
            "http_504",
        )
    )


def remove_failed_proxies(group: str, failures: list[tuple[str, str]]) -> int:
    if not failures or not env_bool("IDEAL_PROXY_REMOVE_FAILED", True):
        return 0
    path = proxy_file_for_group(group)
    if not str(path) or not path.exists():
        return 0
    seed_mode = group == "seed" or bool(
        os.environ.get("IDEAL_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    )
    key_for_proxy = proxy_chain_key if seed_mode else normalize_proxy_url
    reasons = {key_for_proxy(proxy): reason for proxy, reason in failures if key_for_proxy(proxy)}
    if not reasons:
        return 0
    with _proxy_file_lock:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        removed = [line for line in lines if key_for_proxy(line) in reasons]
        if not removed:
            return 0
        kept = [line for line in lines if key_for_proxy(line) not in reasons]
        quarantine = SCRIPT_DIR / "removed_proxies.jsonl"
        with open(quarantine, "a", encoding="utf-8") as f:
            for line in removed:
                normalized = key_for_proxy(line)
                f.write(
                    json.dumps(
                        {
                            "time": int(time.time()),
                            "group": group,
                            "proxy": proxy_label(line.strip()),
                            "reason": redact_log_text(str(reasons.get(normalized) or ""))[:300],
                            "source": path.name,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text("".join(kept), encoding="utf-8")
        os.replace(temp_path, path)
        return len(removed)


def remove_failed_proxy(group: str, proxy: str, reason: str) -> bool:
    return remove_failed_proxies(group, [(proxy, reason)]) > 0


def load_proxy_groups() -> tuple[list[str], list[str]]:
    if env_bool("IDEAL_USE_LOCAL_PROXY_ONLY", False):
        local_proxy = pre_proxy_url()
        if not local_proxy:
            raise RuntimeError("本地代理直连模式需要设置 IDEAL_PRE_PROXY")
        log(f"本地代理直连模式: checkout/provider 均使用 {proxy_label(local_proxy)}")
        return [local_proxy], [local_proxy]

    checkout_file = proxy_file_for_group("checkout")
    provider_file = proxy_file_for_group("provider")
    checkout_proxies = load_proxy_file(checkout_file) if str(checkout_file) else []
    provider_proxies = load_proxy_file(provider_file) if str(provider_file) else []
    if not checkout_proxies:
        raise RuntimeError("Checkout 代理为空：需要 checkout.json")
    if not provider_proxies:
        raise RuntimeError("Provider 代理为空：approve 阶段需要 provider.json")
    prune_proxy_state(checkout_proxies, provider_proxies)
    checkout_proxies = order_proxy_group("checkout", checkout_proxies)
    provider_proxies = order_proxy_group("provider", provider_proxies)
    if not checkout_proxies:
        raise RuntimeError("Checkout 代理已全部被失败状态过滤")
    if not provider_proxies:
        raise RuntimeError("Provider 代理已全部被失败状态过滤")
    log(f"加载 Checkout 代理 {len(checkout_proxies)} 条: {checkout_file}")
    log(f"加载 Provider 代理 {len(provider_proxies)} 条: {provider_file}")
    log(f"代理策略: checkout/0元资格/approve 用 Checkout；Stripe/{payment_method_label()} 用 Provider")
    log(f"裸代理默认协议: {default_proxy_scheme()}://")
    log(f"本机前置代理: {proxy_label(pre_proxy_url())}")
    log(
        "代理检测前置策略: "
        f"国家检测={'on' if env_bool('IDEAL_PROXY_GEO_USE_PRE_PROXY', False) else 'off'}，"
        f"目标站检测={'on' if env_bool('IDEAL_PROXY_TARGET_USE_PRE_PROXY', True) else 'off'}"
    )
    checkout_proxies = precheck_proxy_group("checkout", checkout_proxies)
    provider_proxies = precheck_proxy_group("provider", provider_proxies)
    log(f"预筛后 Checkout 代理 {len(checkout_proxies)} 条")
    log(f"预筛后 Provider 代理 {len(provider_proxies)} 条")
    return checkout_proxies, provider_proxies


def new_session(proxy: str = "", use_pre_proxy: bool = True) -> Any:
    pre_proxy = "" if env_bool("IDEAL_USE_LOCAL_PROXY_ONLY", False) else (pre_proxy_url() if use_pre_proxy else "")
    register_proxy_for_redaction(pre_proxy)
    if CurlCffiSession is not None:
        kwargs: dict[str, Any] = {"impersonate": "chrome136"}
        if pre_proxy:
            if CurlOpt is None:
                raise RuntimeError("本机前置代理需要 curl_cffi 支持")
            kwargs["curl_options"] = {CurlOpt.PRE_PROXY: pre_proxy}
        session = CurlCffiSession(**kwargs)
    else:
        if pre_proxy:
            raise RuntimeError("本机前置代理需要 curl_cffi：python3 -m pip install curl_cffi")
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    if proxy:
        set_proxy(session, proxy)
    return session


def _redact_text(text: str, limit: int | None = None) -> str:
    text = text or ""
    blik_code = os.environ.get("IDEAL_BLIK_CODE", "").strip()
    if blik_code:
        text = text.replace(blik_code, "***")
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._=-]+", r"\1***", text)
    text = re.sub(r"(__Secure-next-auth\.session-token=)[^;\\s]+", r"\1***", text)
    text = re.sub(r"(accessToken|access_token|sessionToken|token)(['\"]?\s*[:=]\s*['\"])[^'\"]+", r"\1\2***", text)
    text = re.sub(r"(?i)(blik_code['\"]?\s*[:=]\s*['\"]?)\d{6}", r"\1***", text)
    text = redact_log_text(text)
    if limit is None:
        limit = env_int("IDEAL_DUMP_LIMIT", 6000, minimum=500)
    return text[:limit]


def dump_http(
    response: requests.Response | None,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
) -> None:
    if not env_bool("IDEAL_DUMP", False):
        return
    global _dump_counter
    with _dump_lock:
        _dump_counter += 1
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{_dump_counter:04d}_{stage}.txt"
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = DUMP_DIR / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    lines = [
        f"stage: {stage}",
        f"request: {request_method} {request_url}",
        "",
        "request_body:",
        _redact_text(json.dumps(request_body, ensure_ascii=False, indent=2) if request_body is not None else ""),
        "",
    ]
    if response is not None:
        lines.extend(
            [
                f"status: {response.status_code}",
                f"url: {response.url}",
                "",
                "response:",
                _redact_text(response.text),
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def token_key_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def find_named_token(payload: Any, aliases: tuple[str, ...]) -> str:
    wanted = {token_key_name(item) for item in aliases}
    if isinstance(payload, dict):
        cookie_name = token_key_name(payload.get("name") or payload.get("key"))
        if cookie_name in wanted:
            for value_key in ("value", "token", "content"):
                value = str(payload.get(value_key) or "").strip()
                if value:
                    return value
        for key, value in payload.items():
            if token_key_name(key) in wanted and isinstance(value, (str, int, float)):
                found = str(value).strip()
                if found:
                    return found
        for value in payload.values():
            found = find_named_token(value, aliases)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_named_token(item, aliases)
            if found:
                return found
    return ""


def collect_strings(payload: Any, result: list[str] | None = None) -> list[str]:
    values = result if result is not None else []
    if isinstance(payload, str):
        values.append(payload)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_strings(value, values)
    elif isinstance(payload, list):
        for item in payload:
            collect_strings(item, values)
    return values


def find_session_cookie(payload: Any) -> str:
    for value in collect_strings(payload):
        match = re.search(r"(?:^|[;\s])__Secure-next-auth\.session-token=([^;\s]+)", value)
        if match:
            return unquote(match.group(1))
    return ""


def normalize_token(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    session_token = ""
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
            access_token = find_named_token(
                data,
                (
                    "accessToken",
                    "access_token",
                    "token",
                    "bearerToken",
                    "bearer_token",
                    "jwt",
                ),
            )
            session_token = find_named_token(
                data,
                (
                    "sessionToken",
                    "session_token",
                    "nextAuthSessionToken",
                    "next_auth_session_token",
                    "__Secure-next-auth.session-token",
                    "secureNextAuthSessionToken",
                ),
            ) or find_session_cookie(data)
            text = access_token
        except json.JSONDecodeError:
            pass
    return text, session_token


def load_token() -> tuple[str, str]:
    for env_name in ("PP_TOKEN", "IDEAL_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            log(f"使用环境变量 {env_name}")
            token, session_token = normalize_token(value)
            env_session = os.environ.get("PP_SESSION_TOKEN", "").strip()
            if env_session or session_token:
                log("已加载 sessionToken cookie")
            return token, env_session or session_token

    candidates = [SCRIPT_DIR / "token.txt"]
    for path in candidates:
        if not path.exists():
            continue
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "utf-8", "ascii"):
            try:
                text = raw.decode(enc).strip()
                break
            except UnicodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="ignore").strip()
        if text:
            log("使用 token 文件")
            token, session_token = normalize_token(text)
            env_session = os.environ.get("PP_SESSION_TOKEN", "").strip()
            if env_session or session_token:
                log("已加载 sessionToken cookie")
            return token, env_session or session_token

    token = input("请输入 access_token: ").strip()
    session_token = os.environ.get("PP_SESSION_TOKEN", "").strip()
    token, parsed_session = normalize_token(token)
    return token, session_token or parsed_session


def build_chatgpt_session(access_token: str, device_id: str, proxy: str, session_token: str = "") -> requests.Session:
    session = new_session(proxy)
    cookie = f"oai-did={device_id}"
    if session_token:
        cookie += f"; __Secure-next-auth.session-token={session_token}"
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": payment_accept_language(),
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": device_id,
            "oai-language": payment_browser_locale(),
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Cookie": cookie,
        }
    )
    return session


def checkout_response_has_promo(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in (
        "scheduled_discount_preview",
        "immediate_discount_settings",
        "promo_campaign",
        "promo_credit_grant",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def checkout_response_has_trial(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("one_click_trial_eligible") is True:
        return True
    subscription_data = payload.get("subscription_data")
    if isinstance(subscription_data, dict) and int(subscription_data.get("trial_period_days") or 0) > 0:
        return True
    for key in ("trial_period_days", "trial_end"):
        value = payload.get(key)
        if value not in (None, "", 0, "0", False):
            return True
    return False


def create_checkout(chatgpt: requests.Session, country: str) -> dict[str, str]:
    country = normalize_country(country)
    promo_mode = os.environ.get("PP_PROMO_MODE", "campaign").strip().lower() or "campaign"
    promo_id = os.environ.get("PP_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "entry_point": os.environ.get("PP_ENTRY_POINT", "all_plans_pricing_modal"),
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency_for_country(country)},
        "checkout_ui_mode": "custom",
    }
    if promo_mode in ("trial", "free_trial"):
        trial_days = env_int("PP_TRIAL_DAYS", 30)
        body["subscription_data"] = {"trial_period_days": trial_days}
    elif promo_mode in ("campaign", "query"):
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": promo_mode == "query",
        }
    elif promo_mode == "coupon":
        body["coupon"] = promo_id
    elif promo_mode == "code":
        body["promotion_code"] = promo_id
    elif promo_mode != "off":
        log(f"未知 PP_PROMO_MODE={promo_mode!r}，已忽略", "[WARN] ")
    log(f"Checkout promo: mode={promo_mode}, id={promo_id}")

    headers = {
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    resp = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "checkout", body, "POST", "https://chatgpt.com/backend-api/payments/checkout", force=resp.status_code >= 400)
    if resp.status_code >= 400:
        if is_user_already_paid_error(resp.text):
            raise RuntimeError("用户已支付: User is already paid")
        raise RuntimeError(f"checkout 创建失败 HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json() or {}
    if (
        promo_mode == "coupon"
        and promo_id == "plus-1-month-free"
        and not checkout_response_has_promo(data)
        and env_bool("IDEAL_COUPON_FALLBACK_PROMO_CAMPAIGN", True)
    ):
        log("coupon 响应未显示优惠，按 promo_campaign 字符串重试", "[PROMO] ")
        fallback_body = dict(body)
        fallback_body.pop("coupon", None)
        fallback_body["promo_campaign"] = promo_id
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            json=fallback_body,
            headers=headers,
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(
            resp,
            "checkout_promo_campaign",
            fallback_body,
            "POST",
            "https://chatgpt.com/backend-api/payments/checkout",
            force=True,
        )
        if resp.status_code >= 400:
            if is_user_already_paid_error(resp.text):
                raise RuntimeError("用户已支付: User is already paid")
            raise RuntimeError(f"checkout promo_campaign 重试失败 HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json() or {}
        log(f"promo_campaign 重试后 promo={checkout_response_has_promo(data)}", "[PROMO] ")

    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout 响应缺少 cs_id: {str(data)[:500]}")

    raw_pk = (
        data.get("stripe_publishable_key")
        or data.get("publishable_key")
        or data.get("publishableKey")
        or data.get("stripePublishableKey")
        or data.get("key")
        or ""
    )
    match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_pk))
    stripe_pk = match.group(0) if match else DEFAULT_STRIPE_PK
    processor_entity = str(data.get("processor_entity") or data.get("processorEntity") or "")
    log(
        f"Checkout 创建成功: {cs_id} / {country} / {currency_for_country(country)} / "
        f"mode={promo_mode} / promo={checkout_response_has_promo(data)} / "
        f"trial={checkout_response_has_trial(data)}"
    )
    return {
        "cs_id": str(cs_id),
        "processor_entity": processor_entity,
        "stripe_pk": stripe_pk,
        "billing_country": country,
        "currency": currency_for_country(country),
    }


def stripe_init(cs_id: str, stripe_pk: str, proxy: str) -> dict[str, Any]:
    stripe = new_session(proxy)
    stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    body = {
        "browser_locale": payment_browser_locale(),
        "browser_timezone": payment_browser_timezone(),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": payment_elements_locale(),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/init"
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "stripe_init", body, "POST", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"Stripe init 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json() or {}
    payload["client_stripe_js_id"] = stripe_js_id
    payload["client_elements_session_id"] = elements_session_id
    return payload


def amount_from_payload(payload: Any) -> int:
    if isinstance(payload, dict):
        total_summary = payload.get("total_summary")
        if isinstance(total_summary, dict) and total_summary.get("due") is not None:
            return int(total_summary.get("due") or 0)
        invoice = payload.get("invoice")
        if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
            return int(invoice.get("amount_due") or 0)
        line_items = payload.get("line_items")
        if isinstance(line_items, list):
            total = 0
            found = False
            for item in line_items:
                if isinstance(item, dict) and item.get("amount") is not None:
                    total += int(item.get("amount") or 0)
                    found = True
            if found:
                return total
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    for pattern in (
        r'"total"\s*:\s*(\d+)',
        r'"amount_total"\s*:\s*(\d+)',
        r'"checkout_amount"\s*:\s*(\d+)',
        r'"amount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return 0


def build_ctx(init_payload: dict[str, Any], checkout: dict[str, str]) -> dict[str, Any]:
    client_context = init_payload.get("_client_context") if isinstance(init_payload.get("_client_context"), dict) else {}
    return {
        "stripe_js_id": str(client_context.get("stripe_js_id") or init_payload.get("client_stripe_js_id") or uuid.uuid4()),
        "client_session_id": str(uuid.uuid4()),
        "guid": stripe_browser_id(),
        "muid": stripe_browser_id(),
        "sid": stripe_browser_id(),
        "elements_session_id": str(init_payload.get("client_elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": init_payload.get("config_id") or "",
        "init_checksum": init_payload.get("init_checksum") or "",
        "checkout_amount": amount_from_payload(init_payload),
        "locale": payment_elements_locale(),
        "currency": str(init_payload.get("currency") or checkout.get("currency") or "eur").lower(),
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION_FULL,
    }


def stripe_elements_session_params(ctx: dict[str, Any]) -> dict[str, str]:
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx["elements_session_id"]),
        "elements_session_client[stripe_js_id]": str(ctx["stripe_js_id"]),
        "elements_session_client[locale]": str(ctx["locale"]),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
    }


def ideal_billing_profile() -> dict[str, str]:
    is_blik = payment_method_type() == "blik"
    first_name, last_name = random.choice(PL_BILLING_NAMES if is_blik else NL_BILLING_NAMES)
    line1, city, postal_code = random.choice(PL_BILLING_ADDRESSES if is_blik else NL_BILLING_ADDRESSES)
    profile = {
        "email": build_email(first_name, last_name),
        "name": f"{first_name} {last_name}",
        "country": "PL" if is_blik else "NL",
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal_code,
        "state": "",
    }
    if env_bool("IDEAL_USE_FIXED_BILLING", False):
        profile = dict(DEFAULT_BLIK_BILLING if is_blik else DEFAULT_IDEAL_BILLING)
    env_map = {
        "email": "IDEAL_EMAIL",
        "name": "IDEAL_NAME",
        "country": "IDEAL_BILLING_COUNTRY",
        "line1": "IDEAL_LINE1",
        "line2": "IDEAL_LINE2",
        "city": "IDEAL_CITY",
        "postal_code": "IDEAL_POSTAL_CODE",
        "state": "IDEAL_STATE",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            profile[key] = value
    profile["country"] = normalize_country(profile.get("country", "NL"))
    return profile


def stripe_update_customer_data(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
) -> bool:
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "expected_amount": str(ctx.get("checkout_amount") or 0),
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "customer_data[email]": billing["email"],
        "customer_data[name]": billing["name"],
        "customer_data[address][country]": billing["country"],
        "customer_data[address][line1]": billing["line1"],
        "customer_data[address][city]": billing["city"],
        "customer_data[address][postal_code]": billing["postal_code"],
    }
    if billing.get("line2"):
        body["customer_data[address][line2]"] = billing["line2"]
    if billing.get("state"):
        body["customer_data[address][state]"] = billing["state"]

    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        dump_http(resp, "customer_data_update_nl", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            log(
                f"荷兰 customer_data 已提交: {billing['name']} / {billing['city']} / {billing['postal_code']}"
            )
            return True
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        log(f"荷兰 customer_data 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"荷兰 customer_data 提交异常: {exc}", "[WARN] ")
    return False


def stripe_update_tax_region(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
) -> bool:
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
    }
    if billing.get("state"):
        body["tax_region[state]"] = billing["state"]

    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        dump_http(resp, "tax_region_update", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            log(f"tax_region 已提交: {billing['country']} / {billing['city']} {billing['postal_code']}")
            return True
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        log(f"tax_region 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"tax_region 提交异常: {exc}", "[WARN] ")
    return False


def checkout_snapshot(chatgpt: requests.Session, checkout: dict[str, str], billing: dict[str, str]) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(checkout.get("billing_country", "NL"), checkout.get("processor_entity") or "")
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    body = {
        "snapshot": {
            "billing_address": {
                "name": billing["name"],
                "address": {
                    "line1": billing["line1"],
                    "city": billing["city"],
                    "country": billing["country"],
                    "postal_code": billing["postal_code"],
                    "state": billing.get("state", ""),
                },
            }
        }
    }
    try:
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout/snapshot",
            json=body,
            headers={
                "Referer": checkout_page_url,
                "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
                "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(resp, "checkout_snapshot", body, "POST", "https://chatgpt.com/backend-api/payments/checkout/snapshot", force=env_bool("IDEAL_DUMP_WARMUP", False) or resp.status_code >= 400)
        if resp.status_code >= 400:
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            log(f"checkout snapshot 失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
        else:
            log("checkout snapshot 已提交")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"checkout snapshot 异常: {exc}", "[WARN] ")


def stripe_create_ideal_pm(stripe: requests.Session, cs_id: str, stripe_pk: str, billing: dict[str, str], ctx: dict[str, Any]) -> str:
    body: dict[str, Any] = {
        "billing_details[name]": billing.get("name") or "Jan de Vries",
        "billing_details[email]": billing.get("email") or "redacted@example.invalid",
        "billing_details[address][country]": billing.get("country") or "NL",
        "billing_details[address][line1]": billing.get("line1") or "Prinsengracht 263",
        "billing_details[address][city]": billing.get("city") or "Amsterdam",
        "billing_details[address][postal_code]": billing.get("postal_code") or "1016 GV",
        "type": "ideal",
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "key": stripe_pk,
    }
    bank = os.environ.get("IDEAL_BANK", "").strip()
    if bank:
        body["ideal[bank]"] = bank
    if billing.get("state"):
        body["billing_details[address][state]"] = billing["state"]

    resp = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "ideal_pm", body, "POST", "https://api.stripe.com/v1/payment_methods", force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"创建 iDEAL PM 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"创建 iDEAL PM 响应异常: {resp.text[:300]}")
    return pm_id


def add_inline_ideal_payment_method_data(body: dict[str, Any], cs_id: str, billing: dict[str, str], ctx: dict[str, Any]) -> None:
    body.update(
        {
            "payment_method_data[type]": "ideal",
            "payment_method_data[allow_redisplay]": "limited",
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[payment_user_agent]": f"stripe.js/{random_runtime_version()}; stripe-js-v3/{random_runtime_version()}; payment-element; deferred-intent",
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(random.randint(18000, 55000)),
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["stripe_js_id"],
            "payment_method_data[client_attribution_metadata][checkout_config_id]": ctx.get("config_id") or "",
            "payment_method_data[client_attribution_metadata][elements_session_id]": ctx["elements_session_id"],
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": ctx["elements_session_config_id"],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
        }
    )
    if billing.get("state"):
        body["payment_method_data[billing_details][address][state]"] = billing["state"]
    bank = os.environ.get("IDEAL_BANK", "").strip()
    if bank:
        body["payment_method_data[ideal][bank]"] = bank


def add_inline_blik_payment_method_data(body: dict[str, Any], cs_id: str, billing: dict[str, str], ctx: dict[str, Any]) -> None:
    body.update(
        {
            "payment_method_data[type]": "blik",
            "payment_method_data[allow_redisplay]": "limited",
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][line2]": billing.get("line2", ""),
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[payment_user_agent]": (
                f"stripe.js/{random_runtime_version()}; stripe-js-v3/{random_runtime_version()}; "
                "payment-element; deferred-intent"
            ),
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(random.randint(18000, 55000)),
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["client_session_id"],
            "payment_method_data[client_attribution_metadata][checkout_config_id]": ctx.get("config_id") or "",
            "payment_method_data[client_attribution_metadata][elements_session_id]": ctx["elements_session_id"],
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": ctx["elements_session_config_id"],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "address",
            "blik_code": os.environ.get("IDEAL_BLIK_CODE", "").strip(),
        }
    )


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    if processor_entity:
        return processor_entity
    return "openai_llc" if normalize_country(country) == "US" else "openai_ie"


def stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str) -> str:
    processor = processor_entity_for_country(country, processor_entity)
    success = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor}&plan_type=plus"
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(success, safe='')}"
    )


def to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com") :]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def stripe_confirm_return_url(cs_id: str, checkout: dict[str, str], stripe_hosted_url: str) -> str:
    country = normalize_country(checkout.get("billing_country") or "NL")
    processor = processor_entity_for_country(country, checkout.get("processor_entity") or "")
    success = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor}&plan_type=plus"
    hosted = to_openai_pay_url(stripe_hosted_url) or stripe_checkout_long_url(cs_id, country, processor)
    if "pay.openai.com/" in hosted or "checkout.stripe.com/" in hosted:
        parsed = urlsplit(hosted)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("success_return_url", success)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted


def stripe_confirm_ideal(
    stripe: requests.Session,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
) -> dict[str, Any]:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "eid": "NA",
        "expected_amount": os.environ.get("PP_EXPECTED_AMOUNT", "").strip() or str(ctx.get("checkout_amount") or amount_from_payload(init_payload)),
        "expected_payment_method_type": "ideal",
        "return_url": stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "key": stripe_pk,
        "version": runtime_version,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": str(ctx.get("client_session_id") or ctx["stripe_js_id"]),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "link_brand": "link",
    }
    if env_bool("IDEAL_CONFIRM_INLINE_PM", False):
        add_inline_ideal_payment_method_data(body, cs_id, billing, ctx)
    else:
        body["payment_method"] = pm_id
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "ideal_confirm", body, "POST", url, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"iDEAL confirm 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json() or {}


def stripe_confirm_blik(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
) -> dict[str, Any]:
    code = os.environ.get("IDEAL_BLIK_CODE", "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise RuntimeError("IDEAL_BLIK_CODE 必须是6位数字")
    body: dict[str, Any] = {
        "eid": "NA",
        "expected_amount": os.environ.get("PP_EXPECTED_AMOUNT", "").strip()
        or str(ctx.get("checkout_amount") or amount_from_payload(init_payload)),
        "expected_payment_method_type": "blik",
        "return_url": stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "key": stripe_pk,
        "version": str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION),
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": str(ctx["client_session_id"]),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "link_brand": "link",
    }
    body.update(stripe_elements_session_params(ctx))
    add_inline_blik_payment_method_data(body, cs_id, billing, ctx)
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "blik_confirm", body, "POST", url, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"BLIK confirm 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json() or {}


def collect_urls(payload: Any, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
        for match in re.findall(r"data:image/(?:png|svg\+xml|jpeg);base64,[A-Za-z0-9+/=]+", payload):
            found.append(match)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            collect_urls(item, found)
    return found


def is_resource_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if is_known_static_host(url):
        return True
    return path.endswith(
        (
            ".js",
            ".css",
            ".map",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".ico",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
        )
    )


def is_known_static_host(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in {
        "stripe-camo.global.ssl.fastly.net",
        "files.stripe.com",
        "js.stripe.com",
        "m.stripe.network",
        "q.stripe.com",
    }


def is_redirect_like_url(url: str, from_action_field: bool = False) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    if is_resource_url(url):
        return False
    if from_action_field:
        return True

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    text = f"{host}{path}?{query}"
    if host in {"hooks.stripe.com", "payments.stripe.com"}:
        return True
    if host.endswith(".ideal.nl") or host == "ideal.nl":
        return True
    return any(part in text for part in ("ideal", "/redirect/", "redirect_to_url", "authenticate"))


def is_qr_candidate(url: str) -> bool:
    lower = url.lower()
    return lower.startswith("data:image/") or "qr" in lower or "qrcode" in lower or "qr-code" in lower


def extract_qr_candidates(payload: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in collect_urls(payload):
        if url in seen:
            continue
        seen.add(url)
        if is_qr_candidate(url) and not is_known_static_host(url):
            result.append(url)
    return result


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get("submission_attempt")
        if isinstance(value, dict):
            return value
        for item in payload.values():
            nested = find_submission_attempt(item)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = find_submission_attempt(item)
            if nested:
                return nested
    return {}


def extract_redirect_url(payload: Any, path: tuple[str, ...] = ()) -> str:
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if is_redirect_like_url(url, True):
                    return url
            for key in ("url", "redirect_url", "redirect_to_url", "hosted_url"):
                value = next_action.get(key)
                if is_redirect_like_url(value, True):
                    return value

        for key in ("redirect_url", "redirect_to_url", "authorization_url", "authentication_url"):
            value = payload.get(key)
            if is_redirect_like_url(value, True):
                return value

        for key, value in payload.items():
            nested = extract_redirect_url(value, path + (str(key),))
            if nested:
                return nested
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            nested = extract_redirect_url(item, path + (str(index),))
            if nested:
                return nested

    return ""


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = first_value_by_key(item, key)
            if found not in (None, "", [], {}):
                return found
    return None


def setup_intent_last_error(payload: Any) -> str:
    if isinstance(payload, dict):
        payload_id = str(payload.get("id") or "").strip()
        is_setup_intent = payload.get("object") == "setup_intent" or payload_id.startswith("seti_")
        last_error = payload.get("last_setup_error") if is_setup_intent else None
        setup_intent = payload.get("setup_intent")
        if not last_error and isinstance(setup_intent, dict):
            last_error = setup_intent.get("last_setup_error")
        if last_error:
            try:
                return json.dumps(last_error, ensure_ascii=False)
            except Exception:
                return str(last_error)
        for value in payload.values():
            found = setup_intent_last_error(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = setup_intent_last_error(value)
            if found:
                return found
    return ""


def raise_if_setup_intent_blocked(payload: Any, context: str) -> None:
    last_error = setup_intent_last_error(payload)
    if not last_error:
        return
    if "generic_decline" in last_error.lower():
        raise RuntimeError(f"Stripe 风控拒绝（generic_decline）：{context} SetupIntent 创建失败；last_setup_error={last_error}")
    raise RuntimeError(f"{context}: setup_intent.last_setup_error: {last_error}")


def infer_processor_entity(payload: Any) -> str:
    for value in collect_strings(payload):
        match = re.search(r"[?&]processor_entity=([A-Za-z0-9_]+)", value)
        if match:
            return match.group(1)
    return ""


def payment_page_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    elements_options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    submission = find_submission_attempt(payload)
    next_action = first_value_by_key(payload, "next_action")
    payment_intent = first_value_by_key(payload, "payment_intent")
    setup_intent = first_value_by_key(payload, "setup_intent")
    summary: dict[str, Any] = {
        "object": payload.get("object"),
        "id": payload.get("id"),
        "status": payload.get("status"),
        "payment_status": payload.get("payment_status"),
        "amount": elements_options.get("amount") if elements_options else first_value_by_key(payload, "amount"),
        "currency": payload.get("currency") or (elements_options.get("currency") if elements_options else None),
        "mode": elements_options.get("mode") if elements_options else payload.get("mode"),
        "payment_method_types": elements_options.get("payment_method_types") if elements_options else None,
        "submission_state": submission.get("state") if submission else None,
        "submission_status": submission.get("status") if submission else None,
        "has_next_action": isinstance(next_action, dict) and bool(next_action),
    }
    if isinstance(payment_intent, dict):
        summary["payment_intent_status"] = payment_intent.get("status")
    elif isinstance(payment_intent, str):
        summary["payment_intent"] = payment_intent
    if isinstance(setup_intent, dict):
        summary["setup_intent_status"] = setup_intent.get("status")
    elif isinstance(setup_intent, str):
        summary["setup_intent"] = setup_intent
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def log_payment_page_summary(stage: str, payload: Any) -> None:
    summary = payment_page_summary(payload)
    if not summary:
        return
    compact = format_payment_summary(summary)
    log(f"{stage} 返回摘要: {compact}")


def format_payment_summary(summary: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in summary.items())


def warmup_approve_context(chatgpt: requests.Session, checkout_page_url: str) -> None:
    try:
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(resp, "sentinel_ping", {}, "POST", "https://chatgpt.com/backend-api/sentinel/ping", force=env_bool("IDEAL_DUMP_WARMUP", False))
    except Exception as exc:
        log(f"approve sentinel 请求异常: {exc}", "[WARN] ")


def chatgpt_approve(chatgpt: requests.Session, checkout: dict[str, str]) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(checkout.get("billing_country", "NL"), checkout.get("processor_entity", ""))
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    if env_bool("IDEAL_APPROVE_WARMUP", True):
        warmup_approve_context(chatgpt, checkout_page_url)
        time.sleep(random.uniform(0.8, 1.6))

    body = {"checkout_session_id": cs_id, "processor_entity": processor}
    headers = {
        "Referer": checkout_page_url,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
    }
    resp = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "approve", body, "POST", "https://chatgpt.com/backend-api/payments/checkout/approve", force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"ChatGPT approve 失败 HTTP {resp.status_code}: {resp.text[:300]}")
    result = ""
    try:
        result = str((resp.json() or {}).get("result") or "")
    except Exception:
        pass
    if result != "approved":
        raise RuntimeError(f"ChatGPT approve 未通过: {result or resp.text[:200]}")


def approve_attempt(
    access_token: str,
    device_id: str,
    checkout: dict[str, str],
    session_token: str,
    proxy: str,
    index: int,
    attempt_count: int,
) -> None:
    log(f"approve 第 {index}/{attempt_count} 次 / proxy={proxy_label(proxy)}")
    chatgpt = build_chatgpt_session(access_token, device_id, proxy, session_token)
    chatgpt_approve(chatgpt, checkout)


def log_approve_failure(error: str) -> bool:
    log(f"approve 失败: {error[:180]}", "[WARN] ")
    if "ChatGPT approve 未通过: blocked" in error:
        log("approve 返回 blocked，当前 checkout/approve 组失败后切下一次尝试", "[WARN] ")
        return True
    return False


def is_approve_failure_error(error: str) -> bool:
    text = str(error or "").lower()
    return "approve" in text or "chatgpt approve" in text


def approve_with_retry(
    access_token: str,
    device_id: str,
    checkout: dict[str, str],
    proxies: list[str],
    session_token: str,
    proxy_group: str = "provider",
) -> str:
    max_retry = env_int("IDEAL_APPROVE_RETRY_MAX", 10)
    parallel = env_int("IDEAL_APPROVE_PARALLEL", 1)
    last_error = ""
    if max_retry <= 0:
        raise RuntimeError("approve 重试次数必须大于 0")
    proxies = [proxy for proxy in dict.fromkeys(proxies) if proxy]
    if not proxies:
        raise RuntimeError("approve 代理为空")
    sticky = env_bool("IDEAL_APPROVE_STICKY", False)
    if sticky:
        selected_proxies = [proxies[0]] * max_retry
    else:
        attempt_count = min(max_retry, len(proxies))
        fixed_proxies = proxies[: min(2, attempt_count)]
        selected_proxies = fixed_proxies[:]
        remain_count = attempt_count - len(selected_proxies)
        if remain_count > 0:
            selected_proxies.extend(random.sample(proxies[2:], min(remain_count, len(proxies) - 2)))
    attempt_count = len(selected_proxies)
    log(f"approve 代理策略: {'sticky' if sticky else 'rotate'}")
    if parallel > 1:
        workers = min(parallel, attempt_count)
        log(f"approve 并发: workers={workers}, attempts={attempt_count}")
        blocked_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    approve_attempt,
                    access_token,
                    device_id,
                    checkout,
                    session_token,
                    proxy,
                    index,
                    attempt_count,
                ): proxy
                for index, proxy in enumerate(selected_proxies, start=1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                    log("approve 成功")
                    for pending in futures:
                        pending.cancel()
                    return futures[future]
                except Exception as exc:
                    last_error = str(exc)
                    if is_checkout_not_active_error(last_error):
                        raise RuntimeError("checkout_not_active_session")
                    if log_approve_failure(last_error):
                        blocked_count += 1
                        record_proxy_health_failure(proxy_group, futures[future], last_error)
        if blocked_count and blocked_count == attempt_count:
            raise RuntimeError("approve blocked")
        raise RuntimeError(f"approve 重试失败: {last_error}")

    blocked_count = 0
    for index, proxy in enumerate(selected_proxies, start=1):
        try:
            approve_attempt(access_token, device_id, checkout, session_token, proxy, index, attempt_count)
            log("approve 成功")
            return proxy
        except Exception as exc:
            last_error = str(exc)
            if is_checkout_not_active_error(last_error):
                raise RuntimeError("checkout_not_active_session")
            if log_approve_failure(last_error):
                blocked_count += 1
                record_proxy_health_failure(proxy_group, proxy, last_error)
            if index < attempt_count:
                time.sleep(random.uniform(1, 2))
    if blocked_count and blocked_count == attempt_count:
        raise RuntimeError("approve blocked")
    raise RuntimeError(f"approve 重试失败: {last_error}")


def poll_payment_page(stripe: requests.Session, checkout: dict[str, str], stripe_pk: str, ctx: dict[str, Any]) -> tuple[str, list[str]]:
    cs_id = checkout["cs_id"]
    deadline = time.time() + env_int("IDEAL_POLL_TIMEOUT", 45)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    last_error = ""
    last_payload: dict[str, Any] = {}
    last_summary = ""
    while time.time() < deadline:
        resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            dump_http(resp, "poll_error", params, "GET", url, force=True)
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            last_error = f"HTTP {resp.status_code}"
            time.sleep(1)
            continue
        payload = resp.json() or {}
        last_payload = payload
        summary = payment_page_summary(payload)
        summary_text = format_payment_summary(summary) if summary else ""
        if summary_text and summary_text != last_summary:
            last_summary = summary_text
            log(f"poll 返回摘要: {summary_text}")
        redirect_url = extract_redirect_url(payload)
        qr_urls = extract_qr_candidates(payload)
        if redirect_url or qr_urls:
            dump_http(resp, "poll_success", params, "GET", url, force=True)
            return redirect_url, qr_urls
        submission = find_submission_attempt(payload)
        if submission.get("state") == "requires_approval":
            last_error = "payment_pages 仍然 requires_approval"
            time.sleep(1)
            continue
        if submission.get("state") == "failed":
            dump_http(resp, "poll_failed", params, "GET", url, force=True)
            raise RuntimeError(f"Stripe submission failed: {submission}")
        last_error = str(submission or "waiting")
        time.sleep(1)
    if last_payload:
        dump_response = type("DumpResponse", (), {})()
        dump_response.status_code = 200
        dump_response.url = url
        dump_response.text = json.dumps(last_payload, ensure_ascii=False, indent=2)
        dump_http(dump_response, "poll_no_redirect", params, "GET", url, force=True)
    log(f"poll 结束，未发现真实 iDEAL redirect/QR: {last_error}", "[WARN] ")
    return "", []


def poll_blik_status(
    stripe: requests.Session,
    checkout: dict[str, str],
    stripe_pk: str,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    cs_id = checkout["cs_id"]
    deadline = time.time() + env_int("IDEAL_BLIK_POLL_TIMEOUT", 65)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    last_state = "waiting"
    last_summary = ""
    while time.time() < deadline:
        resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            last_state = f"HTTP {resp.status_code}"
            time.sleep(1)
            continue
        try:
            payload = resp.json() or {}
        except Exception:
            payload = {"_raw_text": resp.text}
        raise_if_setup_intent_blocked(payload, "BLIK payment_pages")

        summary = payment_page_summary(payload)
        summary_text = format_payment_summary(summary) if summary else ""
        if summary_text and summary_text != last_summary:
            last_summary = summary_text
            log(f"BLIK poll 返回摘要: {summary_text}")

        submission = find_submission_attempt(payload)
        if submission.get("state") == "failed":
            raise RuntimeError(f"Stripe submission failed: {submission}")

        intent = first_value_by_key(payload, "payment_intent")
        if not isinstance(intent, dict):
            intent = first_value_by_key(payload, "setup_intent")
        intent_status = str(intent.get("status") or "") if isinstance(intent, dict) else ""
        next_action = intent.get("next_action") if isinstance(intent, dict) else None
        next_action_type = str(next_action.get("type") or "") if isinstance(next_action, dict) else ""
        if intent_status in {"succeeded", "processing"}:
            return payload
        if intent_status in {"requires_payment_method", "canceled"}:
            raise RuntimeError(f"BLIK 支付失败: intent_status={intent_status}")
        if next_action_type == "blik_authorize":
            last_state = "等待手机银行确认"
        elif submission.get("state") == "requires_approval":
            last_state = "payment_pages 仍然 requires_approval"
        else:
            checkout_status = str(payload.get("status") or "")
            payment_status = str(payload.get("payment_status") or "")
            if checkout_status == "complete" or payment_status in {"paid", "no_payment_required"}:
                return payload
            last_state = intent_status or str(submission or "waiting")
        time.sleep(1)
    raise RuntimeError(f"BLIK 状态确认超时: {last_state}")


def fetch_redirect_page(stripe: requests.Session, start_url: str) -> list[str]:
    if not start_url or not env_bool("IDEAL_FOLLOW_REDIRECT", True):
        return []
    current = start_url
    qr_urls: list[str] = []
    for hop in range(1, 6):
        resp = stripe.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        dump_http(resp, f"redirect_hop_{hop}", None, "GET", current, force=True)
        qr_urls.extend(extract_qr_candidates(resp.text))
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            break
        current = urljoin(current, location)
    return list(dict.fromkeys(qr_urls))


def resolve_external_redirect(stripe: requests.Session, start_url: str) -> str:
    if not start_url or not env_bool("IDEAL_FOLLOW_REDIRECT", True):
        return start_url
    current = start_url
    for hop in range(1, 6):
        host = (urlparse(current).netloc or "").lower()
        if host.endswith(".ideal.nl") or host == "ideal.nl":
            return current
        try:
            resp = stripe.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
            dump_http(resp, f"resolve_redirect_hop_{hop}", None, "GET", current, force=True)
        except Exception as exc:
            log(f"跟随 redirect 异常: {exc}", "[WARN] ")
            return current
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            return current
        current = urljoin(current, location)
    return current


def approve_proxy_candidates(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    approve_preferences = successful_approve_preferences(checkout_proxy, provider_proxy, [checkout_proxy] + approve_pool)
    if approve_preferences:
        log(f"命中成功 approve 代理优先: {proxy_label(approve_preferences[0])}")
    return list(dict.fromkeys(approve_preferences + [checkout_proxy]))


def run_provider_flow(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    device_id: str,
    checkout: dict[str, str],
    billing: dict[str, str],
    stop_event: Event | None = None,
) -> tuple[str, list[str]]:
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    expected_method = payment_method_type()
    expected_label = payment_method_label()
    stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK

    log(f"Stripe init (PM={billing['country']}, proxy={proxy_label(provider_proxy)})...")
    init_payload = stripe_init(checkout["cs_id"], stripe_pk, provider_proxy)
    hosted_url = str(init_payload.get("stripe_hosted_url") or "")
    if not checkout.get("processor_entity"):
        processor_entity = infer_processor_entity(init_payload)
        if processor_entity:
            checkout["processor_entity"] = processor_entity
            log(f"从 Stripe init 推断 processor_entity={processor_entity}")
    ctx = build_ctx(init_payload, checkout)
    amount = ctx.get("checkout_amount") or 0
    record_checkout_zero_result(checkout_proxy, checkout_country, int(amount or 0))
    amount_major = int(amount or 0) / 100
    log(f"Stripe init 成功, 金额={checkout['currency']} {amount_major:.2f}")
    payment_method_types = first_value_by_key(init_payload, "payment_method_types")
    if isinstance(payment_method_types, list):
        methods = [str(item).lower() for item in payment_method_types]
        log(f"Stripe 可用支付方式: {methods}")
        if expected_method not in methods:
            raise RuntimeError(f"当前 checkout 不支持 {expected_label}，payment_method_types={methods}")
    if env_bool("IDEAL_REQUIRE_ZERO", True) and int(amount or 0) != 0:
        raise RuntimeError(f"0 元优惠未生效，当前金额小单位={amount}，已停止生成非 0 元 {expected_label} 链")
    if int(amount or 0) == 0:
        action = "提交 BLIK 接口" if expected_method == "blik" else "提取 iDEAL 支付页 URL"
        log(f"当前金额为 0，继续按 0 元 promo 流程{action}")
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    if expected_method == "blik":
        stripe = new_session(provider_proxy)
        stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})
        log(
            f"BLIK 接口 confirm: 账单={billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']}"
        )
        confirm_payload = stripe_confirm_blik(
            stripe,
            checkout["cs_id"],
            stripe_pk,
            init_payload,
            ctx,
            checkout,
            hosted_url,
            billing,
        )
        log_payment_page_summary("BLIK confirm", confirm_payload)
        raise_if_setup_intent_blocked(confirm_payload, "BLIK confirm")
        submission = find_submission_attempt(confirm_payload)
        if submission.get("state") == "failed":
            raise RuntimeError(f"Stripe submission failed: {submission}")

        approve_proxy = ""
        if submission.get("state") == "requires_approval":
            log("BLIK confirm 需要 ChatGPT approve...")
            approve_proxies = approve_proxy_candidates(checkout_proxy, provider_proxy, approve_pool)
            approve_proxy = approve_with_retry(
                access_token,
                device_id,
                checkout,
                approve_proxies,
                session_token,
                "checkout",
            )
            log("BLIK approve 成功，轮询 Stripe 后续状态...")

        final_payload = poll_blik_status(stripe, checkout, stripe_pk, ctx)
        log_payment_page_summary("BLIK final", final_payload)
        if approve_proxy:
            record_proxy_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)
        log("BLIK 后续状态确认完成")
        return f"blik://{checkout['cs_id']}", []

    stripe = new_session(provider_proxy)
    stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})

    pm_id = ""
    if env_bool("IDEAL_CONFIRM_INLINE_PM", False):
        log(
            f"iDEAL confirm 内联资料: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']} / bank={os.environ.get('IDEAL_BANK', '') or 'auto'}"
        )
    else:
        log(f"创建 PM (IDEAL): {billing['country']} {billing['name']} / {billing['city']}")
        pm_id = stripe_create_ideal_pm(stripe, checkout["cs_id"], stripe_pk, billing, ctx)
        log(f"PM 创建成功: {pm_id}")
        log(
            f"荷兰资料已填入 PM billing_details: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']}"
        )

    if env_bool("IDEAL_UPDATE_CUSTOMER_DATA", False):
        log(
            f"提交荷兰资料填充: {billing['name']} / {billing['line1']} / "
            f"{billing['city']} {billing['postal_code']} / {billing['email']}"
        )
        stripe_update_customer_data(stripe, checkout["cs_id"], stripe_pk, ctx, billing)

    if env_bool("IDEAL_UPDATE_TAX_REGION", False):
        stripe_update_tax_region(stripe, checkout["cs_id"], stripe_pk, ctx, billing)

    if env_bool("IDEAL_CHECKOUT_SNAPSHOT", False):
        snapshot_chatgpt = build_chatgpt_session(access_token, device_id, provider_proxy, session_token)
        checkout_snapshot(snapshot_chatgpt, checkout, billing)

    log("Stripe confirm (expected=IDEAL)...")
    confirm_payload = stripe_confirm_ideal(stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, checkout, hosted_url, billing)
    log("Stripe confirm 成功, 解析跳转...")
    log_payment_page_summary("confirm", confirm_payload)
    redirect_url = extract_redirect_url(confirm_payload)
    qr_urls = extract_qr_candidates(confirm_payload)
    submission = find_submission_attempt(confirm_payload)

    if redirect_url:
        log(f"confirm 提取到最终扫码/授权 URL: {redirect_url[:180]}")
    if qr_urls:
        log(f"confirm 提取到 QR 候选 {len(qr_urls)} 个")
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    if not redirect_url and submission.get("state") == "requires_approval":
        log("需要 ChatGPT approve...")
        approve_preferences = successful_approve_preferences(checkout_proxy, provider_proxy, [checkout_proxy] + approve_pool)
        if approve_preferences:
            log(f"命中成功 approve 代理优先: {proxy_label(approve_preferences[0])}")
        approve_proxies = list(dict.fromkeys(approve_preferences + [checkout_proxy]))
        log("需要 approve：iDEAL 0 元场景，approve 复用当前 checkout 代理，失败后切下一次尝试。")
        approve_proxy = approve_with_retry(access_token, device_id, checkout, approve_proxies, session_token, "checkout")
        log("跟随跳转提取最终链...")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx)
        qr_urls.extend(poll_qr)
        if redirect_url:
            record_proxy_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)
            log("完成 - 已记忆此 checkout/provider/approve combo")
    elif not redirect_url and not qr_urls:
        log("confirm 未返回真实 iDEAL redirect/QR，继续 poll payment_pages 做最终确认", "[WARN] ")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx)
        qr_urls.extend(poll_qr)

    if redirect_url:
        final_url = resolve_external_redirect(stripe, redirect_url)
        if final_url and final_url != redirect_url:
            log(f"跟随 redirect 得到最终链: {final_url[:180]}")
            redirect_url = final_url

    return redirect_url, list(dict.fromkeys(qr_urls))


def run_once(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    attempt: int,
    max_retry: int,
    stop_event: Event | None = None,
) -> tuple[str, list[str]]:
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")
    device_id = str(uuid.uuid4())
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    billing = ideal_billing_profile()
    expected_label = payment_method_label()
    log(f"开始 {expected_label} 提取，第 {attempt}/{max_retry} 次")
    log(
        "组合测试: "
        f"{checkout_country} / {billing['country']} / {currency_for_country(checkout_country)} / "
        f"{payment_browser_locale()} / {os.environ.get('IDEAL_BANK', '').strip() or 'ANY'} / "
        f"{provider_country_label()}"
    )
    log(f"本轮代理: checkout/资格={proxy_label(checkout_proxy)}；Stripe/{expected_label}={proxy_label(provider_proxy)}")
    ensure_proxy_country("checkout", checkout_proxy)
    ensure_proxy_country("provider", provider_proxy)
    ensure_proxy_targets("checkout", checkout_proxy)
    ensure_proxy_targets("provider", provider_proxy)
    zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
    if zero_status == "ok":
        log(f"checkout 0元资格缓存命中: amount={zero_amount}")
    elif zero_status == "bad":
        log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    try:
        chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
        checkout = create_checkout(chatgpt, checkout_country)
    except Exception as exc:
        if is_user_already_paid_error(exc):
            raise RuntimeError("用户已支付: User is already paid") from exc
        if is_checkout_not_active_error(exc):
            raise
        raise RuntimeError(f"checkout 阶段失败: {exc}") from exc
    return run_provider_flow(
        access_token,
        session_token,
        checkout_proxy,
        provider_proxy,
        approve_pool,
        device_id,
        checkout,
        billing,
        stop_event,
    )


def run_attempt(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    attempt: int,
    max_retry: int,
    stop_event: Event | None = None,
    batch_no: int = 0,
    batch_total: int = 0,
) -> tuple[int, str, list[str], str, str, str]:
    previous_log_context = getattr(_log_context, "prefix", "")
    if batch_no > 0:
        _log_context.prefix = f"[批次 {batch_no}/{batch_total or '?'}][轮次 {attempt}/{max_retry}] "
    else:
        _log_context.prefix = f"[轮次 {attempt}/{max_retry}] "
    try:
        redirect_url, qr_urls = run_once(
            access_token,
            session_token,
            checkout_proxy,
            provider_proxy,
            approve_pool,
            attempt,
            max_retry,
            stop_event,
        )
        has_result = bool(redirect_url)
        if has_result and stop_event:
            stop_event.set()
        if has_result:
            record_proxy_pair_result(checkout_proxy, provider_proxy, True, "success")
        else:
            record_proxy_result("provider", provider_proxy, False, "no_redirect_url")
        _log_context.prefix = previous_log_context
        return attempt, redirect_url, qr_urls, checkout_proxy, provider_proxy, ""
    except Exception as exc:
        error = str(exc)
        if error.startswith("任务已停止"):
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, ""
        if is_user_already_paid_error(error):
            log("检测到 User is already paid：用户已支付，停止任务")
            if stop_event:
                stop_event.set()
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        if "当前 checkout 不支持" in error:
            log(f"第 {attempt}/{max_retry} 轮失败: {error[:300]}，继续尝试后续代理/checkout", "[WARN] ")
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        if is_checkout_not_active_error(error):
            log(
                f"第 {attempt}/{max_retry} 轮 Session 已失效；跳过本轮，不记录代理失败，代理保留供后续使用",
                "[WARN] ",
            )
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        record_failure_by_stage(error, checkout_proxy, provider_proxy)
        log(f"第 {attempt}/{max_retry} 轮失败: {error[:300]}", "[WARN] ")
        _log_context.prefix = previous_log_context
        return attempt, "", [], checkout_proxy, provider_proxy, error


def successful_pair_preferences(checkout_proxies: list[str], provider_proxies: list[str]) -> dict[str, list[str]]:
    if not env_bool("IDEAL_PROXY_SCORE", True):
        return {}
    checkout_by_key = {proxy_key(proxy): proxy for proxy in checkout_proxies}
    provider_by_key = {proxy_key(proxy): proxy for proxy in provider_proxies}
    pair_state = load_proxy_state().get("pair", {})
    if not isinstance(pair_state, dict):
        return {}

    candidates: list[tuple[int, int, str, str]] = []
    for record in pair_state.values():
        if not isinstance(record, dict):
            continue
        success_count = int(record.get("success") or 0)
        if success_count <= 0:
            continue
        checkout_proxy = checkout_by_key.get(str(record.get("checkout") or ""))
        provider_proxy = provider_by_key.get(str(record.get("provider") or ""))
        if checkout_proxy and provider_proxy:
            candidates.append((success_count, int(record.get("last_success") or 0), checkout_proxy, provider_proxy))

    candidates.sort(reverse=True)
    preferences: dict[str, list[str]] = {}
    for _success_count, _last_success, checkout_proxy, provider_proxy in candidates:
        providers = preferences.setdefault(checkout_proxy, [])
        if provider_proxy not in providers:
            providers.append(provider_proxy)
    return preferences


def build_attempt_batches(checkout_proxies: list[str], provider_proxies: list[str], max_attempts: int) -> list[tuple[str, list[str]]]:
    per_checkout = env_int("IDEAL_PROVIDER_PER_CHECKOUT", 30)
    provider_pool = provider_proxies[:]
    preferred_pairs = successful_pair_preferences(checkout_proxies, provider_proxies)
    reserved_provider_owner: dict[str, str] = {}
    for checkout_proxy, preferred_providers in preferred_pairs.items():
        for provider_proxy in preferred_providers:
            reserved_provider_owner.setdefault(provider_proxy, checkout_proxy)
    used_providers: set[str] = set()
    batches: list[tuple[str, list[str]]] = []
    provider_index = 0
    attempt_count = 0
    preferred_count = 0
    for checkout_proxy in checkout_proxies:
        batch: list[str] = []
        for provider_proxy in preferred_pairs.get(checkout_proxy, []):
            if len(batch) >= per_checkout or attempt_count >= max_attempts:
                break
            if provider_proxy in used_providers:
                continue
            batch.append(provider_proxy)
            used_providers.add(provider_proxy)
            attempt_count += 1
            preferred_count += 1
        while len(batch) < per_checkout and provider_index < len(provider_pool) and attempt_count < max_attempts:
            provider_proxy = provider_pool[provider_index]
            provider_index += 1
            if provider_proxy in used_providers:
                continue
            reserved_owner = reserved_provider_owner.get(provider_proxy)
            if reserved_owner and reserved_owner != checkout_proxy:
                continue
            batch.append(provider_proxy)
            used_providers.add(provider_proxy)
            attempt_count += 1
        if batch:
            batches.append((checkout_proxy, batch))
        if attempt_count >= max_attempts:
            break
    if preferred_count:
        log(f"调度命中成功组合优先: {preferred_count} 组")
    return batches


def is_preferred_proxy(group: str, proxy: str) -> bool:
    if not group or not env_bool("IDEAL_PROXY_SCORE", True):
        return False
    state = load_proxy_state().get(group, {})
    if not isinstance(state, dict):
        return False
    record = state.get(proxy_state_key(group, proxy), {})
    if not isinstance(record, dict):
        return False
    if int(record.get("success") or 0) > 0:
        return True
    return group in {"checkout", "seed"} and record.get("zero_ok") is True


def pick_random_proxies(proxies: list[str], limit: int, group: str = "") -> list[str]:
    if group:
        proxies = order_proxy_group(group, proxies)
    preferred = [proxy for proxy in proxies if is_preferred_proxy(group, proxy)]
    preferred_set = set(preferred)
    rest = [proxy for proxy in proxies if proxy not in preferred_set]
    if limit >= len(proxies):
        random.shuffle(rest)
        return preferred + rest
    selected = preferred[:limit]
    remain_count = limit - len(selected)
    if remain_count > 0:
        selected.extend(random.sample(rest, min(remain_count, len(rest))))
    return selected


def run_single_link_attempt(
    access_token: str,
    session_token: str,
    checkout_proxies: list[str],
    provider_proxies: list[str],
    attempt: int,
    ideal_retry: int,
    checkout_retry: int,
    provider_retry: int,
    checkout_country: str,
    checkout_currency: str,
    stop_event: Event,
) -> tuple[int, str, str, bool]:
    previous_log_context = getattr(_log_context, "prefix", "")
    expected_label = payment_method_label()
    _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}] "
    last_error = ""
    approve_blocked = False
    checkout_proxy_used = ""
    try:
        if stop_event.is_set():
            return attempt, "", "任务已停止，跳过本轮", False
        billing = ideal_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        checkout_candidates = pick_random_proxies(checkout_proxies, checkout_retry, "checkout")
        provider_candidates = pick_random_proxies(provider_proxies, provider_retry, "provider")
        if not provider_candidates:
            log("Provider 代理已全部被失败状态过滤，停止本次提链", "[WARN] ")
            return attempt, "", "provider_all_filtered", False
        checkout: dict[str, str] | None = None

        log(f"开始第 {attempt}/{ideal_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{ideal_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"首次 PM 国家: {pm_country}")

        for checkout_index, checkout_proxy in enumerate(checkout_candidates, start=1):
            if stop_event.is_set():
                return attempt, "", "任务已停止，跳过本轮", False
            _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}][PM={pm_country}] "
            try:
                log(f"Checkout {checkout_index}/{len(checkout_candidates)}: {checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}")
                ensure_proxy_country("checkout", checkout_proxy)
                ensure_proxy_targets("checkout", checkout_proxy)
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
                if zero_status == "ok":
                    log(f"checkout 0元资格缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
                chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
                checkout = create_checkout(chatgpt, checkout_country)
                checkout_proxy_used = checkout_proxy
                break
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，停止任务")
                    stop_event.set()
                    return attempt, "", error, False
                if not is_checkout_not_active_error(error):
                    record_failure_by_stage(f"checkout 阶段失败: {error}", checkout_proxy, "")
                log(f"Checkout {checkout_index}/{len(checkout_candidates)} 失败: {error[:220]}", "[WARN] ")

        _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}] "
        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{ideal_retry} 次提链 checkout 阶段失败", "[WARN] ")
            return attempt, "", last_error or "checkout_failed", False

        preferred_providers = successful_pair_preferences([checkout_proxy_used], provider_proxies).get(checkout_proxy_used, [])
        if preferred_providers:
            provider_candidates = list(dict.fromkeys(preferred_providers + provider_candidates))[:provider_retry]
            log(f"命中成功 checkout/provider 组合优先: provider={len(preferred_providers)}")

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log(f"Stripe PK: {stripe_pk[:18]}...")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        for provider_index, provider_proxy in enumerate(provider_candidates, start=1):
            if stop_event.is_set():
                return attempt, "", "任务已停止，跳过本轮", False
            _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}][PM={pm_country}] "
            try:
                log(f"Provider(随机 {provider_index}/{len(provider_candidates)}): {proxy_label(provider_proxy)}")
                ensure_proxy_country("provider", provider_proxy)
                ensure_proxy_targets("provider", provider_proxy)
                redirect_url, _qr_urls = run_provider_flow(
                    access_token,
                    session_token,
                    checkout_proxy_used,
                    provider_proxy,
                    checkout_proxies,
                    device_id,
                    checkout,
                    billing,
                    stop_event,
                )
                if redirect_url:
                    record_proxy_pair_result(checkout_proxy_used, provider_proxy, True, "success")
                    stop_event.set()
                    return attempt, redirect_url, "", False
                last_error = "no_redirect_url"
                record_proxy_result("provider", provider_proxy, False, last_error)
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_checkout_not_active_error(error):
                    log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
                    break
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy)
                log(f"Provider {provider_index}/{len(provider_candidates)} 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error):
                    if "approve blocked" in error:
                        approve_blocked = True
                    log("approve 失败，当前 checkout/approve 组失败，切下一次尝试", "[WARN] ")
                    break
                if "当前 checkout 不支持" in error:
                    log(f"当前 provider 未拿到 {expected_label}，继续尝试下一个 provider", "[WARN] ")
                    continue
                if "0 元优惠未生效" in error:
                    break

        _log_context.prefix = f"[{expected_label} {attempt}/{ideal_retry}] "
        log(f"第 {attempt}/{ideal_retry} 次提链结束，未拿到最终 URL", "[WARN] ")
        return attempt, "", last_error, approve_blocked
    finally:
        _log_context.prefix = previous_log_context


def run_single_link_parallel_mode(access_token: str, session_token: str, checkout_proxies: list[str], provider_proxies: list[str]) -> int:
    checkout_retry = env_int("IDEAL_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("IDEAL_PROVIDER_RETRY_MAX", 3)
    ideal_retry = env_int("IDEAL_MAX_RETRY", 5)
    requested_workers = env_int("IDEAL_WORKERS", 1)
    worker_limit = env_int("IDEAL_WORKERS_MAX", requested_workers)
    workers = min(max(1, requested_workers), max(1, worker_limit), ideal_retry)
    expected_label = payment_method_label()
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    checkout_currency = currency_for_country(checkout_country)
    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", ideal_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    if requested_workers > workers:
        log(f"{expected_label}并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(
        f"开始执行 {expected_label} 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={provider_country_label()}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，{expected_label}总重试={ideal_retry}，{expected_label}并发={workers}。"
    )

    executor = ThreadPoolExecutor(max_workers=workers)
    futures: dict[Any, int] = {}
    try:
        for attempt in range(1, ideal_retry + 1):
            futures[
                executor.submit(
                    run_single_link_attempt,
                    access_token,
                    session_token,
                    checkout_proxies,
                    provider_proxies,
                    attempt,
                    ideal_retry,
                    checkout_retry,
                    provider_retry,
                    checkout_country,
                    checkout_currency,
                    stop_event,
                )
            ] = attempt

        for future in as_completed(futures):
            try:
                attempt, redirect_url, error, approve_blocked = future.result()
            except Exception as exc:
                attempt = futures.get(future, 0)
                redirect_url = ""
                error = str(exc)
                approve_blocked = False
                log(f"第 {attempt}/{ideal_retry} 次提链异常: {error[:300]}", "[WARN] ")
            if redirect_url:
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                print("\n===== 结果 =====")
                print_result_url(redirect_url)
                return 0
            last_error = error or last_error
            if is_user_already_paid_error(error):
                log("检测到 User is already paid：用户已支付，任务正常结束")
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                return 0
            if approve_blocked:
                approve_blocked_count += 1
                log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
            if approve_blocked_count >= max_blocked:
                log("达到当前账号 approve blocked 上限，停止继续提交新提链", "[WARN] ")
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                return 1
    finally:
        executor.shutdown(wait=True, cancel_futures=stop_event.is_set())

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def run_single_link_mode(access_token: str, session_token: str, checkout_proxies: list[str], provider_proxies: list[str]) -> int:
    ideal_workers = env_int("IDEAL_WORKERS", 1)
    if ideal_workers > 1:
        return run_single_link_parallel_mode(access_token, session_token, checkout_proxies, provider_proxies)

    checkout_retry = env_int("IDEAL_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("IDEAL_PROVIDER_RETRY_MAX", 3)
    ideal_retry = env_int("IDEAL_MAX_RETRY", 5)
    expected_label = payment_method_label()
    checkout_country = normalize_country(os.environ.get("IDEAL_CHECKOUT_COUNTRY", default_payment_country()))
    checkout_currency = currency_for_country(checkout_country)
    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", ideal_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    log(
        f"开始执行 {expected_label} 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={provider_country_label()}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，{expected_label}总重试={ideal_retry}。"
    )

    for attempt in range(1, ideal_retry + 1):
        billing = ideal_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        checkout_candidates = pick_random_proxies(checkout_proxies, checkout_retry, "checkout")
        provider_candidates = pick_random_proxies(provider_proxies, provider_retry, "provider")
        if not provider_candidates:
            last_error = "Provider 代理已全部被失败状态过滤"
            log(last_error, "[WARN] ")
            break
        checkout: dict[str, str] | None = None
        checkout_proxy_used = ""

        log(f"开始第 {attempt}/{ideal_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{ideal_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"  首次 PM 国家: {pm_country}")

        for checkout_index, checkout_proxy in enumerate(checkout_candidates, start=1):
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"  [PM={pm_country}] "
            try:
                log(f"Checkout {checkout_index}/{len(checkout_candidates)}: {checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}")
                ensure_proxy_country("checkout", checkout_proxy)
                ensure_proxy_targets("checkout", checkout_proxy)
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
                if zero_status == "ok":
                    log(f"checkout 0元资格缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
                chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
                checkout = create_checkout(chatgpt, checkout_country)
                checkout_proxy_used = checkout_proxy
                break
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，任务正常结束")
                    return 0
                if not is_checkout_not_active_error(error):
                    record_failure_by_stage(f"checkout 阶段失败: {error}", checkout_proxy, "")
                log(f"Checkout {checkout_index}/{len(checkout_candidates)} 失败: {error[:220]}", "[WARN] ")
            finally:
                _log_context.prefix = previous_log_context

        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{ideal_retry} 次提链 checkout 阶段失败，换下一次提链", "[WARN] ")
            continue

        preferred_providers = successful_pair_preferences([checkout_proxy_used], provider_proxies).get(checkout_proxy_used, [])
        if preferred_providers:
            provider_candidates = list(dict.fromkeys(preferred_providers + provider_candidates))[:provider_retry]
            log(f"命中成功 checkout/provider 组合优先: provider={len(preferred_providers)}")

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log(f"Stripe PK: {stripe_pk[:18]}...")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        for provider_index, provider_proxy in enumerate(provider_candidates, start=1):
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"  [PM={pm_country}] "
            try:
                log(f"Provider(随机 {provider_index}/{len(provider_candidates)}): {proxy_label(provider_proxy)}")
                ensure_proxy_country("provider", provider_proxy)
                ensure_proxy_targets("provider", provider_proxy)
                redirect_url, _qr_urls = run_provider_flow(
                    access_token,
                    session_token,
                    checkout_proxy_used,
                    provider_proxy,
                    checkout_proxies,
                    device_id,
                    checkout,
                    billing,
                    stop_event,
                )
                if redirect_url:
                    record_proxy_pair_result(checkout_proxy_used, provider_proxy, True, "success")
                    print("\n===== 结果 =====")
                    print_result_url(redirect_url)
                    return 0
                last_error = "no_redirect_url"
                record_proxy_result("provider", provider_proxy, False, last_error)
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_checkout_not_active_error(error):
                    log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
                    break
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy)
                log(f"Provider {provider_index}/{len(provider_candidates)} 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error):
                    if "approve blocked" in error:
                        approve_blocked_count += 1
                        log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
                    log("approve 失败，当前 checkout/approve 组失败，切下一次尝试", "[WARN] ")
                    break
                if "当前 checkout 不支持" in error:
                    log(f"当前 provider 未拿到 {expected_label}，继续尝试下一个 provider", "[WARN] ")
                    continue
                if "0 元优惠未生效" in error:
                    break
            finally:
                _log_context.prefix = previous_log_context

        if approve_blocked_count >= max_blocked:
            log("达到当前账号 approve blocked 上限，停止继续提交新提链", "[WARN] ")
            return 1
        log(f"第 {attempt}/{ideal_retry} 次提链结束，未拿到最终 URL", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def run_single_seed_mode(access_token: str, session_token: str, proxy_seeds: list[str]) -> int:
    checkout_retry = env_int("IDEAL_CHECKOUT_RETRY_MAX", 5)
    max_retry = env_int("IDEAL_MAX_RETRY", 5)
    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", max_retry)
    attempted_seed_keys: set[str] = set()
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    log(
        "开始执行 BLIK 单 Seed 链路："
        f"PL checkout -> PL Stripe/BLIK -> PL approve；"
        f"每轮候选={checkout_retry}，总重试={max_retry}。"
    )
    for attempt in range(1, max_retry + 1):
        available_seeds = [
            proxy_seed
            for proxy_seed in proxy_seeds
            if proxy_chain_key(proxy_seed) not in attempted_seed_keys
        ]
        seed_candidates = pick_random_proxies(available_seeds, checkout_retry, "seed")
        if not seed_candidates:
            last_error = last_error or "本次任务的代理 Seed 已全部尝试"
            log("本次任务的代理 Seed 已全部尝试，不再重复失败节点", "[WARN] ")
            break

        log(f"开始第 {attempt}/{max_retry} 次 BLIK 提交")
        for seed_index, proxy_seed in enumerate(seed_candidates, start=1):
            chain_key = proxy_chain_key(proxy_seed)
            attempted_seed_keys.add(chain_key)
            checkout_proxy = ""
            provider_proxy = ""
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"[BLIK {attempt}/{max_retry}] "
            try:
                checkout_proxy, provider_proxy = blik_proxy_chain(proxy_seed)
                log_blik_proxy_chain(proxy_seed, checkout_proxy, provider_proxy)
                log(
                    f"Seed {seed_index}/{len(seed_candidates)}: 本次已尝试 Seed={len(attempted_seed_keys)}"
                )
                completion, _qr_urls = run_once(
                    access_token,
                    session_token,
                    checkout_proxy,
                    provider_proxy,
                    [checkout_proxy],
                    attempt,
                    max_retry,
                    stop_event,
                )
                if completion:
                    record_proxy_result("seed", proxy_seed, True, "success")
                    print("\n===== 结果 =====")
                    print_result_url(completion)
                    return 0
                last_error = "no_blik_completion"
                record_seed_failure(proxy_seed, last_error)
                log("BLIK 未返回完成状态，换下一个未尝试 Seed", "[WARN] ")
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，任务正常结束")
                    return 0
                record_seed_failure(proxy_seed, error)
                if is_checkout_not_active_error(error):
                    log("Session 已失效；当前 Seed 不记录冷却，但本次任务不会重复抽取", "[WARN] ")
                elif "当前 checkout 不支持" in error:
                    log("当前 checkout 未提供 BLIK，换下一个未尝试 Seed", "[WARN] ")
                else:
                    log(f"Seed {seed_index}/{len(seed_candidates)} 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error) and "approve blocked" in error:
                    approve_blocked_count += 1
                    log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
            finally:
                _log_context.prefix = previous_log_context

            if approve_blocked_count >= max_blocked:
                log("达到当前账号 approve blocked 上限，停止继续提交", "[WARN] ")
                return 1

        log(f"第 {attempt}/{max_retry} 次 BLIK 提交结束，未完成", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def run_legacy_two_pool_mode() -> int:
    if not validate_blik_code_before_start():
        return 1
    access_token, session_token = load_token()
    if not access_token:
        log("access_token 为空", "[ERROR] ")
        return 1

    checkout_proxies, provider_proxies = load_proxy_groups()
    if os.environ.get("IDEAL_FLOW_MODE", "").strip().lower() == "single":
        return run_single_link_mode(access_token, session_token, checkout_proxies, provider_proxies)

    per_checkout = env_int("IDEAL_PROVIDER_PER_CHECKOUT", 30)
    total_capacity = min(len(provider_proxies), len(checkout_proxies) * per_checkout)
    raw_max_retry = os.environ.get("IDEAL_MAX_RETRY", "").strip()
    if raw_max_retry:
        max_retry = env_int("IDEAL_MAX_RETRY", 3)
    else:
        raw_account_attempts = os.environ.get("IDEAL_MAX_ACCOUNT_ATTEMPTS", "").strip()
        if raw_account_attempts:
            try:
                max_retry = int(raw_account_attempts)
            except ValueError:
                max_retry = total_capacity
            if max_retry <= 0:
                max_retry = total_capacity
        else:
            max_retry = min(total_capacity, per_checkout * 30)
        log(f"未设置 IDEAL_MAX_RETRY，默认每批 {per_checkout} 轮、最多 30 批: 最多 {max_retry} 次")

    max_retry = min(max_retry, total_capacity)
    attempt_batches = build_attempt_batches(checkout_proxies, provider_proxies, max_retry)
    max_retry = sum(len(provider_batch) for _checkout_proxy, provider_batch in attempt_batches)
    if max_retry <= 0:
        log("代理为空，无法开始", "[ERROR] ")
        return 1

    max_blocked = env_int("IDEAL_MAX_APPROVE_BLOCKED", max_retry)
    expected_label = payment_method_label()
    checkout_proxy_country = expected_proxy_country("checkout")
    log(f"调度策略: 每批 1 个 checkout/{checkout_proxy_country} 搭配 {per_checkout} 个随机 provider；整批失败后换下一批；approve blocked 上限={max_blocked}")

    requested_workers = env_int("IDEAL_WORKERS", 30)
    worker_limit = env_int("IDEAL_WORKERS_MAX", 30)
    workers = min(requested_workers, worker_limit, per_checkout, max_retry)
    if requested_workers > workers:
        log(f"正式流程并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(f"正式流程并发: 每批 workers={workers}, batches={len(attempt_batches)}, attempts={max_retry}")

    last_error = ""
    approve_blocked_count = 0
    stop_event = Event()
    attempt_no = 0

    for batch_no, (checkout_proxy, provider_batch) in enumerate(attempt_batches, start=1):
        if stop_event.is_set():
            break
        batch_workers = min(workers, len(provider_batch))
        log(
            f"开始第 {batch_no}/{len(attempt_batches)} 批: "
            f"checkout/{checkout_proxy_country}={proxy_label(checkout_proxy)}，provider={len(provider_batch)}，并发={batch_workers}"
        )
        executor = ThreadPoolExecutor(max_workers=batch_workers)
        futures: dict[Any, int] = {}
        batch_success_url = ""
        try:
            for provider_proxy in provider_batch:
                attempt_no += 1
                future = executor.submit(
                    run_attempt,
                    access_token,
                    session_token,
                    checkout_proxy,
                    provider_proxy,
                    checkout_proxies,
                    attempt_no,
                    max_retry,
                    stop_event,
                    batch_no,
                    len(attempt_batches),
                )
                futures[future] = attempt_no

            for future in as_completed(futures):
                try:
                    attempt, redirect_url, qr_urls, _checkout, _provider, error = future.result()
                except Exception as exc:
                    attempt = futures.get(future, 0)
                    redirect_url = ""
                    error = str(exc)
                    log(f"第 {attempt}/{max_retry} 轮异常: {error[:300]}", "[WARN] ")
                if redirect_url:
                    batch_success_url = redirect_url
                    stop_event.set()
                    break
                last_error = error or last_error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，任务正常结束")
                    stop_event.set()
                    return 0
                if "当前 checkout 不支持" in (error or ""):
                    log(f"第 {attempt}/{max_retry} 轮未提供 {expected_label}，继续尝试后续代理/checkout", "[WARN] ")
                    continue
                if "approve blocked" in (error or ""):
                    approve_blocked_count += 1
                    log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
                if approve_blocked_count >= max_blocked:
                    log("达到当前账号 approve blocked 上限，停止继续提交新批次", "[WARN] ")
                    stop_event.set()
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=stop_event.is_set())

        if batch_success_url:
            print("\n===== 结果 =====")
            print_result_url(batch_success_url)
            return 0
        if approve_blocked_count >= max_blocked:
            return 1
        log(f"第 {batch_no}/{len(attempt_batches)} 批结束，未拿到最终 URL", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def main() -> int:
    if not validate_blik_code_before_start():
        return 1
    access_token, session_token = load_token()
    if not access_token:
        log("access_token 为空", "[ERROR] ")
        return 1

    proxy_seeds = load_proxy_seeds()
    os.environ.setdefault("IDEAL_PROXY_SEED_FILE", str(proxy_seed_file()))
    flow_mode = os.environ.get("IDEAL_FLOW_MODE", "single").strip().lower() or "single"
    if flow_mode != "single":
        log(f"IDEAL_FLOW_MODE={flow_mode} 已收敛为 BLIK 单 Seed 链路", "[WARN] ")
    return run_single_seed_mode(access_token, session_token, proxy_seeds)


if __name__ == "__main__":
    sys.exit(main())
