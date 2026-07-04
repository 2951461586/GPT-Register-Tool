import re
from datetime import datetime

OTP_RE = re.compile(r"(^|[^0-9])([0-9]{6})([^0-9]|$)")

def _extract_otp_from_text(text):
    text = text or ""
    candidates = []
    for match in OTP_RE.finditer(text):
        code = match.group(2)
        start, end = match.span(2)
        before = text[max(0, start - 2):start]
        after = text[end:end + 2]
        context = text[max(0, start - 80):min(len(text), end + 80)]
        context_lc = context.lower()
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
    if keyword:
        subject_lc = subject.lower()
        keywords = [part.strip().lower() for part in str(keyword).split("|") if part.strip()]
        if keywords and not any(part in subject_lc for part in keywords):
            return None
    recipients = _message_recipients(msg)
    if mailbox.email.lower() not in recipients and recipients:
        return None
    body = subject + "\n"
    body += str((msg or {}).get("bodyPreview") or "") + "\n"
    body += str((((msg or {}).get("body") or {}).get("content")) or "")
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

