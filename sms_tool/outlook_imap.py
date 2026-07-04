"""Outlook IMAP XOAUTH2 adapter used by mailbox polling.

The mailbox module owns provider routing; this adapter owns only Outlook IMAP
folder discovery and RFC822-to-message normalization.
"""

import email
import html
import imaplib
import re
from datetime import datetime
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
    return {
        "id": f"imap:{folder}:{num.decode(errors='ignore') if isinstance(num, bytes) else num}",
        "message_id": msg.get("Message-ID", ""),
        "subject": msg.get("Subject", ""),
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


def fetch_outlook_imap_messages(mailbox, token_fetcher, folders=None, limit=25):
    token = token_fetcher(DEFAULT_IMAP_SCOPE)
    auth_string = f"user={mailbox.email}\x01auth=Bearer {token}\x01\x01"
    messages = []
    mail = imaplib.IMAP4_SSL("outlook.office365.com", 993)
    try:
        typ, _ = mail.authenticate("XOAUTH2", lambda _: auth_string.encode())
        if typ != "OK":
            raise RuntimeError("imap XOAUTH2 failed")
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
                print(f"[outlook imap folder {folder} error: {exc}]")
                continue
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    if messages:
        print(f"[outlook imap] fetched {len(messages)} message(s)")
    return messages
