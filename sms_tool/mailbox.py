import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from .providers.cfworker_mailbox import CFWorkerMailboxClient
from .providers.luckmail_token import LuckMailTokenClient

# ==========================================
# Mailbox integration.
# Compatible with token files in this format:
# email---password---refresh_token---access_token---0
# ==========================================
@dataclass
class MailboxAccount:
    email: str
    password: str = ""
    refresh_token: str = ""
    access_token: str = ""
    source: str = ""
    provider: str = "graph"
    order_no: str = ""
    token: str = ""
    seen_message_id: str = ""
    purchase_id: str = ""
    project_name: str = ""
    price: str = ""
    purchase_total_cost: str = ""
    balance_after: str = ""


OTP_RE = re.compile(r"(^|[^0-9])([0-9]{6})([^0-9]|$)")
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
    """Accept both Chatai/Microsoft mailbox orders.

    Supported:
    - email----password----client_id----refresh_token
    - email----password----refresh_token----client_id
    """
    p2 = str(p2 or "").strip()
    p3 = str(p3 or "").strip()
    if _looks_ms_client_id(p2):
        return p2, p3
    if _looks_ms_client_id(p3):
        return p3, p2
    return p2, p3


def _email_cfg():
    return CFG.get("email_registration", {})


def _luckmail_enabled():
    return bool((_email_cfg().get("luckmail_api_key") or "").strip())


def _otp_poll_interval():
    try:
        return max(1.0, float(_email_cfg().get("otp_poll_interval", 2)))
    except Exception:
        return 2.0


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


def _luckmail_headers():
    api_key = (_email_cfg().get("luckmail_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("email_registration.luckmail_api_key is required")
    return {"X-API-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"}


def _luckmail_url(path):
    base_url = (_email_cfg().get("luckmail_base_url") or "https://mails.luckyous.com").rstrip("/")
    return base_url + path


def _luckmail_token_client():
    return LuckMailTokenClient(
        _email_cfg().get("luckmail_base_url", "https://mails.luckyous.com"),
        _email_cfg().get("luckmail_api_key", ""),
        timeout=30,
        verify_tls=False,
    )


def _cfworker_cfg():
    cfg = _email_cfg()
    nested = cfg.get("cfworker") if isinstance(cfg.get("cfworker"), dict) else {}
    return {
        "worker_url": str(nested.get("worker_url") or cfg.get("cfworker_url") or "").strip(),
        "domain": str(nested.get("domain") or cfg.get("cfworker_domain") or "edu.liziai.cloud").strip().lstrip("@"),
        "admin_token": str(nested.get("admin_token") or cfg.get("cfworker_admin_token") or "").strip(),
        "cf_api_token": str(nested.get("cf_api_token") or cfg.get("cfworker_api_token") or "").strip(),
    }


def _cfworker_client(proxy=None):
    cfg = _cfworker_cfg()
    return CFWorkerMailboxClient(
        cfg["worker_url"],
        admin_token=cfg["admin_token"],
        cf_api_token=cfg["cf_api_token"],
        timeout=8,
        proxy=proxy,
    )


def _luckmail_token_code(mailbox):
    token = getattr(mailbox, "token", "")
    if not token:
        raise RuntimeError("LuckMail purchased mailbox missing token")
    return _luckmail_token_client().code(token)


def _luckmail_token_mails(mailbox):
    token = getattr(mailbox, "token", "")
    if not token:
        raise RuntimeError("LuckMail purchased mailbox missing token")
    return _luckmail_token_client().mails(token)


def _luckmail_token_alive(mailbox):
    token = getattr(mailbox, "token", "")
    if not token:
        raise RuntimeError("LuckMail purchased mailbox missing token")
    return _luckmail_token_client().alive(token)


def _luckmail_token_email(token):
    if not token:
        return ""
    return _luckmail_token_client().resolve_email(token)


def _latest_luckmail_message(data):
    data = data or {}
    latest = data.get("mail") or data.get("latest_mail") or {}
    if latest:
        return latest
    mails = data.get("mails") or []
    return mails[0] if isinstance(mails, list) and mails and isinstance(mails[0], dict) else {}


def _latest_luckmail_message_id(data):
    latest = _latest_luckmail_message(data)
    return str(latest.get("message_id") or latest.get("id") or "").strip()


def _snapshot_mailbox_message(mailbox, proxy=None):
    provider = getattr(mailbox, "provider", "")
    if provider == "cfworker":
        try:
            messages = _fetch_mailbox_messages(mailbox, limit=1, proxy=proxy)
            message_id = _message_id(messages[0]) if messages else ""
            mailbox.seen_message_id = message_id
            return message_id
        except Exception as e:
            print(f"[cfworker snapshot error: {e}]")
            return ""
    return _snapshot_luckmail_token_message(mailbox)


def _snapshot_luckmail_token_message(mailbox):
    if getattr(mailbox, "provider", "") != "luckmail_token":
        return ""
    try:
        data = (_luckmail_token_code(mailbox).get("data") or {})
        message_id = _latest_luckmail_message_id(data)
        if not message_id:
            data = (_luckmail_token_mails(mailbox).get("data") or {})
            message_id = _latest_luckmail_message_id(data)
        mailbox.seen_message_id = message_id
        return message_id
    except Exception as e:
        print(f"[luckmail token snapshot error: {e}]")
        return ""


def _luckmail_request(method, path, **kwargs):
    method = method.upper()
    url = _luckmail_url(path)
    headers = _luckmail_headers()
    if method == "GET":
        r = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=30, verify=False, **kwargs)
    elif method == "POST":
        r = curl_requests.post(url, headers=headers, impersonate="chrome", timeout=30, verify=False, **kwargs)
    else:
        raise ValueError(f"unsupported LuckMail method: {method}")
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"LuckMail HTTP {r.status_code}: {body}")
    if body.get("code") not in (0, None):
        raise RuntimeError(f"LuckMail API error: {body}")
    return body.get("data")


def _create_luckmail_order():
    cfg = _email_cfg()
    payload = {
        "project_code": cfg.get("luckmail_project_code", "openai"),
        "email_type": cfg.get("luckmail_email_type", "self_built"),
    }
    for src, dest in (
        ("luckmail_domain", "domain"),
        ("luckmail_specified_email", "specified_email"),
        ("luckmail_variant_mode", "variant_mode"),
    ):
        value = str(cfg.get(src, "") or "").strip()
        if value:
            payload[dest] = value
    data = _luckmail_request("POST", "/api/v1/openapi/order/create", json=payload)
    order_no = str((data or {}).get("order_no") or "").strip()
    email = str((data or {}).get("email_address") or "").strip().lower()
    if not order_no or not email:
        raise RuntimeError(f"LuckMail order/create returned incomplete data: {data}")
    return MailboxAccount(
        email=email,
        source="luckmail",
        provider="luckmail",
        order_no=order_no,
    )


def _create_luckmail_purchase(args=None):
    args = args or argparse.Namespace()
    cfg = _email_cfg()
    project_code = (
        getattr(args, "luckmail_purchase_project", None)
        or cfg.get("luckmail_purchase_project_code")
        or cfg.get("luckmail_project_code")
        or "openai"
    )
    email_type = (
        getattr(args, "luckmail_purchase_email_type", None)
        or cfg.get("luckmail_purchase_email_type")
        or "ms_imap"
    )
    domain = (
        getattr(args, "luckmail_purchase_domain", None)
        or cfg.get("luckmail_purchase_domain")
        or "outlook.com"
    )
    quantity = max(1, int(getattr(args, "count", None) or 1))
    payload = {
        "project_code": project_code,
        "email_type": email_type,
        "quantity": quantity,
    }
    if domain:
        payload["domain"] = domain
    print(f"[*] LuckMail purchase: project={project_code} type={email_type} domain={domain or '*'} quantity={quantity}")
    data = _luckmail_request("POST", "/api/v1/openapi/email/purchase", json=payload)
    purchases = (data or {}).get("purchases") or []
    if not purchases:
        raise RuntimeError(f"LuckMail email/purchase returned no purchases: {data}")
    accounts = []
    for item in purchases:
        email = str(item.get("email_address") or "").strip().lower()
        token = str(item.get("token") or "").strip()
        if not email or not token:
            raise RuntimeError(f"LuckMail purchase item incomplete: {item}")
        accounts.append(MailboxAccount(
            email=email,
            source="luckmail_purchase",
            provider="luckmail_token",
            token=token,
            purchase_id=str(item.get("id") or ""),
            project_name=str(item.get("project_name") or item.get("project") or ""),
            price=str(item.get("price") or ""),
            purchase_total_cost=str((data or {}).get("total_cost") or ""),
            balance_after=str((data or {}).get("balance_after") or ""),
        ))
        print(f"[*] Purchased mailbox: {email} token={token} price={item.get('price')}")
    if (data or {}).get("balance_after") is not None:
        print(f"[*] LuckMail balance after purchase: {data.get('balance_after')}")
    return accounts


def _create_cfworker_mailboxes(args=None):
    args = args or argparse.Namespace()
    cfg = _cfworker_cfg()
    domain = str(getattr(args, "cfworker_domain", None) or cfg["domain"] or "edu.liziai.cloud").strip().lstrip("@").lower()
    quantity = max(1, int(getattr(args, "count", None) or 1))
    print(f"[*] CFWorker mailbox batch: domain={domain} quantity={quantity}")
    emails = _cfworker_client(proxy=getattr(args, "proxy", None)).create_mailboxes(count=quantity, domain=domain)
    accounts = [
        MailboxAccount(
            email=email,
            source=cfg["worker_url"],
            provider="cfworker",
        )
        for email in emails
    ]
    for account in accounts:
        print(f"[*] CFWorker mailbox: {account.email}")
    return accounts


def _default_nb_register_token_file():
    return str(Path.cwd() / "mailbox_tokens.txt")


def _mailbox_from_config(args=None):
    args = args or argparse.Namespace()
    luckmail_token = (
        getattr(args, "luckmail_token", None)
        or _email_cfg().get("luckmail_token")
        or ""
    ).strip()
    email = (getattr(args, "email", None) or _email_cfg().get("email") or "").strip().lower()
    if not email and luckmail_token:
        try:
            email = _luckmail_token_email(luckmail_token)
        except Exception as e:
            print(f"[luckmail token mailbox resolve error: {e}]")
    if not email:
        return None
    return MailboxAccount(
        email=email,
        password=(getattr(args, "email_password", None) or _email_cfg().get("password") or "").strip(),
        refresh_token=(getattr(args, "email_refresh_token", None) or _email_cfg().get("refresh_token") or "").strip(),
        access_token=(getattr(args, "email_access_token", None) or _email_cfg().get("access_token") or "").strip(),
        source="luckmail_purchase" if luckmail_token else "config",
        provider="luckmail_token" if luckmail_token else "graph",
        token=luckmail_token,
    )


def _parse_mailbox_token_file(path):
    records = []
    token_path = Path(path)
    if not token_path.exists():
        return records
    for line_no, raw in enumerate(token_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cfworker://") or line.lower().endswith("@edu.liziai.cloud"):
            email = line.split("://", 1)[1].strip() if "://" in line else line
            email = _normalize_mailbox_email(email)
            if not email:
                print(f"[!] Skip malformed CFWorker email {token_path}:{line_no}")
                continue
            records.append(MailboxAccount(
                email=email.lower(),
                source=str(token_path),
                provider="cfworker",
            ))
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
            email=email.lower(),
            password=password,
            refresh_token=refresh_token,
            access_token=access_token,
            source=str(token_path),
            provider="graph",
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
        records.append(MailboxAccount(
            email=email.lower(),
            password=password,
            source=str(password_path),
            provider="graph",
        ))
    return records


def _parse_chatai_mailbox_file(path):
    """Parse chatai format and tolerate standard mailbox lines in mixed temp files."""
    records = []
    chatai_path = Path(path)
    if not chatai_path.exists():
        return records
    for line_no, raw in enumerate(chatai_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cfworker://") or line.lower().endswith("@edu.liziai.cloud"):
            email = line.split("://", 1)[1].strip() if "://" in line else line
            email = _normalize_mailbox_email(email)
            if not email:
                print(f"[!] Skip malformed CFWorker email {chatai_path}:{line_no}")
                continue
            records.append(MailboxAccount(
                email=email.lower(),
                source=str(chatai_path),
                provider="cfworker",
            ))
            continue
        if "----" in line:
            parts = line.split("----", 3)
            if len(parts) < 4:
                print(f"[!] Skip malformed chatai line {chatai_path}:{line_no}")
                continue
            email = parts[0].strip()
            password = parts[1].strip()
            client_id, refresh_token = _split_chatai_client_refresh(parts[2], parts[3])
            email = _normalize_mailbox_email(email)
            if not email or not refresh_token:
                if not email:
                    print(f"[!] Skip malformed chatai email {chatai_path}:{line_no}")
                continue
            records.append(MailboxAccount(
                email=email.lower(),
                password=password,
                refresh_token=refresh_token,
                source=str(chatai_path),
                provider="chatai",
                token=client_id,
            ))
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
        records.append(MailboxAccount(
            email=email.lower(),
            password=password,
            refresh_token=refresh_token,
            access_token=access_token,
            source=str(chatai_path),
            provider="graph",
        ))
    return records


def _load_mailbox_pool(args=None):
    args = args or argparse.Namespace()
    if getattr(args, "buy_luckmail_mailbox", False):
        return _create_luckmail_purchase(args)
    if getattr(args, "buy_cfworker_mailbox", False):
        return _create_cfworker_mailboxes(args)
    chatai_file = getattr(args, "chatai_mailbox_file", None)
    if chatai_file:
        return _parse_chatai_mailbox_file(chatai_file)
    direct = _mailbox_from_config(args)
    if direct:
        return [direct]
    configured = getattr(args, "mailbox_file", None) or _email_cfg().get("token_file")
    token_file = configured or _default_nb_register_token_file()
    return _parse_mailbox_token_file(token_file)


def _pick_mailbox(index=0, args=None):
    pool = _load_mailbox_pool(args)
    if not pool:
        return None
    return pool[index % len(pool)]


def _ensure_mailbox_account(mailbox=None):
    if mailbox:
        return mailbox
    if _luckmail_enabled():
        return _create_luckmail_order()
    return None


def _record_key(record):
    return (record.email or "").strip().lower()


def _ms_oauth_refresh(mailbox, proxy=None):
    cfg = _email_cfg()
    client_id = getattr(mailbox, "token", "") or cfg.get("oauth_client_id", "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
    scope = cfg.get("oauth_scope", "https://graph.microsoft.com/.default offline_access")
    token_url = cfg.get("oauth_token_url", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
    if not mailbox.refresh_token:
        raise RuntimeError("mailbox refresh_token is required")
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": mailbox.refresh_token,
        "scope": scope,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = curl_requests.post(token_url, data=data, proxies=proxies, impersonate="chrome", timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code != 200:
        error_code = str((body.get("error_codes") or [body.get("error") or ""])[0]) if isinstance(body, dict) else ""
        if "invalid_grant" in str(body).lower() or "9002313" in error_code:
            raise MailboxTokenExpiredError(f"mailbox token expired (invalid_grant): {mailbox.email}")
        raise RuntimeError(f"mailbox token refresh failed: {body}")
    access_token = body.get("access_token", "")
    if not access_token:
        raise RuntimeError("mailbox token refresh returned empty access token")
    if body.get("refresh_token"):
        mailbox.refresh_token = body["refresh_token"]
    mailbox.access_token = access_token
    return access_token


class MailboxTokenExpiredError(RuntimeError):
    """Raised when the mailbox refresh token is permanently invalid (invalid_grant)."""
    pass


def _extract_otp_from_text(text):
    match = OTP_RE.search(text or "")
    return match.group(2) if match else ""


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
    if keyword and keyword not in subject.lower():
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


def _email_otp_settle_seconds():
    try:
        cfg = _email_cfg()
        if "otp_settle_seconds" in cfg:
            return max(0.0, float(cfg.get("otp_settle_seconds", 0)))
        return max(0.0, float(cfg.get("cfworker_otp_settle_seconds", 3)))
    except Exception:
        return 3.0


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


def _latest_email_otp_candidate(mailbox, keyword="", issued_after_unix=0, proxy=None):
    latest = None
    for msg in _fetch_mailbox_messages(mailbox, proxy=proxy):
        candidate = _email_otp_candidate(mailbox, msg, keyword=keyword, issued_after_unix=issued_after_unix)
        if not candidate:
            continue
        if latest is None:
            latest = candidate
            continue
        candidate_ts = int(candidate.get("received_ts") or 0)
        latest_ts = int(latest.get("received_ts") or 0)
        if candidate_ts and latest_ts:
            if candidate_ts > latest_ts:
                latest = candidate
        elif not latest_ts:
            latest = candidate
    return latest


def _fetch_mailbox_messages(mailbox, limit=25, proxy=None):
    if getattr(mailbox, "provider", "") == "cfworker":
        if not _cfworker_poll_proxy_enabled():
            return _cfworker_client(proxy=None).fetch_messages(mailbox.email, limit=limit)
        try:
            return _cfworker_client(proxy=proxy).fetch_messages(mailbox.email, limit=limit)
        except Exception as exc:
            if not proxy or not _cfworker_direct_fallback_enabled():
                raise
            print(f"[cfworker proxy poll error: {exc}; retrying direct]")
            return _cfworker_client(proxy=None).fetch_messages(mailbox.email, limit=limit)
    cfg = _email_cfg()
    token = mailbox.access_token or _ms_oauth_refresh(mailbox, proxy=proxy)
    graph_url = cfg.get("graph_messages_url", "https://graph.microsoft.com/v1.0/me/messages")
    params = {
        "$top": str(max(1, min(int(limit or 25), 100))),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,bodyPreview,body,toRecipients,ccRecipients,bccRecipients,internetMessageHeaders,receivedDateTime",
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="text"',
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = curl_requests.get(graph_url, params=params, headers=headers, proxies=proxies, impersonate="chrome", timeout=30)
    if r.status_code in (401, 403):
        token = _ms_oauth_refresh(mailbox, proxy=proxy)
        headers["Authorization"] = "Bearer " + token
        r = curl_requests.get(graph_url, params=params, headers=headers, proxies=proxies, impersonate="chrome", timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"Graph messages failed: {body}")
    return body.get("value", [])


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


def _poll_email_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None):
    if getattr(mailbox, "provider", "") == "luckmail":
        return _poll_luckmail_otp(mailbox, timeout=timeout)
    if getattr(mailbox, "provider", "") == "luckmail_token":
        return _poll_luckmail_token_otp(mailbox, timeout=timeout, issued_after_unix=issued_after_unix)
    if getattr(mailbox, "provider", "") == "cfworker":
        return _poll_cfworker_otp(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
        )
    keyword = (subject_keyword or "").lower()
    deadline = time.time() + timeout
    interval = _otp_poll_interval()
    settle_seconds = _email_otp_settle_seconds()
    while time.time() < deadline:
        try:
            candidate = _latest_email_otp_candidate(
                mailbox,
                keyword=keyword,
                issued_after_unix=issued_after_unix,
                proxy=proxy,
            )
            if candidate:
                stable_until = time.time() + settle_seconds
                while settle_seconds > 0 and time.time() < stable_until and time.time() < deadline:
                    time.sleep(min(interval, max(0.0, stable_until - time.time())))
                    newer = _latest_email_otp_candidate(
                        mailbox,
                        keyword=keyword,
                        issued_after_unix=issued_after_unix,
                        proxy=proxy,
                    )
                    if _candidate_is_newer(newer, candidate):
                        candidate = newer
                        stable_until = time.time() + settle_seconds
                print(f" code:{candidate['otp']}!")
                return candidate["otp"]
        except MailboxTokenExpiredError:
            raise
        except Exception as e:
            print(f"[mailbox poll error: {e}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None


def _cfworker_otp_settle_seconds():
    try:
        return max(0.0, float(_email_cfg().get("cfworker_otp_settle_seconds", 3)))
    except Exception:
        return 3.0


def _cfworker_poll_proxy_enabled():
    value = _email_cfg().get("cfworker_poll_proxy", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cfworker_direct_fallback_enabled():
    value = _email_cfg().get("cfworker_direct_fallback", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _poll_cfworker_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None):
    keyword = (subject_keyword or "").lower()
    deadline = time.time() + timeout
    interval = _otp_poll_interval()
    settle_seconds = _cfworker_otp_settle_seconds()
    seen_message_id = getattr(mailbox, "seen_message_id", "")
    while time.time() < deadline:
        try:
            candidate = _latest_cfworker_otp_candidate(
                mailbox,
                keyword=keyword,
                issued_after_unix=issued_after_unix,
                seen_message_id=seen_message_id,
                proxy=proxy,
            )
            if candidate:
                stable_until = time.time() + settle_seconds
                while settle_seconds > 0 and time.time() < stable_until and time.time() < deadline:
                    time.sleep(min(interval, max(0.0, stable_until - time.time())))
                    newer = _latest_cfworker_otp_candidate(
                        mailbox,
                        keyword=keyword,
                        issued_after_unix=issued_after_unix,
                        seen_message_id=seen_message_id,
                        proxy=proxy,
                    )
                    if newer and newer.get("id") != candidate.get("id"):
                        candidate = newer
                        stable_until = time.time() + settle_seconds
                print(f" code:{candidate['otp']}!")
                return candidate["otp"]
        except Exception as e:
            print(f"[mailbox poll error: {e}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None


def _latest_cfworker_otp_candidate(mailbox, keyword="", issued_after_unix=0, seen_message_id="", proxy=None):
    for msg in _fetch_mailbox_messages(mailbox, proxy=proxy):
        if seen_message_id and _message_id(msg) == seen_message_id:
            continue
        candidate = _email_otp_candidate(mailbox, msg, keyword=keyword, issued_after_unix=issued_after_unix)
        if candidate:
            return candidate
    return None


def _poll_luckmail_otp(mailbox, timeout=300):
    deadline = time.time() + timeout
    interval = _otp_poll_interval()
    order_no = getattr(mailbox, "order_no", "")
    if not order_no:
        raise RuntimeError("LuckMail mailbox missing order_no")
    while time.time() < deadline:
        try:
            data = _luckmail_request("GET", f"/api/v1/openapi/order/{order_no}/code")
            status = str((data or {}).get("status") or "").lower()
            code = str((data or {}).get("verification_code") or "").strip()
            if status == "success" and code:
                print(f" code:{code}!")
                return code
            if status in {"timeout", "cancelled", "canceled"}:
                print(f" [{status}]")
                return None
        except Exception as e:
            print(f"[luckmail poll error: {e}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None


def _luckmail_mail_time(mail):
    value = str((mail or {}).get("received_at") or "").strip()
    if not value:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            pass
    return 0


def _poll_luckmail_token_otp(mailbox, timeout=300, issued_after_unix=0):
    deadline = time.time() + timeout
    interval = _otp_poll_interval()
    seen_message_id = getattr(mailbox, "seen_message_id", "")
    last_error_text = ""
    last_error_at = 0
    while time.time() < deadline:
        try:
            body = _luckmail_token_code(mailbox)
            data = body.get("data") or {}
            code = str(data.get("verification_code") or "").strip()
            message_id = _latest_luckmail_message_id(data)
            if code and message_id and message_id != seen_message_id:
                print(f" code:{code}!")
                return code
            if code and not message_id and data.get("has_new_mail"):
                print(f" code:{code}!")
                return code
            if code:
                print(" old-code", end="", flush=True)
        except Exception as e:
            error_text = str(e)
            now = time.time()
            if error_text != last_error_text or now - last_error_at >= 30:
                print(f"[luckmail token poll error: {error_text}]")
                last_error_text = error_text
                last_error_at = now
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None

