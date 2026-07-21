import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from . import outlook_imap
from . import mailbox_gmail
from .mail_otp import (
    _candidate_is_newer,
    _email_otp_candidate,
    _extract_otp_from_text,
    _message_id,
    _message_received_ts,
    _message_recipients,
)
from . import mailbox_cfworker
from .mailbox_types import MailboxAccount
from .mailbox_parsers import (
    _looks_ms_client_id,
    _split_chatai_client_refresh,
    _normalize_mailbox_email,
    _parse_mailbox_token_file,
    _parse_mailbox_password_file,
    _parse_chatai_mailbox_file,
)
from . import mailbox_remail
from . import mailbox_graph
from .mailbox_graph import MailboxTokenExpiredError
from . import mailbox_chongzhi

# MailboxAccount and parsers moved to mailbox_types/mailbox_parsers.

def _email_cfg():
    return CFG.get("email_registration", {})


def _remail_enabled():
    return mailbox_remail._remail_enabled()


def _gmail_cfg():
    email_cfg = _email_cfg()
    gmail_cfg = email_cfg.get("gmail") if isinstance(email_cfg.get("gmail"), dict) else {}
    return gmail_cfg


def _bool_like(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _gmail_enabled():
    cfg = _gmail_cfg()
    if "enabled" in cfg:
        return _bool_like(cfg.get("enabled"), False)
    return bool(str(cfg.get("email") or "").strip())


def _otp_poll_interval():
    try:
        return max(1.0, float(_email_cfg().get("otp_poll_interval", 2)))
    except Exception:
        return 2.0


# moved _normalize_mailbox_email to dedicated mailbox module.

def _cfworker_cfg():
    return mailbox_cfworker._cfworker_cfg(_email_cfg())


def _cfworker_client(proxy=None):
    return mailbox_cfworker._cfworker_client(_email_cfg(), proxy=proxy)


def _normalize_mailbox_proxy(value):
    proxy = str(value or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def _configured_mailbox_proxy():
    email_cfg = _email_cfg()
    proxy_cfg = CFG.get("proxy") if isinstance(CFG.get("proxy"), dict) else {}
    return _normalize_mailbox_proxy(
        CFG.get("mailbox_proxy")
        or email_cfg.get("mailbox_proxy")
        or proxy_cfg.get("mailbox_proxy")
        or proxy_cfg.get("mailbox")
    )


def _resolve_mailbox_proxy(proxy=None):
    return _configured_mailbox_proxy() or _normalize_mailbox_proxy(proxy)


def _snapshot_mailbox_message(mailbox, proxy=None):
    provider = getattr(mailbox, "provider", "")
    if provider in {"cfworker", "remail"}:
        try:
            messages = _fetch_mailbox_messages(mailbox, limit=1, proxy=proxy)
            message_id = _message_id(messages[0]) if messages else ""
            mailbox.seen_message_id = message_id
            return message_id
        except Exception as e:
            print(f"[{provider} snapshot error: {e}]")
            return ""
    return ""

def _create_cfworker_mailboxes(args=None):
    return mailbox_cfworker._create_cfworker_mailboxes(
        args=args,
        email_cfg=_email_cfg(),
        client_func=_cfworker_client,
    )


def _default_nb_register_token_file():
    return str(Path.cwd() / "mailbox_tokens.txt")


def _gmail_mailbox_from_token_files(email):
    target_email = str(email or "").strip().lower()
    if not target_email or "@" not in target_email:
        return None
    paths = []
    configured = str(_email_cfg().get("token_file") or "").strip()
    if configured:
        paths.append(configured)
    paths.append(_default_nb_register_token_file())
    seen = set()
    for path in paths:
        path_key = str(path or "")
        if not path_key or path_key in seen:
            continue
        seen.add(path_key)
        for record in _parse_mailbox_token_file(path):
            if mailbox_gmail.is_gmail_mailbox(record) and str(record.email or "").strip().lower() == target_email:
                return record
    return None


def _gmail_mailbox_from_config(args=None):
    args = args or argparse.Namespace()
    cfg = _gmail_cfg()
    email_cfg = _email_cfg()
    requested_email = (
        getattr(args, "email", None)
        or cfg.get("email")
        or email_cfg.get("email")
        or ""
    ).strip().lower()
    if not requested_email:
        return None
    if not mailbox_gmail.is_gmail_mailbox(MailboxAccount(email=requested_email, provider="gmail")):
        return None
    email = requested_email
    cfg_email = str(cfg.get("email") or "").strip().lower()
    cfg_matches = bool(cfg_email and cfg_email == email)
    if cfg_matches:
        email = cfg_email
    explicit_password = str(getattr(args, "email_password", None) or "").strip()
    explicit_refresh_token = str(getattr(args, "email_refresh_token", None) or "").strip()
    explicit_access_token = str(getattr(args, "email_access_token", None) or "").strip()
    if not cfg_matches and not any([explicit_password, explicit_refresh_token, explicit_access_token]):
        token_mailbox = _gmail_mailbox_from_token_files(email)
        if token_mailbox is not None:
            return token_mailbox
        return None
    password = (
        explicit_password
        or (cfg.get("app_password") if cfg_matches else "")
        or (cfg.get("password") if cfg_matches else "")
        or (email_cfg.get("password") if cfg_matches else "")
        or ""
    ).strip()
    refresh_token = (
        explicit_refresh_token
        or (cfg.get("refresh_token") if cfg_matches else "")
        or (email_cfg.get("refresh_token") if cfg_matches else "")
        or ""
    ).strip()
    access_token = (
        explicit_access_token
        or (cfg.get("access_token") if cfg_matches else "")
        or (email_cfg.get("access_token") if cfg_matches else "")
        or ""
    ).strip()
    client_id = str(cfg.get("client_id") or "").strip() if cfg_matches else ""
    client_secret = str(cfg.get("client_secret") or "").strip() if cfg_matches else ""
    sender_name = str(cfg.get("sender_name") or "").strip() if cfg_matches else ""
    login_password = str(cfg.get("login_password") or "").strip() if cfg_matches else ""
    auth_mode = str(cfg.get("auth_mode") or "").strip().lower() if cfg_matches else ""
    if not _gmail_enabled() and not any([password, refresh_token, access_token, client_id, client_secret]):
        return None
    if not auth_mode:
        if refresh_token and client_id and client_secret:
            auth_mode = "oauth_refresh"
        elif password:
            auth_mode = "app_password"
    return MailboxAccount(
        email=email,
        password=password,
        login_password=login_password,
        refresh_token=refresh_token,
        access_token=access_token,
        source="config",
        provider="gmail",
        token=client_id,
        client_secret=client_secret,
        auth_mode=auth_mode,
        sender_name=sender_name,
    )


def _mailbox_from_config(args=None):
    args = args or argparse.Namespace()
    remail_cfg = mailbox_remail._remail_cfg()
    remail_token = str(getattr(args, "remail_token", None) or remail_cfg.get("service_token") or "").strip()
    gmail_mailbox = _gmail_mailbox_from_config(args)
    if gmail_mailbox is not None and not remail_token:
        return gmail_mailbox
    email = (getattr(args, "email", None) or _email_cfg().get("email") or "").strip().lower()
    if not email and remail_token:
        email = str(remail_cfg.get("delivery_email") or "").strip().lower()
    if not email:
        return None
    if remail_token:
        return MailboxAccount(
            email=email,
            source="remail_config",
            provider="remail",
            token=remail_token,
            order_no=str(remail_cfg.get("order_no") or "").strip(),
        )
    return MailboxAccount(
        email=email,
        password=(getattr(args, "email_password", None) or _email_cfg().get("password") or "").strip(),
        refresh_token=(getattr(args, "email_refresh_token", None) or _email_cfg().get("refresh_token") or "").strip(),
        access_token=(getattr(args, "email_access_token", None) or _email_cfg().get("access_token") or "").strip(),
        source="config",
        provider="graph",
    )


# moved _parse_mailbox_token_file to dedicated mailbox module.

# moved _parse_mailbox_password_file to dedicated mailbox module.

# moved _parse_chatai_mailbox_file to dedicated mailbox module.

def _load_mailbox_pool(args=None):
    args = args or argparse.Namespace()
    if getattr(args, "buy_remail_mailbox", False):
        mode = getattr(args, "remail_service_mode", None) or "purchase"
        return mailbox_remail._create_remail_mailboxes(args, service_mode=mode)
    if getattr(args, "remail_service_mode", None):
        return mailbox_remail._create_remail_mailboxes(args, service_mode=args.remail_service_mode)
    if getattr(args, "buy_cfworker_mailbox", False):
        return _create_cfworker_mailboxes(args)
    chatai_file = getattr(args, "chatai_mailbox_file", None)
    if chatai_file:
        return _parse_chatai_mailbox_file(chatai_file)
    mailbox_file = getattr(args, "mailbox_file", None)
    if mailbox_file:
        return _parse_mailbox_token_file(mailbox_file)
    direct = _mailbox_from_config(args)
    if direct:
        return [direct]
    configured = _email_cfg().get("token_file")
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
    if _remail_enabled():
        return mailbox_remail._create_remail_order(service_mode="code")
    return None


def _record_key(record):
    return (record.email or "").strip().lower()


def _ms_oauth_refresh(mailbox, proxy=None, scope_override=None):
    mailbox_graph.curl_requests = curl_requests
    return mailbox_graph.ms_oauth_refresh(mailbox, _email_cfg(), proxy=proxy, scope_override=scope_override)


def _gmail_oauth_refresh(mailbox, proxy=None, scope_override=None):
    return mailbox_gmail.refresh_gmail_access_token(mailbox, _gmail_cfg(), proxy=proxy, scope_override=scope_override)


def _email_otp_settle_seconds():
    try:
        cfg = _email_cfg()
        if "otp_settle_seconds" in cfg:
            return max(0.0, float(cfg.get("otp_settle_seconds", 0)))
        return max(0.0, float(cfg.get("cfworker_otp_settle_seconds", 3)))
    except Exception:
        return 3.0


# OTP candidate ordering moved to sms_tool.mail_otp.


def _outlook_imap_enabled():
    cfg = _email_cfg()
    value = cfg.get("outlook_imap_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _outlook_imap_folders():
    cfg = _email_cfg()
    configured = cfg.get("outlook_imap_folders")
    if isinstance(configured, str) and configured.strip():
        return [part.strip() for part in configured.split(",") if part.strip()]
    if isinstance(configured, list):
        return [str(part).strip() for part in configured if str(part).strip()]
    return list(outlook_imap.DEFAULT_FOLDERS)


def _gmail_imap_enabled():
    cfg = _gmail_cfg()
    if "imap_enabled" in cfg:
        return _bool_like(cfg.get("imap_enabled"), True)
    return True


def _gmail_imap_folders():
    cfg = _gmail_cfg()
    configured = cfg.get("imap_folders")
    if isinstance(configured, str) and configured.strip():
        return [part.strip() for part in configured.split(",") if part.strip()]
    if isinstance(configured, list):
        return [str(part).strip() for part in configured if str(part).strip()]
    return list(mailbox_gmail.DEFAULT_IMAP_FOLDERS)


def _gmail_imap_host():
    return str(_gmail_cfg().get("imap_host") or mailbox_gmail.DEFAULT_IMAP_HOST).strip() or mailbox_gmail.DEFAULT_IMAP_HOST


def _gmail_imap_port():
    try:
        return int(_gmail_cfg().get("imap_port") or mailbox_gmail.DEFAULT_IMAP_PORT)
    except Exception:
        return mailbox_gmail.DEFAULT_IMAP_PORT


def mailbox_has_inbox_credentials(mailbox):
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    if provider == "cfworker":
        return bool(getattr(mailbox, "email", ""))
    if provider == "remail":
        return bool(getattr(mailbox, "token", "") and getattr(mailbox, "email", ""))
    if mailbox_gmail.is_gmail_mailbox(mailbox):
        return mailbox_gmail.mailbox_has_credentials(mailbox, _gmail_cfg())
    return bool(getattr(mailbox, "refresh_token", ""))


def _latest_email_otp_candidate(mailbox, keyword="", issued_after_unix=0, proxy=None, override_messages=None):
    latest = None
    messages = override_messages if override_messages is not None else _fetch_mailbox_messages(mailbox, proxy=proxy)
    for msg in messages:
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
    proxy = _resolve_mailbox_proxy(proxy)

    # ── chongzhi.art first priority ──
    # If the mailbox has a password and chongzhi is enabled, try the API first.
    # Fall back to local Graph API / IMAP on any failure.
    provider = str(getattr(mailbox, "provider", "") or "")
    if provider == "chongzhi" or (mailbox_chongzhi.chongzhi_enabled(_email_cfg()) and getattr(mailbox, "password", "")):
        email = str(getattr(mailbox, "email", "") or "").strip()
        password = str(getattr(mailbox, "password", "") or "").strip()
        if email and password:
            try:
                chongzhi_msgs = mailbox_chongzhi.fetch_chongzhi_messages(
                    email, password, folder="all", proxy=proxy, email_cfg=_email_cfg(),
                )
                if chongzhi_msgs:
                    return chongzhi_msgs
                # Empty result means API accepted but no messages yet,
                # or rate limited — fall through to local fetch as fallback.
            except Exception as exc:
                print(f"[chongzhi fallback to local: {exc}]")

    if getattr(mailbox, "provider", "") == "cfworker":
        return mailbox_cfworker._fetch_cfworker_messages(
            mailbox,
            limit=limit,
            proxy=proxy,
            email_cfg=_email_cfg(),
            client_func=_cfworker_client,
        )
    if getattr(mailbox, "provider", "") == "remail":
        return mailbox_remail._fetch_remail_messages(mailbox, limit=limit, proxy=proxy)
    if mailbox_gmail.is_gmail_mailbox(mailbox):
        if not _gmail_imap_enabled():
            raise RuntimeError("gmail imap is disabled in config")
        return mailbox_gmail.fetch_gmail_imap_messages(
            mailbox,
            token_fetcher=lambda scope: _gmail_oauth_refresh(mailbox, proxy=proxy, scope_override=scope),
            folders=_gmail_imap_folders(),
            limit=limit,
            host=_gmail_imap_host(),
            port=_gmail_imap_port(),
            proxy=proxy,
        )
    graph_error = None
    graph_messages = []
    try:
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
        graph_messages = body.get("value", [])
    except Exception as exc:
        graph_error = exc

    imap_messages = []
    if _outlook_imap_enabled() and outlook_imap.is_outlook_mailbox(mailbox):
        try:
            imap_messages = outlook_imap.fetch_outlook_imap_messages(
                mailbox,
                token_fetcher=lambda scope: _ms_oauth_refresh(mailbox, proxy=proxy, scope_override=scope),
                folders=_outlook_imap_folders(),
                limit=limit,
            )
        except MailboxTokenExpiredError:
            if graph_error:
                raise
        except Exception as exc:
            print(f"[outlook imap error: {exc}]")

    merged = []
    seen = set()
    for msg in list(graph_messages or []) + list(imap_messages or []):
        key = _message_id(msg) or str(msg.get("internetMessageId") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(msg)
    if merged:
        return merged
    if graph_error:
        raise graph_error
    return []

# Message recipient extraction moved to sms_tool.mail_otp.

def _poll_email_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None, excluded_otps=None):
    provider = str(getattr(mailbox, "provider", "") or "")

    # ── chongzhi.art OTP polling ──
    # If the mailbox has a password and chongzhi is enabled, poll via API first.
    if provider == "chongzhi" or (mailbox_chongzhi.chongzhi_enabled(_email_cfg()) and getattr(mailbox, "password", "")):
        email = str(getattr(mailbox, "email", "") or "").strip()
        password = str(getattr(mailbox, "password", "") or "").strip()
        if email and password:
            return _poll_chongzhi_otp(
                mailbox, email=email, password=password,
                subject_keyword=subject_keyword, timeout=timeout,
                issued_after_unix=issued_after_unix, proxy=proxy,
            )

    if getattr(mailbox, "provider", "") == "remail":
        return mailbox_remail._poll_remail_otp(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=timeout,
            issued_after_unix=issued_after_unix,
            proxy=_resolve_mailbox_proxy(proxy),
            excluded_otps=excluded_otps,
            poll_interval=_otp_poll_interval(),
        )
    if getattr(mailbox, "provider", "") == "cfworker":
        return _poll_cfworker_otp(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
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
            if candidate and candidate.get("otp") not in {str(value or "").strip() for value in (excluded_otps or ())}:
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
    return mailbox_cfworker._cfworker_otp_settle_seconds(_email_cfg())


def _cfworker_poll_proxy_enabled():
    return mailbox_cfworker._cfworker_poll_proxy_enabled(_email_cfg())


def _cfworker_direct_fallback_enabled():
    return mailbox_cfworker._cfworker_direct_fallback_enabled(_email_cfg())


def _poll_cfworker_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None, excluded_otps=None):
    return mailbox_cfworker._poll_cfworker_otp(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=timeout,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        email_cfg=_email_cfg(),
        otp_poll_interval_func=_otp_poll_interval,
        fetch_messages_func=_fetch_mailbox_messages,
        excluded_otps=excluded_otps,
    )


def _latest_cfworker_otp_candidate(mailbox, keyword="", issued_after_unix=0, seen_message_id="", proxy=None):
    return mailbox_cfworker._latest_cfworker_otp_candidate(
        mailbox,
        keyword=keyword,
        issued_after_unix=issued_after_unix,
        seen_message_id=seen_message_id,
        proxy=proxy,
        fetch_messages_func=_fetch_mailbox_messages,
    )


def _poll_chongzhi_otp(mailbox, email, password, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None):
    """Poll for OTP via chongzhi.art API, falling back to local mailbox on failure."""
    keyword = (subject_keyword or "").lower()
    deadline = time.time() + timeout
    interval = _otp_poll_interval()
    settle_seconds = _email_otp_settle_seconds()
    local_fallback_attempted = False

    while time.time() < deadline:
        # Try chongzhi.art first
        try:
            messages = mailbox_chongzhi.fetch_chongzhi_messages(
                email, password, folder="all", proxy=proxy, email_cfg=_email_cfg(),
            )
            if messages:
                candidate = _latest_email_otp_candidate(
                    mailbox, keyword=keyword,
                    issued_after_unix=issued_after_unix,
                    proxy=proxy, override_messages=messages,
                )
                # Also check chongzhi pre-extracted OTP
                if not candidate:
                    otp = mailbox_chongzhi.chongzhi_latest_otp(
                        messages, keyword=keyword, issued_after_unix=issued_after_unix,
                    )
                    if otp:
                        candidate = {"otp": otp, "received_ts": 0}
                if candidate:
                    # Settle: wait a bit and check again for newer OTP
                    stable_until = time.time() + settle_seconds
                    while settle_seconds > 0 and time.time() < stable_until and time.time() < deadline:
                        time.sleep(min(interval, max(0.0, stable_until - time.time())))
                        newer_msgs = mailbox_chongzhi.fetch_chongzhi_messages(
                            email, password, folder="all", proxy=proxy, email_cfg=_email_cfg(),
                        )
                        newer_candidate = _latest_email_otp_candidate(
                            mailbox, keyword=keyword,
                            issued_after_unix=issued_after_unix,
                            proxy=proxy, override_messages=newer_msgs,
                        )
                        if newer_candidate and _candidate_is_newer(newer_candidate, candidate):
                            candidate = newer_candidate
                            stable_until = time.time() + settle_seconds
                    otp_code = str(candidate.get("otp") or "")
                    if otp_code:
                        print(f" code:{otp_code}!")
                        return otp_code
        except Exception as exc:
            print(f"[chongzhi poll error: {exc}]")

        # If chongzhi returns no messages and we haven't tried local yet,
        # fall back to local Graph API / IMAP for this poll cycle.
        if not local_fallback_attempted:
            local_fallback_attempted = True
            try:
                # Temporarily bypass chongzhi routing to use local fetch
                local_messages = _fetch_mailbox_messages_local(mailbox, limit=25, proxy=proxy)
                if local_messages:
                    candidate = _latest_email_otp_candidate(
                        mailbox, keyword=keyword,
                        issued_after_unix=issued_after_unix,
                        proxy=proxy, override_messages=local_messages,
                    )
                    if candidate:
                        otp_code = str(candidate.get("otp") or "")
                        if otp_code:
                            print(f" code:{otp_code}! (local fallback)")
                            return otp_code
            except Exception as exc:
                print(f"[local fallback error: {exc}]")

        print(".", end="", flush=True)
        time.sleep(interval)

    print(" timeout")
    return None


def _fetch_mailbox_messages_local(mailbox, limit=25, proxy=None):
    """Fetch messages using local Graph API / IMAP only (skip chongzhi.art)."""
    proxy = _resolve_mailbox_proxy(proxy)
    if getattr(mailbox, "provider", "") == "cfworker":
        return mailbox_cfworker._fetch_cfworker_messages(
            mailbox, limit=limit, proxy=proxy,
            email_cfg=_email_cfg(), client_func=_cfworker_client,
        )
    if mailbox_gmail.is_gmail_mailbox(mailbox):
        if not _gmail_imap_enabled():
            raise RuntimeError("gmail imap is disabled in config")
        return mailbox_gmail.fetch_gmail_imap_messages(
            mailbox,
            token_fetcher=lambda scope: _gmail_oauth_refresh(mailbox, proxy=proxy, scope_override=scope),
            folders=_gmail_imap_folders(), limit=limit,
            host=_gmail_imap_host(), port=_gmail_imap_port(), proxy=proxy,
        )
    graph_error = None
    graph_messages = []
    try:
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
        graph_messages = body.get("value", [])
    except Exception as exc:
        graph_error = exc

    imap_messages = []
    if _outlook_imap_enabled() and outlook_imap.is_outlook_mailbox(mailbox):
        try:
            imap_messages = outlook_imap.fetch_outlook_imap_messages(
                mailbox,
                token_fetcher=lambda scope: _ms_oauth_refresh(mailbox, proxy=proxy, scope_override=scope),
                folders=_outlook_imap_folders(),
                limit=limit,
            )
        except MailboxTokenExpiredError:
            if graph_error:
                raise
        except Exception as exc:
            print(f"[outlook imap error: {exc}]")

    merged = []
    seen = set()
    for msg in list(graph_messages or []) + list(imap_messages or []):
        key = _message_id(msg) or str(msg.get("internetMessageId") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(msg)
    if merged:
        return merged
    if graph_error:
        raise graph_error
    return []
