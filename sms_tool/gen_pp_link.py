#!/usr/bin/env python3
r"""PP 直链生成器 -- 分段代理池版。

参考 F:\epsoft\app\app.py 的三段式代理路由:
  Stage 1: checkout (JP/TH 代理) → 创建 ChatGPT checkout session
  Stage 2: provider (目标国代理) → Stripe init + create PM + confirm
  Stage 3: approve  (目标国代理) → ChatGPT approve + 轮询 redirect → 提取 BA 链

用法:
  # 分段代理模式 (checkout→JP, provider/approve→GB)
  python pp_link_v2.py <token> --checkout-proxy "http://user:pass-JP@gate:1000" --provider-proxy "http://user:pass-GB@gate:1000" --target GB

  # 代理模板批量模式 (自动替换国家码)
  python pp_link_v2.py <token> --proxy-template "user:pass-XX@gate:1000" --batch --target-countries AU,GB,DE

  # 单代理模式 (所有阶段用同一代理)
  python pp_link_v2.py <token> --proxy "http://user:pass@gate:1000"

配置说明:
  --checkout-proxy   Stage 1 代理 (默认 JP 出口)
  --provider-proxy   Stage 2 代理 (目标国出口)
  --approve-proxy    Stage 3 代理 (目标国出口，默认同 provider)
  --proxy            单代理模式，所有阶段用同一代理
  --proxy-template   代理模板，配合 --batch 使用
  --target           目标国家 (默认 DE)
  --batch            批量矩阵模式
  --no-require-zero  允许非零金额 (默认要求 0 元)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit

import requests

try:
    from .paypal_proxy import (
        PayPalProxyState,
        infer_proxy_country,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
    )
except ImportError:  # pragma: no cover - direct script execution
    from paypal_proxy import (  # type: ignore
        PayPalProxyState,
        infer_proxy_country,
        is_retryable_network_error,
        probe_proxy,
        redact_proxy_url,
        rotate_proxy_session as rotate_stage_proxy_session,
    )

# curl_cffi functional API (preferred for checkout to avoid Session cookie conflicts)
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None
_CurlCffiSession = None  # Session API disabled — use functional API instead

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
RETRY_ATTEMPTS = 3
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PAYPAL_PROXY_STATE_CACHE: dict[tuple[Any, ...], PayPalProxyState] = {}


class CheckoutNotZeroDueError(Exception):
    def __init__(self, amount: int | None, currency: str = ""):
        self.amount = amount
        self.currency = str(currency or "").upper()
        amount_label = "unknown" if amount is None else str(amount)
        super().__init__(f"checkout_not_zero_due: amount={amount_label} {self.currency}".rstrip())


CURRENCY_MAP = {
    "US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "JP": "JPY",
    "AU": "AUD", "CA": "CAD", "SG": "SGD", "NZ": "NZD", "IE": "EUR",
    "TH": "THB", "ID": "IDR", "IN": "INR", "BR": "BRL", "KR": "KRW",
    "TR": "TRY",
}

BILLING_DATA = {
    "DE": {"name": ("Lukas", "Schneider"), "street": "Friedrichstrasse 123", "city": "Berlin", "state": "BE", "postal": "10117"},
    "GB": {"name": ("James", "Smith"), "street": "10 Downing Street", "city": "London", "state": "London", "postal": "SW1A 2AA"},
    "US": {"name": ("James", "Smith"), "street": "3110 Sunset Boulevard", "city": "Los Angeles", "state": "CA", "postal": "90026"},
    "AU": {"name": ("Oliver", "Smith"), "street": "123 George Street", "city": "Sydney", "state": "NSW", "postal": "2000"},
    "JP": {"name": ("Taro", "Yamada"), "street": "1-1-2 Oshiage", "city": "Sumida-ku", "state": "Tokyo", "postal": "131-0045"},
    "FR": {"name": ("Pierre", "Dupont"), "street": "10 Rue de Rivoli", "city": "Paris", "state": "Ile-de-France", "postal": "75001"},
    "CA": {"name": ("James", "Smith"), "street": "100 King Street W", "city": "Toronto", "state": "ON", "postal": "M5X 1C6"},
    "SG": {"name": ("Wei", "Tan"), "street": "1 Raffles Place", "city": "Singapore", "state": "Singapore", "postal": "048616"},
    "NZ": {"name": ("James", "Smith"), "street": "1 Queen Street", "city": "Auckland", "state": "Auckland", "postal": "1010"},
    "IE": {"name": ("James", "Smith"), "street": "1 O'Connell Street", "city": "Dublin", "state": "Dublin", "postal": "D01 F5P2"},
    "TH": {"name": ("Somchai", "Prasert"), "street": "123 Sukhumvit Road", "city": "Bangkok", "state": "Bangkok", "postal": "10110"},
    "TR": {"name": ("Mehmet", "Yilmaz"), "street": "Istiklal Caddesi 123", "city": "Istanbul", "state": "Istanbul", "postal": "34421"},
    "IN": {"name": ("Rahul", "Sharma"), "street": "Flat 302, Sai Residency", "city": "Mumbai", "state": "Maharashtra", "postal": "400069"},
    "BR": {"name": ("Joao", "Silva"), "street": "Avenida Paulista 1000", "city": "Sao Paulo", "state": "SP", "postal": "01310-100"},
    "KR": {"name": ("Minjun", "Kim"), "street": "123 Teheran-ro", "city": "Seoul", "state": "Seoul", "postal": "06134"},
}

DEFAULT_TARGET_COUNTRIES = ("AU", "TH", "US", "GB", "DE", "JP", "SG", "NZ", "CA", "IE")
DEFAULT_CHECKOUT_COUNTRIES = ("JP", "TH")

PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\s\"'<>]+", re.I)
PAYPAL_BA_RE = re.compile(r"https://www\.paypal\.com/agreements/approve\?[^\s\"']+", re.I)

# ─── UPI 常量 ──────────────────────────────────────────────────────────────────

UPI_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
UPI_CHECKOUT_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
UPI_CHECKOUT_APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
STRIPE_PAYMENT_PAGE_INIT_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/init"
STRIPE_PAYMENT_PAGE_CONFIRM_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
STRIPE_PAYMENT_PAGE_GET_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}"
UPI_APPROVAL_MAX_ATTEMPTS = 60
UPI_QR_POLL_MAX_ATTEMPTS = 30
UPI_QR_POLL_INTERVAL = 1.0

UPI_BILLING_IN = {
    "name": "Rahul Sharma",
    "email": "upi-scanner@example.com",
    "line1": "Flat 302, Sai Residency",
    "line2": "MG Road, Andheri East",
    "city": "Mumbai",
    "state": "Maharashtra",
    "postal": "400069",
    "country": "IN",
}

# ─── Session 工厂 ─────────────────────────────────────────────────────────────


def _new_session(proxy: str = ""):
    """Create a requests Session for non-checkout stages (Stripe, approve, etc.).
    The checkout stage uses ``_checkout_post`` instead to avoid Cloudflare
    session-cookie conflicts."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _checkout_post(url, json_body, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
    """Execute a ChatGPT checkout POST using the functional curl_cffi API.

    The functional API (not Session) is required here because ``curl_cffi``
    Session accumulates a Cloudflare ``__cf_bm`` cookie that conflicts with
    the account's own cookies and causes 403 Forbidden on checkout.

    ``extra_headers`` lets callers add endpoint-specific headers such as
    ``x-openai-target-path``/``x-openai-target-route`` for /checkout/update
    and /checkout/taxes, plus a per-session Referer.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if extra_headers:
        headers.update(extra_headers)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if curl_requests is not None:
        return curl_requests.post(url, json=json_body, headers=headers, proxies=proxies, timeout=timeout, impersonate="chrome")
    return requests.post(url, json=json_body, headers=headers, proxies=proxies, timeout=timeout)


# ─── 代理工具 ──────────────────────────────────────────────────────────────────


def normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if proxy and "://" not in proxy:
        return f"http://{proxy}"
    return proxy


def normalize_proxy_template(template: str) -> str:
    """规范化代理模板，支持多种格式:
    - 标准: user:pass@host:port
    - 反转: host:port@user:pass
    - 冒号分隔: host:port:user:pass
    """
    proxy = str(template or "").strip()
    if not proxy:
        return proxy

    # 格式1: host:port:user:pass (无@符号，4段冒号分隔)
    if "@" not in proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            if "." in host and port.isdigit():
                return normalize_proxy_url(f"{user}:{pwd}@{host}:{port}")
        return normalize_proxy_url(proxy)

    # 格式2/3: 包含@符号
    parts = proxy.split("@")
    if len(parts) != 2:
        return normalize_proxy_url(proxy)
    left, right = parts
    # 检测反转格式: host:port@user:pass
    # 标准格式: user:pass@host:port
    # 判断依据: 左侧包含端口号且含点号(域名特征)
    if re.match(r"^[a-zA-Z0-9\.\-]+:\d+$", left) and "." in left.split(":")[0]:
        # 反转格式，交换
        return normalize_proxy_url(f"{right}@{left}")
    return normalize_proxy_url(proxy)


def proxy_for_country_template(template: str, country: str) -> str:
    """从代理模板中替换国家码，生成目标国家的代理 URL。"""
    proxy = normalize_proxy_template(template)
    country = str(country or "").strip().upper()
    if not proxy or not country:
        return proxy
    userinfo, separator, host = proxy.rpartition("@")
    if not separator:
        return proxy
    # 替换 region-XX
    replaced, count = re.subn(r"region-[A-Za-z]{2}(?=$|[-_:])", f"region-{country}", userinfo, count=1)
    if count != 1:
        # 尝试替换末尾 -XX
        replaced, count = re.subn(r"-[A-Za-z]{2}$", f"-{country}", userinfo)
    elif country != "JP":
        # 非JP国家移除 -st-...-city-... 段
        replaced = re.sub(r"-st-[^-@]+-city-[^-@]+(?=-sid-)", "", replaced, count=1)
    if count != 1:
        return proxy
    return normalize_proxy_url(f"{replaced}@{host}")


def rotate_proxy_session(proxy: str) -> str:
    """轮换代理的 session ID (支持 Kookeey 数字 sid 和 cliproxy 字母 sid)。"""
    return rotate_stage_proxy_session(proxy)


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    direct = payload.get("submission_attempt")
    if isinstance(direct, dict):
        return direct
    for value in payload.values():
        if isinstance(value, dict):
            found = find_submission_attempt(value)
            if found:
                return found
        elif isinstance(value, list):
            for item in value:
                found = find_submission_attempt(item)
                if found:
                    return found
    return {}


def _compact_diagnostic(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def stripe_confirm_error_diagnostics(response: Any, cs_id: str, pm_id: str, init_payload: dict) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {}
    submission = find_submission_attempt(payload)
    parts = [
        f"stripe_confirm_failed:http={getattr(response, 'status_code', 0)}",
        f"cs_id={str(cs_id)[:18]}",
        f"pm_id={str(pm_id)[:18]}",
        f"amount={stripe_amount_details(init_payload).get('amount')}",
        f"init_checksum={'present' if init_payload.get('init_checksum') else 'missing'}",
    ]
    for label, value in (
        ("error_type", error.get("type")),
        ("error_code", error.get("code")),
        ("error_param", error.get("param")),
        ("error_message", error.get("message")),
        ("submission_state", submission.get("state")),
        ("submission_reason", submission.get("reason")),
        ("submission_code", submission.get("code")),
        ("submission_message", submission.get("message")),
    ):
        if value not in (None, ""):
            parts.append(f"{label}={_compact_diagnostic(value)}")
    if len(parts) == 5:
        parts.append(f"body={_compact_diagnostic(getattr(response, 'text', ''))}")
    return "; ".join(parts)


# ─── URL 提取 ──────────────────────────────────────────────────────────────────


def is_paypal_ba_approve_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not (host == "paypal.com" or host.endswith(".paypal.com")):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())


def extract_ba_token(url: str) -> str:
    marker = "ba_token="
    lower = url.lower()
    if marker not in lower:
        return ""
    start = lower.find(marker) + len(marker)
    end = len(url)
    for sep in ("&", "#", '"', "'", " "):
        pos = url.find(sep, start)
        if pos != -1:
            end = min(end, pos)
    return url[start:end]


def find_url_in_value(value: Any, patterns: list[re.Pattern]) -> str:
    if isinstance(value, str):
        for pat in patterns:
            m = pat.search(value)
            if m:
                return m.group(0)
    if isinstance(value, dict):
        # 优先检查已知字段
        for key in ("url", "redirect_url", "return_url"):
            if key in value:
                found = find_url_in_value(value[key], patterns)
                if found:
                    return found
        for child in value.values():
            found = find_url_in_value(child, patterns)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_url_in_value(child, patterns)
            if found:
                return found
    return ""


def extract_redirect_url(payload: dict) -> str:
    """从 Stripe confirm/poll 响应中提取 redirect URL (仅返回真正的 PayPal/Stripe 授权链接)。"""
    # 检查 next_action.redirect_to_url
    next_action = payload.get("next_action") or {}
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        rurl = next_action.get("redirect_to_url") or {}
        if isinstance(rurl, dict) and rurl.get("url"):
            return str(rurl["url"])

    # 递归搜索 (仅匹配 pm-redirects 和 PayPal approve)
    url = find_url_in_value(payload, [PM_REDIRECT_RE, PAYPAL_BA_RE])
    if url:
        return url

    # 检查 setup_intent/payment_intent 的 next_action
    for si_key in ("setup_intent", "payment_intent"):
        si = payload.get(si_key) or {}
        if isinstance(si, dict):
            na = si.get("next_action") or {}
            if isinstance(na, dict) and na.get("type") == "redirect_to_url":
                rurl = na.get("redirect_to_url") or {}
                if isinstance(rurl, dict) and rurl.get("url"):
                    return str(rurl["url"])

    return ""


def resolve_external_redirect(session: Any, redirect_url: str, max_hops: int = 5) -> str:
    """跟随 redirect 链，直到找到 PayPal BA approve URL。"""
    current = redirect_url
    for _ in range(max_hops):
        if not current:
            return ""
        if is_paypal_ba_approve_url(current):
            return current
        try:
            resp = session.get(current, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        except Exception:
            return current
        if resp.status_code not in (301, 302, 303, 307, 308):
            return current
        location = str(resp.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


# ─── 账单地址 ──────────────────────────────────────────────────────────────────


def billing_for_country(country: str) -> dict:
    data = BILLING_DATA.get(country.upper()) or BILLING_DATA.get("DE")
    return {
        "country": country.upper(),
        "name": data["name"],
        "email": f"buyer{random.randint(1000,9999)}@example.{country.lower()}",
        "street": data["street"],
        "city": data["city"],
        "state": data["state"],
        "postal": data["postal"],
    }


# ─── Stripe 金额检查 ──────────────────────────────────────────────────────────


def stripe_amount_details(init_payload: dict) -> dict:
    if not isinstance(init_payload, dict):
        return {"amount": None, "currency": "", "source": "unknown"}
    currency = str(init_payload.get("currency") or "").lower()
    total_summary = init_payload.get("total_summary") or {}
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return {"amount": int(total_summary["due"]), "currency": str(total_summary.get("currency") or currency).lower(), "source": "total_summary.due"}
    invoice = init_payload.get("invoice") or {}
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return {"amount": int(invoice["amount_due"]), "currency": str(invoice.get("currency") or currency).lower(), "source": "invoice.amount_due"}
    return {"amount": None, "currency": currency, "source": "unknown"}


# ─── 核心流程 ──────────────────────────────────────────────────────────────────


class PPLinkExtractor:
    """三段式代理提链器。"""

    def __init__(
        self,
        access_token: str,
        checkout_proxy: str = "",
        provider_proxy: str = "",
        stripe_init_proxy: str = "",
        payment_method_proxy: str = "",
        confirm_proxy: str = "",
        approve_proxy: str = "",
        promotion_proxy: str = "",
        target_country: str = "DE",
        checkout_country: str = "",
        stripe_pk: str = "",
        require_zero: bool = True,
        emit: Any = None,
        cookie_header: str = "",
        promotion_taxes: bool = False,
        promo_campaign_id: str = "plus-1-month-free",
        preflight_proxy_check: bool = False,
        rotate_proxy_sessions: bool = False,
        proxy_probe_timeout: float = 12,
        max_stage_retries: int = RETRY_ATTEMPTS,
        proxy_state: PayPalProxyState | None = None,
        stage_proxy_countries: dict[str, str] | None = None,
    ):
        self.access_token = access_token
        self.checkout_proxy = normalize_proxy_url(checkout_proxy)
        self.provider_proxy = normalize_proxy_url(provider_proxy)
        self.stripe_init_proxy = normalize_proxy_url(stripe_init_proxy or provider_proxy)
        self.payment_method_proxy = normalize_proxy_url(payment_method_proxy or provider_proxy)
        self.confirm_proxy = normalize_proxy_url(confirm_proxy or provider_proxy)
        self.approve_proxy = normalize_proxy_url(approve_proxy or provider_proxy)
        # Promotion stage: apply the 0-due promo to the *existing* checkout via
        # POST /backend-api/payments/checkout/update, routed through a
        # promo-eligible region egress. Empty => stage disabled (behaviour
        # unchanged). This is what makes "0元 + PayPal" possible on one session:
        # the checkout is created in a PayPal region, then the promo is attached
        # from a promo-eligible region.
        self.promotion_proxy = normalize_proxy_url(promotion_proxy)
        self.enable_promotion = bool(self.promotion_proxy)
        self.promotion_taxes = bool(promotion_taxes)
        self.promo_campaign_id = str(promo_campaign_id or "plus-1-month-free")
        self.target_country = target_country.upper()
        self.checkout_country = (checkout_country or target_country).upper()
        self.currency = CURRENCY_MAP.get(self.target_country, "EUR")
        self.checkout_currency = CURRENCY_MAP.get(self.checkout_country, "USD")
        self.stripe_pk = stripe_pk or DEFAULT_STRIPE_PK
        self.require_zero = require_zero
        self.emit = emit or (lambda step, msg, **kw: None)
        self.cookie_header = cookie_header or ""
        self.runtime_version = "6f8494a281"
        self.stripe_js_id = str(uuid.uuid4())
        self.elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        self.elements_session_config_id = str(uuid.uuid4())
        self.preflight_proxy_check = bool(preflight_proxy_check)
        self.rotate_proxy_sessions = bool(rotate_proxy_sessions)
        self.proxy_probe_timeout = max(1.0, float(proxy_probe_timeout or 12))
        self.max_stage_retries = max(1, int(max_stage_retries or RETRY_ATTEMPTS))
        self.proxy_state = proxy_state or PayPalProxyState(
            Path(PROJECT_ROOT) / "runtime" / "paypal_proxy_state.json",
            enabled=False,
        )
        countries = stage_proxy_countries if isinstance(stage_proxy_countries, dict) else {}
        self.stage_proxy_countries = {
            "checkout": str(countries.get("checkout") or self.checkout_country).upper(),
            "promotion": str(countries.get("promotion") or "").upper(),
            "provider": str(countries.get("provider") or self.target_country).upper(),
            "stripe_init": str(countries.get("stripe_init") or countries.get("provider") or self.target_country).upper(),
            "payment_method": str(countries.get("payment_method") or countries.get("provider") or self.target_country).upper(),
            "confirm": str(countries.get("confirm") or countries.get("provider") or self.target_country).upper(),
            "approve": str(countries.get("approve") or self.target_country).upper(),
        }
        self.proxy_exits: dict[str, dict[str, Any]] = {}
        self._active_stage = ""

    def _log(self, step: str, msg: str, **kw):
        self.emit(step, msg, **kw)

    def _reset_stripe_context(self) -> None:
        self.stripe_js_id = str(uuid.uuid4())
        self.elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        self.elements_session_config_id = str(uuid.uuid4())

    def _set_stripe_proxy(self, proxy: str) -> Any:
        stripe = getattr(self, "_stripe_session", None)
        if stripe is None:
            stripe = _new_session(proxy)
            self._stripe_session = stripe
        elif hasattr(stripe, "proxies"):
            stripe.proxies = {"http": proxy, "https": proxy} if proxy else {}
        return stripe

    def _prepare_stage_proxy(self, stage: str, proxy: str, attempt: int = 1) -> str:
        expected_country = self.stage_proxy_countries.get(stage, "")
        prepared = normalize_proxy_url(proxy)
        if prepared and self.rotate_proxy_sessions:
            prepared = rotate_stage_proxy_session(prepared, expected_country)
        label = redact_proxy_url(prepared)
        if not prepared:
            self.proxy_exits[stage] = {"ok": True, "country_code": "", "ip": "", "proxy": "DIRECT"}
            return prepared
        if not self.preflight_proxy_check:
            self._log("proxy", f"{stage} attempt={attempt} proxy={label} (preflight disabled)")
            return prepared
        self._log("proxy", f"{stage} attempt={attempt} checking expected={expected_country or 'ANY'} proxy={label}")
        result = probe_proxy(
            prepared,
            expected_country=expected_country,
            stage=stage,
            timeout=self.proxy_probe_timeout,
        )
        self.proxy_exits[stage] = {**result.to_dict(), "proxy": label}
        self.proxy_state.record_result(stage, prepared, result.ok, result.error, result.country_code)
        if not result.ok:
            raise RuntimeError(
                f"proxy_preflight_failed:{stage}:expected={expected_country or 'ANY'}:"
                f"actual={result.country_code or 'unknown'}:{result.error}"
            )
        self._log("proxy", f"{stage} exit={result.ip}/{result.country_code} {result.country}")
        return prepared

    def _record_stage_result(self, stage: str, proxy: str, success: bool, reason: str = "") -> None:
        country = str((self.proxy_exits.get(stage) or {}).get("country_code") or self.stage_proxy_countries.get(stage) or "")
        self.proxy_state.record_result(stage, proxy, success, reason, country)

    # ─── Stage 1: Checkout (JP/TH 代理) ───────────────────────────────────

    def _create_checkout(self) -> dict:
        base_proxy = self.checkout_proxy
        self._log("checkout", f"Stage 1: proxy={redact_proxy_url(base_proxy)} billing={self.checkout_country}/{self.checkout_currency} target={self.target_country}")
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": self.checkout_country, "currency": self.checkout_currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": "custom",
        }
        for attempt in range(1, self.max_stage_retries + 1):
            attempt_proxy = base_proxy
            try:
                attempt_proxy = self._prepare_stage_proxy("checkout", base_proxy, attempt)
                r = _checkout_post(
                    "https://chatgpt.com/backend-api/payments/checkout",
                    body, self.access_token, self.cookie_header, attempt_proxy, CHATGPT_TIMEOUT,
                )
                if r.status_code == 401:
                    raise Exception("access_token 无效或已过期 (401)")
                if r.status_code == 429:
                    raise Exception(f"请求频率限制 (429), retry-after={r.headers.get('Retry-After', '')}")
                r.raise_for_status()
                data = r.json()
                cs_id = data.get("checkout_session_id") or data.get("id", "")
                if not cs_id or not cs_id.startswith("cs_"):
                    raise Exception(f"checkout 响应异常: {json.dumps(data, ensure_ascii=False)[:200]}")
                pk = data.get("publishable_key") or ""
                if pk.startswith("pk_"):
                    self.stripe_pk = pk
                self.checkout_proxy = attempt_proxy
                self._record_stage_result("checkout", attempt_proxy, True)
                self._log("checkout", f"checkout 成功: cs_id={cs_id}")
                return {
                    "cs_id": cs_id,
                    "processor_entity": data.get("processor_entity") or ("openai_llc" if self.checkout_country == "US" else "openai_ie"),
                    "stripe_publishable_key": self.stripe_pk,
                    "billing_country": self.checkout_country,
                    "currency": self.checkout_currency,
                }
            except Exception as e:
                self._record_stage_result("checkout", attempt_proxy, False, str(e))
                self._log("checkout", f"checkout 第 {attempt} 次失败: {e}")
                retryable = is_retryable_network_error(e) or "proxy_preflight_failed" in str(e)
                if attempt < self.max_stage_retries and retryable:
                    self._log("checkout", f"retry {attempt + 1}/{self.max_stage_retries} with a new proxy session")
                else:
                    raise

    # ─── Stage 1.5: Promotion update (促销可用区代理) ──────────────────────

    def _checkout_page_url(self, cs_id: str, processor_entity: str) -> str:
        entity = processor_entity or ("openai_llc" if self.checkout_country == "US" else "openai_ie")
        return f"https://chatgpt.com/checkout/{entity}/{cs_id}"

    def _checkout_update_promotion(self, cs_id: str, processor_entity: str) -> bool:
        """Apply the 0-due promo to an existing checkout via /checkout/update.

        Routed through ``promotion_proxy`` (a promo-eligible region egress).
        Returns True on success. Non-fatal on failure: logs and returns False so
        the downstream ``require_zero`` gate in _stripe_init decides the outcome.
        """
        self._log("promotion", f"Stage 1.5: proxy={redact_proxy_url(self.promotion_proxy)} promo={self.promo_campaign_id}")
        body = {
            "checkout_session_id": cs_id,
            "processor_entity": processor_entity,
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": self.promo_campaign_id,
                "is_coupon_from_query_param": False,
            },
        }
        extra_headers = {
            "Referer": self._checkout_page_url(cs_id, processor_entity),
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        }
        try:
            self.promotion_proxy = self._prepare_stage_proxy("promotion", self.promotion_proxy)
            r = _checkout_post(
                "https://chatgpt.com/backend-api/payments/checkout/update",
                body, self.access_token, self.cookie_header, self.promotion_proxy, CHATGPT_TIMEOUT,
                extra_headers=extra_headers,
            )
        except Exception as e:
            self._record_stage_result("promotion", self.promotion_proxy, False, str(e))
            self._log("promotion", f"checkout/update 请求异常 (忽略, 由 require_zero 兜底): {e}")
            return False
        if r.status_code >= 400:
            self._record_stage_result("promotion", self.promotion_proxy, False, f"HTTP {r.status_code}")
            self._log("promotion", f"checkout/update 失败 {r.status_code}: {r.text[:200]} (忽略, 由 require_zero 兜底)")
            return False
        try:
            payload = r.json() or {}
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("success") is False:
            self._record_stage_result("promotion", self.promotion_proxy, False, "promotion_rejected")
            self._log("promotion", f"checkout/update 被拒: {json.dumps(payload, ensure_ascii=False)[:200]}")
            return False
        self._record_stage_result("promotion", self.promotion_proxy, True)
        self._log("promotion", "checkout/update 成功: 促销已应用到当前 checkout")
        return True

    def _checkout_update_taxes(self, cs_id: str, processor_entity: str) -> bool:
        """Optionally sync billing/tax region via /checkout/taxes (provider 代理)."""
        try:
            taxes_proxy = self._prepare_stage_proxy("provider", self.provider_proxy)
        except Exception as e:
            self._record_stage_result("provider", self.provider_proxy, False, str(e))
            self._log("promotion", f"checkout/taxes provider proxy failed (ignored): {e}")
            return False
        billing = billing_for_country(self.target_country)
        body = {
            "checkout_session_id": cs_id,
            "checkout_email": billing["email"],
            "billing_country": self.target_country,
            "billing_name": f"{billing['name'][0]} {billing['name'][1]}",
            "currency": self.currency,
            "tax_id": None,
            "processor_entity": processor_entity,
            "billing_address": {
                "line1": billing["street"],
                "city": billing["city"],
                "country": self.target_country,
                "postal_code": billing["postal"],
            },
        }
        extra_headers = {
            "Referer": self._checkout_page_url(cs_id, processor_entity),
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        }
        try:
            r = _checkout_post(
                "https://chatgpt.com/backend-api/payments/checkout/taxes",
                body, self.access_token, self.cookie_header, taxes_proxy, CHATGPT_TIMEOUT,
                extra_headers=extra_headers,
            )
        except Exception as e:
            self._log("promotion", f"checkout/taxes 请求异常 (忽略): {e}")
            return False
        if r.status_code >= 400:
            self._log("promotion", f"checkout/taxes 失败 {r.status_code}: {r.text[:200]} (忽略)")
            return False
        self._log("promotion", "checkout/taxes 同步成功")
        return True

    # ─── Stage 2: Stripe init + create PM + confirm (目标国代理) ───────────

    def _stripe_init(self, cs_id: str) -> dict:
        self._active_stage = "stripe_init"
        self._log("stripe_init", f"Stage 2: proxy={redact_proxy_url(self.stripe_init_proxy)} Stripe init")
        stripe = self._set_stripe_proxy(self.stripe_init_proxy)
        body = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": self.stripe_js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        r = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/init", data=body, timeout=DEFAULT_TIMEOUT)
        if r.status_code >= 400:
            raise Exception(f"stripe init 失败: {r.status_code} {r.text[:300]}")
        init = r.json()
        amount_info = stripe_amount_details(init)
        amount = amount_info.get("amount")
        self._log("stripe_init", f"amount={amount} currency={amount_info.get('currency')} source={amount_info.get('source')}")
        self.proxy_state.record_zero_result(self.checkout_proxy, self.checkout_country, amount)
        if self.require_zero and amount != 0:
            raise CheckoutNotZeroDueError(amount, amount_info.get("currency", ""))
        # 检查 PayPal 是否可用
        pm_types = init.get("payment_method_types") or []
        if pm_types and "paypal" not in [str(t).lower() for t in pm_types]:
            raise Exception(f"当前 checkout 不支持 PayPal, 可用: {pm_types}")
        return init

    def _create_payment_method(self, cs_id: str) -> str:
        self._active_stage = "payment_method"
        self._log("payment_method", f"创建 PayPal payment_method")
        stripe = self._set_stripe_proxy(self.payment_method_proxy)
        billing = billing_for_country(self.target_country)
        body = {
            "type": "paypal",
            "billing_details[name]": f"{billing['name'][0]} {billing['name'][1]}",
            "billing_details[email]": billing["email"],
            "billing_details[address][country]": billing["country"],
            "billing_details[address][line1]": billing["street"],
            "billing_details[address][city]": billing["city"],
            "billing_details[address][state]": billing["state"],
            "billing_details[address][postal_code]": billing["postal"],
            "payment_user_agent": f"stripe.js/{self.runtime_version}; stripe-js-v3/{self.runtime_version}; payment-element; deferred-intent",
            "referrer": "https://chatgpt.com",
            "time_on_page": str(random.randint(25000, 55000)),
            "client_attribution_metadata[client_session_id]": self.stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        r = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            raise Exception(f"payment_method 创建失败: {r.status_code} {r.text[:200]}")
        pm_id = r.json().get("id", "")
        if not pm_id.startswith("pm_"):
            raise Exception(f"payment_method 响应异常: {r.text[:200]}")
        self._log("payment_method", f"pm_id={pm_id}")
        return pm_id

    def _stripe_confirm(self, cs_id: str, pm_id: str, init: dict) -> dict:
        self._active_stage = "confirm"
        self._log("confirm", "Stripe confirm")
        stripe = self._set_stripe_proxy(self.confirm_proxy)
        processor_entity = "openai_llc" if self.checkout_country == "US" else "openai_ie"
        chatgpt_return = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor_entity}&plan_type=plus"
        hosted_url = str(init.get("stripe_hosted_url") or "")
        if hosted_url:
            hosted_url = hosted_url.replace("checkout.stripe.com", "pay.openai.com")
        else:
            hosted_url = f"https://pay.openai.com/c/pay/{cs_id}?returned_from_redirect=true&ui_mode=custom&return_url={quote(chatgpt_return, safe='')}"
        return_url = hosted_url

        amount_info = stripe_amount_details(init)
        expected = str(amount_info.get("amount") if amount_info.get("amount") is not None else 0)

        body = {
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": str(init.get("init_checksum") or ""),
            "version": self.runtime_version,
            "expected_amount": expected,
            "expected_payment_method_type": "paypal",
            "return_url": return_url,
            "elements_session_client[session_id]": self.elements_session_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[stripe_js_id]": self.stripe_js_id,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "client_attribution_metadata[client_session_id]": self.stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": self.elements_session_config_id,
            "client_attribution_metadata[elements_session_id]": self.elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": self.elements_session_config_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "consent[terms_of_service]": "accepted",
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        r = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
        if r.status_code >= 400:
            diagnostics = stripe_confirm_error_diagnostics(r, cs_id, pm_id, init)
            self._log("confirm", diagnostics)
            raise Exception(diagnostics)
        return r.json()

    # ─── Stage 3: Approve (目标国代理) + 轮询 redirect ─────────────────────

    def _chatgpt_approve(self, cs_id: str, processor_entity: str):
        self._active_stage = "approve"
        self._log("approve", f"Stage 3: proxy={redact_proxy_url(self.approve_proxy)} ChatGPT approve")
        cs = _new_session(self.approve_proxy)
        cs.headers.update({
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
        })
        # sentinel ping
        try:
            cs.post("https://chatgpt.com/backend-api/sentinel/ping", json={}, timeout=CHATGPT_TIMEOUT)
        except Exception:
            pass
        r = cs.post(
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
            timeout=CHATGPT_TIMEOUT,
        )
        if r.status_code >= 400:
            raise Exception(f"approve 失败: {r.status_code} {r.text[:300]}")
        result = (r.json() or {}).get("result")
        if result != "approved":
            raise Exception(f"approve 结果异常: {result}")
        self._log("approve", "ChatGPT approve 成功")

    def _poll_payment_page(self, cs_id: str, timeout_seconds: float = 45) -> str:
        """轮询 Stripe payment page 获取 redirect URL。"""
        self._log("poll", f"轮询 payment page (超时 {timeout_seconds}s)")
        stripe = getattr(self, "_stripe_session", None) or _new_session(self.provider_proxy)
        deadline = time.time() + timeout_seconds
        params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": self.elements_session_id,
            "elements_session_client[stripe_js_id]": self.stripe_js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": self.stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            r = stripe.get(f"https://api.stripe.com/v1/payment_pages/{cs_id}", params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                payload = r.json() or {}
                url = extract_redirect_url(payload)
                if url:
                    self._log("poll", f"第 {poll_count} 次轮询发现 redirect URL")
                    return url
                # 检查 submission 状态
                submission = payload.get("submission_attempt") or {}
                if isinstance(submission, dict):
                    state = submission.get("state")
                    if state == "requires_approval":
                        raise Exception("requires_approval")
                    if state == "failed":
                        raise Exception(f"submission failed: {submission}")
            if poll_count % 5 == 0:
                self._log("poll", f"第 {poll_count} 次轮询...")
            time.sleep(1)
        raise Exception(f"轮询超时 ({timeout_seconds}s)")

    def _run_provider_stages(self, cs_id: str) -> tuple[dict, str, dict]:
        base_proxies = {
            "provider": self.provider_proxy,
            "stripe_init": self.stripe_init_proxy,
            "payment_method": self.payment_method_proxy,
            "confirm": self.confirm_proxy,
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_stage_retries + 1):
            try:
                self._active_stage = "provider"
                self.provider_proxy = self._prepare_stage_proxy("provider", base_proxies["provider"], attempt)
                self._active_stage = "stripe_init"
                self.stripe_init_proxy = self._prepare_stage_proxy("stripe_init", base_proxies["stripe_init"], attempt)
                self._active_stage = "payment_method"
                self.payment_method_proxy = self._prepare_stage_proxy("payment_method", base_proxies["payment_method"], attempt)
                self._active_stage = "confirm"
                self.confirm_proxy = self._prepare_stage_proxy("confirm", base_proxies["confirm"], attempt)
                self._reset_stripe_context()
                self._stripe_session = _new_session(self.stripe_init_proxy)
                init = self._stripe_init(cs_id)
                stripe_hosted_url = str(init.get("stripe_hosted_url") or "")
                self._log("stripe_init", f"stripe_hosted_url={stripe_hosted_url[:80]}...")
                pm_id = self._create_payment_method(cs_id)
                confirm_data = self._stripe_confirm(cs_id, pm_id, init)
                for stage, proxy in (
                    ("provider", self.provider_proxy),
                    ("stripe_init", self.stripe_init_proxy),
                    ("payment_method", self.payment_method_proxy),
                    ("confirm", self.confirm_proxy),
                ):
                    self._record_stage_result(stage, proxy, True)
                return init, stripe_hosted_url, confirm_data
            except Exception as exc:
                last_error = exc
                stage = self._active_stage or "provider"
                failed_proxy = {
                    "provider": self.provider_proxy,
                    "stripe_init": self.stripe_init_proxy,
                    "payment_method": self.payment_method_proxy,
                    "confirm": self.confirm_proxy,
                }.get(stage, self.provider_proxy)
                self._record_stage_result(stage, failed_proxy, False, str(exc))
                retryable = is_retryable_network_error(exc) or "proxy_preflight_failed" in str(exc)
                self._log(stage, f"stage attempt {attempt}/{self.max_stage_retries} failed: {exc}")
                if not retryable or attempt >= self.max_stage_retries:
                    raise
                self._log(stage, "retrying provider stages with new proxy sessions")
        assert last_error is not None
        raise last_error

    # ─── 主流程 ────────────────────────────────────────────────────────────

    def extract(self) -> dict:
        """执行完整三段式提链流程。"""
        # Stage 1: Checkout (JP/TH 代理)
        checkout = self._create_checkout()
        cs_id = checkout["cs_id"]
        processor_entity = checkout["processor_entity"]

        # Stage 1.5: 促销更新 (可选). 对已创建的 checkout 从促销可用区打 0元促销,
        # 使 "PayPal 区 checkout + 0元" 能共存于同一会话.
        if self.enable_promotion:
            self._checkout_update_promotion(cs_id, processor_entity)
            if self.promotion_taxes:
                self._checkout_update_taxes(cs_id, processor_entity)

        # Stage 2: Stripe init + create PM + confirm. Each stage can use its own
        # egress while the Stripe session keeps its cookies and identifiers.
        init, stripe_hosted_url, confirm_data = self._run_provider_stages(cs_id)

        # 尝试从 confirm 提取 redirect URL (仅真正的 PayPal/Stripe 授权链接)
        redirect_url = extract_redirect_url(confirm_data)

        # 如果 confirm 没有返回 redirect，走 approve 流程
        if not redirect_url:
            self._log("approve", "confirm 未返回 redirect，走 ChatGPT approve 流程")
            base_approve_proxy = self.approve_proxy
            for attempt in range(1, self.max_stage_retries + 1):
                try:
                    self.approve_proxy = self._prepare_stage_proxy("approve", base_approve_proxy, attempt)
                    self._chatgpt_approve(cs_id, processor_entity)
                    redirect_url = self._poll_payment_page(cs_id, timeout_seconds=45)
                    self._record_stage_result("approve", self.approve_proxy, True)
                    break
                except Exception as e:
                    self._record_stage_result("approve", self.approve_proxy, False, str(e))
                    self._log("approve", f"approve 第 {attempt} 次失败: {e}")
                    if attempt >= self.max_stage_retries:
                        # 降级: 返回 stripe_hosted_url
                        if stripe_hosted_url:
                            self.proxy_state.record_pair_result(
                                self.checkout_proxy,
                                self.provider_proxy,
                                self.approve_proxy,
                                False,
                                "approve_fallback_to_hosted",
                            )
                            self._log("approve", "降级返回 stripe_hosted_url")
                            return {
                                "ok": True,
                                "link_type": "stripe_hosted",
                                "url": stripe_hosted_url,
                                "ba_token": "",
                                "cs_id": cs_id,
                                "amount": stripe_amount_details(init).get("amount"),
                                "currency": self.currency,
                                "target_country": self.target_country,
                                "checkout_country": self.checkout_country,
                            }
                        raise

        # Stage 3: 跟随 redirect 提取 PayPal BA approve URL
        # 复用 Stripe session 保持 cookies
        if not is_paypal_ba_approve_url(redirect_url):
            self._log("redirect", f"跟随 redirect 链提取 BA URL: {redirect_url[:80]}...")
            redirect_url = resolve_external_redirect(self._stripe_session, redirect_url)

        if not is_paypal_ba_approve_url(redirect_url):
            raise Exception(f"未提取到 PayPal BA approve URL: {redirect_url[:200]}")

        ba_token = extract_ba_token(redirect_url)
        self._log("done", f"✅ 提取成功! ba_token={ba_token[:30]}...")
        self.proxy_state.record_pair_result(
            self.checkout_proxy,
            self.provider_proxy,
            self.approve_proxy,
            True,
        )

        return {
            "ok": True,
            "link_type": "paypal_ba_approve",
            "url": redirect_url,
            "ba_token": ba_token,
            "cs_id": cs_id,
            "amount": stripe_amount_details(init).get("amount"),
            "currency": self.currency,
            "target_country": self.target_country,
            "checkout_country": self.checkout_country,
            "checkout_proxy": redact_proxy_url(self.checkout_proxy),
            "promotion_proxy": redact_proxy_url(self.promotion_proxy),
            "provider_proxy": redact_proxy_url(self.provider_proxy),
            "stripe_init_proxy": redact_proxy_url(self.stripe_init_proxy),
            "payment_method_proxy": redact_proxy_url(self.payment_method_proxy),
            "confirm_proxy": redact_proxy_url(self.confirm_proxy),
            "approve_proxy": redact_proxy_url(self.approve_proxy),
            "proxy_exits": self.proxy_exits,
        }


# ─── 批量矩阵 ──────────────────────────────────────────────────────────────────


def run_batch(
    access_token: str,
    proxy_template: str,
    target_countries: list[str] | None = None,
    checkout_countries: list[str] | None = None,
    require_zero: bool = True,
    emit: Any = None,
    promotion_country: str = "",
    promotion_countries: list[str] | None = None,
) -> dict:
    """批量矩阵提链: 成功即停。

    两种矩阵模式:

    - 默认 (target × checkout): 沿用旧行为, 遍历 target 国 × checkout 出口国。
      ``promotion_country`` 非空时对每个组合启用促销更新 (/checkout/update)。
    - 促销矩阵 (paypal_region × promotion_region): 当 ``promotion_countries``
      非空时启用。对齐参考实现的 zero-amount matrix: checkout/provider/approve
      都走 PayPal 支持区 (target), promotion 走促销可用区, 目标是同一 checkout
      同时拿到 0元 + PayPal BA 直链。返回结果附带 ``matrix`` 明细。
    """
    log = emit or (lambda step, msg, **kw: print(f"[{step}] {msg}", file=sys.stderr))
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    state = _paypal_proxy_state(paypal_cfg)
    preflight = bool(paypal_cfg.get("preflight_proxy_check", False))
    rotate_sessions = bool(paypal_cfg.get("rotate_proxy_sessions", False))
    probe_timeout = float(paypal_cfg.get("proxy_probe_timeout_seconds", 12) or 12)
    max_stage_retries = int(paypal_cfg.get("max_stage_retries", RETRY_ATTEMPTS) or RETRY_ATTEMPTS)

    # ── 促销矩阵模式: paypal_region × promotion_region ──────────────────────
    if promotion_countries:
        paypal_regions = target_countries or list(DEFAULT_TARGET_COUNTRIES)
        promo_regions = [c for c in promotion_countries if c]
        combos = [(pp, promo) for pp in paypal_regions for promo in promo_regions]
        combos.sort(
            key=lambda item: state.pair_score(
                proxy_for_country_template(proxy_template, item[0]),
                proxy_for_country_template(proxy_template, item[0]),
            ),
            reverse=True,
        )
        log("batch", f"促销矩阵: {len(combos)} 个组合 (PayPal 区 × promotion 区), 0元+BA 成功即停")
        matrix: list[dict[str, Any]] = []
        for index, (pp_region, promo_region) in enumerate(combos, 1):
            label = f"{pp_region}<-promo:{promo_region}"
            log("batch", f"任务 {index}/{len(combos)}: paypal={pp_region} promotion={promo_region}")
            region_proxy = proxy_for_country_template(proxy_template, pp_region)
            promotion_proxy = proxy_for_country_template(proxy_template, promo_region)
            row: dict[str, Any] = {
                "paypal_region": pp_region, "promotion_region": promo_region,
                "amount": None, "link_type": "", "status": "failed", "error": "",
            }
            try:
                extractor = PPLinkExtractor(
                    access_token=access_token,
                    checkout_proxy=region_proxy,
                    provider_proxy=region_proxy,
                    stripe_init_proxy=region_proxy,
                    payment_method_proxy=region_proxy,
                    confirm_proxy=region_proxy,
                    approve_proxy=region_proxy,
                    promotion_proxy=promotion_proxy,
                    target_country=pp_region,
                    checkout_country=pp_region,
                    require_zero=require_zero,
                    emit=log,
                    preflight_proxy_check=preflight,
                    rotate_proxy_sessions=rotate_sessions,
                    proxy_probe_timeout=probe_timeout,
                    max_stage_retries=max_stage_retries,
                    proxy_state=state,
                    stage_proxy_countries={
                        "checkout": pp_region,
                        "promotion": promo_region,
                        "provider": pp_region,
                        "stripe_init": pp_region,
                        "payment_method": pp_region,
                        "confirm": pp_region,
                        "approve": pp_region,
                    },
                )
                result = extractor.extract()
                row["amount"] = result.get("amount")
                row["link_type"] = result.get("link_type", "")
                is_zero = result.get("amount") == 0
                is_ba = "paypal_ba" in str(result.get("link_type") or "")
                if result.get("ok") and is_zero and is_ba:
                    row["status"] = "success"
                    matrix.append(row)
                    log("batch", f"任务 {label} 成功! 0元+BA url={str(result.get('url'))[:80]}...")
                    return {"ok": True, "tasks_attempted": index, "tasks_total": len(combos),
                            "winning_combo": label, "matrix": matrix, **result}
                row["status"] = "partial" if result.get("ok") else "failed"
                log("batch", f"任务 {label}: amount={row['amount']} link_type={row['link_type']} (未同时满足 0元+BA)")
            except Exception as e:
                row["error"] = str(e)
                log("batch", f"任务 {label} 失败: {e}")
            matrix.append(row)
        return {"ok": False, "error": f"所有 {len(combos)} 个促销矩阵组合均未同时满足 0元+BA",
                "tasks_attempted": len(combos), "matrix": matrix}

    # ── 默认模式: target × checkout ─────────────────────────────────────────
    targets = target_countries or list(DEFAULT_TARGET_COUNTRIES)
    checkouts = checkout_countries or list(DEFAULT_CHECKOUT_COUNTRIES)

    tasks = [(t, c) for t in targets for c in checkouts]
    tasks.sort(
        key=lambda item: (
            state.pair_score(
                proxy_for_country_template(proxy_template, item[1]),
                proxy_for_country_template(proxy_template, item[0]),
            ),
            1 if state.zero_status(proxy_for_country_template(proxy_template, item[1]), item[1])[0] == "ok" else 0,
        ),
        reverse=True,
    )
    log("batch", f"批量任务: {len(tasks)} 个组合, 提取到第一个 BA 链后停止")

    for index, (target, checkout) in enumerate(tasks, 1):
        task_label = f"{target}-{checkout}"
        log("batch", f"任务 {index}/{len(tasks)}: target={target} checkout_proxy={checkout}")

        checkout_proxy = proxy_for_country_template(proxy_template, checkout)
        target_proxy = proxy_for_country_template(proxy_template, target)
        promotion_proxy = (
            proxy_for_country_template(proxy_template, promotion_country)
            if promotion_country else ""
        )

        try:
            extractor = PPLinkExtractor(
                access_token=access_token,
                checkout_proxy=checkout_proxy,
                provider_proxy=target_proxy,
                stripe_init_proxy=target_proxy,
                payment_method_proxy=target_proxy,
                confirm_proxy=target_proxy,
                approve_proxy=target_proxy,
                promotion_proxy=promotion_proxy,
                target_country=target,
                checkout_country=checkout,
                require_zero=require_zero,
                emit=log,
                preflight_proxy_check=preflight,
                rotate_proxy_sessions=rotate_sessions,
                proxy_probe_timeout=probe_timeout,
                max_stage_retries=max_stage_retries,
                proxy_state=state,
                stage_proxy_countries={
                    "checkout": checkout,
                    "promotion": promotion_country,
                    "provider": target,
                    "stripe_init": target,
                    "payment_method": target,
                    "confirm": target,
                    "approve": target,
                },
            )
            result = extractor.extract()
            log("batch", f"任务 {task_label} 成功! url={result['url'][:80]}...")
            return {"ok": True, "tasks_attempted": index, "tasks_total": len(tasks), "winning_combo": task_label, **result}
        except Exception as e:
            log("batch", f"任务 {task_label} 失败: {e}")
            continue

    return {"ok": False, "error": f"所有 {len(tasks)} 个组合均失败", "tasks_attempted": len(tasks)}


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PP 直链生成器 -- 分段代理池版")
    parser.add_argument("token", nargs="?", help="OpenAI Access Token")
    parser.add_argument("--token", dest="token_flag", help="Access Token (alternative)")
    parser.add_argument("--proxy", default="", help="单代理模式 (所有阶段)")
    parser.add_argument("--checkout-proxy", default="", help="Checkout 阶段代理 (JP)")
    parser.add_argument("--provider-proxy", default="", help="Provider/Stripe 阶段代理 (目标国)")
    parser.add_argument("--approve-proxy", default="", help="Approve 阶段代理 (目标国)")
    parser.add_argument("--promotion-proxy", default="", help="促销更新阶段代理 (促销可用区出口, 如 VN/TH; 用于 /checkout/update 打 0元)")
    parser.add_argument("--promotion-country", default="", help="批量模式促销更新出口国家 (如 VN/TH)")
    parser.add_argument("--promotion-countries", default="", help="促销矩阵模式: promotion 出口国列表 (逗号分隔, 如 JP,TH,VN)。设置后 run_batch 走 PayPal区×promotion区 组合搜索")
    parser.add_argument("--proxy-template", default="", help="代理模板 (自动替换国家码)")
    parser.add_argument("--target", default="DE", help="目标国家 (单次模式)")
    parser.add_argument("--checkout-country", default="", help="Checkout 阶段账单国家 (默认同 target, 如 JP/TR)")
    parser.add_argument("--batch", action="store_true", help="批量矩阵模式")
    parser.add_argument("--target-countries", default="", help="批量模式目标国家 (逗号分隔)")
    parser.add_argument("--checkout-countries", default="JP,TH", help="批量模式 checkout 出口 (逗号分隔)")
    parser.add_argument("--no-require-zero", action="store_true", help="不要求 0 元金额")
    parser.add_argument("--json", action="store_true", help="JSON 输出")

    args = parser.parse_args()
    token = args.token or args.token_flag
    if not token:
        parser.error("请提供 Access Token")

    def emit(step, msg, **kw):
        print(f"[{step}] {msg}", file=sys.stderr)

    require_zero = not args.no_require_zero

    if args.batch or args.proxy_template:
        # 批量模式
        template = args.proxy_template or args.proxy
        if not template:
            parser.error("批量模式需要 --proxy-template")
        targets = [c.strip().upper() for c in args.target_countries.split(",") if c.strip()] if args.target_countries else list(DEFAULT_TARGET_COUNTRIES)
        checkouts = [c.strip().upper() for c in args.checkout_countries.split(",") if c.strip()]
        promotion_countries = [c.strip().upper() for c in args.promotion_countries.split(",") if c.strip()]
        result = run_batch(token, template, targets, checkouts, require_zero=require_zero, emit=emit,
                           promotion_country=(args.promotion_country or "").strip().upper(),
                           promotion_countries=promotion_countries or None)
    else:
        # 单次模式
        checkout_proxy = args.checkout_proxy or args.proxy
        provider_proxy = args.provider_proxy or args.proxy
        approve_proxy = args.approve_proxy or args.proxy
        promotion_proxy = args.promotion_proxy or ""
        if not checkout_proxy and not provider_proxy:
            parser.error("请提供代理 (--proxy 或 --checkout-proxy + --provider-proxy)")
        extractor = PPLinkExtractor(
            access_token=token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            approve_proxy=approve_proxy,
            promotion_proxy=promotion_proxy,
            target_country=args.target,
            checkout_country=args.checkout_country,
            require_zero=require_zero,
            emit=emit,
        )
        result = extractor.extract()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("ok"):
            print(f"\n✅ PP 直链提取成功!")
            print(f"   URL: {result['url']}")
            if result.get("ba_token"):
                print(f"   BA Token: {result['ba_token']}")
            print(f"   cs_id: {result['cs_id']}")
            print(f"   金额: {result.get('amount')} {result.get('currency')}")
            print(f"   目标国: {result.get('target_country')}")
            print(f"   链接类型: {result.get('link_type')}")
        else:
            print(f"\n❌ 提取失败: {result.get('error')}")
            sys.exit(1)


# ─── 兼容函数 (供 paypal_links.py 和 cli.py 调用) ──────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def _load_json(path: str) -> dict:
    """Load a JSON object from disk, accepting UTF-8 files with or without BOM."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_token(raw: str) -> str | None:
    """解析 access token，支持 JWT 格式。"""
    token = str(raw or "").strip()
    if not token:
        return None
    # JWT 格式: header.payload.signature
    parts = token.split(".")
    if len(parts) == 3 and all(parts):
        return token
    return None


def _fetch_proxy_api_url(api_url: str) -> str:
    """Fetch a short-lived proxy from a plain-text proxy API such as Cliproxy white/api."""
    api_url = str(api_url or "").strip()
    if not api_url:
        return ""
    try:
        response = requests.get(
            api_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 CodexATStageProxyAPI/1.0"},
        )
        response.raise_for_status()
        for line in response.text.splitlines():
            line = line.strip()
            if line:
                return normalize_proxy_url(line)
    except Exception as exc:
        print(f"[proxy_api] fetch failed: {exc}", file=sys.stderr)
    return ""


def _stage_proxy_value(stage_proxies: dict, api_urls: dict, key: str, fallback: str = "") -> str:
    api_url = str((api_urls or {}).get(key) or "").strip()
    if api_url:
        fetched = _fetch_proxy_api_url(api_url)
        if fetched:
            return fetched
    return str((stage_proxies or {}).get(key) or fallback or "").strip()


def _proxy_health_cfg(paypal_cfg: dict) -> dict:
    value = paypal_cfg.get("proxy_health") if isinstance(paypal_cfg.get("proxy_health"), dict) else {}
    return value or {}


def _paypal_proxy_state(paypal_cfg: dict) -> PayPalProxyState:
    health = _proxy_health_cfg(paypal_cfg)
    state_file = str(health.get("state_file") or "runtime/paypal_proxy_state.json").strip()
    state_path = Path(state_file).expanduser()
    if not state_path.is_absolute():
        state_path = Path(PROJECT_ROOT) / state_path
    key = (
        str(state_path),
        bool(health.get("enabled", True)),
        int(health.get("fail_skip_after", 2) or 2),
        int(health.get("fail_cooldown_seconds", 180) or 0),
        int(health.get("zero_cache_ttl_seconds", 1800) or 0),
    )
    state = _PAYPAL_PROXY_STATE_CACHE.get(key)
    if state is None:
        state = PayPalProxyState(
            state_path,
            enabled=key[1],
            fail_skip_after=key[2],
            fail_cooldown_seconds=key[3],
            zero_cache_ttl_seconds=key[4],
        )
        _PAYPAL_PROXY_STATE_CACHE[key] = state
    return state


def _proxy_pool_values(paypal_cfg: dict, key: str) -> list[str]:
    pools = paypal_cfg.get("stage_proxy_pools") if isinstance(paypal_cfg.get("stage_proxy_pools"), dict) else {}
    raw = pools.get(key)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [normalize_proxy_url(item) for item in raw if str(item or "").strip()]


def _rank_stage_proxy(
    paypal_cfg: dict,
    state: PayPalProxyState,
    stage: str,
    configured_value: str,
    *,
    country: str = "",
    checkout_proxy: str = "",
) -> str:
    candidates = _proxy_pool_values(paypal_cfg, stage)
    if configured_value:
        candidates.append(normalize_proxy_url(configured_value))
    ranked = state.rank(stage, candidates, country=country, checkout_proxy=checkout_proxy)
    return ranked[0] if ranked else (normalize_proxy_url(configured_value) if configured_value else "")


def _proxies_from_config(cfg: dict, checkout_country: str = "", target_country: str = "") -> dict:
    """Resolve payment stage proxies from static config or proxy API URLs.

    Supports static ``paypal.stage_proxies`` and dynamic plain-text proxy APIs in
    ``paypal.stage_proxy_api_urls``.  API values are resolved at runtime so
    short-lived Cliproxy IP:PORT leases do not get frozen in config.json.
    """
    paypal_cfg = cfg.get("paypal") or {}
    stage_proxies = paypal_cfg.get("stage_proxies") or {}
    api_urls = paypal_cfg.get("stage_proxy_api_urls") or {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""
    state = _paypal_proxy_state(paypal_cfg)

    checkout_value = _stage_proxy_value(stage_proxies, api_urls, "checkout", proxy_default)
    checkout = _rank_stage_proxy(
        paypal_cfg,
        state,
        "checkout",
        checkout_value,
        country=checkout_country,
    )
    provider_value = (
        _stage_proxy_value(stage_proxies, api_urls, "provider")
        or _stage_proxy_value(stage_proxies, api_urls, "stripe_init")
        or proxy_default
    )
    provider = _rank_stage_proxy(
        paypal_cfg,
        state,
        "provider",
        provider_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    stripe_init_value = _stage_proxy_value(stage_proxies, api_urls, "stripe_init") or provider
    stripe_init = _rank_stage_proxy(
        paypal_cfg, state, "stripe_init", stripe_init_value, country=target_country, checkout_proxy=checkout,
    )
    payment_method_value = _stage_proxy_value(stage_proxies, api_urls, "payment_method") or provider
    payment_method = _rank_stage_proxy(
        paypal_cfg, state, "payment_method", payment_method_value, country=target_country, checkout_proxy=checkout,
    )
    confirm_value = _stage_proxy_value(stage_proxies, api_urls, "confirm") or provider
    confirm = _rank_stage_proxy(
        paypal_cfg, state, "confirm", confirm_value, country=target_country, checkout_proxy=checkout,
    )
    approve_value = (
        _stage_proxy_value(stage_proxies, api_urls, "approve")
        or confirm
        or provider
        or proxy_default
    )
    approve = _rank_stage_proxy(
        paypal_cfg,
        state,
        "approve",
        approve_value,
        country=target_country,
        checkout_proxy=checkout,
    )
    # Promotion stage is OPT-IN: only resolved from explicit config, no fallback
    # to provider/default, so leaving it unset keeps the original behaviour.
    promotion_value = (
        _stage_proxy_value(stage_proxies, api_urls, "promotion")
        or _stage_proxy_value(stage_proxies, api_urls, "promotion_update")
    )
    promotion = _rank_stage_proxy(
        paypal_cfg,
        state,
        "promotion",
        promotion_value,
    )
    return {
        "checkout": checkout,
        "promotion": promotion,
        "provider": provider,
        "stripe_init": stripe_init,
        "payment_method": payment_method,
        "confirm": confirm,
        "approve": approve,
    }


def _stage_proxy_is_configured(paypal_cfg: dict, *keys: str) -> bool:
    stage_proxies = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    api_urls = paypal_cfg.get("stage_proxy_api_urls") if isinstance(paypal_cfg.get("stage_proxy_api_urls"), dict) else {}
    return any(str(stage_proxies.get(key) or api_urls.get(key) or "").strip() for key in keys)


def _resolve_stage_proxy(
    explicit_stage_proxy: Any,
    single_proxy: Any,
    configured_proxy: Any,
    configured: bool,
    single_proxy_overrides: bool,
) -> str:
    explicit_value = str(explicit_stage_proxy or "").strip()
    if explicit_value:
        return explicit_value
    single_value = str(single_proxy or "").strip()
    configured_value = str(configured_proxy or "").strip()
    if single_proxy_overrides and single_value:
        return single_value
    if configured and configured_value:
        return configured_value
    return single_value or configured_value


def generate_pp_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    stripe_init_proxy: str | None = None,
    payment_method_proxy: str | None = None,
    confirm_proxy: str | None = None,
    approve_proxy: str | None = None,
    promotion_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    require_zero: bool | None = None,
    require_ba_token: bool | None = None,
) -> dict[str, Any]:
    """生成 PayPal BA 直链 (兼容旧接口)。

    Args:
        access_token: OpenAI access token (JWT)
        proxy: 单代理 URL (所有阶段)
        auth_context: 认证上下文 (包含 email 等)
        paypal_generation_type: 链接类型 (已废弃，保留兼容)
        checkout_proxy: Stage 1 代理 (checkout)
        provider_proxy: Stage 2 代理 (Stripe)
        approve_proxy: Stage 3 代理 (approve)
        require_zero: 是否要求 0 元金额 (None 则从配置文件读取)

    Returns:
        {"ok": bool, "url": str, "ba_token": str, "cs_id": str, ...}
    """
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = cfg.get("paypal") or {}
    target_country = str(target_country or paypal_cfg.get("target_country") or "GB").upper()
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    checkout_country = str(
        checkout_country
        or paypal_cfg.get("checkout_country")
        or paypal_cfg.get("billing_country")
        or (regions[0] if regions else None)
        or target_country
    ).strip().upper()
    stage_proxies = _proxies_from_config(cfg, checkout_country=checkout_country, target_country=target_country)

    single_proxy_overrides = bool(paypal_cfg.get("explicit_proxy_overrides_stage_proxies", False))
    _checkout = _resolve_stage_proxy(
        checkout_proxy,
        proxy,
        stage_proxies["checkout"],
        _stage_proxy_is_configured(paypal_cfg, "checkout"),
        single_proxy_overrides,
    )
    _provider = _resolve_stage_proxy(
        provider_proxy,
        proxy,
        stage_proxies["provider"],
        _stage_proxy_is_configured(paypal_cfg, "provider", "stripe_init"),
        single_proxy_overrides,
    )
    _stripe_init = _resolve_stage_proxy(
        stripe_init_proxy,
        provider_proxy or proxy,
        stage_proxies.get("stripe_init", _provider),
        _stage_proxy_is_configured(paypal_cfg, "stripe_init"),
        single_proxy_overrides,
    )
    _payment_method = _resolve_stage_proxy(
        payment_method_proxy,
        provider_proxy or proxy,
        stage_proxies.get("payment_method", _provider),
        _stage_proxy_is_configured(paypal_cfg, "payment_method"),
        single_proxy_overrides,
    )
    _confirm = _resolve_stage_proxy(
        confirm_proxy,
        provider_proxy or proxy,
        stage_proxies.get("confirm", _provider),
        _stage_proxy_is_configured(paypal_cfg, "confirm"),
        single_proxy_overrides,
    )
    _approve = _resolve_stage_proxy(
        approve_proxy,
        proxy,
        stage_proxies["approve"],
        _stage_proxy_is_configured(paypal_cfg, "approve", "confirm"),
        single_proxy_overrides,
    )
    # Promotion 阶段 opt-in: 仅显式传入或配置 promotion 代理时启用 (不回退到单代理/默认)
    _promotion = promotion_proxy if promotion_proxy is not None else stage_proxies.get("promotion", "")

    checkout_proxy = str(_checkout or "").strip()
    provider_proxy = str(_provider or "").strip()
    stripe_init_proxy = str(_stripe_init or provider_proxy).strip()
    payment_method_proxy = str(_payment_method or provider_proxy).strip()
    confirm_proxy = str(_confirm or provider_proxy).strip()
    approve_proxy = str(_approve or "").strip()
    promotion_proxy = str(_promotion or "").strip()
    promotion_taxes = bool(paypal_cfg.get("promotion_taxes", False))
    promo_campaign_id = str(paypal_cfg.get("promo_campaign_id") or "plus-1-month-free")

    generation_type = _normalized_generation_type(paypal_cfg, paypal_generation_type)
    if _is_chatgpt_checkout_link_generation_type(generation_type):
        return generate_chatgpt_checkout_link(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            checkout_proxy=checkout_proxy,
            target_country=target_country,
            checkout_country=checkout_country,
        )
    if _is_hosted_generation_type(generation_type):
        return generate_hosted_long_url(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            stripe_init_proxy=stripe_init_proxy,
            target_country=target_country,
            checkout_country=checkout_country,
            require_zero=require_zero,
        )

    if _is_zero_due_generation_type(generation_type):
        require_zero = True
    elif require_zero is None:
        require_zero = bool(paypal_cfg.get("require_zero_due", True))
    if _is_paypal_direct_generation_type(generation_type):
        require_ba_token = True
    elif require_ba_token is None:
        require_ba_token = bool(paypal_cfg.get("require_ba_token", False))

    # 从 auth_context 提取 email 和 cookie_header
    email = ""
    cookie_header = ""
    if isinstance(auth_context, dict):
        email = str(auth_context.get("email") or "")
        cookie_header = str(auth_context.get("cookie_header") or "")

    def emit(step: str, msg: str, **kw: Any) -> None:
        print(f"[{step}] {msg}", file=sys.stderr)

    state = _paypal_proxy_state(paypal_cfg)
    configured_countries = paypal_cfg.get("stage_proxy_countries") if isinstance(paypal_cfg.get("stage_proxy_countries"), dict) else {}
    stage_proxy_countries = {
        "checkout": str(configured_countries.get("checkout") or infer_proxy_country(checkout_proxy) or checkout_country).upper(),
        "promotion": str(configured_countries.get("promotion") or infer_proxy_country(promotion_proxy) or "").upper(),
        "provider": str(configured_countries.get("provider") or infer_proxy_country(provider_proxy) or target_country).upper(),
        "stripe_init": str(configured_countries.get("stripe_init") or infer_proxy_country(stripe_init_proxy) or target_country).upper(),
        "payment_method": str(configured_countries.get("payment_method") or infer_proxy_country(payment_method_proxy) or target_country).upper(),
        "confirm": str(configured_countries.get("confirm") or infer_proxy_country(confirm_proxy) or target_country).upper(),
        "approve": str(configured_countries.get("approve") or infer_proxy_country(approve_proxy) or target_country).upper(),
    }
    extractor = None
    try:
        extractor = PPLinkExtractor(
            access_token=access_token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            stripe_init_proxy=stripe_init_proxy,
            payment_method_proxy=payment_method_proxy,
            confirm_proxy=confirm_proxy,
            approve_proxy=approve_proxy,
            promotion_proxy=promotion_proxy,
            target_country=target_country,
            checkout_country=checkout_country,
            require_zero=require_zero,
            emit=emit,
            cookie_header=cookie_header,
            promotion_taxes=promotion_taxes,
            promo_campaign_id=promo_campaign_id,
            preflight_proxy_check=bool(paypal_cfg.get("preflight_proxy_check", False)),
            rotate_proxy_sessions=bool(paypal_cfg.get("rotate_proxy_sessions", False)),
            proxy_probe_timeout=float(paypal_cfg.get("proxy_probe_timeout_seconds", 12) or 12),
            max_stage_retries=int(paypal_cfg.get("max_stage_retries", paypal_cfg.get("max_checkout_retries", RETRY_ATTEMPTS)) or RETRY_ATTEMPTS),
            proxy_state=state,
            stage_proxy_countries=stage_proxy_countries,
        )
        result = extractor.extract()
        ba_token = str(result.get("ba_token") or "").strip()
        url = str(result.get("url") or "").strip()
        link_type = str(result.get("link_type") or "").strip()
        if require_ba_token and (not ba_token or "paypal_ba" not in link_type):
            return {
                "ok": False,
                "error": "ba_not_resolved",
                "error_code": "ba_not_resolved",
                "url": "",
                "ba_token": "",
                "cs_id": result.get("cs_id", ""),
                "link_type": link_type,
                "amount": result.get("amount"),
                "currency": result.get("currency", ""),
                "target_country": result.get("target_country", ""),
                "checkout_country": result.get("checkout_country", ""),
                "checkout_proxy": result.get("checkout_proxy", ""),
                "provider_proxy": result.get("provider_proxy", ""),
                "stripe_init_proxy": result.get("stripe_init_proxy", ""),
                "payment_method_proxy": result.get("payment_method_proxy", ""),
                "confirm_proxy": result.get("confirm_proxy", ""),
                "approve_proxy": result.get("approve_proxy", ""),
                "promotion_proxy": result.get("promotion_proxy", ""),
                "proxy_exits": result.get("proxy_exits", {}),
                "fallback_url": url,
            }

        # 兼容旧格式
        return {
            "ok": result.get("ok", False),
            "url": url,
            "ba_token": ba_token,
            "cs_id": result.get("cs_id", ""),
            "link_type": link_type,
            "amount": result.get("amount"),
            "currency": result.get("currency", ""),
            "target_country": result.get("target_country", ""),
            "checkout_country": result.get("checkout_country", ""),
            "checkout_proxy": result.get("checkout_proxy", ""),
            "provider_proxy": result.get("provider_proxy", ""),
            "stripe_init_proxy": result.get("stripe_init_proxy", ""),
            "payment_method_proxy": result.get("payment_method_proxy", ""),
            "confirm_proxy": result.get("confirm_proxy", ""),
            "approve_proxy": result.get("approve_proxy", ""),
            "promotion_proxy": result.get("promotion_proxy", ""),
            "proxy_exits": result.get("proxy_exits", {}),
        }
    except CheckoutNotZeroDueError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": "checkout_not_zero_due",
            "url": "",
            "ba_token": "",
            "amount": e.amount,
            "currency": e.currency,
            "target_country": target_country,
            "checkout_country": checkout_country,
        }
    except Exception as e:
        if extractor is not None:
            extractor.proxy_state.record_pair_result(
                extractor.checkout_proxy,
                extractor.provider_proxy,
                extractor.approve_proxy,
                False,
                str(e),
            )
        return {
            "ok": False,
            "error": str(e),
            "url": "",
            "ba_token": "",
            "target_country": target_country,
            "checkout_country": checkout_country,
        }



def _normalize_hosted_checkout_url(url: str) -> str:
    value = str(url or "").strip()
    if value:
        return value.replace("checkout.stripe.com", "pay.openai.com")
    return value


def _canonical_checkout_long_url(cs_id: str) -> str:
    cs_id = str(cs_id or "").strip()
    return f"https://pay.openai.com/c/pay/{cs_id}" if cs_id else ""


def _normalized_generation_type(paypal_cfg: dict[str, Any], override: str | None = None) -> str:
    raw = str(
        override
        or paypal_cfg.get("link_generation_type")
        or paypal_cfg.get("generation_type")
        or paypal_cfg.get("paypal_generation_type")
        or ""
    ).strip().lower().replace("-", "_")
    return raw


def _is_paypal_direct_generation_type(value: str) -> bool:
    return value in {
        "pp_direct", "paypal_direct", "direct_pp", "paypal_approve", "ba_direct", "ba_approve",
        "pp_direct_zero_due", "paypal_direct_zero_due", "direct_pp_zero_due", "paypal_approve_zero_due",
        "ba_direct_zero_due", "ba_approve_zero_due", "pp_direct_0_due", "paypal_direct_0_due",
        "pp_direct_force_zero", "paypal_direct_force_zero", "paypal_direct_require_zero_due",
    }


def _is_zero_due_generation_type(value: str) -> bool:
    return value in {
        "pp_direct_zero_due", "paypal_direct_zero_due", "direct_pp_zero_due", "paypal_approve_zero_due",
        "ba_direct_zero_due", "ba_approve_zero_due", "pp_direct_0_due", "paypal_direct_0_due",
        "pp_direct_force_zero", "paypal_direct_force_zero", "paypal_direct_require_zero_due",
    }


def _is_hosted_generation_type(value: str) -> bool:
    return value in {"long", "long_link", "hosted", "hosted_long", "hosted_long_url", "stripe_hosted", "chatgpt_checkout"}


def _is_chatgpt_checkout_link_generation_type(value: str) -> bool:
    return value in {"chatgpt_checkout_link", "checkout_link", "short_checkout", "chatgpt_short_link"}


def _chatgpt_checkout_url(processor_entity: str, cs_id: str) -> str:
    processor_entity = str(processor_entity or "").strip()
    cs_id = str(cs_id or "").strip()
    return f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}" if processor_entity and cs_id else ""


def _checkout_country_from_cfg(paypal_cfg: dict[str, Any], explicit_country: str | None = None, default: str = "JP") -> str:
    if explicit_country:
        return str(explicit_country).strip().upper()
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    candidates = [
        regions[0] if regions else "",
        paypal_cfg.get("checkout_country"),
        paypal_cfg.get("billing_country"),
        paypal_cfg.get("target_country"),
        default,
    ]
    for candidate in candidates:
        value = str(candidate or "").strip().upper()
        if value:
            return value
    return default


def _prepare_configured_stage_proxy(
    paypal_cfg: dict,
    state: PayPalProxyState,
    stage: str,
    proxy: str,
    expected_country: str,
    emit: Any,
) -> tuple[str, dict[str, Any]]:
    prepared = normalize_proxy_url(proxy)
    expected = str(expected_country or "").upper()
    if prepared and bool(paypal_cfg.get("rotate_proxy_sessions", False)):
        prepared = rotate_stage_proxy_session(prepared, expected)
    if not prepared:
        return "", {"ok": True, "stage": stage, "proxy": "DIRECT"}
    label = redact_proxy_url(prepared)
    if not bool(paypal_cfg.get("preflight_proxy_check", False)):
        emit("proxy", f"{stage} proxy={label} (preflight disabled)")
        return prepared, {"ok": True, "stage": stage, "proxy": label}
    result = probe_proxy(
        prepared,
        expected_country=expected,
        stage=stage,
        timeout=float(paypal_cfg.get("proxy_probe_timeout_seconds", 12) or 12),
    )
    state.record_result(stage, prepared, result.ok, result.error, result.country_code)
    detail = {**result.to_dict(), "proxy": label}
    if not result.ok:
        raise RuntimeError(
            f"proxy_preflight_failed:{stage}:expected={expected or 'ANY'}:"
            f"actual={result.country_code or 'unknown'}:{result.error}"
        )
    emit("proxy", f"{stage} exit={result.ip}/{result.country_code} {result.country}")
    return prepared, detail


def generate_chatgpt_checkout_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
) -> dict[str, Any]:
    """Create a ChatGPT checkout session and return chatgpt.com/checkout/{entity}/{cs_id}."""
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    target_country = str(
        target_country
        or paypal_cfg.get("target_country")
        or checkout_country
        or (regions[0] if regions else None)
        or "US"
    ).strip().upper()
    checkout_country = str(
        checkout_country
        or paypal_cfg.get("checkout_country")
        or paypal_cfg.get("billing_country")
        or (regions[0] if regions else None)
        or target_country
        or "US"
    ).strip().upper()
    currency = CURRENCY_MAP.get(checkout_country, "USD")
    stage_proxies = _proxies_from_config(cfg, checkout_country=checkout_country, target_country=target_country)
    checkout_proxy = str(checkout_proxy or proxy or stage_proxies["checkout"] or "").strip()
    state = _paypal_proxy_state(paypal_cfg)

    def emit(step: str, msg: str, **kw: Any) -> None:
        print(f"[{step}] {msg}", file=sys.stderr)

    try:
        expected_country = str(
            ((paypal_cfg.get("stage_proxy_countries") or {}).get("checkout") if isinstance(paypal_cfg.get("stage_proxy_countries"), dict) else "")
            or infer_proxy_country(checkout_proxy)
            or checkout_country
        ).upper()
        checkout_proxy, proxy_exit = _prepare_configured_stage_proxy(
            paypal_cfg, state, "checkout", checkout_proxy, expected_country, emit,
        )
        emit("checkout", f"Stage 1: proxy={redact_proxy_url(checkout_proxy)} for ChatGPT checkout link")
        _cookie = ""
        if isinstance(auth_context, dict):
            _cookie = str(auth_context.get("cookie_header") or "")
        checkout_body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checkout_country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": "custom",
        }
        r = _checkout_post(
            "https://chatgpt.com/backend-api/payments/checkout",
            checkout_body, access_token, _cookie, checkout_proxy, CHATGPT_TIMEOUT,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "link_type": "chatgpt_checkout_link"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "link_type": "chatgpt_checkout_link"}
        checkout_data = r.json() or {}
        cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("session_id") or checkout_data.get("id") or ""
        if not str(cs_id).startswith("cs_"):
            return {"ok": False, "error": f"checkout response missing cs_id: {json.dumps(checkout_data, ensure_ascii=False)[:200]}", "error_code": "checkout_bad_response", "link_type": "chatgpt_checkout_link"}
        processor_entity = checkout_data.get("processor_entity") or ("openai_llc" if checkout_country == "US" else "openai_ie")
        url = _chatgpt_checkout_url(processor_entity, cs_id)
        emit("checkout", f"checkout success: cs_id={cs_id} entity={processor_entity} country={checkout_country} currency={currency}")
        return {
            "ok": True,
            "url": url,
            "checkout_url": url,
            "short_url": url,
            "ba_token": "",
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "link_type": "chatgpt_checkout_link",
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "currency": currency,
            "checkout_proxy": redact_proxy_url(checkout_proxy),
            "provider_proxy": "",
            "approve_proxy": "",
            "proxy_exits": {"checkout": proxy_exit},
            "promo_campaign_id": "plus-1-month-free",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "chatgpt_checkout_link_failed", "link_type": "chatgpt_checkout_link", "url": ""}


def generate_hosted_long_url(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    stripe_init_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    require_zero: bool | None = None,
) -> dict[str, Any]:
    """Generate a ChatGPT/Stripe hosted checkout URL without entering BA/approve flow."""
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    target_country = str(
        target_country
        or paypal_cfg.get("target_country")
        or checkout_country
        or (regions[0] if regions else None)
        or "US"
    ).strip().upper()
    checkout_country = str(
        checkout_country
        or paypal_cfg.get("checkout_country")
        or paypal_cfg.get("billing_country")
        or (regions[0] if regions else None)
        or target_country
        or "US"
    ).strip().upper()
    currency = CURRENCY_MAP.get(checkout_country, "USD")
    stage_proxies = _proxies_from_config(cfg, checkout_country=checkout_country, target_country=target_country)
    checkout_proxy = str(checkout_proxy or proxy or stage_proxies["checkout"] or "").strip()
    provider_proxy = str(provider_proxy or proxy or stage_proxies["provider"] or "").strip()
    stripe_init_proxy = str(stripe_init_proxy or stage_proxies.get("stripe_init") or provider_proxy).strip()
    state = _paypal_proxy_state(paypal_cfg)
    if require_zero is None:
        require_zero = bool(paypal_cfg.get("require_zero_due", True))

    def emit(step: str, msg: str, **kw: Any) -> None:
        print(f"[{step}] {msg}", file=sys.stderr)

    try:
        countries = paypal_cfg.get("stage_proxy_countries") if isinstance(paypal_cfg.get("stage_proxy_countries"), dict) else {}
        checkout_proxy, checkout_exit = _prepare_configured_stage_proxy(
            paypal_cfg,
            state,
            "checkout",
            checkout_proxy,
            str(countries.get("checkout") or infer_proxy_country(checkout_proxy) or checkout_country),
            emit,
        )
        emit("checkout", f"Stage 1: proxy={redact_proxy_url(checkout_proxy)} for hosted checkout")
        _cookie = ""
        if isinstance(auth_context, dict):
            _cookie = str(auth_context.get("cookie_header") or "")
        checkout_body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checkout_country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": "custom",
        }
        r = _checkout_post(
            "https://chatgpt.com/backend-api/payments/checkout",
            checkout_body, access_token, _cookie, checkout_proxy, CHATGPT_TIMEOUT,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "link_type": "chatgpt_checkout_hosted_long_url"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "link_type": "chatgpt_checkout_hosted_long_url"}
        checkout_data = r.json() or {}
        cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("session_id") or checkout_data.get("id") or ""
        if not str(cs_id).startswith("cs_"):
            return {"ok": False, "error": f"checkout response missing cs_id: {json.dumps(checkout_data, ensure_ascii=False)[:200]}", "error_code": "checkout_bad_response", "link_type": "chatgpt_checkout_hosted_long_url"}
        stripe_pk = checkout_data.get("publishable_key") or DEFAULT_STRIPE_PK
        processor_entity = checkout_data.get("processor_entity") or ("openai_llc" if checkout_country == "US" else "openai_ie")
        emit("checkout", f"checkout success: cs_id={cs_id} country={checkout_country} currency={currency}")

        stripe_init_proxy, stripe_exit = _prepare_configured_stage_proxy(
            paypal_cfg,
            state,
            "stripe_init",
            stripe_init_proxy,
            str(countries.get("stripe_init") or infer_proxy_country(stripe_init_proxy) or target_country),
            emit,
        )
        emit("stripe_init", f"Stage 2: proxy={redact_proxy_url(stripe_init_proxy)} for Stripe init")
        stripe = _new_session(stripe_init_proxy)
        init_body = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        init_resp = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/init", data=init_body, timeout=DEFAULT_TIMEOUT)
        if init_resp.status_code >= 400:
            return {"ok": False, "error": f"stripe init failed: {init_resp.status_code} {init_resp.text[:300]}", "error_code": "stripe_init_failed", "link_type": "chatgpt_checkout_hosted_long_url", "cs_id": cs_id, "target_country": target_country, "checkout_country": checkout_country, "billing_country": checkout_country}
        init = init_resp.json() or {}
        amount_info = stripe_amount_details(init)
        amount = amount_info.get("amount")
        state.record_zero_result(checkout_proxy, checkout_country, amount)
        emit("stripe_init", f"amount={amount} currency={amount_info.get('currency')} source={amount_info.get('source')}")
        if require_zero and amount != 0:
            amount_label = "unknown" if amount is None else str(amount)
            return {
                "ok": False,
                "error": f"checkout_not_zero_due: amount={amount_label} {amount_info.get('currency')}",
                "error_code": "checkout_not_zero_due",
                "link_type": "chatgpt_checkout_hosted_long_url",
                "url": "",
                "cs_id": cs_id,
                "amount": amount,
                "currency": str(amount_info.get("currency") or currency).upper(),
                "target_country": target_country,
                "checkout_country": checkout_country,
                "billing_country": checkout_country,
                "payment_method_types": init.get("payment_method_types") or [],
            }
        short_url = _canonical_checkout_long_url(cs_id)
        hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or short_url
        return {
            "ok": True,
            "url": hosted_url,
            "checkout_url": hosted_url,
            "short_url": short_url,
            "stripe_hosted_url": hosted_url,
            "ba_token": "",
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "link_type": "chatgpt_checkout_hosted_long_url",
            "amount": amount,
            "currency": str(amount_info.get("currency") or currency).upper(),
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "payment_method_types": init.get("payment_method_types") or [],
            "checkout_proxy": redact_proxy_url(checkout_proxy),
            "provider_proxy": redact_proxy_url(provider_proxy),
            "stripe_init_proxy": redact_proxy_url(stripe_init_proxy),
            "approve_proxy": "",
            "proxy_exits": {"checkout": checkout_exit, "stripe_init": stripe_exit},
            "promo_campaign_id": "plus-1-month-free",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "hosted_long_url_failed", "link_type": "chatgpt_checkout_hosted_long_url", "url": ""}


def _default_qr_path(prefix: str = "upi") -> str:
    directory = Path(PROJECT_ROOT) / "runtime" / "upi_qr"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png")


def _write_qr_png(data: str, qr_path: str = "") -> str:
    url = str(data or "").strip()
    if not url:
        return ""
    path = Path(qr_path or _default_qr_path("upi"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode
    except Exception as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("qrcode package is required for UPI QR generation; run pip install qrcode[pil]") from exc
    img = qrcode.make(url)
    img.save(str(path))
    return str(path)


# ─── UPI 辅助函数 ──────────────────────────────────────────────────────────────


def _upi_nested_get(data: Any, path: list[str]) -> Any:
    """安全地按路径取嵌套值."""
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _upi_amount_minor(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(value) if value == value else None  # reject NaN
    if isinstance(value, dict):
        for key in ("amount", "amount_due", "minor", "value"):
            nested = _upi_amount_minor(value.get(key))
            if nested is not None:
                return nested
    return None


def _upi_extract_payment_amount(init_data: Any) -> int:
    return (
        _upi_amount_minor(_upi_nested_get(init_data, ["total_summary", "due"]))
        or _upi_amount_minor(_upi_nested_get(init_data, ["invoice", "amount_due"]))
        or _upi_amount_minor(_upi_nested_get(init_data, ["elements_options", "amount"]))
        or 0
    )


def _upi_get_payment_method_types(init_data: Any) -> list[str]:
    candidates = [
        _upi_nested_get(init_data, ["elements_options", "payment_method_types"]),
        init_data.get("payment_method_types") if isinstance(init_data, dict) else None,
        _upi_nested_get(init_data, ["payment_method_preference", "payment_method_types"]),
        _upi_nested_get(init_data, ["session", "payment_method_types"]),
        init_data.get("ordered_payment_method_types") if isinstance(init_data, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return [str(item).lower() for item in candidate]
    return []


def _upi_scan_free_trial(value: Any, depth: int = 0, signals: dict | None = None) -> dict:
    """递归搜索 Stripe init 响应中的免费试用信号."""
    if signals is None:
        signals = {"coupon_name": "", "percent_off": None, "duration_months": None}
    if depth > 8 or not value or not isinstance(value, (dict, list)):
        return signals
    if isinstance(value, list):
        for item in value:
            _upi_scan_free_trial(item, depth + 1, signals)
        return signals
    for key, next_val in value.items():
        lower_key = key.lower()
        if isinstance(next_val, str):
            lower_val = next_val.lower()
            if not signals["coupon_name"] and (
                lower_val.startswith("upi://")
                or "free trial" in lower_val
                or "1 month free" in lower_val
                or "one month free" in lower_val
                or "plus-1-month-free" in lower_val
                or "coupon" in lower_key
                or "promotion" in lower_key
            ):
                signals["coupon_name"] = next_val
        elif isinstance(next_val, (int, float)) and not isinstance(next_val, bool):
            if lower_key in ("percent_off", "percentoff"):
                signals["percent_off"] = max(signals["percent_off"] or 0, next_val)
            if lower_key in ("duration_in_months", "durationmonths"):
                signals["duration_months"] = max(signals["duration_months"] or 0, next_val)
        if next_val and isinstance(next_val, (dict, list)):
            _upi_scan_free_trial(next_val, depth + 1, signals)
    return signals


def _upi_get_free_trial_status(init_data: Any) -> dict:
    """分析 Stripe init 响应判断是否有免费试用."""
    due = _upi_extract_payment_amount(init_data)
    signals = _upi_scan_free_trial(init_data)
    pm_types = _upi_get_payment_method_types(init_data)
    coupon = signals["coupon_name"].strip()
    coupon_lower = coupon.lower()
    looks_like_trial = any(s in coupon_lower for s in ("free trial", "1 month free", "one month free", "plus-1-month-free"))
    looks_like_full_discount = (signals["percent_off"] is not None and signals["percent_off"] >= 100) or looks_like_trial
    return {
        "has_free_trial": due == 0 or (looks_like_full_discount and signals["percent_off"] is not None and signals["percent_off"] >= 100),
        "has_upi": "upi" in pm_types,
        "due": due,
        "coupon_name": coupon,
        "percent_off": signals["percent_off"],
        "duration_months": signals["duration_months"],
        "payment_method_types": pm_types,
    }


def _upi_merge_qr_key(result: dict, key: str, value: Any) -> None:
    """将 UPI QR 数据字段合并到 result dict."""
    if value is None:
        return
    normalized_key = key.lower()
    if isinstance(value, str):
        if value.startswith("upi://") and not result.get("upi_uri"):
            result["upi_uri"] = value
            result["mobile_auth_url"] = value
        elif value.startswith("https://payments.stripe.com/upi/instructions/") and not result.get("hosted_instructions_url"):
            result["hosted_instructions_url"] = value
        elif value.startswith("https://qr.stripe.com/") and "svg" in value.lower() and not result.get("qr_image_url_svg"):
            result["qr_image_url_svg"] = value
        elif value.startswith("https://qr.stripe.com/") and "png" in value.lower() and not result.get("qr_image_url_png"):
            result["qr_image_url_png"] = value
    known_keys = {
        "hosted_instructions_url": "hosted_instructions_url",
        "mobile_auth_url": "mobile_auth_url",
        "upi_uri": "upi_uri",
        "image_url_svg": "qr_image_url_svg",
        "qr_image_url_svg": "qr_image_url_svg",
        "image_url_png": "qr_image_url_png",
        "qr_image_url_png": "qr_image_url_png",
    }
    if normalized_key in known_keys and isinstance(value, str) and value:
        out_key = known_keys[normalized_key]
        result.setdefault(out_key, value)
    if normalized_key in ("expires_at", "expires_after_timestamp", "qr_expires_at"):
        try:
            expires = int(value)
            if expires > 0 and not result.get("expires_at"):
                result["expires_at"] = expires
        except (ValueError, TypeError):
            pass


def _upi_extract_next_action(data: Any) -> dict:
    """递归遍历 Stripe 响应提取 UPI QR 数据."""
    result: dict[str, Any] = {}
    def walk(value: Any, key: str = "") -> None:
        _upi_merge_qr_key(result, key, value)
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for child_key, child_value in value.items():
            if child_key == "qr_code" and isinstance(child_value, dict):
                _upi_merge_qr_key(result, "qr_expires_at", child_value.get("expires_at"))
                _upi_merge_qr_key(result, "image_url_svg", child_value.get("image_url_svg"))
                _upi_merge_qr_key(result, "image_url_png", child_value.get("image_url_png"))
            walk(child_value, child_key)
    walk(data)
    return result


def _upi_extract_qr_from_html(html: str) -> dict:
    """从 Stripe hosted instructions HTML 页面解析 UPI QR 数据."""
    result: dict[str, Any] = {}
    # 解析 <meta id="payload" data-message="..." />
    meta_match = re.search(r'<meta\b[^>]*\bid=["\']payload["\'][^>]*\bdata-message=["\']([^"\']+)["\']', html, re.I)
    if not meta_match:
        meta_match = re.search(r'<meta\b[^>]*\bdata-message=["\']([^"\']+)["\'][^>]*\bid=["\']payload["\']', html, re.I)
    if meta_match:
        import base64
        raw = meta_match.group(1).replace("&quot;", '"')
        raw = raw.replace("-", "+").replace("_", "/")
        padded = raw + "=" * (4 - len(raw) % 4) if len(raw) % 4 else raw
        try:
            payload = json.loads(base64.b64decode(padded).decode("utf-8"))
            if isinstance(payload, dict):
                _upi_merge_qr_key(result, "mobile_auth_url", payload.get("mobile_auth_url"))
                _upi_merge_qr_key(result, "upi_uri", payload.get("upi_uri"))
                _upi_merge_qr_key(result, "expires_at", payload.get("expires_at") or payload.get("expires_after_timestamp"))
        except Exception:
            pass
    # 解析 <img src="https://qr.stripe.com/..." />
    for img_match in re.finditer(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
        src = img_match.group(1).replace("&amp;", "&")
        tag = img_match.group(0)
        if "qr.stripe.com" in src or "QRCode-image" in tag:
            _upi_merge_qr_key(result, "png" if "png" in src.lower() else "svg", src)
            break
    return result


def _upi_hydrate_qr_data(qr_data: dict, proxy_url: str) -> dict:
    """如果 JSON 中没有 upi://，访问 hosted_instructions_url 从 HTML 中解析."""
    result = dict(qr_data)
    hosted_url = result.get("hosted_instructions_url")
    if hosted_url and not result.get("upi_uri"):
        try:
            session = _new_session(proxy_url)
            resp = session.get(hosted_url, timeout=DEFAULT_TIMEOUT, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://js.stripe.com/",
            })
            if resp.status_code < 400:
                extracted = _upi_extract_qr_from_html(resp.text)
                for k, v in extracted.items():
                    if v and not result.get(k):
                        result[k] = v
        except Exception:
            pass
    return result


def _method_cfg(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    section = cfg.get(method) if isinstance(cfg.get(method), dict) else {}
    return section if isinstance(section, dict) else {}


def _payment_stage_proxies_from_config(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    method_cfg = _method_cfg(cfg, method)
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    method_api = method_cfg.get("stage_proxy_api_urls") if isinstance(method_cfg.get("stage_proxy_api_urls"), dict) else {}
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    paypal_api = paypal_cfg.get("stage_proxy_api_urls") if isinstance(paypal_cfg.get("stage_proxy_api_urls"), dict) else {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""

    def pick(key: str, fallback: str = "") -> str:
        value = _stage_proxy_value(method_stage, method_api, key)
        if value:
            return value
        return _stage_proxy_value(paypal_stage, paypal_api, key, fallback)

    checkout = pick("checkout", proxy_default)
    provider = pick("provider") or pick("stripe_init")
    if method == "upi" and not provider:
        provider = "http://107.150.109.49:11001"
    provider = provider or proxy_default
    approve = pick("approve") or pick("confirm")
    if method == "upi" and not approve:
        approve = provider or "http://107.150.109.49:11001"
    approve = approve or provider or proxy_default
    return {"checkout": checkout, "provider": provider, "approve": approve}


def generate_upi_qr_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    approve_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    payment_country: str | None = None,
    require_zero: bool | None = None,
    qr_path: str | None = None,
) -> dict[str, Any]:
    """Generate a UPI payment link with full Stripe Confirm + Approve flow.

    Implements the complete 7-stage UPI extraction pipeline:
      1. ChatGPT checkout (create cs_id)
      2. Stripe init (get payment page data)
      3. Free trial detection (coupon / discount analysis)
      4. Tax region update (set IN billing address)
      5. Stripe confirm (submit UPI payment method)
      6. ChatGPT approve (trigger payment approval)
      7. Poll payment page → extract upi:// URI → hydrate → render QR

    Returns ``upi://`` deep link + QR PNG path on success, or
    ``stripe_hosted_url`` as fallback if UPI data is not available.
    """
    cfg = _load_json(DEFAULT_CONFIG_PATH)
    upi_cfg = _method_cfg(cfg, "upi")
    stage_proxies = _payment_stage_proxies_from_config(cfg, "upi")
    _checkout = checkout_proxy or proxy or stage_proxies["checkout"]
    _provider = provider_proxy or proxy or stage_proxies["provider"]
    _approve = approve_proxy or proxy or stage_proxies["approve"]
    checkout_proxy = str(_checkout or "").strip()
    provider_proxy = str(_provider or "").strip()
    approve_proxy = str(_approve or "").strip()
    regions = upi_cfg.get("billing_regions") if isinstance(upi_cfg.get("billing_regions"), list) else []
    checkout_country = str(
        checkout_country
        or upi_cfg.get("checkout_country")
        or upi_cfg.get("checkout_billing_country")
        or upi_cfg.get("billing_country")
        or target_country
        or upi_cfg.get("target_country")
        or (regions[0] if regions else "IN")
        or "IN"
    ).upper()
    payment_country = str(
        payment_country
        or upi_cfg.get("payment_country")
        or upi_cfg.get("payment_method_country")
        or "IN"
    ).upper()
    target_country = checkout_country
    currency = CURRENCY_MAP.get(checkout_country, "INR")
    payment_currency = CURRENCY_MAP.get(payment_country, "INR")
    if require_zero is None:
        paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
        require_zero = bool(upi_cfg.get("require_zero_due", paypal_cfg.get("require_zero_due", True)))

    def emit(step: str, msg: str, **kw: Any) -> None:
        print(f"[{step}] {msg}", file=sys.stderr)

    try:
        # ── Stage 1: ChatGPT checkout ────────────────────────────────────
        emit("checkout", f"Stage 1: using {checkout_proxy or 'DIRECT'} for UPI checkout")
        cs = _new_session(checkout_proxy)
        cs.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://chatgpt.com/",
        })
        checkout_body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checkout_country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": str(upi_cfg.get("checkout_ui_mode") or "hosted"),
        }
        r = cs.post(UPI_CHECKOUT_URL, json=checkout_body, timeout=CHATGPT_TIMEOUT)
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "payment_method": "upi"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "payment_method": "upi"}
        checkout_data = r.json() or {}
        cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("id", "")
        if not str(cs_id).startswith("cs_"):
            return {"ok": False, "error": f"checkout response missing cs_id: {json.dumps(checkout_data, ensure_ascii=False)[:200]}", "error_code": "checkout_bad_response", "payment_method": "upi"}
        stripe_pk = checkout_data.get("publishable_key") or DEFAULT_STRIPE_PK
        processor_entity = checkout_data.get("processor_entity") or ("openai_llc" if checkout_country == "US" else "openai_ie")
        emit("checkout", f"checkout success: cs_id={cs_id}")

        # ── Stage 2: Stripe init (custom mode) ───────────────────────────
        emit("stripe_init", f"Stage 2: using {provider_proxy or 'DIRECT'} for Stripe init")
        stripe = _new_session(provider_proxy)
        stripe_js_id = str(uuid.uuid4())
        init_body = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Kolkata",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        init_resp = stripe.post(
            STRIPE_PAYMENT_PAGE_INIT_URL_T.format(cs_id=cs_id),
            data=init_body, timeout=DEFAULT_TIMEOUT,
        )
        if init_resp.status_code >= 400:
            return {"ok": False, "error": f"stripe init failed: {init_resp.status_code} {init_resp.text[:300]}", "error_code": "stripe_init_failed", "payment_method": "upi", "cs_id": cs_id}
        init = init_resp.json() or {}
        emit("stripe_init", f"init success, analyzing free trial...")

        # ── Stage 3: Free trial detection ────────────────────────────────
        ft_status = _upi_get_free_trial_status(init)
        amount = ft_status["due"]
        pm_types = ft_status["payment_method_types"]
        emit("stripe_init", f"free_trial={ft_status['has_free_trial']} due={amount} coupon={ft_status['coupon_name']} upi={ft_status['has_upi']}")
        if require_zero and not ft_status["has_free_trial"]:
            return {
                "ok": False, "error": f"no_free_trial: due={amount} coupon={ft_status['coupon_name']} percent_off={ft_status['percent_off']}",
                "error_code": "no_free_trial", "payment_method": "upi", "cs_id": cs_id,
                "amount": amount, "currency": payment_currency.upper(),
                "target_country": target_country, "checkout_country": checkout_country,
                "billing_country": checkout_country, "payment_country": payment_country,
                "coupon_name": ft_status["coupon_name"], "percent_off": ft_status["percent_off"],
            }
        if pm_types and not ft_status["has_upi"]:
            return {"ok": False, "error": f"UPI not available for checkout; payment_method_types={pm_types}", "error_code": "upi_not_available", "payment_method": "upi", "cs_id": cs_id, "payment_method_types": pm_types, "amount": amount, "currency": payment_currency.upper(), "target_country": target_country, "checkout_country": checkout_country, "billing_country": checkout_country, "payment_country": payment_country}

        # ── Stage 4: Tax region update ───────────────────────────────────
        emit("tax_region", f"Stage 4: updating tax region to IN")
        tax_body = {
            "tax_region[country]": UPI_BILLING_IN["country"],
            "tax_region[postal_code]": UPI_BILLING_IN["postal"],
            "tax_region[state]": UPI_BILLING_IN["state"],
            "tax_region[city]": UPI_BILLING_IN["city"],
            "tax_region[line1]": UPI_BILLING_IN["line1"],
            "tax_region[line2]": UPI_BILLING_IN["line2"],
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        tax_resp = stripe.post(
            STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
            data=tax_body, timeout=DEFAULT_TIMEOUT,
        )
        if tax_resp.status_code >= 400:
            emit("tax_region", f"tax region update failed (non-fatal): {tax_resp.status_code} {tax_resp.text[:200]}")
        else:
            emit("tax_region", "tax region updated")
            # Use tax-updated data for confirm
            init = tax_resp.json() or init

        # ── Stage 5: Stripe confirm (submit UPI payment method) ──────────
        emit("stripe_confirm", f"Stage 5: Stripe confirm with UPI payment method")
        confirm_body = {
            "payment_method_data[type]": "upi",
            "payment_method_data[billing_details][name]": UPI_BILLING_IN["name"],
            "payment_method_data[billing_details][email]": UPI_BILLING_IN["email"],
            "payment_method_data[billing_details][address][line1]": UPI_BILLING_IN["line1"],
            "payment_method_data[billing_details][address][line2]": UPI_BILLING_IN["line2"],
            "payment_method_data[billing_details][address][city]": UPI_BILLING_IN["city"],
            "payment_method_data[billing_details][address][state]": UPI_BILLING_IN["state"],
            "payment_method_data[billing_details][address][postal_code]": UPI_BILLING_IN["postal"],
            "payment_method_data[billing_details][address][country]": UPI_BILLING_IN["country"],
            "expected_amount": str(amount),
            "expected_payment_method_type": "upi",
            "return_url": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        init_checksum = init.get("init_checksum") if isinstance(init, dict) else None
        if init_checksum:
            confirm_body["init_checksum"] = str(init_checksum)
        confirm_resp = stripe.post(
            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
            data=confirm_body, timeout=DEFAULT_TIMEOUT,
        )
        if confirm_resp.status_code >= 400:
            emit("stripe_confirm", f"confirm failed: {confirm_resp.status_code} {confirm_resp.text[:300]}")
            # Fallback to hosted URL
            hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or f"https://pay.openai.com/c/pay/{cs_id}"
            written_qr_path = _write_qr_png(hosted_url, qr_path or "")
            return {
                "ok": True, "payment_method": "upi", "method": "upi",
                "link_type": "upi_hosted_fallback", "url": hosted_url, "qr_data": hosted_url,
                "qr_path": written_qr_path, "cs_id": cs_id, "processor_entity": processor_entity,
                "amount": amount, "currency": payment_currency.upper(),
                "target_country": target_country, "checkout_country": checkout_country,
                "billing_country": checkout_country, "payment_country": payment_country,
                "payment_method_types": pm_types, "checkout_proxy": checkout_proxy,
                "provider_proxy": provider_proxy, "approve_proxy": approve_proxy,
                "warning": f"stripe_confirm_failed: {confirm_resp.status_code}",
            }
        confirm_data = confirm_resp.json() or {}
        emit("stripe_confirm", "confirm success")

        # ── Stage 6: ChatGPT approve ─────────────────────────────────────
        emit("approve", f"Stage 6: ChatGPT approve using {approve_proxy or 'DIRECT'}")
        approve_session = _new_session(approve_proxy)
        approve_session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
        })
        approval_ok = False
        approval_data: dict[str, Any] = {}
        # Try confirm endpoint first
        try:
            confirm_chatgpt = approve_session.post(
                UPI_CHECKOUT_CONFIRM_URL,
                json={"checkout_session_id": cs_id, "selected_payment_method_type": "upi"},
                timeout=CHATGPT_TIMEOUT,
            )
            if confirm_chatgpt.status_code < 400:
                confirm_json = confirm_chatgpt.json() or {}
                if str(confirm_json.get("result", "")).lower() == "approved":
                    emit("approve", "approved via confirm endpoint")
                    approval_data = confirm_json
                    approval_ok = True
                else:
                    approval_ok = False
                    approval_data = confirm_json
            else:
                approval_ok = False
                approval_data = {}
        except Exception:
            approval_ok = False
            approval_data = {}

        # If confirm didn't approve, try approve endpoint with retries
        if not approval_ok:
            for attempt in range(1, UPI_APPROVAL_MAX_ATTEMPTS + 1):
                try:
                    approve_resp = approve_session.post(
                        UPI_CHECKOUT_APPROVE_URL,
                        json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
                        timeout=CHATGPT_TIMEOUT,
                    )
                    if approve_resp.status_code < 400:
                        approve_json = approve_resp.json() or {}
                        if str(approve_json.get("result", "")).lower() == "approved":
                            emit("approve", f"approved on attempt {attempt}")
                            approval_ok = True
                            approval_data = approve_json
                            break
                    if attempt % 10 == 0:
                        emit("approve", f"attempt {attempt}/{UPI_APPROVAL_MAX_ATTEMPTS}: status={approve_resp.status_code}")
                except Exception as ex:
                    if attempt % 10 == 0:
                        emit("approve", f"attempt {attempt} exception: {ex}")

        if not approval_ok:
            emit("approve", "approval failed after all attempts, trying hosted fallback")

        # ── Stage 7: Poll payment page for upi:// URI ────────────────────
        emit("poll", f"Stage 7: polling payment page for UPI QR data")
        qr_data: dict[str, Any] = {}
        # First check confirm/approve responses
        for source in (confirm_data, approval_data):
            extracted = _upi_extract_next_action(source)
            for k, v in extracted.items():
                if v and not qr_data.get(k):
                    qr_data[k] = v

        # Poll Stripe payment page
        for attempt in range(UPI_QR_POLL_MAX_ATTEMPTS):
            if qr_data.get("upi_uri") or qr_data.get("hosted_instructions_url") or qr_data.get("qr_image_url_svg") or qr_data.get("qr_image_url_png"):
                break
            emit("poll", f"poll attempt {attempt + 1}/{UPI_QR_POLL_MAX_ATTEMPTS}")
            if attempt > 0:
                time.sleep(UPI_QR_POLL_INTERVAL)
            try:
                page_resp = stripe.get(
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    params={"key": stripe_pk, "_stripe_version": STRIPE_VERSION},
                    timeout=DEFAULT_TIMEOUT,
                )
                if page_resp.status_code == 200:
                    extracted = _upi_extract_next_action(page_resp.json() or {})
                    for k, v in extracted.items():
                        if v and not qr_data.get(k):
                            qr_data[k] = v
                else:
                    if page_resp.status_code >= 400:
                        break
            except Exception:
                pass

        # If still no upi://, try re-init then hydrate
        if not qr_data.get("upi_uri") and not qr_data.get("hosted_instructions_url"):
            emit("poll", "re-init to check for UPI data")
            try:
                refresh_resp = stripe.post(
                    STRIPE_PAYMENT_PAGE_INIT_URL_T.format(cs_id=cs_id),
                    data=init_body, timeout=DEFAULT_TIMEOUT,
                )
                if refresh_resp.status_code == 200:
                    extracted = _upi_extract_next_action(refresh_resp.json() or {})
                    for k, v in extracted.items():
                        if v and not qr_data.get(k):
                            qr_data[k] = v
            except Exception:
                pass

        # Hydrate: fetch hosted_instructions_url HTML if no upi://
        emit("hydrate", "hydrating UPI QR data from hosted instructions")
        qr_data = _upi_hydrate_qr_data(qr_data, provider_proxy)

        upi_uri = qr_data.get("upi_uri") or qr_data.get("mobile_auth_url") or ""
        hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or f"https://pay.openai.com/c/pay/{cs_id}"
        expires_at = qr_data.get("expires_at") or int(time.time()) + 300

        if upi_uri:
            emit("done", f"UPI URI extracted: {upi_uri[:40]}...")
            qr_data_str = upi_uri
            link_type = "upi_deep_link"
        else:
            emit("done", f"no upi:// URI found, falling back to hosted URL")
            qr_data_str = hosted_url
            link_type = "upi_hosted_fallback"

        written_qr_path = _write_qr_png(qr_data_str, qr_path or "")
        return {
            "ok": True,
            "payment_method": "upi",
            "method": "upi",
            "link_type": link_type,
            "url": upi_uri or hosted_url,
            "upi_uri": upi_uri,
            "hosted_url": hosted_url,
            "qr_data": qr_data_str,
            "qr_path": written_qr_path,
            "expires_at": expires_at,
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "amount": amount,
            "currency": payment_currency.upper(),
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "payment_country": payment_country,
            "payment_method_types": pm_types,
            "coupon_name": ft_status["coupon_name"],
            "approval_ok": approval_ok,
            "checkout_proxy": checkout_proxy,
            "provider_proxy": provider_proxy,
            "approve_proxy": approve_proxy,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "upi_qr_failed", "payment_method": "upi", "url": "", "qr_path": ""}


def generate_payment_link(
    access_token: str,
    proxy: Any = None,
    payment_method: Any = "paypal",
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility entrypoint backed by the unified payment-link manager."""
    from .payment_link_manager import generate_payment_link as managed_generate

    return managed_generate(
        access_token=access_token,
        proxy=proxy,
        payment_method=payment_method,
        auth_context=auth_context,
        paypal_generation_type=paypal_generation_type,
        **kwargs,
    )


if __name__ == "__main__":
    main()
