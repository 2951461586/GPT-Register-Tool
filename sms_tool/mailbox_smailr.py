"""Smailr integration for the email-registration pipeline.

Smailr is a disposable-email SaaS.  Unlike the providers that hook into a
pre-existing inbox (Gmail/Graph/Cloudflare/ReMail/icloud+), Smailr *creates*
the inbox via its REST API first and then polls it for an OTP email.

Storage model::

    MailboxAccount.email       -> smailr mailbox email address
    MailboxAccount.token       -> smailr mailbox id (``/mailboxes/{id}``)
    MailboxAccount.source      -> response dict of the create call (JSON repr)
    MailboxAccount.provider    -> "smailr"

This module exposes four helpers plus the two low-level configs required by
the strategy registry (``_fetch_smailr_messages`` / ``_poll_smailr_otp``).
They are wired into ``_fetch_mailbox_messages`` / ``_poll_email_otp`` via
``mailbox._register_mailbox_strategies`` when ``email_registration.smailr``
is configured.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

from .config import CFG
from .mailbox_types import MailboxAccount


def _email_cfg() -> dict:
    return CFG.get("email_registration") or {}


def _smailr_cfg() -> dict:
    cfg = _email_cfg().get("smailr")
    return cfg if isinstance(cfg, dict) else {}


def _smailr_api_key() -> str:
    return str(os.environ.get("SMAILR_API_KEY") or _smailr_cfg().get("api_key") or "").strip()


def _smailr_base_url() -> str:
    from .providers.smailr_mailbox import _normalize_base_url
    return _normalize_base_url(str(_smailr_cfg().get("base_url") or "https://smailr.com"))


def _smailr_timeout() -> int:
    try:
        return max(1, int(_smailr_cfg().get("timeout") or 30))
    except (TypeError, ValueError):
        return 30


def _smailr_default_domain() -> str:
    domains = _smailr_cfg().get("domains") or []
    if isinstance(domains, str):
        domains = [domains]
    for domain in domains:
        domain = str(domain or "").strip().lstrip("@").lower()
        if "." in domain:
            return domain
    return str(_smailr_cfg().get("default_domain") or "").strip().lstrip("@").lower() or "smailr.com"


def _smailr_proxy() -> str:
    return str(_smailr_cfg().get("proxy") or "").strip()


def _smailr_client(proxy: str | None = None):
    from .providers.smailr_mailbox import SmailrClient
    merged_proxy = proxy or _smailr_proxy() or None
    return SmailrClient(
        api_key=_smailr_api_key(),
        base_url=_smailr_base_url(),
        timeout=_smailr_timeout(),
        proxy=merged_proxy,
    )


def _smailr_enabled() -> bool:
    return bool(_smailr_api_key())


def _smailr_extract_id_and_email(response: Any) -> tuple[str, str]:
    """Given a Smailr ``POST /mailboxes`` response, return ``(id, email)``."""
    if not isinstance(response, dict):
        return "", ""
    nested = response.get("data")
    if isinstance(nested, dict):
        response = {**response, **nested}
    elif isinstance(nested, list):
        for item in nested:
            mb_id, email = _smailr_extract_id_and_email(item)
            if mb_id or email:
                return mb_id, email
    mb_id = str(response.get("id") or response.get("mailbox_id") or "").strip()
    email = (
        response.get("email")
        or response.get("address")
        or response.get("address_full")
        or ""
    )
    if not isinstance(email, str):
        email = ""
    if not email and response.get("local_part"):
        local = str(response["local_part"]).strip().lower()
        domain = str(response.get("domain") or response.get("domain_name") or _smailr_default_domain() or "").lower().lstrip("@")
        if local and domain:
            email = f"{local}@{domain}"
    return mb_id, email.strip().lower()


def _random_local_part(length: int = 10) -> str:
    return secrets.token_hex(length // 2 + length % 2)[:length]


# ── Public API ─────────────────────────────────────────────────────────────

def create_smailr_mailboxes(
    count: int = 1,
    *,
    local_part: str = "",
    domain: str = "",
    api_key: str = "",
    base_url: str = "",
    proxy: str | None = None,
) -> list[MailboxAccount]:
    """Create *count* fresh disposable mailboxes via Smailr.

    Each ``MailboxAccount`` carries ``token=smailr_id`` and ``source=<raw
    create-response>`` so callers can re-open the inbox later without
    re-creating it.
    """
    if count < 1:
        return []
    if not api_key:
        api_key = _smailr_api_key()
    if not api_key:
        raise RuntimeError("smailr.api_key is required (config: email_registration.smailr.api_key or env SMAILR_API_KEY)")
    if not base_url:
        base_url = _smailr_base_url()
    cfg_domain = _smailr_default_domain()
    domain = str(domain or cfg_domain or "smailr.com").strip().lstrip("@").lower()

    from .providers.smailr_mailbox import SmailrClient
    client = SmailrClient(
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
    )

    local_part_hint = str(local_part or "").strip().lower().split("@")[0]

    accounts: list[MailboxAccount] = []
    for _index in range(count):
        hint = local_part_hint or _random_local_part()
        try:
            resp = client.create_mailbox(local_part=hint)
        except Exception as exc:
            raise RuntimeError(f"smailr.create_mailbox failed: {exc}") from exc

        mb_id, email = _smailr_extract_id_and_email(resp)
        if not email:
            # fall back to probing the list endpoint if the create call didn't
            # surface the final address.
            try:
                for mb in client.list_mailboxes():
                    cand_id, cand_email = _smailr_extract_id_and_email(mb)
                    if cand_email:
                        mb_id, email = cand_id, cand_email
                        resp = mb
                        break
            except Exception:
                pass
        if not mb_id:
            raise RuntimeError(f"smailr.create_mailbox: missing id in response {json.dumps(resp, default=str)[:300]}")

        accounts.append(MailboxAccount(
            email=email,
            token=mb_id,
            source=json.dumps(resp, ensure_ascii=False, default=str),
            provider="smailr",
        ))
    return accounts


def _fetch_smailr_messages(
    mailbox: MailboxAccount,
    limit: int = 25,
    proxy: str | None = None,
    *,
    email_cfg: dict | None = None,
) -> list[dict]:
    """Retrieve up to *limit* shaped mails for a Smailr mailbox."""
    from .providers.smailr_mailbox import fetch_messages
    mb_id = mailbox.token or ""
    if not mb_id:
        raise ValueError("smailr mailbox.token (id) is empty — cannot fetch messages")
    return fetch_messages(
        _smailr_client(proxy=proxy),
        mb_id,
        mailbox.email or "",
        limit=limit,
    )


def _latest_smailr_otp_candidate(
    mailbox: MailboxAccount,
    *,
    keyword: str = "",
    issued_after_unix: int = 0,
    seen_message_id: str = "",
    proxy: str | None = None,
    excluded_otps: Any = None,
) -> dict | None:
    from .mail_otp import _email_otp_candidate, _message_id
    excluded_text = {str(value or "").strip() for value in (excluded_otps or ())}
    for msg in _fetch_smailr_messages(mailbox, limit=25, proxy=proxy):
        if seen_message_id and _message_id(msg) == seen_message_id:
            continue
        candidate = _email_otp_candidate(mailbox, msg, keyword=keyword, issued_after_unix=issued_after_unix)
        if candidate and candidate.get("otp") not in excluded_text:
            return candidate
    return None


def _poll_smailr_otp(
    mailbox: MailboxAccount,
    *,
    subject_keyword: str = "",
    timeout: int = 300,
    issued_after_unix: int = 0,
    proxy: str | None = None,
    excluded_otps: Any = None,
) -> str | None:
    from .providers.smailr_mailbox import poll_otp
    mb_id = mailbox.token or ""
    if not mb_id:
        raise ValueError("smailr mailbox.token (id) is empty — cannot poll for OTP")
    return poll_otp(
        _smailr_client(proxy=proxy),
        mb_id,
        mailbox.email or "",
        subject_keyword=subject_keyword,
        timeout=timeout,
        issued_after_unix=issued_after_unix,
        excluded_otps=excluded_otps,
        log_prefix="smailr poll",
    )
