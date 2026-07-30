"""chongzhi.art mailbox fetcher — uses the chongzhi.art API as a first-priority
email retrieval service, falling back to local Graph API / IMAP when it fails.

Credential format (same as chongzhi.art export):
    email--------password----client_id----refresh_token

The API only needs email + password; client_id and refresh_token are kept
for local fallback via MS Graph / IMAP.

API endpoint: POST https://www.chongzhi.art/api/mailbox/fetch
Request:  {"email": "...", "password": "...", "folder": "all|inbox|junk"}
Response: {"ok": true, "messages": [{"id", "from", "to", "subject", "date", "body", "otp"}, ...]}
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

from curl_cffi import requests as curl_requests

CHONGZHI_API_URL = "https://www.chongzhi.art/api/mailbox/fetch"
CHONGZHI_DEFAULT_TIMEOUT = 30
CHONGZHI_RATE_LIMIT_SECONDS = 32  # API enforces ~30s between calls per email

# Track last fetch time per email to respect rate limits
_last_fetch_ts: dict[str, float] = {}


def _chongzhi_cfg(email_cfg=None):
    """Extract chongzhi config from email_registration config."""
    if not isinstance(email_cfg, dict):
        return {}
    cfg = email_cfg.get("chongzhi") if isinstance(email_cfg.get("chongzhi"), dict) else {}
    return cfg


def _chongzhi_api_url(email_cfg=None):
    cfg = _chongzhi_cfg(email_cfg)
    return str(cfg.get("api_url") or CHONGZHI_API_URL).strip()


def _chongzhi_rate_limit(email_cfg=None):
    cfg = _chongzhi_cfg(email_cfg)
    try:
        return max(5, int(cfg.get("rate_limit_seconds", CHONGZHI_RATE_LIMIT_SECONDS)))
    except Exception:
        return CHONGZHI_RATE_LIMIT_SECONDS


def _chongzhi_timeout(email_cfg=None):
    cfg = _chongzhi_cfg(email_cfg)
    try:
        return max(10, int(cfg.get("timeout", CHONGZHI_DEFAULT_TIMEOUT)))
    except Exception:
        return CHONGZHI_DEFAULT_TIMEOUT


def chongzhi_enabled(email_cfg=None):
    """Check if chongzhi.art integration is enabled."""
    cfg = _chongzhi_cfg(email_cfg)
    enabled = cfg.get("enabled", True)
    if isinstance(enabled, str):
        return enabled.strip().lower() in {"1", "true", "yes", "on"}
    return bool(enabled)


def _parse_chongzhi_credential_line(line):
    """Parse a chongzhi.art credential line.

    Format: email--------password----client_id----refresh_token
    Returns: (email, password, client_id, refresh_token) or None
    """
    line = line.strip().lstrip("\ufeff")
    if not line or line.startswith("#"):
        return None

    # Split by -------- (8 dashes) first
    if "--------" in line:
        email_part, rest = line.split("--------", 1)
        email = email_part.strip().lower()
        parts = rest.split("----")
        password = parts[0].strip() if parts else ""
        client_id = parts[1].strip() if len(parts) > 1 else ""
        refresh_token = parts[2].strip() if len(parts) > 2 else ""
    else:
        # Fallback: try ---- (4 dashes) format like chatai
        parts = line.split("----")
        if len(parts) < 2:
            return None
        email = parts[0].strip().lower()
        password = parts[1].strip()
        client_id = parts[2].strip() if len(parts) > 2 else ""
        refresh_token = parts[3].strip() if len(parts) > 3 else ""

    # Validate email
    if not email or "@" not in email:
        return None

    return (email, password, client_id, refresh_token)


def parse_chongzhi_file(path):
    """Parse a chongzhi.art credential file into MailboxAccount records.

    Each line: email--------password----client_id----refresh_token
    """
    from pathlib import Path
    from .mailbox_types import MailboxAccount

    records = []
    cred_path = Path(path)
    if not cred_path.exists():
        return records

    for line_no, raw in enumerate(cred_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        parsed = _parse_chongzhi_credential_line(raw)
        if not parsed:
            if raw.strip() and not raw.strip().startswith("#"):
                print(f"[!] Skip malformed chongzhi line {cred_path}:{line_no}")
            continue

        email, password, client_id, refresh_token = parsed
        records.append(MailboxAccount(
            email=email,
            password=password,
            refresh_token=refresh_token,
            source=str(cred_path),
            provider="chongzhi",
            token=client_id,
        ))

    return records


def fetch_chongzhi_messages(email, password, folder="all", proxy=None, timeout=None, email_cfg=None):
    """Fetch messages from chongzhi.art API.

    Args:
        email: Email address
        password: Email password
        folder: "all", "inbox", or "junk"
        proxy: HTTP/SOCKS5 proxy URL
        timeout: Request timeout in seconds
        email_cfg: Email registration config dict

    Returns:
        List of message dicts with keys: id, from, to, subject, date, body, otp
        (normalized to match the format used by _fetch_mailbox_messages)
    """
    timeout = timeout or _chongzhi_timeout(email_cfg)
    rate_limit = _chongzhi_rate_limit(email_cfg)
    api_url = _chongzhi_api_url(email_cfg)

    # Respect rate limit
    email_key = email.strip().lower()
    now = time.time()
    last_ts = _last_fetch_ts.get(email_key, 0)
    wait = rate_limit - (now - last_ts)
    if wait > 0:
        time.sleep(wait)

    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        r = curl_requests.post(
            api_url,
            json={"email": email, "password": password, "folder": folder},
            headers={"Content-Type": "application/json"},
            proxies=proxies,
            timeout=timeout,
            impersonate="chrome124",
        )
        _last_fetch_ts[email_key] = time.time()

        try:
            body = r.json()
        except Exception:
            return []

        if not body.get("ok"):
            error = str(body.get("error") or "")
            # If rate limited, return empty (caller will fall back)
            if "频繁" in error or "429" in str(r.status_code):
                return []
            # If email not in service range, return empty (caller will fall back)
            if "不在" in error or "范围" in error:
                return []
            return []

        messages = body.get("messages") or []
        # Normalize to the format used by _fetch_mailbox_messages
        normalized = []
        for msg in messages:
            normalized.append({
                "id": str(msg.get("id") or ""),
                "from": {"emailAddress": {"address": _extract_email_address(msg.get("from") or ""), "name": msg.get("from") or ""}},
                "subject": str(msg.get("subject") or ""),
                "bodyPreview": str(msg.get("body") or "")[:200],
                "body": {"content": str(msg.get("body") or ""), "contentType": "text"},
                "receivedDateTime": _normalize_date(msg.get("date") or ""),
                "internetMessageId": str(msg.get("id") or ""),
                "toRecipients": [{"emailAddress": {"address": msg.get("to") or ""}}],
                "_chongzhi_otp": str(msg.get("otp") or ""),
            })
        return normalized

    except Exception as exc:
        print(f"[chongzhi fetch error: {exc}]")
        return []


def _extract_email_address(from_str):
    """Extract email address from 'Name <email>' format."""
    match = re.search(r"<([^>]+)>", from_str or "")
    return match.group(1) if match else from_str or ""


def _normalize_date(date_str):
    """Normalize various date formats to ISO 8601 for receivedDateTime."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        if dt:
            return dt.isoformat()
    except Exception:
        pass
    return date_str


def chongzhi_latest_otp(messages, keyword="", issued_after_unix=0):
    """Extract the latest OTP from chongzhi.art messages.

    chongzhi.art already extracts OTP in the 'otp' field, but we also
    run our own extraction as a fallback.

    Returns: OTP string or empty string
    """
    from .mail_otp import _extract_otp_from_text, _message_received_ts

    best_otp = ""
    best_ts = 0

    for msg in messages:
        recv_ts = _message_received_ts(msg)
        if issued_after_unix > 0 and recv_ts and recv_ts < issued_after_unix:
            continue

        # Prefer chongzhi's pre-extracted OTP
        otp = str(msg.get("_chongzhi_otp") or "").strip()
        if not otp:
            # Fall back to our own extraction
            subject = str(msg.get("subject") or "")
            body = str(msg.get("body", {}).get("content") or "")
            otp = _extract_otp_from_text(subject + " " + body)

        if otp and recv_ts >= best_ts:
            best_otp = otp
            best_ts = recv_ts

    return best_otp
