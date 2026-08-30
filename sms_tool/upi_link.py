#!/usr/bin/env python3
r"""UPI 支付链接 / QR 生成子系统。

从 ``gen_pp_link.py`` 纯搬迁拆分（零行为变化）。本模块拥有:

* ``generate_upi_qr_link`` -- 完整 7 阶段 UPI 提取流水线
  (checkout → stripe init → 免费试用检测 → 税区更新 → confirm → approve →
  轮询提取 upi:// URI → hydrate → 渲染 QR)
* ``_upi_*`` 系列 -- Stripe init / confirm 响应里的 UPI QR 数据提取助手
* ``_default_qr_path`` / ``_write_qr_png`` -- QR PNG 写出助手
* ``_method_cfg`` / ``_payment_stage_proxies_from_config`` -- 按支付方式的
  配置与阶段代理解析

依赖方向: ``gen_pp_link`` → 本模块; 本模块不得 import ``gen_pp_link``。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .paypal_extract import CURRENCY_MAP, _new_session
    from .pp_link_helpers import (
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
    )
    from .paypal_proxy import _stage_proxy_value
except ImportError:  # pragma: no cover - direct script execution
    from paypal_extract import CURRENCY_MAP, _new_session  # type: ignore
    from pp_link_helpers import (  # type: ignore
        DEFAULT_STRIPE_PK,
        STRIPE_VERSION,
        DEFAULT_TIMEOUT,
        CHATGPT_TIMEOUT,
    )
    from paypal_proxy import _stage_proxy_value  # type: ignore

# ─── 输出 ────────────────────────────────────────────────────────────────────


def _emit(step: str, msg: str, **kw: Any) -> None:
    """Top-level progress/error sink (sunk copy; see ``gen_pp_link._emit``)."""
    print(f"[{step}] {msg}", file=sys.stderr)


# ─── 路径 / 配置装载 (下沉副本, 与 gen_pp_link 同语义) ──────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def _load_json(path: str) -> dict:
    """Load a JSON object from disk, accepting UTF-8 files with or without BOM."""
    if os.path.abspath(path) == os.path.abspath(DEFAULT_CONFIG_PATH):
        from .config import load_merged_config
        return load_merged_config()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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


def _normalize_hosted_checkout_url(url: str) -> str:
    value = str(url or "").strip()
    if value:
        return value.replace("checkout.stripe.com", "pay.openai.com")
    return value


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
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""

    def pick(key: str, fallback: str = "") -> str:
        value = _stage_proxy_value(method_stage, key)
        if value:
            return value
        return _stage_proxy_value(paypal_stage, key, fallback)

    checkout = pick("checkout", proxy_default)
    provider = pick("provider") or pick("stripe_init") or proxy_default
    approve = pick("approve") or pick("confirm") or provider or proxy_default
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
    runtime_config: Mapping[str, Any] | None = None,
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
    cfg = dict(runtime_config) if isinstance(runtime_config, Mapping) else _load_json(DEFAULT_CONFIG_PATH)
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

    emit = _emit

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
