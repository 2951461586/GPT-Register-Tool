"""Outlook IMAP XOAUTH2 adapter used by mailbox polling.

The mailbox module owns provider routing; this adapter owns only Outlook IMAP
folder discovery and RFC822-to-message normalization.
"""

import email
import html
import imaplib
import re
from datetime import datetime
from email.header import decode_header
from email.utils import formataddr, getaddresses
from email.utils import parsedate_to_datetime


OUTLOOK_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
DEFAULT_FOLDERS = ["INBOX", "Junk", "Junk Email", "Spam"]
DEFAULT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def mailbox_domain(mailbox):
    email_value = str(getattr(mailbox, "email", "") or "").strip().lower()
    return email_value.rsplit("@", 1)[1] if "@" in email_value else ""


def is_outlook_mailbox(mailbox):
    domain = mailbox_domain(mailbox)
    return domain in OUTLOOK_DOMAINS or getattr(mailbox, "provider", "") == "chatai"


def outlook_login_email(mailbox):
    """Return the Microsoft mailbox principal used for OAuth IMAP login.

    ChatGPT may register a ``+alias`` address, but Microsoft XOAUTH2 expects the
    owning mailbox address rather than that delivery alias.
    """
    explicit = str(getattr(mailbox, "login_email", "") or "").strip().lower()
    if explicit:
        return explicit
    value = str(getattr(mailbox, "email", "") or "").strip().lower()
    if "@" not in value:
        return value
    local, domain = value.rsplit("@", 1)
    if domain in OUTLOOK_DOMAINS and "+" in local:
        return local.split("+", 1)[0] + "@" + domain
    return value


def message_text_from_email_message(message):
    parts = []
    if message.is_multipart():
        iterable = message.walk()
    else:
        iterable = [message]
    for part in iterable:
        content_type = (part.get_content_type() or "").lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except Exception:
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/html":
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = html.unescape(text)
        parts.append(text)
    return "\n".join(parts)


def imap_message_received_ts(message):
    try:
        dt = parsedate_to_datetime(message.get("Date", ""))
        return int(dt.timestamp()) if dt else 0
    except Exception:
        return 0


def _decode_header_text(value):
    raw = str(value or "")
    if not raw:
        return ""
    try:
        parts = []
        for chunk, charset in decode_header(raw):
            if isinstance(chunk, bytes):
                encoding = charset or "utf-8"
                try:
                    parts.append(chunk.decode(encoding, errors="replace"))
                except Exception:
                    parts.append(chunk.decode("utf-8", errors="replace"))
            else:
                parts.append(str(chunk))
        return "".join(parts).strip()
    except Exception:
        return raw.strip()


def _decoded_from_header(value):
    raw = str(value or "")
    if not raw:
        return ""
    try:
        parsed = getaddresses([raw])
        if not parsed:
            return _decode_header_text(raw)
        name, address = parsed[0]
        decoded_name = _decode_header_text(name)
        address = str(address or "").strip()
        if decoded_name and address:
            return formataddr((decoded_name, address))
        return address or decoded_name or _decode_header_text(raw)
    except Exception:
        return _decode_header_text(raw)


def imap_message_to_graph_shape(folder, num, raw_bytes):
    msg = email.message_from_bytes(raw_bytes)
    body_text = message_text_from_email_message(msg)
    recipients = []
    for header in ("To", "Cc", "Bcc", "Delivered-To", "X-Original-To", "X-Forwarded-To"):
        recipients.extend(addr.lower() for addr in re.findall(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", msg.get(header, "")))
    to_recipients = [{"emailAddress": {"address": addr}} for addr in recipients]
    headers = [{"name": key, "value": value} for key, value in msg.items()]
    received_ts = imap_message_received_ts(msg)
    received_iso = datetime.fromtimestamp(received_ts).isoformat() if received_ts else ""
    from_text = _decoded_from_header(msg.get("From", ""))
    return {
        "id": f"imap:{folder}:{num.decode(errors='ignore') if isinstance(num, bytes) else num}",
        "message_id": msg.get("Message-ID", ""),
        "subject": _decode_header_text(msg.get("Subject", "")),
        "from": from_text,
        "bodyPreview": body_text[:1000],
        "body": {"content": body_text},
        "toRecipients": to_recipients,
        "ccRecipients": [],
        "bccRecipients": [],
        "internetMessageHeaders": headers,
        "receivedDateTime": received_iso,
        "_source": "outlook_imap",
        "_folder": folder,
    }


def discover_imap_folders(mail, configured=None):
    configured = configured or DEFAULT_FOLDERS
    picked = []
    try:
        typ, listing = mail.list()
        if typ == "OK":
            by_lower = {}
            for raw in listing or []:
                line = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                match = re.search(r'"([^"]+)"\s*$', line) or re.search(r"\s(\S+)\s*$", line)
                if match:
                    name = match.group(1).strip('"')
                    by_lower[name.lower()] = name
            for candidate in configured:
                real = by_lower.get(candidate.lower())
                if real and real not in picked:
                    picked.append(real)
            for key, value in by_lower.items():
                if any(token in key for token in ("junk", "spam", "bulk")) and value not in picked:
                    picked.append(value)
    except Exception:
        picked = []
    if "INBOX" not in picked:
        picked.insert(0, "INBOX")
    return picked or configured


def _imap_connect_and_auth(mailbox, token_fetcher):
    """Create a fresh IMAP4_SSL connection and authenticate via XOAUTH2.

    Returns (mail, auth_string) or raises.
    """
    token = token_fetcher(DEFAULT_IMAP_SCOPE)
    auth_string = f"user={outlook_login_email(mailbox)}\x01auth=Bearer {token}\x01\x01"
    mail = imaplib.IMAP4_SSL("outlook.office365.com", 993)
    typ, _ = mail.authenticate("XOAUTH2", lambda _: auth_string.encode())
    if typ != "OK":
        try:
            mail.logout()
        except Exception:
            pass
        raise RuntimeError("imap XOAUTH2 failed")
    return mail


def fetch_outlook_imap_messages(mailbox, token_fetcher, folders=None, limit=25):
    messages = []
    max_retries = 2  # total attempts: initial + 1 reconnect

    for attempt in range(1, max_retries + 1):
        try:
            mail = _imap_connect_and_auth(mailbox, token_fetcher)
        except Exception as exc:
            if attempt < max_retries:
                continue
            raise

        try:
            for folder in discover_imap_folders(mail, folders):
                try:
                    typ, _ = mail.select(f'"{folder}"', readonly=True)
                    if typ != "OK":
                        typ, _ = mail.select(folder, readonly=True)
                    if typ != "OK":
                        continue
                    typ, nums = mail.search(None, "ALL")
                    if typ != "OK" or not nums or not nums[0]:
                        continue
                    selected = nums[0].split()[-max(1, min(int(limit or 25), 50)):]
                    for num in reversed(selected):
                        typ, data = mail.fetch(num, "(RFC822)")
                        if typ != "OK" or not data:
                            continue
                        for item in data:
                            if isinstance(item, tuple) and item[1]:
                                messages.append(imap_message_to_graph_shape(folder, num, item[1]))
                                break
                        if len(messages) >= limit:
                            return messages
                except Exception as exc:
                    # Connection-level errors (e.g. "User is authenticated but
                    # not connected") mean the socket died — break out and retry
                    # with a fresh connection rather than silently skipping.
                    error_text = str(exc).lower()
                    if "not connected" in error_text or "connection" in error_text or "socket" in error_text:
                        if attempt < max_retries:
                            break
                        print(f"[outlook imap folder {folder} error: {exc}]")
                        continue
                    print(f"[outlook imap folder {folder} error: {exc}]")
                    continue
            else:
                # All folders processed without connection-level break
                break
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    if messages:
        print(f"[outlook imap] fetched {len(messages)} message(s)")
    return messages
