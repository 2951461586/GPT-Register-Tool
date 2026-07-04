import argparse
import re
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from . import outlook_imap
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
from .mailbox_luckmail import (
    _create_luckmail_order,
    _create_luckmail_purchase,
    _latest_luckmail_message,
    _latest_luckmail_message_id,
    _luckmail_mail_time,
    _luckmail_request,
    _luckmail_token_alive,
    _luckmail_token_client,
    _luckmail_token_code,
    _luckmail_token_email,
    _luckmail_token_mails,
    _poll_luckmail_otp,
    _poll_luckmail_token_otp,
    _snapshot_luckmail_token_message,
)
from . import mailbox_graph
from .mailbox_graph import MailboxTokenExpiredError

# MailboxAccount and parsers moved to mailbox_types/mailbox_parsers.

def _email_cfg():
    return CFG.get("email_registration", {})


def _luckmail_enabled():
    return bool((_email_cfg().get("luckmail_api_key") or "").strip())


def _otp_poll_interval():
    try:
        return max(1.0, float(_email_cfg().get("otp_poll_interval", 2)))
    except Exception:
        return 2.0


# moved _normalize_mailbox_email to dedicated mailbox module.

# moved _luckmail_headers to dedicated mailbox module.

# moved _luckmail_url to dedicated mailbox module.

# moved _luckmail_token_client to dedicated mailbox module.

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


# moved _luckmail_token_code to dedicated mailbox module.

# moved _luckmail_token_mails to dedicated mailbox module.

# moved _luckmail_token_alive to dedicated mailbox module.

# moved _luckmail_token_email to dedicated mailbox module.

# moved _latest_luckmail_message to dedicated mailbox module.

# moved _latest_luckmail_message_id to dedicated mailbox module.

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


# moved _snapshot_luckmail_token_message to dedicated mailbox module.

# moved _luckmail_request to dedicated mailbox module.

# moved _create_luckmail_order to dedicated mailbox module.

# moved _create_luckmail_purchase to dedicated mailbox module.

def _create_cfworker_mailboxes(args=None):
    return mailbox_cfworker._create_cfworker_mailboxes(
        args=args,
        email_cfg=_email_cfg(),
        client_func=_cfworker_client,
    )


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


# moved _parse_mailbox_token_file to dedicated mailbox module.

# moved _parse_mailbox_password_file to dedicated mailbox module.

# moved _parse_chatai_mailbox_file to dedicated mailbox module.

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


def _ms_oauth_refresh(mailbox, proxy=None, scope_override=None):
    mailbox_graph.curl_requests = curl_requests
    return mailbox_graph.ms_oauth_refresh(mailbox, _email_cfg(), proxy=proxy, scope_override=scope_override)


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
    proxy = _resolve_mailbox_proxy(proxy)
    if getattr(mailbox, "provider", "") == "cfworker":
        return mailbox_cfworker._fetch_cfworker_messages(
            mailbox,
            limit=limit,
            proxy=proxy,
            email_cfg=_email_cfg(),
            client_func=_cfworker_client,
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
    return mailbox_cfworker._cfworker_otp_settle_seconds(_email_cfg())


def _cfworker_poll_proxy_enabled():
    return mailbox_cfworker._cfworker_poll_proxy_enabled(_email_cfg())


def _cfworker_direct_fallback_enabled():
    return mailbox_cfworker._cfworker_direct_fallback_enabled(_email_cfg())


def _poll_cfworker_otp(mailbox, subject_keyword="", timeout=300, issued_after_unix=0, proxy=None):
    return mailbox_cfworker._poll_cfworker_otp(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=timeout,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        email_cfg=_email_cfg(),
        otp_poll_interval_func=_otp_poll_interval,
        fetch_messages_func=_fetch_mailbox_messages,
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


# moved _poll_luckmail_otp to dedicated mailbox module.

# moved _luckmail_mail_time to dedicated mailbox module.

# moved _poll_luckmail_token_otp to dedicated mailbox module.
