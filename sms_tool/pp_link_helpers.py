"""Shared helpers for PP (PayPal) direct-link generation.

Extracted from ``gen_pp_link.py`` to keep the extractor focused on the
three-stage proxy-routing logic.  These utilities cover:

* proxy template normalization and per-country variable substitution
* BA / EC token extraction from redirect URLs
* redirect resolution (internal → external)
* Stripe amount / diagnostics utilities
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests

try:
    from .checkout_contract import CheckoutRequestContract, CheckoutSessionContract
    from .phone_proxy import normalize_proxy_url
except ImportError:  # pragma: no cover - direct script execution
    from checkout_contract import CheckoutRequestContract, CheckoutSessionContract  # type: ignore
    from phone_proxy import normalize_proxy_url  # type: ignore

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

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_STRIPE_PK = (os.environ.get("PP_STRIPE_PUBLISHABLE_KEY", "") or "").strip() or (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
RETRY_ATTEMPTS = 3

_SIDE_EFFECT_STAGES = frozenset({"confirm", "approve", "poll", "follow_redirect"})

PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\s\"'<>]+", re.I)
PAYPAL_BA_RE = re.compile(r"https://www\.paypal\.com/agreements/approve\?[^\s\"']+", re.I)

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


# ─── 代理工具 ──────────────────────────────────────────────────────────────────


def normalize_proxy_template(template: str) -> str:
    """规范化代理模板，支持多种格式:
    - 标准: user:pass@host:port
    - 反转: host:port@user:pass
    - 冒号分隔: host:port:user:pass
    """
    proxy = str(template or "").strip()
    if not proxy:
        return proxy

    if "@" not in proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            if "." in host and port.isdigit():
                return normalize_proxy_url(f"{user}:{pwd}@{host}:{port}")
        return normalize_proxy_url(proxy)

    parts = proxy.split("@")
    if len(parts) != 2:
        return normalize_proxy_url(proxy)
    left, right = parts
    if re.match(r"^[a-zA-Z0-9.\-]+:\d+$", left) and "." in left.split(":")[0]:
        return normalize_proxy_url(f"{right}@{left}")
    return normalize_proxy_url(proxy)


def proxy_for_country_template(template: str, country: str) -> str:
    """Replace a proxy template's region with the requested country."""
    proxy = normalize_proxy_template(template)
    country = str(country or "").strip().upper()
    if not proxy or not country:
        return proxy
    userinfo, separator, host = proxy.rpartition("@")
    if not separator:
        return proxy
    replaced, count = re.subn(r"region-[A-Za-z]{2}(?=$|[-_:])", f"region-{country}", userinfo, count=1)
    if count != 1:
        replaced, count = re.subn(r"-[A-Za-z]{2}$", f"-{country}", userinfo)
    elif country != "JP":
        replaced = re.sub(r"-st-[^-@]+-city-[^-@]+(?=-sid-)", "", replaced, count=1)
    if count != 1:
        return proxy
    return normalize_proxy_url(f"{replaced}@{host}")


def rotate_proxy_session(proxy: str) -> str:
    """轮转代理会话标识（如果代理模板支持）。"""
    return rotate_stage_proxy_session(proxy)


# ─── BA / EC Token 提取 ──────────────────────────────────────────────────────


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
    """Recursively search *value* (dict/str/list) for the first URL match."""
    if isinstance(value, str):
        for pat in patterns:
            m = pat.search(value)
            if m:
                return m.group(0)
        return ""
    if isinstance(value, dict):
        for key in ("url", "redirect_url", "return_url"):
            if key in value:
                found = find_url_in_value(value[key], patterns)
                if found:
                    return found
        for v in value.values():
            found = find_url_in_value(v, patterns)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_url_in_value(item, patterns)
            if found:
                return found
    return ""


def extract_redirect_url(payload: dict) -> str:
    next_action = payload.get("next_action") or {}
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect = next_action.get("redirect_to_url") or {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    url = find_url_in_value(payload, [PM_REDIRECT_RE, PAYPAL_BA_RE])
    if url:
        return url
    for intent_key in ("setup_intent", "payment_intent"):
        intent = payload.get(intent_key) or {}
        action = intent.get("next_action") if isinstance(intent, dict) else {}
        redirect = action.get("redirect_to_url") if isinstance(action, dict) else {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    return ""


# ─── 重定向追踪 ────────────────────────────────────────────────────────────────


def resolve_external_redirect(
    session: Any,
    redirect_url: str,
    max_hops: int = 5,
) -> str:
    """Follow redirects until a PayPal approval URL or terminal location."""
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


# ─── Stripe / 金额 ─────────────────────────────────────────────────────────────


def billing_for_country(country: str) -> dict:
    country = str(country or "DE").upper()
    data = BILLING_DATA.get(country) or BILLING_DATA["DE"]
    return {
        "country": country,
        "name": data["name"],
        "email": f"buyer{uuid.uuid4().int % 9000 + 1000}@example.{country.lower()}",
        "street": data["street"],
        "city": data["city"],
        "state": data["state"],
        "postal": data["postal"],
    }


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


# ─── 诊断 ─────────────────────────────────────────────────────────────────────


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


def stripe_confirm_error_diagnostics(
    response: Any,
    cs_id: str,
    pm_id: str,
    init_payload: dict,
) -> str:
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
        ("error_type", error.get("type")), ("error_code", error.get("code")),
        ("error_param", error.get("param")), ("error_message", error.get("message")),
        ("submission_state", submission.get("state")), ("submission_reason", submission.get("reason")),
        ("submission_code", submission.get("code")), ("submission_message", submission.get("message")),
    ):
        if value not in (None, ""):
            compact = re.sub(r"\s+", " ", str(value)).strip()
            parts.append(f"{label}={compact[:180]}")
    if len(parts) == 5:
        body = re.sub(r"\s+", " ", str(getattr(response, "text", ""))).strip()
        parts.append(f"body={body[:180]}")
    return "; ".join(parts)
