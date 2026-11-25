"""Native direct extractor for GPT Team 5-seat trial hosted checkout links.

Unlike regional wallet / PayPal BA methods that require external Stripe PM
extraction, ChatGPT Team 5-seat trial (``team-1-month-free``) issues a hosted
Stripe Checkout URL directly from ``POST /backend-api/payments/checkout`` with
the account's Access Token and matching egress country.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable, Mapping
from urllib.parse import quote

from ..payment.checkout_contract import (
    CHECKOUT_PATH,
    CHECKOUT_URL,
    TEAM_TRIAL_CAMPAIGN_ID,
)
from ..payment.wire import CURRENCY_MAP, _checkout_get, _checkout_post
from .base import _redact_sensitive_text, safe_int

TEAM_TRIAL_PLAN_NAME = "chatgptteamplan"
TEAM_TRIAL_ENTRY_POINT = "team_workspace_purchase_modal"
TEAM_TRIAL_DEFAULT_WORKSPACE_NAME = "MyTeam"
TEAM_TRIAL_DEFAULT_SEATS = 5
TEAM_TRIAL_MIN_SEATS = 2
TEAM_TRIAL_MAX_SEATS = 5
_CHATGPT_BASE = CHECKOUT_URL.removesuffix(CHECKOUT_PATH)
TEAM_TRIAL_CANCEL_URL = f"{_CHATGPT_BASE}/#pricing"
TEAM_TRIAL_CHECKOUT_UI_MODE = "hosted"

_ME_URL = f"{_CHATGPT_BASE}/backend-api/me"
_ACCOUNTS_CHECK_URL = f"{_CHATGPT_BASE}/backend-api/accounts/check/v4-2023-04-27"
_COUPON_CHECK_URL = (
    f"{_CHATGPT_BASE}/backend-api/promo_campaign/check_coupon"
    f"?coupon={quote(TEAM_TRIAL_CAMPAIGN_ID, safe='')}&is_coupon_from_query_param=true"
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
_CS_ID_RE = re.compile(r"\b(cs_(?:live|test)_[A-Za-z0-9_]+|oaics_[A-Za-z0-9_]+)\b")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_URL_KEYS: tuple[str, ...] = ("url", "stripe_hosted_url", "checkout_url", "hosted_url")


def normalize_team_seat_quantity(value: Any) -> int:
    """Clamp seat quantity to the ``team-1-month-free`` valid window (2..5)."""
    seats = safe_int(value, TEAM_TRIAL_DEFAULT_SEATS)
    if seats <= 0:
        return TEAM_TRIAL_DEFAULT_SEATS
    return max(TEAM_TRIAL_MIN_SEATS, min(seats, TEAM_TRIAL_MAX_SEATS))


def normalize_team_workspace_name(value: Any) -> str:
    """Return a clean workspace name, falling back to ``MyTeam``."""
    name = str(value or "").strip()
    return name[:64] if name else TEAM_TRIAL_DEFAULT_WORKSPACE_NAME


def currency_for_country(country: Any) -> str:
    """Resolve the billing currency for a 2-letter ISO country code."""
    code = str(country or "").strip().upper()
    return str(CURRENCY_MAP.get(code) or "USD")


def build_team_trial_checkout_payload(
    *,
    country: str,
    currency: str = "",
    workspace_name: str = TEAM_TRIAL_DEFAULT_WORKSPACE_NAME,
    seat_quantity: int = TEAM_TRIAL_DEFAULT_SEATS,
    checkout_ui_mode: str = TEAM_TRIAL_CHECKOUT_UI_MODE,
) -> dict[str, Any]:
    """Build the ``POST /backend-api/payments/checkout`` payload for Team trial."""
    billing_country = str(country or "US").strip().upper()
    if not _COUNTRY_RE.fullmatch(billing_country):
        billing_country = "US"
    billing_currency = str(currency or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(billing_currency):
        billing_currency = currency_for_country(billing_country)
    ui_mode = str(checkout_ui_mode or TEAM_TRIAL_CHECKOUT_UI_MODE).strip().lower()
    if ui_mode not in {"hosted", "redirect", "custom"}:
        ui_mode = TEAM_TRIAL_CHECKOUT_UI_MODE
    return {
        "entry_point": TEAM_TRIAL_ENTRY_POINT,
        "plan_name": TEAM_TRIAL_PLAN_NAME,
        "team_plan_data": {
            "workspace_name": normalize_team_workspace_name(workspace_name),
            "price_interval": "month",
            "seat_quantity": normalize_team_seat_quantity(seat_quantity),
        },
        "billing_details": {
            "country": billing_country,
            "currency": billing_currency,
        },
        "cancel_url": TEAM_TRIAL_CANCEL_URL,
        "checkout_ui_mode": ui_mode,
        "promo_campaign": {
            "promo_campaign_id": TEAM_TRIAL_CAMPAIGN_ID,
            "is_coupon_from_query_param": True,
        },
    }


def _jwt_chatgpt_account_id(access_token: str) -> str:
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    segment = parts[1]
    padded = segment + "=" * (-len(segment) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        aid = str(auth_claim.get("chatgpt_account_id") or "").strip()
        if aid and aid != "default":
            return aid
    return ""


def _personal_account_id_from_accounts_body(body: Any) -> str:
    if not isinstance(body, Mapping):
        return ""
    accounts = body.get("accounts")
    if not isinstance(accounts, Mapping):
        data = body.get("data")
        accounts = data.get("accounts") if isinstance(data, Mapping) else None
    if not isinstance(accounts, Mapping):
        return ""
    unique: dict[str, Mapping[str, Any]] = {}
    has_structure = False
    for key, value in accounts.items():
        if not isinstance(value, Mapping):
            continue
        account = value.get("account") if isinstance(value.get("account"), Mapping) else value
        if not isinstance(account, Mapping):
            continue
        if str(account.get("structure") or "").strip():
            has_structure = True
        raw_key = str(key or "").strip()
        aid = str(
            account.get("account_id") or account.get("id") or (raw_key if _UUID_RE.match(raw_key) else "")
        ).strip()
        if not aid or aid == "default":
            continue
        unique[aid] = account
    for aid, account in unique.items():
        if str(account.get("structure") or "").strip().lower() == "personal":
            return aid
    if has_structure:
        return ""
    return next(iter(unique), "")


def _response_json(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    try:
        parsed = response.json()
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def _response_detail(response: Any, body: Mapping[str, Any]) -> str:
    for key in ("detail", "error", "message"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, Mapping):
            nested = val.get("message") or val.get("detail") or val.get("code")
            if nested:
                return str(nested).strip()
    return str(getattr(response, "text", "") or "").strip()[:300]


def _extract_auth_headers(auth_context: Mapping[str, Any] | None) -> tuple[str, dict[str, str]]:
    ctx = auth_context if isinstance(auth_context, Mapping) else {}
    cookie_header = str(ctx.get("cookie_header") or "").strip()
    headers: dict[str, str] = {}
    device_id = str(ctx.get("device_id") or ctx.get("oai_did") or "").strip()
    if device_id:
        headers["OAI-Device-Id"] = device_id
    return cookie_header, headers


def _resolve_personal_account_id(
    access_token: str,
    *,
    auth_context: Mapping[str, Any] | None,
    cookie_header: str,
    base_headers: Mapping[str, str],
    proxy: str,
    timeout: int,
    get_fn: Callable[..., Any],
) -> str:
    ctx = auth_context if isinstance(auth_context, Mapping) else {}
    for key in ("personal_account_id", "chatgpt_account_id", "account_id"):
        candidate = str(ctx.get(key) or "").strip()
        if candidate and candidate != "default":
            return candidate
    jwt_aid = _jwt_chatgpt_account_id(access_token)
    if jwt_aid:
        return jwt_aid
    try:
        resp = get_fn(_ACCOUNTS_CHECK_URL, access_token, cookie_header, proxy, timeout, dict(base_headers))
    except (OSError, RuntimeError, ValueError):
        return ""
    if int(getattr(resp, "status_code", 0) or 0) != 200:
        return ""
    return _personal_account_id_from_accounts_body(_response_json(resp))


def _detect_me_country(
    access_token: str,
    *,
    cookie_header: str,
    extra_headers: Mapping[str, str],
    proxy: str,
    timeout: int,
    get_fn: Callable[..., Any],
) -> str:
    try:
        resp = get_fn(_ME_URL, access_token, cookie_header, proxy, timeout, dict(extra_headers))
    except (OSError, RuntimeError, ValueError):
        return ""
    if int(getattr(resp, "status_code", 0) or 0) != 200:
        return ""
    body = _response_json(resp)
    country = str(body.get("country") or "").strip().upper()
    return country if _COUNTRY_RE.fullmatch(country) else ""


def _scrub(text: str, *, access_token: str = "") -> str:
    cleaned = str(text or "")
    if access_token and access_token in cleaned:
        cleaned = cleaned.replace(access_token, "[REDACTED]")
    return _redact_sensitive_text(cleaned)


def _classify_http_failure(
    status_code: int,
    detail: str,
    *,
    stage: str,
    access_token: str,
) -> dict[str, Any]:
    safe_detail = _scrub(detail, access_token=access_token)
    low = safe_detail.lower()
    if status_code == 401:
        code, retryable = "token_invalid", False
    elif status_code == 403 and any(marker in low for marker in ("deactivated", "account_deactivated")):
        code, retryable = "account_deactivated", False
    elif status_code == 429:
        code, retryable = "rate_limited", True
    elif status_code == 403 or 500 <= status_code <= 599:
        code, retryable = ("checkout_blocked" if status_code == 403 else "service_unavailable"), True
    elif any(marker in low for marker in ("ineligible", "coupon", "promo", "already redeemed", "not eligible")):
        code, retryable = "team_trial_ineligible", False
        stage = "eligibility"
    else:
        code, retryable = f"team_trial_http_{status_code or 'error'}", False
    message = f"team trial {stage} returned HTTP {status_code}"
    if safe_detail:
        message = f"{message}: {safe_detail[:320]}"
    return {
        "ok": False,
        "status": "failed",
        "payment_method": "team_trial",
        "source": "team_trial_direct",
        "status_code": status_code,
        "error": message,
        "error_code": code,
        "error_stage": stage,
        "retryable": retryable,
    }


def probe_team_trial_eligibility(
    access_token: str,
    *,
    proxy: str = "",
    auth_context: Mapping[str, Any] | None = None,
    timeout: int = 30,
    get_fn: Callable[..., Any] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Run read-only ``check_coupon?coupon=team-1-month-free`` eligibility probe."""
    token = str(access_token or "").strip()
    if not token:
        return {
            "ok": False,
            "status": "failed",
            "operation": "payment_method_capability_probe",
            "payment_method": "team_trial",
            "source": "team_trial_direct",
            "eligible": False,
            "decision": "missing_access_token",
            "error": "missing access token",
            "error_code": "missing_access_token",
            "error_stage": "validation",
            "retryable": False,
        }
    getter = get_fn or _checkout_get
    cookie_header, extra_headers = _extract_auth_headers(auth_context)
    account_id = _resolve_personal_account_id(
        token,
        auth_context=auth_context,
        cookie_header=cookie_header,
        base_headers=extra_headers,
        proxy=proxy,
        timeout=max(5, safe_int(timeout, 30)),
        get_fn=getter,
    )
    probe_headers = dict(extra_headers)
    if account_id:
        probe_headers["Chatgpt-Account-Id"] = account_id
    try:
        response = getter(_COUPON_CHECK_URL, token, cookie_header, proxy, max(5, safe_int(timeout, 30)), probe_headers)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "status": "failed",
            "operation": "payment_method_capability_probe",
            "payment_method": "team_trial",
            "source": "team_trial_direct",
            "eligible": None,
            "decision": "team_trial_probe_transport_error",
            "error": _scrub(f"{type(exc).__name__}: {exc}", access_token=token)[:320],
            "error_code": "connection_error",
            "error_stage": "eligibility",
            "retryable": True,
        }
    status_code = int(getattr(response, "status_code", 0) or 0)
    body = _response_json(response)
    if status_code != 200:
        failure = _classify_http_failure(
            status_code, _response_detail(response, body), stage="eligibility", access_token=token
        )
        failure["operation"] = "payment_method_capability_probe"
        failure["eligible"] = False if status_code == 401 else None
        failure["decision"] = failure["error_code"]
        return failure

    coupon = str(body.get("coupon") or "").strip()
    state = str(body.get("state") or body.get("status") or "").strip().lower()
    raw_redemption = body.get("redemption")
    redemption = raw_redemption if isinstance(raw_redemption, Mapping) else {}
    redeemed = bool(redemption.get("redeemed"))
    ineligibility_reason = str(body.get("ineligibility_reason") or "").strip()
    coupon_matches = (not coupon) or (coupon == TEAM_TRIAL_CAMPAIGN_ID)
    eligible = bool(state == "eligible" and not redeemed and coupon_matches)
    return {
        "ok": True,
        "status": "completed",
        "operation": "payment_method_capability_probe",
        "payment_method": "team_trial",
        "source": "team_trial_direct",
        "eligible": eligible,
        "classification": "eligible" if eligible else "ineligible",
        "decision": "eligible" if eligible else (ineligibility_reason or "team_trial_ineligible"),
        "coupon": coupon or TEAM_TRIAL_CAMPAIGN_ID,
        "coupon_state": state,
        "coupon_redeemed": redeemed,
        "ineligibility_reason": ineligibility_reason,
        "personal_account_id": account_id,
    }


def _extract_hosted_url(body: Mapping[str, Any]) -> str:
    for key in _URL_KEYS:
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for nested_key in ("data", "result"):
        nested = body.get(nested_key)
        if isinstance(nested, Mapping):
            for key in _URL_KEYS:
                val = nested.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def _extract_checkout_session_id(body: Mapping[str, Any], url: str) -> str:
    for key in ("checkout_session_id", "session_id", "cs_id", "id"):
        val = str(body.get(key) or "").strip()
        if val.startswith(("cs_", "oaics_")):
            return val
    match = _CS_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def _resolve_team_billing(
    token: str,
    *,
    cookie_header: str,
    req_headers: Mapping[str, str],
    proxy: str,
    timeout: int,
    checkout_country: str,
    target_country: str,
    currency: str,
    get_fn: Callable[..., Any],
) -> tuple[str, str]:
    detected_country = _detect_me_country(
        token,
        cookie_header=cookie_header,
        extra_headers=req_headers,
        proxy=proxy,
        timeout=timeout,
        get_fn=get_fn,
    )
    explicit_country = str(checkout_country or target_country or "").strip().upper()
    billing_country = detected_country or (explicit_country if _COUNTRY_RE.fullmatch(explicit_country) else "US")
    billing_currency = str(currency or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(billing_currency) or (detected_country and billing_country == detected_country):
        billing_currency = currency_for_country(billing_country)
    return billing_country, billing_currency


def _post_team_checkout(
    token: str,
    *,
    billing_country: str,
    billing_currency: str,
    workspace_name: str,
    seat_quantity: int,
    checkout_ui_mode: str,
    cookie_header: str,
    req_headers: Mapping[str, str],
    proxy: str,
    timeout: int,
    get_fn: Callable[..., Any],
    post_fn: Callable[..., Any],
) -> tuple[dict[str, Any] | None, int, dict[str, Any], str, str, str]:
    checkout_headers = {
        **req_headers,
        "x-openai-target-path": CHECKOUT_PATH,
        "x-openai-target-route": CHECKOUT_PATH,
    }
    payload = build_team_trial_checkout_payload(
        country=billing_country,
        currency=billing_currency,
        workspace_name=workspace_name,
        seat_quantity=seat_quantity,
        checkout_ui_mode=checkout_ui_mode,
    )
    try:
        response = post_fn(CHECKOUT_URL, payload, token, cookie_header, proxy, timeout, checkout_headers)
    except (OSError, RuntimeError, ValueError) as exc:
        err = {
            "ok": False,
            "status": "failed",
            "operation": "extract_link",
            "payment_method": "team_trial",
            "source": "team_trial_direct",
            "error": _scrub(f"{type(exc).__name__}: {exc}", access_token=token)[:320],
            "error_code": "connection_error",
            "error_stage": "checkout",
            "retryable": True,
        }
        return err, 0, {}, "", billing_country, billing_currency

    status_code = int(getattr(response, "status_code", 0) or 0)
    body = _response_json(response)
    detail = _response_detail(response, body)
    if status_code == 400 and "billing country must match" in detail.lower():
        fresh_country = _detect_me_country(
            token,
            cookie_header=cookie_header,
            extra_headers=req_headers,
            proxy=proxy,
            timeout=timeout,
            get_fn=get_fn,
        )
        if fresh_country and fresh_country != billing_country:
            billing_country = fresh_country
            billing_currency = currency_for_country(billing_country)
            payload = build_team_trial_checkout_payload(
                country=billing_country,
                currency=billing_currency,
                workspace_name=workspace_name,
                seat_quantity=seat_quantity,
                checkout_ui_mode=checkout_ui_mode,
            )
            try:
                response = post_fn(CHECKOUT_URL, payload, token, cookie_header, proxy, timeout, checkout_headers)
            except (OSError, RuntimeError, ValueError) as exc:
                err = {
                    "ok": False,
                    "status": "failed",
                    "operation": "extract_link",
                    "payment_method": "team_trial",
                    "source": "team_trial_direct",
                    "error": _scrub(f"{type(exc).__name__}: {exc}", access_token=token)[:320],
                    "error_code": "connection_error",
                    "error_stage": "checkout",
                    "retryable": True,
                }
                return err, 0, {}, "", billing_country, billing_currency
            status_code = int(getattr(response, "status_code", 0) or 0)
            body = _response_json(response)
            detail = _response_detail(response, body)
    return None, status_code, body, detail, billing_country, billing_currency


def extract_team_trial_link(
    access_token: str,
    *,
    proxy: str = "",
    auth_context: Mapping[str, Any] | None = None,
    timeout: int = 30,
    workspace_name: str = TEAM_TRIAL_DEFAULT_WORKSPACE_NAME,
    seat_quantity: int = TEAM_TRIAL_DEFAULT_SEATS,
    target_country: str = "",
    checkout_country: str = "",
    currency: str = "",
    verify_eligibility: bool = True,
    checkout_ui_mode: str = TEAM_TRIAL_CHECKOUT_UI_MODE,
    get_fn: Callable[..., Any] | None = None,
    post_fn: Callable[..., Any] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Extract a hosted Stripe Checkout link for a GPT Team 5-seat trial account."""
    token = str(access_token or "").strip()
    if not token:
        return {
            "ok": False,
            "status": "failed",
            "payment_method": "team_trial",
            "source": "team_trial_direct",
            "error": "missing access token",
            "error_code": "missing_access_token",
            "error_stage": "validation",
            "retryable": False,
        }

    getter = get_fn or _checkout_get
    poster = post_fn or _checkout_post
    request_timeout = max(5, safe_int(timeout, 30))
    cookie_header, extra_headers = _extract_auth_headers(auth_context)

    account_id = ""
    if verify_eligibility:
        probe = probe_team_trial_eligibility(
            token,
            proxy=proxy,
            auth_context=auth_context,
            timeout=request_timeout,
            get_fn=getter,
        )
        if not probe.get("ok"):
            return {**probe, "operation": "extract_link"}
        if not probe.get("eligible"):
            reason = str(probe.get("ineligibility_reason") or probe.get("coupon_state") or "ineligible")
            return {
                "ok": False,
                "status": "failed",
                "operation": "extract_link",
                "payment_method": "team_trial",
                "source": "team_trial_direct",
                "eligible": False,
                "decision": "team_trial_ineligible",
                "error": f"account is not eligible for {TEAM_TRIAL_CAMPAIGN_ID}: {reason}",
                "error_code": "team_trial_ineligible",
                "error_stage": "eligibility",
                "retryable": False,
            }
        account_id = str(probe.get("personal_account_id") or "")

    if not account_id:
        account_id = _resolve_personal_account_id(
            token,
            auth_context=auth_context,
            cookie_header=cookie_header,
            base_headers=extra_headers,
            proxy=proxy,
            timeout=request_timeout,
            get_fn=getter,
        )
    req_headers = {**extra_headers, **({"Chatgpt-Account-Id": account_id} if account_id else {})}
    billing_country, billing_currency = _resolve_team_billing(
        token,
        cookie_header=cookie_header,
        req_headers=req_headers,
        proxy=proxy,
        timeout=request_timeout,
        checkout_country=checkout_country,
        target_country=target_country,
        currency=currency,
        get_fn=getter,
    )
    resolved_workspace = normalize_team_workspace_name(workspace_name)
    resolved_seats = normalize_team_seat_quantity(seat_quantity)
    transport_err, status_code, body, detail, billing_country, billing_currency = _post_team_checkout(
        token,
        billing_country=billing_country,
        billing_currency=billing_currency,
        workspace_name=resolved_workspace,
        seat_quantity=resolved_seats,
        checkout_ui_mode=checkout_ui_mode,
        cookie_header=cookie_header,
        req_headers=req_headers,
        proxy=proxy,
        timeout=request_timeout,
        get_fn=getter,
        post_fn=poster,
    )
    if transport_err is not None:
        return transport_err
    if status_code != 200:
        return _classify_http_failure(status_code, detail, stage="checkout", access_token=token)

    url = _extract_hosted_url(body)
    if not url:
        return {
            "ok": False,
            "status": "failed",
            "operation": "extract_link",
            "payment_method": "team_trial",
            "source": "team_trial_direct",
            "status_code": status_code,
            "error": _scrub(detail or "team checkout response missing hosted url", access_token=token)[:320],
            "error_code": "team_trial_missing_url",
            "error_stage": "checkout",
            "retryable": False,
        }

    cs_id = _extract_checkout_session_id(body, url)
    result: dict[str, Any] = {
        "ok": True,
        "status": "completed",
        "operation": "extract_link",
        "payment_method": "team_trial",
        "source": "team_trial_direct",
        "link_type": "team_trial_hosted",
        "url": url,
        "checkout_url": url,
        "target_country": billing_country,
        "currency": billing_currency,
        "plan_name": TEAM_TRIAL_PLAN_NAME,
        "promo_campaign_id": TEAM_TRIAL_CAMPAIGN_ID,
        "workspace_name": resolved_workspace,
        "seat_quantity": resolved_seats,
        "eligible": True,
        "decision": "ready",
    }
    if cs_id:
        result["cs_id"] = cs_id
        result["checkout_session_id"] = cs_id
    processor = str(body.get("processor_entity") or "").strip()
    if processor:
        result["processor_entity"] = processor
    return result


__all__ = [
    "TEAM_TRIAL_CANCEL_URL",
    "TEAM_TRIAL_CHECKOUT_UI_MODE",
    "TEAM_TRIAL_DEFAULT_SEATS",
    "TEAM_TRIAL_DEFAULT_WORKSPACE_NAME",
    "TEAM_TRIAL_ENTRY_POINT",
    "TEAM_TRIAL_MAX_SEATS",
    "TEAM_TRIAL_MIN_SEATS",
    "TEAM_TRIAL_PLAN_NAME",
    "build_team_trial_checkout_payload",
    "currency_for_country",
    "extract_team_trial_link",
    "normalize_team_seat_quantity",
    "normalize_team_workspace_name",
    "probe_team_trial_eligibility",
]
