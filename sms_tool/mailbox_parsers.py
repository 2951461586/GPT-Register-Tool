import re
from pathlib import Path

from .mailbox_types import MailboxAccount

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
    return line.lower().startswith("cfworker://") or line.lower().endswith("@edu.liziai.cloud")


def _parse_cfworker_line(line, source_path, line_no):
    email = line.split("://", 1)[1].strip() if "://" in line else line
    email = _normalize_mailbox_email(email)
    if not email:
        print(f"[!] Skip malformed CFWorker email {source_path}:{line_no}")
        return None
    return MailboxAccount(email=email.lower(), source=str(source_path), provider="cfworker")


def _parse_mailbox_token_file(path):
    records = []
    token_path = Path(path)
    if not token_path.exists():
        return records
    for line_no, raw in enumerate(token_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if _is_cfworker_line(line):
            account = _parse_cfworker_line(line, token_path, line_no)
            if account:
                records.append(account)
            continue
        parts = line.split("---", 4)
        if len(parts) < 3:
            print(f"[!] Skip malformed mailbox line {token_path}:{line_no}")
            continue
        email, password, refresh_token = (part.strip() for part in parts[:3])
        email = _normalize_mailbox_email(email)
        access_token = parts[3].strip() if len(parts) >= 4 else ""
        if not email or not refresh_token:
            if not email:
                print(f"[!] Skip malformed mailbox email {token_path}:{line_no}")
            continue
        records.append(MailboxAccount(
            email=email.lower(), password=password, refresh_token=refresh_token,
            access_token=access_token, source=str(token_path), provider="graph",
        ))
    return records


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
        records.append(MailboxAccount(email=email.lower(), password=password, source=str(password_path), provider="graph"))
    return records


def _parse_chatai_mailbox_file(path):
    records = []
    chatai_path = Path(path)
    if not chatai_path.exists():
        return records
    for line_no, raw in enumerate(chatai_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if _is_cfworker_line(line):
            account = _parse_cfworker_line(line, chatai_path, line_no)
            if account:
                records.append(account)
            continue
        if "----" in line:
            parts = line.split("----", 3)
            if len(parts) < 4:
                print(f"[!] Skip malformed chatai line {chatai_path}:{line_no}")
                continue
            email = _normalize_mailbox_email(parts[0].strip())
            password = parts[1].strip()
            client_id, refresh_token = _split_chatai_client_refresh(parts[2], parts[3])
            if not email or not refresh_token:
                if not email:
                    print(f"[!] Skip malformed chatai email {chatai_path}:{line_no}")
                continue
            records.append(MailboxAccount(email=email.lower(), password=password, refresh_token=refresh_token, source=str(chatai_path), provider="chatai", token=client_id))
            continue
        parts = line.split("---", 4)
        if len(parts) < 3:
            print(f"[!] Skip malformed chatai line {chatai_path}:{line_no}")
            continue
        email, password, refresh_token = (part.strip() for part in parts[:3])
        email = _normalize_mailbox_email(email)
        access_token = parts[3].strip() if len(parts) >= 4 else ""
        if not email or not refresh_token:
            if not email:
                print(f"[!] Skip malformed chatai email {chatai_path}:{line_no}")
            continue
        records.append(MailboxAccount(email=email.lower(), password=password, refresh_token=refresh_token, access_token=access_token, source=str(chatai_path), provider="graph"))
    return records
