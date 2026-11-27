import logging
import os
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import requests as http_requests

from ..config import current_config_data
from ..kernel.paths import project_path, runtime_file
from ..kernel.sanitizer import mask_otp
from ..mailbox.types import MailboxAccount
from ..proxy.health import ProxyHealthTracker
from . import ports
from .mailbox_remail_state import (
    DEFAULT_BASE_URL,
    DEFAULT_PROJECT_ID,
    ReMailCredentialError,
    ReMailHttpError,
    ReMailTransportError,
    _DEAD_REMAIL_LOCK as _DEAD_REMAIL_LOCK,
    _FETCH_STATUS_FAILING as _FETCH_STATUS_FAILING,
    _FETCH_STATUS_READY as _FETCH_STATUS_READY,
    _FETCH_STATUS_SETTLING as _FETCH_STATUS_SETTLING,
    _OPENAI_REMAIL_SENDERS as _OPENAI_REMAIL_SENDERS,
    _REMAIL_ITEM_ERROR_HINTS as _REMAIL_ITEM_ERROR_HINTS,
    _REMAIL_RETRYABLE_STATUS,
    _batch_failure_reason,
    _batch_item_failure,
    _fetch_state_interval,
    _normalize_remail_message,
    _order_created_timestamp,
    _recoverable_batch_error,
    _remail_fetch_state,
    _remail_idempotency_key,
    _remail_identity as _remail_identity,
    _remail_identity_keys,
    _remail_item_received_ts,
    _remail_parse_fetch_next_allowed as _remail_parse_fetch_next_allowed,
    _remail_request_id,
    _summarize_item_failures,
    _trusted_structured_remail_code,
    candidate_is_newer,
    email_otp_candidate,
    extract_otp_from_text,
    read_dead_remail_registry_file,
    record_dead_remail_account_file,
    record_remail_batch_quality_file,
    redact_proxy_url,
    redact_remail_value,
)

logger = logging.getLogger(__name__)


def mailbox_email_config():
    return current_config_data().get("email_registration", {})


def _remail_cfg():
    email_cfg = mailbox_email_config()
    nested = email_cfg.get("remail")
    return nested if isinstance(nested, Mapping) else {}


def _remail_proxy_health_path():
    configured = str(_remail_cfg().get("proxy_health_file") or "").strip()
    return project_path(configured) if configured else runtime_file(current_config_data(), "remail_proxy_health.json")


def _remail_proxy_tracker():
    return ProxyHealthTracker(current_config_data(), path=_remail_proxy_health_path())


def _remail_transport_attempts():
    try:
        return max(1, min(int(_remail_cfg().get("transport_retry_attempts", 3)), 8))
    except (TypeError, ValueError):
        return 3


def _remail_transport_backoff(attempt):
    try:
        base = float(_remail_cfg().get("transport_retry_backoff_seconds", 1.0))
    except (TypeError, ValueError):
        base = 1.0
    return max(0.5, min(base * (2 ** max(0, int(attempt))), 8.0))


def _remail_preflight_enabled():
    value = _remail_cfg().get("preflight_enabled", False)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _remail_preflight(proxy=None):
    """Check the ReMail edge before spending the OTP wait budget.

    ``GET /v1/open/apikey/profile`` is the documented authenticated probe: it
    returns the key's ``balance`` / ``remainingQuota`` / ``expireAt`` and costs
    no credits ("查询接口不消耗积分"), so a dead key or an empty wallet is caught
    before an OTP wait is spent.  ``/docs`` stays as the fallback for a supplier
    build that predates that endpoint — it proves reachability only.
    """
    # A preflight must never be the thing that breaks a poll: only a transport
    # error is allowed out of here (the caller rotates proxies on it).  A missing
    # or rejected key degrades to the unauthenticated reachability probe, and the
    # real pickup call reports the credential problem with its own error class.
    if not _remail_api_key():
        return _remail_request("GET", "/docs", proxy=proxy, timeout=10)
    try:
        profile = _remail_request("GET", "/v1/open/apikey/profile", auth=True, proxy=proxy, timeout=10)
    except ReMailHttpError as exc:
        print(f"  [remail preflight] apikey/profile HTTP {exc.status_code}; falling back to /docs")
        return _remail_request("GET", "/docs", proxy=proxy, timeout=10)
    if isinstance(profile, dict):
        _log_remail_profile(profile)
    return profile


def _log_remail_profile(profile):
    """One line of key status; a zero balance is the actionable part."""
    key = (profile or {}).get("apiKey")
    key = key if isinstance(key, dict) else {}
    balance = str(key.get("balance") or "").strip()
    quota = str(key.get("remainingQuota") or "").strip()
    expire = str(key.get("expireAt") or "").strip()
    try:
        empty = float(balance or 0) <= 0
    except ValueError:
        empty = False
    if empty:
        print(f"  [remail] API key balance is {balance or '0'} — orders will fail with insufficient_balance")
    elif balance or quota or expire:
        print(f"  [remail] key balance={balance or '-'} remainingQuota={quota or '-'} expireAt={expire or '-'}")


def _remail_api_key():
    return str(os.environ.get("REMAIL_API_KEY") or _remail_cfg().get("api_key") or "").strip()


def remail_enabled():
    cfg = _remail_cfg()
    enabled = cfg.get("enabled")
    if enabled is not None and str(enabled).strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(_remail_api_key())


def _remail_base_url():
    return str(_remail_cfg().get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")


def _dead_remail_registry_path():
    configured = str(_remail_cfg().get("dead_registry_file") or "").strip()
    return project_path(configured) if configured else runtime_file(current_config_data(), "remail_dead_accounts.json")


def _remail_dead_history_enabled():
    value = _remail_cfg().get("exclude_dead_history", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _read_dead_remail_registry():
    return read_dead_remail_registry_file(_dead_remail_registry_path())


def record_dead_remail_account(value, reason="account_deactivated"):
    return record_dead_remail_account_file(_dead_remail_registry_path(), value, reason=reason)


def record_remail_credential_failure(value, reason="mailbox_auth_invalid"):
    """Persist a token-free quarantine marker for a mailbox credential."""
    return record_dead_remail_account(value, reason=reason)


def _historically_dead_remail_keys():
    keys = set()
    for item in _read_dead_remail_registry():
        keys.update(_remail_identity_keys(item))
    try:
        from ..storage import list_terminal_remail_accounts

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
    try:
        threshold = float(_remail_cfg().get("supplier_dead_rate_stop_threshold") or 0.25)
    except (TypeError, ValueError):
        threshold = 0.25
    return record_remail_batch_quality_file(
        runtime_file(current_config_data(), "remail_quality.jsonl"),
        batch_id,
        results,
        requested=requested,
        threshold=threshold,
    )


def _redact(value, secrets=()) -> Any:
    return redact_remail_value(value, (_remail_api_key(), *secrets))


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
    # ``request_kwargs`` is splatted into requests/curl_cffi, which accept a plain
    # proxy mapping; the TypedDict annotation only exists on the callee side.
    proxies: Any = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None
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
        raise ReMailTransportError(detail=_redact(str(exc), secrets), proxy=normalized_proxy) from None
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:500]}
    if response.status_code < 200 or response.status_code >= 300:
        raise ReMailHttpError(response.status_code, _redact(body, secrets))
    return body


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
            f"[!] ReMail order {path} attempt {attempt + 1}/{attempts + 1} failed ({last_exc}); retrying in {wait:.0f}s"
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
    suffix = (
        str(_arg_or_config(args, "remail_email_suffix", "email_suffix", "outlook.com") or "outlook.com")
        .strip()
        .lstrip("@")
    )
    payload = {"projectId": project_id, "emailSuffix": suffix}
    return mode, supply, payload


def _mailbox_from_order(order, service_mode=None):
    order = order or {}
    email = str(order.get("deliveryEmail") or "").strip().lower()
    token = str(order.get("serviceToken") or "").strip()
    order_no = str(order.get("orderNo") or "").strip()
    mode = str(order.get("serviceMode") or service_mode or "code").strip().lower()
    if not email or not order_no:
        raise RuntimeError(f"ReMail order returned incomplete mailbox data: {_redact(order, (token,))}")
    if not token:
        product = str(order.get("productType") or order.get("allocationType") or "").strip().lower()
        if product == "icloud":
            raise RuntimeError(
                "ReMail iCloud order has no serviceToken; /v1/pickup requires email and token, "
                "so this order cannot be used as an OTP mailbox source"
            )
        raise RuntimeError(f"ReMail {product or 'mailbox'} order returned no serviceToken")
    return MailboxAccount(
        email=email,
        source=f"remail_{mode}",
        provider="remail",
        order_no=order_no,
        token=token,
        purchase_id=str(order.get("id") or ""),
        price=str(order.get("payAmount") or ""),
    )


def _order_with_service_token(order):
    """Fill in a ``serviceToken`` the create response did not carry.

    Measured 2026-11-19 against ``remail.best``: a **purchase** order on the
    ``icloud`` product comes back from ``POST /v1/open/orders`` **without**
    ``serviceToken``, while ``GET /v1/open/orders/{orderNo}`` carries it -- and
    ``/v1/pickup?email&token`` then answers with the real OpenAI verification
    mail (checked on a completed iCloud order: ``noreply@tm.openai.com``,
    subject "Your temporary ChatGPT verification code").  Without this lookup the
    old guard rejected the order *after* the credits were spent (36 paid per
    iCloud mailbox in the operator's own history), so every iCloud attempt burned
    money and produced nothing.

    The guard itself stays: if the detail response has no token either, the
    order really cannot be polled and `_mailbox_from_order` still says so.
    """
    if not isinstance(order, dict) or str(order.get("serviceToken") or "").strip():
        return order
    order_no = str(order.get("orderNo") or "").strip()
    if not order_no:
        return order
    try:
        detail = _remail_request("GET", f"/v1/open/orders/{order_no}", auth=True)
    except (ReMailHttpError, ReMailTransportError, ValueError):
        return order
    data = detail.get("data") if isinstance(detail, dict) and "data" in detail else detail
    if not isinstance(data, dict):
        return order
    merged = dict(order)
    for key in ("serviceToken", "verificationCode", "receiveUntil", "hasDelivery"):
        if not merged.get(key) and data.get(key):
            merged[key] = data[key]
    return merged


def _create_remail_order(args=None, service_mode=None):
    mode, supply, payload = _order_options(args, service_mode=service_mode)
    # Route the single-order POST through the idempotent-retry helper so a
    # transient Cloudflare 5xx/429 no longer aborts a one-off acquisition, and
    # a retry reuses the same Idempotency-Key rather than double-charging.
    order = _order_with_service_token(
        _post_remail_order(
            "/v1/open/orders",
            params={"serviceMode": mode, "supply": supply},
            payload=payload,
        )
    )
    mailbox = _mailbox_from_order(order, service_mode=mode)
    filtered = filter_dead_remail_mailboxes([mailbox])
    if not filtered:
        raise RuntimeError("remail_historically_deactivated_mailbox")
    return filtered[0]


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
                time.sleep(2**attempt)
    if last_error is None:
        raise RuntimeError("ReMail order lookup failed")
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
    batch_id = str(getattr(args, "registration_batch_id", None) or "").strip()
    order_round = int(getattr(args, "remail_order_round", 0) or 0)
    if batch_id and order_round > 0:
        batch_id = f"{batch_id}:r{order_round}"
    idempotency_key = _remail_idempotency_key(mode, supply, payload, batch_id=batch_id)
    started_at = time.time()
    try:
        # The batch POST goes through the same idempotent-retry helper as the
        # single order.  The OpenAPI contract makes that safe — "相同幂等键重试会
        # 收敛到相同的完整批次结果" — and it is what the 2026-11-11 run needed:
        # a Cloudflare 502 on this call aborted a 5-mailbox batch before any
        # mailbox existed, because only the single-order path retried.
        results = _post_remail_order(
            "/v1/open/orders/batch",
            params={"serviceMode": mode, "supply": supply},
            payload=payload,
            idempotency_key=idempotency_key,
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
            accounts.append(_mailbox_from_order(_order_with_service_token(item["order"]), service_mode=mode))
        else:
            failures.append(_batch_item_failure(item))
    if failures:
        logger.warning("ReMail batch returned %s failed item(s): %s", len(failures), _summarize_item_failures(failures))
    accounts = filter_dead_remail_mailboxes(accounts)
    if not accounts:
        raise RuntimeError(_batch_failure_reason(failures))
    return accounts


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
        item
        for item in (items or [])
        if isinstance(item, dict) and str(item.get("deliveryEmail") or "").strip().lower() == email
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
    except ReMailHttpError:
        # Preserve the API-auth response so callers can distinguish a broken
        # supplier key from a single mailbox's expired service token.
        raise
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


def _pickup_remail_messages_with_recovery(mailbox, proxy=None):
    """Pickup messages once, refreshing a stale token at most once.

    ReMail's pickup endpoint is authenticated by the per-order service token.
    A 401 is not retryable by itself.  We allow one order lookup to replace a
    stale token, then raise a stable credential error so the caller can stop
    polling and quarantine only this mailbox.
    """
    try:
        return _pickup_remail_messages(mailbox, proxy=proxy)
    except ReMailHttpError as pickup_error:
        if pickup_error.status_code != 401:
            raise

        try:
            _recover_remail_service_token(mailbox, proxy=proxy)
        except ReMailHttpError as recovery_error:
            if recovery_error.status_code in {404, 410}:
                raise ReMailCredentialError(
                    code="mailbox_auth_invalid",
                    email=getattr(mailbox, "email", ""),
                    detail="ReMail order no longer exists; repair the mailbox pool",
                ) from None
            if recovery_error.status_code == 401:
                raise ReMailCredentialError(
                    code="remail_api_auth_invalid",
                    email=getattr(mailbox, "email", ""),
                    request_id=_remail_request_id(recovery_error) or _remail_request_id(pickup_error),
                    detail="ReMail API key is invalid or expired",
                ) from None
            raise
        except Exception as recovery_error:
            raise ReMailCredentialError(
                code="mailbox_auth_invalid",
                email=getattr(mailbox, "email", ""),
                request_id=_remail_request_id(pickup_error),
                detail=str(recovery_error),
            ) from None

        try:
            return _pickup_remail_messages(mailbox, proxy=proxy)
        except ReMailHttpError as retry_error:
            if retry_error.status_code != 401:
                raise
            raise ReMailCredentialError(
                code="mailbox_auth_invalid",
                email=getattr(mailbox, "email", ""),
                request_id=_remail_request_id(retry_error) or _remail_request_id(pickup_error),
                detail="ReMail service token remains invalid after refresh",
            ) from None


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
    response = _pickup_remail_messages_with_recovery(mailbox, proxy=proxy)
    items = response.get("items") if isinstance(response, dict) else []
    normalized = []
    for item in list(items or [])[: max(1, int(limit or 25))]:
        if not isinstance(item, dict):
            continue
        text = "\n".join(str(item.get(key) or "") for key in ("subject", "bodyPreview", "verificationCode"))
        if (include_body or not extract_otp_from_text(text)) and item.get("id") is not None:
            try:
                # Separate name: assigning back to ``item`` widened its inferred
                # type to the helper's union, so the next ``item[...]`` read was
                # typed against str/list.
                detail_item = _fetch_remail_message_detail(mailbox, item.get("id"), proxy=proxy)
                if isinstance(detail_item, dict):
                    item = detail_item
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
        candidate = email_otp_candidate(
            mailbox,
            message,
            keyword=keyword,
            issued_after_unix=issued_after_unix,
        )
        if not candidate or candidate.get("otp") in excluded:
            continue
        if candidate_is_newer(candidate, latest):
            latest = candidate
    return latest


def _remail_otp_poll_interval():
    """ReMail-specific OTP poll interval (default 1s, faster than global 2s)."""
    try:
        return max(0.5, float(_remail_cfg().get("otp_poll_interval", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def _poll_remail_otp(
    mailbox,
    subject_keyword="",
    timeout=300,
    issued_after_unix=0,
    proxy=None,
    excluded_otps=None,
    poll_interval=None,
    proxy_candidates=None,
):
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
    candidates = [str(item or "").strip() for item in (proxy_candidates or ()) if str(item or "").strip()]
    if proxy and proxy not in candidates:
        candidates.insert(0, str(proxy).strip())
    if not candidates:
        candidates = [str(proxy or "").strip()]
    try:
        tracker = _remail_proxy_tracker()
        candidates = tracker.rank(candidates)
    except Exception:
        tracker = None
    proxy_index = 0
    active_proxy: Any = candidates[proxy_index] if candidates else proxy
    transport_failures = 0
    # Unnewlined dots glue themselves to the head of the next line another
    # thread flushes, which breaks the host's line-anchored envelope parsing.
    dots = ports.progress_dots_enabled()

    if _remail_preflight_enabled():
        try:
            _remail_preflight(active_proxy)
            if tracker is not None and active_proxy:
                tracker.record(active_proxy, ok=True)
        except ReMailTransportError as exc:
            transport_failures = 1
            if tracker is not None and active_proxy:
                tracker.record(active_proxy, ok=False, error=exc.detail or str(exc))
            if len(candidates) > 1:
                proxy_index = (proxy_index + 1) % len(candidates)
                active_proxy = candidates[proxy_index]
            wait = _remail_transport_backoff(0)
            print(
                f"[remail preflight failed; retry proxy={redact_proxy_url(active_proxy, empty_placeholder='DIRECT')} "
                f"wait={wait:.1f}s]"
            )
            time.sleep(min(wait, max(0.0, deadline - time.time())))

    # Initial settle delay: let the OTP email arrive before first poll.
    # Based on batch data, OTPs typically arrive in 3-10s; a 1s delay
    # avoids one wasted API call without meaningfully slowing detection.
    time.sleep(min(base_interval, 1.0))

    prev_last_received_at = ""
    prev_safe_error = ""

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
            raw_response = _pickup_remail_messages_with_recovery(mailbox, proxy=active_proxy)
            transport_failures = 0
            if tracker is not None and active_proxy:
                tracker.record(active_proxy, ok=True)
            items = (raw_response or {}).get("items") if isinstance(raw_response, dict) else []
            fetch_state = _remail_fetch_state(raw_response)

            # Fast path: check verificationCode directly from raw items
            best_code = None
            best_ts = 0
            for item in items or []:
                if not isinstance(item, dict):
                    continue
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
                print(f" code:{mask_otp(best_code)}!")
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
                print(f" code:{mask_otp(candidate['otp'])}!")
                return candidate["otp"]

            # Respect server-side rate limit hint
            server_delay = fetch_state.get("next_allowed_delay", 0.0)
            if server_delay > 0:
                interval = max(interval, min(server_delay, 5.0))

            interval, prev_safe_error = _fetch_state_interval(interval, fetch_state, prev_safe_error)

            # If lastReceivedAt is unchanged, no new email has arrived —
            # gradually back off to reduce unnecessary API calls.
            current_last_received = fetch_state.get("last_received_at", "")
            if prev_last_received_at and current_last_received == prev_last_received_at:
                interval = max(interval, min(base_interval * 1.5, 2.0))
            prev_last_received_at = current_last_received
        except ReMailCredentialError as exc:
            if exc.code == "mailbox_auth_invalid":
                record_remail_credential_failure(mailbox, reason=exc.code)
            print(f"[remail poll stopped: {exc}]")
            raise
        except ReMailTransportError as exc:
            transport_failures += 1
            if tracker is not None and active_proxy:
                tracker.record(active_proxy, ok=False, error=exc.detail or str(exc))
            if transport_failures >= _remail_transport_attempts():
                raise ReMailTransportError(
                    email=getattr(mailbox, "email", ""),
                    proxy=active_proxy,
                    attempts=transport_failures,
                    detail=exc.detail or str(exc),
                ) from None
            if len(candidates) > 1:
                proxy_index = (proxy_index + 1) % len(candidates)
                active_proxy = candidates[proxy_index]
            wait = _remail_transport_backoff(transport_failures - 1)
            print(
                f"[remail poll transport retry {transport_failures}/{_remail_transport_attempts()} "
                f"proxy={redact_proxy_url(active_proxy, empty_placeholder='DIRECT')} wait={wait:.1f}s]"
            )
            time.sleep(min(wait, max(0.0, deadline - time.time())))
            continue
        except Exception as exc:
            print(f"[remail poll error: {exc}]")
        if dots:
            print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None
