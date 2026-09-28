from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_TIMEOUT, STRIPE_VERSION
except ImportError:
    from pp_link_helpers import CHATGPT_TIMEOUT, DEFAULT_TIMEOUT, STRIPE_VERSION  # type: ignore
from typing import Any
from collections.abc import Mapping
import time
from urllib.parse import urlencode
from ._extract import (
    _upi_copyable_link,
    _upi_extract_next_action,
    _upi_extract_qr_candidates,
    _upi_extract_redirect_url,
    _upi_get_payment_method_types,
    _upi_int_value,
    _upi_is_instructions_url,
    _upi_qr_image_kind,
)
from .constants import UPI_CONFIRMATION_TOKENS_URL, UPI_CPMT_CONFIRM_URL, UPI_CPMT_START_URL, UPI_PAYMENT_INTENT_CONFIRM_URL_T, UPI_PAYMENT_INTENT_GET_URL_T, UPI_QR_POLL_INTERVAL, UPI_QR_POLL_MAX_ATTEMPTS
from .env import _emit
from .dump import _upi_dump_http
from .config import _upi_first_string, _upi_payment_intent_id
from .session import _normalize_hosted_checkout_url, _upi_classify_failure, _write_qr_png
from .stripe import _upi_build_confirmation_token_body
from .sentinel import _upi_sentinel_headers
from .extract import _upi_hydrate_qr_data


def _upi_run_cpmt_flow(
    cs: Any,
    *,
    access_token: Any,
    cs_id: Any,
    cpm_id: Any,
    processor_entity: Any,
    amount: Any,
    payment_currency: Any,
    target_country: Any,
    checkout_country: Any,
    payment_country: Any,
    device_id: Any,
    checkout_proxy: Any,
    qr_path: Any = "",
) -> dict[str, Any]:
    """Run the server-advertised UPI custom payment method (cpmt) rail.

    Aligned with the reference ``_run_upi_cpmt_flow``: when Checkout exposes a
    UPI ``cpmt_*`` method, confirm it with ChatGPT and start the custom method.
    This path never creates a Stripe SetupIntent, so it sidesteps the
    ``setup_attempt_failed / generic_decline`` stage that the Stripe rail can
    hit. Returns the same result contract as the Stripe path.
    """
    referer = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
    risk_headers = _upi_sentinel_headers(cs, device_id, checkout_proxy)
    base_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": referer,
        **risk_headers,
    }

    confirm_body = {"checkout_session_id": cs_id, "selected_payment_method_type": cpm_id}
    confirm_resp = cs.post(
        UPI_CPMT_CONFIRM_URL,
        json=confirm_body,
        headers={
            **base_headers,
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
        },
        timeout=CHATGPT_TIMEOUT,
    )
    _upi_dump_http(
        confirm_resp,
        "chatgpt_cpmt_confirm",
        confirm_body,
        "POST",
        UPI_CPMT_CONFIRM_URL,
        force=confirm_resp.status_code >= 400,
    )
    if confirm_resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"cpmt confirm failed: {confirm_resp.status_code} {str(getattr(confirm_resp, 'text', ''))[:240]}",
            "error_code": _upi_classify_failure(getattr(confirm_resp, "text", "")),
            "payment_method": "upi",
            "cs_id": cs_id,
            "cpmt_id": cpm_id,
        }
    confirm_json = confirm_resp.json() or {}
    confirm_status = str(confirm_json.get("status") or "").strip().lower()
    if confirm_status == "blocked":
        return {
            "ok": False,
            "error": "cpmt confirm blocked (provider risk control)",
            "error_code": "upi_provider_declined",
            "payment_method": "upi",
            "cs_id": cs_id,
            "cpmt_id": cpm_id,
        }
    if confirm_status not in {"success", "ok", ""}:
        return {
            "ok": False,
            "error": f"cpmt confirm status={confirm_status or 'unknown'}",
            "error_code": "upi_cpmt_confirm_failed",
            "payment_method": "upi",
            "cs_id": cs_id,
            "cpmt_id": cpm_id,
        }
    _emit("cpmt", f"ChatGPT confirmed UPI custom method {cpm_id}")

    start_body = {"checkout_session_id": cs_id, "custom_payment_method_type_id": cpm_id}
    start_resp = cs.post(
        UPI_CPMT_START_URL,
        json=start_body,
        headers={
            **base_headers,
            "x-openai-target-path": "/backend-api/payments/checkout/custom_payment_method/start",
            "x-openai-target-route": "/backend-api/payments/checkout/custom_payment_method/start",
        },
        timeout=CHATGPT_TIMEOUT,
    )
    _upi_dump_http(
        start_resp, "chatgpt_cpmt_start", start_body, "POST", UPI_CPMT_START_URL, force=start_resp.status_code >= 400
    )
    if start_resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"cpmt start failed: {start_resp.status_code} {str(getattr(start_resp, 'text', ''))[:240]}",
            "error_code": _upi_classify_failure(getattr(start_resp, "text", "")),
            "payment_method": "upi",
            "cs_id": cs_id,
            "cpmt_id": cpm_id,
        }
    start_json = start_resp.json() or {}
    start_status = str(start_json.get("status") or "").strip().lower()
    if start_status == "blocked":
        return {
            "ok": False,
            "error": "cpmt start blocked (provider risk control)",
            "error_code": "upi_provider_declined",
            "payment_method": "upi",
            "cs_id": cs_id,
            "cpmt_id": cpm_id,
        }
    target = _upi_copyable_link(start_json)
    if not target:
        return {
            "ok": False,
            "error": f"cpmt start returned no link (status={start_status or 'unknown'})",
            "error_code": "upi_cpmt_no_link",
            "payment_method": "upi",
            "cs_id": cs_id,
            "cpmt_id": cpm_id,
        }
    written_qr_path = _write_qr_png(target, qr_path or "")
    return {
        "ok": True,
        "payment_method": "upi",
        "method": "upi",
        "link_type": "upi_cpmt_link",
        "url": target,
        "upi_uri": target if target.startswith("upi://") else "",
        "hosted_url": target,
        "instructions_url": target if _upi_is_instructions_url(target) else "",
        "qr_data": target,
        "qr_path": written_qr_path,
        "expires_at": (_upi_int_value(time.time())[0] or 0) + 300,
        "cs_id": cs_id,
        "processor_entity": processor_entity,
        "amount": amount,
        "currency": payment_currency.upper(),
        "target_country": target_country,
        "checkout_country": checkout_country,
        "billing_country": checkout_country,
        "payment_country": payment_country,
        "payment_method_types": ["upi"],
        "cpmt_id": cpm_id,
        "approval_ok": True,
        "approval_blocked": False,
        "checkout_proxy": checkout_proxy,
    }
def _upi_run_oaics_flow(
    stripe: Any,
    cs: Any,
    *,
    access_token: Any,
    device_id: Any,
    cs_id: Any,
    stripe_pk: Any,
    processor_entity: Any,
    billing: Mapping[str, str],
    fingerprint: Mapping[str, str],
    ctx: Mapping[str, Any],
    elements: Mapping[str, Any],
    oaics_state: Any,
    amount: Any,
    payment_currency: Any,
    target_country: Any,
    checkout_country: Any,
    payment_country: Any,
    checkout_proxy: Any,
    provider_proxy: Any,
    approve_proxy: Any,
    qr_path: Any,
    wait_paid: bool,
    paid_timeout: float,
) -> dict[str, Any]:
    """OAICS/deferred rail: confirmation_tokens -> ChatGPT confirm -> PaymentIntent.

    Port of the reference ``_run_upi_india_phase`` OAICS branch. Unlike the
    custom rail this never posts to ``payment_pages/{cs_id}/confirm``; the
    mandate-carrying form goes to ``/v1/confirmation_tokens`` and the checkout
    is confirmed through ChatGPT with the resulting ``ctoken_``.

    Reachability (measured 2026-09-26): the ChatGPT checkout API rejects
    ``checkout_ui_mode='deferred'`` with 422 (allowed = hosted/redirect/custom/
    embedded) and answers ``cs_live_`` for every accepted mode, so this branch
    only runs when the backend spontaneously returns an ``oaics_`` session.
    The Stripe half was verified live against ``cs_live_``: Stripe **accepts**
    the mandate form and mints a ``ctoken_``; ChatGPT refuses to confirm a
    non-``oaics_`` session (400 "only for internal checkout sessions").
    """
    referer = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
    methods = (
        _upi_get_payment_method_types(elements) or _upi_get_payment_method_types(oaics_state) or ["card", "link", "upi"]
    )
    token_body = _upi_build_confirmation_token_body(
        cs_id=str(cs_id),
        stripe_pk=str(stripe_pk),
        billing=billing,
        ctx=ctx,
        elements=elements,
        payment_method_types=methods,
    )
    token_resp = stripe.post(UPI_CONFIRMATION_TOKENS_URL, data=token_body, timeout=DEFAULT_TIMEOUT)
    _upi_dump_http(
        token_resp,
        "stripe_confirmation_token",
        token_body,
        "POST",
        UPI_CONFIRMATION_TOKENS_URL,
        force=token_resp.status_code >= 400,
    )
    if token_resp.status_code >= 400:
        raise RuntimeError(
            f"confirmation_tokens failed: {token_resp.status_code} {str(getattr(token_resp, 'text', ''))[:240]}"
        )
    token_json = token_resp.json() or {}
    confirm_token = _upi_first_string(token_json, ["id"])
    if not confirm_token.startswith("ctoken_"):
        raise RuntimeError(f"confirmation_tokens did not return ctoken: keys={list(token_json)[:12]}")
    _emit("oaics", f"confirmation token ready ({confirm_token[:16]}...)")

    chat_body = {
        "checkout_session_id": cs_id,
        "confirm_token": confirm_token,
        "selected_payment_method_type": "upi",
    }
    risk_headers = _upi_sentinel_headers(cs, device_id, provider_proxy or checkout_proxy)
    chat_resp = cs.post(
        UPI_CPMT_CONFIRM_URL,
        json=chat_body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": referer,
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
            **risk_headers,
        },
        timeout=CHATGPT_TIMEOUT,
    )
    _upi_dump_http(
        chat_resp,
        "chatgpt_oaics_confirm",
        chat_body,
        "POST",
        UPI_CPMT_CONFIRM_URL,
        force=chat_resp.status_code >= 400,
    )
    if chat_resp.status_code >= 400:
        raise RuntimeError(
            f"chatgpt confirm failed: {chat_resp.status_code} {str(getattr(chat_resp, 'text', ''))[:240]}"
        )
    chat_json = chat_resp.json() or {}
    chat_status = str(chat_json.get("status") or "").strip().lower()
    if chat_status == "blocked":
        raise RuntimeError("chatgpt confirm blocked (provider risk control)")
    if chat_status and chat_status not in {"success", "ok"}:
        raise RuntimeError(f"chatgpt confirm status={chat_status or 'unknown'}")

    client_secret = _upi_first_string(chat_json, ["client_secret"])
    payment_intent_id = _upi_payment_intent_id(_upi_first_string(chat_json, ["payment_intent", "client_secret"]))
    if not client_secret and isinstance(chat_json.get("payment_intent"), Mapping):
        client_secret = _upi_first_string(chat_json["payment_intent"], ["client_secret"])
        payment_intent_id = _upi_payment_intent_id(client_secret)
    if not payment_intent_id:
        payment_intent_id = _upi_payment_intent_id(chat_json.get("id"))
    if not client_secret or not payment_intent_id:
        raise RuntimeError(f"oaics confirm missing PaymentIntent: keys={list(chat_json)[:20]}")
    _emit("oaics", f"payment intent ready ({payment_intent_id})")

    return_url = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
    pi_body = {
        "return_url": return_url,
        "confirmation_token": confirm_token,
        "key": str(stripe_pk),
        "_stripe_version": STRIPE_VERSION,
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "l1",
        "client_secret": client_secret,
    }
    pi_resp = stripe.post(
        UPI_PAYMENT_INTENT_CONFIRM_URL_T.format(pi_id=payment_intent_id), data=pi_body, timeout=DEFAULT_TIMEOUT
    )
    _upi_dump_http(
        pi_resp,
        "stripe_payment_intent_confirm",
        pi_body,
        "POST",
        UPI_PAYMENT_INTENT_CONFIRM_URL_T.format(pi_id=payment_intent_id),
        force=pi_resp.status_code >= 400,
    )
    if pi_resp.status_code >= 400:
        raise RuntimeError(
            f"payment intent confirm failed: {pi_resp.status_code} {str(getattr(pi_resp, 'text', ''))[:240]}"
        )
    pi_json = pi_resp.json() or {}
    pi_status = str(pi_json.get("status") or "").strip().lower()

    qr_data: dict[str, Any] = {}

    def _absorb(source: Any) -> str:
        redirect = _upi_extract_redirect_url(source)
        for url in _upi_extract_qr_candidates(source):
            kind = _upi_qr_image_kind(url)
            if kind == "svg":
                qr_data.setdefault("qr_image_url_svg", url)
            elif kind in ("png", "jpg"):
                qr_data.setdefault("qr_image_url_png", url)
        for k, v in _upi_extract_next_action(source).items():
            if v and (k == "upi_uri" or not qr_data.get(k)):
                qr_data[k] = v
        return redirect

    def _poll_payment_intent() -> dict[str, Any]:
        poll_url = (
            UPI_PAYMENT_INTENT_GET_URL_T.format(pi_id=payment_intent_id)
            + "?"
            + urlencode(
                {
                    "is_stripe_sdk": "false",
                    "client_secret": client_secret,
                    "key": str(stripe_pk),
                    "_stripe_version": STRIPE_VERSION,
                }
            )
        )
        poll_resp = stripe.get(poll_url, timeout=DEFAULT_TIMEOUT)
        if poll_resp.status_code >= 400:
            return {}
        return poll_resp.json() or {}

    redirect_url = _absorb(pi_json)
    if not redirect_url and not qr_data.get("upi_uri"):
        for _attempt in range(1, max(1, UPI_QR_POLL_MAX_ATTEMPTS) + 1):
            if redirect_url or qr_data.get("upi_uri"):
                break
            time.sleep(UPI_QR_POLL_INTERVAL)
            try:
                poll_json = _poll_payment_intent()
            except Exception as exc:
                _emit("oaics", f"payment intent poll error (non-fatal): {type(exc).__name__}: {str(exc)[:100]}")
                continue
            redirect_url = _absorb(poll_json) or redirect_url
            pi_status = str(poll_json.get("status") or pi_status).strip().lower()
            if poll_json:
                pi_json = poll_json
            if pi_status in {"succeeded", "canceled", "requires_payment_method"}:
                break

    if redirect_url and _upi_is_instructions_url(redirect_url):
        qr_data.setdefault("hosted_instructions_url", redirect_url)
    qr_data = _upi_hydrate_qr_data(qr_data, provider_proxy, fingerprint)

    upi_uri = str(qr_data.get("upi_uri") or "")
    if not upi_uri.startswith("upi://"):
        mobile_auth = str(qr_data.get("mobile_auth_url") or "")
        upi_uri = mobile_auth if mobile_auth.startswith("upi://") else ""
    hosted_url = (
        str(pi_json.get("hosted_instructions_url") or "")
        or _normalize_hosted_checkout_url(redirect_url)
        or redirect_url
        or f"https://pay.openai.com/c/pay/{cs_id}"
    )
    if upi_uri:
        target = upi_uri
        link_type = "upi_deep_link"
    elif redirect_url and _upi_is_instructions_url(redirect_url):
        target = redirect_url
        link_type = "upi_instructions_url"
    else:
        target = redirect_url or hosted_url
        link_type = "upi_hosted_fallback"
    written_qr_path = _write_qr_png(target, qr_path or "")

    paid_state = {"paid": pi_status == "succeeded", "payment_status": pi_status or "unknown"}
    if wait_paid and not paid_state["paid"]:
        _emit("paid", f"waiting up to {paid_timeout:g}s for payment on {payment_intent_id}...")
        deadline = time.time() + max(0, _upi_int_value(paid_timeout)[0] or 900)
        while time.time() < deadline and not paid_state["paid"]:
            time.sleep(UPI_QR_POLL_INTERVAL)
            try:
                poll_json = _poll_payment_intent()
            except Exception:
                continue
            if poll_json:
                pi_status = str(poll_json.get("status") or pi_status).strip().lower()
                paid_state = {"paid": pi_status == "succeeded", "payment_status": pi_status}
        _emit("paid", f"payment_status={paid_state.get('payment_status')} paid={paid_state.get('paid')}")

    return {
        "ok": True,
        "payment_method": "upi",
        "method": "upi",
        "link_type": link_type,
        "url": upi_uri or redirect_url or hosted_url,
        "upi_uri": upi_uri,
        "hosted_url": hosted_url,
        "instructions_url": redirect_url if _upi_is_instructions_url(redirect_url) else "",
        "qr_data": target,
        "qr_path": written_qr_path,
        "qr_image_url_png": qr_data.get("qr_image_url_png", ""),
        "qr_image_url_svg": qr_data.get("qr_image_url_svg", ""),
        "expires_at": qr_data.get("expires_at") or (_upi_int_value(time.time())[0] or 0) + 300,
        "cs_id": cs_id,
        "processor_entity": processor_entity,
        "amount": amount,
        "currency": str(payment_currency).upper(),
        "target_country": target_country,
        "checkout_country": checkout_country,
        "billing_country": checkout_country,
        "payment_country": payment_country,
        "payment_method_types": methods,
        "confirmation_token_id": confirm_token,
        "payment_intent_id": payment_intent_id,
        "approval_ok": True,
        "approval_blocked": False,
        "paid": bool(paid_state.get("paid")),
        "payment_status": str(paid_state.get("payment_status") or ""),
        "checkout_ui_mode": "deferred",
        "checkout_proxy": checkout_proxy,
        "provider_proxy": provider_proxy,
        "approve_proxy": approve_proxy,
    }
