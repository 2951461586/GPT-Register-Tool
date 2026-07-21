import os
import time
import uuid
from copy import deepcopy

import requests as http_requests

from .config import CFG
from .mail_otp import _candidate_is_newer, _email_otp_candidate, _extract_otp_from_text
from .mailbox_types import MailboxAccount


DEFAULT_BASE_URL = "https://remail.aishop6.com"
DEFAULT_PROJECT_ID = 2
DEFAULT_PRODUCT_ID = 5


def _email_cfg():
    return CFG.get("email_registration", {})


def _remail_cfg():
    email_cfg = _email_cfg()
    nested = email_cfg.get("remail")
    return nested if isinstance(nested, dict) else {}


def _remail_api_key():
    return str(os.environ.get("REMAIL_API_KEY") or _remail_cfg().get("api_key") or "").strip()


def _remail_enabled():
    cfg = _remail_cfg()
    enabled = cfg.get("enabled")
    if enabled is not None and str(enabled).strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(_remail_api_key())


def _remail_base_url():
    return str(_remail_cfg().get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")


def _redact(value, secrets=()):
    safe = deepcopy(value)
    secret_values = [str(item) for item in (_remail_api_key(), *secrets) if str(item or "")]

    def clean(item):
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clean(child) for child in item]
        text = str(item)
        for secret in secret_values:
            text = text.replace(secret, "[REDACTED]")
        return text

    return clean(safe)


def _remail_request(method, path, *, auth=False, headers=None, secrets=(), proxy=None, **kwargs):
    method = str(method or "GET").upper()
    url = _remail_base_url() + path
    request_headers = {"Accept": "application/json", **(headers or {})}
    if auth:
        api_key = _remail_api_key()
        if not api_key:
            raise RuntimeError("email_registration.remail.api_key or REMAIL_API_KEY is required")
        request_headers["Authorization"] = "Bearer " + api_key
    if "json" in kwargs:
        request_headers.setdefault("Content-Type", "application/json")
    normalized_proxy = str(proxy or "").strip()
    proxies = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None
    request_kwargs = {
        "headers": request_headers,
        "timeout": 30,
        "verify": str(_remail_cfg().get("verify_tls", True)).strip().lower() not in {"0", "false", "no", "off"},
        **kwargs,
    }
    if proxies:
        request_kwargs["proxies"] = proxies
    try:
        if method == "GET":
            response = http_requests.get(url, **request_kwargs)
        elif method == "POST":
            response = http_requests.post(url, **request_kwargs)
        else:
            raise ValueError(f"unsupported ReMail method: {method}")
    except Exception as exc:
        raise RuntimeError(f"ReMail request failed: {_redact(str(exc), secrets)}") from None
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:500]}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"ReMail HTTP {response.status_code}: {_redact(body, secrets)}")
    return body


def _arg_or_config(args, arg_name, config_name, default=None):
    value = getattr(args, arg_name, None) if args is not None else None
    if value is None or value == "":
        value = _remail_cfg().get(config_name, default)
    return value


def _order_options(args=None, service_mode=None):
    mode = str(service_mode or _arg_or_config(args, "remail_service_mode", "service_mode", "code")).strip().lower()
    supply = str(_arg_or_config(args, "remail_supply", "supply", "private_first")).strip().lower()
    if mode not in {"code", "purchase"}:
        raise ValueError("ReMail service_mode must be code or purchase")
    if supply not in {"private_first", "public_only"}:
        raise ValueError("ReMail supply must be private_first or public_only")
    try:
        project_id = int(_arg_or_config(args, "remail_project_id", "project_id", DEFAULT_PROJECT_ID))
        product_id = int(_arg_or_config(args, "remail_product_id", "product_id", DEFAULT_PRODUCT_ID))
    except (TypeError, ValueError) as exc:
        raise ValueError("ReMail project_id and product_id must be integers") from exc
    suffix = str(_arg_or_config(args, "remail_email_suffix", "email_suffix", "") or "").strip().lstrip("@")
    payload = {"projectId": project_id, "productId": product_id}
    if suffix:
        payload["emailSuffix"] = suffix
    return mode, supply, payload


def _mailbox_from_order(order, service_mode=None):
    order = order or {}
    email = str(order.get("deliveryEmail") or "").strip().lower()
    token = str(order.get("serviceToken") or "").strip()
    order_no = str(order.get("orderNo") or "").strip()
    mode = str(order.get("serviceMode") or service_mode or "code").strip().lower()
    if not email or not token or not order_no:
        raise RuntimeError(f"ReMail order returned incomplete mailbox data: {_redact(order, (token,))}")
    return MailboxAccount(
        email=email,
        source=f"remail_{mode}",
        provider="remail",
        order_no=order_no,
        token=token,
        purchase_id=str(order.get("id") or ""),
        price=str(order.get("payAmount") or ""),
    )


def _create_remail_order(args=None, service_mode=None):
    mode, supply, payload = _order_options(args, service_mode=service_mode)
    order = _remail_request(
        "POST",
        "/v1/open/orders",
        auth=True,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        params={"serviceMode": mode, "supply": supply},
        json=payload,
    )
    return _mailbox_from_order(order, service_mode=mode)


def _create_remail_mailboxes(args=None, service_mode="purchase"):
    quantity = max(1, int(getattr(args, "count", None) or 1))
    if quantity == 1:
        return [_create_remail_order(args, service_mode=service_mode)]
    mode, supply, payload = _order_options(args, service_mode=service_mode)
    payload["quantity"] = quantity
    results = _remail_request(
        "POST",
        "/v1/open/orders/batch",
        auth=True,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        params={"serviceMode": mode, "supply": supply},
        json=payload,
    )
    accounts = []
    failures = []
    for item in results if isinstance(results, list) else []:
        if str(item.get("status") or "").lower() == "succeeded" and item.get("order"):
            accounts.append(_mailbox_from_order(item["order"], service_mode=mode))
        else:
            failures.append(_redact(item.get("error") or {"index": item.get("index")}))
    if failures:
        print(f"[!] ReMail batch returned {len(failures)} failed item(s): {failures}")
    if not accounts:
        raise RuntimeError("ReMail batch returned no usable mailboxes")
    return accounts


def _normalize_remail_message(message):
    message = message or {}
    recipient = str(message.get("recipient") or "").strip().lower()
    verification_code = str(message.get("verificationCode") or "").strip()
    preview = str(message.get("bodyPreview") or "")
    if verification_code and verification_code not in preview:
        preview = verification_code + "\n" + preview
    return {
        "id": str(message.get("id") or ""),
        "from": str(message.get("sender") or ""),
        "toRecipients": [{"emailAddress": {"address": recipient}}] if recipient else [],
        "receivedDateTime": str(message.get("receivedAt") or ""),
        "subject": str(message.get("subject") or ""),
        "bodyPreview": preview,
        "body": {"content": str(message.get("body") or "")},
        "verificationCode": verification_code,
    }


def _fetch_remail_message_detail(mailbox, message_id, proxy=None):
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    token = str(getattr(mailbox, "token", "") or "").strip()
    if not email or not token:
        raise RuntimeError("ReMail mailbox requires delivery email and service token")
    return _remail_request(
        "GET",
        f"/v1/pickup/messages/{message_id}",
        params={"email": email, "token": token},
        secrets=(token,),
        proxy=proxy,
    )


def _fetch_remail_messages(mailbox, limit=25, proxy=None):
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    token = str(getattr(mailbox, "token", "") or "").strip()
    if not email or not token:
        raise RuntimeError("ReMail mailbox requires delivery email and service token")
    response = _remail_request(
        "GET",
        "/v1/pickup",
        params={"email": email, "token": token},
        secrets=(token,),
        proxy=proxy,
    )
    items = response.get("items") if isinstance(response, dict) else []
    normalized = []
    for item in list(items or [])[:max(1, int(limit or 25))]:
        text = "\n".join(
            str(item.get(key) or "")
            for key in ("subject", "bodyPreview", "verificationCode")
        )
        if not _extract_otp_from_text(text) and item.get("id") is not None:
            try:
                item = _fetch_remail_message_detail(mailbox, item["id"], proxy=proxy)
            except Exception as exc:
                print(f"[remail message detail error: {exc}]")
        normalized.append(_normalize_remail_message(item))
    normalized.sort(key=lambda item: item.get("receivedDateTime") or "", reverse=True)
    return normalized


def _latest_remail_otp_candidate(mailbox, messages, keyword="", issued_after_unix=0, excluded_otps=None):
    latest = None
    seen_message_id = str(getattr(mailbox, "seen_message_id", "") or "").strip()
    excluded = {str(value or "").strip() for value in (excluded_otps or ())}
    for message in messages or []:
        if seen_message_id and str(message.get("id") or "").strip() == seen_message_id:
            continue
        candidate = _email_otp_candidate(
            mailbox,
            message,
            keyword=keyword,
            issued_after_unix=issued_after_unix,
        )
        if not candidate or candidate.get("otp") in excluded:
            continue
        if _candidate_is_newer(candidate, latest):
            latest = candidate
    return latest


def _poll_remail_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None, excluded_otps=None, poll_interval=2):
    deadline = time.time() + timeout
    interval = max(1.0, float(poll_interval or 2))
    keyword = str(subject_keyword or "").lower()
    while time.time() < deadline:
        try:
            candidate = _latest_remail_otp_candidate(
                mailbox,
                _fetch_remail_messages(mailbox, proxy=proxy),
                keyword=keyword,
                issued_after_unix=issued_after_unix,
                excluded_otps=excluded_otps,
            )
            if candidate:
                print(f" code:{candidate['otp']}!")
                return candidate["otp"]
        except Exception as exc:
            print(f"[remail poll error: {exc}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None
