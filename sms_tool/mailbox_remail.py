import os
import time
import uuid
from copy import deepcopy
from datetime import datetime
from urllib.parse import quote

import requests as http_requests

from .config import CFG
from .mail_otp import _candidate_is_newer, _email_otp_candidate, _extract_otp_from_text, _message_received_ts
from .mailbox_types import MailboxAccount


DEFAULT_BASE_URL = "https://remail.aishop6.com"
DEFAULT_PROJECT_ID = 2
DEFAULT_PRODUCT_ID = 5


class ReMailHttpError(RuntimeError):
    def __init__(self, status_code, body):
        self.status_code = int(status_code or 0)
        self.body = body
        super().__init__(f"ReMail HTTP {self.status_code}: {body}")


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
        raise ReMailHttpError(response.status_code, _redact(body, secrets))
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


def _lookup_remail_order(mailbox, proxy=None):
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    order_no = str(getattr(mailbox, "order_no", "") or "").strip()
    if order_no:
        order = _remail_request(
            "GET",
            "/v1/open/orders/" + quote(order_no, safe=""),
            auth=True,
            proxy=proxy,
        )
        if isinstance(order, dict) and str(order.get("deliveryEmail") or "").strip().lower() == email:
            return order
        raise RuntimeError("ReMail order does not match the selected mailbox")

    response = _remail_request(
        "GET",
        "/v1/open/orders",
        auth=True,
        params={"search": email, "limit": 100},
        proxy=proxy,
    )
    items = response.get("items") if isinstance(response, dict) else []
    matches = [
        item for item in (items or [])
        if isinstance(item, dict)
        and str(item.get("deliveryEmail") or "").strip().lower() == email
    ]
    if not matches:
        raise RuntimeError("ReMail API Key cannot find an order for the selected mailbox")
    matches.sort(
        key=lambda item: (
            bool(item.get("serviceToken")),
            str(item.get("status") or "").lower() == "active",
            int(item.get("id") or 0),
        ),
        reverse=True,
    )
    return matches[0]


def _recover_remail_service_token(mailbox, proxy=None):
    if not _remail_api_key():
        raise RuntimeError(
            "ReMail Service Token is invalid or expired. Configure the API Key to inspect the order; "
            "the API Key cannot be used directly as a pickup token."
        )
    try:
        order = _lookup_remail_order(mailbox, proxy=proxy)
    except Exception as exc:
        raise RuntimeError(f"ReMail Service Token is invalid or expired, and order lookup failed: {exc}") from None

    current_token = str(order.get("serviceToken") or "").strip()
    old_token = str(getattr(mailbox, "token", "") or "").strip()
    if current_token and current_token != old_token:
        mailbox.token = current_token
        mailbox.order_no = str(order.get("orderNo") or getattr(mailbox, "order_no", "") or "").strip()
        mailbox.purchase_id = str(order.get("id") or getattr(mailbox, "purchase_id", "") or "").strip()
        return

    mode = str(order.get("serviceMode") or "").strip().lower()
    status = str(order.get("status") or "").strip().lower()
    receive_until = str(order.get("receiveUntil") or "").strip()
    if mode == "code" and (not current_token or status in {"completed", "closed", "refunded", "failed"}):
        deadline = f"（收件截止 {receive_until}）" if receive_until else ""
        raise RuntimeError(
            f"ReMail 短效接码订单已失效{deadline}。API Key 只能查询订单，不能代替 Service Token "
            "读取过期收件箱；请新建订单，需要长期查看请使用 purchase 模式。"
        )
    raise RuntimeError(
        "ReMail Service Token is invalid or expired, and the order API did not return a replacement token."
    )


def _pickup_remail_messages(mailbox, proxy=None):
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    token = str(getattr(mailbox, "token", "") or "").strip()
    return _remail_request(
        "GET",
        "/v1/pickup",
        params={"email": email, "token": token},
        secrets=(token,),
        proxy=proxy,
    )


def _fetch_remail_messages(mailbox, limit=25, proxy=None, include_body=False):
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    token = str(getattr(mailbox, "token", "") or "").strip()
    if not email or not token:
        raise RuntimeError("ReMail mailbox requires delivery email and service token")
    try:
        response = _pickup_remail_messages(mailbox, proxy=proxy)
    except ReMailHttpError as exc:
        if exc.status_code != 401:
            raise
        _recover_remail_service_token(mailbox, proxy=proxy)
        response = _pickup_remail_messages(mailbox, proxy=proxy)
    items = response.get("items") if isinstance(response, dict) else []
    normalized = []
    for item in list(items or [])[:max(1, int(limit or 25))]:
        text = "\n".join(
            str(item.get(key) or "")
            for key in ("subject", "bodyPreview", "verificationCode")
        )
        if (include_body or not _extract_otp_from_text(text)) and item.get("id") is not None:
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


def _remail_otp_poll_interval():
    """ReMail-specific OTP poll interval (default 1s, faster than global 2s).

    ReMail's /v1/pickup endpoint is lightweight and returns verificationCode
    directly, so we can poll more aggressively than Graph/IMAP providers.
    Configurable via remail.otp_poll_interval in config.json.
    """
    try:
        return max(0.5, float(_remail_cfg().get("otp_poll_interval", 1.0)))
    except Exception:
        return 1.0


def _remail_parse_fetch_next_allowed(response):
    """Parse fetch.nextFetchAllowedAt from the /v1/pickup response.

    ReMail returns this field to indicate the earliest time the next pickup
    request is allowed.  Returns seconds-to-wait (float), or 0 if not present.
    """
    try:
        fetch_info = (response or {}).get("fetch") or {}
        next_allowed = str(fetch_info.get("nextFetchAllowedAt") or "").strip()
        if not next_allowed:
            return 0.0
        dt = datetime.fromisoformat(next_allowed.replace("Z", "+00:00"))
        wait = dt.timestamp() - time.time()
        return max(0.0, wait)
    except Exception:
        return 0.0


def _remail_fetch_state(response):
    """Parse the fetch state from /v1/pickup response.

    ReMail's /v1/pickup returns a ``fetch`` object describing the server-side
    mail-fetch job state.  Key fields (per API docs):

    - lastStatus:      "succeeded" / "pending" / "failed" etc.
    - lastReceivedAt:  ISO timestamp of the most recently received email
    - nextFetchAllowedAt: earliest time the next pickup is allowed
    - lastSafeError:   non-fatal error from the last fetch attempt

    Returns a dict with ``last_status``, ``last_received_at``,
    ``next_allowed_delay`` and ``last_safe_error``.
    """
    try:
        fetch_info = (response or {}).get("fetch") or {}
        return {
            "last_status": str(fetch_info.get("lastStatus") or "").strip().lower(),
            "last_received_at": str(fetch_info.get("lastReceivedAt") or "").strip(),
            "next_allowed_delay": _remail_parse_fetch_next_allowed(response),
            "last_safe_error": str(fetch_info.get("lastSafeError") or "").strip(),
        }
    except Exception:
        return {"last_status": "", "last_received_at": "", "next_allowed_delay": 0.0, "last_safe_error": ""}


def _remail_item_received_ts(item):
    """Parse receivedAt timestamp from a raw ReMail message item."""
    return _message_received_ts({"receivedDateTime": str((item or {}).get("receivedAt") or "")})


def _poll_remail_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None, excluded_otps=None, poll_interval=None):
    """Poll ReMail /v1/pickup for OTP codes with adaptive interval.

    Optimisations:
    1. Calls /v1/pickup directly, using verificationCode fast path.
    2. ReMail-specific default poll interval of 1s (vs global 2s).
    3. Initial settle delay (1s) before first poll to let OTP email arrive,
       avoiding a wasted early API call.
    4. Graduated adaptive backoff: 1s (0-5s) -> 1.5s (5-15s) -> 3s (15s+).
    5. Respects fetch.nextFetchAllowedAt from the response.
    6. Uses fetch.lastStatus to detect mailbox readiness; backs off when
       the mailbox hasn't connected yet.
    7. Tracks fetch.lastReceivedAt to detect stale state and back off
       when no new email has arrived.
    """
    deadline = time.time() + timeout
    base_interval = max(0.5, float(poll_interval) if poll_interval is not None else _remail_otp_poll_interval())
    keyword = str(subject_keyword or "").lower()
    excluded = {str(value or "").strip() for value in (excluded_otps or ())}
    seen_message_id = str(getattr(mailbox, "seen_message_id", "") or "").strip()
    start_time = deadline - timeout

    # Initial settle delay: let the OTP email arrive before first poll.
    # Based on batch data, OTPs typically arrive in 3-10s; a 1s delay
    # avoids one wasted API call without meaningfully slowing detection.
    time.sleep(min(base_interval, 1.0))

    prev_last_received_at = ""

    while time.time() < deadline:
        # Graduated adaptive interval
        elapsed = time.time() - start_time
        if elapsed < 5:
            interval = base_interval
        elif elapsed < 15:
            interval = min(base_interval * 1.5, 2.0)
        else:
            interval = min(base_interval * 3, 3.0)
        try:
            raw_response = _pickup_remail_messages(mailbox, proxy=proxy)
            items = (raw_response or {}).get("items") if isinstance(raw_response, dict) else []
            fetch_state = _remail_fetch_state(raw_response)

            # Fast path: check verificationCode directly from raw items
            best_code = None
            best_ts = 0
            for item in (items or []):
                msg_id = str(item.get("id") or "").strip()
                if seen_message_id and msg_id == seen_message_id:
                    continue
                vc = str(item.get("verificationCode") or "").strip()
                if not vc or vc in excluded:
                    continue
                # Validate keyword if specified
                if keyword:
                    subject_lc = str(item.get("subject") or "").lower()
                    keywords = [p.strip().lower() for p in str(keyword).split("|") if p.strip()]
                    if keywords and not any(p in subject_lc for p in keywords):
                        continue
                # Validate timestamp
                recv_ts = _remail_item_received_ts(item)
                if issued_after_unix > 0 and recv_ts and recv_ts < issued_after_unix:
                    continue
                # Track the most recent matching code
                if recv_ts >= best_ts:
                    best_code = vc
                    best_ts = recv_ts

            if best_code:
                print(f" code:{best_code}!")
                return best_code

            # Fallback: full normalisation + OTP extraction (without detail
            # fetches) for messages where verificationCode is absent.
            normalized = [_normalize_remail_message(item) for item in (items or [])]
            candidate = _latest_remail_otp_candidate(
                mailbox,
                normalized,
                keyword=keyword,
                issued_after_unix=issued_after_unix,
                excluded_otps=excluded_otps,
            )
            if candidate:
                print(f" code:{candidate['otp']}!")
                return candidate["otp"]

            # Respect server-side rate limit hint
            server_delay = fetch_state.get("next_allowed_delay", 0.0)
            if server_delay > 0:
                interval = max(interval, min(server_delay, 5.0))

            # If mailbox fetch hasn't succeeded yet, wait longer before
            # next poll — the mailbox may not be connected.
            last_status = fetch_state.get("last_status", "")
            if last_status and last_status not in ("succeeded", ""):
                interval = max(interval, 2.0)

            # If lastReceivedAt is unchanged, no new email has arrived —
            # gradually back off to reduce unnecessary API calls.
            current_last_received = fetch_state.get("last_received_at", "")
            if prev_last_received_at and current_last_received == prev_last_received_at:
                interval = max(interval, min(base_interval * 1.5, 2.0))
            prev_last_received_at = current_last_received
        except Exception as exc:
            print(f"[remail poll error: {exc}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None
