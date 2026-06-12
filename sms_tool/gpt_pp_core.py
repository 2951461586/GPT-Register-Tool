#!/usr/bin/env python3
"""gpt-pp style PayPal authorize link extractor.

This module is a compact, project-local port of the core gpt-pp chain:
ChatGPT checkout -> Stripe payment_pages init -> PayPal confirm ->
pm-redirects.stripe.com/authorize long link.

It intentionally avoids UI/deploy/proxy-pool surfaces from the upstream project
and returns the same dictionary shape as sms_tool.gen_pp_link.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any
from urllib.parse import quote

import requests

try:  # Prefer browser-like TLS when available.
    from curl_cffi.requests import Session as _CurlCffiSession
except Exception:  # pragma: no cover - depends on optional runtime package
    _CurlCffiSession = None

DEFAULT_OPENAI_STRIPE_PUBLISHABLE_KEY = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\"'\s<>]+")
PAY_OPENAI_RE = re.compile(r"https://pay\.openai\.com/c/pay/[^\"'\s<>]+")
CS_RE = re.compile(r"(cs_(?:live|test)_[A-Za-z0-9]+)")
ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}


def normalize_proxy(value: Any, scheme: str = "socks5h") -> str:
    proxy = str(value or "").strip()
    if not proxy or proxy.lower() in {"direct", "none", "null", "false", "off"}:
        return ""
    if proxy.startswith(("socks5://", "socks5h://", "http://", "https://")):
        return proxy
    if "@" not in proxy:
        parts = proxy.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, user = parts[0].strip(), parts[1].strip(), parts[2].strip()
            password = ":".join(parts[3:]).strip()
            if host and port and user and password:
                proxy = f"{user}:{password}@{host}:{port}"
    scheme = scheme.rstrip(":/") or "socks5h"
    return f"{scheme}://{proxy}"


def _new_session(proxy: str = "", impersonate: str = "chrome"):
    proxy = normalize_proxy(proxy)
    if _CurlCffiSession is not None:
        kwargs: dict[str, Any] = {"impersonate": impersonate, "verify": True}
        if proxy:
            kwargs["proxy"] = proxy
        return _CurlCffiSession(**kwargs)
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _close(session: Any) -> None:
    try:
        session.close()
    except Exception:
        pass


def _post(session: Any, url: str, *, headers: dict[str, str], data: Any = None, json_body: Any = None, timeout: int = 30):
    if json_body is not None:
        return session.post(url, headers=headers, json=json_body, timeout=timeout)
    return session.post(url, headers=headers, data=data, timeout=timeout)


def _find_url(value: Any, pattern: re.Pattern[str] = PM_REDIRECT_RE) -> str:
    if isinstance(value, str):
        match = pattern.search(value)
        return match.group(0) if match else ""
    if isinstance(value, dict):
        for key in ("url", "redirect_url", "authorize_url", "hosted_checkout_url", "checkout_url"):
            found = _find_url(value.get(key), pattern)
            if found:
                return found
        for child in value.values():
            found = _find_url(child, pattern)
            if found:
                return found
    if isinstance(value, (list, tuple)):
        for child in value:
            found = _find_url(child, pattern)
            if found:
                return found
    return ""


def _extract_checkout_context(data: dict[str, Any]) -> tuple[str, str, str, str]:
    cs_id = str(data.get("checkout_session_id") or data.get("id") or "").strip()
    processor_entity = str(data.get("processor_entity") or "").strip()
    publishable_key = str(data.get("publishable_key") or "").strip()
    hosted_url = ""
    for key in ("stripe_hosted_url", "hosted_checkout_url", "url", "checkout_url"):
        candidate = str(data.get(key) or "").strip()
        if "/c/pay/" in candidate:
            hosted_url = candidate
            break
    if not hosted_url:
        hosted_url = _find_url(data, PAY_OPENAI_RE)
    if not hosted_url and cs_id.startswith("cs_"):
        client_secret = str(data.get("client_secret") or "")
        fragment = ""
        if "_secret_" in client_secret:
            fragment = client_secret.split("_secret_", 1)[1]
        hosted_url = f"https://pay.openai.com/c/pay/{cs_id}" + (f"#{fragment}" if fragment else "")
    if not cs_id and hosted_url:
        m = CS_RE.search(hosted_url)
        if m:
            cs_id = m.group(1)
    return cs_id, processor_entity, hosted_url, publishable_key


def create_checkout(
    access_token: str,
    *,
    proxy: str = "",
    country: str = "DE",
    currency: str = "EUR",
    checkout_ui_mode: str = "hosted",
    promo_campaign_id: str = "plus-1-month-free",
    timeout: int = 30,
    auth_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = _new_session(proxy)
    path = "/backend-api/payments/checkout"
    payload: dict[str, Any] = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "entry_point": "all_plans_pricing_modal",
        "checkout_ui_mode": checkout_ui_mode or "hosted",
    }
    if promo_campaign_id:
        payload["promo_campaign"] = {
            "promo_campaign_id": promo_campaign_id,
            "is_coupon_from_query_param": False,
        }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "X-OpenAI-Target-Path": path,
        "X-OpenAI-Target-Route": path,
    }
    cookie_header = ""
    if isinstance(auth_context, dict):
        cookie_header = str(auth_context.get("cookie_header") or auth_context.get("cookies") or "").strip()
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        resp = _post(session, "https://chatgpt.com" + path, headers=headers, json_body=payload, timeout=timeout)
        status = int(getattr(resp, "status_code", 0) or 0)
        text = getattr(resp, "text", "") or ""
        try:
            data = resp.json()
        except Exception:
            data = {"raw": text[:1000]}
        cs_id, processor_entity, hosted_url, publishable_key = _extract_checkout_context(data if isinstance(data, dict) else {})
        return {
            "ok": 200 <= status < 300 and bool(cs_id or hosted_url),
            "status": status,
            "checkout_session_id": cs_id,
            "processor_entity": processor_entity,
            "publishable_key": publishable_key,
            "hosted_checkout_url": hosted_url,
            "raw_response": data,
            "error": "" if 200 <= status < 300 else _public_error(data, text),
        }
    finally:
        _close(session)


def _public_error(data: Any, text: str = "") -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or json.dumps(err, ensure_ascii=False)[:300])
        if err:
            return str(err)[:300]
    return str(text or data or "")[:300]


def fetch_checkout_init(
    hosted_url: str,
    *,
    proxy: str = "",
    publishable_key: str = "",
    timeout: int = 12,
) -> tuple[str, str, dict[str, Any], str]:
    cs_match = CS_RE.search(str(hosted_url or ""))
    if not cs_match:
        return "", "", {}, "checkout_session_id_missing"
    cs = cs_match.group(1)
    prefix = "pk_live_" if cs.startswith("cs_live_") else "pk_test_"
    candidates: list[str] = []
    if publishable_key and publishable_key.startswith(prefix):
        candidates.append(publishable_key)
    if DEFAULT_OPENAI_STRIPE_PUBLISHABLE_KEY.startswith(prefix):
        candidates.append(DEFAULT_OPENAI_STRIPE_PUBLISHABLE_KEY)
    # Keep order but remove duplicates.
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return "", cs, {}, "publishable_key_missing"

    session = _new_session(proxy)
    try:
        init_url = f"https://api.stripe.com/v1/payment_pages/{cs}/init"
        last_error = ""
        for pk in candidates:
            form = {
                "key": pk,
                "eid": "NA",
                "browser_locale": "en-US",
                "browser_timezone": "Asia/Shanghai",
                "redirect_type": "url",
            }
            for referer in ("https://pay.openai.com/", hosted_url.split("#", 1)[0]):
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "Origin": "https://pay.openai.com",
                    "Referer": referer,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                }
                resp = _post(session, init_url, headers=headers, data=form, timeout=timeout)
                status = int(getattr(resp, "status_code", 0) or 0)
                if status != 200:
                    last_error = f"stripe_init_http_{status}"
                    continue
                try:
                    init = resp.json()
                except Exception:
                    last_error = "stripe_init_invalid_json"
                    continue
                if isinstance(init, dict) and init:
                    init.setdefault("url", hosted_url)
                    return pk, cs, init, ""
        return candidates[0], cs, {}, last_error or "stripe_init_failed"
    finally:
        _close(session)


def _coerce_int(value: Any) -> tuple[int, bool]:
    if isinstance(value, bool):
        return 0, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value), True
    return 0, False


def payable_amount(init: dict[str, Any]) -> tuple[int, bool]:
    total_summary = init.get("total_summary") if isinstance(init.get("total_summary"), dict) else {}
    amount, ok = _coerce_int(total_summary.get("due"))
    if ok:
        return amount, True
    invoice = init.get("invoice") if isinstance(init.get("invoice"), dict) else {}
    if invoice and str(invoice.get("billing_cycle_anchor") or "") and not bool(invoice.get("has_prorations")):
        return 0, True
    return _coerce_int(invoice.get("amount_due"))


def _currency(init: dict[str, Any], fallback: str = "") -> str:
    invoice = init.get("invoice") if isinstance(init.get("invoice"), dict) else {}
    return str(init.get("currency") or invoice.get("currency") or fallback or "").lower()


def amount_display(amount: int | None, currency: str) -> str:
    if amount is None:
        return "unknown"
    code = (currency or "UNKNOWN").upper()
    if (currency or "").lower() in ZERO_DECIMAL:
        return f"{amount} {code}"
    return f"{amount / 100:.2f} {code}"


def display_amounts(init: dict[str, Any]) -> dict[str, int]:
    invoice = init.get("invoice") if isinstance(init.get("invoice"), dict) else {}
    total_summary = init.get("total_summary") if isinstance(init.get("total_summary"), dict) else {}
    lines = invoice.get("lines") if isinstance(invoice.get("lines"), dict) else {}
    items = lines.get("data") if isinstance(lines.get("data"), list) else []
    subtotal = 0
    for item in items:
        if isinstance(item, dict):
            val, ok = _coerce_int(item.get("amount"))
            if ok:
                subtotal += val
    total, _ = _coerce_int(total_summary.get("total"))
    due, due_ok = payable_amount(init)
    if not subtotal:
        subtotal, _ = _coerce_int(total_summary.get("subtotal"))
    if not total:
        total = due if due_ok else 0
    return {
        "subtotal": subtotal,
        "total_exclusive_tax": 0,
        "total_inclusive_tax": total,
        "total_discount_amount": max(subtotal - total, 0),
        "shipping_rate_amount": 0,
        "due": due if due_ok else 0,
    }


def verify_checkout(init: dict[str, Any], *, require_zero: bool = False) -> dict[str, Any]:
    amount, amount_ok = payable_amount(init)
    currency = _currency(init)
    if not amount_ok:
        return {"ok": False, "code": "checkout_guard_failed", "message": "Stripe init amount_due is missing", "amount_due": None, "currency": currency, "zero_verified": False}
    methods_raw = init.get("payment_method_types")
    methods = {str(x or "").lower() for x in methods_raw} if isinstance(methods_raw, list) else set()
    if "paypal" not in methods and "paypal" not in json.dumps(init, ensure_ascii=False).lower():
        return {"ok": False, "code": "paypal_not_supported", "message": "Stripe checkout does not support PayPal", "amount_due": amount, "currency": currency, "zero_verified": amount == 0}
    if require_zero and amount != 0:
        return {"ok": False, "code": "checkout_not_zero_due", "message": "Stripe checkout is not zero due", "amount_due": amount, "currency": currency, "zero_verified": False}
    return {"ok": True, "code": "amount_verified", "amount_due": amount, "currency": currency, "zero_verified": amount == 0}


def billing_address(country: str) -> dict[str, str]:
    country = (country or "US").upper()
    if country == "JP":
        return {"country": "JP", "postal_code": "100-0001", "state": "Tokyo", "city": "Chiyoda-ku", "line1": "1-1 Chiyoda", "line2": "Tokyo"}
    if country == "DE":
        return {"country": "DE", "postal_code": "10115", "state": "Berlin", "city": "Berlin", "line1": "Invalidenstrasse 1", "line2": "Berlin"}
    if country == "FR":
        return {"country": "FR", "postal_code": "75001", "state": "Ile-de-France", "city": "Paris", "line1": "10 Rue de Rivoli", "line2": "Paris"}
    if country == "GB":
        return {"country": "GB", "postal_code": "SW1A 2AA", "state": "London", "city": "London", "line1": "10 Downing Street", "line2": "London"}
    if country == "AU":
        return {"country": "AU", "postal_code": "2000", "state": "NSW", "city": "Sydney", "line1": "1 Macquarie Street", "line2": "Sydney"}
    return {"country": "US", "postal_code": "10001", "state": "NY", "city": "New York", "line1": "350 5th Ave", "line2": "New York"}


def build_confirm_payload(pk: str, cs: str, init: dict[str, Any], *, require_zero: bool = False, country: str = "US") -> tuple[dict[str, str], dict[str, Any]]:
    gate = verify_checkout(init, require_zero=require_zero)
    if not gate.get("ok"):
        return {}, gate
    amount_due = int(gate.get("amount_due") or 0)
    amounts = display_amounts(init)
    address = billing_address(country)
    hosted_url = str(init.get("url") or f"https://pay.openai.com/c/pay/{cs}")
    return_url = hosted_url or f"https://pay.openai.com/c/pay/{cs}"
    payload = {
        "eid": "NA",
        "key": pk,
        "init_checksum": str(init.get("init_checksum") or ""),
        "expected_amount": str(amount_due),
        "expected_payment_method_type": "paypal",
        "payment_method_data[type]": "paypal",
        "payment_method_data[billing_details][email]": str(init.get("customer_email") or "buyer@example.com"),
        "payment_method_data[billing_details][address][country]": address["country"],
        "payment_method_data[billing_details][address][postal_code]": address["postal_code"],
        "payment_method_data[billing_details][address][state]": address["state"],
        "payment_method_data[billing_details][address][city]": address["city"],
        "payment_method_data[billing_details][address][line1]": address["line1"],
        "payment_method_data[billing_details][address][line2]": address["line2"],
        "payment_method_data[client_attribution_metadata][client_session_id]": uuid.uuid4().hex,
        "payment_method_data[client_attribution_metadata][checkout_session_id]": cs,
        "payment_method_data[payment_user_agent]": "stripe.js/payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": "31000",
        "consent[terms_of_service]": "accepted",
        "last_displayed_line_item_group_details[subtotal]": str(amounts["subtotal"]),
        "last_displayed_line_item_group_details[total_exclusive_tax]": str(amounts["total_exclusive_tax"]),
        "last_displayed_line_item_group_details[total_inclusive_tax]": str(amounts["total_inclusive_tax"]),
        "last_displayed_line_item_group_details[total_discount_amount]": str(amounts["total_discount_amount"]),
        "last_displayed_line_item_group_details[shipping_rate_amount]": str(amounts["shipping_rate_amount"]),
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    return payload, gate


def confirm_paypal_authorize(
    *,
    pk: str,
    cs: str,
    init: dict[str, Any],
    proxy: str = "",
    require_zero: bool = False,
    country: str = "US",
    timeout: int = 12,
) -> dict[str, Any]:
    payload, gate = build_confirm_payload(pk, cs, init, require_zero=require_zero, country=country)
    if not gate.get("ok"):
        return {"ok": False, "status": 422 if gate.get("code") != "checkout_not_zero_due" else 409, "code": gate.get("code"), "message": gate.get("message"), "zero_gate": gate, "pm_authorize_url": ""}
    session = _new_session(proxy)
    hosted_url = str(init.get("url") or "")
    referer = (hosted_url or f"https://pay.openai.com/c/pay/{cs}").split("#", 1)[0]
    endpoint = f"https://api.stripe.com/v1/payment_pages/{cs}/confirm"
    headers = {
        "Origin": "https://pay.openai.com",
        "Referer": referer,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    try:
        resp = _post(session, endpoint, headers=headers, data=payload, timeout=timeout)
        status = int(getattr(resp, "status_code", 0) or 0)
        text = getattr(resp, "text", "") or ""
        try:
            body = resp.json()
        except Exception:
            body = {"raw": text[:2000]}
        pm_url = _find_url(body) or _find_url(text)
        return {
            "ok": 200 <= status < 300 and bool(pm_url),
            "status": status,
            "code": "paypal_authorize_extracted" if pm_url else "stripe_confirm_missing_redirect",
            "message": "" if pm_url else _public_error(body, text) or "Stripe confirm did not return PayPal authorize URL",
            "endpoint": endpoint,
            "pm_authorize_url": pm_url,
            "zero_gate": gate,
            "response": body,
        }
    finally:
        _close(session)


def generate_gpt_pp_paypal_link(
    access_token: str,
    *,
    proxy: str = "",
    checkout_proxy: str = "",
    stripe_init_proxy: str = "",
    stripe_confirm_proxy: str = "",
    country: str = "DE",
    currency: str = "EUR",
    checkout_ui_mode: str = "hosted",
    promo_campaign_id: str = "plus-1-month-free",
    require_zero: bool = False,
    publishable_key: str = "",
    timeout: int = 30,
    auth_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    checkout_proxy = normalize_proxy(checkout_proxy or proxy)
    stripe_init_proxy = normalize_proxy(stripe_init_proxy or proxy)
    stripe_confirm_proxy = normalize_proxy(stripe_confirm_proxy or stripe_init_proxy or proxy)
    checkout = create_checkout(
        access_token,
        proxy=checkout_proxy,
        country=country,
        currency=currency,
        checkout_ui_mode=checkout_ui_mode or "hosted",
        promo_campaign_id=promo_campaign_id,
        timeout=timeout,
        auth_context=auth_context,
    )
    if not checkout.get("ok"):
        status = int(checkout.get("status") or 0)
        code = "checkout_unauthorized" if status == 401 else "checkout_failed"
        return {
            "ok": False,
            "error": f"gpt-pp checkout failed: status={status} {checkout.get('error') or ''}".strip(),
            "error_code": code,
            "terminal": status in {400, 401, 422},
            "retryable": status not in {400, 401, 422},
            "source": "gpt_pp_core",
            "link_type": "gpt_pp_paypal_authorize",
            "payment_method": "paypal",
            "stage_proxies": {"checkout": checkout_proxy or "DIRECT", "stripe_init": stripe_init_proxy or "DIRECT", "confirm": stripe_confirm_proxy or "DIRECT"},
        }
    hosted = str(checkout.get("hosted_checkout_url") or "")
    checkout_pk = str(checkout.get("publishable_key") or publishable_key or "")
    pk, cs, init, init_error = fetch_checkout_init(hosted, proxy=stripe_init_proxy, publishable_key=checkout_pk, timeout=min(timeout, 15))
    if not (pk and cs and init):
        return {
            "ok": False,
            "error": f"gpt-pp Stripe init failed: {init_error}",
            "error_code": "stripe_init_failed",
            "terminal": False,
            "retryable": True,
            "source": "gpt_pp_core",
            "link_type": "gpt_pp_paypal_authorize",
            "payment_method": "paypal",
            "checkout_url": hosted,
            "hosted_checkout_url": hosted,
            "cs_id": cs or checkout.get("checkout_session_id") or "",
            "stage_proxies": {"checkout": checkout_proxy or "DIRECT", "stripe_init": stripe_init_proxy or "DIRECT", "confirm": stripe_confirm_proxy or "DIRECT"},
        }
    if hosted:
        init.setdefault("url", hosted)
    gate = verify_checkout(init, require_zero=require_zero)
    if not gate.get("ok"):
        return {
            "ok": False,
            "error": str(gate.get("message") or "gpt-pp checkout guard failed"),
            "error_code": str(gate.get("code") or "checkout_guard_failed"),
            "terminal": True,
            "retryable": False,
            "source": "gpt_pp_core",
            "link_type": "gpt_pp_paypal_authorize",
            "payment_method": "paypal",
            "checkout_url": hosted,
            "hosted_checkout_url": hosted,
            "cs_id": cs,
            "amount_due": gate.get("amount_due"),
            "currency": gate.get("currency") or currency.lower(),
            "zero_due_verified": bool(gate.get("zero_verified")),
            "stage_proxies": {"checkout": checkout_proxy or "DIRECT", "stripe_init": stripe_init_proxy or "DIRECT", "confirm": stripe_confirm_proxy or "DIRECT"},
        }
    confirm = confirm_paypal_authorize(
        pk=pk,
        cs=cs,
        init=init,
        proxy=stripe_confirm_proxy,
        require_zero=require_zero,
        country=country,
        timeout=min(timeout, 15),
    )
    amount_due = gate.get("amount_due") if isinstance(gate, dict) else None
    cur = str(gate.get("currency") or currency).lower() if isinstance(gate, dict) else str(currency).lower()
    pm_url = str(confirm.get("pm_authorize_url") or "")
    ok = bool(confirm.get("ok") and pm_url)
    result = {
        "ok": ok,
        "url": pm_url,
        "stripe_redirect_url": pm_url,
        "checkout_url": hosted,
        "hosted_checkout_url": hosted,
        "provider_url": hosted,
        "link_type": "gpt_pp_paypal_authorize",
        "source": "gpt_pp_core",
        "method": "paypal",
        "payment_method": "paypal",
        "cs_id": cs,
        "session_id": cs,
        "processor_entity": checkout.get("processor_entity") or "",
        "pm_id": "",
        "due": amount_due,
        "amount_due": amount_due,
        "currency": cur.upper(),
        "expected_amount": str(amount_due if amount_due is not None else 0),
        "zero_due_verified": bool(gate.get("zero_verified")) if isinstance(gate, dict) else False,
        "amount_display": amount_display(amount_due if isinstance(amount_due, int) else None, cur),
        "payment_method_types": init.get("payment_method_types") if isinstance(init.get("payment_method_types"), list) else [],
        "has_paypal": True,
        "promo_campaign_id": promo_campaign_id,
        "checkout_ui_mode": checkout_ui_mode or "hosted",
        "link_mode": "stripe_redirect",
        "redirect_url_format": "stripe_authorize",
        "stripe_publishable_key_source": "checkout_or_default",
        "region": f"{country.upper()} ({currency.upper()})",
        "billing_country": country.upper(),
        "proxy": normalize_proxy(proxy),
        "stage_proxies": {"checkout": checkout_proxy or "DIRECT", "stripe_init": stripe_init_proxy or "DIRECT", "payment_method": "INLINE_CONFIRM", "confirm": stripe_confirm_proxy or "DIRECT"},
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if not ok:
        result.update({
            "error": str(confirm.get("message") or "gpt-pp Stripe confirm did not return PayPal authorize URL"),
            "error_code": str(confirm.get("code") or "stripe_confirm_missing_redirect"),
            "terminal": int(confirm.get("status") or 0) in {400, 402, 409, 422},
            "retryable": int(confirm.get("status") or 0) not in {400, 402, 409, 422},
            "confirm_summary": {
                "status": confirm.get("status"),
                "code": confirm.get("code"),
                "setup_intent_status": (confirm.get("response") or {}).get("setup_intent", {}).get("status") if isinstance(confirm.get("response"), dict) and isinstance((confirm.get("response") or {}).get("setup_intent"), dict) else "",
            },
        })
    return result
