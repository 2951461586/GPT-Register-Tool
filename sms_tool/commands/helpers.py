"""Shared CLI helper functions extracted from cli.py.

These are pure utility functions that don't depend on cli.CFG.
Functions that need CFG remain in cli.py so test patches work correctly.
"""

from __future__ import annotations

import os
from email.utils import formataddr


def read_email_file(path):
    """Read one email per line from a file, skipping blanks and comments."""
    if not path:
        return []
    if not os.path.exists(path):
        print(f"[Error] --email-file not found: {path}")
        raise SystemExit(2)
    emails = []
    seen = set()
    with open(path, "r", encoding="utf-8-sig") as handle:
        for raw in handle:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            email = value.split()[0].strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            emails.append(email)
    return emails


def unique_emails(emails):
    """Deduplicate email list, preserving order."""
    output = []
    seen = set()
    for email in emails or []:
        value = str(email or "").strip().lower()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def payment_method(args):
    """Normalize payment method from args."""
    from ..payment_link_manager import normalize_payment_method

    return normalize_payment_method(getattr(args, "payment_method", "")) or "paypal"


def payment_method_label(payment_method):
    """Human-readable payment method label."""
    from ..payment_link_manager import payment_method_label as label

    return label(payment_method) or "PayPal"


def public_mail_message(msg):
    """Normalize a mailbox message dict for JSON output."""
    msg = msg if isinstance(msg, dict) else {}
    from_value = msg.get("from")
    if isinstance(from_value, dict):
        email_address = (from_value.get("emailAddress") or {}) if isinstance(from_value.get("emailAddress"), dict) else {}
        sender_name = str(email_address.get("name") or from_value.get("name") or "").strip()
        sender_address = str(email_address.get("address") or from_value.get("address") or "").strip()
        from_value = formataddr((sender_name, sender_address)) if sender_name and sender_address else (sender_address or sender_name)
    body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
    recipients = msg.get("toRecipients") if isinstance(msg.get("toRecipients"), list) else []
    recipient = ""
    if recipients and isinstance(recipients[0], dict):
        email_address = recipients[0].get("emailAddress")
        if isinstance(email_address, dict):
            recipient = str(email_address.get("address") or "").strip()
    return {
        "id": str(msg.get("id") or msg.get("message_id") or ""),
        "receivedDateTime": str(msg.get("receivedDateTime") or msg.get("received_at") or msg.get("created_at") or ""),
        "from": str(from_value or msg.get("from_email") or msg.get("sender") or ""),
        "recipient": recipient or str(msg.get("recipient") or msg.get("to") or ""),
        "subject": str(msg.get("subject") or msg.get("title") or ""),
        "bodyPreview": str(msg.get("bodyPreview") or msg.get("preview") or body.get("content") or msg.get("text") or "")[:2000],
        "body": str(body.get("content") or msg.get("text") or msg.get("bodyPreview") or ""),
        "verificationCode": str(msg.get("verificationCode") or ""),
    }


def public_oauth_result(result):
    """Sanitize OAuth result for JSON output (strip raw tokens)."""
    if not isinstance(result, dict):
        return {}
    output = {key: value for key, value in result.items() if key != "tokens"}
    tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
    if tokens:
        output["has_access_token"] = bool(tokens.get("access_token"))
        output["has_refresh_token"] = bool(tokens.get("refresh_token"))
    return output


def mailbox_from_explicit_args(args):
    """Load a specific mailbox from explicit CLI args."""
    if not (getattr(args, "chatai_mailbox_file", None) or getattr(args, "mailbox_file", None)):
        return None
    from ..mailbox import _load_mailbox_pool

    requested = str(getattr(args, "email", "") or "").strip().lower()
    mailboxes = _load_mailbox_pool(args)
    if not mailboxes:
        return None
    if requested:
        for mailbox in mailboxes:
            if str(getattr(mailbox, "email", "") or "").strip().lower() == requested:
                return mailbox
    return mailboxes[0]


def one_click_sms_max_reuse(args) -> int:
    """One-click SMS forces max_reuse_count=1."""
    requested = int(getattr(args, "max_reuse_count", 0) or 0)
    if requested and requested != 1:
        print("[*] One-click SMS forces max_reuse_count=1 so each email account gets its own phone number")
    return 1
