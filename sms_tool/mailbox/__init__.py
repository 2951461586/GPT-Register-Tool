"""Mailbox package: provider wiring, proxy resolution and OTP polling.

The implementation is :mod:`.router` (the former ``sms_tool/mailbox/router.py``).  This
``__init__`` is a **lazy** facade, for one reason:

``providers/mailbox_*.py`` import ``..mailbox.<submodule>``, and importing a
submodule imports this package first.  An eager ``from .router import ...`` here
would therefore run the router -> providers -> ``sms_tool.mailbox.router`` chain while
``sms_tool.mailbox.router`` is still half-initialised -- the classic package facade
cycle.  The same shape was removed from ``auth_flow`` and ``upi_link`` on
2026-10-21 (see ``tests/governance/test_lazy_facades.py``); here it is avoided
before it can appear rather than after.

Consequences worth knowing:

* ``from sms_tool.mailbox.router import poll_email_otp`` still works, and resolves on
  first use.
* ``patch("sms_tool.mailbox.router.poll_email_otp", ...)`` is **inert**: consumers bind
  their own name.  The live seams are ``sms_tool.mailbox.router.router.<name>`` and the
  ``CFG`` hook the router documents.
* the ``TYPE_CHECKING`` block below is mandatory, not cosmetic.  Without it every
  consumer reports ``"X" is unknown import symbol`` and ``pyright_ratchet`` fails
  on files whose baseline is 0.  The names are written ``X as X`` because that is
  what marks them as re-exports instead of 22 unused imports.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .router import (
        MailboxAccount as MailboxAccount,
        MailboxTokenExpiredError as MailboxTokenExpiredError,
        _email_otp_candidate as _email_otp_candidate,
        _extract_otp_from_text as _extract_otp_from_text,
        _parse_chatai_mailbox_file as _parse_chatai_mailbox_file,
        candidate_is_newer as candidate_is_newer,
        email_otp_settle_seconds as email_otp_settle_seconds,
        ensure_mailbox_account as ensure_mailbox_account,
        fetch_mailbox_messages as fetch_mailbox_messages,
        fetch_mailbox_messages_local as fetch_mailbox_messages_local,
        latest_email_otp_candidate as latest_email_otp_candidate,
        load_mailbox_pool as load_mailbox_pool,
        mailbox_email_config as mailbox_email_config,
        mailbox_from_config as mailbox_from_config,
        mailbox_has_inbox_credentials as mailbox_has_inbox_credentials,
        mailbox_proxy_candidates as mailbox_proxy_candidates,
        otp_poll_interval as otp_poll_interval,
        poll_email_otp as poll_email_otp,
        provider_otp_issued_after as provider_otp_issued_after,
        remail_enabled as remail_enabled,
        resolve_mailbox_proxy as resolve_mailbox_proxy,
        snapshot_mailbox_message as snapshot_mailbox_message,
    )

#: Every name the router exposes to the rest of the repository.  Kept as data so
#: ``__getattr__`` stays one line and the list is greppable.
_LAZY_NAMES: frozenset[str] = frozenset(
    {
        "MailboxAccount",
        "MailboxTokenExpiredError",
        "_email_otp_candidate",
        "_extract_otp_from_text",
        "_parse_chatai_mailbox_file",
        "candidate_is_newer",
        "email_otp_settle_seconds",
        "ensure_mailbox_account",
        "fetch_mailbox_messages",
        "fetch_mailbox_messages_local",
        "latest_email_otp_candidate",
        "load_mailbox_pool",
        "mailbox_email_config",
        "mailbox_from_config",
        "mailbox_has_inbox_credentials",
        "mailbox_proxy_candidates",
        "otp_poll_interval",
        "poll_email_otp",
        "provider_otp_issued_after",
        "remail_enabled",
        "resolve_mailbox_proxy",
        "snapshot_mailbox_message",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve a router name on first use (PEP 562)."""
    if name not in _LAZY_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(".router", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_NAMES})
