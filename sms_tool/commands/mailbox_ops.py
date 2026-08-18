"""CLI boundary for mailbox inspection commands (--view-inbox, --gmail-send).

Mailbox domain modules own fetching/sending behavior; this module only
resolves the target mailbox from argparse values and formats JSON output.
``MailboxCommandContext`` keeps the legacy CLI's replaceable hooks explicit.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import mailbox_from_explicit_args, public_mail_message


@dataclass(frozen=True)
class MailboxCommandContext:
    """Legacy CLI hooks required by mailbox command orchestration."""

    upsert_account: Callable[..., Any]


def view_inbox(args: Any, ctx: MailboxCommandContext) -> None:
    from ..codex_oauth import _mailbox_from_data
    from ..desktop_ipc import emit_result
    from ..mailbox import _fetch_mailbox_messages, _mailbox_from_config
    from ..session_refresh import _load_seed_session

    def output(payload):
        emit_result(payload, enabled=bool(getattr(args, "desktop_ipc", False)))

    with contextlib.redirect_stdout(sys.stderr):
        data, json_path = _load_seed_session(email=args.email or "", session_file=args.session_file or "")
        mailbox = mailbox_from_explicit_args(args)
        if mailbox is None:
            mailbox = _mailbox_from_data(data)
        if mailbox is None and (getattr(args, "remail_token", None) or os.environ.get("REMAIL_SERVICE_TOKEN")):
            mailbox = _mailbox_from_config(args)
    if mailbox is None:
        output({
            "ok": False,
            "email": args.email or data.get("email", ""),
            "error": "missing_mailbox_credentials",
        })
        raise SystemExit(2)
    try:
        original_mailbox_token = str(getattr(mailbox, "token", "") or "")
        with contextlib.redirect_stdout(sys.stderr):
            messages = _fetch_mailbox_messages(
                mailbox,
                limit=max(1, min(int(args.inbox_limit or 20), 100)),
                proxy=args.proxy,
                include_body=True,
            )
            refreshed_mailbox_token = str(getattr(mailbox, "token", "") or "")
            if (
                getattr(mailbox, "provider", "") == "remail"
                and refreshed_mailbox_token
                and refreshed_mailbox_token != original_mailbox_token
            ):
                mailbox_data = data.get("mailbox") if isinstance(data.get("mailbox"), dict) else {}
                mailbox_data.update({
                    "email": mailbox.email,
                    "provider": "remail",
                    "token": refreshed_mailbox_token,
                    "order_no": str(getattr(mailbox, "order_no", "") or mailbox_data.get("order_no") or ""),
                    "purchase_id": str(getattr(mailbox, "purchase_id", "") or mailbox_data.get("purchase_id") or ""),
                })
                data["mailbox"] = mailbox_data
                if json_path:
                    Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                ctx.upsert_account(data, json_path=json_path)
    except Exception as exc:
        output({
            "ok": False,
            "email": mailbox.email,
            "provider": mailbox.provider,
            "error": str(exc),
        })
        raise SystemExit(3)
    output({
        "ok": True,
        "email": mailbox.email,
        "provider": mailbox.provider,
        "messages": [public_mail_message(item) for item in messages],
    })


def gmail_send(args: Any) -> None:
    from ..codex_oauth import _mailbox_from_data
    from ..mailbox import _mailbox_from_config
    from ..mailbox_gmail import is_gmail_mailbox, send_gmail_message
    from ..session_refresh import _load_seed_session

    with contextlib.redirect_stdout(sys.stderr):
        data, _ = _load_seed_session(email=args.email or "", session_file=args.session_file or "")
        mailbox = mailbox_from_explicit_args(args)
        if mailbox is None:
            mailbox = _mailbox_from_data(data)
        if mailbox is None:
            mailbox = _mailbox_from_config(args)
    if mailbox is None:
        print(json.dumps({
            "ok": False,
            "email": args.email or data.get("email", ""),
            "error": "missing_gmail_mailbox_credentials",
        }, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    if not is_gmail_mailbox(mailbox):
        print(json.dumps({
            "ok": False,
            "email": mailbox.email,
            "provider": mailbox.provider,
            "error": "selected_mailbox_is_not_gmail",
        }, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    recipients = args.gmail_send_to or ""
    if args.gmail_send_self or not str(recipients or "").strip():
        recipients = mailbox.email
    subject = args.gmail_send_subject or "GPT-Register-Tool Gmail test"
    body = args.gmail_send_body or "This is a Gmail test message sent by GPT-Register-Tool."
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = send_gmail_message(
                mailbox,
                recipients,
                subject=subject,
                text_body=body,
                html_body=args.gmail_send_html or "",
            )
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "email": mailbox.email,
            "provider": mailbox.provider,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
