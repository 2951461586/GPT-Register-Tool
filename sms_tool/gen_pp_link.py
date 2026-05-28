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
import uuid
from typing import Any
from urllib.parse import quote

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

BILLING_REGIONS = [
    {"country": "US", "currency": "USD", "label": "United States (USD)"},
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
}


def _log_prefix(payment_method: Any = "") -> str:
    return "[gopay]" if _normalize_payment_method(payment_method) == "gopay" else "[paypal]"

# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ Session 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _new_session(impersonate: str = "chrome136") -> Any:
    if _CurlCffiSession is not None:
        return _CurlCffiSession(impersonate=impersonate)
    return requests.Session()


def _build_chatgpt_session(access_token: str) -> Any:
    device_id = str(uuid.uuid4())
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    s = _new_session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    if access_token:
        s.headers["Authorization"] = f"Bearer {access_token}"
    s.headers["Cookie"] = f"oai-did={device_id}; __Secure-next-auth.session-token=dummy"
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
        return [_normalize_proxy(explicit_proxy)], True

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
    return "paypal"


def _payment_cfg(cfg: dict[str, Any], payment_method: str) -> dict[str, Any]:
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    if payment_method == "paypal":
        return paypal_cfg
    method_cfg = cfg.get(payment_method) if isinstance(cfg.get(payment_method), dict) else {}
    merged = dict(paypal_cfg)
    merged.update(method_cfg)
    if not (method_cfg.get("billing_regions") or method_cfg.get("billing_region") or method_cfg.get("billing_country")):
        merged["billing_regions"] = ["ID"]
    return merged


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


def _is_terminal_confirm_decline(details: dict[str, Any]) -> bool:
    if details.get("status") != 402:
        return False
    if details.get("decline_code"):
        return True
    return details.get("code") in {"setup_attempt_failed", "payment_intent_payment_attempt_failed"}


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


def _extract_checkout_context(data: dict[str, Any]) -> tuple[str, str, str]:
    cs_id = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "").strip()
    processor_entity = str(data.get("processor_entity") or "").strip()
    checkout_url = str(
        data.get("checkout_url") or data.get("url") or data.get("openai_checkout_url") or ""
    ).strip()
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


def _try_paypal_link(
    access_token: str,
    cfg: dict,
    region: dict,
    proxy: str,
    force_proxy: bool = False,
    payment_method: str = "paypal",
) -> dict[str, Any] | None:
    payment_method = _normalize_payment_method(payment_method)
    payment_label = PAYMENT_METHOD_LABELS[payment_method]
    log_prefix = _log_prefix(payment_method)
    payment_cfg = _payment_cfg(cfg, payment_method)
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
    payment_email = str(region.get("payment_email") or "buyer@example.com")

    # 鏋勫缓 ChatGPT session
    cs = _build_chatgpt_session(access_token)
    _set_session_proxy(cs, checkout_proxy)

    # 鏋勫缓 Stripe 澶栭儴 session
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

    # 鈹€鈹€ Step 1: ChatGPT checkout 鈹€鈹€
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": region["country"],
            "currency": region["currency"],
        },
        "checkout_ui_mode": "hosted",
        "cancel_url": "https://chatgpt.com/#pricing",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }

    print(
        f"{log_prefix} checkout: method={payment_method} billing_region={region['country']} promo=plus-1-month-free proxy={checkout_proxy or 'DIRECT'}",
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
        }
    if r.status_code == 400:
        err_text = r.text[:300]
        if "already paid" in err_text.lower():
            return {"ok": False, "error": "account already has ChatGPT Plus; checkout cannot be created again"}
        return {"ok": False, "error": f"checkout create failed: {r.status_code} {err_text}"}
    r.raise_for_status()

    data = r.json()
    cs_id, processor_entity, checkout_url = _extract_checkout_context(data)
    if not cs_id or not cs_id.startswith("cs_"):
        return {"ok": False, "error": f"checkout 鍝嶅簲寮傚父: {json.dumps(data, ensure_ascii=False)[:300]}"}

    processor_entity = processor_entity or ("openai_llc" if region["country"] == "US" else "openai_ie")
    print(f"{log_prefix} cs_id={cs_id} processor_entity={processor_entity}", file=sys.stderr)

    # 鈹€鈹€ Step 2: Stripe init 鈹€鈹€
    init_body = {
        "browser_locale": browser_locale,
        "browser_timezone": browser_timezone,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": stripe_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "key": stripe_pk,
        "_stripe_version": (
            "2025-03-31.basil; checkout_server_update_beta=v1; "
            "checkout_manual_approval_preview=v1"
        ),
    }
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

    due = (init_data.get("total_summary") or {}).get("due")
    amount_due = (init_data.get("invoice") or {}).get("amount_due")
    currency = (init_data.get("invoice") or {}).get("currency") or region["currency"]
    pm_types = init_data.get("payment_method_types") or []
    has_paypal = any("paypal" in (p or "").lower() for p in pm_types)
    has_gopay = any("gopay" in (p or "").lower() for p in pm_types)
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
        }

    # Step 3: create the selected Stripe payment method.
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"

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
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method": pm_id,
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
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": (
            "2025-03-31.basil; checkout_server_update_beta=v1; "
            "checkout_manual_approval_preview=v1"
        ),
    }

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
        currency = (init_data.get("invoice") or {}).get("currency") or region["currency"]
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
        )

    confirm_data = r3.json() or {}

    # 鎻愬彇鎺堟潈閾炬帴
    redirect_url = ""
    si = confirm_data.get("setup_intent") or {}
    na = si.get("next_action") or {}
    if na.get("type") == "redirect_to_url":
        redirect_url = (na.get("redirect_to_url") or {}).get("url", "")
    if not redirect_url:
        pi = confirm_data.get("payment_intent") or {}
        na = pi.get("next_action") or {}
        if na.get("type") == "redirect_to_url":
            redirect_url = (na.get("redirect_to_url") or {}).get("url", "")
    if not redirect_url:
        return {
            "ok": False,
            "error": f"Stripe confirm did not return {payment_label} redirect URL",
            "region": region["label"],
            "payment_method": payment_method,
            "due": due,
            "amount_due": amount_due,
            "expected_amount": expected_amount,
            "zero_due_verified": zero_check["ok"],
            "tax_after_zero": zero_check["tax_after_zero"],
        }

    promo_applied = bool(zero_check["ok"])
    coupon_state = f"eligible (0 {currency.upper()})" if promo_applied else f"not_eligible ({amount_due or due} {currency.upper()})"

    return {
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
        "coupon_state": coupon_state,
        "region": region["label"],
        "proxy": proxy,
        "stage_proxies": {
            "checkout": checkout_proxy or "DIRECT",
            "stripe_init": stripe_init_proxy or "DIRECT",
            "payment_method": stripe_pm_proxy or "DIRECT",
            "confirm": stripe_confirm_proxy or "DIRECT",
        },
    }


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ 鍏ュ彛 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def generate_payment_link(access_token: str, proxy: Any = None, payment_method: Any = "paypal") -> dict[str, Any]:
    try:
        cfg = _load_json(DEFAULT_CONFIG_PATH)
    except Exception as e:
        cfg = {}

    payment_method = _normalize_payment_method(payment_method)
    payment_cfg = _payment_cfg(cfg, payment_method)
    default_proxy = (cfg.get("proxy") or {}).get("default") or "direct"
    proxies, force_proxy = _proxy_candidates(payment_cfg, default_proxy, explicit_proxy=proxy)
    regions = _billing_regions(payment_cfg)
    max_checkout_retries = max(1, int(payment_cfg.get("max_checkout_retries", 3)))

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
                    )
                    if result and result.get("ok"):
                        result["checkout_attempt"] = attempt
                        result["payment_method"] = payment_method
                        result["method"] = payment_method
                        return result
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


def generate_pp_link(access_token: str, proxy: Any = None) -> dict[str, Any]:
    return generate_payment_link(access_token, proxy=proxy, payment_method="paypal")


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
        _pp_cfg = (_cfg.get("paypal") or {})
        _default_proxy = (_cfg.get("proxy") or {}).get("default") or "direct"
        _proxies = _pp_cfg.get("proxies") or [_default_proxy]
        _regions = _billing_regions(_pp_cfg)
        print(json.dumps({
            "ok": True,
            "mode": "dry-run",
            "config_exists": os.path.exists(DEFAULT_CONFIG_PATH),
            "proxies": _proxies,
            "regions": [r["label"] for r in _regions],
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
