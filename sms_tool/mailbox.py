import argparse
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import ConfigInput, current_config_data, resolve_runtime_config
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
from . import mailbox_icloud_url
from . import mailbox_smailr
from . import mailbox_strategies

# MailboxAccount and parsers moved to mailbox_types/mailbox_parsers.
# Deprecated monkeypatch hook for older integrations. Production composition
# injects RuntimeConfig through MailboxService and leaves this as None.
CFG = None

def _config_data(runtime_config: ConfigInput = None) -> Mapping[str, object]:
    if runtime_config is not None:
        return resolve_runtime_config(runtime_config).data
    if isinstance(CFG, Mapping):
        return CFG
    return current_config_data()


def _email_cfg(runtime_config: ConfigInput = None):
    value = _config_data(runtime_config).get("email_registration", {})
    return value if isinstance(value, Mapping) else {}


# ── Provider strategy registrations ──────────────────────────────────────────
# Register each provider's message-fetcher and OTP-poller. Providers are tried
# in registration order; the Graph API fallback (last) handles everything else.

def _register_mailbox_strategies():
    """Register provider-specific message fetchers and OTP pollers."""

    # cfworker
    mailbox_strategies.register_message_fetcher(
        "cfworker",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == "cfworker",
        lambda mb, *, limit, proxy, email_cfg, **kw: mailbox_cfworker._fetch_cfworker_messages(
            mb, limit=limit, proxy=proxy, email_cfg=email_cfg, client_func=_cfworker_client,
        ),
    )
    mailbox_strategies.register_otp_poller(
        "cfworker",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == "cfworker",
        lambda mb, **kw: _poll_cfworker_otp(mb, **kw),
    )

    # remail
    mailbox_strategies.register_message_fetcher(
        "remail",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == "remail",
        lambda mb, *, limit, proxy, include_body, **kw: mailbox_remail._fetch_remail_messages(
            mb, limit=limit, proxy=proxy, include_body=include_body,
        ),
    )
    mailbox_strategies.register_otp_poller(
        "remail",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == "remail",
        lambda mb, *, subject_keyword, timeout, issued_after_unix, proxy, excluded_otps, **kw: mailbox_remail._poll_remail_otp(
            mb,
            subject_keyword=subject_keyword,
            timeout=timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
            poll_interval=None,
        ),
    )

    # smailr
    mailbox_strategies.register_message_fetcher(
        "smailr",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == "smailr",
        lambda mb, *, limit, proxy, **kw: mailbox_smailr._fetch_smailr_messages(
            mb, limit=limit, proxy=proxy,
        ),
    )
    mailbox_strategies.register_otp_poller(
        "smailr",
        lambda mb, cfg: (
            str(getattr(mb, "provider", "") or "") == "smailr"
            and bool(getattr(mb, "token", ""))
        ),
        lambda mb, *, subject_keyword, timeout, issued_after_unix, proxy, excluded_otps, **kw: mailbox_smailr._poll_smailr_otp(
            mb,
            subject_keyword=subject_keyword,
            timeout=timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
        ),
    )

    # iCloud URL
    mailbox_strategies.register_message_fetcher(
        "icloud",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == mailbox_icloud_url.PROVIDER,
        lambda mb, *, limit, proxy, **kw: mailbox_icloud_url.fetch_icloud_url_messages(
            mb, limit=limit, proxy=proxy,
        ),
    )
    mailbox_strategies.register_otp_poller(
        "icloud",
        lambda mb, cfg: str(getattr(mb, "provider", "") or "") == mailbox_icloud_url.PROVIDER,
        mailbox_strategies._graph_poll_otp,
    )

    # Gmail
    def _gmail_matcher(mb, cfg):
        return mailbox_gmail.is_gmail_mailbox(mb) and _gmail_imap_enabled()

    def _gmail_fetch(mb, *, limit, proxy, **kw):
        return mailbox_gmail.fetch_gmail_imap_messages(
            mb,
            token_fetcher=lambda scope: _gmail_oauth_refresh(mb, proxy=proxy, scope_override=scope),
            folders=_gmail_imap_folders(),
            limit=limit,
            host=_gmail_imap_host(),
            port=_gmail_imap_port(),
            proxy=proxy,
        )

    mailbox_strategies.register_message_fetcher("gmail", _gmail_matcher, _gmail_fetch)

    # chongzhi
    def _chongzhi_matcher(mb, cfg):
        provider = str(getattr(mb, "provider", "") or "")
        return provider == "chongzhi" or (
            mailbox_chongzhi.chongzhi_enabled(cfg) and bool(getattr(mb, "password", ""))
        )

    def _chongzhi_fetch(mb, *, limit, proxy, email_cfg, **kw):
        email = str(getattr(mb, "email", "") or "").strip()
        password = str(getattr(mb, "password", "") or "").strip()
        if email and password:
            try:
                msgs = mailbox_chongzhi.fetch_chongzhi_messages(
                    email, password, folder="all", proxy=proxy, email_cfg=email_cfg,
                )
                if msgs:
                    return msgs
            except Exception:
                pass
        return _fetch_mailbox_messages_local(mb, limit=limit, proxy=proxy)

    mailbox_strategies.register_message_fetcher("chongzhi", _chongzhi_matcher, _chongzhi_fetch)

    # Re-register the catch-all after all provider-specific strategies.
    mailbox_strategies.register_message_fetcher(
        "graph_api",
        mailbox_strategies._graph_matcher,
        lambda mb, *, limit, proxy, **kw: _fetch_mailbox_messages_local(
            mb, limit=limit, proxy=proxy,
        ),
    )
    mailbox_strategies.register_otp_poller(
        "graph_api",
        mailbox_strategies._graph_matcher,
        mailbox_strategies._graph_poll_otp,
    )


# Compose once, then freeze so workflows cannot mutate provider resolution.
_register_mailbox_strategies()
mailbox_strategies.DEFAULT_MAILBOX_PROVIDERS.freeze()


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


def _configured_mailbox_proxy(runtime_config: ConfigInput = None):
    config = _config_data(runtime_config)
    email_cfg = _email_cfg(runtime_config)
    proxy_value = config.get("proxy")
    proxy_cfg = proxy_value if isinstance(proxy_value, Mapping) else {}
    return _normalize_mailbox_proxy(
        config.get("mailbox_proxy")
        or email_cfg.get("mailbox_proxy")
        or proxy_cfg.get("mailbox_proxy")
        or proxy_cfg.get("mailbox")
    )


def _resolve_mailbox_proxy(proxy=None, runtime_config: ConfigInput = None):
    return _configured_mailbox_proxy(runtime_config) or _normalize_mailbox_proxy(proxy)


def _provider_otp_issued_after(mailbox, issued_after_unix, runtime_config: ConfigInput = None):
    issued_after_unix = int(issued_after_unix or 0)
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    defaults = {
        "remail": 90,
        "smailr": 10,
        "cfworker": 10,
        mailbox_icloud_url.PROVIDER: 90,
    }
    if provider not in defaults:
        return issued_after_unix
    try:
        grace = int(_email_cfg(runtime_config).get(f"{provider}_otp_issued_after_grace_seconds", defaults[provider]))
    except (TypeError, ValueError):
        grace = defaults[provider]
    return max(0, issued_after_unix - min(max(0, grace), 300))


def _snapshot_mailbox_message(mailbox, proxy=None):
    provider = getattr(mailbox, "provider", "")
    if provider in {"cfworker", "remail", mailbox_icloud_url.PROVIDER}:
        try:
            if provider == mailbox_icloud_url.PROVIDER:
                messages = mailbox_icloud_url.snapshot_icloud_url_messages(
                    mailbox,
                    limit=25,
                    proxy=_resolve_mailbox_proxy(proxy),
                )
            else:
                messages = _fetch_mailbox_messages(mailbox, limit=1, proxy=proxy)
            message_id = _message_id(messages[0]) if messages else ""
            mailbox.seen_message_id = message_id
            mailbox.seen_message_ids = tuple(
                message_id for message_id in (_message_id(message) for message in messages) if message_id
            )
            mailbox.seen_message_received_ts = _message_received_ts(messages[0]) if messages else 0
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


def _create_smailr_mailboxes(args=None):
    args = args or argparse.Namespace()
    count = max(1, int(getattr(args, "count", 1) or 1))
    domain = getattr(args, "smailr_domain", None) or None
    return mailbox_smailr.create_smailr_mailboxes(count, domain=domain)


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
    remail_token = str(
        getattr(args, "remail_token", None)
        or os.environ.get("REMAIL_SERVICE_TOKEN")
        or remail_cfg.get("service_token")
        or ""
    ).strip()
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
        pool = mailbox_remail._create_remail_mailboxes(args, service_mode=mode)
    elif getattr(args, "remail_service_mode", None):
        pool = mailbox_remail._create_remail_mailboxes(args, service_mode=args.remail_service_mode)
    elif getattr(args, "buy_cfworker_mailbox", False):
        pool = _create_cfworker_mailboxes(args)
    elif getattr(args, "buy_smailr_mailbox", False):
        pool = _create_smailr_mailboxes(args)
    elif getattr(args, "chatai_mailbox_file", None):
        pool = _parse_chatai_mailbox_file(args.chatai_mailbox_file)
    elif getattr(args, "mailbox_file", None):
        pool = _parse_mailbox_token_file(args.mailbox_file)
    else:
        direct = _mailbox_from_config(args)
        if direct:
            pool = [direct]
        else:
            configured = _email_cfg().get("token_file")
            pool = _parse_mailbox_token_file(configured or _default_nb_register_token_file())
    return mailbox_remail.filter_dead_remail_mailboxes(pool)


def _pick_mailbox(index=0, args=None):
    pool = _load_mailbox_pool(args)
    if not pool:
        return None
    return pool[index % len(pool)]


def _ensure_mailbox_account(mailbox=None):
    if mailbox:
        filtered = mailbox_remail.filter_dead_remail_mailboxes([mailbox])
        return filtered[0] if filtered else None
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
    if provider == "smailr":
        return bool(getattr(mailbox, "token", "") and getattr(mailbox, "email", ""))
    if provider == mailbox_icloud_url.PROVIDER:
        return bool(getattr(mailbox, "token", "") and getattr(mailbox, "email", ""))
    if mailbox_gmail.is_gmail_mailbox(mailbox):
        return mailbox_gmail.mailbox_has_credentials(mailbox, _gmail_cfg())
    return bool(getattr(mailbox, "refresh_token", ""))


def _latest_email_otp_candidate(mailbox, keyword="", issued_after_unix=0, proxy=None, override_messages=None):
    latest = None
    seen_message_id = str(getattr(mailbox, "seen_message_id", "") or "").strip()
    seen_message_ids = {
        str(message_id or "").strip()
        for message_id in (getattr(mailbox, "seen_message_ids", ()) or ())
        if str(message_id or "").strip()
    }
    if seen_message_id:
        seen_message_ids.add(seen_message_id)
    seen_message_received_ts = int(getattr(mailbox, "seen_message_received_ts", 0) or 0)
    messages = override_messages if override_messages is not None else _fetch_mailbox_messages(mailbox, proxy=proxy)
    for msg in messages:
        if _message_id(msg) in seen_message_ids:
            continue
        candidate = _email_otp_candidate(mailbox, msg, keyword=keyword, issued_after_unix=issued_after_unix)
        if not candidate:
            continue
        candidate_ts = int(candidate.get("received_ts") or 0)
        if seen_message_received_ts and candidate_ts and candidate_ts < seen_message_received_ts:
            continue
        if latest is None:
            latest = candidate
            continue
        latest_ts = int(latest.get("received_ts") or 0)
        if candidate_ts and latest_ts:
            if candidate_ts > latest_ts:
                latest = candidate
        elif not latest_ts:
            latest = candidate
    return latest


def _fetch_mailbox_messages_local(mailbox, limit=25, proxy=None):
    """Fetch a non-provider mailbox through Microsoft Graph and optional IMAP."""
    proxy = _resolve_mailbox_proxy(proxy)
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
        response = curl_requests.get(
            graph_url,
            params=params,
            headers=headers,
            proxies=proxies,
            impersonate="chrome124",
            timeout=30,
        )
        if response.status_code in (401, 403):
            token = _ms_oauth_refresh(mailbox, proxy=proxy)
            headers["Authorization"] = "Bearer " + token
            response = curl_requests.get(
                graph_url,
                params=params,
                headers=headers,
                proxies=proxies,
                impersonate="chrome124",
                timeout=30,
            )
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:500]}
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Graph messages failed: {body}")
        graph_messages = body.get("value", [])
    except Exception as exc:
        graph_error = exc

    imap_messages = []
    if _outlook_imap_enabled() and outlook_imap.is_outlook_mailbox(mailbox):
        try:
            imap_messages = outlook_imap.fetch_outlook_imap_messages(
                mailbox,
                token_fetcher=lambda scope: _ms_oauth_refresh(
                    mailbox, proxy=proxy, scope_override=scope,
                ),
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
    for message in list(graph_messages or []) + list(imap_messages or []):
        key = _message_id(message) or str(message.get("internetMessageId") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(message)
    if merged:
        return merged
    if graph_error:
        raise graph_error
    return []


def _fetch_mailbox_messages(
    mailbox,
    limit=25,
    proxy=None,
    include_body=False,
    *,
    runtime_config: ConfigInput = None,
    registry=None,
):
    proxy = _resolve_mailbox_proxy(proxy, runtime_config)
    cfg = _email_cfg(runtime_config)

    # Try registered provider strategies in order (cfworker → remail → icloud → gmail → chongzhi → Graph API fallback)
    fetcher = mailbox_strategies.resolve_message_fetcher(mailbox, cfg, registry=registry)
    if fetcher is not None:
        try:
            return fetcher(
                mailbox,
                limit=limit,
                proxy=proxy,
                include_body=include_body,
                email_cfg=cfg,
                runtime_config=runtime_config,
                registry=registry,
            )
        except Exception as exc:
            # Graph fallback error will be raised below; for provider-specific
            # errors we let it propagate so the caller can see the real cause.
            print(f"[mailbox fetch error via strategy: {exc}]")
            raise

    # Should never reach here (Graph API is catch-all), but guard anyway
    raise RuntimeError("no mailbox message fetcher resolved")

# Message recipient extraction moved to sms_tool.mail_otp.

def _poll_email_otp(
    mailbox,
    subject_keyword="",
    timeout=300,
    issued_after_unix=0,
    proxy=None,
    excluded_otps=None,
    *,
    runtime_config: ConfigInput = None,
    registry=None,
):
    cfg = _email_cfg(runtime_config)
    provider = str(getattr(mailbox, "provider", "") or "")

    # Chongzhi credentials are only selected explicitly.  Older unlabelled
    # mailbox records may still use the provider when the integration is
    # enabled and a password is present; labelled providers must keep their
    # own strategy (notably ReMail) even when the account also has a login
    # password.
    if provider == "chongzhi" or (
        not provider
        and mailbox_chongzhi.chongzhi_enabled(cfg)
        and getattr(mailbox, "password", "")
    ):
        email = str(getattr(mailbox, "email", "") or "").strip()
        password = str(getattr(mailbox, "password", "") or "").strip()
        if email and password:
            return _poll_chongzhi_otp(
                mailbox, email=email, password=password,
                subject_keyword=subject_keyword, timeout=timeout,
                issued_after_unix=issued_after_unix, proxy=proxy,
            )

    issued_after_unix = _provider_otp_issued_after(mailbox, issued_after_unix, runtime_config)
    proxy = _resolve_mailbox_proxy(proxy, runtime_config)

    # Try registered OTP pollers in order (cfworker -> remail -> Graph API fallback graph_otp_poll)
    poller = mailbox_strategies.resolve_otp_poller(mailbox, cfg, registry=registry)
    if poller is not None:
        try:
            return poller(
                mailbox,
                subject_keyword=subject_keyword,
                timeout=timeout,
                issued_after_unix=issued_after_unix,
                proxy=proxy,
                excluded_otps=excluded_otps,
                runtime_config=runtime_config,
                registry=registry,
            )
        except Exception as exc:
            print(f"[poll_otp error via strategy: {exc}]")
            raise

    # Should never reach here (Graph API is catch-all poller)
    raise RuntimeError("no OTP poller resolved")

def _cfworker_otp_settle_seconds():
    return mailbox_cfworker._cfworker_otp_settle_seconds(_email_cfg())


def _cfworker_poll_proxy_enabled():
    return mailbox_cfworker._cfworker_poll_proxy_enabled(_email_cfg())


def _cfworker_direct_fallback_enabled():
    return mailbox_cfworker._cfworker_direct_fallback_enabled(_email_cfg())


def _poll_cfworker_otp(
    mailbox,
    subject_keyword="",
    timeout=300,
    issued_after_unix=0,
    proxy=None,
    excluded_otps=None,
    runtime_config: ConfigInput = None,
    registry=None,
):
    return mailbox_cfworker._poll_cfworker_otp(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=timeout,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        email_cfg=_email_cfg(runtime_config),
        otp_poll_interval_func=_otp_poll_interval,
        fetch_messages_func=lambda account, **kwargs: _fetch_mailbox_messages(
            account,
            runtime_config=runtime_config,
            registry=registry,
            **kwargs,
        ),
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
        r = curl_requests.get(graph_url, params=params, headers=headers, proxies=proxies, impersonate="chrome124", timeout=30)
        if r.status_code in (401, 403):
            token = _ms_oauth_refresh(mailbox, proxy=proxy)
            headers["Authorization"] = "Bearer " + token
            r = curl_requests.get(graph_url, params=params, headers=headers, proxies=proxies, impersonate="chrome124", timeout=30)
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
