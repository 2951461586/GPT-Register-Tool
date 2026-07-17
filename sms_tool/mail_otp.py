import re
from datetime import datetime

OTP_RE = re.compile(r"(^|[^0-9])([0-9]{6})([^0-9]|$)")
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
OUTLOOK_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
OPENAI_OTP_SENDER_MARKERS = ("openai.com", "chatgpt.com", "auth.openai", "tm.openai", "tm.open")
BAD_OTP_SENDER_MARKERS = ("tm1.openai.com", "tm1.openai")
OTP_CONTEXT_MARKERS = ("code", "verification", "verify", "openai", "chatgpt", "login", "验证码", "驗證碼")
FAKE_OTP_CONTEXT_MARKERS = (
    "tracking",
    "track id",
    "message id",
    "ticket",
    "utm",
    "unsubscribe",
    "pixel",
    "color",
    "background",
    "border",
    "font-size",
    "padding",
    "margin",
    "radius",
    "hex",
    "rgb",
    "rgba",
    "received",
    "arc-",
    "dkim",
    "spf",
    "content-type",
    "mime",
    "boundary",
    "for <",
    "m=+",
    "timestamp",
    "message-id",
)

def _extract_otp_from_text(text):
    text = text or ""
    candidates = []
    for match in OTP_RE.finditer(text):
        code = match.group(2)
        start, end = match.span(2)
        # Skip numbers that are part of an email address
        around = text[max(0, start - 3):min(len(text), end + 3)]
        if re.search(r"[a-z0-9._%+-]\.?\d{6}@|@.*\d{6}[a-z0-9._-]", around, re.I):
            continue
        # Skip if preceded/followed by hex-ish chars (part of longer token)
        char_before = text[start - 1] if start > 0 else ""
        char_after = text[end] if end < len(text) else ""
        if char_before and re.match(r"[a-fA-F0-9-]", char_before):
            continue
        if char_after and re.match(r"[a-fA-F0-9-]", char_after):
            continue
        before = text[max(0, start - 2):start]
        after = text[end:end + 2]
        context = text[max(0, start - 80):min(len(text), end + 80)]
        context_lc = context.lower()
        before_context = text[max(0, start - 60):start].lower()
        if any(marker in before_context for marker in FAKE_OTP_CONTEXT_MARKERS) and not any(marker in before_context for marker in OTP_CONTEXT_MARKERS):
            continue
        if _looks_fake_otp_context(context_lc):
            continue
        if text[max(0, start - 1):end].startswith("#"):
            continue
        if re.search(r"(?i)(color|background|border|rgb|rgba|font)[^\n]{0,20}#?" + re.escape(code), context):
            continue
        # Only reject CSS-like dimensions when the unit is attached to the
        # number.  CFWorker-extracted OTP JSON commonly looks like
        # {"value":"453831","remark":"ChatGPT OTP"}; the older broad
        # ``.{0,40}(px|em|rem|%)`` check matched the ``em`` in "remark" and
        # incorrectly discarded the real OTP.
        if re.search(r"(?i)" + re.escape(code) + r"\s*(px|em|rem|%)\b", context):
            continue
        score = 0
        if any(k in context_lc for k in ("code", "verification", "verify", "openai", "chatgpt", "login", "验证码", "驗證碼")):
            score += 10
        if any(k in context_lc for k in ("your", "is", "use", "enter", "sign")):
            score += 2
        if before.strip() or after.strip():
            score += 1
        candidates.append((score, start, code))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _message_id(msg):
    msg = msg or {}
    return str(msg.get("id") or msg.get("message_id") or "").strip()


def _message_received_ts(msg):
    value = str((msg or {}).get("receivedDateTime") or "")
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _email_otp_candidate(mailbox, msg, keyword="", issued_after_unix=0):
    if issued_after_unix > 0:
        recv_ts = _message_received_ts(msg)
        if recv_ts and recv_ts < issued_after_unix:
            return None
    subject = str((msg or {}).get("subject") or "")
    sender = _message_sender(msg)
    if _bad_otp_sender(sender):
        return None
    if _requires_openai_sender_filter(mailbox) and _sender_has_address(sender) and not _allowed_otp_sender(sender):
        return None
    if keyword:
        subject_lc = subject.lower()
        keywords = [part.strip().lower() for part in str(keyword).split("|") if part.strip()]
        if keywords and not any(part in subject_lc for part in keywords):
            return None
    recipients = _message_recipients(msg)
    if recipients and not _recipient_matches_mailbox(getattr(mailbox, "email", ""), recipients):
        return None
    body = subject + "\n"
    body += str((msg or {}).get("bodyPreview") or "") + "\n"
    body += str((((msg or {}).get("body") or {}).get("content")) or "")
    if _message_looks_fake_otp_noise(body):
        return None
    otp = _extract_otp_from_text(body)
    if not otp:
        return None
    return {
        "otp": otp,
        "id": _message_id(msg),
        "received_ts": _message_received_ts(msg),
    }


def _candidate_is_newer(candidate, current):
    if not candidate:
        return False
    if not current:
        return True
    candidate_ts = int(candidate.get("received_ts") or 0)
    current_ts = int(current.get("received_ts") or 0)
    if candidate_ts and current_ts:
        return candidate_ts > current_ts
    candidate_id = str(candidate.get("id") or "")
    current_id = str(current.get("id") or "")
    return bool(candidate_id and candidate_id != current_id)


def _message_sender(msg):
    values = [str((msg or {}).get("from") or "")]
    sender = (msg or {}).get("sender")
    if isinstance(sender, dict):
        values.append(str(((sender.get("emailAddress") or {}).get("address")) or ""))
    for header in (msg or {}).get("internetMessageHeaders") or []:
        name = str((header or {}).get("name") or "").strip().lower()
        if name in {"from", "sender", "return-path", "reply-to"}:
            values.append(str((header or {}).get("value") or ""))
    return " ".join(values).lower()


def _sender_has_address(sender_text):
    return bool(re.search(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", str(sender_text or "")))


def _allowed_otp_sender(sender_text):
    text = str(sender_text or "").lower()
    return any(marker in text for marker in OPENAI_OTP_SENDER_MARKERS)


def _bad_otp_sender(sender_text):
    text = str(sender_text or "").lower()
    if re.search(r"\bbounces?\+[^\s@]+@tm1\.openai\.com\b", text):
        return False
    return any(marker in text for marker in BAD_OTP_SENDER_MARKERS)


def _looks_fake_otp_context(lowered_text):
    lowered = str(lowered_text or "").lower()
    if any(marker in lowered for marker in OTP_CONTEXT_MARKERS):
        return False
    return any(marker in lowered for marker in FAKE_OTP_CONTEXT_MARKERS)


def _message_looks_fake_otp_noise(text):
    return _looks_fake_otp_context(str(text or "").lower())

def _message_recipients(msg):
    recipients = []
    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        for item in msg.get(key) or []:
            address = (((item or {}).get("emailAddress") or {}).get("address") or "").strip().lower()
            if address:
                recipients.append(address)
    for header in msg.get("internetMessageHeaders") or []:
        name = str((header or {}).get("name") or "").strip().lower()
        value = str((header or {}).get("value") or "")
        if name in {"to", "cc", "bcc", "delivered-to", "x-original-to", "x-forwarded-to"}:
            recipients.extend(addr.lower() for addr in re.findall(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", value))
    return set(recipients)


def _recipient_matches_mailbox(mailbox_email, recipients):
    mailbox_key = _canonical_mailbox_email(mailbox_email)
    if not mailbox_key:
        return False
    for recipient in recipients or []:
        if _canonical_mailbox_email(recipient) == mailbox_key:
            return True
    return False


def _canonical_mailbox_email(value):
    text = str(value or "").strip().lower()
    if "@" not in text:
        return ""
    local, domain = text.rsplit("@", 1)
    if not local or not domain:
        return ""
    return f"{local}@{domain}"


def _requires_openai_sender_filter(mailbox):
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    domain = email.rsplit("@", 1)[1] if "@" in email else ""
    return provider in {"gmail", "graph", "chatai", "outlook"} or domain in GMAIL_DOMAINS or domain in OUTLOOK_DOMAINS
