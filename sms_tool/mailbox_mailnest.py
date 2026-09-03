"""MailNest mailbox provider.

MailNest supports two mailbox shapes used by this project:

* Outlook Graph token accounts delivered as
  ``email----password----client_id----refresh_token``.  Those accounts use the
  existing Microsoft Graph path after parsing.
* MailNest API mailboxes, either bought from MailNest or uploaded as
  user-mailboxes, fetched through MailNest's REST API.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from curl_cffi import requests as curl_requests

from .config import CFG
from .mail_otp import _candidate_is_newer, _email_otp_candidate
from .mailbox_types import MailboxAccount


DEFAULT_BASE_URL = "https://mailnest.top"
DEFAULT_PROJECT_CODE = "chatgpt001"
API_PROVIDER = "mailnest"
GRAPH_PROVIDER = "mailnest_graph"
TEMPORARY_MODE = "temporary"
EXCLUSIVE_MODE = "exclusive"
USER_MAILBOX_MODE = "user_mailbox"
VALID_API_MODES = {TEMPORARY_MODE, EXCLUSIVE_MODE, USER_MAILBOX_MODE}
TRANSIENT_RECEIVE_CODES = {"D0005"}


class MailNestApiError(RuntimeError):
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = int(status_code or 0)
        self.body = body
        super().__init__(f"MailNest API {self.status_code}: {_safe_error(body)}")


def _email_cfg() -> Mapping[str, Any]:
    value = CFG.get("email_registration", {})
    return value if isinstance(value, Mapping) else {}


def _mailnest_cfg(email_cfg: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    cfg = email_cfg if isinstance(email_cfg, Mapping) else _email_cfg()
    nested = cfg.get("mailnest") if isinstance(cfg, Mapping) else {}
    return nested if isinstance(nested, Mapping) else {}


def _mailnest_api_key(email_cfg: Mapping[str, Any] | None = None) -> str:
    return str(os.environ.get("MAILNEST_API_KEY") or _mailnest_cfg(email_cfg).get("api_key") or "").strip()


def _mailnest_base_url(email_cfg: Mapping[str, Any] | None = None) -> str:
    return str(_mailnest_cfg(email_cfg).get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")


def _mailnest_timeout(email_cfg: Mapping[str, Any] | None = None) -> int:
    try:
        return max(1, int(_mailnest_cfg(email_cfg).get("timeout") or 30))
    except (TypeError, ValueError):
        return 30


def _mailnest_enabled(email_cfg: Mapping[str, Any] | None = None) -> bool:
    cfg = _mailnest_cfg(email_cfg)
    enabled = cfg.get("enabled")
    if enabled is not None and str(enabled).strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(_mailnest_api_key(email_cfg))


def _mailnest_mode(args: Any = None, email_cfg: Mapping[str, Any] | None = None) -> str:
    raw = (
        getattr(args, "mailnest_mode", None)
        or _mailnest_cfg(email_cfg).get("mode")
        or TEMPORARY_MODE
    )
    mode = str(raw or "").strip().lower().replace("-", "_")
    if mode in {"user", "private", "private_mailbox"}:
        mode = USER_MAILBOX_MODE
    if mode not in VALID_API_MODES:
        raise RuntimeError("mailnest.mode must be temporary, exclusive, or user_mailbox")
    return mode


def _mailnest_project_code(args: Any = None, email_cfg: Mapping[str, Any] | None = None) -> str:
    value = (
        getattr(args, "mailnest_project_code", None)
        or _mailnest_cfg(email_cfg).get("project_code")
        or DEFAULT_PROJECT_CODE
    )
    return str(value or "").strip()


def _create_mailnest_mailboxes(args: Any = None) -> list[MailboxAccount]:
    args = args or object()
    cfg = _email_cfg()
    mode = _mailnest_mode(args, cfg)
    count = max(1, min(int(getattr(args, "count", 1) or 1), 100))
    if mode == USER_MAILBOX_MODE:
        return _list_user_mailboxes(count=count, email_cfg=cfg)

    payload: dict[str, Any] = {"count": count}
    if mode == TEMPORARY_MODE:
        project_code = _mailnest_project_code(args, cfg)
        if not project_code:
            raise RuntimeError("mailnest.project_code is required for temporary mailbox purchases")
        payload["project_code"] = project_code
        path = "/api/v1/email/temporary/buy"
    else:
        path = "/api/v1/email/exclusive/buy"
    body = _mailnest_request("POST", path, json_body=payload, email_cfg=cfg)
    items = _as_items(body.get("data") if isinstance(body, dict) else body)
    accounts = [_account_from_api_item(item, source=f"mailnest_{mode}", mode=mode) for item in items]
    accounts = [account for account in accounts if account is not None]
    if not accounts:
        raise RuntimeError("MailNest mailbox purchase returned no usable mailboxes")
    return accounts[:count]


def _list_user_mailboxes(count: int, email_cfg: Mapping[str, Any] | None = None) -> list[MailboxAccount]:
    body = _mailnest_request(
        "GET",
        f"/api/v1/email/user-mailbox?page=1&page_size={max(1, min(count, 100))}",
        email_cfg=email_cfg,
    )
    data = body.get("data") if isinstance(body, dict) else {}
    items = data.get("items") if isinstance(data, dict) else data
    accounts = [
        _account_from_api_item(item, source="mailnest_user_mailbox", mode=USER_MAILBOX_MODE)
        for item in _as_items(items)
    ]
    return [account for account in accounts if account is not None][:count]


def _fetch_mailnest_messages(
    mailbox: MailboxAccount,
    *,
    limit: int = 25,
    proxy: str | None = None,
    include_body: bool = False,
    email_cfg: Mapping[str, Any] | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    if not email:
        return []
    mode = _normalize_api_mode(getattr(mailbox, "auth_mode", "") or _mailnest_mode(email_cfg=email_cfg))
    path = "/api/v1/email/user-mailbox/receive" if mode == USER_MAILBOX_MODE else "/api/v1/email/receive"
    try:
        body = _mailnest_request(
            "POST",
            path,
            json_body={"email": email},
            email_cfg=email_cfg,
            proxy=proxy,
            transient_codes=TRANSIENT_RECEIVE_CODES,
        )
    except MailNestApiError as exc:
        if _mailnest_error_code(exc.body) in TRANSIENT_RECEIVE_CODES:
            return []
        raise
    data = body.get("data") if isinstance(body, dict) else body
    messages = [_normalize_mailnest_message(item, email=email) for item in _as_items(data)]
    return messages[: max(1, min(int(limit or 25), 100))]


def _latest_mailnest_otp_candidate(
    mailbox: MailboxAccount,
    *,
    keyword: str = "",
    issued_after_unix: int = 0,
    proxy: str | None = None,
    email_cfg: Mapping[str, Any] | None = None,
    excluded_otps: Any = None,
) -> dict[str, Any] | None:
    excluded = {str(value or "").strip() for value in (excluded_otps or ())}
    latest = None
    for msg in _fetch_mailnest_messages(mailbox, proxy=proxy, email_cfg=email_cfg):
        candidate = _email_otp_candidate(mailbox, msg, keyword=keyword, issued_after_unix=issued_after_unix)
        if not candidate or str(candidate.get("otp") or "").strip() in excluded:
            continue
        if _candidate_is_newer(candidate, latest):
            latest = candidate
    return latest


def _poll_mailnest_otp(
    mailbox: MailboxAccount,
    *,
    subject_keyword: str = "",
    timeout: int = 300,
    issued_after_unix: int = 0,
    proxy: str | None = None,
    excluded_otps: Any = None,
    email_cfg: Mapping[str, Any] | None = None,
    **_: Any,
) -> str | None:
    from .mailbox import _email_otp_settle_seconds, _otp_poll_interval
    from .mailbox_poll import _poll_otp_with_settle

    keyword = str(subject_keyword or "").lower()

    def fetch_candidate() -> dict[str, Any] | None:
        return _latest_mailnest_otp_candidate(
            mailbox,
            keyword=keyword,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            email_cfg=email_cfg,
            excluded_otps=excluded_otps,
        )

    return _poll_otp_with_settle(
        fetch_candidate,
        timeout=timeout,
        interval=_otp_poll_interval(),
        settle_seconds=_email_otp_settle_seconds(),
        excluded_otps=excluded_otps,
        log_prefix="mailnest poll",
        is_newer=_candidate_is_newer,
    )


def _mailnest_request(
    method: str,
    path: str,
    *,
    json_body: Mapping[str, Any] | None = None,
    email_cfg: Mapping[str, Any] | None = None,
    proxy: str | None = None,
    transient_codes: set[str] | None = None,
) -> dict[str, Any]:
    base_url = _mailnest_base_url(email_cfg)
    api_key = _mailnest_api_key(email_cfg)
    if not base_url:
        raise RuntimeError("mailnest.base_url is required")
    if not api_key:
        raise RuntimeError("mailnest.api_key is required")
    url = base_url + (path if path.startswith("/") else "/" + path)
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + api_key,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = _mailnest_timeout(email_cfg)
    method = method.upper()
    if method == "POST":
        response = curl_requests.post(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json=dict(json_body or {}),
            proxies=proxies,
            impersonate="chrome124",
            timeout=timeout,
        )
    elif method == "GET":
        response = curl_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            impersonate="chrome124",
            timeout=timeout,
        )
    else:
        raise ValueError(f"unsupported MailNest method: {method}")
    try:
        body = response.json()
    except Exception:
        body = {"raw": str(getattr(response, "text", ""))[:500]}
    if response.status_code != 200:
        raise MailNestApiError(response.status_code, body)
    code = _mailnest_error_code(body)
    if code and code != "00000":
        if transient_codes and code in transient_codes:
            return body if isinstance(body, dict) else {"data": None}
        raise MailNestApiError(response.status_code, body)
    return body if isinstance(body, dict) else {"data": body}


def _account_from_api_item(item: Any, *, source: str, mode: str) -> MailboxAccount | None:
    if not isinstance(item, Mapping):
        return None
    email = str(item.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    mailbox_id = str(item.get("id") or item.get("order_id") or "").strip()
    return MailboxAccount(
        email=email,
        password=str(item.get("password") or ""),
        refresh_token=str(item.get("refresh_token") or ""),
        source=json.dumps(dict(item), ensure_ascii=False, separators=(",", ":")),
        provider=API_PROVIDER,
        token=mailbox_id,
        order_no=mailbox_id,
        purchase_id=mailbox_id,
        auth_mode=_normalize_api_mode(mode),
        project_name=str(item.get("project_name") or item.get("project_code") or ""),
        price=str(item.get("price") or ""),
    )


def _normalize_mailnest_message(msg: Any, *, email: str) -> dict[str, Any]:
    if not isinstance(msg, Mapping):
        msg = {"body": str(msg or "")}
    code = str(msg.get("code_match") or "").strip()
    body = str(msg.get("body") or msg.get("body_preview") or msg.get("bodyPreview") or "")
    if code and code not in body:
        body = f"{code}\n{body}"
    body_preview = str(msg.get("body_preview") or msg.get("bodyPreview") or body)[:500]
    if code and code not in body_preview:
        body_preview = f"{code} {body_preview}".strip()[:500]
    from_email = str(msg.get("from_email") or msg.get("from_domain") or "")
    return {
        "id": str(msg.get("id") or msg.get("message_id") or ""),
        "receivedDateTime": str(msg.get("received_at") or msg.get("receivedDateTime") or ""),
        "from": {"emailAddress": {"address": from_email}},
        "sender": {"emailAddress": {"address": from_email}},
        "subject": str(msg.get("subject") or ""),
        "bodyPreview": body_preview,
        "body": {"content": body},
        "toRecipients": [{"emailAddress": {"address": str(msg.get("to_email") or email)}}],
    }


def _normalize_api_mode(value: Any) -> str:
    mode = str(value or TEMPORARY_MODE).strip().lower().replace("-", "_")
    if mode in {"user", "private", "private_mailbox"}:
        return USER_MAILBOX_MODE
    if mode in VALID_API_MODES:
        return mode
    return TEMPORARY_MODE


def _mailnest_error_code(body: Any) -> str:
    return str(body.get("code") or "").strip() if isinstance(body, Mapping) else ""


def _as_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        items = value.get("items")
        if isinstance(items, list):
            return items
        data = value.get("data")
        if isinstance(data, list):
            return data
    return []


def _safe_error(body: Any) -> str:
    if isinstance(body, str):
        return body[:500]
    try:
        return json.dumps(body, ensure_ascii=False, default=str)[:500]
    except Exception:
        return str(body)[:500]
