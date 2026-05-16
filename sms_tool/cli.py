import argparse
import json
import os
import re
import sys
import time

from .config import CFG
from .mailbox import _email_cfg, _load_mailbox_pool, run_outlook_batch
from .registration import _build_session_file, run_batch, run_phone
from .sms_provider import _phone_sms_cfg, _resolve_sms_provider

def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="ChatGPT Phone Number Registration")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--outlook-register", action="store_true", help="Batch-register Outlook mailboxes via nb-register integration")
    parser.add_argument("--outlook-script", default=None, help="Path to nb-register outlook-register-service/camoufox_register.py")
    parser.add_argument("--outlook-results-dir", default=None, help="Directory for unlogged_email.txt and outlook_token.txt")
    parser.add_argument("--outlook-suffix", default=None, help="Mailbox suffix, e.g. @outlook.com or @hotmail.com")
    parser.add_argument("--outlook-max-captcha-retries", type=int, default=None)
    parser.add_argument("--outlook-timeout", type=int, default=None, help="Timeout seconds for one Outlook registration")
    parser.add_argument("--outlook-oauth-timeout", type=int, default=None, help="Timeout seconds for one Outlook OAuth flow")
    parser.add_argument("--outlook-skip-oauth", action="store_true", help="Only create mailbox/password records, do not fetch refresh token")
    parser.add_argument("--outlook-debug", action="store_true", help="Keep Outlook browser open for debugging where supported")
    parser.add_argument("--phone", default=None, help="Use a specific phone number (manual mode)")
    parser.add_argument("--password", default=None, help="Use a specific password")
    parser.add_argument("--activation-id", default=None, help="SMS activation ID")
    parser.add_argument("--sms-provider", default=None, choices=("herosms", "smsbower"), help="SMS provider")
    parser.add_argument("--service", default=None, help=f"SMS service code (default: {_phone_sms_cfg().get('service','dr')})")
    parser.add_argument("--country", default=None, help="SMS country code")
    parser.add_argument("--email", default=None, help="Mailbox email address")
    parser.add_argument("--email-password", default=None, help="Mailbox password")
    parser.add_argument("--email-refresh-token", default=None, help="Mailbox refresh token")
    parser.add_argument("--email-access-token", default=None, help="Mailbox access token")
    parser.add_argument("--mailbox-file", default=None, help="nb-register outlook_token.txt compatible file")
    parser.add_argument("--email-as-username", action="store_true", help="Use mailbox email as registration username")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    if args.outlook_register:
        result = run_outlook_batch(count=args.count, args=args)
        print(f"\n[*] Outlook batch done. registered={result['registered']} saved={result['saved']} failures={len(result['failures'])}")
        return

    provider = _resolve_sms_provider(args.sms_provider)
    if not provider.api_key:
        print(f"[Error] phone_sms.{provider.name}_api_key (or phone_sms.api_key) not set in config.json")
        return

    service = args.service or _phone_sms_cfg().get("service", "dr")
    mailboxes = _load_mailbox_pool(args)
    email_as_username = bool(args.email_as_username or _email_cfg().get("use_as_username", False))
    if email_as_username and not mailboxes:
        print("[Error] email-as-username requested but no mailbox account was found")
        return

    if args.count > 1:
        results = run_batch(count=args.count, proxy=args.proxy, sms_service=service, country=args.country,
                            sms_provider_name=provider.name, mailboxes=mailboxes,
                            email_as_username=email_as_username)
    else:
        mailbox = mailboxes[0] if mailboxes else None
        results = [run_phone(proxy=args.proxy, phone=args.phone, password=args.password,
                            activation_id=args.activation_id, sms_service=service, country=args.country,
                            sms_provider_name=provider.name, mailbox=mailbox,
                            email_as_username=email_as_username)]

    base_dir = args.output_dir or CFG.get("output", {}).get("directory", ".")
    out_pattern = CFG.get("output", {}).get("filename_pattern", "session_{email}_{timestamp}.json")
    os.makedirs(base_dir, exist_ok=True)

    saved_count = 0
    for data in filter(None, results):
        if not data.get("success", False):
            continue
        session_data = _build_session_file(data)
        if not session_data.get("refresh_token"):
            print("[!] Successful registration has no refresh_token; session file was not saved")
            continue
        identifier = (session_data.get("email") or session_data.get("phone") or "unknown").replace("+", "")
        safe_identifier = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", identifier)
        fname = out_pattern.format(email=safe_identifier, phone=safe_identifier, timestamp=int(time.time()))
        out_path = os.path.join(base_dir, fname)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
        saved_count += 1
        print(f"[*] Saved session: {out_path}")

    success_count = sum(1 for r in results if r and r.get("success"))
    print(f"\n[*] Done. {success_count}/{args.count} registered successfully, {saved_count} session file(s) saved.")


