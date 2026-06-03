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
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from snap_signature import sign_snap_request, snap_json_body

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
SMSBOWER_API_TIMEOUT = 20
SMSBOWER_API_ATTEMPTS = 3
SMSBOWER_API_RETRY_SLEEP_S = 2.0


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
        self.snap_signing_key = str(
            self.runtime.get("snap_signing_key")
            or gopay_cfg.get("snap_signing_key")
            or os.getenv("MIDTRANS_SNAP_SIGNING_KEY", "")
        ).strip()
        self._snap_signature_warned = False
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
        snap_path: str | None = None,
        snap_body: Any = "",
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
        if snap_path:
            signed = sign_snap_request(snap_path, snap_body, signing_key=self.snap_signing_key)
            if signed:
                headers.update(signed)
            elif not self._snap_signature_warned:
                self._snap_signature_warned = True
                self.log("[gopay] Midtrans Snap signing key missing; sending unsigned Snap request")
        return headers

    # ───── Step 7: Midtrans linking initiation ─────

    def _midtrans_init_linking(self, snap_token: str) -> str:
        """POST snap/v3/accounts/{snap}/linking. Unlinks on 406, bypasses on 429."""
        url = f"https://app.midtrans.com/snap/v3/accounts/{snap_token}/linking"
        link_path = f"/snap/v3/accounts/{snap_token}/linking"
        body = {
            "type": "gopay",
            "country_code": self.country_code,
            "phone_number": self.phone,
        }
        wire = snap_json_body(body)
        base_headers = self._midtrans_headers(
            snap_token,
            json_body=True,
            snap_path=link_path,
            snap_body=body,
        )
        auth_headers = self._midtrans_headers(
            snap_token,
            json_body=True,
            auth=True,
            snap_path=link_path,
            snap_body=body,
        )
        last_err: Optional[str] = None
        bypass_tried = False
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            r = self.ext.post(url, data=wire, headers=auth_headers, timeout=DEFAULT_TIMEOUT)
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
                    url, data=wire, headers=base_headers, timeout=DEFAULT_TIMEOUT,
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
        charge_path = f"/snap/v2/transactions/{snap_token}/charge"
        charge_body = {"payment_type": "gopay", "tokenization": "true", "promo_details": None}
        headers = self._midtrans_headers(
            snap_token,
            json_body=True,
            source=True,
            snap_path=charge_path,
            snap_body=charge_body,
        )
        r = self.ext.post(
            url,
            data=snap_json_body(charge_body),
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
        status_path = f"/snap/v1/transactions/{snap_token}/status"
        last = ""
        for _ in range(MIDTRANS_STATUS_POLL_LIMIT):
            r = self.ext.get(
                url,
                headers=self._midtrans_headers(
                    snap_token,
                    source=True,
                    snap_path=status_path,
                    snap_body="",
                ),
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
    attempts = max(1, int(float(smsbower.get("number_attempts") or gopay_cfg.get("smsbower_number_attempts") or 3)))
    register_account = _truthy(smsbower.get("register_account", gopay_cfg.get("register_smsbower_account", True)))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
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
        log(f"[gopay] smsbower acquired {phone} id={activation_id} service={service} country={country} price={price} attempt={attempt}/{attempts}")
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
        if not register_account:
            return activation
        try:
            _bootstrap_gojek_account(gopay_cfg, activation, log=log)
            return activation
        except Exception as exc:
            last_error = exc
            finish_smsbower_otp({"smsbower": activation}, success=False, log=log)
            if _retry_smsbower_gopay_bootstrap_error(exc):
                if attempt < attempts:
                    log(f"[gopay] SMSBower phone rejected by GoPay ({exc}); trying next number")
                    continue
                raise GoPayError(f"SMSBower GoPay registration failed after {attempts} number attempt(s): {exc}") from exc
            raise
    raise GoPayError(f"SMSBower GoPay registration failed after {attempts} number attempt(s): {last_error}")


def _retry_smsbower_gopay_bootstrap_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "phone_registered",
            "phone is already registered",
            "already registered as gojek",
            "phone_already_taken",
            "nomor sudah",
            "registered",
            "signup otp initiate rate limited",
            "ratelimit:init_verification",
            "unable to continue",
        )
    )


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
    retry_ok = retry_result.get("status") in (200, 201, 202, 204) or _rpc_success(retry_result)
    if not retry_ok:
        raise GoPayError(
            f"Gojek OTP retry failed status={retry_result.get('status')} "
            f"body={str(retry_result.get('body') or retry_result.get('errorMessage') or retry_result)[:300]}"
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


def _balance_wait_cfg(gopay_cfg: dict, smsbower_cfg: dict) -> tuple[int, int]:
    timeout = _int_cfg(
        smsbower_cfg.get("balance_wait_timeout_seconds"),
        smsbower_cfg.get("balance_wait_timeout"),
        gopay_cfg.get("balance_wait_timeout_seconds"),
        gopay_cfg.get("balance_wait_timeout"),
        os.getenv("OPAI_GOPAY_BALANCE_WAIT_TIMEOUT_SECONDS"),
        default=120,
    )
    interval = _int_cfg(
        smsbower_cfg.get("balance_poll_interval_seconds"),
        smsbower_cfg.get("balance_poll_interval"),
        gopay_cfg.get("balance_poll_interval_seconds"),
        gopay_cfg.get("balance_poll_interval"),
        os.getenv("OPAI_GOPAY_BALANCE_POLL_INTERVAL_SECONDS"),
        default=5,
    )
    return max(0, timeout), max(1, interval)


def _wait_for_gojek_min_balance(
    client: Any,
    *,
    min_balance_rp: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
    log: Callable[[str], None] = print,
) -> int:
    if min_balance_rp <= 0:
        return _check_gojek_balance_rp(client, log=log)
    deadline = time.time() + max(0, timeout_seconds)
    last_balance = -1
    log(
        f"[gopay] waiting for GoPay min balance: required>={min_balance_rp} Rp "
        f"timeout={timeout_seconds}s poll={poll_interval_seconds}s"
    )
    while True:
        last_balance = _check_gojek_balance_rp(client, log=log)
        if last_balance >= min_balance_rp:
            log(f"[gopay] GoPay balance ready={last_balance} Rp")
            return last_balance
        remaining = deadline - time.time()
        if remaining <= 0:
            return last_balance
        if last_balance >= 0:
            log(f"[gopay] GoPay balance not ready: {last_balance} Rp < {min_balance_rp} Rp")
        else:
            log("[gopay] GoPay balance check not ready")
        time.sleep(min(float(poll_interval_seconds), max(0.1, remaining)))


class PureGoPayPhoneAlreadyRegistered(GoPayError):
    pass


def _pure_jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _pure_has_error_code(data: Any, code: str) -> bool:
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list):
            return any(isinstance(item, dict) and item.get("code") == code for item in errors)
        return any(_pure_has_error_code(item, code) for item in data.values())
    if isinstance(data, list):
        return any(_pure_has_error_code(item, code) for item in data)
    return False


def _pure_phone_registered_error(data: Any) -> bool:
    text = json.dumps(data, ensure_ascii=False, default=str) if not isinstance(data, str) else data
    return any(marker in text for marker in (
        "CO:CUST:phone_already_taken",
        "Nomor HP-mu sudah terdaftar",
        "phone_already_taken",
        "already registered",
    ))


def _pure_success(status: int, data: Any, allow: tuple[int, ...] = (200, 201, 202)) -> bool:
    if status not in allow:
        return False
    return not (isinstance(data, dict) and data.get("success") is False)


def _pure_require_success(step: str, status: int, data: Any, allow: tuple[int, ...] = (200, 201, 202)) -> None:
    if not _pure_success(status, data, allow=allow):
        raise GoPayError(f"{step} failed: HTTP {status} {_pure_jdump(data)[:800]}")


def _pure_is_waf_html(status: int, data: Any) -> bool:
    raw = data.get("raw", "") if isinstance(data, dict) else ""
    return status == 403 and isinstance(raw, str) and ("WAF Block Page" in raw or "Tencent Cloud WAF" in raw)


def _pure_extract_account_id(data: Any) -> str:
    candidates: list[tuple[int, int, str]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lower = str(key).lower()
                if lower in {"account_id", "accountid", "customer_id", "userid", "user_id", "id"}:
                    if isinstance(item, (str, int)) and re.fullmatch(r"\d{5,20}", str(item)):
                        candidates.append((0 if "account" in lower else 1, depth, str(item)))
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(data)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1], len(item[2])))
    return candidates[0][2]


def _pure_step(status: int, data: Any) -> dict[str, Any]:
    return {"status": status, "body": data}


def _pure_pick_method(data: Any, pick_first: Callable[[Any, Any], Any]) -> tuple[str, str]:
    verification_id = str(pick_first(data, ["verification_id", "challenge_id"]) or "")
    default_method = str(pick_first(data, ["default_method"]) or "otp_sms")
    method = default_method
    methods = pick_first(data, ["methods"])
    if isinstance(methods, list) and "otp_sms" in methods:
        method = "otp_sms"
    return verification_id, method


def _pure_balance_rp(gp: Any, access_token: str, refresh_token: str, *, log: Callable[[str], None]) -> tuple[int, str, str]:
    from gopay_pure_protocol import CUSTOMER, pick_first

    sc, data, _headers = gp.get(CUSTOMER, "/v1/payment-options/balances", auth=access_token)
    balance = _extract_gopay_balance_rp({"status": sc, "body": data})
    if balance >= 0:
        return balance, access_token, refresh_token
    if refresh_token:
        log("[gopay] pure protocol balance check failed; refreshing token")
        sc2, data2, _headers2 = gp.token(refresh_token=refresh_token, account_id="")
        if _pure_success(sc2, data2, allow=(200, 201, 202)):
            access_token = str(pick_first(data2, ["access_token", "accessToken"]) or access_token)
            refresh_token = str(pick_first(data2, ["refresh_token", "refreshToken"]) or refresh_token)
            sc, data, _headers = gp.get(CUSTOMER, "/v1/payment-options/balances", auth=access_token)
            balance = _extract_gopay_balance_rp({"status": sc, "body": data})
    return balance, access_token, refresh_token


def _pure_wait_for_min_balance(
    gp: Any,
    access_token: str,
    refresh_token: str,
    *,
    min_balance_rp: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
    log: Callable[[str], None],
) -> tuple[int, str, str]:
    if min_balance_rp <= 0:
        balance, access_token, refresh_token = _pure_balance_rp(gp, access_token, refresh_token, log=log)
        return balance, access_token, refresh_token
    deadline = time.time() + max(0, timeout_seconds)
    last_balance = -1
    log(
        f"[gopay] waiting for GoPay min balance: required>={min_balance_rp} Rp "
        f"timeout={timeout_seconds}s poll={poll_interval_seconds}s"
    )
    while True:
        last_balance, access_token, refresh_token = _pure_balance_rp(gp, access_token, refresh_token, log=log)
        if last_balance >= min_balance_rp:
            log(f"[gopay] GoPay balance ready={last_balance} Rp")
            return last_balance, access_token, refresh_token
        remaining = deadline - time.time()
        if remaining <= 0:
            return last_balance, access_token, refresh_token
        if last_balance >= 0:
            log(f"[gopay] GoPay balance not ready: {last_balance} Rp < {min_balance_rp} Rp")
        else:
            log("[gopay] GoPay balance check not ready")
        time.sleep(min(float(poll_interval_seconds), max(0.1, remaining)))


def _gopay_envelope_ids(gopay_cfg: dict, smsbower_cfg: dict) -> list[str]:
    values: list[Any] = []
    for key in (
        "envelope_deeplink_id",
        "envelope_deeplink",
        "envelope_url",
        "red_envelope_deeplink_id",
        "red_envelope_url",
    ):
        values.append(smsbower_cfg.get(key))
        values.append(gopay_cfg.get(key))
    for key in ("envelope_links", "red_envelope_links"):
        values.append(smsbower_cfg.get(key))
        values.append(gopay_cfg.get(key))
    values.append(os.getenv("OPAI_GOPAY_ENVELOPE_DEEPLINK", ""))

    out: list[str] = []

    def add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(item)
            return
        text = str(raw).strip()
        if not text:
            return
        for piece in re.split(r"[\s,]+", text):
            piece = piece.strip()
            if not piece:
                continue
            did = piece
            if piece.startswith(("http://", "https://")):
                parsed = urllib.parse.urlparse(piece)
                query = urllib.parse.parse_qs(parsed.query)
                did = (
                    (query.get("deeplink_id") or query.get("did") or query.get("id") or [""])[0]
                    or parsed.path.rstrip("/").rsplit("/", 1)[-1]
                )
            did = did.strip().strip("/")
            if did and did not in out:
                out.append(did)

    for value in values:
        add(value)
    return out


def _pure_claim_envelope(
    gp: Any,
    access_token: str,
    envelope_id: str,
    *,
    log: Callable[[str], None],
) -> bool:
    from gopay_pure_protocol import CUSTOMER, pick_first

    safe_id = urllib.parse.quote(str(envelope_id).strip(), safe="")
    if not safe_id:
        return False
    sc, data, _headers = gp.get(CUSTOMER, f"/v1/festivals/envelope-requests/{safe_id}", auth=access_token)
    if not _pure_success(sc, data, allow=(200, 201, 202)):
        log(f"[gopay] red envelope detail failed id={envelope_id}: HTTP {sc} {_pure_jdump(data)[:240]}")
        return False
    envelope_request_id = str(
        pick_first(data, ["envelope_request_id", "envelopeRequestId", "request_id", "requestId"]) or ""
    )
    if not envelope_request_id:
        log(f"[gopay] red envelope detail missing envelope_request_id id={envelope_id}")
        return False
    sc, data, _headers = gp.post(
        CUSTOMER,
        "/v1/festivals/envelope-requests",
        {"envelope_request_id": envelope_request_id},
        auth=access_token,
    )
    if _pure_success(sc, data, allow=(200, 201, 202)):
        log(f"[gopay] red envelope claim accepted id={envelope_id}")
        return True
    log(f"[gopay] red envelope claim failed id={envelope_id}: HTTP {sc} {_pure_jdump(data)[:240]}")
    return False


def _pure_fund_account_before_payment(
    gopay_cfg: dict,
    smsbower_cfg: dict,
    gp: Any,
    access_token: str,
    refresh_token: str,
    *,
    phone: str,
    current_balance_rp: int,
    min_balance_rp: int,
    poll_interval_seconds: int,
    log: Callable[[str], None],
) -> tuple[int, str, str, str]:
    if min_balance_rp <= 0:
        return current_balance_rp, access_token, refresh_token, "not_required"
    if current_balance_rp >= min_balance_rp:
        return current_balance_rp, access_token, refresh_token, "welcome"

    welcome_wait = _int_cfg(
        smsbower_cfg.get("welcome_wait_seconds"),
        smsbower_cfg.get("welcome_gift_wait_seconds"),
        gopay_cfg.get("welcome_wait_seconds"),
        gopay_cfg.get("welcome_gift_wait_seconds"),
        os.getenv("OPAI_GOPAY_WELCOME_WAIT_SEC"),
        default=300,
    )
    log(f"[gopay] waiting for GoPay welcome balance phone={phone} timeout={welcome_wait}s")
    balance_rp, access_token, refresh_token = _pure_wait_for_min_balance(
        gp,
        access_token,
        refresh_token,
        min_balance_rp=min_balance_rp,
        timeout_seconds=welcome_wait,
        poll_interval_seconds=poll_interval_seconds,
        log=log,
    )
    if balance_rp >= min_balance_rp:
        return balance_rp, access_token, refresh_token, "welcome"

    envelope_ids = _gopay_envelope_ids(gopay_cfg, smsbower_cfg)
    if envelope_ids:
        log(f"[gopay] welcome balance not ready; trying {len(envelope_ids)} red envelope link(s)")
        claimed = False
        for envelope_id in envelope_ids:
            claimed = _pure_claim_envelope(gp, access_token, envelope_id, log=log) or claimed
            if claimed:
                break
        if claimed:
            settle_wait = _int_cfg(
                smsbower_cfg.get("fund_wait_timeout_seconds"),
                smsbower_cfg.get("balance_wait_timeout_seconds"),
                gopay_cfg.get("fund_wait_timeout_seconds"),
                gopay_cfg.get("balance_wait_timeout_seconds"),
                os.getenv("OPAI_GOPAY_FUND_WAIT_TIMEOUT_SECONDS"),
                default=120,
            )
            balance_rp, access_token, refresh_token = _pure_wait_for_min_balance(
                gp,
                access_token,
                refresh_token,
                min_balance_rp=min_balance_rp,
                timeout_seconds=settle_wait,
                poll_interval_seconds=poll_interval_seconds,
                log=log,
            )
            if balance_rp >= min_balance_rp:
                return balance_rp, access_token, refresh_token, "envelope"
    else:
        log("[gopay] no red envelope configured; skipping envelope funding")

    if _truthy(gopay_cfg.get("master_transfer_enabled", False)):
        log("[gopay] master transfer is configured but not supported in pure Python bootstrap; skipping")
    return balance_rp, access_token, refresh_token, "none"


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
    balance_wait_timeout, balance_poll_interval = _balance_wait_cfg(gopay_cfg, smsbower_cfg)
    phone = str(activation.get("phone") or "").strip()
    if not phone:
        raise GoPayError("SMSBower activation phone missing before GoPay signup")

    from gopay_pure_protocol import (
        AUTH_SECRET,
        SIGNUP_BASIC_SUFFIX,
        SIGNUP_CLIENT_NAME,
        SIGNUP_CLIENT_SECRET,
        SIGNUP_XOR_SECRET_CANDIDATE,
        DeviceProfile,
        EnhancedPythonXESigner,
        GoPayProtocol,
        PurePythonXESigner,
        normalize_id_phone,
        pick_first,
    )

    country_code, local = normalize_id_phone(phone)
    if str(activation.get("country_code") or "").strip():
        country_code = "+" + str(activation.get("country_code")).strip().lstrip("+")

    xe_mode = str(gopay_cfg.get("pure_xe_mode") or os.getenv("GOPAY_XE_MODE") or "enhanced").strip().lower()
    xe_key = str(gopay_cfg.get("pure_xe_resolution_key") or os.getenv("GOPAY_XE_RESOLUTION_KEY") or "").strip()
    xe_random = str(gopay_cfg.get("pure_xe_random_hex") or os.getenv("GOPAY_XE_RANDOM_HEX") or "").strip() or None
    if xe_mode == "pure":
        signer = PurePythonXESigner(resolution_key=xe_key, random_hex=xe_random) if xe_key else PurePythonXESigner(random_hex=xe_random)
    else:
        signer = EnhancedPythonXESigner(resolution_key=xe_key, random_hex=xe_random) if xe_key else EnhancedPythonXESigner(random_hex=xe_random)
    device = DeviceProfile.default(
        unique_id=str(gopay_cfg.get("pure_device_id") or "").strip() or None,
        x_m1=str(gopay_cfg.get("pure_x_m1") or "").strip() or None,
    )
    gp = GoPayProtocol(
        device=device,
        signer=signer,
        debug=_truthy(gopay_cfg.get("pure_protocol_debug", False)),
        dry_run=False,
        proxy=_tls_client_proxy(str(gopay_cfg.get("proxy") or gopay_cfg.get("proxy_url") or "").strip()),
        timeout=_int_cfg(gopay_cfg.get("pure_protocol_timeout_seconds"), gopay_cfg.get("timeout_seconds"), default=35),
    )
    activation["gojek_phone"] = phone
    activation["pure_protocol"] = {
        "signer": getattr(signer, "name", xe_mode),
        "device_unique_id": device.unique_id,
    }
    log(f"[gopay] GoPay pure protocol bootstrap start phone={phone}")

    try:
        sc, data, _headers = gp.login_methods(local, country_code)
        if _pure_has_error_code(data, "auth:error:user:not_found"):
            log("[gopay] pure protocol fresh signup: login_methods=user:not_found")
            sc, data, _headers = gp.cvs_methods(local, flow="signup", country_code=country_code)
        elif sc in (200, 201, 202):
            raise PureGoPayPhoneAlreadyRegistered(f"SMSBower phone is already registered as Gojek account: {phone}")
        else:
            _pure_require_success("login_methods", sc, data)
        _pure_require_success("cvs_methods/login_methods", sc, data)

        verification_id, method = _pure_pick_method(data, pick_first)
        if not verification_id:
            raise GoPayError(f"pure protocol login/cvs methods missing verification_id: {_pure_jdump(data)[:500]}")
        sc, data, _headers = gp.cvs_initiate(local, verification_id, method=method, flow="signup", country_code=country_code)
        _pure_require_success("cvs_initiate", sc, data, allow=(200, 201, 202, 204))
        otp_token = str(pick_first(data, ["otp_token", "otpToken"]) or "")
        signup_otp = _wait_smsbower_otp_with_retry(
            activation,
            first_timeout=60,
            retry_timeout=180,
            retry_callback=lambda: _pure_step(*gp.cvs_retry(otp_token, method=method, flow="signup")[:2]),
            retry_flow="signup",
            log=log,
        )

        sc, data, _headers = gp.cvs_verify(
            local,
            verification_id,
            signup_otp,
            method=method,
            flow="signup",
            country_code=country_code,
            otp_token=otp_token,
        )
        _pure_require_success("cvs_verify", sc, data)

        verification_token = pick_first(data, ["verification_token", "verificationToken", "device_verification_token", "device_verification_token_id"])
        auth_code = pick_first(data, ["authorization_code", "auth_code", "code"])
        access_token = pick_first(data, ["access_token", "accessToken"])
        refresh_token = pick_first(data, ["refresh_token", "refreshToken"])
        account_id = ""
        signup_created_without_token = False

        if verification_token and not access_token:
            names = [
                "Budi Santoso", "Adi Pratama", "Siti Rahayu", "Dewi Lestari",
                "Rizky Ramadhan", "Putri Wulandari", "Agus Setiawan", "Rina Kusuma",
                "Hendra Wijaya", "Novi Anggraini", "Dian Permata", "Wahyu Hidayat",
            ]
            signup_name = str(gopay_cfg.get("signup_name") or random.choice(names)).strip()
            variants = [
                {
                    "label": "auth_id_authsecret_cc62_escaped",
                    "client_name": "gopay:consumer:app",
                    "client_secret": AUTH_SECRET,
                    "basic": SIGNUP_BASIC_SUFFIX,
                    "signed_up_country": "62",
                    "escape_client_name_colon": True,
                },
                {
                    "label": "configured",
                    "client_name": str(gopay_cfg.get("signup_client_name") or SIGNUP_CLIENT_NAME),
                    "client_secret": str(gopay_cfg.get("signup_client_secret") or SIGNUP_CLIENT_SECRET),
                    "basic": str(gopay_cfg.get("signup_basic") or SIGNUP_BASIC_SUFFIX),
                    "signed_up_country": str(gopay_cfg.get("signed_up_country") or "62"),
                    "escape_client_name_colon": False,
                },
                {
                    "label": "gopay_consumer_authsecret_cc62",
                    "client_name": SIGNUP_CLIENT_NAME,
                    "client_secret": AUTH_SECRET,
                    "basic": SIGNUP_BASIC_SUFFIX,
                    "signed_up_country": "62",
                    "escape_client_name_colon": False,
                },
                {
                    "label": "gopay_consumer_xorsecret_cc62",
                    "client_name": SIGNUP_CLIENT_NAME,
                    "client_secret": SIGNUP_XOR_SECRET_CANDIDATE,
                    "basic": SIGNUP_BASIC_SUFFIX,
                    "signed_up_country": "62",
                    "escape_client_name_colon": False,
                },
                {
                    "label": "gopay_consumer_authsecret_ID",
                    "client_name": SIGNUP_CLIENT_NAME,
                    "client_secret": AUTH_SECRET,
                    "basic": SIGNUP_BASIC_SUFFIX,
                    "signed_up_country": "ID",
                    "escape_client_name_colon": False,
                },
            ]
            seen: set[tuple[Any, ...]] = set()
            last_signup: tuple[int, Any] | None = None
            waf_retries = max(0, int(float(gopay_cfg.get("signup_waf_retries") or os.getenv("GOPAY_SIGNUP_WAF_RETRIES") or 3)))
            waf_sleep = max(0.0, float(gopay_cfg.get("signup_waf_sleep") or os.getenv("GOPAY_SIGNUP_WAF_SLEEP") or 2.0))
            for variant in variants:
                key = (
                    variant["client_name"],
                    variant["client_secret"],
                    variant["basic"],
                    variant["signed_up_country"],
                    bool(variant["escape_client_name_colon"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                log(f"[gopay] pure protocol customer_signup variant={variant['label']}")
                for waf_try in range(waf_retries + 1):
                    sc, signup_data, _headers = gp.customer_signup(
                        local,
                        signup_name,
                        country_code=country_code,
                        verification_token=str(verification_token),
                        signup_client_name=str(variant["client_name"]),
                        signup_client_secret=str(variant["client_secret"]),
                        signup_basic=str(variant["basic"]),
                        signed_up_country=str(variant["signed_up_country"]),
                        escape_client_name_colon=bool(variant["escape_client_name_colon"]),
                    )
                    if not _pure_is_waf_html(sc, signup_data) or waf_try >= waf_retries:
                        break
                    log(f"[gopay] pure protocol customer_signup WAF 403; retrying after {waf_sleep}s ({waf_try + 1}/{waf_retries})")
                    time.sleep(waf_sleep)
                last_signup = (sc, signup_data)
                if _pure_phone_registered_error(signup_data):
                    raise PureGoPayPhoneAlreadyRegistered(f"SMSBower phone became registered before signup completed: {phone}")
                account_id = account_id or _pure_extract_account_id(signup_data)
                access_token = access_token or pick_first(signup_data, ["access_token", "accessToken"])
                refresh_token = refresh_token or pick_first(signup_data, ["refresh_token", "refreshToken"])
                auth_code = auth_code or pick_first(signup_data, ["authorization_code", "auth_code", "code"])
                if access_token or _pure_success(sc, signup_data, allow=(200, 201, 202, 206)):
                    log(f"[gopay] pure protocol customer_signup accepted variant={variant['label']} access_token={bool(access_token)}")
                    signup_created_without_token = not bool(access_token)
                    break
            if not access_token and not signup_created_without_token and last_signup is not None:
                raise GoPayError(f"pure protocol customer_signup failed: HTTP {last_signup[0]} {_pure_jdump(last_signup[1])[:800]}")

        if signup_created_without_token and not access_token:
            log("[gopay] pure protocol signup created customer without token; running post-signup OTP login")
            sc, data_lm, _headers = gp.login_methods(local, country_code)
            _pure_require_success("post_signup_login_methods", sc, data_lm)
            login_verification_id, login_method = _pure_pick_method(data_lm, pick_first)
            if not login_verification_id:
                raise GoPayError(f"post_signup_login_methods missing verification_id: {_pure_jdump(data_lm)[:500]}")
            sc, data_li, _headers = gp.cvs_initiate(local, login_verification_id, method=login_method, flow="login_1fa", country_code=country_code)
            _pure_require_success("post_signup_login_cvs_initiate", sc, data_li, allow=(200, 201, 202, 204))
            login_otp_token = str(pick_first(data_li, ["otp_token", "otpToken"]) or "")
            login_otp = _wait_smsbower_otp_with_retry(
                activation,
                first_timeout=60,
                retry_timeout=180,
                retry_callback=lambda: _pure_step(*gp.cvs_retry(login_otp_token, method=login_method, flow="login_1fa")[:2]),
                retry_flow="login_1fa",
                log=log,
            )
            sc, data_lv, _headers = gp.cvs_verify(
                local,
                login_verification_id,
                login_otp,
                method=login_method,
                flow="login_1fa",
                country_code=country_code,
                otp_token=login_otp_token,
            )
            _pure_require_success("post_signup_login_cvs_verify", sc, data_lv)
            login_verification_token = pick_first(data_lv, ["verification_token", "verificationToken"])
            if not login_verification_token:
                raise GoPayError(f"post_signup_login_cvs_verify missing verification_token: {_pure_jdump(data_lv)[:500]}")
            sc, data_acct, _headers = gp.accountlist(str(login_verification_token))
            _pure_require_success("post_signup_login_accountlist", sc, data_acct)
            account_id = _pure_extract_account_id(data_acct)
            one_fa_token = pick_first(data_acct, ["1fa_token", "one_fa_token", "token"])
            if not account_id or not one_fa_token:
                raise GoPayError(f"post_signup_login_accountlist missing account_id/1fa_token: {_pure_jdump(data_acct)[:500]}")
            sc, data_tok, _headers = gp.token(verification_token=str(one_fa_token), account_id=account_id)
            _pure_require_success("post_signup_login_token", sc, data_tok, allow=(200, 201, 202))
            access_token = pick_first(data_tok, ["access_token", "accessToken"])
            refresh_token = pick_first(data_tok, ["refresh_token", "refreshToken"])

        if not access_token:
            if verification_token:
                sc, token_data, _headers = gp.token(verification_token=str(verification_token), account_id=account_id or local)
            elif auth_code:
                sc, token_data, _headers = gp.token(authorization_code=str(auth_code), account_id=account_id or local)
            else:
                raise GoPayError("pure protocol OTP verify returned no access token, verification token, or auth code")
            _pure_require_success("goto_auth_token", sc, token_data)
            access_token = pick_first(token_data, ["access_token", "accessToken"])
            refresh_token = pick_first(token_data, ["refresh_token", "refreshToken"])
        if not access_token:
            raise GoPayError("pure protocol did not obtain GoPay access_token")

        if refresh_token:
            sc, refreshed, _headers = gp.token(refresh_token=str(refresh_token), account_id="")
            if _pure_success(sc, refreshed, allow=(200, 201, 202)):
                access_token = pick_first(refreshed, ["access_token", "accessToken"]) or access_token
                refresh_token = pick_first(refreshed, ["refresh_token", "refreshToken"]) or refresh_token
                log("[gopay] pure protocol refreshed signup token for customer APIs")

        sc, data, _headers = gp.pin_allowed(str(access_token), pin)
        _pure_require_success("pin_allowed", sc, data)
        sc, data, _headers = gp.cvs_methods_pin(str(access_token))
        _pure_require_success("pin_cvs_methods", sc, data)
        pin_verification_id, pin_method = _pure_pick_method(data, pick_first)
        if not pin_verification_id:
            raise GoPayError(f"pin_cvs_methods missing verification_id: {_pure_jdump(data)[:500]}")
        _smsbower_set_status(activation, "3", log=log)
        sc, data, _headers = gp.cvs_initiate_pin(str(access_token), pin_verification_id, method=pin_method)
        _pure_require_success("pin_cvs_initiate", sc, data, allow=(200, 201, 202, 204))
        pin_otp_token = str(pick_first(data, ["otp_token", "otpToken"]) or "")
        pin_otp = _wait_smsbower_otp_with_retry(
            activation,
            first_timeout=60,
            retry_timeout=180,
            retry_callback=lambda: _pure_step(*gp.cvs_retry_pin(str(access_token), pin_otp_token, method=pin_method)[:2]),
            retry_flow="goto_pin_wa_sms",
            log=log,
        )
        sc, data, _headers = gp.cvs_verify_pin(str(access_token), pin_verification_id, pin_otp, pin_otp_token, method=pin_method)
        _pure_require_success("pin_cvs_verify", sc, data)
        pin_verification_token = pick_first(data, ["verification_token", "verificationToken"])
        if not pin_verification_token:
            raise GoPayError(f"pin_cvs_verify missing verification_token: {_pure_jdump(data)[:500]}")
        sc, data, _headers = gp.pin_setup_token_after_otp(str(access_token), pin, str(pin_verification_token))
        _pure_require_success("pin_setup_token_after_otp", sc, data)
        sc, profile, _headers = gp.user_profile(str(access_token))
        _pure_require_success("profile_after_pin_setup", sc, profile)
        pin_setup = pick_first(profile, ["is_pin_setup", "isPinSetup"])
        if pin_setup is False:
            raise GoPayError(f"profile_after_pin_setup says PIN is not set: {_pure_jdump(profile)[:500]}")

        balance_rp, access_token, refresh_token = _pure_balance_rp(gp, str(access_token), str(refresh_token or ""), log=log)
        if balance_rp >= 0:
            log(f"[gopay] GoPay balance={balance_rp} Rp")
        funded_via = "none"
        if min_balance_rp > 0:
            balance_rp, access_token, refresh_token, funded_via = _pure_fund_account_before_payment(
                gopay_cfg,
                smsbower_cfg,
                gp,
                str(access_token),
                str(refresh_token or ""),
                phone=phone,
                current_balance_rp=balance_rp,
                min_balance_rp=min_balance_rp,
                poll_interval_seconds=balance_poll_interval,
                log=log,
            )
    except PureGoPayPhoneAlreadyRegistered:
        raise
    except Exception:
        raise
    finally:
        gp.close()

    activation["balance_rp"] = balance_rp
    activation["funded_via"] = funded_via
    if min_balance_rp > 0 and balance_rp < min_balance_rp:
        if balance_rp < min_balance_rp:
            if balance_rp < 0:
                raise GoPayError(f"GoPay pure protocol balance check failed before payment after funding precheck")
            raise GoPayError(
                f"GoPay balance insufficient before payment after funding precheck: "
                f"balance={balance_rp} Rp required>={min_balance_rp} Rp funded_via={funded_via}"
            )

    activation["gojek_registered"] = True
    activation["request_retry_before_wait"] = True
    log(f"[gopay] GoPay pure protocol bootstrap ok phone={phone}")


def _bootstrap_gojek_account_via_app_service(
    gopay_cfg: dict,
    activation: dict,
    *,
    log: Callable[[str], None] = print,
) -> None:
    pin = str(gopay_cfg.get("pin") or "147258").strip()
    if not pin:
        raise GoPayError("gopay.pin is required for SMSBower GoPay account registration")
    phone = str(activation.get("phone") or "").strip()
    country_code = str(gopay_cfg.get("country_code") or activation.get("country_code") or "62").strip().lstrip("+")
    if not phone:
        raise GoPayError("SMSBower activation phone missing before GoPay signup")

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
    balance_wait_timeout, balance_poll_interval = _balance_wait_cfg(gopay_cfg, smsbower_cfg)
    state = str(gopay_cfg.get("gopay_app_state_json") or "")
    name = random.choice([
        "Budi Santoso", "Adi Pratama", "Siti Rahayu", "Dewi Lestari",
        "Rizky Ramadhan", "Putri Wulandari", "Agus Setiawan", "Rina Kusuma",
        "Hendra Wijaya", "Novi Anggraini", "Dian Permata", "Wahyu Hidayat",
    ])

    log(f"[gopay] GoPay app signup start phone={phone}")
    signup = _call_gopay_app(
        "SignupStart",
        {
            "phone": phone,
            "name": name,
            "email": "",
            "country_code": country_code,
            "otp_channel": "sms",
            "skip_phone_probe": _truthy(smsbower_cfg.get("skip_phone_probe", gopay_cfg.get("skip_phone_probe", True))),
            "state_json": state,
        },
        gopay_cfg,
    )
    if not _rpc_success(signup):
        raise GoPayError(f"GoPay app SignupStart failed: {_rpc_error(signup)}")
    state = _rpc_state(signup, state)
    activation["gojek_phone"] = phone
    activation["gopay_app_state_json"] = state

    if _rpc_bool(signup, "otpSent", "otp_sent"):
        signup_otp = _wait_smsbower_otp_with_retry(
            activation,
            first_timeout=60,
            retry_timeout=180,
            retry_callback=lambda: _call_gopay_app("SignupRetry", {"state_json": state}, gopay_cfg),
            retry_flow="signup",
            log=log,
        )
        completed = _call_gopay_app("SignupComplete", {"otp": signup_otp, "state_json": state}, gopay_cfg)
        if not _rpc_success(completed):
            raise GoPayError(f"GoPay app SignupComplete failed: {_rpc_error(completed)}")
        state = _rpc_state(completed, state)
        activation["gopay_app_state_json"] = state
    else:
        raise GoPayError(f"GoPay app SignupStart did not send OTP: {signup}")

    pin_start = _call_gopay_app(
        "CreatePinStart",
        {"pin": pin, "otp_channel": "sms", "state_json": state},
        gopay_cfg,
    )
    if not _rpc_success(pin_start):
        raise GoPayError(f"GoPay app CreatePinStart failed: {_rpc_error(pin_start)}")
    state = _rpc_state(pin_start, state)
    activation["gopay_app_state_json"] = state

    if _rpc_bool(pin_start, "pinSetupComplete", "pin_setup_complete"):
        log("[gopay] GoPay PIN already set")
    elif _rpc_bool(pin_start, "otpSent", "otp_sent"):
        _smsbower_set_status(activation, "3", log=log)
        pin_otp = _wait_smsbower_otp_with_retry(
            activation,
            first_timeout=60,
            retry_timeout=180,
            retry_callback=lambda: _call_gopay_app("CreatePinRetry", {"state_json": state}, gopay_cfg),
            retry_flow="goto_pin_wa_sms",
            log=log,
        )
        pin_completed = _call_gopay_app(
            "CreatePinComplete",
            {"otp": pin_otp, "pin": pin, "state_json": state},
            gopay_cfg,
        )
        if not _rpc_success(pin_completed):
            raise GoPayError(f"GoPay app CreatePinComplete failed: {_rpc_error(pin_completed)}")
        state = _rpc_state(pin_completed, state)
        activation["gopay_app_state_json"] = state
    else:
        raise GoPayError(f"GoPay app CreatePinStart did not send OTP or complete PIN setup: {pin_start}")

    balance_rp = _check_gopay_app_balance_rp(gopay_cfg, state, log=log)
    activation["balance_rp"] = balance_rp
    if balance_rp >= 0:
        log(f"[gopay] GoPay balance={balance_rp} Rp")
    if min_balance_rp > 0 and balance_rp < min_balance_rp:
        deadline = time.time() + max(0, int(balance_wait_timeout))
        while time.time() < deadline:
            remaining = deadline - time.time()
            if balance_rp >= 0:
                log(f"[gopay] GoPay balance not ready: {balance_rp} Rp < {min_balance_rp} Rp")
            else:
                log("[gopay] GoPay balance check not ready")
            time.sleep(min(float(balance_poll_interval), max(0.1, remaining)))
            balance_rp = _check_gopay_app_balance_rp(gopay_cfg, state, log=log)
            activation["balance_rp"] = balance_rp
            if balance_rp >= min_balance_rp:
                log(f"[gopay] GoPay balance ready={balance_rp} Rp")
                break
        if balance_rp < min_balance_rp:
            if balance_rp < 0:
                raise GoPayError(f"GoPay app balance check failed before payment after waiting {balance_wait_timeout}s")
            raise GoPayError(
                f"GoPay balance insufficient before payment after waiting {balance_wait_timeout}s: "
                f"balance={balance_rp} Rp required>={min_balance_rp} Rp"
            )

    activation["gojek_registered"] = True
    activation["request_retry_before_wait"] = True
    log(f"[gopay] GoPay app signup ok phone={phone}")


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
        raise GoPayError(
            "SMSBower GoPay registration requires either gopay.gopay_app_service_addr "
            "pointing to a compatible GopayAppService, or a legacy gopay_deploy_src "
            f"containing opai/core/gojek_client.py; attempted legacy path {path}: {exc}"
        ) from exc
    return GojekClient


def _gopay_app_cfg(gopay_cfg: dict) -> dict[str, Any]:
    wa_cfg = gopay_cfg.get("wa_rebind") if isinstance(gopay_cfg.get("wa_rebind"), dict) else {}
    return {
        "addr": str(gopay_cfg.get("gopay_app_service_addr") or wa_cfg.get("gopay_app_service_addr") or "").strip(),
        "service": str(gopay_cfg.get("gopay_app_service") or wa_cfg.get("gopay_app_service") or "gopay_app.GopayAppService").strip(),
        "grpcurl": str(gopay_cfg.get("grpcurl_path") or gopay_cfg.get("grpcurl") or "grpcurl").strip() or "grpcurl",
        "proto_path": str(gopay_cfg.get("gopay_app_proto_path") or wa_cfg.get("gopay_app_proto_path") or "services\\gopay-app\\proto\\gopay_app.proto").strip(),
        "proto_import_path": str(gopay_cfg.get("gopay_app_proto_import_path") or wa_cfg.get("gopay_app_proto_import_path") or "services\\gopay-app\\proto").strip(),
        "timeout_seconds": int(float(gopay_cfg.get("gopay_app_timeout_seconds") or wa_cfg.get("timeout_seconds") or gopay_cfg.get("provider_timeout_seconds") or 600)),
    }


def _gopay_app_service_configured(gopay_cfg: dict) -> bool:
    return bool(_gopay_app_cfg(gopay_cfg)["addr"])


def _resolve_repo_path(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return str(path)


def _call_gopay_app(method: str, body: dict[str, Any], gopay_cfg: dict) -> dict[str, Any]:
    cfg = _gopay_app_cfg(gopay_cfg)
    addr = cfg["addr"]
    if not addr:
        return {"success": False, "errorMessage": "gopay_app_service_addr is required for SMSBower GoPay registration"}
    command = [
        cfg["grpcurl"],
        "-plaintext",
        "-max-time",
        str(cfg["timeout_seconds"]),
    ]
    proto_path = _resolve_repo_path(cfg["proto_path"])
    if proto_path:
        proto_import = _resolve_repo_path(cfg["proto_import_path"]) or str(Path(proto_path).parent)
        command.extend(["-import-path", proto_import, "-proto", str(Path(proto_path).name)])
    command.extend([
        "-d",
        json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")),
        addr,
        f"{cfg['service']}/{method}",
    ])
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=cfg["timeout_seconds"] + 5,
        )
    except FileNotFoundError:
        return {"success": False, "errorMessage": f"grpcurl not found: {cfg['grpcurl']}"}
    except Exception as exc:
        return {"success": False, "errorMessage": str(exc)}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"success": False, "errorMessage": stderr or stdout or f"grpcurl exited {proc.returncode}"}
    if not stdout:
        return {"success": True}
    try:
        parsed = json.loads(stdout)
    except Exception:
        return {"success": False, "errorMessage": stdout[:500]}
    return parsed if isinstance(parsed, dict) else {"success": True, "data": parsed}


def _rpc_success(result: dict[str, Any]) -> bool:
    return _rpc_bool(result, "success")


def _rpc_bool(result: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = result.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def _rpc_error(result: dict[str, Any]) -> str:
    return str(result.get("errorMessage") or result.get("error_message") or result.get("error") or result)[:500]


def _rpc_state(result: dict[str, Any], fallback: str = "") -> str:
    return str(result.get("stateJson") or result.get("state_json") or fallback or "")


def _check_gopay_app_balance_rp(gopay_cfg: dict, state_json: str, *, log: Callable[[str], None] = print) -> int:
    result = _call_gopay_app("CheckTokenValid", {"state_json": state_json}, gopay_cfg)
    if not _rpc_success(result):
        log(f"[gopay] GoPay app balance check failed: {_rpc_error(result)}")
        return -1
    amount = result.get("balanceAmount")
    if amount is None:
        amount = result.get("balance_amount")
    try:
        return int(amount)
    except Exception:
        return -1


def _tls_client_proxy(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if proxy.lower().startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://"):]
    return proxy


def _smsbower_api(api_key: str, endpoint: str, action: str, params: dict[str, Any] | None = None) -> str:
    query = {"api_key": api_key, "action": action}
    if params:
        query.update(params)
    last_error: Exception | None = None
    transient_errors = (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.SSLError,
    )
    for attempt in range(1, SMSBOWER_API_ATTEMPTS + 1):
        try:
            response = requests.get(endpoint, params=query, timeout=SMSBOWER_API_TIMEOUT)
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and status >= 500 and attempt < SMSBOWER_API_ATTEMPTS:
                last_error = requests.exceptions.HTTPError(f"smsbower HTTP {status}")
                time.sleep(SMSBOWER_API_RETRY_SLEEP_S * attempt)
                continue
            response.raise_for_status()
            return response.text.strip()
        except transient_errors as exc:
            last_error = exc
            if attempt >= SMSBOWER_API_ATTEMPTS:
                raise
            time.sleep(SMSBOWER_API_RETRY_SLEEP_S * attempt)
    raise GoPayError(f"smsbower API failed after {SMSBOWER_API_ATTEMPTS} attempt(s): {last_error}")


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
