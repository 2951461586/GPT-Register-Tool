#!/usr/bin/env python3
"""鐢熸垚 ChatGPT Plus PayPal 鎺堟潈閾炬帴锛圫tripe Elements confirm 娴佺▼锛夈€?

瀹屽叏鐙珛瀹炵幇锛屼笉渚濊禆 gopay.py銆?

鐢ㄦ硶锛?
  python3 gen_pp_link.py <access_token>
  python3 gen_pp_link.py --dry-run

娴佺▼锛歝heckout 鈫?stripe init 鈫?create pm (paypal) 鈫?confirm 鈫?鎺堟潈閾炬帴
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
from urllib.parse import quote, urlparse

import requests

# 鍙€?curl_cffi锛圕hrome TLS 鎸囩汗锛?
try:
    from curl_cffi.requests import Session as _CurlCffiSession
except ImportError:
    _CurlCffiSession = None

# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ 甯搁噺 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)

DEFAULT_TIMEOUT = 30

STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)

PAY_URL_RE = re.compile(
    r"^https://(?:pay\.openai\.com|checkout\.stripe\.com)/c/pay/",
    re.IGNORECASE,
)

BILLING_REGIONS = [
    {
        "country": "DE",
        "currency": "EUR",
        "label": "Germany (EUR)",
        "browser_locale": "de-DE",
        "browser_timezone": "Europe/Berlin",
        "stripe_locale": "de",
        "payment_email": "buyer@example.de",
        "address": {
            "country": "DE",
            "line1": "Unter den Linden 1",
            "city": "Berlin",
            "postal_code": "10117",
            "state": "Berlin",
        },
    },
]

REGION_PRESETS = {
    "ID": {
        "country": "ID",
        "currency": "IDR",
        "label": "Indonesia (IDR)",
        "browser_locale": "id-ID",
        "browser_timezone": "Asia/Jakarta",
        "stripe_locale": "id",
        "payment_email": "buyer@example.id",
        "address": {
            "country": "ID",
            "line1": "Jl. M. H. Thamrin No. 1",
            "city": "Jakarta",
            "postal_code": "10310",
            "state": "DKI Jakarta",
        },
    },
    "JP": {
        "country": "JP",
        "currency": "JPY",
        "label": "Japan (JPY)",
        "browser_locale": "ja-JP",
        "browser_timezone": "Asia/Tokyo",
        "stripe_locale": "ja",
        "payment_email": "buyer@example.jp",
        "address": {
            "country": "JP",
            "line1": "1-1-2 Oshiage",
            "city": "Sumida-ku",
            "postal_code": "131-0045",
            "state": "Tokyo",
        },
    },
    "DE": {
        "country": "DE",
        "currency": "EUR",
        "label": "Germany (EUR)",
        "browser_locale": "de-DE",
        "browser_timezone": "Europe/Berlin",
        "stripe_locale": "de",
        "payment_email": "buyer@example.de",
        "address": {
            "country": "DE",
            "line1": "Unter den Linden 1",
            "city": "Berlin",
            "postal_code": "10117",
            "state": "Berlin",
        },
    },
    "FR": {
        "country": "FR",
        "currency": "EUR",
        "label": "France (EUR)",
        "browser_locale": "fr-FR",
        "browser_timezone": "Europe/Paris",
        "stripe_locale": "fr",
        "payment_email": "buyer@example.fr",
        "address": {
            "country": "FR",
            "line1": "10 Rue de Rivoli",
            "city": "Paris",
            "postal_code": "75001",
            "state": "Ile-de-France",
        },
    },
    "GB": {
        "country": "GB",
        "currency": "GBP",
        "label": "United Kingdom (GBP)",
        "browser_locale": "en-GB",
        "browser_timezone": "Europe/London",
        "stripe_locale": "en-GB",
        "payment_email": "buyer@example.co.uk",
        "address": {
            "country": "GB",
            "line1": "10 Downing Street",
            "city": "London",
            "postal_code": "SW1A 2AA",
            "state": "London",
        },
    },
    "IN": {
        "country": "IN",
        "currency": "INR",
        "label": "India (INR)",
        "browser_locale": "en-IN",
        "browser_timezone": "Asia/Kolkata",
        "stripe_locale": "en",
        "payment_email": "buyer@example.in",
        "address": {
            "country": "IN",
            "line1": "Connaught Place 1",
            "city": "New Delhi",
            "postal_code": "110001",
            "state": "Delhi",
        },
    },
    "BR": {
        "country": "BR",
        "currency": "BRL",
        "label": "Brazil (BRL)",
        "browser_locale": "pt-BR",
        "browser_timezone": "America/Sao_Paulo",
        "stripe_locale": "pt-BR",
        "payment_email": "buyer@example.br",
        "address": {
            "country": "BR",
            "line1": "Avenida Paulista 1000",
            "city": "Sao Paulo",
            "postal_code": "01310-100",
            "state": "SP",
        },
    },
    "AU": {
        "country": "AU",
        "currency": "AUD",
        "label": "Australia (AUD)",
        "browser_locale": "en-AU",
        "browser_timezone": "Australia/Sydney",
        "stripe_locale": "en",
        "payment_email": "buyer@example.au",
        "address": {
            "country": "AU",
            "line1": "1 Macquarie Street",
            "city": "Sydney",
            "postal_code": "2000",
            "state": "NSW",
        },
    },
    "US": {
        "country": "US",
        "currency": "USD",
        "label": "United States (USD)",
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Shanghai",
        "stripe_locale": "en",
        "payment_email": "buyer@example.com",
        "address": {
            "country": "US",
            "line1": "3110 Sunset Boulevard",
            "city": "Los Angeles",
            "postal_code": "90026",
            "state": "CA",
        },
    },
}

PAYMENT_METHOD_LABELS = {
    "paypal": "PayPal",
    "gopay": "GoPay",
    "upi": "UPI",
}


def _log_prefix(payment_method: Any = "") -> str:
    return f"[{_normalize_payment_method(payment_method)}]"

# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ Session 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _new_session(impersonate: str = "chrome136") -> Any:
    if _CurlCffiSession is not None:
        return _CurlCffiSession(impersonate=impersonate)
    return requests.Session()


def _auth_context_value(auth_context: Any, *keys: str) -> str:
    cur = auth_context
    for key in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return str(cur or "").strip()


def _cookie_names(cookie_header: str) -> set[str]:
    names: set[str] = set()
    for part in str(cookie_header or "").split(";"):
        name = part.strip().split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _build_chatgpt_cookie_header(device_id: str, auth_context: Any) -> str:
    cookie_header = _auth_context_value(auth_context, "cookie_header")
    session_token = (
        _auth_context_value(auth_context, "session_token")
        or _auth_context_value(auth_context, "sessionToken")
        or _auth_context_value(auth_context, "auth_session", "sessionToken")
        or _auth_context_value(auth_context, "auth_session", "session_token")
    )
    parts = [part.strip() for part in cookie_header.split(";") if part.strip()]
    names = _cookie_names(cookie_header)
    if device_id and "oai-did" not in names:
        parts.append(f"oai-did={device_id}")
    if session_token and "__Secure-next-auth.session-token" not in names:
        parts.append(f"__Secure-next-auth.session-token={session_token}")
    if not parts:
        parts.append(f"oai-did={device_id}")
    return "; ".join(parts)


def _build_chatgpt_session(access_token: str, auth_context: Any | None = None) -> Any:
    auth_context = auth_context if isinstance(auth_context, dict) else {}
    device_id = (
        _auth_context_value(auth_context, "device_id")
        or _auth_context_value(auth_context, "oai_device_id")
        or str(uuid.uuid4())
    )
    user_agent = str(auth_context.get("user_agent") or auth_context.get("ua") or "").strip() or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    )
    s = _new_session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "zh-CN",
        "sec-ch-ua": '"Google Chrome";v="148", "Not.A/Brand";v="8", "Chromium";v="148"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    if access_token:
        s.headers["Authorization"] = f"Bearer {access_token}"
    s.headers["Cookie"] = _build_chatgpt_cookie_header(device_id, auth_context)
    return s


def _normalize_proxy(proxy: Any) -> str:
    value = str(proxy or "").strip()
    if value.lower() in ("", "none", "null", "direct", "no_proxy", "nopoxy"):
        return ""
    return value


def _set_session_proxy(session: Any, proxy: str):
    proxy = _normalize_proxy(proxy)
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}


def _stage_proxy(paypal_cfg: dict[str, Any], stage: str, fallback_proxy: str, force_fallback: bool = False) -> str:
    if force_fallback:
        return _normalize_proxy(fallback_proxy)
    stages = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    fallback = _normalize_proxy(stages.get("default") or fallback_proxy)
    return _normalize_proxy(stages.get(stage, fallback))


def _proxy_candidates(paypal_cfg: dict[str, Any], default_proxy: str, explicit_proxy: Any = None) -> tuple[list[str], bool]:
    explicit_supplied = explicit_proxy is not None and str(explicit_proxy).strip() != ""
    if explicit_supplied:
        force_stage_proxy = bool(paypal_cfg.get("explicit_proxy_overrides_stage_proxies", False))
        return [_normalize_proxy(explicit_proxy)], force_stage_proxy

    raw_proxies = paypal_cfg.get("proxies")
    if raw_proxies is None or raw_proxies == "":
        raw_proxies = [default_proxy]
    elif not isinstance(raw_proxies, list):
        raw_proxies = [raw_proxies]

    proxies = [_normalize_proxy(item) for item in raw_proxies]
    return proxies or [_normalize_proxy(default_proxy)], False


def _normalize_payment_method(value: Any) -> str:
    method = str(value or "").strip().lower()
    if method in {"gopay", "go-pay", "go_pay"}:
        return "gopay"
    if method in {"upi", "upiqr", "upi_qr", "upi-qr"}:
        return "upi"
    return "paypal"


def _payment_cfg(cfg: dict[str, Any], payment_method: str) -> dict[str, Any]:
    payment_method = _normalize_payment_method(payment_method)
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    if payment_method == "paypal":
        return _apply_paypal_generation_type(paypal_cfg)
    method_cfg = cfg.get(payment_method) if isinstance(cfg.get(payment_method), dict) else {}
    merged = dict(paypal_cfg)
    merged.update(method_cfg)
    if not (method_cfg.get("billing_regions") or method_cfg.get("billing_region") or method_cfg.get("billing_country")):
        merged["billing_regions"] = ["IN"] if payment_method == "upi" else ["ID"]
    if payment_method == "upi":
        if not method_cfg.get("checkout_ui_mode"):
            merged["checkout_ui_mode"] = "hosted"
        if not method_cfg.get("link_mode"):
            merged["link_mode"] = "chatgpt_checkout"
    return merged


def _paypal_generation_type(payment_cfg: dict[str, Any]) -> str:
    raw = str(
        payment_cfg.get("link_generation_type")
        or payment_cfg.get("generation_type")
        or payment_cfg.get("paypal_generation_type")
        or ""
    ).strip().lower().replace("-", "_")
    if raw in {
        "pp_direct_zero_due",
        "paypal_direct_zero_due",
        "direct_pp_zero_due",
        "paypal_approve_zero_due",
        "ba_direct_zero_due",
        "ba_approve_zero_due",
        "pp_direct_0_due",
        "paypal_direct_0_due",
        "pp_direct_force_zero",
        "paypal_direct_force_zero",
        "paypal_direct_require_zero_due",
    }:
        return "paypal_direct_zero_due"
    if raw in {"pp_direct", "paypal_direct", "direct_pp", "paypal_approve", "ba_direct", "ba_approve"}:
        return "paypal_direct"
    if raw in {
        "gpt_pp",
        "gpt_pp_core",
        "gpt_pp_protocol",
        "gpt_pp_paypal",
        "gpt_pp_paypal_authorize",
        "gpt_pp_longlink",
        "gptpp",
        "pp_gateway",
        "plus_paypal_gateway",
    }:
        return "gpt_pp_core"
    if raw in {"long", "long_link", "hosted", "hosted_long", "hosted_long_url", "stripe_hosted", "chatgpt_checkout"}:
        return "hosted_long_url"
    return ""


def _apply_paypal_generation_type(payment_cfg: dict[str, Any]) -> dict[str, Any]:
    generation_type = _paypal_generation_type(payment_cfg)
    if not generation_type:
        return payment_cfg
    patched = dict(payment_cfg or {})
    patched["link_generation_type"] = generation_type
    if generation_type == "gpt_pp_core":
        patched["checkout_only_long_url"] = False
        patched["stop_after_pm_create"] = False
        patched.setdefault("checkout_ui_mode", "hosted")
        patched["link_mode"] = "stripe_redirect"
        patched["redirect_url_format"] = "stripe_authorize"
        patched["resolve_ba_redirect"] = False
        patched["require_ba_token"] = False
        patched["approve_missing_redirect"] = False
        patched.setdefault("require_zero_due", False)
        return patched
    if generation_type in {"paypal_direct", "paypal_direct_zero_due"}:
        patched["checkout_only_long_url"] = False
        patched["stop_after_pm_create"] = False
        patched["checkout_ui_mode"] = "custom"
        patched["link_mode"] = "stripe_redirect"
        patched["redirect_url_format"] = "any"
        patched["confirm_style"] = "payment_method_id"
        patched["resolve_ba_redirect"] = True
        patched["require_ba_token"] = True
        patched["approve_missing_redirect"] = True
        patched["require_zero_due"] = generation_type == "paypal_direct_zero_due"
        return patched
    patched["checkout_only_long_url"] = False
    patched["stop_after_pm_create"] = False
    patched["checkout_ui_mode"] = "hosted"
    patched["link_mode"] = "chatgpt_checkout"
    patched["redirect_url_format"] = "any"
    patched["confirm_style"] = "inline_payment_method_data"
    patched["resolve_ba_redirect"] = False
    patched["require_ba_token"] = False
    patched["approve_missing_redirect"] = False
    patched.setdefault("require_zero_due", True)
    return patched


def _checkout_ui_mode(payment_cfg: dict[str, Any]) -> str:
    mode = str(payment_cfg.get("checkout_ui_mode") or "custom").strip().lower()
    return mode or "custom"


def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default



def _reference_confirm_mode(payment_cfg: dict[str, Any], payment_method: str) -> bool:
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    return _config_bool(
        payment_cfg.get("reference_confirm_mode", payment_cfg.get("fast_reference_confirm")),
        False,
    )


def _reference_confirm_cfg(payment_cfg: dict[str, Any]) -> dict[str, Any]:
    """Match the external gen_pp_link.py fast Stripe-confirm flow.

    This mode intentionally skips the slow page warmup/snapshot/tax/elements/approve
    helpers and relies on Stripe init -> PM create -> confirm redirect only.
    """
    patched = dict(payment_cfg or {})
    patched["reference_confirm_mode"] = True
    patched["checkout_only_long_url"] = False
    patched["stop_after_pm_create"] = False
    patched["checkout_ui_mode"] = "hosted"
    patched["link_mode"] = "stripe_redirect"
    patched["confirm_style"] = "payment_method_id"
    patched["use_elements_session"] = False
    patched["refresh_tax_region"] = False
    patched["approve_missing_redirect"] = False
    patched["resolve_ba_redirect"] = False
    patched["require_ba_token"] = False
    return patched

def _stop_after_pm_create(payment_cfg: dict[str, Any], payment_method: str) -> bool:
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    return _config_bool(payment_cfg.get("stop_after_pm_create"), False)


def _checkout_only_long_url(payment_cfg: dict[str, Any], payment_method: str) -> bool:
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    return _config_bool(payment_cfg.get("checkout_only_long_url"), False)


def _hosted_long_url_cfg(payment_cfg: dict[str, Any]) -> dict[str, Any]:
    patched = dict(payment_cfg or {})
    patched["checkout_ui_mode"] = "hosted"
    patched["link_mode"] = "chatgpt_checkout"
    patched["stop_after_pm_create"] = False
    patched["resolve_ba_redirect"] = False
    patched["require_ba_token"] = False
    patched["approve_missing_redirect"] = False
    # User explicitly wants the order/billing region to stay Germany.
    patched["billing_regions"] = ["DE"]
    return patched


def _pm_create_only_cfg(payment_cfg: dict[str, Any]) -> dict[str, Any]:
    patched = dict(payment_cfg or {})
    patched["checkout_ui_mode"] = "custom"
    patched["link_mode"] = "stripe_redirect"
    patched["confirm_style"] = "payment_method_id"
    patched["resolve_ba_redirect"] = False
    patched["require_ba_token"] = False
    patched["approve_missing_redirect"] = False
    return patched


def _payment_link_mode(payment_cfg: dict[str, Any], payment_method: str) -> str:
    payment_method = _normalize_payment_method(payment_method)
    if payment_method != "paypal":
        raw = payment_cfg.get("link_mode") or ("chatgpt_checkout" if payment_method == "upi" else "stripe_redirect")
        mode = str(raw or "").strip().lower().replace("-", "_")
        if mode in {"hosted", "hosted_long_url", "long_url", "checkout_long_url", "chatgpt_checkout"}:
            return "chatgpt_checkout"
        return "stripe_redirect"
    raw = (
        payment_cfg.get("link_mode")
        or payment_cfg.get("paypal_link_mode")
        or "chatgpt_checkout"
    )
    mode = str(raw or "").strip().lower().replace("-", "_")
    if mode in {"ba", "ba_redirect", "paypal_approve"}:
        return "ba_redirect"
    if mode in {"stripe", "stripe_confirm", "stripe_redirect", "paypal_redirect", "legacy"}:
        return "stripe_redirect"
    if mode in {"hosted", "hosted_long_url", "long_url", "checkout_long_url", "chatgpt_checkout"}:
        return "chatgpt_checkout"
    return "chatgpt_checkout"


def _promo_campaign_id(payment_cfg: dict[str, Any]) -> str:
    if payment_cfg.get("use_promo_campaign") is False:
        return ""
    raw = payment_cfg.get("promo_campaign_id", "plus-1-month-free")
    value = str(raw or "").strip()
    if value.lower() in {"", "none", "null", "false", "off", "direct"}:
        return ""
    return value


def _checkout_body(payment_cfg: dict[str, Any], region: dict[str, Any], promo_campaign_id: str) -> dict[str, Any]:
    checkout_ui_mode = _checkout_ui_mode(payment_cfg)
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": region["country"],
            "currency": region["currency"],
        },
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": checkout_ui_mode,
    }
    if promo_campaign_id:
        body["promo_campaign"] = {
            "promo_campaign_id": promo_campaign_id,
            "is_coupon_from_query_param": False,
        }
    return body


def _billing_regions(paypal_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    raw = paypal_cfg.get("billing_regions") or paypal_cfg.get("billing_region") or paypal_cfg.get("billing_country")
    if raw is None or raw == "":
        return [dict(region) for region in BILLING_REGIONS]
    if not isinstance(raw, list):
        raw = [raw]

    regions: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            code = item.strip().upper()
            if not code:
                continue
            region = dict(REGION_PRESETS.get(code, {}))
            if not region:
                region = {"country": code, "currency": "USD", "label": code}
        elif isinstance(item, dict):
            country = str(item.get("country") or item.get("code") or "").strip().upper()
            region = dict(REGION_PRESETS.get(country, {}))
            region.update(item)
            if country:
                region["country"] = country
        else:
            continue

        country = str(region.get("country") or "").strip().upper()
        if not country:
            continue
        region["country"] = country
        region["currency"] = str(region.get("currency") or "USD").strip().upper()
        region["label"] = str(region.get("label") or f"{country} ({region['currency']})")
        address = region.get("address") if isinstance(region.get("address"), dict) else {}
        preset_address = REGION_PRESETS.get(country, {}).get("address") or {}
        region["address"] = {**preset_address, **address, "country": country}
        regions.append(region)

    return regions or [dict(region) for region in BILLING_REGIONS]


def _stripe_error_details(response: Any) -> dict[str, Any]:
    details: dict[str, Any] = {"status": getattr(response, "status_code", None)}
    body: Any = None
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        for key in ("code", "decline_code", "type", "message", "param", "doc_url", "request_log_url"):
            value = error.get(key)
            if value:
                details[key] = value
        payment_method = error.get("payment_method") if isinstance(error.get("payment_method"), dict) else {}
        if payment_method.get("id"):
            details["payment_method_id"] = payment_method["id"]
        setup_intent = error.get("setup_intent") if isinstance(error.get("setup_intent"), dict) else {}
        if setup_intent.get("id"):
            details["setup_intent_id"] = setup_intent["id"]
    text = str(getattr(response, "text", "") or "")
    if text:
        details["raw"] = text[:500]
    return details


def _post_stripe_form(session: Any, url: str, body: dict[str, Any], *, timeout: int, step: str) -> Any:
    current_body = dict(body)
    removed_params: list[str] = []
    while True:
        response = session.post(url, data=current_body, timeout=timeout)
        details = _stripe_error_details(response)
        unknown_param = str(details.get("param") or "")
        if response.status_code == 400 and details.get("code") == "parameter_unknown" and unknown_param in current_body:
            removed_params.append(unknown_param)
            current_body.pop(unknown_param, None)
            print(f"[stripe] {step}: retry without unknown param {unknown_param}", file=sys.stderr)
            continue
        if removed_params:
            response.removed_unknown_params = removed_params
        return response


def _get_stripe_params(session: Any, url: str, params: dict[str, str], *, timeout: int, step: str) -> Any:
    current_params = dict(params)
    removed_params: list[str] = []
    while True:
        response = session.get(url, params=current_params, timeout=timeout)
        details = _stripe_error_details(response)
        unknown_param = str(details.get("param") or "")
        if response.status_code == 400 and details.get("code") == "parameter_unknown" and unknown_param in current_params:
            removed_params.append(unknown_param)
            current_params.pop(unknown_param, None)
            params.pop(unknown_param, None)
            print(f"[stripe] {step}: retry without unknown param {unknown_param}", file=sys.stderr)
            continue
        if removed_params:
            response.removed_unknown_params = removed_params
        return response


def _expected_amount_from_init(init_data: dict[str, Any]) -> str:
    for path in (
        ("invoice", "amount_due"),
        ("total_summary", "due"),
        ("invoice", "total"),
        ("total_summary", "total"),
    ):
        amount = _amount_to_int(_amount_at(init_data, *path))
        if amount is not None:
            return str(amount)
    return "0"


def _payment_method_types_from_init(init_data: dict[str, Any]) -> list[str]:
    values = init_data.get("payment_method_types") or []
    if not isinstance(values, list):
        values = [values]
    return [str(item).strip().lower() for item in values if str(item or "").strip()]


def _elements_session_params(
    init_data: dict[str, Any],
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    stripe_locale: str,
) -> list[tuple[str, str]]:
    currency = str(
        init_data.get("currency")
        or _amount_at(init_data, "invoice", "currency")
        or ""
    ).strip().lower()
    pm_types = _payment_method_types_from_init(init_data)
    params: list[tuple[str, str]] = [
        ("client_betas[0]", "custom_checkout_server_updates_1"),
        ("client_betas[1]", "custom_checkout_manual_approval_1"),
        ("deferred_intent[mode]", str(init_data.get("mode") or "subscription")),
        ("deferred_intent[amount]", _expected_amount_from_init(init_data)),
    ]
    if currency:
        params.append(("deferred_intent[currency]", currency))
    params.append(("deferred_intent[setup_future_usage]", "off_session"))
    for index, pm_type in enumerate(pm_types):
        params.append((f"deferred_intent[payment_method_types][{index}]", pm_type))
    if currency:
        params.append(("currency", currency))
    params.extend([
        ("key", stripe_pk),
        ("_stripe_version", STRIPE_VERSION),
        ("elements_init_source", "custom_checkout"),
        ("referrer_host", "chatgpt.com"),
        ("stripe_js_id", stripe_js_id),
        ("locale", str(stripe_locale or "en").strip().lower() or "en"),
        ("type", "deferred_intent"),
        ("checkout_session_id", cs_id),
    ])
    return params


def _get_elements_session(
    session: Any,
    init_data: dict[str, Any],
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    stripe_locale: str,
    timeout: int,
) -> tuple[str, dict[str, Any], Any]:
    params = _elements_session_params(
        init_data,
        cs_id=cs_id,
        stripe_pk=stripe_pk,
        stripe_js_id=stripe_js_id,
        stripe_locale=stripe_locale,
    )
    response = session.get(
        "https://api.stripe.com/v1/elements/sessions",
        params=params,
        timeout=timeout,
    )
    if getattr(response, "status_code", None) != 200:
        return "", {}, response
    data = response.json() or {}
    session_id = str(data.get("session_id") or "").strip()
    return session_id, data if isinstance(data, dict) else {}, response


def _checkout_data_url(checkout_url: str) -> str:
    checkout_url = str(checkout_url or "").strip()
    if not checkout_url:
        return ""
    base = checkout_url.split("?", 1)[0].rstrip("/")
    if base.endswith(".data"):
        return base
    return f"{base}.data"


def _with_referer(session: Any, referer: str):
    class _RefererGuard:
        def __enter__(self_inner):
            self_inner.old = None
            self_inner.had = False
            headers = getattr(session, "headers", None)
            if isinstance(headers, dict) and referer:
                self_inner.had = "Referer" in headers
                self_inner.old = headers.get("Referer")
                headers["Referer"] = referer
            return session

        def __exit__(self_inner, exc_type, exc, tb):
            headers = getattr(session, "headers", None)
            if isinstance(headers, dict) and referer:
                if self_inner.had:
                    headers["Referer"] = self_inner.old
                else:
                    headers.pop("Referer", None)
            return False

    return _RefererGuard()




def _post_empty_chatgpt(session: Any, url: str, *, timeout: int):
    try:
        return session.post(url, data="", timeout=timeout)
    except TypeError as exc:
        if "data" not in str(exc):
            raise
        # Unit-test fakes and older session wrappers may only accept json=.
        # Keep the logical request empty instead of sending the old JSON body.
        return session.post(url, json=None, timeout=timeout)

def _chatgpt_load_checkout_route(session: Any, *, checkout_url: str, log_prefix: str) -> dict[str, Any]:
    data_url = _checkout_data_url(checkout_url)
    if not data_url or not hasattr(session, "get"):
        return {"ok": False, "skipped": True}
    page_status = None
    data_status = None
    attempts: list[dict[str, Any]] = []
    try:
        with _with_referer(session, "https://chatgpt.com/"):
            page_response = session.get(checkout_url, timeout=DEFAULT_TIMEOUT)
        page_status = getattr(page_response, "status_code", None)
    except Exception as exc:
        print(f"{log_prefix} checkout page load skipped: {exc}", file=sys.stderr)
    deadline = time.time() + 8
    while True:
        try:
            with _with_referer(session, checkout_url or "https://chatgpt.com/"):
                response = session.get(
                    data_url,
                    params={"_routes": "routes/checkout.$entity.$checkoutId"},
                    timeout=DEFAULT_TIMEOUT,
                )
            data_status = getattr(response, "status_code", None)
            attempts.append({"status": data_status})
            if data_status == 200 or time.time() >= deadline or data_status not in (202, 425, 429):
                return {
                    "ok": data_status == 200,
                    "status": data_status,
                    "page_status": page_status,
                    "attempts": attempts,
                }
            time.sleep(0.8)
        except Exception as exc:
            print(f"{log_prefix} checkout route load skipped: {exc}", file=sys.stderr)
            return {"ok": False, "error": str(exc), "page_status": page_status, "attempts": attempts}


def _chatgpt_checkout_snapshot(
    session: Any,
    *,
    checkout_url: str,
    cs_id: str = "",
    processor_entity: str = "",
    log_prefix: str,
) -> dict[str, Any]:
    url = "https://chatgpt.com/backend-api/payments/checkout/snapshot"
    attempts: list[dict[str, Any]] = []
    snapshot_candidates: list[tuple[str, dict[str, Any] | None]] = [
        ("empty", None),
        ("empty_json", {}),
    ]
    if cs_id and processor_entity:
        # HAR body length for snapshot is 147 bytes, matching the compact
        # JSON below with payment_method=card. The UI snapshots the default
        # card payment element before the later PayPal confirm.
        snapshot_candidates.extend([
            ("checkout_card_json", {
                "checkout_session_id": cs_id,
                "processor_entity": processor_entity,
                "payment_method": "card",
            }),
            ("checkout_paypal_json", {
                "checkout_session_id": cs_id,
                "processor_entity": processor_entity,
                "payment_method": "paypal",
            }),
            ("checkout_json", {
                "checkout_session_id": cs_id,
                "processor_entity": processor_entity,
            }),
        ])
    deadline = time.time() + 8
    try:
        while True:
            status = None
            mode = ""
            for mode, payload in snapshot_candidates:
                with _with_referer(session, checkout_url):
                    response = (
                        _post_empty_chatgpt(session, url, timeout=DEFAULT_TIMEOUT)
                        if payload is None
                        else session.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
                    )
                status = getattr(response, "status_code", None)
                attempts.append({"mode": mode, "status": status})
                if status in (200, 204):
                    return {"ok": True, "status": status, "mode": mode, "attempts": attempts}
            if time.time() >= deadline or status not in (202, 409, 422, 425, 429):
                return {"ok": False, "status": status, "mode": mode, "attempts": attempts}
            time.sleep(0.8)
    except Exception as exc:
        print(f"{log_prefix} checkout snapshot skipped: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc), "attempts": attempts}


def _chatgpt_approve_checkout(
    session: Any,
    *,
    cs_id: str,
    processor_entity: str,
    log_prefix: str,
    checkout_url: str = "",
) -> dict[str, Any]:
    try:
        with _with_referer(session, checkout_url):
            session.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={},
                timeout=DEFAULT_TIMEOUT,
            )
    except Exception as exc:
        print(f"{log_prefix} sentinel/ping skipped: {exc}", file=sys.stderr)
    try:
        # Browser HAR shows checkout/approve is an empty POST scoped by the
        # checkout-page Referer. Sending checkout_session_id/processor_entity as
        # JSON can make ChatGPT return result=blocked even after Stripe confirm
        # succeeds, so keep this call browser-compatible.
        approve_url = "https://chatgpt.com/backend-api/payments/checkout/approve"
        attempts: list[dict[str, Any]] = []
        with _with_referer(session, checkout_url):
            response = _post_empty_chatgpt(session, approve_url, timeout=DEFAULT_TIMEOUT)
        status = getattr(response, "status_code", None)
        attempts.append({"mode": "empty", "status": status})
        if status in (400, 415, 422):
            with _with_referer(session, checkout_url):
                response = session.post(approve_url, json={}, timeout=DEFAULT_TIMEOUT)
            status = getattr(response, "status_code", None)
            attempts.append({"mode": "empty_json", "status": status})
        if status in (400, 415, 422):
            with _with_referer(session, checkout_url):
                response = session.post(
                    approve_url,
                    json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
                    timeout=DEFAULT_TIMEOUT,
                )
            status = getattr(response, "status_code", None)
            attempts.append({"mode": "checkout_json", "status": status})
        if status != 200:
            return {
                "ok": False,
                "status": status,
                "attempts": attempts,
                "error": str(getattr(response, "text", "") or "")[:300],
            }
        try:
            data = response.json() or {}
        except Exception:
            data = {}
        result = data.get("result") if isinstance(data, dict) else ""
        if not result and not str(getattr(response, "text", "") or "").strip():
            result = "approved_empty_response"
        return {
            "ok": result in ("approved", "approved_empty_response"),
            "status": status,
            "result": result,
            "response_keys": sorted(str(key) for key in data.keys())[:20] if isinstance(data, dict) else [],
            "attempts": attempts,
            "error": str(data.get("error") or data.get("message") or "")[:300] if isinstance(data, dict) else "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _payment_page_poll_params(
    *,
    elements_session_id: str,
    stripe_js_id: str,
    stripe_locale: str,
    stripe_pk: str,
) -> dict[str, str]:
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": str(stripe_locale or "en").strip().lower() or "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[stripe_js_locale]": "auto",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
    }


def _poll_payment_page_redirect_url(
    session: Any,
    *,
    cs_id: str,
    elements_session_id: str,
    stripe_js_id: str,
    stripe_locale: str,
    stripe_pk: str,
    payment_method: str,
    redirect_format: str,
    timeout_seconds: float,
    poll_interval: float,
) -> tuple[str, dict[str, Any]]:
    deadline = time.time() + max(1.0, timeout_seconds)
    params = _payment_page_poll_params(
        elements_session_id=elements_session_id,
        stripe_js_id=stripe_js_id,
        stripe_locale=stripe_locale,
        stripe_pk=stripe_pk,
    )
    attempts = 0
    last_summary: dict[str, Any] = {}
    while time.time() < deadline:
        attempts += 1
        try:
            response = _get_stripe_params(
                session,
                f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                params=params,
                timeout=DEFAULT_TIMEOUT,
                step="payment page poll",
            )
        except Exception as exc:
            last_summary = {"attempts": attempts, "error": str(exc)}
            time.sleep(max(0.2, poll_interval))
            continue
        status = getattr(response, "status_code", None)
        if status == 200:
            payload = response.json() or {}
            redirect_url = _find_payment_redirect_url(
                payload,
                payment_method,
                redirect_format=redirect_format,
            )
            if redirect_url:
                return redirect_url, {"attempts": attempts, "status": status}
            last_summary = {
                "attempts": attempts,
                "status": status,
                "confirm_summary": _confirm_summary(payload),
            }
        else:
            last_summary = {
                "attempts": attempts,
                "status": status,
                "error": str(getattr(response, "text", "") or "")[:240],
            }
        time.sleep(max(0.2, poll_interval))
    return "", last_summary


def _is_terminal_confirm_decline(details: dict[str, Any]) -> bool:
    if details.get("status") != 402:
        return False
    if details.get("decline_code"):
        return True
    return details.get("code") in {"setup_attempt_failed", "payment_intent_payment_attempt_failed"}


def _should_retry_without_promo(result: dict[str, Any], payment_method: str, payment_cfg: dict[str, Any]) -> bool:
    if payment_method != "paypal":
        return False
    if payment_cfg.get("disable_promo_on_confirm_decline") is False:
        return False
    return (
        bool(result.get("terminal"))
        and result.get("error_code") == "stripe_confirm_declined"
        and bool(result.get("zero_due_verified"))
        and bool(result.get("promo_campaign_id"))
    )


def _paypal_redirect_format(payment_cfg: dict[str, Any]) -> str:
    raw = str(payment_cfg.get("redirect_url_format") or "stripe_authorize").strip().lower().replace("-", "_")
    if raw in {"any", "legacy", "all"}:
        return "any"
    if raw in {"paypal", "paypal_approve", "ba", "ba_approve"}:
        return "paypal_approve"
    if raw in {"pm_redirect", "stripe", "stripe_authorize", "stripe_redirect", "stripe_pm_redirect"}:
        return "stripe_authorize"
    return "stripe_authorize"


def _paypal_confirm_style(payment_cfg: dict[str, Any]) -> str:
    raw = str(payment_cfg.get("confirm_style") or payment_cfg.get("paypal_confirm_style") or "payment_method_id")
    raw = raw.strip().lower().replace("-", "_")
    if raw in {"inline", "inline_payment_method", "inline_payment_method_data", "browser"}:
        return "inline_payment_method_data"
    return "payment_method_id"


def _is_payment_redirect_url(value: Any, payment_method: str, redirect_format: str = "any") -> bool:
    text = str(value or "")
    if not text.startswith(("http://", "https://")):
        return False
    lower = text.lower()
    if payment_method == "paypal":
        parsed = urlparse(text)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        is_stripe_authorize = host == "pm-redirects.stripe.com" and path.startswith("/authorize/")
        is_paypal_approve = host.endswith("paypal.com") and path.startswith("/agreements/approve")
        if redirect_format == "stripe_authorize":
            return is_stripe_authorize
        if redirect_format == "paypal_approve":
            return is_paypal_approve and "ba_token=" in lower
        return (
            is_stripe_authorize
            or (is_paypal_approve and "ba_token=" in lower)
            or ("paypal" in lower and "redirect" in lower)
        )
    if payment_method == "gopay":
        return "gopay" in lower or "midtrans" in lower
    if payment_method == "upi":
        return _is_hosted_checkout_url(text) or "upi" in lower or "india" in lower or "npci" in lower
    return False


def _find_payment_redirect_url(value: Any, payment_method: str, redirect_format: str = "any") -> str:
    if _is_payment_redirect_url(value, payment_method, redirect_format=redirect_format):
        return str(value)
    if isinstance(value, dict):
        for key in ("url", "redirect_url", "return_url"):
            if key in value and _is_payment_redirect_url(value.get(key), payment_method, redirect_format=redirect_format):
                return str(value[key])
        for child in value.values():
            found = _find_payment_redirect_url(child, payment_method, redirect_format=redirect_format)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_payment_redirect_url(child, payment_method, redirect_format=redirect_format)
            if found:
                return found
    return ""


def _paypal_ba_token(value: str) -> str:
    text = str(value or "")
    marker = "ba_token="
    lower = text.lower()
    if marker not in lower:
        return ""
    start = lower.find(marker) + len(marker)
    end = len(text)
    for sep in ("&", "#"):
        pos = text.find(sep, start)
        if pos != -1:
            end = min(end, pos)
    return text[start:end]


def _resolve_paypal_approve_url(redirect_url: str, proxy: str = "", log_prefix: str = "[paypal]") -> tuple[str, dict[str, Any]]:
    if _paypal_ba_token(redirect_url):
        return redirect_url, {"ok": True, "already_resolved": True}
    try:
        from .paypal_nocard import _follow_stripe_redirect
        resolved = _follow_stripe_redirect(
            redirect_url,
            proxy=proxy or None,
            log=lambda message: print(f"{log_prefix} resolve BA: {message}", file=sys.stderr),
        )
    except Exception as exc:
        return redirect_url, {"ok": False, "error": str(exc)}
    return resolved or redirect_url, {
        "ok": bool(_paypal_ba_token(resolved)),
        "resolved": bool(resolved and resolved != redirect_url),
        "has_ba_token": bool(_paypal_ba_token(resolved)),
    }


def _intent_summary(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, dict):
        return {}
    next_action = intent.get("next_action") if isinstance(intent.get("next_action"), dict) else {}
    last_error = intent.get("last_setup_error") or intent.get("last_payment_error")
    if not isinstance(last_error, dict):
        last_error = {}
    return {
        "id_prefix": str(intent.get("id") or "")[:8],
        "status": intent.get("status") or "",
        "next_action_type": next_action.get("type") or "",
        "last_error_code": last_error.get("code") or "",
        "last_error_decline_code": last_error.get("decline_code") or "",
        "last_error_message": str(last_error.get("message") or "")[:180],
    }


def _confirm_summary(confirm_data: Any) -> dict[str, Any]:
    if not isinstance(confirm_data, dict):
        return {"response_type": type(confirm_data).__name__}
    return {
        "top_level_keys": sorted(str(key) for key in confirm_data.keys())[:30],
        "status": confirm_data.get("status") or "",
        "mode": confirm_data.get("mode") or "",
        "setup_intent": _intent_summary(confirm_data.get("setup_intent")),
        "payment_intent": _intent_summary(confirm_data.get("payment_intent")),
    }


def _stripe_step_error_result(
    response: Any,
    *,
    step: str,
    error_code: str,
    region: dict,
    proxy: str,
    stage_proxy: str,
    cs_id: str = "",
    terminal: bool | None = None,
) -> dict[str, Any]:
    details = _stripe_error_details(response)
    status = int(details.get("status") or 0)
    is_terminal = terminal if terminal is not None else 400 <= status < 500 and status != 429
    reason = details.get("code") or details.get("type") or "unknown"
    message = details.get("message") or str(getattr(response, "text", "") or "")[:180]
    print(
        f"[stripe] {step} failed: status={status} reason={reason} "
        f"param={details.get('param', '')} message={message}",
        file=sys.stderr,
    )
    return {
        "ok": False,
        "error": f"Stripe {step} failed: status={status} reason={reason} message={message}",
        "error_code": error_code,
        "terminal": is_terminal,
        "retryable": not is_terminal,
        "stripe_error": details,
        "cs_id": cs_id,
        "region": region["label"],
        "proxy": proxy,
        "stage_proxy": stage_proxy or "DIRECT",
    }


def _stripe_confirm_error_result(
    response: Any,
    *,
    region: dict,
    proxy: str,
    checkout_proxy: str,
    stripe_init_proxy: str,
    stripe_pm_proxy: str,
    stripe_confirm_proxy: str,
    cs_id: str,
    pm_id: str,
    due: Any,
    amount_due: Any,
    currency: str,
    expected_amount: str,
    zero_check: dict[str, Any],
    pm_types: list[Any],
    has_paypal: bool,
    has_upi: bool,
    promo_campaign_id: str,
    checkout_ui_mode: str,
) -> dict[str, Any]:
    details = _stripe_error_details(response)
    terminal = _is_terminal_confirm_decline(details)
    reason = details.get("decline_code") or details.get("code") or "unknown"
    message = details.get("message") or str(getattr(response, "text", "") or "")[:180]
    return {
        "ok": False,
        "error": f"Stripe confirm declined: status={details.get('status')} reason={reason} message={message}",
        "error_code": "stripe_confirm_declined",
        "terminal": terminal,
        "retryable": not terminal,
        "stripe_error": details,
        "cs_id": cs_id,
        "pm_id": pm_id,
        "due": due,
        "amount_due": amount_due,
        "currency": currency,
        "expected_amount": expected_amount,
        "zero_due_verified": bool(zero_check.get("ok")),
        "tax_after_zero": zero_check.get("tax_after_zero"),
        "zero_due_amounts": zero_check.get("amounts"),
        "tax_amounts": zero_check.get("tax_amounts"),
        "payment_method_types": pm_types,
        "has_paypal": has_paypal,
        "has_upi": has_upi,
        "promo_campaign_id": promo_campaign_id,
        "checkout_ui_mode": checkout_ui_mode,
        "region": region["label"],
        "proxy": proxy,
        "stage_proxies": {
            "checkout": checkout_proxy or "DIRECT",
            "stripe_init": stripe_init_proxy or "DIRECT",
            "payment_method": stripe_pm_proxy or "DIRECT",
            "confirm": stripe_confirm_proxy or "DIRECT",
        },
    }


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ Token 瑙ｆ瀽 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def parse_token(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith("{"):
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        for key in ("accessToken", "access_token"):
            tok = data.get(key)
            if isinstance(tok, str) and tok.startswith("eyJ"):
                return tok
        return None
    if value.startswith("eyJ") and value.count(".") == 2:
        return value
    return None


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ 杈呭姪鍑芥暟 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _bfs_find_pay_url(payload: Any) -> str:
    queue = [payload]
    seen = 0
    while queue and seen < 5000:
        seen += 1
        cur = queue.pop(0)
        if isinstance(cur, str):
            value = cur.strip()
            if PAY_URL_RE.match(value):
                return value
            continue
        if isinstance(cur, dict):
            queue.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            queue.extend(cur)
    return ""


def _checkout_response_hosted_url(data: dict[str, Any]) -> str:
    """Extract hosted checkout URL using chatgpt-payment-test-v3.js priority.

    The browser snippet only creates a hosted checkout and then reads:
    data.url || data.stripe_hosted_url || data.checkout_url.
    Keep a final BFS fallback for equivalent nested hosted URLs in newer payloads.
    """
    if not isinstance(data, dict):
        return ""
    for key in ("url", "stripe_hosted_url", "checkout_url", "openai_checkout_url"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return _bfs_find_pay_url(data)


def _extract_checkout_context(data: dict[str, Any]) -> tuple[str, str, str]:
    cs_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "").strip()
    processor_entity = str(data.get("processor_entity") or "").strip()
    checkout_url = _checkout_response_hosted_url(data)
    candidate_texts = [
        checkout_url,
        str(data.get("success_url") or ""),
        str(data.get("cancel_url") or ""),
        str(data.get("return_url") or ""),
        str(data.get("client_secret") or ""),
    ]
    if not cs_id:
        for text in candidate_texts:
            m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", text or "")
            if m:
                cs_id = m.group(1)
                break
    if not processor_entity:
        for text in candidate_texts:
            m = re.search(r"/checkout/([^/]+)/cs_(?:live|test)_[A-Za-z0-9]+", text or "")
            if m:
                processor_entity = m.group(1)
                break
        if not processor_entity:
            m = re.search(r"processor_entity=([A-Za-z0-9_]+)", " ".join(candidate_texts))
            if m:
                processor_entity = m.group(1)
    if not checkout_url and cs_id and processor_entity:
        checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
    return cs_id, processor_entity, checkout_url


def _canonical_checkout_url(cs_id: str, processor_entity: str) -> str:
    if not cs_id or not processor_entity:
        return ""
    return f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"


def _hosted_pay_url(cs_id: str) -> str:
    if not cs_id:
        return ""
    return f"https://pay.openai.com/c/pay/{cs_id}"


def _is_hosted_checkout_url(url: Any) -> bool:
    value = str(url or "").strip().lower()
    return "pay.openai.com/c/pay/" in value or "checkout.stripe.com/c/pay/" in value


def _normalize_hosted_checkout_url(url: Any) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + value[len("https://checkout.stripe.com") :]

    parsed = urlsplit(value)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return value


def _stripe_init_hosted_url(init_data: dict[str, Any]) -> str:
    if not isinstance(init_data, dict):
        return ""
    raw = str(init_data.get("stripe_hosted_url") or init_data.get("url") or "").strip()
    return _normalize_hosted_checkout_url(raw)


def _select_checkout_output_url(provider_url: str, cs_id: str, processor_entity: str, checkout_ui_mode: str) -> str:
    canonical_url = _canonical_checkout_url(cs_id, processor_entity)
    if str(checkout_ui_mode or "").strip().lower() == "hosted":
        provider = str(provider_url or "").strip()
        if _is_hosted_checkout_url(provider):
            return provider
        # Match the committed flow: when ChatGPT only returns the internal
        # /checkout route, synthesize the OpenAI hosted checkout URL from cs_id.
        return _hosted_pay_url(cs_id) or provider or canonical_url
    return canonical_url or provider_url


def _amount_to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _amount_at(data: dict[str, Any], *path: str) -> Any:
    cur: Any = data
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _collect_tax_amounts(value: Any, allow_scalar: bool = True) -> list[int]:
    amounts: list[int] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_lower = str(key).lower()
            if key_lower in ("amount", "tax_amount", "taxamount"):
                direct = _amount_to_int(nested)
                if direct is not None:
                    amounts.append(direct)
                continue
            if isinstance(nested, (dict, list)):
                amounts.extend(_collect_tax_amounts(nested, allow_scalar=False))
    elif isinstance(value, list):
        for item in value:
            amounts.extend(_collect_tax_amounts(item, allow_scalar=False))
    elif allow_scalar:
        direct = _amount_to_int(value)
        if direct is not None:
            amounts.append(direct)
    return amounts


def _zero_due_check(init_data: dict[str, Any]) -> dict[str, Any]:
    amount_candidates = {
        "total_summary.due": _amount_at(init_data, "total_summary", "due"),
        "total_summary.total": _amount_at(init_data, "total_summary", "total"),
        "invoice.amount_due": _amount_at(init_data, "invoice", "amount_due"),
        "invoice.total": _amount_at(init_data, "invoice", "total"),
    }
    amounts = {key: amount for key, raw in amount_candidates.items() if (amount := _amount_to_int(raw)) is not None}

    tax_candidates = [
        _amount_at(init_data, "total_summary", "tax"),
        _amount_at(init_data, "total_summary", "tax_amount"),
        _amount_at(init_data, "total_summary", "total_tax_amounts"),
        _amount_at(init_data, "invoice", "tax"),
        _amount_at(init_data, "invoice", "tax_amount"),
        _amount_at(init_data, "invoice", "total_tax_amounts"),
    ]
    tax_amounts: list[int] = []
    for candidate in tax_candidates:
        tax_amounts.extend(_collect_tax_amounts(candidate))

    amount_zero = bool(amounts) and all(amount == 0 for amount in amounts.values())
    tax_zero = all(amount == 0 for amount in tax_amounts)
    return {
        "ok": amount_zero and tax_zero,
        "amounts": amounts,
        "tax_amounts": tax_amounts,
        "tax_after_zero": tax_zero,
    }


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ 鏍稿績娴佺▼ 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€



def _try_gpt_pp_core_link(
    access_token: str,
    cfg: dict,
    region: dict,
    proxy: str,
    force_proxy: bool = False,
    payment_method: str = "paypal",
    auth_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .gpt_pp_core import generate_gpt_pp_paypal_link

    payment_cfg = _payment_cfg(cfg, payment_method)
    checkout_proxy = _stage_proxy(payment_cfg, "checkout", proxy, force_fallback=force_proxy)
    stripe_init_proxy = _stage_proxy(payment_cfg, "stripe_init", proxy, force_fallback=force_proxy)
    stripe_confirm_proxy = _stage_proxy(payment_cfg, "confirm", _stage_proxy(payment_cfg, "payment_method", stripe_init_proxy, force_fallback=force_proxy), force_fallback=force_proxy)
    stripe_pk = str((cfg.get("stripe") or {}).get("publishable_key") or "").strip()
    promo_campaign_id = _promo_campaign_id(payment_cfg)
    result = generate_gpt_pp_paypal_link(
        access_token,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        stripe_init_proxy=stripe_init_proxy,
        stripe_confirm_proxy=stripe_confirm_proxy,
        country=str(region.get("country") or "DE"),
        currency=str(region.get("currency") or "EUR"),
        checkout_ui_mode=_checkout_ui_mode(payment_cfg) or "hosted",
        promo_campaign_id=promo_campaign_id,
        require_zero=bool(payment_cfg.get("require_zero_due", False)),
        publishable_key=stripe_pk,
        timeout=DEFAULT_TIMEOUT,
        auth_context=auth_context,
    )
    result.setdefault("region", region.get("label") or f"{region.get('country', '')} ({region.get('currency', '')})")
    result.setdefault("billing_country", region.get("country") or "")
    result.setdefault("currency", str(region.get("currency") or result.get("currency") or "").upper())
    result.setdefault("promo_campaign_id", promo_campaign_id)
    result.setdefault("checkout_ui_mode", _checkout_ui_mode(payment_cfg) or "hosted")
    result.setdefault("link_mode", "stripe_redirect")
    result.setdefault("redirect_url_format", "stripe_authorize")
    result.setdefault("payment_method", "paypal")
    result.setdefault("method", "paypal")
    result.setdefault("source", "gpt_pp_core")
    result.setdefault("link_type", "gpt_pp_paypal_authorize")
    return result


def _try_paypal_link(
    access_token: str,
    cfg: dict,
    region: dict,
    proxy: str,
    force_proxy: bool = False,
    payment_method: str = "paypal",
    promo_campaign_id: str | None = None,
    auth_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    payment_method = _normalize_payment_method(payment_method)
    payment_label = PAYMENT_METHOD_LABELS[payment_method]
    log_prefix = _log_prefix(payment_method)
    payment_cfg = _payment_cfg(cfg, payment_method)
    reference_confirm_mode = _reference_confirm_mode(payment_cfg, payment_method)
    if reference_confirm_mode:
        payment_cfg = _reference_confirm_cfg(payment_cfg)
    checkout_only_long_url = _checkout_only_long_url(payment_cfg, payment_method)
    if checkout_only_long_url:
        payment_cfg = _hosted_long_url_cfg(payment_cfg)
    stop_after_pm_create = False if checkout_only_long_url else _stop_after_pm_create(payment_cfg, payment_method)
    if stop_after_pm_create:
        payment_cfg = _pm_create_only_cfg(payment_cfg)
    checkout_proxy = _stage_proxy(payment_cfg, "checkout", proxy, force_fallback=force_proxy)
    stripe_init_proxy = _stage_proxy(payment_cfg, "stripe_init", proxy, force_fallback=force_proxy)
    stripe_pm_proxy = _stage_proxy(payment_cfg, "payment_method", stripe_init_proxy, force_fallback=force_proxy)
    stripe_confirm_proxy = _stage_proxy(payment_cfg, "confirm", stripe_pm_proxy, force_fallback=force_proxy)
    stripe_pk = (cfg.get("stripe") or {}).get("publishable_key") or DEFAULT_STRIPE_PK
    runtime_cfg = cfg.get("runtime") or {}
    runtime_version = runtime_cfg.get("version") or "fed52f3bc6"
    address = region.get("address") if isinstance(region.get("address"), dict) else {}
    browser_locale = str(region.get("browser_locale") or "en-US")
    browser_timezone = str(region.get("browser_timezone") or "Asia/Shanghai")
    stripe_locale = str(region.get("stripe_locale") or "auto")
    payment_email = str((auth_context or {}).get("email") or region.get("payment_email") or "buyer@example.com")
    if promo_campaign_id is None:
        promo_campaign_id = _promo_campaign_id(payment_cfg)
    promo_label = promo_campaign_id or "none"
    checkout_ui_mode = _checkout_ui_mode(payment_cfg)
    link_mode = _payment_link_mode(payment_cfg, payment_method)
    redirect_format = _paypal_redirect_format(payment_cfg) if payment_method == "paypal" else "any"
    confirm_style = _paypal_confirm_style(payment_cfg) if payment_method == "paypal" else "payment_method_id"
    reference_confirm_mode = _reference_confirm_mode(payment_cfg, payment_method)

    # 鏋勫缓 ChatGPT session
    cs = _build_chatgpt_session(access_token, auth_context=auth_context)
    _set_session_proxy(cs, checkout_proxy)

    stripe_js_id = str(uuid.uuid4())
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    elements_session_error: dict[str, Any] = {}
    cs_id = ""
    pm_id = ""

    # 鈹€鈹€ Step 1: ChatGPT checkout 鈹€鈹€
    checkout_region = dict(region)
    body = _checkout_body(payment_cfg, checkout_region, promo_campaign_id)

    print(
        f"{log_prefix} checkout: method={payment_method} billing_region={region['country']} "
        f"ui_mode={checkout_ui_mode} link_mode={link_mode} redirect_format={redirect_format} "
        f"promo={promo_label} proxy={checkout_proxy or 'DIRECT'}",
        file=sys.stderr,
    )

    r = cs.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=body, timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code == 401:
        return {
            "ok": False,
            "error": f"checkout unauthorized: {r.status_code} {r.text[:300]}",
            "error_code": "checkout_unauthorized",
            "terminal": True,
            "retryable": False,
            "region": region["label"],
            "proxy": proxy,
            "stage_proxy": checkout_proxy or "DIRECT",
            "promo_campaign_id": promo_campaign_id,
            "checkout_ui_mode": checkout_ui_mode,
            "link_mode": link_mode,
            "redirect_url_format": redirect_format,
            "elements_session_id": elements_session_id,
            "elements_session_error": elements_session_error,
            "cs_id": cs_id,
            "pm_id": pm_id,
        }
    if r.status_code == 429:
        retry_after = str(getattr(r, "headers", {}).get("Retry-After", "") or "").strip()
        wait_hint = f" retry_after={retry_after}" if retry_after else ""
        return {
            "ok": False,
            "error": f"checkout rate limited: status=429{wait_hint}",
            "error_code": "checkout_rate_limited",
            "terminal": True,
            "retryable": True,
            "retry_after": retry_after,
            "region": region["label"],
            "proxy": proxy,
            "stage_proxy": checkout_proxy or "DIRECT",
            "promo_campaign_id": promo_campaign_id,
            "checkout_ui_mode": checkout_ui_mode,
            "link_mode": link_mode,
            "redirect_url_format": redirect_format,
        }
    if (
        r.status_code == 422
        and checkout_ui_mode == "hosted"
        and str(checkout_region.get("currency") or "").upper() != "USD"
        and _config_bool(payment_cfg.get("hosted_usd_fallback_on_422"), checkout_only_long_url)
    ):
        original_currency = str(checkout_region.get("currency") or "")
        checkout_region["currency"] = "USD"
        body = _checkout_body(payment_cfg, checkout_region, promo_campaign_id)
        print(
            f"{log_prefix} checkout hosted currency fallback: {original_currency}->USD",
            file=sys.stderr,
        )
        r = cs.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            json=body, timeout=DEFAULT_TIMEOUT,
        )
    if r.status_code == 400:
        err_text = r.text[:300]
        if "already paid" in err_text.lower():
            return {"ok": False, "error": "account already has ChatGPT Plus; checkout cannot be created again"}
        return {"ok": False, "error": f"checkout create failed: {r.status_code} {err_text}"}
    if r.status_code == 422:
        return {
            "ok": False,
            "error": f"checkout create failed: {r.status_code} {r.text[:300]}",
            "error_code": "checkout_unprocessable",
            "terminal": True,
            "retryable": False,
            "region": region["label"],
            "billing_country": checkout_region.get("country") or region["country"],
            "currency": checkout_region.get("currency") or region["currency"],
            "proxy": proxy,
            "stage_proxy": checkout_proxy or "DIRECT",
            "promo_campaign_id": promo_campaign_id,
            "checkout_ui_mode": checkout_ui_mode,
            "link_mode": link_mode,
        }
    r.raise_for_status()

    data = r.json()
    cs_id, processor_entity, checkout_url = _extract_checkout_context(data)
    if not cs_id or not cs_id.startswith("cs_"):
        return {"ok": False, "error": f"checkout 鍝嶅簲寮傚父: {json.dumps(data, ensure_ascii=False)[:300]}"}

    processor_entity = processor_entity or ("openai_llc" if region["country"] == "US" else "openai_ie")
    stripe_pk_source = "config_or_default"
    checkout_publishable_key = str(data.get("publishable_key") or "").strip()
    if checkout_publishable_key.startswith("pk_"):
        stripe_pk = checkout_publishable_key
        stripe_pk_source = "checkout_response"
    provider_checkout_url = checkout_url
    checkout_url = _select_checkout_output_url(provider_checkout_url, cs_id, processor_entity, checkout_ui_mode)
    print(f"{log_prefix} cs_id={cs_id} processor_entity={processor_entity}", file=sys.stderr)

    if not reference_confirm_mode:
        route_load_result = _chatgpt_load_checkout_route(cs, checkout_url=checkout_url, log_prefix=log_prefix)
        if route_load_result.get("status") or route_load_result.get("page_status"):
            print(
                f"{log_prefix} checkout route load: page_status={route_load_result.get('page_status')} "
                f"data_status={route_load_result.get('status')}",
                file=sys.stderr,
            )
    else:
        print(f"{log_prefix} reference confirm mode: skip checkout route load", file=sys.stderr)

    # Build Stripe sessions for init / optional payment-method / optional confirm stages.
    stripe_init = _new_session()
    _set_session_proxy(stripe_init, stripe_init_proxy)
    stripe_init.headers.update({
        "User-Agent": cs.headers.get("User-Agent", ""),
        "Accept-Language": "en-US,en;q=0.9",
    })
    stripe_pm = _new_session()
    _set_session_proxy(stripe_pm, stripe_pm_proxy)
    stripe_pm.headers.update(stripe_init.headers)
    stripe_confirm = _new_session()
    _set_session_proxy(stripe_confirm, stripe_confirm_proxy)
    stripe_confirm.headers.update(stripe_init.headers)

    # Step 2: Stripe init. Hosted long-link mode uses init_data.stripe_hosted_url.
    init_body = {
        "browser_locale": browser_locale,
        "browser_timezone": browser_timezone,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": stripe_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": stripe_pk,
        "_stripe_version": (
            "2025-03-31.basil; checkout_server_update_beta=v1; "
            "checkout_manual_approval_preview=v1"
        ),
    }
    if reference_confirm_mode:
        # External reference script does not send saved-payment-method options at init.
        init_body.pop("elements_options_client[saved_payment_method][enable_save]", None)
        init_body.pop("elements_options_client[saved_payment_method][enable_redisplay]", None)
    print(f"{log_prefix} stripe init: proxy={stripe_init_proxy or 'DIRECT'}", file=sys.stderr)
    r1 = _post_stripe_form(
        stripe_init,
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        init_body,
        timeout=DEFAULT_TIMEOUT,
        step="stripe init",
    )
    print(f"{log_prefix} stripe init: status={r1.status_code}", file=sys.stderr)
    if r1.status_code != 200:
        return _stripe_step_error_result(
            r1,
            step="init",
            error_code="stripe_init_failed",
            region=region,
            proxy=proxy,
            stage_proxy=stripe_init_proxy,
            cs_id=cs_id,
        )
    r1.raise_for_status()
    init_data = r1.json() or {}

    init_checksum = init_data.get("init_checksum") or ""
    if not init_checksum:
        return {"ok": False, "error": f"Stripe init missing init_checksum: {r1.text[:200]}"}

    elements_session_data: dict[str, Any] = {}
    if bool(payment_cfg.get("use_elements_session", True)):
        print(f"{log_prefix} elements session: proxy={stripe_init_proxy or 'DIRECT'}", file=sys.stderr)
        try:
            stripe_elements_session_id, elements_session_data, elements_session_response = _get_elements_session(
                stripe_init,
                init_data,
                cs_id=cs_id,
                stripe_pk=stripe_pk,
                stripe_js_id=stripe_js_id,
                stripe_locale=stripe_locale,
                timeout=DEFAULT_TIMEOUT,
            )
            print(
                f"{log_prefix} elements session: status={getattr(elements_session_response, 'status_code', None)}",
                file=sys.stderr,
            )
            if stripe_elements_session_id:
                elements_session_id = stripe_elements_session_id
            elif getattr(elements_session_response, "status_code", None) != 200:
                elements_session_error = _stripe_error_details(elements_session_response)
        except Exception as exc:
            elements_session_error = {"error": str(exc)}
            print(f"{log_prefix} elements session: failed={exc}", file=sys.stderr)

    if bool(payment_cfg.get("refresh_tax_region", True)):
        refresh_body = {
            "tax_region[country]": region["country"],
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": elements_session_id,
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": stripe_locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "auto",
            "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
            "key": stripe_pk,
            "_stripe_version": (
                "2025-03-31.basil; checkout_server_update_beta=v1; "
                "checkout_manual_approval_preview=v1"
            ),
        }
        print(f"{log_prefix} tax refresh: country={region['country']} proxy={stripe_init_proxy or 'DIRECT'}", file=sys.stderr)
        tax_response = _post_stripe_form(
            stripe_init,
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            refresh_body,
            timeout=DEFAULT_TIMEOUT,
            step="tax refresh",
        )
        print(f"{log_prefix} tax refresh: status={tax_response.status_code}", file=sys.stderr)
        if tax_response.status_code == 200:
            refreshed_init = tax_response.json() or {}
            if isinstance(refreshed_init, dict):
                init_data = refreshed_init
                init_checksum = init_data.get("init_checksum") or init_checksum

    due = (init_data.get("total_summary") or {}).get("due")
    amount_due = (init_data.get("invoice") or {}).get("amount_due")
    currency = (init_data.get("invoice") or {}).get("currency") or checkout_region.get("currency") or region["currency"]
    pm_types = init_data.get("payment_method_types") or []
    has_paypal = any("paypal" in (p or "").lower() for p in pm_types)
    has_gopay = any("gopay" in (p or "").lower() for p in pm_types)
    has_upi = any("upi" in (p or "").lower() for p in pm_types)
    has_payment_method = any(payment_method in (p or "").lower() for p in pm_types)
    zero_check = _zero_due_check(init_data)

    require_zero_due = bool(payment_cfg.get("require_zero_due", False))
    expected_amount = "0" if zero_check["ok"] else str(amount_due if amount_due is not None else (due if due is not None else 0))

    print(
        f"{log_prefix} init: due={due} amount_due={amount_due} currency={currency} "
        f"amounts={zero_check['amounts']} tax_amounts={zero_check['tax_amounts']} pm_types={pm_types}",
        file=sys.stderr,
    )

    if require_zero_due and not zero_check["ok"]:
        return {
            "ok": False,
            "error": (
                "Stripe checkout is not zero due after tax: "
                f"expected_amount=0 amounts={zero_check['amounts']} tax_amounts={zero_check['tax_amounts']}"
            ),
            "region": region["label"],
            "due": due,
            "amount_due": amount_due,
            "error_code": "checkout_not_zero_due",
            "terminal": True,
            "retryable": False,
            "expected_amount": expected_amount,
            "zero_due_verified": False,
            "tax_after_zero": zero_check["tax_after_zero"],
        }

    if not has_payment_method:
        return {
            "ok": False,
            "error": f"Stripe does not support {payment_label} for this checkout (available: {pm_types})",
            "region": region["label"],
            "payment_method": payment_method,
            "payment_method_types": pm_types,
            "has_paypal": has_paypal,
            "has_gopay": has_gopay,
            "has_upi": has_upi,
        }

    if link_mode == "chatgpt_checkout":
        if checkout_only_long_url or checkout_ui_mode == "hosted":
            hosted_url = _stripe_init_hosted_url(init_data)
            if not hosted_url or not _is_hosted_checkout_url(hosted_url):
                return {
                    "ok": False,
                    "error": (
                        "Stripe init did not include a usable hosted URL "
                        "(pay.openai.com/c/pay or checkout.stripe.com/c/pay)"
                    ),
                    "error_code": "hosted_checkout_url_missing",
                    "terminal": True,
                    "retryable": False,
                    "payment_method": payment_method,
                    "cs_id": cs_id,
                    "processor_entity": processor_entity,
                    "checkout_url": checkout_url,
                    "checkout_response_url": provider_checkout_url,
                    "stripe_hosted_url": str(init_data.get("stripe_hosted_url") or init_data.get("url") or ""),
                    "checkout_ui_mode": checkout_ui_mode,
                    "link_mode": link_mode,
                    "checkout_only_long_url": bool(checkout_only_long_url),
                    "region": region["label"],
                    "billing_country": region["country"],
                    "currency": str(checkout_region.get("currency") or region["currency"]).upper(),
                    "proxy": proxy,
                    "stage_proxies": {
                        "checkout": checkout_proxy or "DIRECT",
                        "stripe_init": stripe_init_proxy or "DIRECT",
                        "payment_method": "SKIPPED",
                        "confirm": "SKIPPED",
                    },
                }

            hosted_currency = str(currency or "").upper()
            promo_applied = bool(zero_check["ok"])
            coupon_state = f"eligible (0 {hosted_currency})" if promo_applied else f"not_eligible ({amount_due or due} {hosted_currency})"
            return {
                "ok": True,
                "url": hosted_url,
                "checkout_url": checkout_url,
                "hosted_checkout_url": hosted_url,
                "checkout_response_url": provider_checkout_url,
                "provider_url": provider_checkout_url,
                "preferred_url": hosted_url,
                "link_type": "hosted_long_url",
                "source": "stripe_init_hosted_url",
                "method": payment_method,
                "payment_method": payment_method,
                "cs_id": cs_id,
                "session_id": cs_id,
                "processor_entity": processor_entity,
                "pm_id": "",
                "due": due,
                "amount_due": amount_due,
                "currency": hosted_currency,
                "expected_amount": expected_amount,
                "zero_due_verified": zero_check["ok"],
                "tax_after_zero": zero_check["tax_after_zero"],
                "zero_due_amounts": zero_check["amounts"],
                "tax_amounts": zero_check["tax_amounts"],
                "payment_method_types": pm_types,
                "has_paypal": has_paypal,
                "has_gopay": has_gopay,
                "has_upi": has_upi,
                "coupon_state": coupon_state,
                "promo_campaign_id": promo_campaign_id,
                "checkout_ui_mode": checkout_ui_mode,
                "link_mode": link_mode,
                "redirect_url_format": redirect_format,
                "stripe_publishable_key_source": stripe_pk_source,
                "region": region["label"],
                "billing_country": region["country"],
                "proxy": proxy,
                "stage_proxies": {
                    "checkout": checkout_proxy or "DIRECT",
                    "stripe_init": stripe_init_proxy or "DIRECT",
                    "payment_method": "SKIPPED",
                    "confirm": "SKIPPED",
                },
            }
        if not checkout_url:
            if checkout_ui_mode == "hosted" and not _is_hosted_checkout_url(provider_checkout_url):
                return {
                    "ok": False,
                    "error": (
                        "Hosted checkout requested, but ChatGPT did not return a pay.openai.com "
                        "or checkout.stripe.com hosted URL; refusing to synthesize an unusable hosted link"
                    ),
                    "error_code": "hosted_checkout_url_missing",
                    "terminal": True,
                    "retryable": False,
                    "payment_method": payment_method,
                    "cs_id": cs_id,
                    "processor_entity": processor_entity,
                    "provider_url": provider_checkout_url,
                    "canonical_url": _canonical_checkout_url(cs_id, processor_entity),
                    "checkout_ui_mode": checkout_ui_mode,
                    "link_mode": link_mode,
                    "redirect_url_format": redirect_format,
                    "stripe_publishable_key_source": stripe_pk_source,
                    "region": region["label"],
                    "proxy": proxy,
                    "stage_proxies": {
                        "checkout": checkout_proxy or "DIRECT",
                        "stripe_init": stripe_init_proxy or "DIRECT",
                        "payment_method": "SKIPPED",
                        "confirm": "SKIPPED",
                    },
                }
            return {
                "ok": False,
                "error": "ChatGPT checkout response did not include a reusable checkout URL",
                "error_code": "checkout_url_missing",
                "terminal": True,
                "retryable": False,
                "payment_method": payment_method,
                "cs_id": cs_id,
                "processor_entity": processor_entity,
                "checkout_ui_mode": checkout_ui_mode,
                "link_mode": link_mode,
                "redirect_url_format": redirect_format,
                "stripe_publishable_key_source": stripe_pk_source,
            }

        promo_applied = bool(zero_check["ok"])
        coupon_state = f"eligible (0 {currency.upper()})" if promo_applied else f"not_eligible ({amount_due or due} {currency.upper()})"
        return {
            "ok": True,
            "url": checkout_url,
            "checkout_url": checkout_url,
            "provider_url": provider_checkout_url,
            "link_type": "chatgpt_checkout",
            "source": "chatgpt_checkout",
            "method": payment_method,
            "payment_method": payment_method,
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "pm_id": "",
            "due": due,
            "amount_due": amount_due,
            "currency": currency,
            "expected_amount": expected_amount,
            "zero_due_verified": zero_check["ok"],
            "tax_after_zero": zero_check["tax_after_zero"],
            "zero_due_amounts": zero_check["amounts"],
            "tax_amounts": zero_check["tax_amounts"],
            "payment_method_types": pm_types,
            "has_paypal": has_paypal,
            "has_gopay": has_gopay,
            "has_upi": has_upi,
            "coupon_state": coupon_state,
            "promo_campaign_id": promo_campaign_id,
            "checkout_ui_mode": checkout_ui_mode,
            "link_mode": link_mode,
            "redirect_url_format": redirect_format,
            "stripe_publishable_key_source": stripe_pk_source,
            "region": region["label"],
            "proxy": proxy,
            "stage_proxies": {
                "checkout": checkout_proxy or "DIRECT",
                "stripe_init": stripe_init_proxy or "DIRECT",
                "payment_method": "SKIPPED",
                "confirm": "SKIPPED",
            },
        }

    # Step 3: create the selected Stripe payment method.
    pm_body = {
        "type": payment_method,
        "billing_details[name]": "John Doe",
        "billing_details[email]": payment_email,
        "billing_details[address][country]": address.get("country") or region["country"],
        "billing_details[address][line1]": address.get("line1") or "",
        "billing_details[address][city]": address.get("city") or "",
        "billing_details[address][postal_code]": address.get("postal_code") or "",
        "billing_details[address][state]": address.get("state") or "",
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[client_session_id]": stripe_js_id,
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
        "key": stripe_pk,
        "_stripe_version": (
            "2025-03-31.basil; checkout_server_update_beta=v1; "
            "checkout_manual_approval_preview=v1"
        ),
    }

    use_inline_confirm = payment_method == "paypal" and confirm_style == "inline_payment_method_data"
    if use_inline_confirm:
        print(f"{log_prefix} pm create: skipped (inline confirm)", file=sys.stderr)
    else:
        print(f"{log_prefix} pm create: proxy={stripe_pm_proxy or 'DIRECT'}", file=sys.stderr)
        r2 = _post_stripe_form(
            stripe_pm,
            "https://api.stripe.com/v1/payment_methods",
            pm_body,
            timeout=DEFAULT_TIMEOUT,
            step="pm create",
        )
        print(f"{log_prefix} pm create: status={r2.status_code}", file=sys.stderr)

        if r2.status_code != 200:
            return _stripe_step_error_result(
                r2,
                step="payment_method",
                error_code="stripe_payment_method_failed",
                region=region,
                proxy=proxy,
                stage_proxy=stripe_pm_proxy,
                cs_id=cs_id,
            )

        pm_id = r2.json().get("id", "")
        if not pm_id.startswith("pm_"):
            return {"ok": False, "error": f"payment method response invalid: {r2.text[:200]}"}

        print(f"{log_prefix} pm_id={pm_id}", file=sys.stderr)

        if stop_after_pm_create:
            promo_applied = bool(zero_check["ok"])
            coupon_state = (
                f"eligible (0 {currency.upper()})"
                if promo_applied
                else f"not_eligible ({amount_due or due} {currency.upper()})"
            )
            print(f"{log_prefix} stop after pm create: success", file=sys.stderr)
            return {
                "ok": True,
                "url": "",
                "checkout_url": checkout_url,
                "provider_url": provider_checkout_url,
                "link_type": "pm_created",
                "source": "stripe_payment_method",
                "method": payment_method,
                "payment_method": payment_method,
                "status": "pm_created",
                "paypal_status": "pm_created",
                "cs_id": cs_id,
                "processor_entity": processor_entity,
                "pm_id": pm_id,
                "due": due,
                "amount_due": amount_due,
                "currency": currency,
                "expected_amount": expected_amount,
                "zero_due_verified": zero_check["ok"],
                "tax_after_zero": zero_check["tax_after_zero"],
                "zero_due_amounts": zero_check["amounts"],
                "tax_amounts": zero_check["tax_amounts"],
                "payment_method_types": pm_types,
                "has_paypal": has_paypal,
                "has_gopay": has_gopay,
                "has_upi": has_upi,
                "coupon_state": coupon_state,
                "promo_campaign_id": promo_campaign_id,
                "checkout_ui_mode": checkout_ui_mode,
                "link_mode": link_mode,
                "redirect_url_format": redirect_format,
                "stripe_publishable_key_source": stripe_pk_source,
                "elements_session_id": elements_session_id,
                "elements_session_error": elements_session_error,
                "elements_payment_method_types": (
                    (elements_session_data.get("payment_method_preference") or {}).get("ordered_payment_method_types")
                    if isinstance(elements_session_data.get("payment_method_preference"), dict)
                    else []
                ),
                "region": region["label"],
                "proxy": proxy,
                "stage_proxies": {
                    "checkout": checkout_proxy or "DIRECT",
                    "stripe_init": stripe_init_proxy or "DIRECT",
                    "payment_method": stripe_pm_proxy or "DIRECT",
                    "confirm": "SKIPPED",
                },
            }

    # 鈹€鈹€ Step 4: Stripe confirm 鈹€鈹€
    chatgpt_return = (
        f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}"
        f"&processor_entity={processor_entity}&plan_type=plus"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(chatgpt_return, safe='')}"
    )

    confirm_body = {
        "guid": str(uuid.uuid4()),
        "muid": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "init_checksum": init_checksum,
        "version": runtime_version,
        "expected_amount": expected_amount,
        "expected_payment_method_type": payment_method,
        "return_url": return_url,
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[locale]": stripe_locale,
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "elements_options_client[saved_payment_method][enable_save]": "never" if payment_method == "paypal" else "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never" if payment_method == "paypal" else "auto",
        "key": stripe_pk,
        "_stripe_version": (
            "2025-03-31.basil; checkout_server_update_beta=v1; "
            "checkout_manual_approval_preview=v1"
        ),
    }
    if reference_confirm_mode:
        # Match E:\QQ\Downloads\gen_pp_link.py: payment + address only, no
        # ChatGPT approve fallback and no expressCheckout marker.
        confirm_body["guid"] = uuid.uuid4().hex
        confirm_body["muid"] = uuid.uuid4().hex
        confirm_body["sid"] = uuid.uuid4().hex
        confirm_body["client_attribution_metadata[merchant_integration_additional_elements][0]"] = "payment"
        confirm_body["client_attribution_metadata[merchant_integration_additional_elements][1]"] = "address"
        confirm_body.pop("client_attribution_metadata[merchant_integration_additional_elements][2]", None)

    if use_inline_confirm:
        inline_payment_user_agent = (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        )
        confirm_body.update({
            "payment_method_data[billing_details][name]": "Z",
            "payment_method_data[billing_details][email]": payment_email,
            "payment_method_data[billing_details][address][line1]": address.get("line1") or "",
            "payment_method_data[billing_details][address][city]": address.get("city") or "",
            "payment_method_data[billing_details][address][postal_code]": address.get("postal_code") or "",
            "payment_method_data[billing_details][address][country]": address.get("country") or region["country"],
            "payment_method_data[type]": payment_method,
            "payment_method_data[payment_user_agent]": inline_payment_user_agent,
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(random.randint(18000, 45000)),
            "payment_method_data[client_attribution_metadata][client_session_id]": stripe_js_id,
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][elements_session_id]": elements_session_id,
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
        })
    else:
        confirm_body["payment_method"] = pm_id

    # Terms of service consent
    consent_collection = init_data.get("consent_collection") or {}
    tos = consent_collection.get("terms_of_service")
    if tos and tos not in ("none", ""):
        confirm_body["consent[terms_of_service]"] = "accepted"

    # Runtime anti-bot tokens
    if runtime_cfg.get("js_checksum"):
        confirm_body["js_checksum"] = runtime_cfg["js_checksum"]
    if runtime_cfg.get("rv_timestamp"):
        confirm_body["rv_timestamp"] = runtime_cfg["rv_timestamp"]

    if not reference_confirm_mode:
        snapshot_result = _chatgpt_checkout_snapshot(
            cs,
            checkout_url=checkout_url,
            cs_id=cs_id,
            processor_entity=processor_entity,
            log_prefix=log_prefix,
        )
        if snapshot_result.get("status"):
            print(f"{log_prefix} checkout snapshot: status={snapshot_result.get('status')}", file=sys.stderr)
    else:
        print(f"{log_prefix} reference confirm mode: skip checkout snapshot", file=sys.stderr)

    print(f"{log_prefix} confirm: proxy={stripe_confirm_proxy or 'DIRECT'}", file=sys.stderr)
    r3 = _post_stripe_form(
        stripe_confirm,
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        confirm_body,
        timeout=DEFAULT_TIMEOUT,
        step="confirm",
    )
    print(f"{log_prefix} confirm: status={r3.status_code}", file=sys.stderr)

    # Re-init retry for amount mismatch (race condition: invoice changes between init and confirm)
    reinit_attempts = 0
    while r3.status_code != 200 and reinit_attempts < 2:
        details = _stripe_error_details(r3)
        if details.get("code") != "checkout_amount_mismatch":
            break
        reinit_attempts += 1
        print(f"{log_prefix} amount mismatch, re-init retry {reinit_attempts}/2", file=sys.stderr)
        r1 = _post_stripe_form(
            stripe_init,
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
            init_body,
            timeout=DEFAULT_TIMEOUT,
            step="stripe init (re-init)",
        )
        if r1.status_code != 200:
            break
        init_data = r1.json() or {}
        init_checksum = init_data.get("init_checksum") or init_checksum
        due = (init_data.get("total_summary") or {}).get("due")
        amount_due = (init_data.get("invoice") or {}).get("amount_due")
        currency = (init_data.get("invoice") or {}).get("currency") or checkout_region.get("currency") or region["currency"]
        zero_check = _zero_due_check(init_data)
        expected_amount = "0" if zero_check["ok"] else str(amount_due if amount_due is not None else (due if due is not None else 0))
        confirm_body["init_checksum"] = init_checksum
        confirm_body["expected_amount"] = expected_amount
        r3 = _post_stripe_form(
            stripe_confirm,
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
            confirm_body,
            timeout=DEFAULT_TIMEOUT,
            step="confirm (re-init retry)",
        )
        print(f"{log_prefix} confirm (re-init retry {reinit_attempts}): status={r3.status_code}", file=sys.stderr)

    if r3.status_code != 200:
        return _stripe_confirm_error_result(
            r3,
            region=region,
            proxy=proxy,
            checkout_proxy=checkout_proxy,
            stripe_init_proxy=stripe_init_proxy,
            stripe_pm_proxy=stripe_pm_proxy,
            stripe_confirm_proxy=stripe_confirm_proxy,
            cs_id=cs_id,
            pm_id=pm_id,
            due=due,
            amount_due=amount_due,
            currency=currency,
            expected_amount=expected_amount,
            zero_check=zero_check,
            pm_types=pm_types,
            has_paypal=has_paypal,
            has_upi=has_upi,
            promo_campaign_id=promo_campaign_id,
            checkout_ui_mode=checkout_ui_mode,
        )

    confirm_data = r3.json() or {}

    # 鎻愬彇鎺堟潈閾炬帴
    redirect_url = _find_payment_redirect_url(confirm_data, payment_method, redirect_format=redirect_format)
    redirect_source = "confirm"
    approve_result: dict[str, Any] = {}
    redirect_poll_summary: dict[str, Any] = {}
    if (
        not redirect_url
        and payment_method == "paypal"
        and bool(payment_cfg.get("approve_missing_redirect", True))
    ):
        print(f"{log_prefix} confirm returned no redirect; approving checkout", file=sys.stderr)
        approve_result = _chatgpt_approve_checkout(
            cs,
            cs_id=cs_id,
            processor_entity=processor_entity,
            log_prefix=log_prefix,
            checkout_url=checkout_url,
        )
        print(
            f"{log_prefix} approve: ok={bool(approve_result.get('ok'))} "
            f"status={approve_result.get('status', '')} result={approve_result.get('result', '')}",
            file=sys.stderr,
        )
        poll_timeout = float(payment_cfg.get("redirect_poll_timeout_seconds", 30) or 30)
        poll_interval = float(payment_cfg.get("redirect_poll_interval_seconds", 1) or 1)
        redirect_url, redirect_poll_summary = _poll_payment_page_redirect_url(
            stripe_confirm,
            cs_id=cs_id,
            elements_session_id=elements_session_id,
            stripe_js_id=stripe_js_id,
            stripe_locale=stripe_locale,
            stripe_pk=stripe_pk,
            payment_method=payment_method,
            redirect_format=redirect_format,
            timeout_seconds=poll_timeout,
            poll_interval=poll_interval,
        )
        if redirect_url:
            redirect_source = "post_approve_payment_page"
    if not redirect_url:
        approve_blocked = (
            payment_method == "paypal"
            and approve_result
            and not approve_result.get("ok")
            and str(approve_result.get("result") or "").strip().lower() == "blocked"
        )
        redirect_label = (
            "Stripe authorize"
            if payment_method == "paypal" and redirect_format == "stripe_authorize"
            else "PayPal approve"
            if payment_method == "paypal" and redirect_format == "paypal_approve"
            else payment_label
        )
        return {
            "ok": False,
            "error": (
                "ChatGPT checkout approve was blocked after Stripe confirm returned no redirect"
                if approve_blocked
                else f"Stripe confirm did not return {redirect_label} redirect URL"
            ),
            "error_code": "checkout_approve_blocked" if approve_blocked else "stripe_confirm_missing_redirect",
            "terminal": True,
            "retryable": False,
            "confirm_summary": _confirm_summary(confirm_data),
            "region": region["label"],
            "payment_method": payment_method,
            "due": due,
            "amount_due": amount_due,
            "expected_amount": expected_amount,
            "zero_due_verified": zero_check["ok"],
            "tax_after_zero": zero_check["tax_after_zero"],
            "promo_campaign_id": promo_campaign_id,
            "checkout_ui_mode": checkout_ui_mode,
            "link_mode": link_mode,
            "redirect_url_format": redirect_format,
            "stripe_publishable_key_source": stripe_pk_source,
            "cs_id": cs_id,
            "pm_id": pm_id,
            "elements_session_id": elements_session_id,
            "elements_session_error": elements_session_error,
            "approve_result": approve_result,
            "redirect_poll_summary": redirect_poll_summary,
            "elements_payment_method_types": (
                (elements_session_data.get("payment_method_preference") or {}).get("ordered_payment_method_types")
                if isinstance(elements_session_data.get("payment_method_preference"), dict)
                else []
            ),
        }

    ba_resolve_result: dict[str, Any] = {}
    stripe_redirect_url = ""
    if payment_method == "paypal" and bool(payment_cfg.get("resolve_ba_redirect", False)):
        resolved_url, ba_resolve_result = _resolve_paypal_approve_url(
            redirect_url,
            proxy=stripe_confirm_proxy or proxy,
            log_prefix=log_prefix,
        )
        if resolved_url and resolved_url != redirect_url:
            stripe_redirect_url = redirect_url
            redirect_url = resolved_url
            redirect_source = "resolved_paypal_approve"
        if bool(payment_cfg.get("require_ba_token", False)) and not _paypal_ba_token(redirect_url):
            return {
                "ok": False,
                "error": ba_resolve_result.get("error") or "PayPal BA token was not resolved",
                "error_code": "paypal_ba_token_missing",
                "terminal": True,
                "retryable": False,
                "ba_resolve_result": ba_resolve_result,
                "region": region["label"],
                "payment_method": payment_method,
                "cs_id": cs_id,
                "pm_id": pm_id,
                "redirect_url_format": redirect_format,
                "checkout_ui_mode": checkout_ui_mode,
                "link_mode": link_mode,
            }

    promo_applied = bool(zero_check["ok"])
    coupon_state = f"eligible (0 {currency.upper()})" if promo_applied else f"not_eligible ({amount_due or due} {currency.upper()})"

    result = {
        "ok": True,
        "url": redirect_url,
        "method": payment_method,
        "payment_method": payment_method,
        "cs_id": cs_id,
        "pm_id": pm_id,
        "due": due,
        "amount_due": amount_due,
        "currency": currency,
        "expected_amount": expected_amount,
        "zero_due_verified": zero_check["ok"],
        "tax_after_zero": zero_check["tax_after_zero"],
        "zero_due_amounts": zero_check["amounts"],
        "tax_amounts": zero_check["tax_amounts"],
        "payment_method_types": pm_types,
        "has_paypal": has_paypal,
        "has_gopay": has_gopay,
        "has_upi": has_upi,
        "coupon_state": coupon_state,
        "promo_campaign_id": promo_campaign_id,
        "checkout_ui_mode": checkout_ui_mode,
        "link_mode": link_mode,
        "redirect_url_format": redirect_format,
        "stripe_publishable_key_source": stripe_pk_source,
        "redirect_source": redirect_source,
        "ba_resolve_result": ba_resolve_result,
        "elements_session_id": elements_session_id,
        "elements_payment_method_types": (
            (elements_session_data.get("payment_method_preference") or {}).get("ordered_payment_method_types")
            if isinstance(elements_session_data.get("payment_method_preference"), dict)
            else []
        ),
        "region": region["label"],
        "proxy": proxy,
        "stage_proxies": {
            "checkout": checkout_proxy or "DIRECT",
            "stripe_init": stripe_init_proxy or "DIRECT",
            "payment_method": stripe_pm_proxy or "DIRECT",
            "confirm": stripe_confirm_proxy or "DIRECT",
        },
    }
    if stripe_redirect_url:
        result["stripe_redirect_url"] = stripe_redirect_url
        result["ba_resolved"] = True
        result["ba_token_present"] = True
    return result


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ 鍏ュ彛 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _should_fallback_to_hosted_checkout(result: dict[str, Any] | None, payment_method: str, payment_cfg: dict[str, Any]) -> bool:
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    if _paypal_generation_type(payment_cfg) in {"paypal_direct", "paypal_direct_zero_due"}:
        return False
    if not isinstance(result, dict) or result.get("ok"):
        return False
    if str(result.get("error_code") or "") != "checkout_approve_blocked":
        return False
    return bool(payment_cfg.get("fallback_to_hosted_checkout_on_blocked", True))


def _hosted_checkout_fallback_cfg(cfg: dict[str, Any], payment_method: str) -> dict[str, Any]:
    cloned = json.loads(json.dumps(cfg or {}))
    paypal_cfg = cloned.setdefault("paypal", {})
    paypal_cfg["link_generation_type"] = "hosted_long_url"
    paypal_cfg["checkout_ui_mode"] = "hosted"
    paypal_cfg["link_mode"] = "chatgpt_checkout"
    paypal_cfg["resolve_ba_redirect"] = False
    paypal_cfg["require_ba_token"] = False
    paypal_cfg["approve_missing_redirect"] = False
    # Keep require_zero_due unchanged: user explicitly requires 0 due only.
    return cloned


def _should_fallback_from_missing_hosted_url(result: dict[str, Any] | None, payment_method: str, payment_cfg: dict[str, Any]) -> bool:
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    if not isinstance(result, dict) or result.get("ok"):
        return False
    if str(result.get("error_code") or "") != "hosted_checkout_url_missing":
        return False
    return bool(payment_cfg.get("fallback_to_stripe_redirect_on_missing_hosted", True))


def _stripe_redirect_fallback_cfg(cfg: dict[str, Any], payment_method: str) -> dict[str, Any]:
    cloned = json.loads(json.dumps(cfg or {}))
    paypal_cfg = cloned.setdefault("paypal", {})
    paypal_cfg.pop("link_generation_type", None)
    paypal_cfg.pop("generation_type", None)
    paypal_cfg.pop("paypal_generation_type", None)
    paypal_cfg["checkout_only_long_url"] = False
    paypal_cfg["checkout_ui_mode"] = "custom"
    paypal_cfg["link_mode"] = "stripe_redirect"
    paypal_cfg["confirm_style"] = "payment_method_id"
    paypal_cfg["resolve_ba_redirect"] = False
    paypal_cfg["require_ba_token"] = False
    paypal_cfg["approve_missing_redirect"] = True
    return cloned


def generate_payment_link(
    access_token: str,
    proxy: Any = None,
    payment_method: Any = "paypal",
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
) -> dict[str, Any]:
    try:
        cfg = _load_json(DEFAULT_CONFIG_PATH)
    except Exception as e:
        cfg = {}

    payment_method = _normalize_payment_method(payment_method)
    if payment_method == "paypal" and paypal_generation_type:
        paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
        paypal_cfg = dict(paypal_cfg)
        paypal_cfg["link_generation_type"] = str(paypal_generation_type or "").strip()
        cfg["paypal"] = paypal_cfg
    payment_cfg = _payment_cfg(cfg, payment_method)
    default_proxy = (cfg.get("proxy") or {}).get("default") or "direct"
    proxies, force_proxy = _proxy_candidates(payment_cfg, default_proxy, explicit_proxy=proxy)
    regions = _billing_regions(payment_cfg)
    max_checkout_retries = max(1, int(payment_cfg.get("max_checkout_retries", 3)))

    if payment_method == "paypal" and _paypal_generation_type(payment_cfg) == "gpt_pp_core":
        last_err = None
        for region in regions:
            for proxy in proxies:
                for attempt in range(1, max_checkout_retries + 1):
                    try:
                        if attempt > 1:
                            print(f"{_log_prefix(payment_method)} gpt-pp retry checkout: attempt={attempt}/{max_checkout_retries}", file=sys.stderr)
                        result = _try_gpt_pp_core_link(
                            access_token,
                            cfg,
                            region,
                            proxy,
                            force_proxy=force_proxy,
                            payment_method=payment_method,
                            auth_context=auth_context,
                        )
                        result["checkout_attempt"] = attempt
                        result["payment_method"] = payment_method
                        result["method"] = payment_method
                        if result.get("ok") or result.get("terminal"):
                            return result
                        if result.get("error"):
                            last_err = result["error"]
                    except Exception as e:
                        last_err = str(e)
                        print(f"{_log_prefix(payment_method)} gpt-pp attempt failed: {region['label']}+{proxy}: {last_err}", file=sys.stderr)
                        continue
        return {"ok": False, "error": f"gpt-pp all attempts failed, last error: {last_err}", "error_code": "gpt_pp_core_failed"}

    last_err = None
    for region in regions:
        for proxy in proxies:
            for attempt in range(1, max_checkout_retries + 1):
                try:
                    if attempt > 1:
                        print(f"{_log_prefix(payment_method)} retry checkout: method={payment_method} attempt={attempt}/{max_checkout_retries}", file=sys.stderr)
                    result = _try_paypal_link(
                        access_token,
                        cfg,
                        region,
                        proxy,
                        force_proxy=force_proxy,
                        payment_method=payment_method,
                        auth_context=auth_context,
                    )
                    if result and result.get("ok"):
                        result["checkout_attempt"] = attempt
                        result["payment_method"] = payment_method
                        result["method"] = payment_method
                        return result
                    if _should_fallback_to_hosted_checkout(result, payment_method, payment_cfg):
                        print(
                            f"{_log_prefix(payment_method)} approve blocked; retry hosted checkout long-link",
                            file=sys.stderr,
                        )
                        hosted_cfg = _hosted_checkout_fallback_cfg(cfg, payment_method)
                        hosted_result = _try_paypal_link(
                            access_token,
                            hosted_cfg,
                            region,
                            proxy,
                            force_proxy=force_proxy,
                            payment_method=payment_method,
                            auth_context=auth_context,
                        )
                        if hosted_result and hosted_result.get("ok"):
                            hosted_result["checkout_attempt"] = attempt
                            hosted_result["payment_method"] = payment_method
                            hosted_result["method"] = payment_method
                            hosted_result["fallback_from"] = "checkout_approve_blocked"
                            hosted_result["fallback_link_mode"] = "hosted_checkout"
                            return hosted_result
                        if hosted_result and hosted_result.get("error"):
                            result = dict(result or {})
                            result["hosted_fallback_error"] = hosted_result.get("error")
                    if _should_fallback_from_missing_hosted_url(result, payment_method, payment_cfg):
                        print(
                            f"{_log_prefix(payment_method)} hosted URL missing; retry stripe redirect flow",
                            file=sys.stderr,
                        )
                        redirect_cfg = _stripe_redirect_fallback_cfg(cfg, payment_method)
                        redirect_result = _try_paypal_link(
                            access_token,
                            redirect_cfg,
                            region,
                            proxy,
                            force_proxy=force_proxy,
                            payment_method=payment_method,
                            auth_context=auth_context,
                        )
                        if redirect_result:
                            redirect_result["checkout_attempt"] = attempt
                            redirect_result["payment_method"] = payment_method
                            redirect_result["method"] = payment_method
                            redirect_result["fallback_from"] = "hosted_checkout_url_missing"
                            redirect_result["fallback_link_mode"] = "stripe_redirect"
                            if redirect_result.get("ok") or redirect_result.get("terminal"):
                                return redirect_result
                            if redirect_result.get("error"):
                                result = dict(result or {})
                                result["stripe_redirect_fallback_error"] = redirect_result.get("error")
                    if result and _should_retry_without_promo(result, payment_method, payment_cfg):
                        print(
                            f"{_log_prefix(payment_method)} zero-due promo confirm declined; retry without promo",
                            file=sys.stderr,
                        )
                        fallback = _try_paypal_link(
                            access_token,
                            cfg,
                            region,
                            proxy,
                            force_proxy=force_proxy,
                            payment_method=payment_method,
                            promo_campaign_id="",
                            auth_context=auth_context,
                        )
                        if fallback:
                            fallback["checkout_attempt"] = attempt
                            fallback["payment_method"] = payment_method
                            fallback["method"] = payment_method
                            fallback["promo_fallback_attempted"] = True
                            if fallback.get("ok") or fallback.get("terminal"):
                                return fallback
                            if fallback.get("error"):
                                last_err = fallback["error"]
                                continue
                    if result and result.get("terminal"):
                        result["checkout_attempt"] = attempt
                        result["payment_method"] = payment_method
                        result["method"] = payment_method
                        return result
                    if result and result.get("error"):
                        last_err = result["error"]
                except Exception as e:
                    last_err = str(e)
                    print(f"{_log_prefix(payment_method)} attempt failed: {region['label']}+{proxy}: {last_err}", file=sys.stderr)
                    continue

    return {"ok": False, "error": f"all attempts failed, last error: {last_err}"}


def generate_pp_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
) -> dict[str, Any]:
    return generate_payment_link(
        access_token,
        proxy=proxy,
        payment_method="paypal",
        auth_context=auth_context,
        paypal_generation_type=paypal_generation_type,
    )


def main() -> int:
    args = sys.argv[1:]
    payment_method = "paypal"
    if "--payment-method" in args:
        idx = args.index("--payment-method")
        if idx + 1 < len(args):
            payment_method = _normalize_payment_method(args[idx + 1])
            del args[idx:idx + 2]

    if args and args[0] == "--dry-run":
        try:
            _cfg = _load_json(DEFAULT_CONFIG_PATH)
        except Exception:
            _cfg = {}
        _pp_cfg = _payment_cfg(_cfg, payment_method)
        _default_proxy = (_cfg.get("proxy") or {}).get("default") or "direct"
        _proxies = _pp_cfg.get("proxies") or [_default_proxy]
        _regions = _billing_regions(_pp_cfg)
        print(json.dumps({
            "ok": True,
            "mode": "dry-run",
            "config_exists": os.path.exists(DEFAULT_CONFIG_PATH),
            "proxies": _proxies,
            "regions": [r["label"] for r in _regions],
            "checkout_ui_mode": _checkout_ui_mode(_pp_cfg),
            "paypal_generation_type": _paypal_generation_type(_pp_cfg) if payment_method == "paypal" else "",
            "reference_confirm_mode": _reference_confirm_mode(_pp_cfg, payment_method),
            "link_mode": _payment_link_mode(_pp_cfg, payment_method),
            "checkout_only_long_url": _checkout_only_long_url(_pp_cfg, payment_method),
            "stop_after_pm_create": _stop_after_pm_create(_pp_cfg, payment_method),
            "effective_link_mode": "stripe_redirect" if _stop_after_pm_create(_pp_cfg, payment_method) else _payment_link_mode(_pp_cfg, payment_method),
            "redirect_url_format": _paypal_redirect_format(_pp_cfg) if payment_method == "paypal" else "any",
            "use_elements_session": bool(_pp_cfg.get("use_elements_session", True)),
            "approve_missing_redirect": bool(_pp_cfg.get("approve_missing_redirect", True)),
            "hosted_usd_fallback_on_422": _config_bool(_pp_cfg.get("hosted_usd_fallback_on_422"), _checkout_only_long_url(_pp_cfg, payment_method)),
            "hosted_usd_fallback_on_non_hosted": _config_bool(_pp_cfg.get("hosted_usd_fallback_on_non_hosted"), _checkout_only_long_url(_pp_cfg, payment_method)),
            "allow_chatgpt_checkout_fallback": _config_bool(_pp_cfg.get("allow_chatgpt_checkout_fallback"), False),
            "redirect_poll_timeout_seconds": _pp_cfg.get("redirect_poll_timeout_seconds", 30),
            "promo_campaign_id": _promo_campaign_id(_pp_cfg) or "",
            "require_zero_due": bool(_pp_cfg.get("require_zero_due", False)),
            "resolve_ba_redirect": _pp_cfg.get("resolve_ba_redirect", True),
            "require_ba_token": bool(_pp_cfg.get("require_ba_token", False)),
            "explicit_proxy_overrides_stage_proxies": bool(_pp_cfg.get("explicit_proxy_overrides_stage_proxies", False)),
            "disable_promo_on_confirm_decline": _pp_cfg.get("disable_promo_on_confirm_decline", True),
            "stage_proxies": _pp_cfg.get("stage_proxies") or {},
        }, ensure_ascii=False, indent=2))
        return 0

    if not args:
        print(json.dumps({"ok": False, "error": "鐢ㄦ硶: gen_pp_link.py <access_token>"}, ensure_ascii=False))
        return 2

    access_token = parse_token(args[0])
    if not access_token:
        print(json.dumps({"ok": False, "error": "invalid access_token: expected an eyJ JWT or session JSON"}, ensure_ascii=False))
        return 1

    result = generate_payment_link(access_token, payment_method=payment_method)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
