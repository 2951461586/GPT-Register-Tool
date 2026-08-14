"""Smailr temporary mailbox provider.

``smailr_mailbox`` implements the parts of the Smailr REST API needed for
one-shot email verification (create mailbox → poll for OTP message → fetch
body → extract code).  Higher-level message-fetch / OTP-poll plumbing is
exposed via the helpers at the bottom of this module; the strategy registry
in :mod:`mailbox_strategies` picks them up automatically on import.

Smailr REST surface used here::

    GET  /api/v1/mailboxes                list current mailboxes
    POST /api/v1/mailboxes                create a temporary mailbox
    GET  /api/v1/mailboxes/{id}           mailbox metadata (email address)
    GET  /api/v1/mailboxes/{mbId}/mails   list received mail (paged)
    GET  /api/v1/mails/{id}               fetch one mail's full body

Configure via ``email_registration.smailr.*`` in ``config.json``.  Keep the
API key outside source control (``SMAILR_API_KEY`` is preferred)::

    {
      "email_registration": {
        "smailr": {
          "api_key": "",
          "base_url": "https://smailr.com",
          "domains": ["smailr.com", "loc.cc", "mail.nodeloc.cc", "nodeloc.cc"],
          "default_domain": "smailr.com",
          "timeout": 30
        }
      }
    }
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

from curl_cffi import requests as curl_requests

from ..phone_proxy import normalize_proxy_url, redact_proxy_text as _redact_proxy_text


DEFAULT_BASE_URL = "https://smailr.com"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
_RETRYABLE_CONNECT_ERROR_CODES = frozenset({5, 6, 7, 35})


def _normalize_base_url(value: str) -> str:
    """Accept either the site URL or the OpenAPI server URL."""
    base = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    while base.lower().endswith("/api/v1"):
        base = base[:-7].rstrip("/")
    return base or DEFAULT_BASE_URL


def _redact_secret(value: Any, secret: str) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "<redacted>")
    if isinstance(value, dict):
        return {key: _redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    return value


class SmailrError(RuntimeError):
    """Raised for Smailr API errors (non-2xx, HTTP-level, malformed JSON)."""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SmailrClient:
    """Low-level Smailr REST client.

    Client code should prefer the functional helpers in this module
    (``create_mailbox``, ``fetch_messages``, ``poll_otp``) — they raise
    :class:`MailboxAccountSmailr`-compatible errors and redact secrets.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        proxy: str | None = None,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ):
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise RuntimeError("smailr.api_key is required")
        self.base_url = _normalize_base_url(base_url)
        self.timeout = max(1, int(timeout or DEFAULT_TIMEOUT))
        self.proxy = normalize_proxy_url(proxy)
        self._proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.retry_attempts = max(1, int(retry_attempts or DEFAULT_RETRY_ATTEMPTS))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    # ── REST verbs ──────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        kwargs: dict[str, Any] = {
            "headers": self._headers(),
            "timeout": self.timeout,
            "impersonate": "chrome124",
        }
        if self._proxies:
            kwargs["proxies"] = self._proxies
        if json_body is not None:
            kwargs["json"] = json_body
        for attempt in range(self.retry_attempts):
            try:
                response = curl_requests.request(method, url, **kwargs)
                break
            except Exception as exc:
                error_code = int(getattr(exc, "code", 0) or 0)
                should_retry = (
                    error_code in _RETRYABLE_CONNECT_ERROR_CODES
                    and attempt + 1 < self.retry_attempts
                )
                if should_retry:
                    time.sleep(self.retry_backoff_seconds * (2 ** attempt))
                    continue
                message = _redact_secret(_redact_proxy_text(exc, self.proxy), self.api_key)
                raise SmailrError(f"smailr request failed: {message}", body=message) from exc
        if response.status_code < 200 or response.status_code >= 300:
            body: Any
            try:
                body = response.json()
            except Exception:
                body = response.text[:500]
            safe_body = _redact_secret(body, self.api_key)
            raise SmailrError(
                f"smailr {method} {path} -> {response.status_code}: {safe_body}",
                status_code=response.status_code,
                body=safe_body,
            )
        try:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        except Exception:
            return response.text

    # ── Mailbox CRUD ────────────────────────────────────────────────────────

    def list_mailboxes(self) -> list[dict]:
        data = _unwrap_data(self._request("GET", "/api/v1/mailboxes") or [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "mailboxes", "value", "items"):
                sub = data.get(key)
                if isinstance(sub, list):
                    return sub
        return []

    def create_mailbox(self, local_part: str, domain_id: str = "") -> dict:
        payload: dict[str, str] = {"local_part": str(local_part or "").strip()}
        if domain_id:
            payload["domain_id"] = str(domain_id).strip()
        data = _unwrap_data(self._request("POST", "/api/v1/mailboxes", json_body=payload) or {})
        if not isinstance(data, dict):
            data = {"raw": data}
        return data

    def mailbox_detail(self, mailbox_id: str) -> dict:
        data = _unwrap_data(self._request("GET", f"/api/v1/mailboxes/{mailbox_id}") or {})
        return data if isinstance(data, dict) else {"raw": data}

    # ── Mail access ─────────────────────────────────────────────────────────

    def list_mails(self, mailbox_id: str, folder: str = "INBOX", page: int = 1, per_page: int = 25) -> list[dict]:
        params = urllib.parse.urlencode({"folder": folder, "page": int(page), "per_page": int(per_page)})
        data = _unwrap_data(self._request("GET", f"/api/v1/mailboxes/{mailbox_id}/mails?{params}") or [])
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ("data", "mails", "items", "value"):
                sub = data.get(key)
                if isinstance(sub, list):
                    items = sub
                    break
        return items

    def mail_detail(self, mail_id: str) -> dict:
        data = _unwrap_data(self._request("GET", f"/api/v1/mails/{mail_id}") or {})
        return data if isinstance(data, dict) else {"raw": data}


# ── Response shaping ──────────────────────────────────────────────────────────

def _mailbox_email(mb: dict) -> str:
    if not isinstance(mb, dict):
        return ""
    candidates = [
        mb.get("email"),
        mb.get("address"),
        mb.get("local_part"),
    ]
    for value in candidates:
        value = str(value or "").strip()
        if "@" in value:
            return value.lower()
    local = str(mb.get("local_part") or "").strip().lower()
    domain = str(mb.get("domain") or mb.get("domain_name") or "").strip().lower().lstrip("@")
    if local and domain:
        return f"{local}@{domain}"
    return ""


def _unwrap_data(value: Any) -> Any:
    """Unwrap common OpenAPI response envelopes without assuming one shape."""
    if not isinstance(value, dict):
        return value
    nested = value.get("data")
    return nested if isinstance(nested, (dict, list)) else value


def _mailbox_id(mb: dict) -> str:
    if not isinstance(mb, dict):
        return ""
    return str(mb.get("id") or mb.get("mailbox_id") or mb.get("uuid") or "").strip()


def _format_received(value: Any) -> str:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        except Exception:
            return str(value)
    text = str(value or "")
    if text.isdigit():
        return _format_received(float(text))
    return text


def _normalize_message(raw: dict, mailbox_email: str = "") -> dict:
    """Shape any Smailr mail object into the Graph-API-flavoured envelope used
    by :mod:`mail_otp` helpers (``_email_otp_candidate``/``_message_id``).
    """
    if not isinstance(raw, dict):
        raw = {"body": str(raw or "")}

    def _first(*keys: str) -> Any:
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                return value
        return ""

    subject = str(_first("subject", "title") or "")
    body_raw = _first("body", "body_html", "body_text", "content", "html", "text")
    body_text = str(body_raw or "")
    sender = _first("from", "sender", "from_email", "sender_email", "from_address", "from_addr")
    if isinstance(sender, dict):
        sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or ""
    recipients: list[str] = []
    for recipient_key in ("to", "toRecipients", "recipients", "to_addrs"):
        raw_list = raw.get(recipient_key)
        if not raw_list:
            continue
        if isinstance(raw_list, dict):
            raw_list = [raw_list]
        if not isinstance(raw_list, list):
            raw_list = [raw_list]
        for item in raw_list:
            if isinstance(item, dict):
                address = (
                    item.get("address")
                    or (item.get("emailAddress") or {}).get("address")
                    or item.get("email")
                )
            else:
                address = item
            address = str(address or "").strip().lower()
            if address and address not in recipients:
                recipients.append(address)

    body = raw.get("body")
    if isinstance(body, dict):
        nested = body.get("content") or body.get("text") or body_text
        body_text = str(nested or body_text)

    received = str(_format_received(_first(
        "received_at", "receivedAt", "received_datetime",
        "created_at", "createdAt", "date", "timestamp",
        "sent_at", "sentAt",
    )))

    return {
        "id": str(_first("id", "mail_id", "message_id") or ""),
        "receivedDateTime": received,
        "from": {"emailAddress": {"address": str(sender)}},
        "subject": subject,
        "bodyPreview": body_text[:500],
        "body": {"content": body_text},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
        "_mailbox_email": (mailbox_email or "").strip().lower(),
    }


# ── OTP polling ──────────────────────────────────────────────────────────────

def fetch_messages(client: SmailrClient, mailbox_id: str, mailbox_email: str, limit: int = 25) -> list[dict]:
    """Fetch up to *limit* mails and shape them with ``_normalize_message``."""
    limit = max(1, min(int(limit or 25), 100))
    combined: list[dict] = []
    page = 1
    while len(combined) < limit:
        items = client.list_mails(mailbox_id, page=page, per_page=max(25, limit))
        if not items:
            break
        for item in items:
            # The list endpoint may return only headers.  Fetch the detail so
            # OTP extraction still works when the body is missing or is the
            # provider's 200-character preview instead of the full message.
            body_value = ""
            if isinstance(item, dict):
                for key in ("body", "body_text", "body_html", "content", "text", "html"):
                    value = item.get(key)
                    if value:
                        body_value = value.get("content") if isinstance(value, dict) else value
                        break
            needs_detail = not body_value or len(str(body_value)) <= 500
            if isinstance(item, dict) and needs_detail:
                message_id = str(item.get("id") or item.get("mail_id") or item.get("message_id") or "").strip()
                if message_id:
                    try:
                        detail = client.mail_detail(message_id)
                        detail = _unwrap_data(detail)
                        if isinstance(detail, dict):
                            item = {**item, **detail}
                    except Exception:
                        pass
            combined.append(_normalize_message(item, mailbox_email=mailbox_email))
        if len(items) < limit:
            break
        page += 1
    return combined[:limit]


def poll_otp(
    client: SmailrClient,
    mailbox_id: str,
    mailbox_email: str,
    *,
    subject_keyword: str = "",
    timeout: int = 300,
    issued_after_unix: int = 0,
    excluded_otps: Any = None,
    log_prefix: str = "smailr",
) -> str | None:
    """Settle-stability OTP polling."""
    from ..mailbox_poll import _poll_otp_with_settle
    from ..mail_otp import _email_otp_candidate

    keyword = (subject_keyword or "").lower()
    excluded = {str(value or "").strip() for value in (excluded_otps or ())}

    def _fetch_candidate():
        for msg in fetch_messages(client, mailbox_id, mailbox_email, limit=10):
            candidate = _email_otp_candidate(
                type("FakeMB", (), {"email": mailbox_email, "provider": "smailr"})(),
                msg,
                keyword=keyword,
                issued_after_unix=issued_after_unix,
            )
            if not candidate and keyword:
                # Smailr currently stores some CJK OpenAI subjects as mojibake.
                # Keep timestamp/sender/recipient checks, but allow the body OTP
                # parser to decide when the subject cannot carry the keyword.
                candidate = _email_otp_candidate(
                    type("FakeMB", (), {"email": mailbox_email, "provider": "smailr"})(),
                    msg,
                    keyword="",
                    issued_after_unix=issued_after_unix,
                )
            if candidate and candidate.get("otp") not in excluded:
                return candidate
        return None

    def _is_newer(a, b) -> bool:
        if a is None:
            return False
        if b is None:
            return True
        return (a or {}).get("id") != (b or {}).get("id")

    return _poll_otp_with_settle(
        _fetch_candidate,
        timeout=timeout,
        interval=2.0,
        settle_seconds=1.5,
        excluded_otps=excluded,
        log_prefix=log_prefix,
        is_newer=_is_newer,
    )
