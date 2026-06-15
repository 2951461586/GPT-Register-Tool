#!/usr/bin/env python3
"""PP 直链生成器 -- 分段代理池版。

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
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit

import requests

# curl_cffi 可选
_CurlCffiSession = None
if os.environ.get("USE_CURL_CFFI", "").lower() in ("1", "true", "yes"):
    try:
        from curl_cffi.requests import Session as _CurlCffiSession
    except ImportError:
        _CurlCffiSession = None

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
RETRY_ATTEMPTS = 3

CURRENCY_MAP = {
    "US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "JP": "JPY",
    "AU": "AUD", "CA": "CAD", "SG": "SGD", "NZ": "NZD", "IE": "EUR",
    "TH": "THB", "ID": "IDR", "IN": "INR", "BR": "BRL", "KR": "KRW",
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
}

DEFAULT_TARGET_COUNTRIES = ("AU", "TH", "US", "GB", "DE", "JP", "SG", "NZ", "CA", "IE")
DEFAULT_CHECKOUT_COUNTRIES = ("JP", "TH")

PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\s\"'<>]+", re.I)
PAYPAL_BA_RE = re.compile(r"https://www\.paypal\.com/agreements/approve\?[^\s\"']+", re.I)

# ─── Session 工厂 ─────────────────────────────────────────────────────────────


def _new_session(proxy: str = ""):
    s = None
    if _CurlCffiSession is not None:
        try:
            s = _CurlCffiSession(impersonate="chrome", verify=True)
        except Exception:
            s = None
    if s is None:
        s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


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
    if "sid-" not in proxy:
        return proxy
    # cliproxy 格式: sid-ZLaanVyM (字母数字混合)
    m = re.search(r"sid-([A-Za-z0-9]+)", proxy)
    if m:
        old_sid = m.group(1)
        # 生成新的随机 session ID (8位字母数字)
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        new_sid = "".join(random.choice(chars) for _ in range(len(old_sid)))
        return proxy[:m.start(1)] + new_sid + proxy[m.end(1):]
    return proxy


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
        approve_proxy: str = "",
        target_country: str = "DE",
        stripe_pk: str = "",
        require_zero: bool = True,
        emit: Any = None,
    ):
        self.access_token = access_token
        self.checkout_proxy = normalize_proxy_url(checkout_proxy)
        self.provider_proxy = normalize_proxy_url(provider_proxy)
        self.approve_proxy = normalize_proxy_url(approve_proxy or provider_proxy)
        self.target_country = target_country.upper()
        self.currency = CURRENCY_MAP.get(self.target_country, "EUR")
        self.stripe_pk = stripe_pk or DEFAULT_STRIPE_PK
        self.require_zero = require_zero
        self.emit = emit or (lambda step, msg, **kw: None)
        self.runtime_version = "6f8494a281"
        self.stripe_js_id = str(uuid.uuid4())
        self.elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        self.elements_session_config_id = str(uuid.uuid4())

    def _log(self, step: str, msg: str, **kw):
        self.emit(step, msg, **kw)

    # ─── Stage 1: Checkout (JP/TH 代理) ───────────────────────────────────

    def _create_checkout(self) -> dict:
        self._log("checkout", f"Stage 1: 使用 {self.checkout_proxy or 'DIRECT'} 代理创建 checkout")
        cs = _new_session(self.checkout_proxy)
        cs.headers.update({
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://chatgpt.com/",
        })
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": self.target_country, "currency": self.currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": "custom",
        }
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                r = cs.post("https://chatgpt.com/backend-api/payments/checkout", json=body, timeout=CHATGPT_TIMEOUT)
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
                self._log("checkout", f"checkout 成功: cs_id={cs_id}")
                return {
                    "cs_id": cs_id,
                    "processor_entity": data.get("processor_entity") or ("openai_llc" if self.target_country == "US" else "openai_ie"),
                    "stripe_publishable_key": self.stripe_pk,
                    "billing_country": self.target_country,
                    "currency": self.currency,
                }
            except Exception as e:
                self._log("checkout", f"checkout 第 {attempt} 次失败: {e}")
                if attempt < RETRY_ATTEMPTS:
                    cs = _new_session(rotate_proxy_session(self.checkout_proxy))
                    cs.headers.update({"Authorization": f"Bearer {self.access_token}", "Accept": "application/json", "Content-Type": "application/json"})
                else:
                    raise

    # ─── Stage 2: Stripe init + create PM + confirm (目标国代理) ───────────

    def _stripe_init(self, cs_id: str) -> dict:
        self._log("stripe_init", f"Stage 2: 使用 {self.provider_proxy or 'DIRECT'} 代理 Stripe init")
        stripe = getattr(self, "_stripe_session", None) or _new_session(self.provider_proxy)
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
        if self.require_zero and amount is not None and amount != 0:
            raise Exception(f"要求 0 元但实际金额={amount} {amount_info.get('currency')}")
        # 检查 PayPal 是否可用
        pm_types = init.get("payment_method_types") or []
        if pm_types and "paypal" not in [str(t).lower() for t in pm_types]:
            raise Exception(f"当前 checkout 不支持 PayPal, 可用: {pm_types}")
        return init

    def _create_payment_method(self, cs_id: str) -> str:
        self._log("payment_method", f"创建 PayPal payment_method")
        stripe = getattr(self, "_stripe_session", None) or _new_session(self.provider_proxy)
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
        self._log("confirm", "Stripe confirm")
        stripe = getattr(self, "_stripe_session", None) or _new_session(self.provider_proxy)
        processor_entity = "openai_llc" if self.target_country == "US" else "openai_ie"
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
            raise Exception(f"confirm 失败: {r.status_code} {r.text[:300]}")
        return r.json()

    # ─── Stage 3: Approve (目标国代理) + 轮询 redirect ─────────────────────

    def _chatgpt_approve(self, cs_id: str, processor_entity: str):
        self._log("approve", f"Stage 3: 使用 {self.approve_proxy or 'DIRECT'} 代理 ChatGPT approve")
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

    # ─── 主流程 ────────────────────────────────────────────────────────────

    def extract(self) -> dict:
        """执行完整三段式提链流程。"""
        # Stage 1: Checkout (JP/TH 代理)
        checkout = self._create_checkout()
        cs_id = checkout["cs_id"]
        processor_entity = checkout["processor_entity"]

        # Stage 2: Stripe init + create PM + confirm (目标国代理)
        # 复用同一个 Stripe session 保持 cookies
        self._stripe_session = _new_session(self.provider_proxy)
        init = self._stripe_init(cs_id)
        stripe_hosted_url = str(init.get("stripe_hosted_url") or "")
        self._log("stripe_init", f"stripe_hosted_url={stripe_hosted_url[:80]}...")

        pm_id = self._create_payment_method(cs_id)
        confirm_data = self._stripe_confirm(cs_id, pm_id, init)

        # 尝试从 confirm 提取 redirect URL (仅真正的 PayPal/Stripe 授权链接)
        redirect_url = extract_redirect_url(confirm_data)

        # 如果 confirm 没有返回 redirect，走 approve 流程
        if not redirect_url:
            self._log("approve", "confirm 未返回 redirect，走 ChatGPT approve 流程")
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    self._chatgpt_approve(cs_id, processor_entity)
                    redirect_url = self._poll_payment_page(cs_id, timeout_seconds=45)
                    break
                except Exception as e:
                    self._log("approve", f"approve 第 {attempt} 次失败: {e}")
                    if attempt >= RETRY_ATTEMPTS:
                        # 降级: 返回 stripe_hosted_url
                        if stripe_hosted_url:
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

        return {
            "ok": True,
            "link_type": "paypal_ba_approve",
            "url": redirect_url,
            "ba_token": ba_token,
            "cs_id": cs_id,
            "amount": stripe_amount_details(init).get("amount"),
            "currency": self.currency,
            "target_country": self.target_country,
            "checkout_proxy": self.checkout_proxy,
            "provider_proxy": self.provider_proxy,
            "approve_proxy": self.approve_proxy,
        }


# ─── 批量矩阵 ──────────────────────────────────────────────────────────────────


def run_batch(
    access_token: str,
    proxy_template: str,
    target_countries: list[str] | None = None,
    checkout_countries: list[str] | None = None,
    require_zero: bool = True,
    emit: Any = None,
) -> dict:
    """批量矩阵提链: target x checkout 组合，成功即停。"""
    targets = target_countries or list(DEFAULT_TARGET_COUNTRIES)
    checkouts = checkout_countries or list(DEFAULT_CHECKOUT_COUNTRIES)
    log = emit or (lambda step, msg, **kw: print(f"[{step}] {msg}", file=sys.stderr))

    tasks = [(t, c) for t in targets for c in checkouts]
    log("batch", f"批量任务: {len(tasks)} 个组合, 提取到第一个 BA 链后停止")

    for index, (target, checkout) in enumerate(tasks, 1):
        task_label = f"{target}-{checkout}"
        log("batch", f"任务 {index}/{len(tasks)}: target={target} checkout_proxy={checkout}")

        checkout_proxy = proxy_for_country_template(proxy_template, checkout)
        target_proxy = proxy_for_country_template(proxy_template, target)

        try:
            extractor = PPLinkExtractor(
                access_token=access_token,
                checkout_proxy=checkout_proxy,
                provider_proxy=target_proxy,
                approve_proxy=target_proxy,
                target_country=target,
                require_zero=require_zero,
                emit=log,
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
    parser.add_argument("--proxy-template", default="", help="代理模板 (自动替换国家码)")
    parser.add_argument("--target", default="DE", help="目标国家 (单次模式)")
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
        result = run_batch(token, template, targets, checkouts, require_zero=require_zero, emit=emit)
    else:
        # 单次模式
        checkout_proxy = args.checkout_proxy or args.proxy
        provider_proxy = args.provider_proxy or args.proxy
        approve_proxy = args.approve_proxy or args.proxy
        if not checkout_proxy and not provider_proxy:
            parser.error("请提供代理 (--proxy 或 --checkout-proxy + --provider-proxy)")
        extractor = PPLinkExtractor(
            access_token=token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            approve_proxy=approve_proxy,
            target_country=args.target,
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


def _proxies_from_config(cfg: dict) -> dict:
    """??????????????

    Supports static ``paypal.stage_proxies`` and dynamic plain-text proxy APIs in
    ``paypal.stage_proxy_api_urls``.  API values are resolved at runtime so
    short-lived Cliproxy IP:PORT leases do not get frozen in config.json.
    """
    paypal_cfg = cfg.get("paypal") or {}
    stage_proxies = paypal_cfg.get("stage_proxies") or {}
    api_urls = paypal_cfg.get("stage_proxy_api_urls") or {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""

    checkout = _stage_proxy_value(stage_proxies, api_urls, "checkout", proxy_default)
    provider = (
        _stage_proxy_value(stage_proxies, api_urls, "provider")
        or _stage_proxy_value(stage_proxies, api_urls, "stripe_init")
        or proxy_default
    )
    approve = (
        _stage_proxy_value(stage_proxies, api_urls, "approve")
        or _stage_proxy_value(stage_proxies, api_urls, "confirm")
        or provider
        or proxy_default
    )
    return {"checkout": checkout, "provider": provider, "approve": approve}


def generate_pp_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    approve_proxy: str | None = None,
    target_country: str | None = None,
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
    stage_proxies = _proxies_from_config(cfg)

    # 代理优先级: 明确传入的分段代理 > 单代理 > 配置文件 > 默认
    _checkout = checkout_proxy or proxy or stage_proxies["checkout"]
    _provider = provider_proxy or proxy or stage_proxies["provider"]
    _approve = approve_proxy or proxy or stage_proxies["approve"]

    checkout_proxy = str(_checkout or "").strip()
    provider_proxy = str(_provider or "").strip()
    approve_proxy = str(_approve or "").strip()

    target_country = str(target_country or paypal_cfg.get("target_country") or "GB").upper()
    if require_zero is None:
        require_zero = bool(paypal_cfg.get("require_zero_due", True))
    if require_ba_token is None:
        require_ba_token = bool(paypal_cfg.get("require_ba_token", False))

    # 从 auth_context 提取 email
    email = ""
    if isinstance(auth_context, dict):
        email = str(auth_context.get("email") or "")

    def emit(step: str, msg: str, **kw: Any) -> None:
        print(f"[{step}] {msg}", file=sys.stderr)

    try:
        extractor = PPLinkExtractor(
            access_token=access_token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            approve_proxy=approve_proxy,
            target_country=target_country,
            require_zero=require_zero,
            emit=emit,
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
                "checkout_proxy": result.get("checkout_proxy", ""),
                "provider_proxy": result.get("provider_proxy", ""),
                "approve_proxy": result.get("approve_proxy", ""),
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
            "checkout_proxy": result.get("checkout_proxy", ""),
            "provider_proxy": result.get("provider_proxy", ""),
            "approve_proxy": result.get("approve_proxy", ""),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "url": "",
            "ba_token": "",
        }


def generate_payment_link(
    access_token: str,
    proxy: Any = None,
    payment_method: Any = "paypal",
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
) -> dict[str, Any]:
    """生成支付链接 (兼容旧接口)。

    目前仅支持 PayPal，其他支付方式返回错误。
    """
    method = str(payment_method or "paypal").lower().strip()
    if method != "paypal":
        return {"ok": False, "error": f"不支持的支付方式: {method}，仅支持 paypal"}
    return generate_pp_link(
        access_token=access_token,
        proxy=proxy,
        auth_context=auth_context,
        paypal_generation_type=paypal_generation_type,
    )


if __name__ == "__main__":
    main()
