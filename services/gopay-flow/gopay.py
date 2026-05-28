#!/usr/bin/env python3
"""GoPay tokenization payment flow for ChatGPT Plus subscriptions.

Replays Stripe → Midtrans → GoPay's tokenization linking + charge in pure
HTTP. No browser needed. WhatsApp OTP is delivered through an injected provider:
the WhatsApp protocol sidecar gRPC channel or the local ADB HTTP sidecar.

Flow (15 steps):

    1.  POST chatgpt.com/backend-api/payments/checkout
            body: {entry_point, plan_name, billing_details:{country:ID,currency:IDR}, ...}
            ← cs_live_xxx
    2.  POST api.stripe.com/v1/payment_methods (type=gopay)         ← pm_xxx
    3.  POST api.stripe.com/v1/payment_pages/{cs}/confirm           ← status:open
    4.  POST chatgpt.com/backend-api/payments/checkout/approve      ← approved
    5.  GET  pm-redirects.stripe.com/authorize/{nonce}              → 302 → midtrans
    6.  GET  app.midtrans.com/snap/v1/transactions/{snap_token}     ← merchant info
    7.  POST app.midtrans.com/snap/v3/accounts/{snap_token}/linking
            body: {type:gopay, country_code, phone_number}
            (406 first attempt if account already linked, retry → 201)  ← reference_id
    8.  POST gwa.gopayapi.com/v1/linking/validate-reference         ← display info
    9.  POST gwa.gopayapi.com/v1/linking/user-consent               ← OTP triggered
    10. POST gwa.gopayapi.com/v1/linking/validate-otp               ← challenge_id, client_id
    11. POST customer.gopayapi.com/api/v1/users/pin/tokens/nb       ← pin_token (JWT)
    12. POST gwa.gopayapi.com/v1/linking/validate-pin               ← linking complete
    13. POST app.midtrans.com/snap/v2/transactions/{snap}/charge    ← charge_ref (A12...)
    14. GET  gwa.gopayapi.com/v1/payment/validate?reference_id=...
        POST gwa.gopayapi.com/v1/payment/confirm?reference_id=...   ← second challenge
        POST customer.gopayapi.com/api/v1/users/pin/tokens/nb       ← second pin_token
        POST gwa.gopayapi.com/v1/payment/process?reference_id=...   ← settled
    15. GET  chatgpt.com/checkout/verify?stripe_session_id=...      ← Plus active
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests

# Cloudflare 拦 plain requests 的 TLS 指纹（403 + HTML challenge），用 curl_cffi
# 模拟真 Chrome 指纹。
try:
    from curl_cffi.requests import Session as _CurlCffiSession  # type: ignore
except ImportError:
    _CurlCffiSession = None  # type: ignore


def _new_session(impersonate: str = "chrome136") -> Any:
    """Build session with chrome TLS fingerprint when available."""
    if _CurlCffiSession is not None:
        return _CurlCffiSession(impersonate=impersonate)
    return requests.Session()


# ──────────────────────────── constants ───────────────────────────

# OpenAI's Midtrans merchant client id (public, embedded in JS).
# Override via gopay config block if rotated.
DEFAULT_MIDTRANS_CLIENT_ID = "Mid-client-3TX8nUa-f_RgNrky"

# OpenAI's Stripe live publishable key (public, embedded in checkout page JS).
# Override via cfg["stripe"]["publishable_key"] if it ever changes.
DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)

GOPAY_PIN_CLIENT_ID_LINK = "51b5f09a-3813-11ee-be56-0242ac120002-MGUPA"
GOPAY_PIN_CLIENT_ID_CHARGE = "47180a8e-f56e-11ed-a05b-0242ac120003-GWC"

DEFAULT_TIMEOUT = 30
LINK_RETRY_LIMIT = 2  # 406 "account already linked" retry
LINK_RETRY_SLEEP_S = 12.0  # Midtrans 需要冷却 ~10s 才会让 406 → 201（实测）
# 429 "There's a technical error" 风控触发条件：带 Authorization 的 SDK 路径
# 在某些 IP / 高频场景必现。剥掉 Authorization 头同 endpoint 重发即返回 201
# + activation_link_url（实测 + 反向工程参考实现确认）。
LINK_BYPASS_BODY_HINTS = (
    "technical error",
    "too many",
    "rate limit",
    "rate_limit",
)
DEFAULT_OTP_REGEX = r"(?<!\d)(\d{6})(?!\d)"
MIDTRANS_STATUS_POLL_LIMIT = 12
SMSBOWER_ENDPOINT = "https://smsbower.page/stubs/handler_api.php"


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
    text = str(getattr(response, "text", "") or "")
    if text:
        details["raw"] = text[:500]
    return details


def _stripe_error_summary(response: Any) -> str:
    details = _stripe_error_details(response)
    parts = [f"status={details.get('status')}"]
    for key in ("code", "type", "param", "message"):
        if details.get(key):
            parts.append(f"{key}={details[key]}")
    if details.get("raw") and not details.get("message"):
        parts.append(f"raw={details['raw']}")
    return " ".join(parts)


def _post_stripe_form(
    session: Any,
    url: str,
    body: dict[str, Any],
    *,
    timeout: int,
    step: str,
    log: Callable[[str], None],
) -> Any:
    current_body = dict(body)
    while True:
        response = session.post(url, data=current_body, timeout=timeout)
        details = _stripe_error_details(response)
        unknown_param = str(details.get("param") or "")
        if response.status_code == 400 and details.get("code") == "parameter_unknown" and unknown_param in current_body:
            current_body.pop(unknown_param, None)
            log(f"[gopay] {step}: retry without unknown param {unknown_param}")
            continue
        return response


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
    }


def _expected_amount_from_init(init_data: dict[str, Any]) -> str:
    zero_check = _zero_due_check(init_data)
    if zero_check["ok"]:
        return "0"
    amount_due = _amount_at(init_data, "invoice", "amount_due")
    due = _amount_at(init_data, "total_summary", "due")
    return str(amount_due if amount_due is not None else (due if due is not None else 0))


# ──────────────────────────── exceptions ──────────────────────────


class GoPayError(RuntimeError):
    pass


class OTPCancelled(GoPayError):
    pass


class GoPayPINRejected(GoPayError):
    pass


class GoPayFraudDeny(GoPayError):
    pass


# ──────────────────────────── core ────────────────────────────────


class GoPayCharger:
    """Drive the entire GoPay tokenization flow for one subscription.

    Construction needs:
        chatgpt_session: a requests.Session pre-configured with the user's
            chatgpt.com cookies + sentinel headers. Caller is responsible.
        gopay_cfg: {"country_code": "86", "phone_number": "...", "pin": "..."}
        otp_provider: () -> str. Called once per linking; should block until
            the user supplies the OTP via WhatsApp.
        log: () -> None. Called for human-readable progress messages.
    """

    def __init__(
        self,
        chatgpt_session: Any,
        gopay_cfg: dict,
        otp_provider: Callable[[], str],
        log: Callable[[str], None] = print,
        proxy: Optional[str] = None,
        runtime_cfg: Optional[dict] = None,
    ):
        self.cs = chatgpt_session
        self.country_code = str(gopay_cfg["country_code"]).lstrip("+")
        self.phone = re.sub(r"\D", "", str(gopay_cfg["phone_number"]))
        self.pin = str(gopay_cfg["pin"])
        self.otp_channel = str(gopay_cfg.get("otp_channel") or "sms").strip().lower()
        self.browser_locale = str(gopay_cfg.get("browser_locale") or "zh-CN")
        self.pin_locale = str(gopay_cfg.get("pin_locale") or "id")
        self.browser_platform = str(gopay_cfg.get("browser_platform") or "Mac OS 10.15.7")
        self.midtrans_client_id = str(
            gopay_cfg.get("midtrans_client_id") or DEFAULT_MIDTRANS_CLIENT_ID
        )
        self.otp_provider = otp_provider
        self.log = log
        self._midtrans_merchant_id: Optional[str] = None
        # Stripe runtime fingerprint (js_checksum / rv_timestamp / version) — these
        # are computed by Stripe.js client-side; replay the captured values from
        # config.runtime or HAR. Without them confirm 400.
        self.runtime = runtime_cfg or {}
        # separate session for non-chatgpt domains (avoid leaking chatgpt cookies)
        self.ext = _new_session()
        self.ext.headers.update({
            "User-Agent": (
                self.cs.headers.get("User-Agent")
                or "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_2_1) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "Accept-Language": (
                "zh-CN,zh;q=0.9,en;q=0.8"
                if self.browser_locale.lower().startswith("zh")
                else "en-US,en;q=0.9"
            ),
        })
        if proxy:
            try:
                self.cs.proxies = {"http": proxy, "https": proxy}
            except Exception:
                pass
            try:
                self.ext.proxies = {"http": proxy, "https": proxy}
            except Exception:
                pass

    def close(self) -> None:
        for sess in (self.cs, self.ext):
            close = getattr(sess, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    # ───── Step 1-4: ChatGPT/Stripe checkout ─────

    def _chatgpt_create_checkout(self) -> str:
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "ID", "currency": "IDR"},
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
            "checkout_ui_mode": "hosted",
            "cancel_url": "https://chatgpt.com/#pricing",
        }
        r = self.cs.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            json=body, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        cs_id = (
            data.get("checkout_session_id")
            or data.get("session_id")
            or data.get("id")
        )
        if not cs_id or not str(cs_id).startswith("cs_"):
            raise GoPayError(f"checkout create: bad response {data!r}")
        self.log(f"[gopay] checkout created cs={cs_id}")
        return cs_id

    def _stripe_create_pm(self, cs_id: str, stripe_pk: str, billing: dict) -> str:
        # PM billing 即使 IDR 计划也接受 US 地址（HAR 验证）；空配置时给个有效默认
        body = {
            "billing_details[name]": billing.get("name") or "John Doe",
            "billing_details[email]": billing.get("email") or "buyer@example.com",
            "billing_details[address][country]": billing.get("country") or "US",
            "billing_details[address][line1]": billing.get("line1") or "3110 Sunset Boulevard",
            "billing_details[address][city]": billing.get("city") or "Los Angeles",
            "billing_details[address][postal_code]": billing.get("postal_code") or "90026",
            "billing_details[address][state]": billing.get("state") or "CA",
            "type": "gopay",
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "key": stripe_pk,
        }
        r = self.ext.post(
            "https://api.stripe.com/v1/payment_methods",
            data=body, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        pm_id = r.json().get("id", "")
        if not pm_id.startswith("pm_"):
            raise GoPayError(f"stripe payment_methods: bad response {r.text[:300]}")
        self.log(f"[gopay] stripe pm={pm_id}")
        return pm_id

    def _stripe_init(self, cs_id: str, stripe_pk: str) -> dict:
        """Call /payment_pages/{cs}/init and validate this session supports GoPay."""
        body = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[stripe_js_locale]": "auto",
            "key": stripe_pk,
            "_stripe_version": (
                "2025-03-31.basil; checkout_server_update_beta=v1; "
                "checkout_manual_approval_preview=v1"
            ),
        }
        r = _post_stripe_form(
            self.ext,
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
            body,
            timeout=DEFAULT_TIMEOUT,
            step="stripe init",
            log=self.log,
        )
        if r.status_code != 200:
            raise GoPayError(f"stripe init failed: {_stripe_error_summary(r)}")
        data = r.json() or {}
        pm_types = [pm for pm in data.get("payment_method_types", []) if isinstance(pm, str)]
        currency = str(data.get("currency") or "").lower()
        expected_amount = _expected_amount_from_init(data)
        zero_check = _zero_due_check(data)
        self.log(
            f"[gopay] stripe init currency={currency or '?'} expected_amount={expected_amount} "
            f"amounts={zero_check['amounts']} tax_amounts={zero_check['tax_amounts']} "
            f"payment_method_types={pm_types}"
        )
        if "gopay" not in pm_types:
            raise GoPayError(
                "checkout does not support GoPay: "
                f"currency={currency or '?'} payment_method_types={pm_types}; "
                "need modern hosted IDR checkout",
            )
        ic = data.get("init_checksum") or ""
        if not ic:
            raise GoPayError(f"stripe init: no init_checksum {r.text[:200]}")
        return data

    @staticmethod
    def _extract_redirect_to_url(payload: dict) -> str:
        for key in ("next_action", "payment_intent", "setup_intent"):
            obj = payload.get(key)
            if not isinstance(obj, dict):
                continue
            action = obj if key == "next_action" else obj.get("next_action")
            if isinstance(action, dict) and action.get("type") == "redirect_to_url":
                return ((action.get("redirect_to_url") or {}).get("url") or "").strip()
        return ""

    def _stripe_confirm(self, cs_id: str, pm_id: str, stripe_pk: str) -> dict:
        init_data = self._stripe_init(cs_id, stripe_pk)
        init_checksum = init_data.get("init_checksum", "")
        expected_amount = _expected_amount_from_init(init_data)
        # Stripe 需要 return_url 才会把 checkout 推进到 requires_action（带 setup_intent）
        chatgpt_return = (
            f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}"
            f"&processor_entity=openai_llc&plan_type=plus"
        )
        from urllib.parse import quote
        return_url = (
            f"https://checkout.stripe.com/c/pay/{cs_id}"
            f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(chatgpt_return, safe='')}"
        )
        body = {
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": init_checksum,
            "version": self.runtime.get("version") or "fed52f3bc6",
            "expected_amount": expected_amount,
            "expected_payment_method_type": "gopay",
            "return_url": return_url,
            "elements_session_client[session_id]": f"elements_session_{uuid.uuid4().hex[:11]}",
            "elements_session_client[locale]": "en",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
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
        consent_collection = init_data.get("consent_collection") or {}
        tos = consent_collection.get("terms_of_service")
        if tos and tos not in ("none", ""):
            body["consent[terms_of_service]"] = "accepted"
        # Stripe runtime anti-bot tokens (replayable per-session-only; without
        # these confirm fails for hCaptcha-protected merchants like OpenAI).
        if self.runtime.get("js_checksum"):
            body["js_checksum"] = self.runtime["js_checksum"]
        if self.runtime.get("rv_timestamp"):
            body["rv_timestamp"] = self.runtime["rv_timestamp"]
        r = _post_stripe_form(
            self.ext,
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
            body,
            timeout=DEFAULT_TIMEOUT,
            step="stripe confirm",
            log=self.log,
        )
        if (
            r.status_code == 400
            and "terms of service" in (r.text or "").lower()
            and "consent[terms_of_service]" not in body
        ):
            self.log("[gopay] Stripe confirm requires ToS consent; retrying once")
            body["consent[terms_of_service]"] = "accepted"
            r = _post_stripe_form(
                self.ext,
                f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
                body,
                timeout=DEFAULT_TIMEOUT,
                step="stripe confirm",
                log=self.log,
            )
        if r.status_code != 200:
            reinit_attempts = 0
            while r.status_code != 200 and reinit_attempts < 2:
                details = _stripe_error_details(r)
                if details.get("code") != "checkout_amount_mismatch":
                    break
                reinit_attempts += 1
                self.log(f"[gopay] stripe confirm amount mismatch; re-init retry {reinit_attempts}/2")
                init_data = self._stripe_init(cs_id, stripe_pk)
                body["init_checksum"] = init_data.get("init_checksum") or body["init_checksum"]
                body["expected_amount"] = _expected_amount_from_init(init_data)
                r = _post_stripe_form(
                    self.ext,
                    f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
                    body,
                    timeout=DEFAULT_TIMEOUT,
                    step="stripe confirm",
                    log=self.log,
                )
            if r.status_code != 200:
                raise GoPayError(f"stripe confirm failed: {_stripe_error_summary(r)}")
        data = r.json() or {}
        self.log(
            f"[gopay] stripe confirm: payment_status={data.get('payment_status')} "
            f"setup_intent_status={(data.get('setup_intent') or {}).get('status')}"
        )
        return data

    def _chatgpt_sentinel_ping(self):
        try:
            self.cs.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={}, timeout=DEFAULT_TIMEOUT,
            )
        except Exception as e:
            self.log(f"[gopay] sentinel/ping skipped: {e}")

    def _chatgpt_approve(self, cs_id: str, processor_entity: str = "openai_llc"):
        # sentinel/ping 在 approve 之前刷一下，否则 approve 过但 setup_intent 不创
        self._chatgpt_sentinel_ping()
        r = self.cs.post(
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        result = r.json().get("result")
        if result != "approved":
            raise GoPayError(f"chatgpt approve: result={result!r}")
        self.log("[gopay] chatgpt approved")

    # ───── Step 5-6: Stripe → Midtrans redirect ─────

    def _follow_redirect_to_midtrans(self, cs_id: str, stripe_pk: str) -> str:
        """Resolve the Midtrans snap_token from setup_intent.next_action.

        After approve, Stripe populates setup_intent on the checkout session.
        The frontend re-GETs payment_pages/{cs} to read
        setup_intent.next_action.redirect_to_url.url which is
        https://pm-redirects.stripe.com/authorize/{acct}/{nonce}. GETting
        that URL with redirects disabled returns 302 → app.midtrans.com/...
        whose path contains the snap_token.
        """
        deadline = time.time() + 60
        last_err = ""
        sess_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        js_id = str(uuid.uuid4())
        params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": sess_id,
            "elements_session_client[stripe_js_id]": js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[stripe_js_locale]": "auto",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": (
                "2025-03-31.basil; checkout_server_update_beta=v1; "
                "checkout_manual_approval_preview=v1"
            ),
        }
        while time.time() < deadline:
            r = self.ext.get(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                params=params,
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 200:
                payload = r.json() or {}
                si = payload.get("setup_intent") or {}
                if si.get("status") == "requires_action":
                    rtu = (si.get("next_action") or {}).get("redirect_to_url") or {}
                    pm_url = rtu.get("url") or ""
                    if pm_url:
                        snap_token = self._fetch_pm_redirect_snap_token(pm_url)
                        self.log(f"[gopay] midtrans snap_token={snap_token}")
                        return snap_token
                last_err = (
                    f"setup_intent status={si.get('status')!r} "
                    f"payment_status={payload.get('payment_status')!r} "
                    f"status={payload.get('status')!r} "
                    f"keys=[{','.join(sorted(payload.keys())[:8])}]"
                )
            else:
                last_err = f"http {r.status_code}: {r.text[:120]}"
            time.sleep(1)
        raise GoPayError(f"snap_token resolution timeout: {last_err}")

    def _fetch_pm_redirect_snap_token(self, pm_url: str) -> str:
        """GET pm-redirects.stripe.com/authorize/... → 302 to midtrans.
        Extract snap_token from the Location header.
        """
        direct = re.search(
            r"app\.midtrans\.com/snap/v[14]/redirection/([a-f0-9-]{36})",
            pm_url,
        )
        if direct:
            return direct.group(1)
        r = self.ext.get(pm_url, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        if r.status_code not in (301, 302, 303, 307, 308):
            raise GoPayError(f"pm-redirects: expected redirect, got {r.status_code}")
        loc = r.headers.get("Location", "")
        m = re.search(r"app\.midtrans\.com/snap/v[14]/redirection/([a-f0-9-]{36})", loc)
        if not m:
            raise GoPayError(f"pm-redirects: no midtrans token in Location={loc!r}")
        return m.group(1)

    def _midtrans_load_transaction(self, snap_token: str):
        """Seed Midtrans cookies, then load transaction metadata."""
        redirection_url = self._midtrans_redirection_url(snap_token)
        try:
            landing = self.ext.get(
                redirection_url,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                    "Referer": "https://pay.openai.com/",
                },
                timeout=DEFAULT_TIMEOUT,
            )
            if landing.status_code >= 400:
                self.log(f"[gopay] midtrans redirection warmup status={landing.status_code}")
        except Exception as e:
            self.log(f"[gopay] midtrans redirection warmup skipped: {e}")

        try:
            self.ext.cookies.set("locale", "en", domain="app.midtrans.com", path="/")
        except Exception:
            pass

        r = self.ext.get(
            f"https://app.midtrans.com/snap/v1/transactions/{snap_token}",
            headers=self._midtrans_headers(snap_token, source=True),
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        merchant = body.get("merchant") or {}
        merchant_id = merchant.get("merchant_id") or ""
        if merchant_id:
            self._midtrans_merchant_id = merchant_id
            try:
                self.ext.cookies.set(
                    f"preferredPayment-{merchant_id}",
                    "gopay",
                    domain="app.midtrans.com",
                    path="/",
                )
            except Exception:
                pass
        enabled = [p.get("type") for p in body.get("enabled_payments", [])]
        self.log(f"[gopay] midtrans enabled_payments={enabled}")
        self._midtrans_warm_snap_side_effects(snap_token)

    def _midtrans_warm_snap_side_effects(self, snap_token: str):
        """Replay non-critical Snap XHRs seen before linking in the browser."""
        try:
            self.ext.post(
                f"https://app.midtrans.com/snap/v1/promos/{snap_token}/search",
                headers=self._midtrans_headers(snap_token, source=True, origin=True),
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as e:
            self.log(f"[gopay] midtrans promos warmup skipped: {e}")
        try:
            self.ext.get(
                "https://app.midtrans.com/snap/v3/experiment",
                params={"id": snap_token},
                headers=self._midtrans_headers(snap_token, source=True),
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as e:
            self.log(f"[gopay] midtrans experiment warmup skipped: {e}")

    def _midtrans_basic_auth(self) -> dict:
        import base64
        token = base64.b64encode(
            f"{self.midtrans_client_id}:".encode("ascii"),
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    @staticmethod
    def _midtrans_redirection_url(snap_token: str) -> str:
        return f"https://app.midtrans.com/snap/v4/redirection/{snap_token}"

    def _midtrans_headers(
        self,
        snap_token: str,
        *,
        json_body: bool = False,
        source: bool = False,
        auth: bool = False,
        origin: bool = False,
    ) -> dict:
        headers = {
            "Accept": "application/json",
            "Referer": self._midtrans_redirection_url(snap_token),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
            origin = True
        if origin:
            headers["Origin"] = "https://app.midtrans.com"
        if source:
            headers.update({
                "x-source": "snap",
                "x-source-app-type": "redirection",
                "x-source-version": "2.3.0",
            })
        if auth:
            headers.update(self._midtrans_basic_auth())
        return headers

    # ───── Step 7: Midtrans linking initiation ─────

    def _midtrans_init_linking(self, snap_token: str) -> str:
        """POST snap/v3/accounts/{snap}/linking. Unlinks on 406, bypasses on 429."""
        url = f"https://app.midtrans.com/snap/v3/accounts/{snap_token}/linking"
        body = {
            "type": "gopay",
            "country_code": self.country_code,
            "phone_number": self.phone,
        }
        base_headers = self._midtrans_headers(snap_token, json_body=True)
        auth_headers = self._midtrans_headers(snap_token, json_body=True, auth=True)
        last_err: Optional[str] = None
        bypass_tried = False
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            r = self.ext.post(url, json=body, headers=auth_headers, timeout=DEFAULT_TIMEOUT)
            ref = self._parse_linking_reference(r)
            if ref:
                self.log(f"[gopay] midtrans linking ok reference={ref}")
                return ref
            if r.status_code == 406:
                try:
                    j = r.json()
                except Exception:
                    j = None
                if isinstance(j, dict):
                    last_err = (j.get("error_messages") or ["?"])[0]
                elif isinstance(j, list) and j:
                    last_err = str(j[0])
                else:
                    last_err = r.text[:120]
                self.log(f"[gopay] midtrans linking 406 ({last_err}), unlink then retry {attempt}/{LINK_RETRY_LIMIT}")
                try:
                    self._midtrans_unlink_gopay(snap_token)
                except Exception as exc:
                    self.log(f"[gopay] midtrans unlink before relink failed: {exc}")
                time.sleep(LINK_RETRY_SLEEP_S)
                continue
            if not bypass_tried and self._linking_is_rate_limited(r):
                bypass_tried = True
                self.log(
                    f"[gopay] midtrans linking rate-limited status={r.status_code}; retrying without Authorization",
                )
                rb = self.ext.post(
                    url, json=body, headers=base_headers, timeout=DEFAULT_TIMEOUT,
                )
                ref = self._parse_linking_reference(rb)
                if ref:
                    self.log(f"[gopay] midtrans linking bypass ok reference={ref}")
                    return ref
                raise GoPayError(
                    f"midtrans linking bypass failed status={rb.status_code} body={rb.text[:300]}",
                )
            raise GoPayError(
                f"midtrans linking unexpected status={r.status_code} body={r.text[:300]}",
            )
        raise GoPayError(f"midtrans linking exhausted retries: {last_err}")

    def _midtrans_unlink_gopay(self, snap_token: str) -> None:
        r = self.ext.delete(
            f"https://app.midtrans.com/snap/v3/accounts/{snap_token}/gopay",
            headers=self._midtrans_headers(snap_token, source=True),
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code in (200, 201, 204, 404):
            self.log(f"[gopay] midtrans unlink status={r.status_code}")
            return
        raise GoPayError(f"midtrans unlink failed status={r.status_code} body={r.text[:200]}")

    @staticmethod
    def _parse_linking_reference(r) -> Optional[str]:
        if r.status_code not in (200, 201):
            return None
        try:
            data = r.json()
        except Exception:
            return None
        m = re.search(r"reference=([a-f0-9-]{36})", data.get("activation_link_url", ""))
        if not m:
            raise GoPayError(f"midtrans linking 201 but no reference: {data}")
        return m.group(1)

    @staticmethod
    def _linking_is_rate_limited(r) -> bool:
        if r.status_code == 429:
            return True
        text = (r.text or "").lower()
        return any(h in text for h in LINK_BYPASS_BODY_HINTS)

    # ───── Step 8-12: GoPay linking ─────

    def _gopay_headers(
        self,
        *,
        json_body: bool = True,
        locale: Optional[str] = None,
        origin: str = "https://merchants-gws-app.gopayapi.com",
        referer: str = "https://merchants-gws-app.gopayapi.com/",
    ) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": origin,
            "Referer": referer,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if locale:
            headers["x-user-locale"] = locale
        return headers

    def _ext_request(self, method: str, url: str, **kwargs: Any):
        request = getattr(self.ext, method)
        for attempt in range(3):
            try:
                return request(url, **kwargs)
            except Exception as exc:
                text = str(exc)
                transient = any(
                    hint in text.lower()
                    for hint in (
                        "tls connect error",
                        "failed to perform",
                        "timed out",
                        "timeout",
                        "connection reset",
                        "connection aborted",
                        "connection refused",
                    )
                )
                if not transient or attempt >= 2:
                    raise
                wait = 2 * (attempt + 1)
                self.log(f"[gopay] transient {method.upper()} error; retrying in {wait}s: {text[:160]}")
                time.sleep(wait)

    def _gopay_validate_reference(self, reference_id: str):
        r = self._ext_request(
            "post",
            "https://gwa.gopayapi.com/v1/linking/validate-reference",
            json={"reference_id": reference_id},
            headers=self._gopay_headers(locale=None),
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        if not r.json().get("success"):
            raise GoPayError(f"validate-reference failed: {r.text[:300]}")

    def _gopay_user_consent(self, reference_id: str):
        r = self._ext_request(
            "post",
            "https://gwa.gopayapi.com/v1/linking/user-consent",
            json={"reference_id": reference_id},
            headers=self._gopay_headers(locale=self.browser_locale),
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        if not r.json().get("success"):
            raise GoPayError(f"user-consent failed: {r.text[:300]}")
        self.log("[gopay] consent ok")

    def _gopay_resend_otp(self, reference_id: str) -> None:
        channel = self.otp_channel.upper()
        r = self._ext_request(
            "post",
            "https://gwa.gopayapi.com/v1/linking/resend-otp",
            json={"reference_id": reference_id, "otp_channel": channel},
            headers=self._gopay_headers(locale=self.browser_locale),
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            raise GoPayError(f"resend-otp failed: {r.status_code} {r.text[:300]}")
        self.log(f"[gopay] resend-otp ok channel={channel}")

    @staticmethod
    def _extract_challenge_details(body: Any) -> tuple[str, str]:
        if not isinstance(body, dict):
            return "", ""
        challenge_id = str(body.get("challenge_id") or body.get("challengeId") or "")
        client_id = str(body.get("client_id") or body.get("clientId") or "")
        if challenge_id or client_id:
            return challenge_id, client_id
        for key in ("data", "challenge", "action", "value"):
            found_id, found_client = GoPayCharger._extract_challenge_details(body.get(key))
            if found_id or found_client:
                return found_id, found_client
        for value in body.values():
            if isinstance(value, dict):
                found_id, found_client = GoPayCharger._extract_challenge_details(value)
                if found_id or found_client:
                    return found_id, found_client
            elif isinstance(value, list):
                for item in value:
                    found_id, found_client = GoPayCharger._extract_challenge_details(item)
                    if found_id or found_client:
                        return found_id, found_client
        return "", ""

    def _gopay_validate_otp(self, reference_id: str, otp: str) -> tuple[str, str]:
        """Returns (challenge_id, client_id) for PIN tokenization."""
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-otp",
            json={"reference_id": reference_id, "otp": otp},
            headers=self._gopay_headers(locale=self.browser_locale),
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise GoPayError(f"validate-otp failed: {data}")
        challenge_id, client_id = self._extract_challenge_details(data)
        if not challenge_id:
            raise GoPayError(f"validate-otp: missing challenge details {data}")
        client_id = client_id or GOPAY_PIN_CLIENT_ID_LINK
        self.log(f"[gopay] otp ok challenge_id={challenge_id[:8]}…")
        return challenge_id, client_id

    def _tokenize_pin(self, challenge_id: str, client_id: str, *, purpose: str) -> str:
        """POST customer.gopayapi.com/api/v1/users/pin/tokens/nb → JWT."""
        if purpose == "linking":
            headers = self._gopay_headers(
                locale=self.pin_locale,
                origin="https://pin-web-client.gopayapi.com",
                referer="https://pin-web-client.gopayapi.com/",
            )
            headers.update({
                "x-appversion": "1.0.0",
                "x-correlation-id": str(uuid.uuid4()),
                "x-is-mobile": "false",
                "x-platform": self.browser_platform,
                "x-request-id": str(uuid.uuid4()),
            })
            body = {
                "challenge_id": challenge_id,
                "client_id": client_id,
                "pin": self.pin,
            }
        elif purpose == "payment":
            headers = self._gopay_headers(locale=None)
            headers["x-request-id"] = str(uuid.uuid4())
            body = {
                "pin": self.pin,
                "challenge_id": challenge_id,
                "client_id": client_id,
            }
        else:
            raise GoPayError(f"unknown pin token purpose={purpose!r}")
        r = self.ext.post(
            "https://customer.gopayapi.com/api/v1/users/pin/tokens/nb",
            json=body,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code in (400, 401, 403):
            raise GoPayPINRejected(f"PIN rejected: {r.text[:200]}")
        r.raise_for_status()
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        # Token can be in different shapes; check common keys
        token = (
            body.get("token")
            or body.get("data", {}).get("token")
            or body.get("data", {}).get("pin_token")
            or ""
        )
        if not token:
            # Some flows return the JWT in a wrapper; check for raw redirect URL
            # hash extraction not needed since the JWT is in the body for /nb endpoints
            raise GoPayError(f"pin tokenize: no token in response {r.text[:300]}")
        return token

    def _gopay_validate_pin(self, reference_id: str, pin_token: str):
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-pin",
            json={"reference_id": reference_id, "token": pin_token},
            headers=self._gopay_headers(locale=self.browser_locale),
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        if not r.json().get("success"):
            raise GoPayError(f"validate-pin failed: {r.text[:300]}")
        self.log("[gopay] linking complete")

    # ───── Step 13: Midtrans charge initiation ─────

    def _midtrans_create_charge(self, snap_token: str) -> str:
        """POST snap/v2/transactions/{snap}/charge → charge_ref like A12..."""
        url = f"https://app.midtrans.com/snap/v2/transactions/{snap_token}/charge"
        headers = self._midtrans_headers(snap_token, json_body=True, source=True)
        r = self.ext.post(
            url,
            json={"payment_type": "gopay", "tokenization": "true", "promo_details": None},
            headers=headers, timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            raise GoPayError(f"midtrans charge failed: HTTP {r.status_code} body={r.text[:600]}")
        data = r.json()
        charge_json = json.dumps(data, ensure_ascii=False)
        body_status = str(data.get("status_code") or "")
        fraud = str(data.get("fraud_status") or "").lower()
        txn_status = str(data.get("transaction_status") or "").lower()
        if fraud == "deny" or txn_status == "deny":
            raise GoPayFraudDeny(f"midtrans fraud denied: {charge_json[:400]}")
        if txn_status in {"settlement", "capture"}:
            self.log(f"[gopay] midtrans charge already settled status={txn_status}")
            return ""
        if body_status and body_status not in {"200", "201", "202"}:
            raise GoPayError(f"midtrans charge body_status={body_status}: {charge_json[:400]}")
        link = str(data.get("gopay_verification_link_url") or "")
        if not link:
            for action in data.get("actions") or []:
                if isinstance(action, dict) and action.get("url"):
                    link = str(action.get("url") or "")
                    break
        if not link:
            for key in ("redirect_url", "url", "deeplink_url"):
                if data.get(key):
                    link = str(data.get(key) or "")
                    break
        m = re.search(r"reference=([A-Za-z0-9]+)", link)
        if not m:
            raise GoPayError(f"midtrans charge: no reference in response {charge_json[:400]}")
        charge_ref = m.group(1)
        self.log(f"[gopay] midtrans charge ref={charge_ref}")
        return charge_ref

    def _midtrans_poll_status(self, snap_token: str) -> dict:
        """Poll Snap transaction status until GoPay settlement is visible."""
        url = f"https://app.midtrans.com/snap/v1/transactions/{snap_token}/status"
        last = ""
        for _ in range(MIDTRANS_STATUS_POLL_LIMIT):
            r = self.ext.get(
                url,
                headers=self._midtrans_headers(snap_token, source=True),
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                status = str(data.get("transaction_status") or "")
                status_code = str(data.get("status_code") or "")
                last = f"status={status!r} status_code={status_code!r}"
                if status in {"settlement", "capture"} or status_code == "200":
                    self.log(f"[gopay] midtrans status ok {last}")
                    return data
                if status in {"deny", "cancel", "expire", "failure"}:
                    raise GoPayError(f"midtrans transaction failed: {data}")
            else:
                last = f"http {r.status_code}: {r.text[:120]}"
            time.sleep(2)
        self.log(f"[gopay] midtrans status poll timeout: {last}")
        return {}

    # ───── Step 14: GoPay charge processing ─────

    def _gopay_payment_validate(self, charge_ref: str):
        # midtrans 创建 charge 后 GoPay 后端要数秒才能 fetch；轮询直到 ready
        for i in range(8):
            r = self.ext.get(
                f"https://gwa.gopayapi.com/v1/payment/validate?reference_id={charge_ref}",
                headers=self._gopay_headers(json_body=False),
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 200 and r.json().get("success"):
                return
            time.sleep(1.5)
        raise GoPayError(f"payment/validate failed after retries: {r.status_code} {r.text[:200]}")

    def _gopay_payment_confirm(self, charge_ref: str) -> tuple[str, str]:
        """Returns (challenge_id, client_id) for the charge PIN."""
        r = self.ext.post(
            f"https://gwa.gopayapi.com/v1/payment/confirm?reference_id={charge_ref}",
            json={"payment_instructions": []},
            headers=self._gopay_headers(locale=None),
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise GoPayError(f"payment/confirm failed: {data}")
        challenge_id, client_id = self._extract_challenge_details(data)
        if not challenge_id:
            raise GoPayError(f"payment/confirm missing challenge details: {data}")
        return challenge_id, client_id or GOPAY_PIN_CLIENT_ID_CHARGE

    def _gopay_payment_process(self, charge_ref: str, pin_token: str):
        r = self.ext.post(
            f"https://gwa.gopayapi.com/v1/payment/process?reference_id={charge_ref}",
            json={
                "challenge": {
                    "type": "GOPAY_PIN_CHALLENGE",
                    "value": {"pin_token": pin_token},
                },
            },
            headers=self._gopay_headers(locale=None),
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            raise GoPayError(f"payment/process {r.status_code}: {r.text[:600]}")
        data = r.json()
        if not data.get("success") or data.get("data", {}).get("next_action") != "payment-success":
            raise GoPayError(f"payment/process failed: {data}")
        self.log("[gopay] charge settled")

    # ───── Step 15: Stripe + ChatGPT verify ─────

    def _chatgpt_verify(self, cs_id: str) -> dict:
        """Poll chatgpt verify until plan is active."""
        deadline = time.time() + 60
        while time.time() < deadline:
            r = self.cs.get(
                "https://chatgpt.com/checkout/verify",
                params={
                    "stripe_session_id": cs_id,
                    "processor_entity": "openai_llc",
                    "plan_type": "plus",
                },
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                self.log("[gopay] chatgpt verify ok")
                return {"state": "succeeded", "cs_id": cs_id}
            time.sleep(2)
        return {"state": "verify_timeout", "cs_id": cs_id}

    # ───── Top-level driver ─────

    def run(self, stripe_pk: str, billing: Optional[dict] = None) -> dict:
        state = self.start_until_otp(stripe_pk, billing=billing)
        otp = self.otp_provider()
        return self.complete_after_otp(state, otp)

    def run_from_redirect(
        self, pm_redirect_url: str, cs_id: str = "", stripe_pk: str = "",
    ) -> dict:
        """半自动模式：用户在浏览器走到 pm-redirects.stripe.com 那一步，把
        URL 粘过来；gopay 接管 Midtrans linking + OTP + PIN + 扣款 + verify。
        """
        snap_token = self._fetch_pm_redirect_snap_token(pm_redirect_url)
        self.log(f"[gopay] midtrans snap_token={snap_token}")
        return self._run_midtrans_and_gopay(snap_token, cs_id, stripe_pk)

    def start_until_otp(self, stripe_pk: str, billing: Optional[dict] = None) -> dict:
        """Run checkout/linking until GoPay has sent the WhatsApp OTP."""
        billing = billing or {}
        cs_id = self._chatgpt_create_checkout()
        pm_id = self._stripe_create_pm(cs_id, stripe_pk, billing)
        confirm_data = self._stripe_confirm(cs_id, pm_id, stripe_pk)
        redirect_url = self._extract_redirect_to_url(confirm_data)
        if redirect_url:
            self.log("[gopay] confirm returned redirect directly")
            snap_token = self._fetch_pm_redirect_snap_token(redirect_url)
        else:
            self._chatgpt_approve(cs_id)
            snap_token = self._follow_redirect_to_midtrans(cs_id, stripe_pk)
        self.log(f"[gopay] midtrans snap_token={snap_token}")
        return self.start_linking_until_otp(snap_token, cs_id, stripe_pk)

    def start_linking_until_otp(
        self, snap_token: str, cs_id: str = "", stripe_pk: str = "",
    ) -> dict:
        """Load Midtrans, trigger GoPay linking OTP, and return resumable state."""
        self._midtrans_load_transaction(snap_token)
        reference_id = self._midtrans_init_linking(snap_token)
        self._gopay_validate_reference(reference_id)
        self._gopay_user_consent(reference_id)
        if self.otp_channel in {"sms", "text", "message"}:
            self._gopay_resend_otp(reference_id)
        else:
            self.log(f"[gopay] OTP delivery channel={self.otp_channel or 'default'}")
        return {
            "cs_id": cs_id,
            "stripe_pk": stripe_pk,
            "snap_token": snap_token,
            "reference_id": reference_id,
            "issued_after_unix": int(time.time() - 15),
        }

    def complete_after_otp(self, state: dict, otp: str) -> dict:
        """Resume a segmented GoPay flow after orchestrator supplies OTP."""
        reference_id = str(state.get("reference_id") or "")
        snap_token = str(state.get("snap_token") or "")
        cs_id = str(state.get("cs_id") or "")
        if not reference_id or not snap_token:
            raise GoPayError("payment flow state is missing reference_id/snap_token")
        otp = (otp or "").strip()
        if not otp:
            raise OTPCancelled("OTP not provided")

        challenge_id, client_id = self._gopay_validate_otp(reference_id, otp)
        pin_token = self._tokenize_pin(challenge_id, client_id, purpose="linking")
        self._gopay_validate_pin(reference_id, pin_token)

        charge_ref = self._midtrans_create_charge(snap_token)
        if charge_ref:
            self._gopay_payment_validate(charge_ref)
            ch2_id, ch2_client = self._gopay_payment_confirm(charge_ref)
            pin_token2 = self._tokenize_pin(ch2_id, ch2_client, purpose="payment")
            self._gopay_payment_process(charge_ref, pin_token2)
        midtrans_status = self._midtrans_poll_status(snap_token)

        if cs_id:
            result = self._chatgpt_verify(cs_id)
            result.update({
                "snap_token": snap_token,
                "charge_ref": charge_ref,
                "midtrans_status": midtrans_status.get("transaction_status", ""),
            })
            return result
        return {
            "state": "succeeded",
            "snap_token": snap_token,
            "charge_ref": charge_ref,
            "midtrans_status": midtrans_status.get("transaction_status", ""),
        }

    def _run_midtrans_and_gopay(
        self, snap_token: str, cs_id: str, stripe_pk: str = "",
    ) -> dict:
        state = self.start_linking_until_otp(snap_token, cs_id, stripe_pk)
        otp = self.otp_provider()
        return self.complete_after_otp(state, otp)


# ──────────────────────────── OTP providers ───────────────────────


def grpc_otp_provider(
    addr: str,
    *,
    timeout: float = 150.0,
    attempts: int = 2,
    purpose: str = "gopay",
    issued_after_slack_s: float = 15.0,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Wait for GoPay OTP through the WhatsApp protocol sidecar gRPC API."""
    if not addr:
        raise GoPayError("gopay.otp source=grpc requires addr")
    attempts = max(1, int(attempts))

    def provider() -> str:
        import grpc
        import otp_pb2
        import otp_pb2_grpc

        issued_after = int(time.time() - max(0.0, issued_after_slack_s))
        last_error = ""
        for attempt in range(1, attempts + 1):
            log(f"[gopay] waiting WhatsApp OTP via protocol gRPC {addr} attempt={attempt}/{attempts}")
            try:
                with grpc.insecure_channel(addr) as channel:
                    stub = otp_pb2_grpc.OtpServiceStub(channel)
                    resp = stub.WaitForOtp(
                        otp_pb2.WaitForOtpRequest(
                            purpose=purpose,
                            timeout_seconds=int(timeout),
                            issued_after_unix=issued_after,
                        ),
                        timeout=float(timeout) + 10.0,
                    )
                if resp.found and resp.otp:
                    return str(resp.otp).strip()
                last_error = resp.error_message or "not found"
            except Exception as exc:
                last_error = str(exc)
            if attempt < attempts:
                log(f"[gopay] OTP not received; retrying ({last_error[:120]})")
        raise OTPCancelled(f"OTP not received after {attempts} gRPC waits; last_error={last_error}")

    return provider


def build_configured_otp_provider(
    gopay_cfg: dict,
    *,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Build the configured OTP provider."""
    otp_cfg = gopay_cfg.get("otp") or gopay_cfg.get("otp_provider") or {}
    if not isinstance(otp_cfg, dict):
        otp_cfg = {}

    source = str(
        gopay_cfg.get("otp_source")
        or otp_cfg.get("source")
        or otp_cfg.get("type")
        or "grpc"
    ).strip().lower()
    unsupported = {
        "", "manual", "cli", "stdin",
        "relay", "whatsapp_http", "wa_http",
        "file", "state_file", "log", "whatsapp_file", "wa_file",
        "command", "cmd",
    }
    if source in unsupported:
        raise GoPayError(
            "unsupported gopay.otp source: "
            f"{source or '<empty>'}; use source=grpc or source=adb"
        )
    if source not in ("auto", "grpc", "whatsapp_grpc", "wa_grpc", "adb", "emulator", "termux", "phone", "http", "https", "smsbower", "sms_bower"):
        raise GoPayError(f"unsupported gopay.otp source: {source}; use source=grpc, source=adb, or source=smsbower")

    def _float_cfg(d: dict, key: str, default: float = 0.0) -> float:
        try:
            return float(d.get(key, default))
        except (TypeError, ValueError):
            return default

    timeout = _float_cfg(otp_cfg, "timeout", _float_cfg(otp_cfg, "timeout_s", 300.0))
    slack = _float_cfg(otp_cfg, "issued_after_slack_s", 15.0)
    attempts = int(_float_cfg(otp_cfg, "attempts", 2.0))
    purpose = str(otp_cfg.get("purpose") or "gopay")

    if source in ("smsbower", "sms_bower"):
        activation = prepare_smsbower_otp(gopay_cfg, log=log)
        gopay_cfg["phone_number"] = activation["phone_number"]
        gopay_cfg["country_code"] = activation["country_code"]

        def provider() -> str:
            return wait_smsbower_otp({"smsbower": activation}, log=log)

        return provider

    if source in ("adb", "emulator", "termux", "phone", "http", "https"):
        sidecar_url = str(
            otp_cfg.get("adb_url")
            or otp_cfg.get("termux_url")
            or otp_cfg.get("url")
            or os.getenv("GOPAY_ADB_URL", "").strip()
            or os.getenv("GOPAY_TERMUX_URL", "").strip()
            or ""
        ).strip()
        if not sidecar_url:
            raise GoPayError("gopay.otp source=adb requires adb_url or GOPAY_ADB_URL")
        poll_interval = _float_cfg(otp_cfg, "poll_interval", 2.0)
        return http_sidecar_otp_provider(
            sidecar_url,
            timeout=timeout,
            poll_interval=poll_interval,
            log=log,
        )

    env_grpc_addr = os.getenv("WEBUI_GOPAY_OTP_GRPC_ADDR", "").strip()
    grpc_addr = str(otp_cfg.get("addr") or otp_cfg.get("grpc_addr") or env_grpc_addr or "").strip()
    if not grpc_addr:
        raise GoPayError("gopay.otp source=grpc requires addr/grpc_addr or WEBUI_GOPAY_OTP_GRPC_ADDR")

    return grpc_otp_provider(
        grpc_addr,
        timeout=timeout,
        attempts=attempts,
        purpose=purpose,
        issued_after_slack_s=slack,
        log=log,
    )


def smsbower_source_enabled(gopay_cfg: dict) -> bool:
    otp_cfg = gopay_cfg.get("otp") or gopay_cfg.get("otp_provider") or {}
    if not isinstance(otp_cfg, dict):
        return False
    source = str(
        gopay_cfg.get("otp_source")
        or otp_cfg.get("source")
        or otp_cfg.get("type")
        or ""
    ).strip().lower()
    return source in {"smsbower", "sms_bower"}


def prepare_smsbower_otp(gopay_cfg: dict, *, log: Callable[[str], None] = print) -> dict[str, Any]:
    otp_cfg = gopay_cfg.get("otp") or {}
    if not isinstance(otp_cfg, dict):
        otp_cfg = {}
    smsbower = otp_cfg.get("smsbower") or gopay_cfg.get("smsbower") or {}
    if not isinstance(smsbower, dict):
        smsbower = {}
    api_key = _resolve_secret(str(smsbower.get("api_key") or ""), "SMSBOWER_API_KEY")
    if not api_key:
        raise GoPayError("gopay.otp.source=smsbower requires gopay.otp.smsbower.api_key or SMSBOWER_API_KEY")
    endpoint = str(smsbower.get("endpoint") or SMSBOWER_ENDPOINT).strip() or SMSBOWER_ENDPOINT
    service = str(smsbower.get("service") or "").strip()
    country = str(smsbower.get("country") or "").strip()
    if not service or not country:
        raise GoPayError("gopay.otp.smsbower.service and country are required for GoPay SMSBower mode")
    params = {
        "service": service,
        "country": country,
    }
    for src, dst in (("max_price", "maxPrice"), ("min_price", "minPrice")):
        value = str(smsbower.get(src) or "").strip()
        if value:
            params[dst] = value
    result = _smsbower_api(api_key, endpoint, "getNumberV2", params)
    if result.startswith("{"):
        data = json.loads(result)
        activation_id = str(data.get("activationId") or data.get("activation_id") or data.get("id") or "").strip()
        phone = _normalize_phone(data.get("phoneNumber") or data.get("phone") or data.get("number") or "")
        price = str(data.get("activationCost") or data.get("price") or "")
    else:
        parts = result.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise GoPayError(f"smsbower getNumber error: {result}")
        activation_id = parts[1]
        phone = _normalize_phone(parts[2])
        price = ""
    if not activation_id or not phone:
        raise GoPayError(f"smsbower getNumber returned incomplete activation: {result[:200]}")
    country_code = str(gopay_cfg.get("country_code") or smsbower.get("phone_country_code") or "62").strip().lstrip("+")
    local_phone = _strip_country_code(phone, country_code)
    log(f"[gopay] smsbower acquired {phone} id={activation_id} service={service} country={country} price={price}")
    activation = {
        "provider": "smsbower",
        "activation_id": activation_id,
        "phone": phone,
        "phone_number": local_phone,
        "country_code": country_code,
        "api_key": api_key,
        "endpoint": endpoint,
        "service": service,
        "country": country,
        "price": price,
        "timeout": int(float(smsbower.get("sms_timeout") or otp_cfg.get("timeout") or 120)),
        "poll_interval": int(float(smsbower.get("sms_poll_interval") or otp_cfg.get("poll_interval") or 5)),
        "completed": False,
    }
    if _truthy(smsbower.get("register_account", gopay_cfg.get("register_smsbower_account", True))):
        try:
            _bootstrap_gojek_account(gopay_cfg, activation, log=log)
        except Exception:
            finish_smsbower_otp({"smsbower": activation}, success=False, log=log)
            raise
    return activation


def wait_smsbower_otp(state: dict, *, log: Callable[[str], None] = print) -> str:
    activation = state.get("smsbower") if isinstance(state.get("smsbower"), dict) else {}
    activation_id = str(activation.get("activation_id") or "").strip()
    api_key = str(activation.get("api_key") or "").strip()
    endpoint = str(activation.get("endpoint") or SMSBOWER_ENDPOINT).strip() or SMSBOWER_ENDPOINT
    if not activation_id or not api_key:
        raise OTPCancelled("smsbower activation missing from payment flow")
    timeout = int(activation.get("timeout") or 120)
    poll_interval = max(1, int(activation.get("poll_interval") or 5))
    if activation.get("request_retry_before_wait"):
        _smsbower_set_status(activation, "3", log=log)
        activation["request_retry_before_wait"] = False
    log(f"[gopay] waiting GoPay OTP via SMSBower id={activation_id} timeout={timeout}s")
    otp = _wait_smsbower_code(activation, timeout=timeout, poll_interval=poll_interval, log=log)
    if otp:
        return otp
    raise OTPCancelled(f"SMSBower OTP timeout after {timeout}s")


def _wait_smsbower_code(
    activation: dict,
    *,
    timeout: int,
    poll_interval: int,
    log: Callable[[str], None] = print,
) -> str:
    activation_id = str(activation.get("activation_id") or "").strip()
    api_key = str(activation.get("api_key") or "").strip()
    endpoint = str(activation.get("endpoint") or SMSBOWER_ENDPOINT).strip() or SMSBOWER_ENDPOINT
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _smsbower_api(api_key, endpoint, "getStatus", {"id": activation_id})
        if result.startswith("STATUS_OK:"):
            otp = result[len("STATUS_OK:"):].strip().strip("'\"")
            if otp:
                log("[gopay] SMSBower OTP received")
                activation["used_codes"] = int(activation.get("used_codes") or 0) + 1
                return otp
        if result.startswith("STATUS_WAIT_RETRY"):
            if not activation.get("logged_wait_retry"):
                log("[gopay] SMSBower waiting for retry OTP")
                activation["logged_wait_retry"] = True
            time.sleep(min(poll_interval, max(1, deadline - time.time())))
            continue
        if result == "STATUS_CANCEL":
            raise OTPCancelled("smsbower activation was cancelled")
        time.sleep(min(poll_interval, max(1, deadline - time.time())))
    return ""


def _wait_smsbower_otp_with_retry(
    activation: dict,
    *,
    first_timeout: int,
    retry_timeout: int,
    retry_callback: Callable[[], dict],
    retry_flow: str,
    log: Callable[[str], None] = print,
) -> str:
    poll_interval = max(1, int(activation.get("poll_interval") or 5))
    code = _wait_smsbower_code(activation, timeout=first_timeout, poll_interval=poll_interval, log=log)
    if code:
        return code
    log(f"[gopay] SMSBower OTP not received after {first_timeout}s; retrying {retry_flow}")
    _smsbower_set_status(activation, "3", log=log)
    retry_result = retry_callback()
    if retry_result.get("status") not in (200, 201):
        raise GoPayError(
            f"Gojek OTP retry failed status={retry_result.get('status')} "
            f"body={str(retry_result.get('body'))[:300]}"
        )
    code = _wait_smsbower_code(activation, timeout=retry_timeout, poll_interval=poll_interval, log=log)
    if code:
        return code
    raise OTPCancelled(f"SMSBower OTP timeout after {first_timeout + retry_timeout}s")


def finish_smsbower_otp(state: dict, *, success: bool, log: Callable[[str], None] = print) -> None:
    activation = state.get("smsbower") if isinstance(state.get("smsbower"), dict) else {}
    if not activation or activation.get("completed"):
        return
    activation_id = str(activation.get("activation_id") or "").strip()
    api_key = str(activation.get("api_key") or "").strip()
    endpoint = str(activation.get("endpoint") or SMSBOWER_ENDPOINT).strip() or SMSBOWER_ENDPOINT
    if not activation_id or not api_key:
        return
    status = "6" if success else "8"
    try:
        _smsbower_api(api_key, endpoint, "setStatus", {"id": activation_id, "status": status})
        activation["completed"] = True
        log(f"[gopay] SMSBower activation {'completed' if success else 'cancelled'} id={activation_id}")
    except Exception as exc:
        log(f"[gopay] SMSBower activation cleanup failed id={activation_id}: {exc}")


def _smsbower_set_status(activation: dict, status: str, *, log: Callable[[str], None] = print) -> str:
    activation_id = str(activation.get("activation_id") or "").strip()
    api_key = str(activation.get("api_key") or "").strip()
    endpoint = str(activation.get("endpoint") or SMSBOWER_ENDPOINT).strip() or SMSBOWER_ENDPOINT
    if not activation_id or not api_key:
        return ""
    result = _smsbower_api(api_key, endpoint, "setStatus", {"id": activation_id, "status": status})
    log(f"[gopay] SMSBower setStatus id={activation_id} status={status} result={result}")
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _int_cfg(*values: Any, default: int = 0) -> int:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return default


def _list_cfg(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = str(value).split(",")
        for item in candidates:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def _extract_gopay_balance_rp(response: dict[str, Any]) -> int:
    if int(response.get("status") or 0) != 200:
        return -1
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    data = body.get("data", [])
    if isinstance(data, list) and data:
        balance = data[0].get("balance", {}) if isinstance(data[0], dict) else {}
        return _int_cfg(balance.get("value"), default=0)
    if isinstance(data, dict):
        balance = data.get("balance", {})
        if isinstance(balance, dict):
            return _int_cfg(balance.get("value"), default=0)
    return 0


def _extract_envelope_ids(text: str) -> tuple[str, str]:
    envelope_id = ""
    link_id = ""
    m = re.search(r"envelope_request_id[:%\s\"'=]*([0-9a-f]{24})", text)
    if m:
        envelope_id = m.group(1)
    m = re.search(r"link_id[:%\s\"'=]*([0-9a-f]{24})", text)
    if m:
        link_id = m.group(1)
    return envelope_id, link_id


def _resolve_gopay_envelope_url(url: str, *, proxy: str = "", log: Callable[[str], None] = print) -> tuple[str, str]:
    text = str(url or "").strip()
    if re.fullmatch(r"[0-9a-f]{24}", text):
        return text, ""
    if not text:
        return "", ""
    proxies = None
    if proxy and proxy.lower().startswith(("http://", "https://")):
        proxies = {"http": proxy, "https": proxy}
    response = requests.get(
        text,
        headers={"User-Agent": "GoPay/2.7.0 (com.gojek.gopay; build:2070; Android, 9)"},
        timeout=20,
        allow_redirects=True,
        proxies=proxies,
    )
    response.raise_for_status()
    body = "\n".join([
        str(getattr(response, "url", "") or ""),
        str(getattr(response, "text", "") or ""),
    ])
    envelope_id, link_id = _extract_envelope_ids(body)
    if not envelope_id and not link_id:
        log(f"[gopay] envelope URL did not expose id: {text}")
    return envelope_id, link_id


def _gopay_error_code(response: dict[str, Any]) -> str:
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    errors = body.get("errors", [])
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        return str(first.get("code") or "")
    return ""


def _claim_one_gopay_envelope(
    client: Any,
    *,
    envelope_id: str = "",
    link_id: str = "",
    log: Callable[[str], None] = print,
) -> bool:
    if envelope_id:
        claim = getattr(client, "envelope_claim", None)
        if callable(claim):
            result = _gojek_call(claim, envelope_id, log=log)
        else:
            get = getattr(client, "_gopay_get", None)
            post = getattr(client, "_gopay_post", None)
            if not callable(get) or not callable(post):
                result = {"status": 0, "body": {"errors": [{"code": "missing_envelope_claim"}]}}
            else:
                details = _gojek_call(get, f"/v1/festivals/envelope-requests/{envelope_id}", log=log)
                if int(details.get("status") or 0) == 200:
                    body = details.get("body") if isinstance(details.get("body"), dict) else {}
                    data = body.get("data") if isinstance(body.get("data"), dict) else {}
                    eid = str(data.get("envelope_request_id") or "")
                    result = _gojek_call(post, "/v1/festivals/envelope-requests", {"envelope_request_id": eid}, log=log)
                else:
                    result = details
        if int(result.get("status") or 0) == 200 and (result.get("body") or {}).get("success", True):
            log(f"[gopay] envelope claimed id={envelope_id}")
            return True
        code = _gopay_error_code(result)
        if code == "GoPay-36009":
            log(f"[gopay] envelope already claimed by this account id={envelope_id}")
            return True
        log(f"[gopay] envelope claim failed id={envelope_id} status={result.get('status')} code={code}")
    if link_id:
        claim_by_link = getattr(client, "envelope_claim_by_link", None)
        if callable(claim_by_link):
            result = _gojek_call(claim_by_link, link_id, log=log)
        else:
            post = getattr(client, "_gopay_post", None)
            if not callable(post):
                result = {"status": 0, "body": {"errors": [{"code": "missing_envelope_claim_by_link"}]}}
            else:
                result = _gojek_call(post, "/v1/festivals/link", {"link_id": link_id}, log=log)
        if int(result.get("status") or 0) == 200 and (result.get("body") or {}).get("success", True):
            log(f"[gopay] envelope link claimed id={link_id}")
            return True
        code = _gopay_error_code(result)
        if code == "GoPay-36009":
            log(f"[gopay] envelope link already claimed by this account id={link_id}")
            return True
        log(f"[gopay] envelope link claim failed id={link_id} status={result.get('status')} code={code}")
    return False


def _claim_configured_gopay_envelope(
    client: Any,
    gopay_cfg: dict,
    *,
    log: Callable[[str], None] = print,
) -> bool:
    otp_cfg = gopay_cfg.get("otp") or {}
    smsbower_cfg = otp_cfg.get("smsbower") if isinstance(otp_cfg, dict) else {}
    if not isinstance(smsbower_cfg, dict):
        smsbower_cfg = {}
    proxy = _tls_client_proxy(str(gopay_cfg.get("proxy") or gopay_cfg.get("proxy_url") or "").strip())
    envelope_ids = _list_cfg(
        smsbower_cfg.get("envelope_deeplink_ids"),
        smsbower_cfg.get("envelope_deeplink_id"),
        gopay_cfg.get("envelope_deeplink_ids"),
        gopay_cfg.get("envelope_deeplink_id"),
        os.getenv("GOPAY_ENVELOPE_DEEPLINK_IDS"),
    )
    envelope_urls = _list_cfg(
        smsbower_cfg.get("envelope_urls"),
        smsbower_cfg.get("envelope_url"),
        gopay_cfg.get("envelope_urls"),
        gopay_cfg.get("envelope_url"),
        os.getenv("GOPAY_ENVELOPE_URLS"),
    )
    for envelope_id in envelope_ids:
        if _claim_one_gopay_envelope(client, envelope_id=envelope_id, log=log):
            return True
    for url in envelope_urls:
        try:
            envelope_id, link_id = _resolve_gopay_envelope_url(url, proxy=proxy, log=log)
        except Exception as exc:
            log(f"[gopay] envelope URL resolve failed url={url}: {exc}")
            continue
        if _claim_one_gopay_envelope(client, envelope_id=envelope_id, link_id=link_id, log=log):
            return True
    return False


def _check_gojek_balance_rp(client: Any, *, log: Callable[[str], None] = print) -> int:
    get_balance = getattr(client, "get_balance", None) or getattr(client, "gopay_get_balances", None)
    if not callable(get_balance):
        return -1
    balance = _extract_gopay_balance_rp(_gojek_call(get_balance, log=log))
    if balance >= 0:
        return balance
    refresh = getattr(client, "refresh_token", None)
    if callable(refresh):
        log("[gopay] GoPay balance check failed; refreshing token")
        _gojek_call(refresh, log=log)
        balance = _extract_gopay_balance_rp(_gojek_call(get_balance, log=log))
    return balance


def _bootstrap_gojek_account(gopay_cfg: dict, activation: dict, *, log: Callable[[str], None] = print) -> None:
    pin = str(gopay_cfg.get("pin") or "147258").strip()
    if not pin:
        raise GoPayError("gopay.pin is required for SMSBower GoPay account registration")
    otp_cfg = gopay_cfg.get("otp") or {}
    smsbower_cfg = otp_cfg.get("smsbower") if isinstance(otp_cfg, dict) else {}
    if not isinstance(smsbower_cfg, dict):
        smsbower_cfg = {}
    min_balance_rp = _int_cfg(
        smsbower_cfg.get("min_balance_rp"),
        gopay_cfg.get("min_balance_rp"),
        os.getenv("OPAI_GOPAY_MIN_BALANCE_RP"),
        default=1,
    )
    GojekClient = _load_gojek_client(gopay_cfg)
    phone = str(activation.get("phone") or "").strip()
    local = str(activation.get("phone_number") or "").strip()
    proxy = _tls_client_proxy(str(gopay_cfg.get("proxy") or gopay_cfg.get("proxy_url") or "").strip())
    client = GojekClient.from_phone(phone, proxy=proxy)
    activation["gojek_phone"] = phone
    log(f"[gopay] GoPay account bootstrap start phone={phone}")

    time.sleep(2)
    methods = _gojek_call(client.get_login_methods, "+62", local, log=log)
    if methods.get("status") in (200, 201):
        raise GoPayError(f"SMSBower phone is already registered as Gojek account: {phone}")
    if methods.get("status") == 403:
        raise GoPayError(f"Gojek login-methods WAF/403 for {phone}: {str(methods.get('body'))[:240]}")

    otp_result = _gojek_call(client.signup_request_otp, phone, log=log)
    if otp_result.get("status") not in (200, 201):
        raise GoPayError(f"Gojek signup OTP failed status={otp_result.get('status')} body={str(otp_result.get('body'))[:300]}")
    signup_otp = _wait_smsbower_otp_with_retry(
        activation,
        first_timeout=60,
        retry_timeout=180,
        retry_callback=lambda: client.retry_otp(flow="signup_na"),
        retry_flow="signup_na",
        log=log,
    )
    time.sleep(2)
    verify = _gojek_call(client.signup_verify_otp, signup_otp, phone, log=log)
    if verify.get("status") not in (200, 201):
        raise GoPayError(f"Gojek signup verify failed status={verify.get('status')} body={str(verify.get('body'))[:300]}")

    names = [
        "Budi Santoso", "Adi Pratama", "Siti Rahayu", "Dewi Lestari",
        "Rizky Ramadhan", "Putri Wulandari", "Agus Setiawan", "Rina Kusuma",
        "Hendra Wijaya", "Novi Anggraini", "Dian Permata", "Wahyu Hidayat",
    ]
    time.sleep(2)
    signup = _gojek_call(client.signup_create_account, name=random.choice(names), phone=phone, email="", country="ID", log=log)
    if signup.get("status") not in (200, 201):
        body = signup.get("body") or {}
        if "phone_already_taken" in str(body):
            raise GoPayError(f"SMSBower phone became registered before signup completed: {phone}")
        raise GoPayError(f"Gojek signup failed status={signup.get('status')} body={str(body)[:300]}")
    if not str(getattr(client.auth, "refresh_token", "") or "").strip():
        raise GoPayError(f"Gojek signup did not return refresh token for {phone}")

    time.sleep(5)
    refresh = _gojek_call(client.refresh_token, log=log)
    if refresh.get("status") not in (200, 201):
        raise GoPayError(f"Gojek token refresh failed status={refresh.get('status')} body={str(refresh.get('body'))[:300]}")

    time.sleep(2)
    _gojek_call(client.gopay_init, log=log)
    time.sleep(2)
    _gojek_call(client.gopay_get_profiles, log=log)
    time.sleep(2)
    profile = _gojek_call(client.get_user_profile, log=log)
    body = profile.get("body") if isinstance(profile.get("body"), dict) else {}
    is_pin_set = bool((body.get("data") or {}).get("is_pin_setup")) if profile.get("status") == 200 else False
    if not is_pin_set:
        _smsbower_set_status(activation, "3", log=log)
        time.sleep(2)
        pin_otp = _gojek_call(client.pin_request_otp, log=log)
        if pin_otp.get("status") not in (200, 201):
            raise GoPayError(f"Gojek PIN OTP request failed status={pin_otp.get('status')} body={str(pin_otp.get('body'))[:300]}")
        pin_code = _wait_smsbower_otp_with_retry(
            activation,
            first_timeout=60,
            retry_timeout=180,
            retry_callback=lambda: client.retry_otp(flow="goto_pin_wa_sms"),
            retry_flow="goto_pin_wa_sms",
            log=log,
        )
        time.sleep(2)
        pin_verify = _gojek_call(client.pin_verify_otp, pin_code, log=log)
        if pin_verify.get("status") not in (200, 201):
            raise GoPayError(f"Gojek PIN OTP verify failed status={pin_verify.get('status')} body={str(pin_verify.get('body'))[:300]}")
        time.sleep(2)
        pin_result = _gojek_call(client.pin_setup, pin, log=log)
        if pin_result.get("status") not in (200, 201):
            raise GoPayError(f"Gojek PIN setup failed status={pin_result.get('status')} body={str(pin_result.get('body'))[:300]}")

    post_registration_hook = getattr(client, "pin_post_registration_hook", None)
    if callable(post_registration_hook):
        time.sleep(2)
        hook = _gojek_call(post_registration_hook, log=log)
        if hook.get("status") in (200, 201, 204):
            log("[gopay] GoPay post-registration hook ok")
        else:
            log(f"[gopay] GoPay post-registration hook failed status={hook.get('status')} body={str(hook.get('body'))[:240]}")
        time.sleep(2)
        _gojek_call(client.gopay_get_profiles, log=log)

    balance_rp = _check_gojek_balance_rp(client, log=log)
    activation["balance_rp"] = balance_rp
    if balance_rp < 0:
        raise GoPayError("GoPay balance check failed before payment")
    log(f"[gopay] GoPay balance={balance_rp} Rp")
    if min_balance_rp > 0 and balance_rp < min_balance_rp:
        if _truthy(smsbower_cfg.get("claim_envelope_on_low_balance", gopay_cfg.get("claim_envelope_on_low_balance", True))):
            log(f"[gopay] GoPay balance below {min_balance_rp} Rp; trying configured envelope")
            if _claim_configured_gopay_envelope(client, gopay_cfg, log=log):
                time.sleep(2)
                balance_rp = _check_gojek_balance_rp(client, log=log)
                activation["balance_rp"] = balance_rp
                if balance_rp >= 0:
                    log(f"[gopay] GoPay balance after envelope={balance_rp} Rp")
        if balance_rp < min_balance_rp:
            raise GoPayError(f"GoPay balance insufficient before payment: balance={balance_rp} Rp required>={min_balance_rp} Rp")

    activation["gojek_registered"] = True
    activation["request_retry_before_wait"] = True
    log(f"[gopay] GoPay account bootstrap ok phone={phone}")


def _gojek_call(fn: Callable, *args: Any, log: Callable[[str], None] = print, **kwargs: Any) -> dict:
    last: dict[str, Any] = {}
    for attempt in range(3):
        last = fn(*args, **kwargs)
        status = int(last.get("status") or 0)
        if status in (200, 201, 204):
            return last
        body = str(last.get("body") or "")
        if (
            status >= 500
            or status == 429
            or "ratelimit" in body.lower()
            or "rate_limit" in body.lower()
            or "WAF Block Page" in body
        ):
            if attempt < 2:
                wait = 5 * (attempt + 1)
                log(f"[gopay] Gojek API retry in {wait}s status={status}")
                time.sleep(wait)
                continue
        return last
    return last


def _load_gojek_client(gopay_cfg: dict) -> Any:
    path = str(gopay_cfg.get("gopay_deploy_src") or os.getenv("GOPAY_DEPLOY_SRC") or "").strip()
    if not path:
        root = Path(__file__).resolve().parents[2]
        sibling = root.parent / "gopay-deploy" / "app" / "src"
        path = str(sibling)
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        from opai.core.gojek_client import GojekClient  # type: ignore
    except Exception as exc:
        raise GoPayError(f"unable to import gopay-deploy GojekClient from {path}: {exc}") from exc
    return GojekClient


def _tls_client_proxy(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if proxy.lower().startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://"):]
    return proxy


def _smsbower_api(api_key: str, endpoint: str, action: str, params: dict[str, Any] | None = None) -> str:
    query = {"api_key": api_key, "action": action}
    if params:
        query.update(params)
    response = requests.get(endpoint, params=query, timeout=20)
    response.raise_for_status()
    return response.text.strip()


def _normalize_phone(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("+"):
        digits = "".join(ch for ch in text[1:] if ch.isdigit())
    elif text.startswith("00"):
        digits = "".join(ch for ch in text[2:] if ch.isdigit())
    else:
        digits = "".join(ch for ch in text if ch.isdigit())
    return f"+{digits}" if digits else ""


def _strip_country_code(phone: str, country_code: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    code = "".join(ch for ch in str(country_code or "") if ch.isdigit())
    if code and digits.startswith(code):
        return digits[len(code):]
    return digits


def _resolve_secret(value: str, env_name: str) -> str:
    value = str(value or "").strip()
    if value.startswith("$"):
        return os.getenv(value[1:], "").strip()
    if value:
        return value
    return os.getenv(env_name, "").strip()


def http_sidecar_otp_provider(
    sidecar_url: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    if not sidecar_url:
        raise GoPayError("gopay.otp HTTP sidecar requires url")
    sidecar_url = sidecar_url.rstrip("/")

    def provider() -> str:
        start = time.time()
        last_error = ""
        log(f"[gopay] waiting OTP via HTTP sidecar {sidecar_url} timeout={timeout}s")
        try:
            requests.post(f"{sidecar_url}/otp/clear", timeout=5, proxies={"http": None, "https": None})
        except Exception:
            pass
        while time.time() - start < timeout:
            try:
                resp = requests.get(
                    f"{sidecar_url}/otp",
                    timeout=5,
                    proxies={"http": None, "https": None},
                    headers={"X-Since-Ts": str(start - 30)},
                )
                resp.raise_for_status()
                data = resp.json()
                otp = data.get("otp")
                ts = float(data.get("ts") or 0)
                if otp and ts > start - 30:
                    log(f"[gopay] OTP received via HTTP sidecar after {time.time() - start:.1f}s")
                    return str(otp).strip()
            except Exception as exc:
                last_error = str(exc)
            time.sleep(poll_interval)
        raise OTPCancelled(f"OTP not received via HTTP sidecar after {timeout}s; last_error={last_error}")

    return provider


# ──────────────────────────── chatgpt session ─────────────────────


def _build_chatgpt_session(auth_cfg: dict) -> Any:
    """Build a chatgpt-authed session with chrome TLS fingerprint + OAI headers.

    /backend-api/payments/checkout requires: Cookie session-token, Bearer
    access_token, oai-device-id, x-openai-target-path/route, sentinel token.
    We supply everything except sentinel — caller refreshes via
    _ensure_sentinel before each protected call.
    """
    session_token = (auth_cfg.get("session_token") or "").strip()
    access_token = (auth_cfg.get("access_token") or "").strip()
    cookie_header = (auth_cfg.get("cookie_header") or "").strip()
    device_id = (auth_cfg.get("device_id") or "").strip() or str(uuid.uuid4())
    user_agent = auth_cfg.get("user_agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )

    if not (session_token or cookie_header):
        raise GoPayError(
            "auth missing: need session_token or cookie_header in config",
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

    parts = []
    seen = set()
    for raw in (cookie_header or "").split(";"):
        p = raw.strip()
        if p and "=" in p:
            n = p.split("=", 1)[0].strip()
            if n and n not in seen:
                seen.add(n)
                parts.append(p)
    if session_token and "__Secure-next-auth.session-token" not in seen:
        parts.append(f"__Secure-next-auth.session-token={session_token}")
    if device_id and "oai-did" not in seen:
        parts.append(f"oai-did={device_id}")
    s.headers["Cookie"] = "; ".join(parts)
    try:
        r = s.get(
            "https://chatgpt.com/api/auth/session",
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Language": s.headers.get("Accept-Language", "en-US,en;q=0.9"),
                "Referer": "https://chatgpt.com/",
                "Cookie": s.headers["Cookie"],
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            refreshed_token = (r.json() or {}).get("accessToken") or ""
            if refreshed_token:
                s.headers["Authorization"] = f"Bearer {refreshed_token}"
    except Exception:
        pass
    # Cache device_id on session for subsequent header use
    s._oai_device_id = device_id  # type: ignore[attr-defined]
    return s


# ──────────────────────────── CLI entry ───────────────────────────


def _load_cfg(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="ChatGPT Plus 订阅 via GoPay tokenization",
    )
    parser.add_argument("--config", required=True, help="GoPay config json")
    parser.add_argument("--json-result", action="store_true",
                        help="Emit GOPAY_RESULT_JSON=... line on success")
    parser.add_argument("--session-token", default="",
                        help="Override ChatGPT __Secure-next-auth.session-token from config")
    parser.add_argument("--from-redirect-url", default="", metavar="URL",
                        help="半自动模式：跳过 chatgpt+stripe 前段，直接从 pm-redirects.stripe.com URL 接管 Midtrans+GoPay")
    parser.add_argument("--cs-id", default="", help="可选：cs_live_xxx，verify 阶段用")
    args = parser.parse_args()

    cfg = _load_cfg(args.config)
    gopay_cfg = cfg.get("gopay") or {}
    if not gopay_cfg:
        print("[error] config has no 'gopay' block", file=sys.stderr)
        sys.exit(2)
    if not all(k in gopay_cfg for k in ("country_code", "phone_number", "pin")):
        print("[error] gopay block missing country_code / phone_number / pin",
              file=sys.stderr)
        sys.exit(2)

    auth_cfg = (cfg.get("fresh_checkout") or {}).get("auth") or {}
    session_token = args.session_token.strip()
    if session_token:
        auth_cfg = dict(auth_cfg)
        auth_cfg["session_token"] = session_token
        auth_cfg.pop("cookie_header", None)
        auth_cfg.pop("access_token", None)
    try:
        cs_session = _build_chatgpt_session(auth_cfg)
    except GoPayError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(2)
    # Apply proxy from config to both chatgpt + ext sessions
    proxy_url = (cfg.get("proxy") or "").strip() or None

    stripe_pk = (
        (cfg.get("stripe") or {}).get("publishable_key")
        or auth_cfg.get("stripe_pk")
        or DEFAULT_STRIPE_PK
    )

    billing = cfg.get("billing") or {}
    if not billing:
        cards = cfg.get("cards") or []
        if cards and isinstance(cards[0], dict):
            card0 = cards[0]
            billing = dict(card0.get("address") or {})
            for key in ("name", "email"):
                if card0.get(key):
                    billing[key] = card0[key]

    provider = build_configured_otp_provider(gopay_cfg)

    charger = GoPayCharger(
        cs_session, gopay_cfg,
        otp_provider=provider, proxy=proxy_url,
        runtime_cfg=cfg.get("runtime"),
    )
    try:
        if args.from_redirect_url:
            print(f"[gopay] semi-auto mode: starting from {args.from_redirect_url[:80]}...")
            result = charger.run_from_redirect(args.from_redirect_url, cs_id=args.cs_id)
        else:
            result = charger.run(stripe_pk=stripe_pk, billing=billing)
    except GoPayError as e:
        print(f"[gopay] FAILED: {e}", file=sys.stderr)
        if args.json_result:
            print(f"GOPAY_RESULT_JSON={json.dumps({'state':'failed','error':str(e)})}")
        sys.exit(1)

    print(f"[gopay] result: {result}")
    if args.json_result:
        print(f"GOPAY_RESULT_JSON={json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
