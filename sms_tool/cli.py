import argparse
import json
import os
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import CFG
from .codex_export import export_codex_session, export_codex_sessions
from .cpa_import import import_cpa_session, import_cpa_sessions
from .mailbox import _load_mailbox_pool, _luckmail_enabled
from .paypal_auto import auto_pay
from .paypal_links import regenerate_paypal_link
from .paths import output_dir
from .registration import _build_session_file, run_batch, run_email
from .session_refresh import refresh_session
from .storage import database_path, get_paypal_url, list_paypal_accounts, mark_paypal_status, rebuild_from_session_dir, upsert_account

def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="ChatGPT Email Registration + PayPal link generation")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers for batch registration/link regeneration")
    parser.add_argument("--password", default=None, help="Use a specific password")
    parser.add_argument("--email", default=None, help="Mailbox email address")
    parser.add_argument("--email-password", default=None, help="Mailbox password")
    parser.add_argument("--email-refresh-token", default=None, help="Mailbox refresh token")
    parser.add_argument("--email-access-token", default=None, help="Mailbox access token")
    parser.add_argument("--luckmail-token", default=None, help="LuckMail purchased mailbox token")
    parser.add_argument("--buy-luckmail-mailbox", action="store_true", help="Buy LuckMail long-term mailbox before registration")
    parser.add_argument("--luckmail-purchase-project", default=None, help="LuckMail purchase project code, default openai")
    parser.add_argument("--luckmail-purchase-email-type", default=None, help="LuckMail purchase email type, default ms_imap")
    parser.add_argument("--luckmail-purchase-domain", default=None, help="LuckMail purchase domain, default outlook.com")
    parser.add_argument("--mailbox-file", default=None, help="Mailbox token file: email---password---refresh_token---access_token---0")
    parser.add_argument("--chatai-mailbox-file", default=None, help="Chatai mailbox token file: email----password----client_id----refresh_token")
    parser.add_argument("--skip-paypal-link", action="store_true", help="Do not generate PayPal payment link after registration")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rebuild-sqlite", action="store_true", help="Rebuild SQLite account index from session JSON files")
    parser.add_argument("--list-paypal-links", action="store_true", help="List saved PayPal payment links")
    parser.add_argument("--open-paypal-link", action="store_true", help="Open saved PayPal payment link for --email")
    parser.add_argument("--mark-paypal-status", default=None, help="Update saved PayPal status for --email")
    parser.add_argument("--export-codex-json", action="store_true", help="Export paid account session as Codex JSON")
    parser.add_argument("--import-cpa", action="store_true", help="Import an existing AT-only session JSON into CPA")
    parser.add_argument("--codex-export-dir", default=None, help="Directory for Codex JSON exports")
    parser.add_argument("--cpa-api-url", default=None, help="CPA API base URL, defaults to cpa/cpa_mode.api_url in config.json")
    parser.add_argument("--cpa-api-token", default=None, help="CPA API token, defaults to cpa/cpa_mode.api_token in config.json")
    parser.add_argument("--no-session-refresh", action="store_true", help="Do not refresh session before Codex JSON export")
    parser.add_argument("--regenerate-paypal-link", action="store_true", help="Regenerate PayPal link for --email and update SQLite/session JSON")
    parser.add_argument("--refresh-session", action="store_true", help="Refresh ChatGPT auth session with protocol requests")
    parser.add_argument("--session-file", default=None, help="Session JSON path for --refresh-session or --regenerate-paypal-link")
    parser.add_argument("--email-file", default=None, help="One email per line for batch PayPal link regeneration")
    parser.add_argument("--refresh-timeout", type=int, default=300, help="Seconds to wait for interactive auth refresh")
    parser.add_argument("--browser-refresh-session", action="store_true", help="Use the old browser-based refresh flow")
    parser.add_argument("--headless-refresh", action="store_true", help="Run browser refresh headless; visible browser is default")
    parser.add_argument("--auto-pay", action="store_true", help="Automate PayPal payment (reverse protocol first, browser fallback)")
    parser.add_argument("--auto-pay-reverse-only", action="store_true", help="Use reverse protocol only, no browser fallback")
    parser.add_argument("--auto-pay-headless", action="store_true", help="Run auto-pay browser headless")
    parser.add_argument("--auto-pay-timeout", type=int, default=180, help="Seconds to wait for auto-pay completion")
    parser.add_argument("--batch-auto-pay", action="store_true", help="Run auto-pay for all pending accounts in SQLite")
    parser.add_argument("--batch-auto-pay-limit", type=int, default=0, help="Max accounts to process in batch (0=all)")
    args = parser.parse_args()
    if not args.proxy:
        args.proxy = ((CFG.get("proxy") or {}).get("default") or "").strip() or None

    base_dir = args.output_dir or str(output_dir(CFG))
    if args.rebuild_sqlite:
        count = rebuild_from_session_dir(base_dir)
        print(f"[*] SQLite rebuilt: {database_path()} ({count} account record(s))")
        return
    if args.list_paypal_links:
        _print_paypal_links(args.email)
        return
    if args.open_paypal_link:
        _open_paypal_link(args.email)
        return
    if args.mark_paypal_status:
        _mark_paypal_status(args)
        return
    if args.import_cpa:
        _import_cpa(args)
        return
    if args.export_codex_json:
        _export_codex_json(args)
        return
    if args.regenerate_paypal_link:
        _regenerate_paypal_link(args)
        return
    if args.refresh_session:
        _refresh_session(args)
        return
    if args.auto_pay or args.auto_pay_reverse_only:
        _auto_pay(args)
        return
    if args.batch_auto_pay:
        _batch_auto_pay(args)
        return

    pipeline_started = time.time()
    mailbox_started = time.time()
    mailboxes = _load_mailbox_pool(args)
    mailbox_seconds = time.time() - mailbox_started
    explicit_mailbox_source = bool(
        args.chatai_mailbox_file
        or args.mailbox_file
        or args.email
        or args.email_refresh_token
        or args.email_access_token
        or args.luckmail_token
        or args.buy_luckmail_mailbox
    )
    if not mailboxes and explicit_mailbox_source:
        print("[Error] no mailbox account was found from the requested source; check the selected mailbox row or mailbox file format")
        raise SystemExit(2)
    if not mailboxes and not _luckmail_enabled():
        print("[Error] no mailbox account was found; set email_registration.token_file, pass --email/--email-refresh-token, or configure LuckMail")
        raise SystemExit(2)
    paypal_link = not args.skip_paypal_link and bool(CFG.get("paypal", {}).get("auto_generate", True))

    requested_count = max(1, int(args.count or 1))
    effective_count = requested_count
    if getattr(args, "buy_luckmail_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), LuckMail returned {effective_count}; registering returned mailboxes only.")
    elif mailboxes and requested_count > len(mailboxes):
        effective_count = len(mailboxes)
        print(f"[!] Requested {requested_count} account(s), but only {effective_count} mailbox(es) were loaded; registering loaded mailboxes only.")

    register_started = time.time()
    if effective_count > 1:
        results = run_batch(count=effective_count, proxy=args.proxy, mailboxes=mailboxes, paypal_link=paypal_link, workers=args.workers)
    else:
        mailbox = mailboxes[0] if mailboxes else None
        results = [run_email(proxy=args.proxy, password=args.password, mailbox=mailbox, paypal_link=paypal_link)]
    register_seconds = time.time() - register_started

    pipeline_seconds = time.time() - pipeline_started
    pipeline_timing = {
        "mailbox_load_seconds": round(mailbox_seconds, 2),
        "registration_batch_seconds": round(register_seconds, 2),
        "total_seconds": round(pipeline_seconds, 2),
    }
    for data in filter(None, results):
        data["pipeline_timing"] = pipeline_timing

    out_pattern = CFG.get("output", {}).get("filename_pattern", "session_{email}_{timestamp}.json")
    os.makedirs(base_dir, exist_ok=True)

    saved_count = 0
    db_saved_count = 0
    for data in filter(None, results):
        if not data.get("success", False):
            if upsert_account(data):
                db_saved_count += 1
            continue
        session_data = _build_session_file(data)
        if not session_data.get("access_token"):
            print("[!] Successful registration has no access_token; session file was not saved")
            continue
        identifier = (session_data.get("email") or session_data.get("phone") or "unknown").replace("+", "")
        safe_identifier = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", identifier)
        fname = out_pattern.format(email=safe_identifier, phone=safe_identifier, timestamp=int(time.time()))
        out_path = os.path.join(base_dir, fname)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
        if upsert_account(session_data, json_path=out_path):
            db_saved_count += 1
        saved_count += 1
        print(f"[*] Saved session: {out_path}")

    success_count = sum(1 for r in results if r and r.get("success"))
    print(f"[*] SQLite index: {database_path()} ({db_saved_count} record(s) upserted)")
    print(f"\n[*] Done. {success_count}/{effective_count} registered successfully, {saved_count} session file(s) saved.")
    paypal_failures = [
        r for r in results
        if r and r.get("success") and paypal_link and not ((r.get("paypal") or {}).get("ok") and (r.get("paypal") or {}).get("url"))
    ]
    if paypal_failures:
        for data in paypal_failures:
            paypal = data.get("paypal") or {}
            print(f"[Error] PayPal link generation failed for {data.get('email', '')}: {paypal.get('error', 'missing PayPal URL')}")
        raise SystemExit(3)


def _print_paypal_links(email=""):
    rows = list_paypal_accounts(email=email or "")
    if not rows:
        print("[*] No PayPal records found")
        return
    for row in rows:
        print(json.dumps({
            "email": row.get("email", ""),
            "paypal_url": row.get("paypal_url", ""),
            "paypal_status": row.get("paypal_status", ""),
            "refresh_token_status": row.get("refresh_token_status", ""),
            "json_path": row.get("json_path", ""),
        }, ensure_ascii=False))


def _open_paypal_link(email):
    email = (email or "").strip()
    if not email:
        print("[Error] --email is required with --open-paypal-link")
        return
    url = get_paypal_url(email)
    if not url:
        print(f"[Error] no PayPal URL found for {email}")
        return
    print(url)
    webbrowser.open(url)


def _mark_paypal_status(args):
    status = args.mark_paypal_status
    emails = _read_email_file(args.email_file)
    email = (args.email or "").strip()
    if not emails and email:
        emails = [email]
    if not emails:
        print("[Error] --email or --email-file is required with --mark-paypal-status")
        return

    results = []
    for item_email in emails:
        if mark_paypal_status(item_email, status=status):
            print(f"[*] PayPal status updated: {item_email} -> {status}")
            result = {"ok": True, "email": item_email, "paypal_status": status}
        else:
            print(f"[Error] account not found: {item_email}")
            result = {"ok": False, "email": item_email, "error": "account_not_found"}
        results.append(result)

    if args.import_cpa:
        import_emails = [result["email"] for result in results if result.get("ok")]
        import_result = import_cpa_sessions(
            import_emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
        )
        print(json.dumps(import_result, ensure_ascii=False, indent=2))
        if any(not result.get("ok") for result in results) or not import_result.get("ok"):
            raise SystemExit(3)
    elif args.export_codex_json:
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


def _refresh_session(args):
    result = refresh_session(
        email=args.email or "",
        session_file=args.session_file or "",
        timeout=args.refresh_timeout,
        headless=args.headless_refresh,
        browser=args.browser_refresh_session,
        proxy=args.proxy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _export_codex_json(args):
    emails = _read_email_file(args.email_file)
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
            row for row in list_paypal_accounts()
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


def _import_cpa(args):
    emails = _read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if emails:
        result = import_cpa_sessions(
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
        )
    elif args.session_file:
        result = import_cpa_session(
            session_file=args.session_file,
            export_dir=args.codex_export_dir or "",
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
        )
    else:
        rows = [
            row for row in list_paypal_accounts()
            if str(row.get("paypal_status") or "").strip().lower() == "completed"
        ]
        emails = [row.get("email", "") for row in rows if row.get("email")]
        result = import_cpa_sessions(
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _regenerate_paypal_link(args):
    email = (args.email or "").strip()
    emails = _read_email_file(args.email_file)
    if emails:
        workers = max(1, min(int(args.workers or 1), 4, len(emails)))
        print(f"[*] Batch regenerate PayPal links: {len(emails)} account(s), workers={workers}")
        results = []
        ordered = [None] * len(emails)

        def _run_one(index, item_email):
            print(f"[{index + 1}/{len(emails)}] Regenerating PayPal link: {item_email}")
            return index, regenerate_paypal_link(email=item_email, session_file="", proxy=args.proxy)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run_one, i, item_email) for i, item_email in enumerate(emails)]
            for future in as_completed(futures):
                index, result = future.result()
                ordered[index] = result

        results.extend(result for result in ordered if result is not None)
        ok_count = sum(1 for result in results if result.get("ok"))
        print(json.dumps({"ok": ok_count == len(emails), "total": len(emails), "success": ok_count, "failed": len(emails) - ok_count, "results": results}, ensure_ascii=False, indent=2))
        if ok_count != len(emails):
            raise SystemExit(3)
        return

    if not email and not args.session_file:
        print("[Error] --email or --session-file is required with --regenerate-paypal-link")
        return
    result = regenerate_paypal_link(email=email, session_file=args.session_file or "", proxy=args.proxy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _read_email_file(path):
    if not path:
        return []
    if not os.path.exists(path):
        print(f"[Error] --email-file not found: {path}")
        raise SystemExit(2)
    emails = []
    seen = set()
    with open(path, "r", encoding="utf-8-sig") as handle:
        for raw in handle:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            email = value.split()[0].strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            emails.append(email)
    return emails

def _auto_pay(args):
    """Run automated PayPal payment for a ChatGPT account."""
    email = (args.email or "").strip()
    session_file = (args.session_file or "").strip()
    if not email and not session_file:
        print("[Error] --email or --session-file is required with --auto-pay")
        return

    reverse_only = getattr(args, 'auto_pay_reverse_only', False)
    mode = "reverse-only" if reverse_only else "reverse+browser"
    print(f"[*] Starting auto-pay ({mode}) for: {email or session_file}")
    result = auto_pay(
        email=email,
        session_file=session_file,
        proxy=args.proxy,
        headless=args.auto_pay_headless,
        timeout=args.auto_pay_timeout,
        reverse_only=reverse_only,
    )

    if result.get("ok"):
        print(f"\n[*] Auto-pay completed successfully!")
        print(f"    Email: {result.get('email', '')}")
        print(f"    Alias: {result.get('alias_email', '')}")
        print(f"    Card: ****{result.get('card_last4', '')}")
        print(f"    Status: {result.get('paypal_status', '')}")
        print(f"    Session: {result.get('json_path', '')}")
    else:
        print(f"\n[!] Auto-pay failed: {result.get('error', 'unknown error')}")
        if result.get("failed_step"):
            print(f"    Failed step: {result['failed_step']}")

    print(json.dumps(result, ensure_ascii=False, indent=2))

def _batch_auto_pay(args):
    """Run automated PayPal payment for all pending accounts."""
    from .storage import list_paypal_accounts

    limit = max(0, int(args.batch_auto_pay_limit or 0))

    # Get accounts with pending PayPal status
    all_accounts = list_paypal_accounts()
    pending = [
        row for row in all_accounts
        if row.get("paypal_status") in ("", "missing", "failed", "link_ready")
        and row.get("access_token")
    ]

    if limit > 0:
        pending = pending[:limit]

    if not pending:
        print("[*] No pending accounts found for auto-pay")
        return

    total = len(pending)
    print(f"[*] Batch auto-pay: {total} account(s) to process")
    print("=" * 60)

    results = []
    for i, row in enumerate(pending, 1):
        email = row.get("email", "")
        print(f"[{i}/{total}] Processing: {email}")
        print("-" * 40)

        result = auto_pay(
            email=email,
            proxy=args.proxy,
            headless=args.auto_pay_headless,
            timeout=args.auto_pay_timeout,
        )
        results.append(result)

        if result.get("ok"):
            print(f"[OK] {email} - Payment completed")
        else:
            print(f"[FAIL] {email} - {result.get('error', 'unknown')}")

        # Small delay between accounts
        if i < total:
            time.sleep(5)

    # Summary
    print("" + "=" * 60)

    print("Batch Auto-Pay Summary:")
    print("=" * 60)
    ok_count = sum(1 for r in results if r.get("ok"))
    fail_count = total - ok_count
    print(f"  Total: {total}")
    print(f"  Success: {ok_count}")
    print(f"  Failed: {fail_count}")

    if fail_count > 0:
        print("Failed accounts:")

        for r in results:
            if not r.get("ok"):
                print(f"  - {r.get('email', 'unknown')}: {r.get('error', 'unknown')}")
