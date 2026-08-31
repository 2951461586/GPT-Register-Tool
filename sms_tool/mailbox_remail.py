import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from urllib.parse import quote

import requests as http_requests

from .config import CFG
from .mail_otp import _candidate_is_newer, _email_otp_candidate, _extract_otp_from_text, _message_received_ts
from .mailbox_types import MailboxAccount
from .paths import project_path, runtime_file

logger = logging.getLogger(__name__)


def _remail_idempotency_key(mode: str, supply: str, payload: dict) -> str:
    """Stable idempotency key for a ReMail batch order.

    Derived from the order intent (service mode + supply + canonical payload) so a
    retried batch request reuses the same key and the server de-duplicates it instead
    of double-charging. A fresh random key per call (the old behaviour) meant zero
    de-duplication on retry.
    """
    canonical = json.dumps(
        {"serviceMode": mode, "supply": supply, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


DEFAULT_BASE_URL = "https://remail.aishop6.com"
DEFAULT_PROJECT_ID = 2
_DEAD_REMAIL_LOCK = threading.Lock()
_OPENAI_REMAIL_SENDERS = re.compile(
    r"(?i)(?:otp@tm1\.openai\.com|noreply@tm\.openai\.com|(?:otp|noreply)@openai\.com)"
)


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


def _dead_remail_registry_path():
    configured = str(_remail_cfg().get("dead_registry_file") or "").strip()
    return project_path(configured) if configured else runtime_file(CFG, "remail_dead_accounts.json")


def _remail_dead_history_enabled():
    value = _remail_cfg().get("exclude_dead_history", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _remail_identity(value):
    if isinstance(value, dict):
        mailbox = value.get("mailbox") if isinstance(value.get("mailbox"), dict) else {}
        purchase = value.get("purchase") if isinstance(value.get("purchase"), dict) else {}
        email = value.get("email") or mailbox.get("email") or purchase.get("email")
        order_no = value.get("order_no") or mailbox.get("order_no") or purchase.get("order_no")
        purchase_id = value.get("purchase_id") or mailbox.get("purchase_id") or purchase.get("purchase_id")
    else:
        email = getattr(value, "email", "")
        order_no = getattr(value, "order_no", "")
        purchase_id = getattr(value, "purchase_id", "")
    return {
        "email": str(email or "").strip().lower(),
        "order_no": str(order_no or "").strip(),
        "purchase_id": str(purchase_id or "").strip(),
    }


def _remail_identity_keys(value):
    identity = _remail_identity(value)
    return {
        f"{key}:{item.lower() if key == 'email' else item}"
        for key, item in identity.items()
        if item
    }


def _read_dead_remail_registry():
    path = _dead_remail_registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    records = payload.get("accounts") if isinstance(payload, dict) else payload
    return [item for item in (records or []) if isinstance(item, dict)]


def record_dead_remail_account(value, reason="account_deactivated"):
    identity = _remail_identity(value)
    if not identity["email"]:
        return False
    now = int(time.time())
    path = _dead_remail_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DEAD_REMAIL_LOCK:
        records = _read_dead_remail_registry()
        target_keys = _remail_identity_keys(identity)
        existing = next((item for item in records if target_keys & _remail_identity_keys(item)), None)
        if existing is None:
            existing = {**identity, "first_seen_at": now}
            records.append(existing)
        else:
            for key, item in identity.items():
                if item:
                    existing[key] = item
        existing["reason"] = str(reason or "account_deactivated")[:120]
        existing["last_seen_at"] = now
        payload = {"version": 1, "accounts": records}
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    return True


def _historically_dead_remail_keys():
    keys = set()
    for item in _read_dead_remail_registry():
        keys.update(_remail_identity_keys(item))
    try:
        from .storage import list_terminal_remail_accounts

        for item in list_terminal_remail_accounts():
            keys.update(_remail_identity_keys(item))
    except Exception:
        pass
    return keys


def filter_dead_remail_mailboxes(mailboxes):
    accounts = list(mailboxes or [])
    if not _remail_dead_history_enabled():
        return accounts
    dead_keys = _historically_dead_remail_keys()
    if not dead_keys:
        return accounts
    kept = []
    skipped = 0
    for mailbox in accounts:
        if str(getattr(mailbox, "provider", "") or "").strip().lower() == "remail" and (
            _remail_identity_keys(mailbox) & dead_keys
        ):
            skipped += 1
            continue
        kept.append(mailbox)
    if skipped:
        print(f"[!] Skipped {skipped} historically deactivated ReMail mailbox(es)")
    return kept


def record_remail_batch_quality(batch_id, results, *, requested=0):
    """Append token-free supplier quality metrics and return the stop decision."""
    rows = [row for row in (results or []) if isinstance(row, dict)]
    total = max(int(requested or 0), len(rows))
    deactivated = sum("account_deactivated" in str(row.get("error") or "").lower() for row in rows)
    otp_timeout = sum(
        "otp" in str(row.get("error") or "").lower()
        and "timeout" in str(row.get("error") or "").lower()
        for row in rows
    )
    dead_rate = (deactivated / total) if total else 0.0
    try:
        threshold = float(_remail_cfg().get("supplier_dead_rate_stop_threshold") or 0.25)
    except (TypeError, ValueError):
        threshold = 0.25
    payload = {
        "batch_id": str(batch_id or "")[:100],
        "requested": total,
        "completed": len(rows),
        "account_deactivated": deactivated,
        "otp_timeout": otp_timeout,
        "dead_rate": round(dead_rate, 6),
        "stop_threshold": threshold,
        "halt_replenishment": bool(total and dead_rate >= threshold),
        "created_at": int(time.time()),
    }
    path = runtime_file(CFG, "remail_quality.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DEAD_REMAIL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return payload


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


_REMAIL_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _remail_order_retry_attempts():
    try:
        return max(0, min(int(_remail_cfg().get("order_retry_attempts", 3)), 8))
    except (TypeError, ValueError):
        return 3


def _remail_order_retry_backoff(attempt):
    try:
        base = float(_remail_cfg().get("order_retry_backoff_seconds", 5))
    except (TypeError, ValueError):
        base = 5.0
    return max(1.0, min(base * (attempt + 1), 30.0))


def _post_remail_order(path, *, params, payload, idempotency_key=None, timeout=30):
    """POST a ReMail order with a STABLE idempotency key and bounded retries.

    ReMail rides behind Cloudflare; a single transient 5xx/429 gateway blip or
    network hiccup otherwise aborts a whole batch acquisition (observed as a
    502 killing a 100-mailbox run at the acquisition step).  The idempotency
    key is generated once and reused across retries so a retried order never
    double-charges the account.
    """
    key = str(idempotency_key or uuid.uuid4())
    attempts = _remail_order_retry_attempts()
    last_exc = None
    for attempt in range(attempts + 1):
        try:
            return _remail_request(
                "POST",
                path,
                auth=True,
                headers={"Idempotency-Key": key},
                params=params,
                json=payload,
                timeout=timeout,
            )
        except ReMailHttpError as exc:
            last_exc = exc
            if exc.status_code not in _REMAIL_RETRYABLE_STATUS or attempt >= attempts:
                raise
        except RuntimeError as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
        wait = _remail_order_retry_backoff(attempt)
        print(
            f"[!] ReMail order {path} attempt {attempt + 1}/{attempts + 1} failed "
            f"({last_exc}); retrying in {wait:.0f}s"
        )
        time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("ReMail order failed without a specific error")


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
    except (TypeError, ValueError) as exc:
        raise ValueError("ReMail project_id must be an integer") from exc
    suffix = str(
        _arg_or_config(args, "remail_email_suffix", "email_suffix", "outlook.com") or "outlook.com"
    ).strip().lstrip("@")
    payload = {"projectId": project_id, "emailSuffix": suffix}
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
    # Route the single-order POST through the idempotent-retry helper so a
    # transient Cloudflare 5xx/429 no longer aborts a one-off acquisition, and
    # a retry reuses the same Idempotency-Key rather than double-charging.
    order = _post_remail_order(
        "/v1/open/orders",
        params={"serviceMode": mode, "supply": supply},
        payload=payload,
    )
    mailbox = _mailbox_from_order(order, service_mode=mode)
    filtered = filter_dead_remail_mailboxes([mailbox])
    if not filtered:
        raise RuntimeError("remail_historically_deactivated_mailbox")
    return filtered[0]


def _recoverable_batch_error(exc):
    if isinstance(exc, ReMailHttpError):
        return exc.status_code in {408, 425, 429} or exc.status_code >= 500
    return "ReMail request failed:" in str(exc)


def _order_created_timestamp(order):
    raw = str((order or {}).get("createdAt") or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _fetch_remail_order_detail(order):
    order_no = str((order or {}).get("orderNo") or "").strip()
    if not order_no:
        raise RuntimeError("ReMail recovery candidate has no order number")
    last_error = None
    for attempt in range(3):
        try:
            return _remail_request(
                "GET",
                "/v1/open/orders/" + quote(order_no, safe=""),
                auth=True,
            )
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise last_error


def _recover_recent_remail_batch(*, started_at, quantity, mode, payload):
    try:
        response = _remail_request(
            "GET",
            "/v1/open/orders",
            auth=True,
            params={"limit": max(100, min(500, quantity * 2))},
        )
    except Exception as exc:
        print(f"[!] ReMail ambiguous-batch recovery lookup failed: {_redact(str(exc))}")
        return []

    now = time.time()
    candidates = []
    for item in response.get("items", []) if isinstance(response, dict) else []:
        if not isinstance(item, dict):
            continue
        created_at = _order_created_timestamp(item)
        if created_at < started_at - 5 or created_at > now + 10:
            continue
        if str(item.get("serviceMode") or "").strip().lower() != mode:
            continue
        if int(item.get("projectId") or 0) != int(payload.get("projectId") or 0):
            continue
        requested_suffix = str(payload.get("emailSuffix") or "").strip().lower()
        if requested_suffix and requested_suffix not in {"outlook", "domain"}:
            delivery_domain = str(item.get("deliveryEmail") or "").strip().lower().rpartition("@")[2]
            if delivery_domain != requested_suffix:
                continue
        candidates.append(item)

    if len(candidates) != quantity:
        logger.warning(
            "ReMail ambiguous-batch recovery refused: expected exactly %s matching recent orders, found %s",
            quantity,
            len(candidates),
        )
        return []

    candidates.sort(key=lambda item: int(item.get("id") or 0))
    try:
        with ThreadPoolExecutor(max_workers=min(8, quantity)) as executor:
            details = list(executor.map(_fetch_remail_order_detail, candidates))
        return filter_dead_remail_mailboxes([_mailbox_from_order(item, service_mode=mode) for item in details])
    except Exception as exc:
        logger.warning("ReMail ambiguous-batch detail recovery failed: %s", _redact(str(exc)))
        return []


def _create_remail_mailboxes(args=None, service_mode="purchase"):
    quantity = max(1, int(getattr(args, "count", None) or 1))
    if quantity == 1:
        return [_create_remail_order(args, service_mode=service_mode)]
    mode, supply, payload = _order_options(args, service_mode=service_mode)
    payload["quantity"] = quantity
    started_at = time.time()
    try:
        results = _remail_request(
            "POST",
            "/v1/open/orders/batch",
            auth=True,
            headers={"Idempotency-Key": _remail_idempotency_key(mode, supply, payload)},
            params={"serviceMode": mode, "supply": supply},
            json=payload,
            timeout=max(30, int(_remail_cfg().get("batch_timeout") or quantity * 2)),
        )
    except Exception as exc:
        if not _recoverable_batch_error(exc):
            raise
        accounts = _recover_recent_remail_batch(
            started_at=started_at,
            quantity=quantity,
            mode=mode,
            payload=payload,
        )
        if accounts:
            logger.info("Recovered %s ReMail order(s) after an ambiguous batch response", len(accounts))
            return accounts
        raise
    accounts = []
    failures = []
    for item in results if isinstance(results, list) else []:
        if str(item.get("status") or "").lower() == "succeeded" and item.get("order"):
            accounts.append(_mailbox_from_order(item["order"], service_mode=mode))
        else:
            failures.append(_redact(item.get("error") or {"index": item.get("index")}))
    if failures:
        logger.warning("ReMail batch returned %s failed item(s): %s", len(failures), failures)
    accounts = filter_dead_remail_mailboxes(accounts)
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


def _trusted_structured_remail_code(mailbox, item):
    """Return ReMail's structured OTP only for the target mailbox and a known OpenAI sender."""
    code = str((item or {}).get("verificationCode") or "").strip()
    recipient = str((item or {}).get("recipient") or "").strip().lower()
    target = str(getattr(mailbox, "email", "") or "").strip().lower()
    sender = str((item or {}).get("sender") or "")
    if not re.fullmatch(r"\d{6}", code):
        return ""
    if not recipient or recipient != target:
        return ""
    if not _OPENAI_REMAIL_SENDERS.search(sender):
        return ""
    return code


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
                vc = _trusted_structured_remail_code(mailbox, item)
                if not vc or vc in excluded:
                    continue
                # Validate keyword if specified
                if keyword:
                    subject_lc = str(item.get("subject") or "").lower()
                    keywords = [p.strip().lower() for p in str(keyword).split("|") if p.strip()]
                    # ReMail currently returns localized subjects with broken
                    # encoding for some Outlook orders. A verified structured
                    # code from an exact OpenAI sender remains authoritative.
                    if keywords and not any(p in subject_lc for p in keywords) and "chatgpt" not in subject_lc:
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
