import argparse
import time

from .mail_otp import _email_otp_candidate, _message_id
from .mailbox_types import MailboxAccount
from .providers.cfworker_mailbox import CFWorkerMailboxClient


def _cfworker_cfg(email_cfg):
    nested = email_cfg.get("cfworker") if isinstance(email_cfg.get("cfworker"), dict) else {}
    return {
        "worker_url": str(nested.get("worker_url") or email_cfg.get("cfworker_url") or "").strip(),
        "domain": str(nested.get("domain") or email_cfg.get("cfworker_domain") or "liziai.cloud").strip().lstrip("@"),
        "admin_token": str(nested.get("admin_token") or email_cfg.get("cfworker_admin_token") or "").strip(),
        "cf_api_token": str(nested.get("cf_api_token") or email_cfg.get("cfworker_api_token") or "").strip(),
    }


def _cfworker_client(email_cfg, proxy=None):
    cfg = _cfworker_cfg(email_cfg)
    nested = email_cfg.get("cfworker") if isinstance(email_cfg.get("cfworker"), dict) else {}
    try:
        timeout = max(5, int(nested.get("timeout") or email_cfg.get("cfworker_timeout_seconds") or 30))
    except (TypeError, ValueError):
        timeout = 30
    return CFWorkerMailboxClient(
        cfg["worker_url"],
        admin_token=cfg["admin_token"],
        cf_api_token=cfg["cf_api_token"],
        timeout=timeout,
        proxy=proxy,
    )


def _cfworker_otp_settle_seconds(email_cfg):
    try:
        return max(0.0, float(email_cfg.get("cfworker_otp_settle_seconds", 3)))
    except Exception:
        return 3.0


def _cfworker_poll_proxy_enabled(email_cfg):
    value = email_cfg.get("cfworker_poll_proxy", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cfworker_direct_fallback_enabled(email_cfg):
    value = email_cfg.get("cfworker_direct_fallback", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _create_cfworker_mailboxes(args=None, email_cfg=None, client_func=None):
    args = args or argparse.Namespace()
    email_cfg = email_cfg or {}
    cfg = _cfworker_cfg(email_cfg)
    domain = str(getattr(args, "cfworker_domain", None) or cfg["domain"] or "liziai.cloud").strip().lstrip("@").lower()
    quantity = max(1, int(getattr(args, "count", None) or 1))
    print(f"[*] CFWorker mailbox batch: domain={domain} quantity={quantity}")
    client = (client_func or (lambda proxy=None: _cfworker_client(email_cfg, proxy=proxy)))(proxy=getattr(args, "proxy", None))
    emails = client.create_mailboxes(count=quantity, domain=domain)
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


def _fetch_cfworker_messages(mailbox, limit=25, proxy=None, email_cfg=None, client_func=None):
    email_cfg = email_cfg or {}
    client_func = client_func or (lambda proxy=None: _cfworker_client(email_cfg, proxy=proxy))
    if not _cfworker_poll_proxy_enabled(email_cfg):
        return client_func(proxy=None).fetch_messages(mailbox.email, limit=limit)
    try:
        return client_func(proxy=proxy).fetch_messages(mailbox.email, limit=limit)
    except Exception as exc:
        if not proxy or not _cfworker_direct_fallback_enabled(email_cfg):
            raise
        print(f"[cfworker proxy poll error: {exc}; retrying direct]")
        return client_func(proxy=None).fetch_messages(mailbox.email, limit=limit)


def _latest_cfworker_otp_candidate(
    mailbox,
    keyword="",
    issued_after_unix=0,
    seen_message_id="",
    proxy=None,
    fetch_messages_func=None,
    excluded_otps=None,
):
    fetch_messages_func = fetch_messages_func or (lambda mb, **kwargs: [])
    excluded_otps = {str(value or "").strip() for value in (excluded_otps or ())}
    for msg in fetch_messages_func(mailbox, proxy=proxy):
        candidate = _email_otp_candidate(mailbox, msg, keyword=keyword, issued_after_unix=issued_after_unix)
        if seen_message_id and _message_id(msg) == seen_message_id:
            received_ts = int((candidate or {}).get("received_ts") or 0)
            if not issued_after_unix or not received_ts or received_ts < issued_after_unix:
                continue
        if candidate and candidate.get("otp") not in excluded_otps:
            return candidate
    return None


def _poll_cfworker_otp(
    mailbox,
    subject_keyword="",
    timeout=300,
    issued_after_unix=0,
    proxy=None,
    email_cfg=None,
    otp_poll_interval_func=None,
    fetch_messages_func=None,
    excluded_otps=None,
):
    keyword = (subject_keyword or "").lower()
    seen_message_id = getattr(mailbox, "seen_message_id", "")

    def _fetch_candidate():
        return _latest_cfworker_otp_candidate(
            mailbox,
            keyword=keyword,
            issued_after_unix=issued_after_unix,
            seen_message_id=seen_message_id,
            proxy=proxy,
            fetch_messages_func=fetch_messages_func,
            excluded_otps=excluded_otps,
        )

    def _is_newer(a, b):
        return (a or {}).get("id") != (b or {}).get("id") and a is not None

    from .mailbox_poll import _poll_otp_with_settle
    return _poll_otp_with_settle(
        _fetch_candidate,
        timeout=timeout,
        interval=(otp_poll_interval_func or (lambda: 2.0))(),
        settle_seconds=_cfworker_otp_settle_seconds(email_cfg or {}),
        log_prefix="mailbox poll",
        is_newer=_is_newer,
    )
