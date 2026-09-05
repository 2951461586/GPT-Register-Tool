import re
from pathlib import Path

from .mailbox_types import MailboxAccount
from .providers import mailbox_icloud_url

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MS_CLIENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
KNOWN_EMAIL_DOMAINS = (
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "gmail.com",
    "googlemail.com",
)


def _looks_ms_client_id(value):
    return bool(MS_CLIENT_ID_RE.fullmatch(str(value or "").strip()))


def _split_chatai_client_refresh(p2, p3):
    p2 = str(p2 or "").strip()
    p3 = str(p3 or "").strip()
    if _looks_ms_client_id(p2):
        return p2, p3
    if _looks_ms_client_id(p3):
        return p3, p2
    return p2, p3


def _normalize_mailbox_email(email):
    value = str(email or "").strip().lstrip("\ufeff")
    if "@+" in value:
        local, suffix = value.split("@+", 1)
        suffix_lower = suffix.lower()
        for domain in KNOWN_EMAIL_DOMAINS:
            if suffix_lower.endswith(domain) and len(suffix) > len(domain):
                alias = suffix[: -len(domain)]
                repaired = f"{local}+{alias}@{domain}"
                if EMAIL_RE.match(repaired):
                    print(f"[!] Repaired malformed mailbox email: {value} -> {repaired.lower()}")
                    return repaired.lower()
    if EMAIL_RE.match(value):
        domain = value.rsplit("@", 1)[1]
        if not domain.startswith("+"):
            return value.lower()
    return ""


def _is_cfworker_line(line):
    lowered = line.lower()
    return lowered.startswith("cfworker://") or lowered.endswith("@edu.liziai.cloud") or lowered.endswith("@liziai.cloud")


def _is_gmail_line(line):
    return line.lower().startswith("gmail://")


def _is_remail_line(line):
    return line.lower().startswith("remail://")


def _is_smailr_line(line):
    return line.lower().startswith("smailr://")


def _parse_smailr_line(line, source_path, line_no):
    payload = line.split("://", 1)[1].strip() if "://" in line else line.strip()
    parts = [part.strip() for part in payload.split("---", 1)]
    email = _normalize_mailbox_email(parts[0] if parts else "")
    mailbox_id = parts[1] if len(parts) >= 2 else ""
    if not email or not mailbox_id:
        print(f"[!] Skip malformed Smailr mailbox line {source_path}:{line_no}")
        return None
    return MailboxAccount(
        email=email,
        source=str(source_path),
        provider="smailr",
        token=mailbox_id,
    )


def _is_icloud_url_line(line):
    return mailbox_icloud_url.is_icloud_url_line(line)


def _parse_cfworker_line(line, source_path, line_no):
    email = line.split("://", 1)[1].strip() if "://" in line else line
    email = _normalize_mailbox_email(email)
    if not email:
        print(f"[!] Skip malformed CFWorker email {source_path}:{line_no}")
        return None
    return MailboxAccount(email=email.lower(), source=str(source_path), provider="cfworker")


def _parse_remail_line(line, source_path, line_no):
    payload = line.split("://", 1)[1].strip() if "://" in line else line.strip()
    parts = [part.strip() for part in payload.split("---", 3)]
    email = _normalize_mailbox_email(parts[0] if parts else "")
    service_token = parts[1] if len(parts) >= 2 else ""
    order_no = parts[2] if len(parts) >= 3 else ""
    purchase_id = parts[3] if len(parts) >= 4 else ""
    # Older persisted ReMail rows may contain only ``email---token``.  The
    # provider can use that token first and recover the current order identity
    # through the API Key when necessary, so keep this compatibility form.
    if not email or not service_token:
        print(f"[!] Skip malformed ReMail mailbox line {source_path}:{line_no}")
        return None
    return MailboxAccount(
        email=email.lower(),
        source=str(source_path),
        provider="remail",
        token=service_token,
        order_no=order_no,
        purchase_id=purchase_id,
    )


def _parse_icloud_url_line(line, source_path, line_no):
    email, url = mailbox_icloud_url.split_icloud_url_line(line)
    email = _normalize_mailbox_email(email)
    if not email or not url:
        print(f"[!] Skip malformed iCloud OTP URL line {source_path}:{line_no}")
        return None
    return MailboxAccount(
        email=email,
        source=str(source_path),
        provider=mailbox_icloud_url.PROVIDER,
        token=url,
        auth_mode="otp_url",
    )


def _parse_gmail_line(line, source_path, line_no):
    payload = line.split("://", 1)[1].strip() if "://" in line else line.strip()
    if "----" in payload:
        parts = [part.strip() for part in payload.split("----")]
        email = _normalize_mailbox_email(parts[0] if parts else "")
        if not email:
            print(f"[!] Skip malformed Gmail email {source_path}:{line_no}")
            return None
        if len(parts) >= 4:
            client_id = parts[1]
            client_secret = parts[2]
            refresh_token = parts[3]
            access_token = parts[4] if len(parts) >= 5 else ""
            if not client_id or not client_secret or not refresh_token:
                print(f"[!] Skip malformed Gmail OAuth mailbox line {source_path}:{line_no}")
                return None
            return MailboxAccount(
                email=email.lower(),
                refresh_token=refresh_token,
                access_token=access_token,
                source=str(source_path),
                provider="gmail",
                token=client_id,
                client_secret=client_secret,
                auth_mode="oauth_refresh",
            )
        if len(parts) == 3:
            login_password = parts[1]
            app_password = parts[2]
            if not app_password:
                print(f"[!] Skip malformed Gmail app-password mailbox line {source_path}:{line_no}")
                return None
            return MailboxAccount(
                email=email.lower(),
                password=app_password,
                login_password=login_password,
                source=str(source_path),
                provider="gmail",
                auth_mode="app_password",
            )
        if len(parts) == 2:
            app_password = parts[1]
            if not app_password:
                print(f"[!] Skip malformed Gmail app-password mailbox line {source_path}:{line_no}")
                return None
            return MailboxAccount(
                email=email.lower(),
                password=app_password,
                source=str(source_path),
                provider="gmail",
                auth_mode="app_password",
            )
        print(f"[!] Skip malformed Gmail mailbox line {source_path}:{line_no}")
        return None

    parts = [part.strip() for part in payload.split("---")]
    if len(parts) < 2:
        print(f"[!] Skip malformed Gmail mailbox line {source_path}:{line_no}")
        return None
    email = _normalize_mailbox_email(parts[0])
    app_password = parts[1]
    sender_name = parts[2] if len(parts) >= 3 else ""
    if not email or not app_password:
        if not email:
            print(f"[!] Skip malformed Gmail email {source_path}:{line_no}")
        return None
    return MailboxAccount(
        email=email.lower(),
        password=app_password,
        source=str(source_path),
        provider="gmail",
        auth_mode="app_password",
        sender_name=sender_name,
    )


def parse_mailbox_pool_line(line, source_path="", line_no=0):
    """Parse one mailbox-pool line with the canonical provider dispatch order.

    This is the single owner of pool-line format detection (used by both the
    file loaders below and the desktop mailbox-pool read). Returns a
    MailboxAccount, or None for blank/comment/malformed lines.
    """
    line = str(line or "").strip().lstrip("\ufeff")
    if not line or line.startswith("#"):
        return None
    if _is_remail_line(line):
        return _parse_remail_line(line, source_path, line_no)
    if _is_smailr_line(line):
        return _parse_smailr_line(line, source_path, line_no)
    if _is_icloud_url_line(line):
        return _parse_icloud_url_line(line, source_path, line_no)
    if _is_cfworker_line(line):
        return _parse_cfworker_line(line, source_path, line_no)
    if _is_gmail_line(line):
        return _parse_gmail_line(line, source_path, line_no)
    if "----" in line:
        parts = line.split("----", 3)
        if len(parts) < 4:
            print(f"[!] Skip malformed chatai line {source_path}:{line_no}")
            return None
        email = _normalize_mailbox_email(parts[0].strip())
        password = parts[1].strip()
        client_id, refresh_token = _split_chatai_client_refresh(parts[2], parts[3])
        if not email or not refresh_token:
            if not email:
                print(f"[!] Skip malformed chatai email {source_path}:{line_no}")
            return None
        return MailboxAccount(
            email=email.lower(), password=password, refresh_token=refresh_token,
            source=str(source_path), provider="chatai", token=client_id,
        )
    parts = line.split("---", 4)
    if len(parts) < 3:
        print(f"[!] Skip malformed mailbox line {source_path}:{line_no}")
        return None
    email, password, refresh_token = (part.strip() for part in parts[:3])
    email = _normalize_mailbox_email(email)
    access_token = parts[3].strip() if len(parts) >= 4 else ""
    if not email or not refresh_token:
        if not email:
            print(f"[!] Skip malformed mailbox email {source_path}:{line_no}")
        return None
    return MailboxAccount(
        email=email.lower(), password=password, refresh_token=refresh_token,
        access_token=access_token, source=str(source_path), provider="graph",
    )


def _parse_mailbox_lines(path):
    """Load one mailbox text file, dispatching each line through the shared parser.

    This used to be duplicated: ``_parse_mailbox_token_file`` carried its own
    copy of the six-branch provider chain, and the two copies had drifted - the
    token-file copy had no ``----`` (chatai) branch, so a chatai line in a token
    file fell through to the ``---`` graph splitter and produced a silently
    corrupt record: ``email=a``, ``password='-b'``, ``refresh_token='-c'``.
    Format detection now lives in exactly one place.
    """
    records = []
    file_path = Path(path)
    if not file_path.exists():
        return records
    for line_no, raw in enumerate(file_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        account = parse_mailbox_pool_line(raw, file_path, line_no)
        if account:
            records.append(account)
    return records


def _parse_mailbox_token_file(path):
    return _parse_mailbox_lines(path)


def _parse_mailbox_password_file(path):
    records = []
    password_path = Path(path)
    if not password_path.exists():
        return records
    for line_no, raw in enumerate(password_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            print(f"[!] Skip malformed mailbox line {password_path}:{line_no}")
            continue
        email, password = (part.strip() for part in line.split(":", 1))
        email = _normalize_mailbox_email(email)
        if not email:
            print(f"[!] Skip malformed mailbox email {password_path}:{line_no}")
            continue
        provider = "gmail" if email.endswith(("@gmail.com", "@googlemail.com")) else "graph"
        records.append(MailboxAccount(
            email=email.lower(),
            password=password,
            source=str(password_path),
            provider=provider,
            auth_mode="app_password" if provider == "gmail" else "",
        ))
    return records


def _parse_chatai_mailbox_file(path):
    return _parse_mailbox_lines(path)
