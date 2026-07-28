#!/usr/bin/env python3
"""Safely probe ChatGPT trial eligibility and Stripe MoMo QR availability.

独立模块：不并入 PP 批量提链 / 协议开通主线。
流程：Checkout(VN/VND) → init(+update rezero到0) → momo PM → confirm
     → ChatGPT approve → setup_intent redirect → payment.momo.vn 可扫 QR。
默认不打印账号凭据 / email / cs_id / 代理原文。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import ac_paylink_core as paylink


ROOT = Path(__file__).resolve().parent
DEFAULT_PROXY_FILE = ROOT / "upi" / "proxy_seeds_vn_200_http.txt"
CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
DEFAULT_STRIPE_PK = paylink.DEFAULT_STRIPE_PK
COUNTRY_CURRENCY = dict(paylink.COUNTRY_CURRENCY)
COUNTRY_CURRENCY.setdefault("VN", "VND")

DECISION_TEXT = {
    "ready": "支持 MoMo 且已 0 元",
    "momo_supported": "支持 MoMo 且已 0 元（可出码）",
    "ready_with_qr": "0 元 + 已抽出可扫 MoMo 付款码",
    "momo_qr_failed": "0 元且支持 MoMo，但未拿到 payment.momo.vn/真QR（Stripe中间页不算）",
    "promo_nonzero": "有 MoMo 但未刷到 0 元，正价盘强制失败",
    "account_trial_ineligible": "账号没有真正试用资格，且未检测到 MoMo",
    "trial_not_applied": "trial 未生效，且未检测到 MoMo",
    "momo_not_enabled": "当前 Session 未启用 MoMo",
    "card_only_full_price": "仅 card 正价盘，无 MoMo/无 0 元",
    "already_paid": "账号已订阅，无法用此流程检测新订阅资格",
    "credential_invalid": "凭证无效或已过期",
    "credential_parse_failed": "无法从文件解析凭证",
    "checkout_failed": "Checkout 创建失败，结果不确定",
    "stripe_init_failed": "Checkout 已创建，但 Stripe init 失败",
    "payment_methods_unknown": "Stripe init 未返回明确的支付方式列表",
    "unexpected_mode": "Stripe Session 不是 subscription 模式",
    "credential_ready": "凭证格式有效，尚未联网检测",
}
CHECKOUT_UPDATE_URL = "https://chatgpt.com/backend-api/payments/checkout/update"
DEFAULT_PROMO_ID = "plus-1-month-free"

MOMO_BILLING = {
    "name": "Nguyen Van A",
    "email": "buyer.vn@example.com",
    "country": "VN",
    "line1": "1 Nguyen Hue",
    "city": "Ho Chi Minh City",
    "postal_code": "700000",
    "state": "Ho Chi Minh",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="脱敏检测 ChatGPT 账号的真正试用资格与 Stripe MoMo 支持情况。",
    )
    parser.add_argument(
        "token_files",
        nargs="+",
        type=Path,
        metavar="TOKEN_FILE",
        help="包含 accessToken 的文本或 JSON 文件；可一次传入多个。",
    )
    parser.add_argument(
        "--proxy-file",
        type=Path,
        default=DEFAULT_PROXY_FILE,
        help=f"越南代理列表（默认：{DEFAULT_PROXY_FILE}）。",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="不使用代理文件，直接连接。",
    )
    parser.add_argument(
        "--pre-proxy",
        default="auto",
        help=(
            "上游 SOCKS/HTTP 代理；默认 auto 会在本机 127.0.0.1:7897 可用时自动使用。"
            "传 off 可禁用。"
        ),
    )
    parser.add_argument(
        "--trial-days",
        type=int,
        default=30,
        help="请求的试用天数（默认：30）。",
    )
    parser.add_argument(
        "--max-proxies",
        type=int,
        default=1,
        help="仅遇到明确 Cloudflare 拦截时切换代理的上限（默认：1，不重试）。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="单个网络请求超时秒数（默认：20）。",
    )
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="只验证凭证格式和有效期，不发送任何网络请求。",
    )
    parser.add_argument(
        "--check-methods-anyway",
        action="store_true",
        help="即使账号试用资格为 false，也继续调用一次 Stripe init 检查支付方式。",
    )
    parser.add_argument(
        "--emit-qr",
        action="store_true",
        default=True,
        help="Session 支持 MoMo 时继续 create PM + confirm，抽出二维码（默认开）。",
    )
    parser.add_argument(
        "--no-emit-qr",
        action="store_true",
        help="只探测资格/支付方式，不创建 PM、不 confirm、不出码。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="每个账号输出一行 JSON，便于其他程序处理。",
    )
    args = parser.parse_args()
    if args.trial_days < 1:
        parser.error("--trial-days 必须大于 0")
    if args.max_proxies < 1:
        parser.error("--max-proxies 必须大于 0")
    if args.timeout < 1:
        parser.error("--timeout 必须大于 0")
    return args


def account_label(index: int) -> str:
    if 0 <= index < 26:
        return chr(ord("A") + index)
    return f"#{index + 1}"


def jwt_expiry(access_token: str) -> tuple[bool | None, float | None]:
    parts = access_token.split(".")
    if len(parts) != 3:
        return None, None
    try:
        payload_raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(payload_raw)
        exp = payload.get("exp")
        if exp is None:
            return None, None
        ttl_minutes = round((float(exp) - time.time()) / 60, 1)
        return ttl_minutes <= 0, ttl_minutes
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, None


def normalize_token(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    access = ""
    session = ""
    try:
        access = paylink.extract_access_token(text)
    except Exception:
        access = ""
    try:
        session = paylink.extract_session_token(text)
    except Exception:
        session = ""
    if access and access.lstrip().startswith(("{", "[")):
        access = ""
    if not access:
        match = re.search(
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
            text,
        )
        if match:
            access = match.group(0)
    return access, session


def load_credential(path: Path) -> tuple[str, str, dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "", "", {
            "credential_valid": False,
            "decision": "credential_parse_failed",
        }
    stripped = raw.strip()
    candidates = [stripped]
    if stripped.startswith(("{", "[")) and stripped.endswith(","):
        candidates.insert(0, stripped[:-1].rstrip())

    access_token = ""
    session_token = ""
    for candidate in candidates:
        parsed_access, parsed_session = normalize_token(candidate)
        if parsed_access and not parsed_access.lstrip().startswith(("{", "[")):
            access_token, session_token = parsed_access, parsed_session
            break
    if not access_token:
        return "", "", {
            "credential_valid": False,
            "decision": "credential_parse_failed",
        }
    expired, _ttl_minutes = jwt_expiry(access_token)
    return access_token, session_token, {
        "credential_valid": expired is not True,
        "credential_expired": expired,
        "decision": "credential_invalid" if expired is True else "credential_ready",
    }


def load_credential_text(raw: str) -> tuple[str, str, dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return "", "", {
            "credential_valid": False,
            "decision": "credential_parse_failed",
        }
    access_token, session_token = normalize_token(text)
    if not access_token:
        return "", "", {
            "credential_valid": False,
            "decision": "credential_parse_failed",
        }
    expired, _ttl_minutes = jwt_expiry(access_token)
    return access_token, session_token, {
        "credential_valid": expired is not True,
        "credential_expired": expired,
        "decision": "credential_invalid" if expired is True else "credential_ready",
    }


def local_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def configure_pre_proxy(value: str) -> None:
    normalized = value.strip()
    if normalized.lower() == "auto":
        normalized = (
            "socks5h://127.0.0.1:7897"
            if local_port_open("127.0.0.1", 7897)
            else ""
        )
    elif normalized.lower() in {"", "off", "none", "false", "0"}:
        normalized = ""
    if normalized:
        os.environ["IDEAL_PRE_PROXY"] = normalized
        os.environ["PP_PRE_PROXY"] = normalized
    else:
        os.environ.pop("IDEAL_PRE_PROXY", None)
        os.environ.pop("PP_PRE_PROXY", None)


def normalize_proxy_url(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    # udeal residential sticky lines are SOCKS5H in this environment.
    # Forcing http:// makes chatgpt checkout hang until timeout.
    lower = value.lower()
    if "://" not in value and "udealproxy.com" in lower:
        parts = value.split(":")
        if len(parts) >= 4:
            host, port = parts[0], parts[1]
            user = ":".join(parts[2:-1])
            password = parts[-1]
            return f"socks5h://{user}:{password}@{host}:{port}"
    try:
        return paylink.normalize_proxy_url(value)
    except Exception:
        if "://" in value:
            return value
        return value


def load_proxies(path: Path, direct: bool) -> list[str]:
    if direct:
        return [""]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("代理文件无法读取") from exc
    proxies = [
        normalize_proxy_url(line.strip())
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not proxies:
        raise RuntimeError("代理文件中没有可用条目")
    return proxies


def proxy_for_vietnam(proxy: str) -> str:
    return normalize_proxy_url(proxy)


def is_cloudflare_response(text: str, status_code: int = 0) -> bool:
    body = str(text or "").lower()
    if status_code in {403, 503} and (
        "cloudflare" in body
        or "cf-ray" in body
        or "just a moment" in body
        or "attention required" in body
    ):
        return True
    return "cloudflare" in body and (
        "just a moment" in body or "attention required" in body or "cf-ray" in body
    )


def is_user_already_paid_error(text: str) -> bool:
    body = str(text or "").lower()
    markers = (
        "already_subscribed",
        "already subscribed",
        "user_already_has",
        "active_subscription",
        "subscription_already",
        "already_paid",
        "has an active plus",
        "already on a paid plan",
    )
    return any(marker in body for marker in markers)


def checkout_body(trial_days: int, strategy: str = "hosted_promo") -> dict[str, Any]:
    """Build VN/VND checkout payloads.

    Live success sample used a ₫0 promo disk with momo on hosted checkout.
    We try multiple strategies because custom+trial_period_days often lands
    card-only full-price sessions on this account pool.
    """
    strategy = str(strategy or "hosted_promo").strip().lower()
    base: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": "VN", "currency": "VND"},
        "cancel_url": "https://chatgpt.com/",
    }
    if strategy in {"hosted_promo", "hosted", "promo"}:
        base["checkout_ui_mode"] = "hosted"
        base["promo_campaign"] = {
            "promo_campaign_id": DEFAULT_PROMO_ID,
            "is_coupon_from_query_param": False,
        }
        return base
    if strategy in {"custom_promo", "custom+promo"}:
        base["checkout_ui_mode"] = "custom"
        base["promo_campaign"] = {
            "promo_campaign_id": DEFAULT_PROMO_ID,
            "is_coupon_from_query_param": False,
        }
        return base
    # legacy/custom trial days
    base["checkout_ui_mode"] = "custom"
    base["subscription_data"] = {"trial_period_days": int(trial_days or 30)}
    return base


CHECKOUT_STRATEGIES = ("hosted_promo", "custom_promo", "custom_trial")


class _HttpResponse:
    def __init__(self, status_code: int, text: str, data: Any = None):
        self.status_code = int(status_code or 0)
        self.text = str(text or "")
        self._data = data

    def json(self) -> Any:
        if self._data is not None:
            return self._data
        return json.loads(self.text or "{}")


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    proxy: str = "",
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, Any] | None = None,
    timeout: int = 20,
) -> _HttpResponse:
    proxy_url = normalize_proxy_url(proxy)
    pre_proxy = str(
        os.environ.get("IDEAL_PRE_PROXY")
        or os.environ.get("PP_PRE_PROXY")
        or ""
    ).strip()
    effective_proxy = proxy_url or pre_proxy
    timeout = max(5, int(timeout or 20))

    # Prefer curl_cffi when available (better TLS fingerprint).
    try:
        from curl_cffi import requests as curl_requests  # type: ignore
    except Exception:
        curl_requests = None  # type: ignore

    if curl_requests is not None:
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "impersonate": "chrome",
        }
        if effective_proxy:
            kwargs["proxies"] = {"http": effective_proxy, "https": effective_proxy}
        if json_body is not None:
            resp = curl_requests.request(method.upper(), url, json=json_body, **kwargs)
        else:
            resp = curl_requests.request(method.upper(), url, data=form_body, **kwargs)
        return _HttpResponse(resp.status_code, resp.text)

    import requests  # type: ignore

    kwargs = {"headers": headers, "timeout": timeout}
    if effective_proxy:
        kwargs["proxies"] = {"http": effective_proxy, "https": effective_proxy}
    if json_body is not None:
        resp = requests.request(method.upper(), url, json=json_body, **kwargs)
    else:
        resp = requests.request(method.upper(), url, data=form_body, **kwargs)
    return _HttpResponse(resp.status_code, resp.text)


def post_chatgpt_checkout(
    access_token: str,
    session_token: str,
    proxy: str,
    trial_days: int,
    timeout: int,
    strategy: str = "hosted_promo",
) -> _HttpResponse:
    headers = paylink.build_headers(
        access_token,
        session_token=session_token,
        language="vi-VN",
    )
    headers.update(
        {
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        }
    )
    return _request_json(
        "POST",
        CHECKOUT_URL,
        headers=headers,
        proxy=proxy,
        json_body=checkout_body(trial_days, strategy=strategy),
        timeout=timeout,
    )


def classify_checkout_error(response: _HttpResponse) -> str:
    if is_user_already_paid_error(response.text):
        return "already_paid"
    if is_cloudflare_response(response.text, response.status_code):
        return "cloudflare"
    if response.status_code == 401:
        return "credential_invalid"
    if response.status_code == 429:
        return "rate_limited"
    return f"http_{response.status_code}"


def create_checkout(
    access_token: str,
    session_token: str,
    proxies: list[str],
    start_index: int,
    max_proxies: int,
    trial_days: int,
    timeout: int,
    strategy: str = "hosted_promo",
) -> tuple[dict[str, Any] | None, str, str, str, int, int]:
    last_error = "no_attempt"
    attempts = 0
    for offset in range(min(max_proxies, len(proxies) or 1)):
        proxy_index = (start_index + offset) % max(1, len(proxies) or 1)
        proxy = proxy_for_vietnam(proxies[proxy_index] if proxies else "")
        attempts += 1
        try:
            response = post_chatgpt_checkout(
                access_token,
                session_token,
                proxy,
                trial_days,
                timeout,
                strategy=strategy,
            )
            if response.status_code >= 400:
                last_error = classify_checkout_error(response)
                if last_error in {"already_paid", "credential_invalid"}:
                    return None, "", "", last_error, attempts, proxy_index + 1
                # Rotate sticky on cloudflare / rate-limit / soft network blocks.
                if last_error in {"cloudflare", "rate_limited"} and offset + 1 < min(
                    max_proxies, len(proxies) or 1
                ):
                    continue
                if last_error.startswith("http_") and offset + 1 < min(
                    max_proxies, len(proxies) or 1
                ):
                    continue
                return None, "", "", last_error, attempts, proxy_index + 1
            data = response.json() or {}
            if not isinstance(data, dict):
                last_error = "checkout_non_object"
                continue
            checkout_id = (
                data.get("checkout_session_id")
                or data.get("session_id")
                or data.get("id")
            )
            if not checkout_id or not str(checkout_id).startswith("cs_"):
                last_error = "checkout_missing_session"
                continue
            data["_checkout_strategy"] = strategy
            raw_key = (
                data.get("stripe_publishable_key")
                or data.get("publishable_key")
                or data.get("publishableKey")
                or data.get("stripePublishableKey")
                or data.get("key")
                or ""
            )
            key_match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_key))
            stripe_key = key_match.group(0) if key_match else DEFAULT_STRIPE_PK
            return data, str(checkout_id), stripe_key, proxy, attempts, proxy_index + 1
        except Exception as exc:
            last_error = f"network_{type(exc).__name__}"
            # Timeout / TLS on one sticky should rotate, not freeze the whole probe.
            if offset + 1 < min(max_proxies, len(proxies)):
                continue
            return None, "", "", last_error, attempts, proxy_index + 1
    return None, "", "", last_error, attempts, start_index


def stripe_init(
    checkout_id: str,
    stripe_key: str,
    selected_proxy: str,
    timeout: int = 20,
) -> tuple[dict[str, Any] | None, str, int]:
    try:
        payload = paylink.stripe_init_payment_page(
            checkout_id,
            stripe_key,
            proxy=selected_proxy,
            timeout=float(timeout),
        )
        return payload, "ok", 1
    except Exception as exc:
        return None, f"network_or_init_{type(exc).__name__}", 1


def processor_entity_of(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "openai_llc"
    for node in (
        payload,
        payload.get("checkout_session"),
        payload.get("session"),
        payload.get("checkout"),
        payload.get("data"),
    ):
        if isinstance(node, dict):
            value = str(node.get("processor_entity") or node.get("processorEntity") or "").strip()
            if value:
                return value
    # walk shallow strings for entity-looking tokens
    for key in ("processor_entity", "processorEntity", "entity"):
        value = str(payload.get(key) or "").strip()
        if value.startswith("openai_"):
            return value
    return "openai_llc"


def apply_promo_update(
    access_token: str,
    session_token: str,
    checkout_id: str,
    processor_entity: str,
    proxy: str,
    timeout: int = 20,
) -> tuple[bool, str]:
    """Force plus-1-month-free onto an existing cs. Returns (ok, status)."""
    if not checkout_id.startswith("cs_"):
        return False, "bad_checkout"
    entity = str(processor_entity or "openai_llc").strip() or "openai_llc"
    headers = paylink.build_headers(
        access_token,
        session_token=session_token,
        language="vi-VN",
    )
    path = "/backend-api/payments/checkout/update"
    headers.update(
        {
            "Referer": f"https://chatgpt.com/checkout/{entity}/{checkout_id}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        }
    )
    body = {
        "checkout_session_id": checkout_id,
        "processor_entity": entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": DEFAULT_PROMO_ID,
            "is_coupon_from_query_param": False,
        },
    }
    try:
        resp = _request_json(
            "POST",
            CHECKOUT_UPDATE_URL,
            headers=headers,
            proxy=proxy,
            json_body=body,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            return False, f"update_http_{resp.status_code}"
        data = resp.json() or {}
        if isinstance(data, dict) and data.get("success") is False:
            return False, "update_rejected"
        return True, "ok"
    except Exception as exc:
        return False, f"update_network_{type(exc).__name__}"


def extract_methods(payload: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    methods = payload.get("payment_method_types")
    source = "top_level"
    if not isinstance(methods, list):
        elements = payload.get("elements_options")
        methods = elements.get("payment_method_types") if isinstance(elements, dict) else None
        source = "elements_options"
    if not isinstance(methods, list):
        return None, None
    return sorted({str(method).lower() for method in methods}), source


def stripe_field(payload: dict[str, Any], key: str) -> Any:
    elements = payload.get("elements_options")
    if isinstance(elements, dict) and key in elements:
        return elements[key]
    return payload.get(key)


def amount_due(payload: dict[str, Any]) -> int | None:
    summary = payload.get("total_summary")
    if isinstance(summary, dict) and summary.get("due") is not None:
        return int(summary.get("due") or 0)
    if payload.get("amount_total") is not None:
        return int(payload.get("amount_total") or 0)
    amount = stripe_field(payload, "amount")
    if amount is not None:
        return int(amount or 0)
    invoice = payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return int(invoice.get("amount_due") or 0)
    return None


def trial_marker(
    payload: dict[str, Any], nested_key: str | None = None
) -> tuple[bool, Any, bool]:
    candidates = [payload]
    if nested_key and isinstance(payload.get(nested_key), dict):
        candidates.append(payload[nested_key])
    trial_days = None
    trial_end = None
    for candidate in candidates:
        subscription_data = candidate.get("subscription_data")
        if isinstance(subscription_data, dict):
            trial_days = subscription_data.get("trial_period_days")
            trial_end = subscription_data.get("trial_end")
        if trial_days in (None, "", 0, "0", False):
            trial_days = candidate.get("trial_period_days")
        if trial_end in (None, "", 0, "0", False):
            trial_end = candidate.get("trial_end")
        if trial_days not in (None, "", 0, "0", False) or trial_end not in (
            None,
            "",
            0,
            "0",
            False,
        ):
            break
    try:
        has_days = int(trial_days or 0) > 0
    except (TypeError, ValueError):
        has_days = False
    has_end = trial_end not in (None, "", 0, "0", False)
    return has_days or has_end, trial_days, has_end


def has_actual_trial_in_response(payload: dict[str, Any]) -> bool:
    has_trial, _, _ = trial_marker(payload, "checkout_session")
    return has_trial


def choose_decision(
    one_click_eligible: Any,
    actual_trial: bool,
    stripe_mode: Any,
    has_momo: bool | None,
    *,
    amount_due_cents: int | None = None,
) -> str:
    """Decide after checkout+init(+promo rezero).

    HARD GATE: amount_due must be 0 for any MoMo success.
    Non-zero + momo => promo_nonzero (强制失败，不出码).
    """
    if stripe_mode not in (None, "", "subscription") and stripe_mode != "subscription":
        if stripe_mode not in {"subscription"}:
            return "unexpected_mode"
    if has_momo is None:
        return "payment_methods_unknown"
    zero = amount_due_cents == 0
    if has_momo is True:
        if zero:
            return "ready"
        return "promo_nonzero"
    # no momo
    if amount_due_cents not in (None, 0) and not actual_trial and one_click_eligible is not True:
        return "card_only_full_price"
    if not actual_trial and one_click_eligible is False:
        return "account_trial_ineligible"
    if not actual_trial:
        return "trial_not_applied"
    return "momo_not_enabled"


def _iter_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_values(item)
    else:
        yield value


def extract_momo_qr_payload(payload: Any) -> dict[str, Any]:
    """Pull MoMo QR / pay-url fields from Stripe confirm tree.

    Accept only real artifacts:
    - http(s) momo/stripe pay URLs
    - data:image QR
    - long vietQR / payment payload strings
    Never accept bare tokens like "momo".
    """
    out: dict[str, Any] = {
        "qr_data": "",
        "qr_image_url": "",
        "qr_png_url": "",
        "hosted_instructions_url": "",
        "next_action_type": "",
        "expires_at": None,
    }
    if not isinstance(payload, dict):
        return out

    junk = {
        "momo",
        "card",
        "paypal",
        "link",
        "qr",
        "code",
        "data",
        "null",
        "none",
        "undefined",
        "true",
        "false",
    }

    def _is_pm_icon(text: str) -> bool:
        # Stripe/payment-method brand icons are NOT QR codes.
        low = str(text or "").lower()
        return any(
            x in low
            for x in (
                "icon-pm-",
                "/payment-methods/",
                "fingerprinted/img/payment-methods",
                "brand-icon",
                "pm-icon",
            )
        )

    def _usable_pay_text(text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        low = t.lower()
        if low in junk or _is_pm_icon(t):
            return False
        if t.startswith(("http://", "https://", "data:image")):
            return True
        if "payment.momo.vn" in low or "momo.vn" in low or "vietqr" in low:
            return len(t) >= 16
        # raw QR payloads are usually long
        if low.startswith("2|99|") or low.startswith("000201"):
            return len(t) >= 24
        return len(t) >= 32 and any(x in low for x in ("momo", "qr", "pay", "iban", "http"))

    def _usable_qr_image(text: str) -> bool:
        t = str(text or "").strip()
        if not t or _is_pm_icon(t):
            return False
        if t.startswith("data:image"):
            return True
        low = t.lower()
        # Real QR image URLs usually say qr / voucher / code; reject bare brand assets.
        if t.startswith(("http://", "https://")):
            return any(x in low for x in ("qr", "voucher", "barcode", "display")) and "payment-methods" not in low
        return False

    def _prefer(old: str, new: str) -> str:
        if not new:
            return old
        if not old:
            return new
        # prefer URLs / images over plain text; longer otherwise
        if old.startswith(("http://", "https://", "data:image")) and not new.startswith(("http://", "https://", "data:image")):
            return old
        if new.startswith(("http://", "https://", "data:image")) and not old.startswith(("http://", "https://", "data:image")):
            return new
        return new if len(new) > len(old) else old

    def _walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            ntype = str(node.get("type") or "").strip()
            if ntype and not out["next_action_type"]:
                if "qr" in ntype.lower() or ntype in {
                    "display_qr_code",
                    "momo_display_qr_code",
                    "redirect_to_url",
                    "oxxo_display_details",
                    "konbini_display_details",
                }:
                    out["next_action_type"] = ntype
            for key, val in node.items():
                key_l = str(key).lower()
                if isinstance(val, str):
                    text = val.strip()
                    if not text:
                        continue
                    if key_l in {
                        "image_url_png",
                        "image_url_svg",
                        "qr_image_url",
                        "image_url",
                        "png",
                        "svg",
                    }:
                        if _usable_qr_image(text):
                            if "png" in key_l or text.endswith(".png") or "png" in text or text.startswith("data:image/png"):
                                out["qr_png_url"] = out["qr_png_url"] or text
                            out["qr_image_url"] = out["qr_image_url"] or text
                    if key_l in {
                        "hosted_voucher_url",
                        "hosted_instructions_url",
                        "mobile_auth_url",
                        "url",
                        "redirect_url",
                    }:
                        if text.startswith(("http://", "https://")) and not _is_pm_icon(text):
                            out["hosted_instructions_url"] = _prefer(out["hosted_instructions_url"], text)
                            if any(x in text.lower() for x in ("momo", "qr", "checkout.stripe.com", "pay.openai.com", "payment.momo")):
                                out["qr_data"] = _prefer(out["qr_data"], text)
                    if key_l in {"data", "qr_data", "qr_code", "qrcode", "payload", "value"}:
                        if _usable_pay_text(text):
                            if text.startswith(("http://", "https://")) and any(
                                x in text.lower() for x in ("momo", "pay", "qr")
                            ):
                                out["hosted_instructions_url"] = _prefer(out["hosted_instructions_url"], text)
                            if text.startswith("data:image"):
                                out["qr_image_url"] = out["qr_image_url"] or text
                            out["qr_data"] = _prefer(out["qr_data"], text)
                    if key_l in {"expires_at", "expires_after"} and out["expires_at"] is None:
                        out["expires_at"] = text
                    # bare long pay strings only
                    if _usable_pay_text(text) and (
                        "momo" in text.lower()
                        or "vietqr" in text.lower()
                        or text.startswith(("http://", "https://", "data:image", "2|99|", "000201"))
                    ):
                        if text.startswith(("http://", "https://")):
                            out["hosted_instructions_url"] = _prefer(out["hosted_instructions_url"], text)
                            if text.startswith("data:image") or text.lower().endswith((".png", ".svg")):
                                out["qr_image_url"] = out["qr_image_url"] or text
                            else:
                                out["qr_data"] = _prefer(out["qr_data"], text)
                        elif text.startswith("data:image"):
                            out["qr_image_url"] = out["qr_image_url"] or text
                        else:
                            out["qr_data"] = _prefer(out["qr_data"], text)
                else:
                    _walk(val, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for item in node:
                _walk(item, path)

    next_action = payload.get("next_action")
    if isinstance(next_action, dict):
        out["next_action_type"] = str(next_action.get("type") or "") or out["next_action_type"]
        _walk(next_action, "next_action")
        if next_action.get("type") == "redirect_to_url":
            redir = next_action.get("redirect_to_url") or {}
            if isinstance(redir, dict):
                url = str(redir.get("url") or "").strip()
                if url.startswith(("http://", "https://")):
                    out["hosted_instructions_url"] = _prefer(out["hosted_instructions_url"], url)
                    out["qr_data"] = _prefer(out["qr_data"], url)
        # common display_qr_code shape
        for nest_key in ("display_qr_code", "momo_display_qr_code", "oxxo_display_details"):
            nest = next_action.get(nest_key)
            if isinstance(nest, dict):
                _walk(nest, nest_key)
    for key in ("setup_intent", "payment_intent", "submission_attempt"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            _walk(nested, key)
            na = nested.get("next_action")
            if isinstance(na, dict):
                if na.get("type") == "redirect_to_url":
                    redir = na.get("redirect_to_url") or {}
                    if isinstance(redir, dict):
                        url = str(redir.get("url") or "").strip()
                        if url.startswith(("http://", "https://")):
                            out["hosted_instructions_url"] = _prefer(out["hosted_instructions_url"], url)
                            out["qr_data"] = _prefer(out["qr_data"], url)
    _walk(payload, "root")

    # Final sanitization: drop junk leftovers + brand icons
    for k in ("qr_data", "qr_image_url", "qr_png_url", "hosted_instructions_url"):
        val = str(out.get(k) or "").strip()
        bad = (
            not val
            or val.lower() in junk
            or _is_pm_icon(val)
            or (k in {"qr_image_url", "qr_png_url"} and not _usable_qr_image(val))
            or (k == "qr_data" and not _usable_pay_text(val) and not val.startswith(("http://", "https://", "data:image")))
        )
        out[k] = "" if bad else val

    if out["qr_png_url"] and not out["qr_image_url"]:
        out["qr_image_url"] = out["qr_png_url"]
    if out["qr_image_url"] and not out["qr_png_url"] and (
        "png" in out["qr_image_url"].lower() or out["qr_image_url"].startswith("data:image/png")
    ):
        out["qr_png_url"] = out["qr_image_url"]
    # Prefer real pay chain over empty qr_data (checkout.stripe / momo.vn / openai pay)
    if not out["qr_data"] and out["hosted_instructions_url"]:
        out["qr_data"] = out["hosted_instructions_url"]
    return out


def _stripe_form_post(
    url: str,
    form: dict[str, Any],
    *,
    proxy: str = "",
    timeout: int = 20,
) -> _HttpResponse:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": paylink.DEFAULT_USER_AGENT,
    }
    return _request_json(
        "POST",
        url,
        headers=headers,
        proxy=proxy,
        form_body=form,
        timeout=timeout,
    )


def create_momo_payment_method(
    checkout_id: str,
    stripe_key: str,
    *,
    proxy: str = "",
    timeout: int = 20,
    billing: dict[str, str] | None = None,
) -> tuple[str, str]:
    bill = dict(MOMO_BILLING)
    if isinstance(billing, dict):
        bill.update({k: str(v) for k, v in billing.items() if v is not None})
    form = {
        "type": "momo",
        "billing_details[name]": bill["name"],
        "billing_details[email]": bill["email"],
        "billing_details[address][country]": bill["country"],
        "billing_details[address][line1]": bill["line1"],
        "billing_details[address][city]": bill["city"],
        "billing_details[address][postal_code]": bill["postal_code"],
        "billing_details[address][state]": bill["state"],
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "key": stripe_key or DEFAULT_STRIPE_PK,
    }
    resp = _stripe_form_post(
        "https://api.stripe.com/v1/payment_methods",
        form,
        proxy=proxy,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        return "", f"pm_http_{resp.status_code}"
    try:
        data = resp.json() or {}
    except Exception:
        return "", "pm_bad_json"
    pm_id = str((data or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        return "", "pm_missing_id"
    return pm_id, "ok"


def confirm_momo_payment_page(
    checkout_id: str,
    stripe_key: str,
    pm_id: str,
    init_payload: dict[str, Any],
    *,
    proxy: str = "",
    timeout: int = 25,
    billing: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Confirm momo on payment_pages. Returns (payload|None, status, raw_error_snip).

    IMPORTANT: do NOT send top-level billing_details / payment_method_types —
    Stripe returns parameter_unknown (use payment_method=pm_ only; billing is on PM).
    """
    expected = amount_due(init_payload)
    expected_amount = "0" if expected is None else str(int(expected))
    init_checksum = str(init_payload.get("init_checksum") or "")
    return_url = f"https://chatgpt.com/?cs={checkout_id}"
    config_id = str(init_payload.get("config_id") or "")
    # runtime version from live PP confirm evidence (not stale fed52f3bc6)
    runtime_version = str(
        os.environ.get("PP_STRIPE_RUNTIME_VERSION")
        or os.environ.get("MOMO_STRIPE_RUNTIME_VERSION")
        or "6f8494a281"
    ).strip() or "6f8494a281"
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    stripe_js_id = str(uuid.uuid4())
    form = {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method": pm_id,
        "init_checksum": init_checksum,
        "version": runtime_version,
        "expected_amount": expected_amount,
        "expected_payment_method_type": "momo",
        "return_url": return_url,
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[locale]": "vi",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[checkout_config_id]": config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "consent[terms_of_service]": "accepted",
        "key": stripe_key or DEFAULT_STRIPE_PK,
        "_stripe_version": paylink.STRIPE_VERSION_FULL,
    }
    # billing already on pm_ — do not re-send top-level billing_details (400)
    _ = billing  # kept for API compat
    try:
        resp = _stripe_form_post(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
            form,
            proxy=proxy,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            snip = (resp.text or "")[:240].replace("\n", " ")
            # try once more with leaner body if checksum/version issues
            return None, f"confirm_http_{resp.status_code}", snip
        data = resp.json() or {}
        if not isinstance(data, dict):
            return None, "confirm_non_object", ""
        return data, "ok", ""
    except Exception as exc:
        return None, f"confirm_network_{type(exc).__name__}", str(exc)[:120]


def is_scannable_momo_artifact(text: str) -> bool:
    """True only for things a MoMo app can pay: gateway URL, vietQR, real QR image."""
    t = str(text or "").strip()
    if not t:
        return False
    low = t.lower()
    if any(x in low for x in ("icon-pm-", "/payment-methods/", "fingerprinted/img/payment-methods")):
        return False
    # Stripe hosted checkout form is NOT scannable MoMo QR.
    if "checkout.stripe.com" in low or "pay.openai.com" in low:
        return False
    if t.startswith("data:image"):
        return True
    if "payment.momo.vn" in low or "momo.vn" in low:
        return t.startswith(("http://", "https://"))
    if "vietqr" in low or low.startswith("000201") or low.startswith("2|99|"):
        return len(t) >= 24
    if t.startswith(("http://", "https://")) and any(x in low for x in ("/qr", "qrcode", "voucher", "barcode")):
        return "payment-methods" not in low
    return False


def chatgpt_approve_checkout(
    access_token: str,
    session_token: str,
    checkout_id: str,
    processor_entity: str,
    proxy: str = "",
    timeout: int = 30,
) -> tuple[bool, str]:
    """POST /payments/checkout/approve after Stripe confirm (manual approval)."""
    entity = str(processor_entity or "openai_llc").strip() or "openai_llc"
    headers = paylink.build_headers(
        access_token,
        session_token=session_token,
        language="vi-VN",
    )
    headers.update(
        {
            "Referer": f"https://chatgpt.com/checkout/{entity}/{checkout_id}",
            "Origin": "https://chatgpt.com",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        }
    )
    body = {"checkout_session_id": checkout_id, "processor_entity": entity}
    try:
        resp = _request_json(
            "POST",
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            headers=headers,
            proxy=proxy,
            json_body=body,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            return False, f"approve_http_{resp.status_code}"
        data = resp.json() or {}
        result = str((data or {}).get("result") or "").strip().lower()
        if result == "approved":
            return True, "approved"
        return False, f"approve_result_{result or 'empty'}"
    except Exception as exc:
        return False, f"approve_network_{type(exc).__name__}"


def _setup_intent_redirect(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    si = payload.get("setup_intent")
    if isinstance(si, dict):
        na = si.get("next_action")
        if isinstance(na, dict):
            redir = na.get("redirect_to_url")
            if isinstance(redir, dict):
                url = str(redir.get("url") or "").strip()
                if url.startswith("http"):
                    return url
    # also payment_intent
    pi = payload.get("payment_intent")
    if isinstance(pi, dict):
        na = pi.get("next_action")
        if isinstance(na, dict):
            redir = na.get("redirect_to_url")
            if isinstance(redir, dict):
                url = str(redir.get("url") or "").strip()
                if url.startswith("http"):
                    return url
    return ""


def follow_momo_redirect(url: str, proxy: str = "", timeout: int = 40) -> dict[str, Any]:
    """Follow pm-redirects.stripe.com → payment.momo.vn and pull scannable QR."""
    out: dict[str, Any] = {
        "final_url": "",
        "qr_image_url": "",
        "qr_png_url": "",
        "hosted_instructions_url": "",
        "qr_data": "",
        "error": "",
    }
    if not url.startswith("http"):
        out["error"] = "bad_redirect"
        return out
    try:
        from curl_cffi import requests as curl_requests  # type: ignore
    except Exception:
        curl_requests = None  # type: ignore
    headers = {
        "User-Agent": paylink.DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }
    try:
        if curl_requests is not None:
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": timeout,
                "impersonate": "chrome",
                "allow_redirects": True,
            }
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            resp = curl_requests.get(url, **kwargs)
            final = str(getattr(resp, "url", "") or url)
            text = str(resp.text or "")
        else:
            import requests  # type: ignore

            kwargs = {"headers": headers, "timeout": timeout, "allow_redirects": True}
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            resp = requests.get(url, **kwargs)
            final = str(getattr(resp, "url", "") or url)
            text = str(resp.text or "")
    except Exception as exc:
        out["error"] = f"follow_network_{type(exc).__name__}"
        return out

    out["final_url"] = final
    # Gateway URL itself is the pay chain
    if "payment.momo.vn" in final.lower() or "momo.vn" in final.lower():
        out["hosted_instructions_url"] = final
        out["qr_data"] = final
    # Pull embedded QR image (MoMo page uses data:image/png;base64,...)
    m_img = re.search(r"(data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+)", text)
    if m_img:
        img = m_img.group(1)
        out["qr_image_url"] = img
        if "png" in img[:30]:
            out["qr_png_url"] = img
        out["qr_data"] = img  # prefer scannable image payload
    # Also any absolute momo pay URLs in body
    if not out["hosted_instructions_url"]:
        for u in re.findall(r"https?://[^\s\"'<>]+", text):
            if "payment.momo.vn" in u.lower() and "/gateway/pay" in u.lower():
                out["hosted_instructions_url"] = u
                if not out["qr_data"]:
                    out["qr_data"] = u
                break
    if not (out["qr_image_url"] or out["hosted_instructions_url"]):
        out["error"] = f"no_qr_in_redirect final={final[:80]}"
    return out


def emit_momo_qr(
    checkout_id: str,
    stripe_key: str,
    init_payload: dict[str, Any],
    *,
    proxy: str = "",
    timeout: int = 25,
    access_token: str = "",
    session_token: str = "",
    processor_entity: str = "openai_llc",
) -> dict[str, Any]:
    """Full path to scannable MoMo QR:

    PM → confirm → ChatGPT approve → re-init setup_intent redirect
      → follow pm-redirects → payment.momo.vn (+ data:image QR)
    """
    info: dict[str, Any] = {
        "qr_status": "skipped",
        "qr_error": "",
        "has_qr": False,
        "qr_data": "",
        "qr_image_url": "",
        "qr_png_url": "",
        "hosted_instructions_url": "",
        "next_action_type": "",
        "qr_expires_at": None,
        "pm_status": "",
        "confirm_status": "",
        "approve_status": "",
        "middle_checkout_url": "",
        "redirect_url": "",
    }
    if not checkout_id.startswith("cs_"):
        info["qr_status"] = "bad_checkout"
        info["qr_error"] = "invalid_checkout_id"
        return info

    pm_id, pm_status = create_momo_payment_method(
        checkout_id, stripe_key, proxy=proxy, timeout=timeout
    )
    info["pm_status"] = pm_status
    if not pm_id:
        info["qr_status"] = "pm_failed"
        info["qr_error"] = pm_status
        return info

    confirm_payload, confirm_status, confirm_snip = confirm_momo_payment_page(
        checkout_id,
        stripe_key,
        pm_id,
        init_payload,
        proxy=proxy,
        timeout=timeout,
    )
    info["confirm_status"] = confirm_status
    if not confirm_payload:
        info["qr_status"] = "confirm_failed"
        info["qr_error"] = (confirm_status + (" " + confirm_snip if confirm_snip else ""))[:300]
        return info

    # Early extract if confirm already has scannable (rare)
    early = extract_momo_qr_payload(confirm_payload)
    for cand in (
        early.get("qr_png_url"),
        early.get("qr_image_url"),
        early.get("qr_data"),
        early.get("hosted_instructions_url"),
    ):
        if is_scannable_momo_artifact(str(cand or "")):
            c = str(cand)
            if c.startswith("data:image"):
                info["qr_image_url"] = c
                info["qr_png_url"] = c if "png" in c[:40] else ""
            if "momo.vn" in c.lower():
                info["hosted_instructions_url"] = c
            info["qr_data"] = c
            info["has_qr"] = True
            info["qr_status"] = "ok"
            return info

    # Manual-approval path (OpenAI): must approve then poll setup_intent redirect
    if not access_token:
        sa = confirm_payload.get("submission_attempt") or {}
        state = sa.get("state") if isinstance(sa, dict) else ""
        info["qr_status"] = "needs_approve"
        info["qr_error"] = f"confirm_ok state={state}; missing access_token for approve"
        mid = str(confirm_payload.get("stripe_hosted_url") or "")
        if mid:
            info["middle_checkout_url"] = mid
        return info

    ok_ap, ap_status = chatgpt_approve_checkout(
        access_token,
        session_token,
        checkout_id,
        processor_entity,
        proxy=proxy,
        timeout=max(timeout, 25),
    )
    info["approve_status"] = ap_status
    if not ok_ap:
        info["qr_status"] = "approve_failed"
        info["qr_error"] = ap_status
        return info

    # Poll re-init for setup_intent.next_action.redirect
    redirect = ""
    last_state = ""
    for _ in range(8):
        time.sleep(1.0)
        re_payload, re_st, _ = stripe_init(checkout_id, stripe_key, proxy, timeout=timeout)
        if not re_payload:
            continue
        sa = re_payload.get("submission_attempt") or {}
        if isinstance(sa, dict):
            last_state = str(sa.get("state") or "")
        redirect = _setup_intent_redirect(re_payload)
        if redirect:
            info["next_action_type"] = "setup_intent.redirect_to_url"
            break
        # already succeeded somehow
        early2 = extract_momo_qr_payload(re_payload)
        for cand in (
            early2.get("qr_png_url"),
            early2.get("qr_image_url"),
            early2.get("hosted_instructions_url"),
            early2.get("qr_data"),
        ):
            if is_scannable_momo_artifact(str(cand or "")):
                c = str(cand)
                info["qr_data"] = c
                if c.startswith("data:image"):
                    info["qr_image_url"] = c
                if "momo.vn" in c.lower():
                    info["hosted_instructions_url"] = c
                info["has_qr"] = True
                info["qr_status"] = "ok"
                return info

    info["redirect_url"] = redirect
    if not redirect:
        info["qr_status"] = "no_redirect"
        info["qr_error"] = f"approved but no setup_intent redirect state={last_state}"
        return info

    followed = follow_momo_redirect(redirect, proxy=proxy, timeout=max(timeout, 35))
    if followed.get("error") and not (
        followed.get("qr_image_url") or followed.get("hosted_instructions_url")
    ):
        info["qr_status"] = "redirect_follow_failed"
        info["qr_error"] = followed.get("error") or "follow_failed"
        return info

    img = str(followed.get("qr_image_url") or followed.get("qr_png_url") or "")
    host = str(followed.get("hosted_instructions_url") or "")
    data = str(followed.get("qr_data") or "")
    if img:
        info["qr_image_url"] = img
        if "png" in img[:40] or img.startswith("data:image/png"):
            info["qr_png_url"] = img
    if host and is_scannable_momo_artifact(host):
        info["hosted_instructions_url"] = host
    # Prefer image for scanning; keep gateway URL as hosted link
    if img and img.startswith("data:image"):
        info["qr_data"] = img
    elif host:
        info["qr_data"] = host
    elif data:
        info["qr_data"] = data

    if info["qr_image_url"] or (
        info["hosted_instructions_url"]
        and is_scannable_momo_artifact(info["hosted_instructions_url"])
    ):
        info["has_qr"] = True
        info["qr_status"] = "ok" if info["qr_image_url"] else "ok_momo_gateway"
        return info

    info["has_qr"] = False
    info["qr_status"] = "qr_missing"
    info["qr_error"] = followed.get("error") or f"followed {followed.get('final_url','')[:80]} no qr"
    return info


def probe_account(
    label: str,
    path: Path | None,
    raw_text: str | None,
    proxies: list[str],
    start_index: int,
    *,
    trial_days: int = 30,
    max_proxies: int = 3,
    timeout: int = 20,
    parse_only: bool = False,
    check_methods_anyway: bool = True,
    emit_qr: bool = True,
    max_attempts: int = 3,
) -> tuple[dict[str, Any], int]:
    if path is not None:
        access_token, session_token, credential = load_credential(path)
    else:
        access_token, session_token, credential = load_credential_text(raw_text or "")
    result: dict[str, Any] = {"account": label, **credential}
    if not access_token or credential.get("credential_expired") is True:
        result["conclusive"] = True
        result["supported"] = False
        result["decision_text"] = DECISION_TEXT.get(
            str(result.get("decision") or ""), str(result.get("decision") or "")
        )
        return result, start_index
    if parse_only:
        result["conclusive"] = True
        result["supported"] = None
        result["decision_text"] = DECISION_TEXT.get(
            str(result.get("decision") or ""), str(result.get("decision") or "")
        )
        return result, start_index

    if not proxies:
        proxies = [""]
    # Speed first: QR expires ~10 min. Cap attempts, keep proxy rotates small.
    max_attempts = max(1, min(3, int(max_attempts or 3)))
    max_proxies = max(1, min(3, int(max_proxies or 2)))
    timeout = max(8, min(25, int(timeout or 12)))

    best: dict[str, Any] | None = None
    next_index = start_index
    total_attempts = 0
    attempt_logs: list[dict[str, Any]] = []

    # Prefer custom first for Payment Element confirm → next_action/momo gateway.
    # hosted often only yields checkout.stripe.com middle form (not scannable).
    speed_strategies = ("custom_promo", "hosted_promo", "custom_trial")

    for attempt_no in range(1, max_attempts + 1):
        strategy = speed_strategies[(attempt_no - 1) % len(speed_strategies)]
        data, checkout_id, stripe_key, proxy, attempts, next_index = create_checkout(
            access_token,
            session_token,
            proxies,
            next_index,
            max_proxies,
            trial_days,
            timeout,
            strategy=strategy,
        )
        total_attempts += attempts
        attempt_info: dict[str, Any] = {
            "attempt": attempt_no,
            "strategy": strategy,
            "proxy_attempts": attempts,
        }
        if data is None:
            failure = proxy
            attempt_info["checkout_status"] = failure
            attempt_logs.append(attempt_info)
            # hard account terminal
            if failure in {"already_paid", "credential_invalid"}:
                result["checkout_status"] = failure
                result["decision"] = failure
                result["checkout_proxy_attempts"] = total_attempts
                result["attempt_logs"] = attempt_logs
                result["conclusive"] = True
                result["supported"] = False
                result["decision_text"] = DECISION_TEXT.get(failure, failure)
                if failure == "credential_invalid":
                    result["credential_valid"] = False
                return result, next_index
            # network-ish: continue attempts
            continue

        one_click_eligible = data.get("one_click_trial_eligible")
        is_new_customer = data.get("is_new_stripe_customer")
        attempt_info.update(
            {
                "checkout_status": "created",
                "one_click_trial_eligible": one_click_eligible,
                "is_new_stripe_customer": is_new_customer,
                "trial_in_openai_response": has_actual_trial_in_response(data),
            }
        )

        init_payload, init_status, init_attempts = stripe_init(
            checkout_id,
            stripe_key,
            proxy,
            timeout=timeout,
        )
        attempt_info["stripe_init_status"] = init_status
        attempt_info["stripe_init_attempts"] = init_attempts
        if init_payload is None:
            attempt_info["decision"] = "stripe_init_failed"
            attempt_logs.append(attempt_info)
            # keep going; maybe another strategy works
            cand = {
                "account": label,
                **credential,
                "credential_valid": True,
                "checkout_status": "created",
                "one_click_trial_eligible": one_click_eligible,
                "is_new_stripe_customer": is_new_customer,
                "trial_in_openai_response": attempt_info["trial_in_openai_response"],
                "stripe_init_status": init_status,
                "decision": "stripe_init_failed",
                "decision_text": DECISION_TEXT["stripe_init_failed"],
                "conclusive": False,
                "supported": None,
                "checkout_strategy": strategy,
                "has_momo": None,
                "has_qr": False,
            }
            if best is None:
                best = cand
            continue

        # Force 0-dong: if first init not zero, checkout/update promo then re-init.
        promo_update_status = "skipped"
        due = amount_due(init_payload) if init_payload else None
        entity = processor_entity_of(data)
        if due not in (0,):
            ok_upd, promo_update_status = apply_promo_update(
                access_token,
                session_token,
                checkout_id,
                entity,
                proxy,
                timeout=timeout,
            )
            if ok_upd:
                re_payload, re_status, re_attempts = stripe_init(
                    checkout_id,
                    stripe_key,
                    proxy,
                    timeout=timeout,
                )
                init_attempts += re_attempts
                if re_payload is not None:
                    init_payload = re_payload
                    init_status = f"{init_status}+rezero_{re_status}"
                    due = amount_due(init_payload)
                else:
                    promo_update_status = f"{promo_update_status};reinit_{re_status}"
            attempt_info["promo_update_status"] = promo_update_status
            attempt_info["promo_rezero_due"] = due

        methods, methods_source = extract_methods(init_payload)
        init_has_trial, trial_days_value, has_trial_end = trial_marker(
            init_payload, "elements_options"
        )
        actual_trial = bool(attempt_info["trial_in_openai_response"] or init_has_trial)
        has_momo = None if methods is None else "momo" in (methods or [])
        stripe_mode = stripe_field(init_payload, "mode")
        due = amount_due(init_payload)
        decision = choose_decision(
            one_click_eligible,
            actual_trial,
            stripe_mode,
            has_momo,
            amount_due_cents=due,
        )
        cand = {
            "account": label,
            **credential,
            "credential_valid": True,
            "checkout_status": "created",
            "one_click_trial_eligible": one_click_eligible,
            "is_new_stripe_customer": is_new_customer,
            "trial_in_openai_response": attempt_info["trial_in_openai_response"],
            "stripe_init_status": init_status,
            "stripe_init_attempts": init_attempts,
            "stripe_mode": stripe_mode,
            "payment_method_collection": stripe_field(
                init_payload, "payment_method_collection"
            ),
            "amount_due": due,
            "currency": stripe_field(init_payload, "currency"),
            "methods": methods,
            "methods_source": methods_source,
            "has_momo": has_momo,
            "trial_period_days_in_init": trial_days_value,
            "trial_end_present_in_init": has_trial_end,
            "actual_trial": actual_trial,
            "promo_update_status": promo_update_status,
            "processor_entity": entity,
            "decision": decision,
            "decision_text": DECISION_TEXT.get(decision, decision),
            "conclusive": decision not in {"payment_methods_unknown", "stripe_init_failed"},
            # only zero+momo counts as supported
            "supported": decision in {"ready", "momo_supported", "ready_with_qr"},
            "has_qr": False,
            "qr_status": "skipped",
            "checkout_strategy": strategy,
            "checkout_session_tail": str(checkout_id)[-10:],
            "_checkout_id": checkout_id,
            "_stripe_key": stripe_key,
            "_proxy": proxy,
            "_init_payload": init_payload,
        }
        attempt_info.update(
            {
                "methods": methods,
                "has_momo": has_momo,
                "amount_due": due,
                "decision": decision,
                "promo_update_status": promo_update_status,
            }
        )
        attempt_logs.append(attempt_info)

        # rank: ZERO first, then momo (non-zero momo is not success)
        def _score(row: dict[str, Any]) -> tuple:
            return (
                1 if row.get("amount_due") == 0 else 0,
                1 if row.get("has_momo") else 0,
                1 if row.get("actual_trial") or row.get("one_click_trial_eligible") is True else 0,
                1 if row.get("decision") in {"ready", "momo_supported"} else 0,
            )

        if best is None or _score(cand) > _score(best):
            best = cand

        # only stop early on forced-zero + momo
        if has_momo is True and due == 0:
            break
        # Fast-fail: full-price card-only after 2 strategies rarely flips.
        if (
            attempt_no >= 2
            and has_momo is False
            and due not in (None, 0)
            and one_click_eligible is False
        ):
            break
        # continue loop

    result["checkout_proxy_attempts"] = total_attempts
    result["attempt_logs"] = attempt_logs
    result["attempts_used"] = len(attempt_logs)

    if best is None:
        result["decision"] = "checkout_failed"
        result["decision_text"] = DECISION_TEXT["checkout_failed"]
        result["conclusive"] = False
        result["supported"] = None
        result["checkout_status"] = "failed"
        return result, next_index

    # promote best
    for k, v in best.items():
        if not str(k).startswith("_"):
            result[k] = v

    # HARD: emit QR only when momo + amount_due==0
    due_final = best.get("amount_due")
    if best.get("has_momo") is True and due_final not in (0,):
        result["decision"] = "promo_nonzero"
        result["decision_text"] = DECISION_TEXT["promo_nonzero"]
        result["supported"] = False
        result["conclusive"] = True
        result["has_qr"] = False
        result["qr_status"] = "blocked_nonzero"
        result["qr_error"] = f"amount_due={due_final}"
    elif emit_qr and best.get("has_momo") is True and due_final == 0:
        # If best was hosted, try one custom_promo session for real next_action QR.
        emit_checkout = str(best.get("_checkout_id") or "")
        emit_key = str(best.get("_stripe_key") or "")
        emit_proxy = str(best.get("_proxy") or "")
        emit_init = best.get("_init_payload") or {}
        emit_strategy = str(best.get("checkout_strategy") or "")
        if emit_strategy.startswith("hosted") or "custom" not in emit_strategy:
            data2, cs2, key2, proxy2, attempts2, next_index = create_checkout(
                access_token,
                session_token,
                proxies,
                next_index,
                max_proxies,
                trial_days,
                timeout,
                strategy="custom_promo",
            )
            result["checkout_proxy_attempts"] = int(result.get("checkout_proxy_attempts") or 0) + attempts2
            if data2 and cs2:
                # rezero custom session
                entity2 = processor_entity_of(data2)
                init2, st2, _ = stripe_init(cs2, key2, proxy2, timeout=timeout)
                if init2 is not None and amount_due(init2) not in (0,):
                    apply_promo_update(
                        access_token, session_token, cs2, entity2, proxy2, timeout=timeout
                    )
                    init2, st2, _ = stripe_init(cs2, key2, proxy2, timeout=timeout)
                methods2, _ = extract_methods(init2 or {})
                due2 = amount_due(init2) if init2 else None
                if init2 and methods2 and "momo" in methods2 and due2 == 0:
                    emit_checkout, emit_key, emit_proxy, emit_init = cs2, key2, proxy2, init2
                    result["amount_due"] = 0
                    result["checkout_strategy"] = "custom_promo"
                    result["methods"] = methods2
                    result["promo_update_status"] = (
                        str(result.get("promo_update_status") or "") + "+custom_emit"
                    )

        qr_info = emit_momo_qr(
            emit_checkout,
            emit_key,
            emit_init,
            proxy=emit_proxy,
            timeout=max(timeout, 25),
            access_token=access_token,
            session_token=session_token or "",
            processor_entity=str(result.get("processor_entity") or best.get("processor_entity") or "openai_llc"),
        )
        result.update(
            {
                "qr_status": qr_info.get("qr_status") or "",
                "qr_error": qr_info.get("qr_error") or "",
                "has_qr": bool(qr_info.get("has_qr")),
                "qr_data": qr_info.get("qr_data") or "",
                "qr_image_url": qr_info.get("qr_image_url") or "",
                "qr_png_url": qr_info.get("qr_png_url") or "",
                "hosted_instructions_url": qr_info.get("hosted_instructions_url") or "",
                "next_action_type": qr_info.get("next_action_type") or "",
                "qr_expires_at": qr_info.get("qr_expires_at"),
                "pm_status": qr_info.get("pm_status") or "",
                "confirm_status": qr_info.get("confirm_status") or "",
                "approve_status": qr_info.get("approve_status") or "",
                "middle_checkout_url": qr_info.get("middle_checkout_url") or "",
                "redirect_url": (qr_info.get("redirect_url") or "")[:120],
            }
        )
        # Only scannable momo gateway / real QR image counts.
        if result.get("has_qr") and result.get("amount_due") == 0:
            result["decision"] = "ready_with_qr"
            result["decision_text"] = DECISION_TEXT["ready_with_qr"]
            result["supported"] = True
            result["conclusive"] = True
        else:
            # demote stripe middle page
            if result.get("middle_checkout_url") and not result.get("has_qr"):
                result["decision"] = "momo_qr_failed"
                result["decision_text"] = "0元有MoMo，但只拿到Stripe中间页，未出可扫码"
                result["qr_status"] = result.get("qr_status") or "stripe_middle_only"
            elif result.get("amount_due") not in (0,):
                result["decision"] = "promo_nonzero"
                result["decision_text"] = DECISION_TEXT["promo_nonzero"]
                result["has_qr"] = False
            else:
                result["decision"] = "momo_qr_failed"
                result["decision_text"] = DECISION_TEXT["momo_qr_failed"]
            result["supported"] = False
            result["conclusive"] = True
            result["has_qr"] = False

    # cleanup internals if any leaked
    for k in list(result.keys()):
        if str(k).startswith("_"):
            result.pop(k, None)
    return result, next_index


def probe_token_blob(
    raw_text: str,
    *,
    proxies: list[str] | None = None,
    direct: bool = True,
    proxy_file: Path | None = None,
    trial_days: int = 30,
    max_proxies: int = 1,
    timeout: int = 20,
    parse_only: bool = False,
    check_methods_anyway: bool = False,
    emit_qr: bool = True,
    pre_proxy: str = "auto",
    label: str = "A",
) -> dict[str, Any]:
    configure_pre_proxy(pre_proxy)
    if parse_only:
        proxy_list = [""]
    elif proxies is not None:
        proxy_list = [normalize_proxy_url(p) for p in proxies] or [""]
    elif direct:
        proxy_list = [""]
    else:
        proxy_list = load_proxies(proxy_file or DEFAULT_PROXY_FILE, direct=False)
    result, _ = probe_account(
        label,
        None,
        raw_text,
        proxy_list,
        0,
        trial_days=trial_days,
        max_proxies=max_proxies,
        timeout=timeout,
        parse_only=parse_only,
        check_methods_anyway=check_methods_anyway,
        emit_qr=emit_qr,
    )
    return result


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    label = result["account"]
    decision = result.get("decision", "checkout_failed")
    decision_text = result.get("decision_text") or DECISION_TEXT.get(decision, decision)
    if "methods" not in result:
        print(f"[{label}] {decision_text}")
        return
    eligible = result.get("one_click_trial_eligible")
    eligible_text = "是" if eligible is True else "否" if eligible is False else "未知"
    trial_text = "是" if result.get("actual_trial") else "否"
    momo_value = result.get("has_momo")
    momo_text = "是" if momo_value is True else "否" if momo_value is False else "未知"
    methods = ",".join(result.get("methods") or []) or "无"
    qr_flag = "是" if result.get("has_qr") else "否"
    qr_status = result.get("qr_status") or "-"
    print(
        f"[{label}] 账号试用资格={eligible_text} | 真trial={trial_text} | "
        f"模式={result.get('stripe_mode') or '未知'} | 方法={methods} | "
        f"MoMo={momo_text} | QR={qr_flag}({qr_status}) | 结论={decision_text}"
    )


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    configure_pre_proxy(args.pre_proxy)
    COUNTRY_CURRENCY["VN"] = "VND"

    try:
        proxies = [""] if args.parse_only else load_proxies(args.proxy_file, args.direct)
    except RuntimeError as exc:
        print(f"检测无法启动：{exc}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    next_proxy_index = 0
    for index, token_file in enumerate(args.token_files):
        result, next_proxy_index = probe_account(
            account_label(index),
            token_file,
            None,
            proxies,
            next_proxy_index,
            trial_days=args.trial_days,
            max_proxies=args.max_proxies,
            timeout=args.timeout,
            parse_only=args.parse_only,
            check_methods_anyway=args.check_methods_anyway,
            emit_qr=(not args.no_emit_qr) and bool(args.emit_qr),
        )
        results.append(result)
        print_result(result, args.json)

    return 2 if any(result.get("conclusive") is False for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
