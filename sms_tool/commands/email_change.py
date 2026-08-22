"""CLI adapter for the protocol email-change workflow."""

from __future__ import annotations

from typing import Any

from .helpers import read_email_file, unique_emails


def run_change_email(args: Any, *, load_accounts, change_email_batch, request_type, emit_result) -> None:
    provider = str(args.change_email_provider or "").strip()
    if not provider:
        raise SystemExit("--change-email-provider is required with --change-email")

    emails = read_email_file(args.email_file)
    if args.email:
        emails.insert(0, args.email)
    emails = unique_emails(emails)
    accounts = load_accounts(emails)
    if emails and len(accounts) != len(emails):
        found = {str(item.get("email") or "").strip().lower() for item in accounts}
        missing = [email for email in emails if email.strip().lower() not in found]
        payload = {"ok": False, "total": len(emails), "error": "account_not_found", "missing": missing}
        emit_result(payload, enabled=bool(args.desktop_ipc))
        raise SystemExit(3)
    if not accounts:
        raise SystemExit("--change-email requires --email or --email-file")

    request = request_type(
        provider=provider,
        target_mailbox_file=args.change_email_mailbox_file or args.mailbox_file or "",
        workers=args.change_email_workers or args.workers,
        timeout=args.change_email_timeout,
        otp_timeout=args.change_email_otp_timeout,
        proxy=args.proxy,
        service_mode=args.change_email_service_mode,
        smailr_domain=args.change_email_smailr_domain or "",
        cfworker_domain=args.cfworker_domain or "",
    )
    try:
        result = change_email_batch(accounts, request)
    except Exception as exc:
        result = {
            "ok": False,
            "total": len(accounts),
            "success": 0,
            "failed": len(accounts),
            "error": str(exc)[:300],
            "results": [],
        }
    emit_result(result, enabled=bool(args.desktop_ipc))
    if not result.get("ok"):
        raise SystemExit(3)
