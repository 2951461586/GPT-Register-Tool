"""CLI boundary for account management commands (links, export, import, quota).

Storage/import/export domain modules own behavior; this module only translates
``argparse`` values into those domain calls.  ``AccountCommandContext`` keeps
the legacy CLI's replaceable hooks explicit so tests can keep patching
``sms_tool.cli`` symbols.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import read_email_file, unique_emails


@dataclass(frozen=True)
class AccountCommandContext:
    """Legacy CLI hooks required by account command orchestration."""

    list_paypal_accounts: Callable[..., list[dict[str, Any]]]
    get_paypal_url: Callable[[str], str]


def import_sessions_kwargs(args: Any, *, include_workers: bool = True) -> dict[str, Any]:
    """Shared ``import_account_session(s)`` keyword arguments built from args."""
    kwargs: dict[str, Any] = {
        "export_dir": args.codex_export_dir or "",
        "refresh": not args.no_session_refresh,
        "proxy": args.proxy,
        "timeout": args.refresh_timeout,
        "cpa_api_url": args.cpa_api_url or "",
        "cpa_api_token": args.cpa_api_token or "",
        "sub2api_url": args.sub2api_url or "",
        "sub2api_token": args.sub2api_token or "",
        "sub2api_email": args.sub2api_email or "",
        "sub2api_password": args.sub2api_password or "",
        "sub2api_group": args.sub2api_group or "",
        "sub2api_group_ids": args.sub2api_group_ids or "",
        "sub2api_proxy": args.sub2api_proxy or "",
        "sub2api_proxy_id": args.sub2api_proxy_id,
        "sub2api_priority": args.sub2api_priority,
        "sub2api_concurrency": args.sub2api_concurrency,
        "sub2api_auth_mode": getattr(args, "sub2api_auth_mode", "") or "",
        "sub2api_verify_after_import": getattr(args, "sub2api_verify_after_import", None),
    }
    if include_workers:
        kwargs["workers"] = args.workers
    return kwargs


def import_registered_accounts(args: Any, emails: list[str]) -> None:
    from ..import_targets import import_account_sessions

    emails = [str(email or "").strip() for email in emails if str(email or "").strip()]
    if not emails:
        print("[!] No successful registered account to import into CPA/SUB2API")
        return
    result = import_account_sessions(args.import_target, emails, **import_sessions_kwargs(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def print_paypal_links(email: str, ctx: AccountCommandContext) -> None:
    rows = ctx.list_paypal_accounts(email=email or "")
    if not rows:
        print("[*] No payment records found")
        return
    for row in rows:
        print(json.dumps({
            "email": row.get("email", ""),
            "payment_method": row.get("payment_method", ""),
            "paypal_url": row.get("paypal_url", ""),
            "paypal_status": row.get("paypal_status", ""),
            "refresh_token_status": row.get("refresh_token_status", ""),
            "json_path": row.get("json_path", ""),
        }, ensure_ascii=False))


def open_paypal_link(email: str, ctx: AccountCommandContext) -> None:
    email = (email or "").strip()
    if not email:
        print("[Error] --email is required with --open-paypal-link")
        return
    url = ctx.get_paypal_url(email)
    if not url:
        print(f"[Error] no PayPal URL found for {email}")
        return
    print(url)
    webbrowser.open(url)


def mark_paypal_status(args: Any, ctx: AccountCommandContext) -> None:
    from ..storage import mark_paypal_status as storage_mark_paypal_status

    status = args.mark_paypal_status
    emails = read_email_file(args.email_file)
    email = (args.email or "").strip()
    if not emails and email:
        emails = [email]
    if not emails:
        print("[Error] --email or --email-file is required with --mark-paypal-status")
        return

    results = []
    for item_email in emails:
        if storage_mark_paypal_status(item_email, status=status):
            print(f"[*] Payment status updated: {item_email} -> {status}")
            result = {"ok": True, "email": item_email, "paypal_status": status}
        else:
            print(f"[Error] account not found: {item_email}")
            result = {"ok": False, "email": item_email, "error": "account_not_found"}
        results.append(result)

    if args.import_cpa:
        from ..import_targets import import_account_sessions

        import_emails = [result["email"] for result in results if result.get("ok")]
        import_result = import_account_sessions(
            args.import_target,
            import_emails,
            **import_sessions_kwargs(args),
        )
        print(json.dumps(import_result, ensure_ascii=False, indent=2))
        if any(not result.get("ok") for result in results) or not import_result.get("ok"):
            raise SystemExit(3)
    elif args.export_codex_json:
        from ..codex_export import export_codex_sessions

        export_emails = [result["email"] for result in results if result.get("ok")]
        export_result = export_codex_sessions(
            export_emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
        print(json.dumps(export_result, ensure_ascii=False, indent=2))
        if any(not result.get("ok") for result in results) or not export_result.get("ok"):
            raise SystemExit(3)
    elif any(not result.get("ok") for result in results):
        raise SystemExit(3)


def refresh_session(args: Any) -> None:
    from ..session_refresh import refresh_session as refresh_auth_session

    result = refresh_auth_session(
        email=args.email or "",
        session_file=args.session_file or "",
        timeout=args.refresh_timeout,
        proxy=args.proxy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def export_codex_json(args: Any, ctx: AccountCommandContext) -> None:
    from ..codex_export import export_codex_session, export_codex_sessions

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if emails:
        result = export_codex_sessions(
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
    elif args.session_file:
        result = export_codex_session(
            session_file=args.session_file,
            export_dir=args.codex_export_dir or "",
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
    else:
        rows = [
            row for row in ctx.list_paypal_accounts()
            if str(row.get("paypal_status") or "").strip().lower() == "completed"
        ]
        emails = [row.get("email", "") for row in rows if row.get("email")]
        result = export_codex_sessions(
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def importable_account_rows(ctx: AccountCommandContext) -> list[dict[str, Any]]:
    rows = []
    for row in ctx.list_paypal_accounts():
        email = str(row.get("email") or "").strip()
        access_token = str(row.get("access_token") or "").strip()
        if email and access_token:
            rows.append(row)
    return rows


def import_cpa(args: Any, ctx: AccountCommandContext) -> None:
    from ..import_targets import import_account_session, import_account_sessions

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if emails:
        result = import_account_sessions(
            args.import_target,
            emails,
            **import_sessions_kwargs(args),
        )
    elif args.session_file:
        result = import_account_session(
            args.import_target,
            session_file=args.session_file,
            **import_sessions_kwargs(args, include_workers=False),
        )
    else:
        rows = importable_account_rows(ctx)
        emails = [row.get("email", "") for row in rows if row.get("email")]
        result = import_account_sessions(
            args.import_target,
            emails,
            **import_sessions_kwargs(args),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)
    try:
        from ..cpa_import import refresh_cpa_quota_statuses
        quota_emails = emails if emails else [str(item.get("email") or "") for item in (result.get("results") or []) if isinstance(item, dict)]
        if not quota_emails and isinstance(result, dict) and result.get("email"):
            quota_emails = [str(result.get("email") or "")]
        quota_result = refresh_cpa_quota_statuses(
            emails=quota_emails,
            workers=max(1, int(args.quota_workers or args.workers or 4)),
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
            timeout=max(5, int(args.refresh_timeout or 30)),
        )
        if quota_result.get("total", 0):
            print("[*] CPA quota refreshed after import:")
            print(json.dumps(quota_result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"[*] CPA quota refresh after import skipped: {exc}")


def check_promotion(args: Any, ctx: AccountCommandContext) -> None:
    from ..account_promotion import refresh_promotion_statuses

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    emails = unique_emails(emails)
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in ctx.list_paypal_accounts()]
    result = refresh_promotion_statuses(
        emails=emails,
        workers=max(1, int(args.quota_workers or args.workers or 4)),
        proxy=args.proxy,
        timeout=max(5, int(args.refresh_timeout or 20)),
    )
    from ..desktop_ipc import emit_result

    if bool(getattr(args, "desktop_ipc", False)):
        emit_result(result, enabled=True)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def refresh_cpa_quota(args: Any, ctx: AccountCommandContext) -> None:
    from ..account_recovery import refresh_local_quota_statuses
    from ..cpa_import import refresh_cpa_quota_statuses

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    emails = unique_emails(emails)
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in ctx.list_paypal_accounts()]
    quota_mode = "local" if getattr(args, "refresh_local_quota", False) else str(getattr(args, "quota_mode", "local") or "local")
    if quota_mode == "cpa":
        result = refresh_cpa_quota_statuses(
            emails=emails,
            workers=max(1, int(args.quota_workers or args.workers or 4)),
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
            timeout=max(5, int(args.refresh_timeout or 30)),
        )
    else:
        result = refresh_local_quota_statuses(
            emails=emails,
            workers=max(1, int(args.quota_workers or args.workers or 4)),
            proxy=args.proxy,
            timeout=max(5, int(args.refresh_timeout or 30)),
            relogin_on_401=bool(getattr(args, "quota_auto_relogin", False)),
            relogin_timeout=max(30, int(getattr(args, "quota_relogin_timeout", 180) or 180)),
            relogin_mode=str(getattr(args, "scan_relogin_mode", "auto") or "auto"),
        )
        fallback_emails = [
            item.get("email")
            for item in result.get("results", [])
            if not item.get("ok")
            and str((item.get("probe") or {}).get("status") or "").strip().lower() != "account_deactivated"
            and not bool(
                (item.get("relogin") if isinstance(item.get("relogin"), dict) else {}).get("terminal")
            )
            and "account_deactivated" not in str(
                (item.get("relogin") if isinstance(item.get("relogin"), dict) else {}).get("error") or ""
            ).lower()
        ]
        if quota_mode == "auto" and fallback_emails:
            fallback = refresh_cpa_quota_statuses(
                emails=fallback_emails,
                workers=max(1, int(args.quota_workers or args.workers or 4)),
                api_url=args.cpa_api_url or "",
                api_token=args.cpa_api_token or "",
                timeout=max(5, int(args.refresh_timeout or 30)),
            )
            result["fallback_cpa"] = fallback
            result["ok"] = bool(fallback.get("ok"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def quota_usage(args: Any) -> None:
    """Fetch wham/usage 5h/7d quota for a single account and return structured JSON."""
    from ..account_liveness import probe_account_liveness
    from ..storage import get_account_record

    email = (getattr(args, "email", None) or "").strip()
    if not email:
        print(json.dumps({"ok": False, "error": "missing --email"}))
        raise SystemExit(1)

    account = get_account_record(email)
    if not account:
        print(json.dumps({"ok": False, "error": "account_not_found", "email": email}))
        raise SystemExit(1)

    proxy = getattr(args, "proxy", None) or None
    timeout = max(5, int(getattr(args, "refresh_timeout", None) or 30))
    probe = probe_account_liveness(account, proxy=proxy, timeout=timeout)
    result = {
        "ok": probe.get("ok", False),
        "email": email,
        "status": probe.get("status", "unknown"),
        "quota_status": probe.get("quota_status", ""),
        "wham_usage": probe.get("wham_usage"),
        "status_code": probe.get("status_code"),
        "error": probe.get("error", ""),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(3)


def convert_session_json(args: Any) -> None:
    from ..session_converter import convert_json_file

    result = convert_json_file(args.convert_session_json, fmt=args.convert_format)
    output_text = result.get("outputText") or ""
    if args.convert_output:
        target = Path(args.convert_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output_text, encoding="utf-8")
        print(json.dumps({
            "ok": bool(result.get("converted")),
            "format": args.convert_format,
            "converted": len(result.get("converted") or []),
            "skipped": result.get("skipped") or [],
            "output": str(target),
        }, ensure_ascii=False, indent=2))
    else:
        print(output_text)
        if result.get("skipped"):
            print(json.dumps({"skipped": result.get("skipped")}, ensure_ascii=False, indent=2), file=sys.stderr)
    if not result.get("converted"):
        raise SystemExit(3)
