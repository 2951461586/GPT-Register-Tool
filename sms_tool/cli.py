import argparse
import json
import os
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import CFG
from .mailbox import _load_mailbox_pool, _luckmail_enabled
from .paths import output_dir
from .registration import _build_session_file, run_batch, run_email
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
    parser.add_argument("--buy-cfworker-mailbox", action="store_true", help="Use CF Worker temp mailboxes before registration")
    parser.add_argument("--cfworker-domain", default=None, help="CF Worker mailbox domain, default cfworker_domain in config.json")
    parser.add_argument("--luckmail-purchase-project", default=None, help="LuckMail purchase project code, default openai")
    parser.add_argument("--luckmail-purchase-email-type", default=None, help="LuckMail purchase email type, default ms_imap")
    parser.add_argument("--luckmail-purchase-domain", default=None, help="LuckMail purchase domain, default outlook.com")
    parser.add_argument("--mailbox-file", default=None, help="Mailbox token file: email---password---refresh_token---access_token---0")
    parser.add_argument("--chatai-mailbox-file", default=None, help="Chatai mailbox token file: email----password----client_id----refresh_token")
    parser.add_argument("--phone-register", action="store_true", help="Register with phone number via SMSBower instead of email")
    parser.add_argument("--smsbower-country", default=None, help="SMSBower country ID for phone registration (default: from config)")
    parser.add_argument("--skip-paypal-link", action="store_true", help="Do not generate PayPal payment link after registration")
    parser.add_argument("--payment-method", "--payment-link-method", choices=["paypal", "gopay", "upi"], default=None, help="Payment link/payment method: paypal, gopay, or upi")
    parser.add_argument("--paypal-generation-type", default=None, help="Override PayPal link generation type: hosted_long_url, paypal_direct, or paypal_direct_zero_due")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rebuild-sqlite", action="store_true", help="Rebuild SQLite account index from session JSON files")
    parser.add_argument("--list-paypal-links", action="store_true", help="List saved PayPal payment links")
    parser.add_argument("--open-paypal-link", action="store_true", help="Open saved PayPal payment link for --email")
    parser.add_argument("--mark-paypal-status", default=None, help="Update saved PayPal status for --email")
    parser.add_argument("--export-codex-json", action="store_true", help="Export paid account session as Codex JSON")
    parser.add_argument("--import-cpa", action="store_true", help="Import an existing AT-only session JSON into CPA/SUB2API")
    parser.add_argument("--import-target", choices=["cpa", "sub2api", "cliproxyapi"], default="cpa", help="Target for --import-cpa and 401 re-import")
    parser.add_argument("--cpa-domain-filter", default=None, help="Only process CPA accounts under this email domain")
    parser.add_argument("--codex-export-dir", default=None, help="Directory for Codex JSON exports")
    parser.add_argument("--cpa-api-url", default=None, help="CPA API base URL, defaults to cpa/cpa_mode.api_url in config.json")
    parser.add_argument("--cpa-api-token", default=None, help="CPA API token, defaults to cpa/cpa_mode.api_token in config.json")
    parser.add_argument("--sub2api-url", default=None, help="SUB2API base URL, defaults to sub2api.api_url in config.json")
    parser.add_argument("--sub2api-token", default=None, help="SUB2API bearer access token, defaults to sub2api.api_token in config.json")
    parser.add_argument("--sub2api-email", default=None, help="SUB2API login email when no bearer token is configured")
    parser.add_argument("--sub2api-password", default=None, help="SUB2API login password when no bearer token is configured")
    parser.add_argument("--sub2api-group", default=None, help="SUB2API target group name(s), defaults to codex")
    parser.add_argument("--sub2api-group-ids", default=None, help="SUB2API target group id list, comma separated")
    parser.add_argument("--sub2api-proxy", default=None, help="SUB2API default proxy name or id")
    parser.add_argument("--sub2api-proxy-id", type=int, default=None, help="SUB2API default proxy id")
    parser.add_argument("--sub2api-priority", type=int, default=None, help="SUB2API account priority, defaults to config or 1")
    parser.add_argument("--sub2api-concurrency", type=int, default=None, help="SUB2API account concurrency, defaults to config or 10")
    parser.add_argument("--no-session-refresh", action="store_true", help="Do not refresh session before Codex JSON export")
    parser.add_argument("--regenerate-paypal-link", action="store_true", help="Regenerate PayPal link for --email and update SQLite/session JSON")
    parser.add_argument("--generate-ba-link", action="store_true", help="Generate PayPal BA link directly from Access Token")
    parser.add_argument("--at", default=None, help="Access Token (JWT) for --generate-ba-link")
    parser.add_argument("--target-country", default="GB", help="Target country for BA link generation (default: GB)")
    parser.add_argument("--checkout-proxy", default=None, help="Stage 1 proxy for checkout (JP/TH exit)")
    parser.add_argument("--provider-proxy", default=None, help="Stage 2 proxy for Stripe init/PM/confirm (target country exit)")
    parser.add_argument("--approve-proxy", default=None, help="Stage 3 proxy for ChatGPT approve (target country exit)")
    parser.add_argument("--no-require-zero", action="store_true", help="Allow non-zero amount (default: require 0)")
    parser.add_argument("--require-ba-token", action="store_true", help="Require a PayPal BA approve URL/token; fail instead of returning hosted fallback")
    parser.add_argument("--refresh-session", action="store_true", help="Refresh ChatGPT auth session with protocol requests")
    parser.add_argument("--session-file", default=None, help="Session JSON path for --refresh-session or --regenerate-paypal-link")
    parser.add_argument("--email-file", default=None, help="One email per line for batch PayPal link regeneration")
    parser.add_argument("--refresh-timeout", type=int, default=300, help="Seconds to wait for interactive auth refresh")
    parser.add_argument("--view-inbox", action="store_true", help="Fetch recent mailbox messages for --email/--session-file and print JSON")
    parser.add_argument("--inbox-limit", type=int, default=20, help="Max messages for --view-inbox")
    parser.add_argument("--browser-refresh-session", action="store_true", help="Use the old browser-based refresh flow")
    parser.add_argument("--headless-refresh", action="store_true", help="Run browser refresh headless; visible browser is default")
    parser.add_argument("--auto-pay", action="store_true", help="Automate PayPal payment (reverse protocol first, browser fallback)")
    parser.add_argument("--auto-pay-reverse-only", action="store_true", help="Use reverse protocol only, no browser fallback")
    parser.add_argument("--auto-pay-headless", action="store_true", help="Run auto-pay browser headless")
    parser.add_argument("--auto-pay-timeout", type=int, default=180, help="Seconds to wait for auto-pay completion")
    parser.add_argument("--batch-auto-pay", action="store_true", help="Run auto-pay for all pending accounts in SQLite")
    parser.add_argument("--batch-auto-pay-limit", type=int, default=0, help="Max accounts to process in batch (0=all)")
    parser.add_argument("--one-click-pay", action="store_true", help="一键支付: PayPal 无卡协议支付或 GoPay 支付 (单账号或 --email-file 批量)")
    parser.add_argument("--one-click-pay-all", action="store_true", help="一键支付: 对所有待支付账号执行支付")
    parser.add_argument("--gopay-phone", default=None, help="GoPay provider mode phone number override")
    parser.add_argument("--gopay-country-code", default=None, help="GoPay provider mode country code override, e.g. 62")
    parser.add_argument("--gopay-otp-channel", default=None, choices=["sms", "wa", "whatsapp", "none"], help="GoPay provider OTP channel")
    parser.add_argument("--gopay-flow-id", default=None, help="Existing GoPay provider flow_id to complete with --gopay-otp")
    parser.add_argument("--gopay-otp", default=None, help="GoPay provider OTP code for completing a prepared flow")
    parser.add_argument("--gopay-pin", default=None, help="GoPay provider PIN for completing provider payment")
    parser.add_argument("--gopay-wa-phone", default=None, help="WA-channel GoPay phone used for payment/rebind mode")
    parser.add_argument("--gopay-user-id", default=None, help="GoPay app state user id for WA rebind mode, default local")
    parser.add_argument("--gopay-auth-otp", default=None, help="WA login OTP for GoPay app auth before rebind")
    parser.add_argument("--gopay-rebind-phone", default=None, help="New phone number used by WA rebind after payment")
    parser.add_argument("--gopay-rebind-otp", default=None, help="SMS OTP for GoPay change-phone completion")
    parser.add_argument("--one-click-sms", action="store_true", help="Run Codex OAuth login for selected account(s), complete phone SMS verification, and store RT")
    parser.add_argument("--one-click-scan", action="store_true", help="Batch OAuth scan accounts for account_deactivated and add-phone/secondary phone verification")
    parser.add_argument("--registration-at-only", action="store_true", help="Registration stores ChatGPT AT only; skip Codex OAuth RT and phone verification")
    parser.add_argument("--phone-reuse", action="store_true", help="Enable phone number reuse: one phone verifies up to N accounts")
    parser.add_argument("--no-phone-reuse", action="store_true", help="Disable phone verification even when smsbower is configured")
    parser.add_argument("--phone-source", default=None, choices=["smsbower", "nextsms", "phone_pool"], help="Override phone source for registration/one-click SMS")
    parser.add_argument("--max-reuse-count", type=int, default=0, help="Max times a phone can be reused (0=config default or 1)")
    parser.add_argument("--phone-send-cooldown", type=int, default=None, help="Seconds to wait before sending another OTP to the same phone")
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
        return
        return
    if args.export_codex_json:
        _export_codex_json(args)
        return
    if args.regenerate_paypal_link:
        _regenerate_paypal_link(args)
        return
    if args.generate_ba_link:
        _generate_ba_link(args)
        return
    if args.refresh_session:
        _refresh_session(args)
        return
    if args.view_inbox:
        _view_inbox(args)
        return
    if args.auto_pay or args.auto_pay_reverse_only:
        _auto_pay(args)
        return
    if args.batch_auto_pay:
        _batch_auto_pay(args)
        return
    if args.one_click_pay or args.one_click_pay_all:
        _one_click_pay(args)
        return
    if args.one_click_sms:
        _one_click_sms(args)
        return
    if args.one_click_scan:
        _one_click_scan(args)
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
        or args.buy_cfworker_mailbox
    )
    if not mailboxes and explicit_mailbox_source:
        print("[Error] no mailbox account was found from the requested source; check the selected mailbox row or mailbox file format")
        raise SystemExit(2)
    if not mailboxes and not _luckmail_enabled():
        print("[Error] no mailbox account was found; set email_registration.token_file, pass --email/--email-refresh-token, or configure LuckMail")
        raise SystemExit(2)
    payment_method = _payment_method(args)
    paypal_link = _payment_link_enabled(payment_method, args)

    requested_count = max(1, int(args.count or 1))
    effective_count = requested_count
    if getattr(args, "buy_luckmail_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), LuckMail returned {effective_count}; registering returned mailboxes only.")
    elif getattr(args, "buy_cfworker_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), CFWorker returned {effective_count}; registering returned mailboxes only.")
    elif mailboxes and requested_count > len(mailboxes):
        effective_count = len(mailboxes)
        print(f"[!] Requested {requested_count} account(s), but only {effective_count} mailbox(es) were loaded; registering loaded mailboxes only.")

    # Phone reuse pool (auto-enable when smsbower or paypal_auto phone is configured)
    phone_pool = None
    if not args.no_phone_reuse and not args.registration_at_only:
        from .phone_reuse import create_phone_pool, has_phone_reuse_config, print_phone_pool_status
        auto_enable = has_phone_reuse_config()
        if args.phone_reuse or auto_enable:
            phone_pool = create_phone_pool(
                max_reuse_count=args.max_reuse_count,
                send_cooldown_seconds=args.phone_send_cooldown,
                source_override=args.phone_source,
            )
            if not phone_pool.phones:
                if args.phone_reuse:
                    print("[Error] --phone-reuse enabled but no phone numbers configured. Add phone_reuse.smsbower.api_key, SMSBOWER_API_KEY, phone_reuse.phone_pool, or paypal_auto.phone_numbers")
                    raise SystemExit(2)
            else:
                if auto_enable and not args.phone_reuse:
                    first = phone_pool.phones[0] if phone_pool.phones else None
                    source = first.provider if first else "configured"
                    print(f"[*] Auto-enabled phone verification ({source} mode)")
                print_phone_pool_status(phone_pool)

    # Phone registration mode (via SMSBower)
    if getattr(args, "phone_register", False):
        from .registration import run_phone_register
        payment_method = _payment_method(args)
        paypal_link = _payment_link_enabled(payment_method, args)
        results = []
        for i in range(effective_count):
            print(f"\n{'='*60}")
            print(f"[*] Phone registration {i+1}/{effective_count}")
            print(f"{'='*60}")
            result = run_phone_register(
                proxy=args.proxy,
                password=args.password,
                paypal_link=paypal_link,
                codex_oauth=not args.registration_at_only,
                payment_method=payment_method,
                paypal_generation_type=args.paypal_generation_type,
                smsbower_country=args.smsbower_country,
            )
            results.append(result)
            if result.get("success"):
                print(f"[OK] Phone registered: {result.get('phone', '')} | AT: {str(result.get('access_token', ''))[:20]}...")
            else:
                print(f"[FAIL] {result.get('error', 'unknown')}")
        _save_registration_results(
            args, results, effective_count=effective_count, base_dir=base_dir,
            pipeline_started=pipeline_started, mailbox_seconds=0,
            register_seconds=time.time() - pipeline_started,
            paypal_link=paypal_link, payment_method=payment_method,
        )
        return

    register_started = time.time()
    if effective_count > 1:
        results = run_batch(
            count=effective_count,
            proxy=args.proxy,
            mailboxes=mailboxes,
            paypal_link=paypal_link,
            workers=args.workers,
            phone_pool=phone_pool,
            codex_oauth=not args.registration_at_only,
            payment_method=payment_method,
            paypal_generation_type=args.paypal_generation_type,
        )
    else:
        mailbox = mailboxes[0] if mailboxes else None
        results = [run_email(
            proxy=args.proxy,
            password=args.password,
            mailbox=mailbox,
            paypal_link=paypal_link,
            phone_pool=phone_pool,
            codex_oauth=not args.registration_at_only,
            payment_method=payment_method,
            paypal_generation_type=args.paypal_generation_type,
        )]
    register_seconds = time.time() - register_started

    _save_registration_results(
        args,
        results,
        effective_count=effective_count,
        base_dir=base_dir,
        pipeline_started=pipeline_started,
        mailbox_seconds=mailbox_seconds,
        register_seconds=register_seconds,
        paypal_link=paypal_link,
        payment_method=payment_method,
    )


def _payment_method(args):
    value = str(getattr(args, "payment_method", "") or "").strip().lower()
    if value in {"gopay", "go-pay", "go_pay"}:
        return "gopay"
    if value in {"upi", "upiqr", "upi_qr", "upi-qr"}:
        return "upi"
    return "paypal"


def _save_registration_results(
    args,
    results,
    effective_count,
    base_dir,
    pipeline_started,
    mailbox_seconds,
    register_seconds,
    paypal_link,
    payment_method,
):
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
    import_emails = []
    for data in filter(None, results):
        if not data.get("success", False):
            failed_email = data.get("email") or data.get("phone") or "unknown"
            failed_error = str(data.get("error") or "registration_failed")
            print(f"[!] Registration failed for {failed_email}: {failed_error[:500]}")
            if failed_error in ("phone_already_registered_or_login_redirect",):
                print(f"    Skipped: phone number already registered, not saving to database")
            elif upsert_account(data):
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
        if session_data.get("email"):
            import_emails.append(session_data["email"])
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
            label = _payment_method_label(payment_method)
            print(f"[Error] {label} link generation failed for {data.get('email', '')}: {paypal.get('error', 'missing payment URL')}")
        raise SystemExit(3)

    if getattr(args, "import_cpa", False):
        _import_registered_accounts(args, import_emails)


def _import_registered_accounts(args, emails):
    from .import_targets import import_account_sessions

    emails = [str(email or "").strip() for email in emails if str(email or "").strip()]
    if not emails:
        print("[!] No successful registered account to import into CPA/SUB2API")
        return
    result = import_account_sessions(
        args.import_target,
        emails,
        export_dir=args.codex_export_dir or "",
        workers=args.workers,
        refresh=not args.no_session_refresh,
        proxy=args.proxy,
        timeout=args.refresh_timeout,
        cpa_api_url=args.cpa_api_url or "",
        cpa_api_token=args.cpa_api_token or "",
        sub2api_url=args.sub2api_url or "",
        sub2api_token=args.sub2api_token or "",
        sub2api_email=args.sub2api_email or "",
        sub2api_password=args.sub2api_password or "",
        sub2api_group=args.sub2api_group or "",
        sub2api_group_ids=args.sub2api_group_ids or "",
        sub2api_proxy=args.sub2api_proxy or "",
        sub2api_proxy_id=args.sub2api_proxy_id,
        sub2api_priority=args.sub2api_priority,
        sub2api_concurrency=args.sub2api_concurrency,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _payment_method_label(payment_method):
    raw = str(payment_method or "").strip().lower()
    if raw in {"gopay", "go-pay", "go_pay"}:
        value = "gopay"
    elif raw in {"upi", "upiqr", "upi_qr", "upi-qr"}:
        value = "upi"
    else:
        value = "paypal"
    if value == "gopay":
        return "GoPay"
    if value == "upi":
        return "UPI"
    return "PayPal"


def _payment_link_enabled(payment_method, args):
    if args.skip_paypal_link:
        return False
    paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    if payment_method in {"gopay", "upi"}:
        method_cfg = CFG.get(payment_method) if isinstance(CFG.get(payment_method), dict) else {}
        return bool(method_cfg.get("auto_generate", paypal_cfg.get("auto_generate", True)))
    return bool(paypal_cfg.get("auto_generate", True))


def _print_paypal_links(email=""):
    rows = list_paypal_accounts(email=email or "")
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
            print(f"[*] Payment status updated: {item_email} -> {status}")
            result = {"ok": True, "email": item_email, "paypal_status": status}
        else:
            print(f"[Error] account not found: {item_email}")
            result = {"ok": False, "email": item_email, "error": "account_not_found"}
        results.append(result)

    if args.import_cpa:
        from .import_targets import import_account_sessions

        import_emails = [result["email"] for result in results if result.get("ok")]
        import_result = import_account_sessions(
            args.import_target,
            import_emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            cpa_api_url=args.cpa_api_url or "",
            cpa_api_token=args.cpa_api_token or "",
            sub2api_url=args.sub2api_url or "",
            sub2api_token=args.sub2api_token or "",
            sub2api_email=args.sub2api_email or "",
            sub2api_password=args.sub2api_password or "",
            sub2api_group=args.sub2api_group or "",
            sub2api_group_ids=args.sub2api_group_ids or "",
            sub2api_proxy=args.sub2api_proxy or "",
            sub2api_proxy_id=args.sub2api_proxy_id,
            sub2api_priority=args.sub2api_priority,
            sub2api_concurrency=args.sub2api_concurrency,
        )
        print(json.dumps(import_result, ensure_ascii=False, indent=2))
        if any(not result.get("ok") for result in results) or not import_result.get("ok"):
            raise SystemExit(3)
    elif args.export_codex_json:
        from .codex_export import export_codex_sessions

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
    from .session_refresh import refresh_session

    result = refresh_session(
        email=args.email or "",
        session_file=args.session_file or "",
        timeout=args.refresh_timeout,
        headless=args.headless_refresh,
        browser=args.browser_refresh_session,
        proxy=args.proxy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _view_inbox(args):
    from .codex_oauth import _mailbox_from_data
    from .mailbox import _fetch_mailbox_messages
    from .session_refresh import _load_seed_session

    data, _ = _load_seed_session(email=args.email or "", session_file=args.session_file or "")
    mailbox = _mailbox_from_explicit_args(args)
    if mailbox is None:
        mailbox = _mailbox_from_data(data)
    if mailbox is None:
        print(json.dumps({
            "ok": False,
            "email": args.email or data.get("email", ""),
            "error": "missing_mailbox_credentials",
        }, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    try:
        messages = _fetch_mailbox_messages(
            mailbox,
            limit=max(1, min(int(args.inbox_limit or 20), 100)),
            proxy=args.proxy,
        )
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "email": mailbox.email,
            "provider": mailbox.provider,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(3)
    print(json.dumps({
        "ok": True,
        "email": mailbox.email,
        "provider": mailbox.provider,
        "messages": [_public_mail_message(item) for item in messages],
    }, ensure_ascii=False, indent=2))


def _public_mail_message(msg):
    msg = msg if isinstance(msg, dict) else {}
    from_value = msg.get("from")
    if isinstance(from_value, dict):
        from_value = ((from_value.get("emailAddress") or {}).get("address") or from_value.get("address") or "")
    body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
    return {
        "id": str(msg.get("id") or msg.get("message_id") or ""),
        "receivedDateTime": str(msg.get("receivedDateTime") or msg.get("received_at") or msg.get("created_at") or ""),
        "from": str(from_value or msg.get("from_email") or msg.get("sender") or ""),
        "subject": str(msg.get("subject") or msg.get("title") or ""),
        "bodyPreview": str(msg.get("bodyPreview") or msg.get("preview") or body.get("content") or msg.get("text") or "")[:2000],
    }


def _mailbox_from_explicit_args(args):
    if not (getattr(args, "chatai_mailbox_file", None) or getattr(args, "mailbox_file", None)):
        return None
    from .mailbox import _load_mailbox_pool

    requested = str(getattr(args, "email", "") or "").strip().lower()
    mailboxes = _load_mailbox_pool(args)
    if not mailboxes:
        return None
    if requested:
        for mailbox in mailboxes:
            if str(getattr(mailbox, "email", "") or "").strip().lower() == requested:
                return mailbox
    return mailboxes[0]


def _export_codex_json(args):
    from .codex_export import export_codex_session, export_codex_sessions

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
    from .import_targets import import_account_session, import_account_sessions

    emails = _read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if emails:
        result = import_account_sessions(
            args.import_target,
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            cpa_api_url=args.cpa_api_url or "",
            cpa_api_token=args.cpa_api_token or "",
            sub2api_url=args.sub2api_url or "",
            sub2api_token=args.sub2api_token or "",
            sub2api_email=args.sub2api_email or "",
            sub2api_password=args.sub2api_password or "",
            sub2api_group=args.sub2api_group or "",
            sub2api_group_ids=args.sub2api_group_ids or "",
            sub2api_proxy=args.sub2api_proxy or "",
            sub2api_proxy_id=args.sub2api_proxy_id,
            sub2api_priority=args.sub2api_priority,
            sub2api_concurrency=args.sub2api_concurrency,
        )
    elif args.session_file:
        result = import_account_session(
            args.import_target,
            session_file=args.session_file,
            export_dir=args.codex_export_dir or "",
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            cpa_api_url=args.cpa_api_url or "",
            cpa_api_token=args.cpa_api_token or "",
            sub2api_url=args.sub2api_url or "",
            sub2api_token=args.sub2api_token or "",
            sub2api_email=args.sub2api_email or "",
            sub2api_password=args.sub2api_password or "",
            sub2api_group=args.sub2api_group or "",
            sub2api_group_ids=args.sub2api_group_ids or "",
            sub2api_proxy=args.sub2api_proxy or "",
            sub2api_proxy_id=args.sub2api_proxy_id,
            sub2api_priority=args.sub2api_priority,
            sub2api_concurrency=args.sub2api_concurrency,
        )
    else:
        rows = _importable_account_rows()
        emails = [row.get("email", "") for row in rows if row.get("email")]
        result = import_account_sessions(
            args.import_target,
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            cpa_api_url=args.cpa_api_url or "",
            cpa_api_token=args.cpa_api_token or "",
            sub2api_url=args.sub2api_url or "",
            sub2api_token=args.sub2api_token or "",
            sub2api_email=args.sub2api_email or "",
            sub2api_password=args.sub2api_password or "",
            sub2api_group=args.sub2api_group or "",
            sub2api_group_ids=args.sub2api_group_ids or "",
            sub2api_proxy=args.sub2api_proxy or "",
            sub2api_proxy_id=args.sub2api_proxy_id,
            sub2api_priority=args.sub2api_priority,
            sub2api_concurrency=args.sub2api_concurrency,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _importable_account_rows():
    rows = []
    for row in list_paypal_accounts():
        email = str(row.get("email") or "").strip()
        access_token = str(row.get("access_token") or "").strip()
        if email and access_token:
            rows.append(row)
    return rows


def _generate_ba_link(args):
    """直接从 Access Token 生成 BA 链接。"""
    from .gen_pp_link import generate_pp_link

    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "请提供 --at 参数 (Access Token)"}))
        raise SystemExit(1)

    target_country = (getattr(args, "target_country", None) or "GB").strip().upper()
    proxy = (getattr(args, "proxy", None) or "").strip() or None
    checkout_proxy = (getattr(args, "checkout_proxy", None) or "").strip() or None
    provider_proxy = (getattr(args, "provider_proxy", None) or "").strip() or None
    approve_proxy = (getattr(args, "approve_proxy", None) or "").strip() or None
    require_zero = not getattr(args, "no_require_zero", False)
    require_ba_token = bool(getattr(args, "require_ba_token", False))

    # 从配置文件加载默认代理
    if not proxy and not checkout_proxy and not provider_proxy:
        paypal_cfg = CFG.get("paypal") or {}
        stage_proxies = paypal_cfg.get("stage_proxies") or {}
        proxy_default = (CFG.get("proxy") or {}).get("default") or ""
        checkout_proxy = checkout_proxy or stage_proxies.get("checkout") or proxy_default
        provider_proxy = provider_proxy or stage_proxies.get("provider") or stage_proxies.get("stripe_init") or proxy_default
        approve_proxy = approve_proxy or stage_proxies.get("approve") or stage_proxies.get("confirm") or proxy_default

    result = generate_pp_link(
        access_token=at,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        target_country=target_country,
        require_zero=require_zero,
        require_ba_token=require_ba_token,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _regenerate_paypal_link(args):
    from .paypal_links import regenerate_paypal_link

    email = (args.email or "").strip()
    emails = _read_email_file(args.email_file)
    if emails:
        payment_method = _payment_method(args)
        workers = _payment_regenerate_workers(args, payment_method, len(emails))
        delay_seconds = _payment_regenerate_delay_seconds(payment_method)
        print(
            f"[*] Batch regenerate PayPal links: {len(emails)} account(s), "
            f"workers={workers} requested={int(args.workers or 1)} delay={delay_seconds:g}s"
        )
        results = []
        ordered = [None] * len(emails)

        def _run_one(index, item_email):
            if index > 0 and delay_seconds > 0:
                time.sleep(delay_seconds * index if workers > 1 else delay_seconds)
            print(f"[{index + 1}/{len(emails)}] Regenerating PayPal link: {item_email}")
            return index, regenerate_paypal_link(
                email=item_email,
                session_file="",
                proxy=args.proxy,
                payment_method=payment_method,
                paypal_generation_type=args.paypal_generation_type,
            )

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
    result = regenerate_paypal_link(
        email=email,
        session_file=args.session_file or "",
        proxy=args.proxy,
        payment_method=_payment_method(args),
        paypal_generation_type=args.paypal_generation_type,
        checkout_proxy=getattr(args, "checkout_proxy", None),
        provider_proxy=getattr(args, "provider_proxy", None),
        approve_proxy=getattr(args, "approve_proxy", None),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _payment_regenerate_workers(args, payment_method: str, total: int) -> int:
    requested = max(1, int(getattr(args, "workers", 1) or 1))
    cfg = CFG.get(payment_method) if isinstance(CFG.get(payment_method), dict) else {}
    if payment_method == "paypal":
        cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    configured = cfg.get("max_regenerate_workers", cfg.get("regenerate_workers"))
    try:
        cap = int(configured)
    except (TypeError, ValueError):
        cap = 4
    cap = max(1, cap)
    return max(1, min(requested, cap, total))


def _payment_regenerate_delay_seconds(payment_method: str) -> float:
    cfg = CFG.get(payment_method) if isinstance(CFG.get(payment_method), dict) else {}
    if payment_method == "paypal":
        cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    try:
        return max(0.0, float(cfg.get("regenerate_delay_seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _regenerate_paypal_link_fallback(args, emails):
    """网关不可用时的回退：ThreadPoolExecutor 逐个调用。"""
    from .paypal_links import regenerate_paypal_link

    payment_method = _payment_method(args)
    workers = _payment_regenerate_workers(args, payment_method, len(emails))
    delay_seconds = _payment_regenerate_delay_seconds(payment_method)
    print(f"[*] Fallback: {len(emails)} account(s), workers={workers} delay={delay_seconds:g}s")
    ordered = [None] * len(emails)

    def _run_one(index, item_email):
        if index > 0 and delay_seconds > 0:
            time.sleep(delay_seconds * index if workers > 1 else delay_seconds)
        print(f"[{index + 1}/{len(emails)}] Regenerating PayPal link: {item_email}")
        return index, regenerate_paypal_link(
            email=item_email,
            session_file="",
            proxy=args.proxy,
            payment_method=payment_method,
            paypal_generation_type=args.paypal_generation_type,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_one, i, item_email) for i, item_email in enumerate(emails)]
        for future in as_completed(futures):
            index, result = future.result()
            ordered[index] = result

    results = [r for r in ordered if r is not None]
    ok_count = sum(1 for r in results if r.get("ok"))
    print(json.dumps({"ok": ok_count == len(emails), "total": len(emails), "success": ok_count, "failed": len(emails) - ok_count, "results": results}, ensure_ascii=False, indent=2))
    if ok_count != len(emails):
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
    from .paypal_auto import auto_pay

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
    from .paypal_auto import auto_pay
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


def _one_click_pay(args):
    """一键支付: PayPal 无卡协议支付。"""
    payment_method = _payment_method(args)
    if payment_method == "gopay":
        from .gopay_payment import one_click_pay_batch
    elif payment_method == "upi":
        print("[Error] UPI currently supports hosted long-link generation only; one-click payment is not implemented")
        raise SystemExit(2)
    else:
        from .paypal_browser_auto import one_click_pay_batch
    one_click_pay_batch(args)


def _one_click_sms(args):
    """Refresh selected account(s) through Codex OAuth and phone SMS, then store RT."""
    from .codex_oauth import refresh_codex_oauth_session
    from .phone_reuse import create_phone_pool, print_phone_pool_status
    from .session_refresh import _load_seed_session

    emails = _read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if not emails and args.session_file:
        seed, _ = _load_seed_session(session_file=args.session_file)
        if seed.get("email"):
            emails = [str(seed.get("email") or "").strip()]
    emails = _unique_emails(emails)
    if not emails:
        print("[Error] --email, --email-file, or --session-file is required with --one-click-sms")
        raise SystemExit(2)

    one_click_max_reuse = _one_click_sms_max_reuse(args)
    phone_pool = create_phone_pool(
        max_reuse_count=one_click_max_reuse,
        send_cooldown_seconds=args.phone_send_cooldown,
        source_override=args.phone_source,
    )
    if not phone_pool.phones:
        print("[Error] --one-click-sms requires a phone pool. Configure phone_reuse.smsbower.api_key/SMSBOWER_API_KEY or phone_reuse.phone_pool.")
        raise SystemExit(2)
    phone_pool.reset_exhausted_smsbower_slots()
    print_phone_pool_status(phone_pool)
    if phone_pool.total_capacity <= 0:
        print("[Error] --one-click-sms requires at least one available phone slot; current phone pool is exhausted.")
        raise SystemExit(2)

    workers = max(1, min(int(args.workers or 1), 4, len(emails)))
    print(f"[*] One-click SMS RT refresh: {len(emails)} account(s), workers={workers}")

    def _run_one(index, email):
        print(f"\n[{index + 1}/{len(emails)}] One-click SMS: {email}")
        data, json_path = _load_seed_session(
            email=email,
            session_file=args.session_file if len(emails) == 1 else "",
        )
        data.setdefault("email", email)
        result = refresh_codex_oauth_session(
            data,
            json_path=json_path,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            force_email_otp_login=True,
            phone_pool=phone_pool,
        )
        if result.get("ok"):
            phone = str(result.get("phone") or "").strip()
            phone_suffix = f" phone={phone}" if phone else ""
            print(f"[OK] {email} RT stored: {result.get('refresh_token_status', '')}{phone_suffix}")
        else:
            print(f"[FAIL] {email}: {result.get('error', 'unknown')}")
            _persist_one_click_sms_failure(data, json_path, email, result)
        result.setdefault("email", email)
        return index, result

    ordered = [None] * len(emails)
    if workers <= 1:
        for index, email in enumerate(emails):
            i, result = _run_one(index, email)
            ordered[i] = result
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run_one, i, email) for i, email in enumerate(emails)]
            for future in as_completed(futures):
                i, result = future.result()
                ordered[i] = result

    results = [result for result in ordered if result is not None]
    ok_count = sum(1 for result in results if result.get("ok"))
    summary = {
        "ok": ok_count == len(emails),
        "total": len(emails),
        "success": ok_count,
        "failed": len(emails) - ok_count,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if ok_count != len(emails):
        raise SystemExit(3)


def _one_click_scan(args):
    """Batch OAuth probe accounts without sending SMS."""
    from .account_scan import scan_accounts
    from .session_refresh import _load_seed_session
    from .storage import list_paypal_accounts

    emails = _read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if not emails and args.session_file:
        seed, _ = _load_seed_session(session_file=args.session_file)
        if seed.get("email"):
            emails = [str(seed.get("email") or "").strip()]
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in list_paypal_accounts()]
    emails = _unique_emails(emails)
    if not emails:
        print("[Error] no account email was found for --one-click-scan")
        raise SystemExit(2)

    summary = scan_accounts(
        emails,
        session_file=args.session_file if len(emails) == 1 else "",
        workers=args.workers,
        proxy=args.proxy,
        timeout=args.refresh_timeout,
    )
    if summary.get("failed", 0):
        raise SystemExit(3)


def _one_click_sms_max_reuse(args) -> int:
    requested = int(getattr(args, "max_reuse_count", 0) or 0)
    if requested and requested != 1:
        print("[*] One-click SMS forces max_reuse_count=1 so each email account gets its own phone number")
    return 1


def _persist_one_click_sms_failure(data, json_path, email, result):
    now = int(time.time())
    refreshed = dict(data or {})
    refreshed["email"] = email
    refreshed["success"] = bool(refreshed.get("access_token"))
    refreshed["error"] = str(result.get("error") or "one_click_sms_failed")
    refreshed["refresh_token_status"] = str(refreshed.get("refresh_token_status") or "no_rt")
    refreshed["refresh_token_updated_at"] = now
    response = refreshed.get("response") if isinstance(refreshed.get("response"), dict) else {}
    response["codex_oauth"] = _public_oauth_result(result)
    refreshed["response"] = response
    phone_attempt = result.get("phone_attempt") if isinstance(result.get("phone_attempt"), dict) else {}
    if phone_attempt:
        refreshed["phone"] = phone_attempt.get("phone", refreshed.get("phone", ""))
        response["phone_verification"] = phone_attempt
    if json_path:
        try:
            Path(json_path).write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[!] Failed to update session JSON {json_path}: {exc}")
    upsert_account(refreshed, json_path=json_path)


def _public_oauth_result(result):
    if not isinstance(result, dict):
        return {}
    output = {key: value for key, value in result.items() if key != "tokens"}
    tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
    if tokens:
        output["has_access_token"] = bool(tokens.get("access_token"))
        output["has_refresh_token"] = bool(tokens.get("refresh_token"))
    return output


def _unique_emails(emails):
    output = []
    seen = set()
    for email in emails or []:
        value = str(email or "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
