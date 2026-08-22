import argparse
import json
import os
import re
import sys
import time

from .config import CFG, initialize_runtime_config
from .diagnostics import install_safe_stdio
from .mailbox import _load_mailbox_pool, _remail_enabled
from .paths import output_dir, runtime_file
from .registration import _build_session_file, _mailbox_snapshot, run_email
from .batch_runner import run_batch_impl as run_batch
from .storage import database_path, get_paypal_url, list_paypal_accounts, rebuild_from_session_dir, upsert_account
from .commands.helpers import (
    read_email_file as _read_email_file,
    payment_method as _payment_method,
    mailbox_from_explicit_args as _mailbox_from_explicit_args,
    one_click_sms_max_reuse as _one_click_sms_max_reuse,
)
from .commands import accounts as account_commands
from .commands import mailbox_ops as mailbox_commands
from .commands import one_click as one_click_commands
from .commands import omakse as omakse_commands
from .commands import payment as payment_commands
from .commands import payment_links as payment_link_commands
from .commands import registration as registration_commands
from .commands.email_change import run_change_email


def _configured_registration_proxy() -> str:
    proxy_cfg = CFG.get("proxy") if isinstance(CFG.get("proxy"), dict) else {}
    return str(
        proxy_cfg.get("registration")
        or CFG.get("registration_proxy")
        or proxy_cfg.get("default")
        or "http://127.0.0.1:7897"
    ).strip()


def _apply_registration_proxy_defaults(args) -> None:
    if bool(getattr(args, "proxy_explicit", False)):
        return
    if str(getattr(args, "proxy_pool", "") or "").strip():
        args.proxy = None
        return
    args.proxy = _configured_registration_proxy() or None


def _proxy_pool_values(args) -> list[str]:
    raw = str(getattr(args, "proxy_pool", "") or "").strip()
    values = [item.strip() for item in re.split(r"[\r\n,;]+", raw) if item.strip()]
    primary = str(getattr(args, "proxy", "") or "").strip()
    if bool(getattr(args, "proxy_explicit", False)) and primary:
        values.insert(0, primary)
    if values:
        return list(dict.fromkeys(values))

    configured_primary = _configured_registration_proxy()
    if configured_primary:
        values.append(configured_primary)
    proxy_cfg = CFG.get("proxy") if isinstance(CFG.get("proxy"), dict) else {}
    configured = proxy_cfg.get("pool") or []
    if isinstance(configured, str):
        configured = re.split(r"[\r\n,;]+", configured)
    values.extend(str(item or "").strip() for item in configured if str(item or "").strip())
    return list(dict.fromkeys(values))


def _registration_command_context():
    return registration_commands.RegistrationCommandContext(
        proxy_pool_values=_proxy_pool_values,
        load_mailbox_pool=_load_mailbox_pool,
        run_batch=run_batch,
        run_email=run_email,
        build_session_file=_build_session_file,
        save_results=_save_registration_results,
        check_registered_promotions=_check_registered_promotions,
        import_registered_accounts=_import_registered_accounts,
        registration_phone_pool=_registration_phone_pool,
        upsert_account=upsert_account,
        database_path=database_path,
        runtime_file=lambda name: runtime_file(CFG, name),
        runtime_config=CFG,
    )


def _preflight_registration_before_mailbox(args) -> dict:
    return registration_commands.preflight_registration_before_mailbox(args, _registration_command_context())


def _protocol_proxy_pool() -> list[str]:
    return payment_commands.protocol_proxy_pool(CFG)


def _payment_proxy_pools(payment_method: str) -> dict[str, list[str]]:
    return payment_commands.payment_proxy_pools(CFG, payment_method)


def _has_explicit_payment_proxy(args) -> bool:
    return payment_commands.has_explicit_payment_proxy(args)


def _registration_phone_pool(args):
    return registration_commands.registration_phone_pool(args)


def _payment_country(payment_method: str, explicit: str = "") -> str:
    return payment_commands.payment_country(payment_method, explicit)


def _payment_method_choices() -> tuple[str, ...]:
    from .payment_catalog import PAYMENT_CATALOG

    return tuple(PAYMENT_CATALOG.aliases)


def _at_payment_stage_args(args, payment_method="paypal"):
    return payment_commands.payment_stage_args(
        args,
        payment_method,
        CFG,
        apply_country_overrides=_apply_stage_country_overrides,
    )


def _apply_stage_country_overrides(args, proxy, checkout_proxy, provider_proxy, approve_proxy):
    return payment_commands.apply_stage_country_overrides(
        args,
        proxy,
        checkout_proxy,
        provider_proxy,
        approve_proxy,
    )


def _at_promotion_proxy_arg(args, payment_method="paypal"):
    return payment_commands.promotion_proxy_arg(args, payment_method, CFG)


def main():
    initialize_runtime_config()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    install_safe_stdio()

    parser = argparse.ArgumentParser(description="ChatGPT Email Registration + PayPal link generation")
    parser.add_argument("--desktop-ipc", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--desktop-serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--doctor", action="store_true", help="Offline environment self-check (python/node/playwright/curl_cffi/config), then exit")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable JSON output for --doctor")
    parser.add_argument(
        "--desktop-read",
        choices=["accounts", "account", "mailbox-file", "account-file", "payment-url-file", "mailbox-pool"],
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--account-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-pool", default="", help="Ordered registration proxy fallbacks, one per line or comma separated")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers for batch registration and account operations")
    parser.add_argument("--target-at200", type=int, default=0, help="Replenish ReMail registrations until this many stable HTTP-200 AT accounts are saved")
    parser.add_argument("--max-mailbox-purchases", type=int, default=0, help="Hard mailbox purchase cap for --target-at200 (default: target x 2)")
    parser.add_argument("--max-remail-cost", type=float, default=0.0, help="Optional total ReMail purchase-cost cap for --target-at200")
    parser.add_argument("--password", default=None, help="Use a specific password")
    parser.add_argument("--email", default=None, help="Mailbox email address")
    parser.add_argument("--email-password", default=None, help="Mailbox password")
    parser.add_argument("--email-refresh-token", default=None, help="Mailbox refresh token")
    parser.add_argument("--email-access-token", default=None, help="Mailbox access token")
    parser.add_argument("--remail-token", default=None, help="ReMail service token; requires --email")
    parser.add_argument("--buy-remail-mailbox", action="store_true", help="Buy ReMail long-term mailbox before registration")
    parser.add_argument("--buy-cfworker-mailbox", action="store_true", help="Use CF Worker temp mailboxes before registration")
    parser.add_argument("--cfworker-domain", default=None, help="CF Worker mailbox domain, default cfworker_domain in config.json")
    parser.add_argument("--buy-smailr-mailbox", action="store_true", help="Use Smailr disposable mailboxes before registration")
    parser.add_argument("--smailr-domain", default=None, help="Smailr mailbox domain, default smailr.default_domain in config.json")
    parser.add_argument("--remail-service-mode", choices=["code", "purchase"], default=None, help="ReMail service mode override")
    parser.add_argument("--remail-supply", choices=["private_first", "public_only"], default=None, help="ReMail inventory policy")
    parser.add_argument("--remail-email-suffix", default=None, help="ReMail mailbox domain suffix")
    parser.add_argument("--remail-project-id", type=int, default=None, help="ReMail project ID")
    parser.add_argument("--remail-product-id", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--mailbox-file", default=None, help="Unified mailbox file: Graph, Gmail, ReMail, CFWorker, or iCloud receive URL")
    parser.add_argument("--chatai-mailbox-file", default=None, help="Legacy mixed mailbox file: Chatai plus all unified mailbox formats")
    parser.add_argument("--phone-register", action="store_true", help="Register with phone number via SMSBower instead of email")
    parser.add_argument("--smsbower-country", default=None, help="SMSBower country ID for phone registration (default: from config)")
    parser.add_argument("--skip-paypal-link", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--registration-mode", choices=["passwordless", "password", "har", "legacy"], default=None, help="Registration auth mode: passwordless/HAR login_or_signup (default) or legacy password")
    parser.add_argument("--registration-batch-id", default=None, help="Stable registration cohort ID stored with active accounts and audit rows")
    parser.add_argument("--payment-method", "--payment-link-method", choices=_payment_method_choices(), default=None, help="Protocol payment-link method")
    parser.add_argument("--paypal-generation-type", default=None, help="Override PayPal link generation type: hosted_long_url, paypal_direct, or paypal_direct_zero_due")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rebuild-sqlite", action="store_true", help="Rebuild SQLite account index from session JSON files")
    parser.add_argument("--delete-account", action="store_true", help="Delete/archive one or more accounts through the lifecycle adapter")
    parser.add_argument("--list-paypal-links", action="store_true", help="List saved PayPal payment links")
    parser.add_argument("--open-paypal-link", action="store_true", help="Open saved PayPal payment link for --email")
    parser.add_argument("--export-codex-json", action="store_true", help="Export paid account session as Codex JSON")
    parser.add_argument("--import-cpa", action="store_true", help="Import an existing AT-only session JSON into CPA/SUB2API")
    parser.add_argument("--register-and-import", action="store_true", help="Register new account(s), then import only the successful registrations into CPA/SUB2API")
    parser.add_argument("--import-target", choices=["cpa", "sub2api", "cliproxyapi"], default="cpa", help="Target for --import-cpa and 401 re-import")
    parser.add_argument("--cpa-domain-filter", default=None, help="Only process CPA accounts under this email domain")
    parser.add_argument("--codex-export-dir", default=None, help="Directory for Codex JSON exports")
    parser.add_argument("--cpa-api-url", default=None, help="CPA API base URL, defaults to cpa/cpa_mode.api_url in config.json")
    parser.add_argument("--cpa-api-token", default=None, help="CPA API token, defaults to cpa/cpa_mode.api_token in config.json")
    parser.add_argument("--refresh-cpa-quota", action="store_true", help="Refresh quota status and update SQLite; defaults to local access_token probing")
    parser.add_argument("--refresh-local-quota", action="store_true", help="Refresh quota status locally with saved access_token and update SQLite")
    parser.add_argument("--quota-usage", action="store_true", help="Fetch wham/usage 5h/7d quota for a single account and return structured JSON (no SQLite write)")
    parser.add_argument("--check-promotion", action="store_true", help="Probe accounts/check plan and Plus-trial/discount (优惠) eligibility and persist promotion_status")
    parser.add_argument("--check-promotion-after-registration", action="store_true", help="After registration, probe saved successful accounts for Plus trial/discount eligibility")
    parser.add_argument("--quota-mode", choices=["local", "cpa", "auto"], default="local", help="Quota refresh mode: local direct probe, cpa management API, or local with CPA fallback")
    parser.add_argument("--quota-auto-relogin", action="store_true", help="When local quota probe returns 401/token_invalidated, retry login with saved mailbox credentials and persist the new AT")
    parser.add_argument("--quota-relogin-timeout", type=int, default=180, help="Timeout in seconds for --quota-auto-relogin")
    parser.add_argument("--quota-workers", type=int, default=4, help="Concurrent workers for quota refresh")
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
    parser.add_argument("--sub2api-auth-mode", choices=["auto", "oauth", "agent_identity"], default="", help="SUB2API credential mode; auto prefers Agent Identity for free accounts")
    parser.add_argument("--sub2api-no-verify", dest="sub2api_verify_after_import", action="store_false", default=None, help="Skip the SUB2API post-import connectivity test")
    parser.add_argument("--no-session-refresh", action="store_true", help="Do not refresh session before Codex JSON export")
    parser.add_argument("--generate-ba-link", action="store_true", help="Generate PayPal BA link directly from Access Token")
    parser.add_argument("--generate-upi-qr", action="store_true", help="Generate India UPI hosted payment link and QR directly from Access Token")
    parser.add_argument("--extract-payment-link", action="store_true", help="Extract a protocol payment link through the unified manager")
    parser.add_argument("--payment-batch-id", default=None, help="Batch ID; reused only together with --payment-resume-checkpoint")
    parser.add_argument("--payment-resume-checkpoint", action="store_true", help="Explicitly resume matching accounts from an existing payment batch checkpoint")
    parser.add_argument("--no-jit-at-refresh", action="store_true", help="Probe the saved AT but do not run email OTP OAuth on HTTP 401")
    parser.add_argument("--payment-probe-only", action="store_true", help="Create Checkout and run Stripe capability detection without creating a payment method")
    parser.add_argument("--payment-matrix", default=None, help="Payment eligibility matrix as JSON text/path; defaults to protocol_payments.matrix")
    parser.add_argument("--payment-canary", type=int, default=0, help="Limit a payment batch to the first N unique accounts")
    parser.add_argument("--payment-retries", type=int, default=3, help="Retries for classified transient payment failures")
    parser.add_argument("--payment-token-map", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--list-payment-methods", action="store_true", help="List protocol payment methods and adapter availability")
    parser.add_argument("--at", default=None, help="Access Token (JWT) for --generate-ba-link/--generate-upi-qr")
    parser.add_argument("--qr-path", default=None, help="Output PNG path for --generate-upi-qr")
    parser.add_argument("--target-country", default=None, help="Target/order country for PayPal generation; legacy checkout-country alias for UPI")
    parser.add_argument("--checkout-country", "--billing-country", dest="checkout_country", default=None, help="Hosted/UPI checkout billing country/currency, e.g. US or JP")
    parser.add_argument("--payment-country", default=None, help="UPI local payment-method country, e.g. IN")
    parser.add_argument("--checkout-proxy", default=None, help="Stage 1 proxy for checkout (JP/TH exit)")
    parser.add_argument("--checkout-proxy-pool", default="", help="Checkout proxy pool; comma or newline separated")
    parser.add_argument("--provider-proxy", default=None, help="Stage 2 proxy for Stripe init/PM/confirm (target country exit)")
    parser.add_argument("--stripe-init-proxy", default=None, help="Explicit Stripe init proxy (falls back to provider proxy)")
    parser.add_argument("--payment-method-proxy", default=None, help="Explicit payment-method creation proxy")
    parser.add_argument("--confirm-proxy", default=None, help="Explicit Stripe confirm proxy")
    parser.add_argument("--approve-proxy", default=None, help="Stage 3 proxy for ChatGPT approve (target country exit)")
    parser.add_argument("--approve-proxy-pool", default="", help="Approve proxy pool; comma or newline separated")
    parser.add_argument("--redirect-proxy", default=None, help="Explicit final provider redirect proxy")
    parser.add_argument("--promotion-proxy", default=None, help="Promotion-update proxy (promo-eligible region exit, e.g. VN/TH) for /checkout/update to make the checkout 0-due")
    payment_proxy_countries = ["US", "GB", "DE", "JP", "BR", "TR", "TH", "VN", "ID", "IN", "NL", "KR", "PL", "CH", "PH"]
    parser.add_argument("--checkout-proxy-country", choices=payment_proxy_countries, default=None, help="Rotate checkout proxy credentials to this exit country")
    parser.add_argument("--approve-proxy-country", choices=payment_proxy_countries, default=None, help="Rotate approve proxy credentials to this exit country")
    parser.add_argument("--promotion-proxy-country", "--update-proxy-country", dest="promotion_proxy_country", choices=payment_proxy_countries, default=None, help="Rotate checkout/update proxy credentials to this exit country")
    parser.add_argument("--auto-proxy-country", action="store_true", help="Let the payment router probe each proxy and match the backend-required exit country")
    parser.add_argument("--test-payment-proxies", action="store_true", help="Probe checkout/approve/update proxy exits and print JSON")
    parser.add_argument("--no-require-zero", action="store_true", help="Allow non-zero amount (default: require 0)")
    parser.add_argument("--require-ba-token", action="store_true", help="Require a PayPal BA approve URL/token; fail instead of returning hosted fallback")
    parser.add_argument("--blik-code", default=None, help="Six-digit BLIK code; supplying it explicitly executes the BLIK payment")
    # ─── Omakse integration ───────────────────────────────────────────────
    parser.add_argument("--omakse-extract", action="store_true", help="Extract PayPal links via omakse server (POST /api/link-extract/jobs)")
    parser.add_argument("--omakse-us-pay", action="store_true", help="Run US PayPal protocol payment via omakse server")
    parser.add_argument("--omakse-base-url", default=None, help="Omakse server base URL (default: http://oai.omakse.xyz)")
    parser.add_argument("--omakse-local-proxy", default=None, help="Local proxy to reach the omakse server")
    parser.add_argument("--omakse-us-proxies", default=None, help="US proxy list for link extraction (newline-separated)")
    parser.add_argument("--omakse-promo-proxies", default=None, help="Promotion-region proxy list for link extraction (newline-separated)")
    parser.add_argument("--omakse-provider-country", default="US", help="PayPal provider country for link extraction")
    parser.add_argument("--omakse-promo-country", default="VN", help="Promotion region country for link extraction")
    parser.add_argument("--omakse-concurrency", type=int, default=5, help="Concurrency for link extraction")
    parser.add_argument("--omakse-max-attempts", type=int, default=3, help="Max attempts per credential for link extraction")
    parser.add_argument("--omakse-poll-interval", type=float, default=1.5, help="Seconds between status polls")
    parser.add_argument("--omakse-max-poll-seconds", type=int, default=300, help="Max seconds to poll for job completion")
    parser.add_argument("--ba-token", default=None, help="PayPal BA token for --omakse-us-pay")
    parser.add_argument("--omakse-phone-country", default="US", help="Phone country for US protocol payment")
    parser.add_argument("--omakse-phone-cc", default="1", help="Phone country code for US protocol payment")
    parser.add_argument("--omakse-proxy-region", default="US", help="Proxy region for US protocol payment")
    parser.add_argument("--omakse-client-id", default=None, help="Client ID for US protocol payment (auto-generated if omitted)")
    parser.add_argument("--omakse-randomize-device", action="store_true", help="Randomize device fingerprint for US payment")
    parser.add_argument("--omakse-preconfirm-phone", action="store_true", help="Pre-confirm phone in US payment flow")
    parser.add_argument("--omakse-send-otp", action="store_true", help="Send phone OTP in US payment flow")
    parser.add_argument("--omakse-load-return-url", action="store_true", help="Load return URL in US payment flow")
    parser.add_argument("--refresh-session", action="store_true", help="Refresh ChatGPT auth session with protocol requests")
    parser.add_argument("--session-file", default=None, help="Session JSON path for account and payment operations")
    parser.add_argument("--email-file", default=None, help="One email per line for batch operations")
    parser.add_argument("--refresh-timeout", type=int, default=300, help="Seconds to wait for interactive auth refresh")
    parser.add_argument("--view-inbox", action="store_true", help="Fetch recent mailbox messages for --email/--session-file and print JSON")
    parser.add_argument("--inbox-limit", type=int, default=20, help="Max messages for --view-inbox")
    parser.add_argument("--gmail-send", action="store_true", help="Send mail through a configured/selected Gmail mailbox")
    parser.add_argument("--gmail-send-to", default=None, help="Recipient list for --gmail-send, separated by comma/newline")
    parser.add_argument("--gmail-send-subject", default=None, help="Subject for --gmail-send")
    parser.add_argument("--gmail-send-body", default=None, help="Plain-text body for --gmail-send")
    parser.add_argument("--gmail-send-html", default=None, help="Optional HTML body for --gmail-send")
    parser.add_argument("--gmail-send-self", action="store_true", help="Send --gmail-send to the Gmail mailbox itself")
    parser.add_argument("--auto-pay", action="store_true", help="Automate PayPal payment (reverse protocol first, browser fallback)")
    parser.add_argument("--auto-pay-reverse-only", action="store_true", help="Use reverse protocol only, no browser fallback")
    parser.add_argument("--auto-pay-headless", action="store_true", help="Run auto-pay browser headless")
    parser.add_argument("--auto-pay-timeout", type=int, default=180, help="Seconds to wait for auto-pay completion")
    parser.add_argument("--batch-auto-pay", action="store_true", help="Run auto-pay for all pending accounts in SQLite")
    parser.add_argument("--batch-auto-pay-limit", type=int, default=0, help="Max accounts to process in batch (0=all)")
    parser.add_argument("--list-paypal-ba-queue", action="store_true", help="List durable PayPal BA authorization queue")
    parser.add_argument("--process-paypal-ba-queue", action="store_true", help="Process pending PayPal BA authorization jobs")
    parser.add_argument("--paypal-ba-queue-limit", type=int, default=0, help="Max PayPal BA authorization jobs (0=all)")
    parser.add_argument("--one-click-sms", action="store_true", help="Run Codex OAuth login for selected account(s), complete phone SMS verification, and store RT")
    parser.add_argument("--one-click-scan", action="store_true", help="Batch OAuth scan accounts for account_deactivated and add-phone/secondary phone verification")
    parser.add_argument("--no-scan-workspace-status", action="store_true", help="Deprecated compatibility flag; --one-click-scan no longer performs workspace checks")
    parser.add_argument("--scan-switch-workspace-id", default=None, help="Deprecated compatibility flag; no longer used")
    parser.add_argument("--scan-fallback-workspace-ids", default=None, help="Deprecated compatibility flag; no longer used")
    parser.add_argument("--scan-auto-switch-workspace", action="store_true", help="Deprecated compatibility flag; no longer used")
    parser.add_argument("--scan-relogin-mode", choices=["auto", "web_session", "codex_oauth"], default="auto", help="Relogin mode for --one-click-scan --quota-auto-relogin; auto tries RT, web session, protocol email-OTP, then Codex OAuth")
    
    parser.add_argument("--convert-session-json", default=None, help="Convert ChatGPT/Codex session JSON file to another import format")
    parser.add_argument("--convert-format", choices=["cpa", "sub2api", "cockpit", "9router", "codex", "axonhub", "codexmanager"], default="cpa", help="Output format for --convert-session-json")
    parser.add_argument("--convert-output", default=None, help="Optional output path for --convert-session-json")
    parser.add_argument("--registration-at-only", action="store_true", default=True, help="Compatibility flag; protocol registration is AT-only by default")
    parser.add_argument("--no-2fa", action="store_true", help="Skip TOTP 2FA enrollment after a successful registration")
    parser.add_argument("--phone-reuse", action="store_true", help="Enable phone number reuse: one phone verifies up to N accounts")
    parser.add_argument("--no-phone-reuse", action="store_true", help="Disable phone verification even when smsbower is configured")
    parser.add_argument("--phone-source", default=None, choices=["smsbower", "phone_pool"], help="Override phone source for registration/one-click SMS")
    parser.add_argument("--max-reuse-count", type=int, default=0, help="Max times a phone can be reused (0=config default or 1)")
    parser.add_argument("--phone-send-cooldown", type=int, default=None, help="Seconds to wait before sending another OTP to the same phone")
    parser.add_argument("--change-email", action="store_true", help="Batch change ChatGPT protocol email addresses")
    parser.add_argument("--change-email-provider", choices=["remail", "cfworker", "smailr", "icloud", "outlook", "hotmail"], default=None)
    parser.add_argument("--change-email-mailbox-file", default=None, help="Credential pool for persistent target email providers")
    parser.add_argument("--change-email-workers", type=int, default=None)
    parser.add_argument("--change-email-timeout", type=int, default=180)
    parser.add_argument("--change-email-otp-timeout", type=int, default=300)
    parser.add_argument("--change-email-service-mode", choices=["code", "purchase"], default="purchase")
    parser.add_argument("--change-email-smailr-domain", default="")
    args = parser.parse_args()
    if args.register_and_import:
        args.import_cpa = True
    # Keep whether --proxy came from the operator.  Some commands (notably
    # --generate-ba-link) need an omitted single proxy to mean "use the
    # configured stage proxies", even though the rest of the CLI still wants
    # CFG.proxy.default as its normal default proxy.
    args.proxy_explicit = bool(args.proxy)
    if not args.proxy:
        args.proxy = ((CFG.get("proxy") or {}).get("default") or "").strip() or None

    base_dir = args.output_dir or str(output_dir(CFG))
    if getattr(args, "desktop_serve", False):
        from .desktop_serve import serve_forever

        raise SystemExit(serve_forever())
    if getattr(args, "doctor", False):
        from .config import default_config_path
        from .doctor import print_doctor_report, run_doctor

        report = run_doctor(CFG, str(default_config_path()))
        if getattr(args, "json_output", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif getattr(args, "desktop_ipc", False):
            from .desktop_ipc import emit_result

            emit_result(dict(report), enabled=True)
        else:
            print_doctor_report(report)
        raise SystemExit(0 if getattr(args, "desktop_ipc", False) else report["failed"])
    if args.desktop_read:
        from .desktop_ipc import emit_result
        from .desktop_read import (
            create_account_file,
            create_mailbox_file,
            create_payment_url_file,
            read_account,
            read_accounts,
            read_mailbox_pool,
        )
        if args.desktop_read == "accounts":
            payload = {"ok": True, "accounts": read_accounts(CFG)}
        elif args.desktop_read == "account":
            payload = {"ok": True, "account": read_account(args.account_id or "", args.email or "", CFG)}
        elif args.desktop_read == "mailbox-pool":
            extra_files = (args.chatai_mailbox_file,) if args.chatai_mailbox_file else ()
            payload = {"ok": True, **read_mailbox_pool(CFG, extra_files=extra_files)}
        elif args.desktop_read == "mailbox-file":
            payload = create_mailbox_file(args.account_id or "", args.email or "", CFG)
        elif args.desktop_read == "account-file":
            payload = create_account_file(args.account_id or "", args.email or "", CFG)
        else:
            payload = create_payment_url_file(args.account_id or "", args.email or "", CFG)
        emit_result(payload, enabled=True)
        return
    if args.delete_account:
        from .account_lifecycle import AccountDeleteRequest, AccountLifecycle
        from .commands.helpers import read_email_file, unique_emails
        emails = read_email_file(args.email_file)
        if args.email:
            emails.insert(0, args.email)
        emails = unique_emails(emails)
        if not emails:
            raise SystemExit("--delete-account requires --email or --email-file")
        lifecycle = AccountLifecycle(CFG)
        results = lifecycle.delete_many(
            (AccountDeleteRequest(email) for email in emails),
            workers=max(1, int(args.workers or 1)),
        )
        failures = sum(isinstance(result, Exception) for result in results)
        payload = {
            "ok": failures == 0,
            "total": len(emails),
            "deleted": len(emails) - failures,
            "failed": failures,
            "results": [
                ({"ok": False, "email": email, "error": str(result)}
                 if isinstance(result, Exception)
                 else {"ok": True, **result.to_dict()})
                for email, result in zip(emails, results)
            ],
        }
        from .desktop_ipc import emit_result
        emit_result(payload, enabled=bool(args.desktop_ipc))
        if failures:
            raise SystemExit(3)
        return
    if args.change_email:
        from .account_email_change import EmailChangeRequest, change_email_batch, load_change_email_accounts
        from .desktop_ipc import emit_result
        run_change_email(
            args,
            load_accounts=load_change_email_accounts,
            change_email_batch=change_email_batch,
            request_type=EmailChangeRequest,
            emit_result=emit_result,
        )
        return
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
    if args.list_paypal_ba_queue:
        _list_paypal_ba_queue(args)
        return
    if args.process_paypal_ba_queue:
        _process_paypal_ba_queue(args)
        return
    if args.import_cpa and not args.register_and_import:
        _import_cpa(args)
        return
    if args.refresh_cpa_quota or args.refresh_local_quota:
        _refresh_cpa_quota(args)
        return
    if getattr(args, "quota_usage", False):
        _quota_usage(args)
        return
    if getattr(args, "check_promotion", False):
        _check_promotion(args)
        return
    if args.export_codex_json:
        _export_codex_json(args)
        return
    if args.list_payment_methods:
        _list_payment_methods()
        return
    if args.test_payment_proxies:
        _test_payment_proxies(args)
        return
    if args.extract_payment_link:
        _extract_payment_link(args)
        return
    if args.generate_ba_link:
        _generate_ba_link(args)
        return
    if args.generate_upi_qr:
        _generate_upi_qr(args)
        return
    if args.omakse_extract:
        _omakse_extract(args)
        return
    if args.omakse_us_pay:
        _omakse_us_pay(args)
        return
    if args.refresh_session:
        _refresh_session(args)
        return
    if args.view_inbox:
        _view_inbox(args)
        return
    if args.gmail_send:
        _gmail_send(args)
        return
    if args.auto_pay or args.auto_pay_reverse_only:
        _auto_pay(args)
        return
    if args.batch_auto_pay:
        _batch_auto_pay(args)
        return

    _apply_registration_proxy_defaults(args)

    if args.one_click_sms:
        _one_click_sms(args)
        return
    if args.one_click_scan:
        _one_click_scan(args)
        return
    
    if args.convert_session_json:
        _convert_session_json(args)
        return

    try:
        _preflight_registration_before_mailbox(args)
    except Exception as exc:
        print(f"[Error] {exc}")
        raise SystemExit(2) from None

    if getattr(args, "target_at200", 0):
        _run_target_at200(args, base_dir)
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
        or args.remail_token
        or args.buy_remail_mailbox
        or args.remail_service_mode
        or args.buy_cfworker_mailbox
        or args.buy_smailr_mailbox
    )
    if not mailboxes and explicit_mailbox_source:
        print("[Error] no mailbox account was found from the requested source; check the selected mailbox row or mailbox file format")
        raise SystemExit(2)
    if not mailboxes and not _remail_enabled():
        print("[Error] no mailbox account was found; set email_registration.token_file, pass --email/--email-refresh-token, or configure ReMail")
        raise SystemExit(2)
    requested_count = max(1, int(args.count or 1))
    if not getattr(args, "registration_batch_id", None):
        args.registration_batch_id = f"registration_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    effective_count = requested_count
    if getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), ReMail returned {effective_count}; registering returned mailboxes only.")
    elif getattr(args, "buy_cfworker_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), CFWorker returned {effective_count}; registering returned mailboxes only.")
    elif getattr(args, "buy_smailr_mailbox", False):
        effective_count = len(mailboxes)
        if effective_count != requested_count:
            print(f"[!] Requested {requested_count} mailbox(es), Smailr returned {effective_count}; registering returned mailboxes only.")
    elif mailboxes and requested_count > len(mailboxes):
        effective_count = len(mailboxes)
        print(f"[!] Requested {requested_count} account(s), but only {effective_count} mailbox(es) were loaded; registering loaded mailboxes only.")

    # Phone reuse pool (auto-enable when smsbower or paypal_auto phone is configured)
    phone_pool = _registration_phone_pool(args)

    # Phone registration mode (via SMSBower)
    if getattr(args, "phone_register", False):
        from .registration import run_phone_register
        proxy_pool = _proxy_pool_values(args)
        if len(proxy_pool) > 1:
            from .batch_runner import select_registration_proxy_base
            selected_proxy = select_registration_proxy_base(proxy_pool, args.proxy)
            proxy_pool = [selected_proxy] if selected_proxy else []
        registration_proxy = proxy_pool[0] if proxy_pool else args.proxy
        results = []
        register_started = time.time()
        for i in range(effective_count):
            print(f"\n{'='*60}")
            print(f"[*] Phone registration {i+1}/{effective_count}")
            print(f"{'='*60}")
            result = run_phone_register(
                proxy=registration_proxy,
                password=args.password,
                codex_oauth=False,
                smsbower_country=args.smsbower_country,
            )
            results.append(result)
            registration_commands.persist_registration_result(
                args,
                result,
                base_dir,
                _registration_command_context(),
                pipeline_timing=registration_commands.registration_pipeline_timing(
                    pipeline_started,
                    mailbox_seconds,
                    register_started,
                ),
            )
            if result.get("success"):
                print(f"[OK] Phone registered: {result.get('phone', '')} | AT: [REDACTED]")
            else:
                print(f"[FAIL] {result.get('error', 'unknown')}")
        _save_registration_results(
            args, results, effective_count=effective_count, base_dir=base_dir,
            pipeline_started=pipeline_started, mailbox_seconds=0,
            register_seconds=time.time() - register_started,
        )
        return

    register_started = time.time()
    if effective_count > 1:
        proxy_pool = _proxy_pool_values(args)
        def persist_completed_result(_index, result):
            registration_commands.persist_registration_result(
                args,
                result,
                base_dir,
                _registration_command_context(),
                pipeline_timing=registration_commands.registration_pipeline_timing(
                    pipeline_started,
                    mailbox_seconds,
                    register_started,
                ),
            )

        results = run_batch(
            count=effective_count,
            proxy=args.proxy,
            proxy_pool=proxy_pool,
            mailboxes=mailboxes,
            workers=args.workers,
            phone_pool=phone_pool,
            codex_oauth=False,
            registration_mode=args.registration_mode,
            browser_headless=bool(getattr(args, "browser_headless", False)),
            enroll_2fa=not getattr(args, "no_2fa", False),
            run_email_func=run_email,
            on_result=persist_completed_result,
        )
    else:
        mailbox = mailboxes[0] if mailboxes else None
        proxy_pool = _proxy_pool_values(args)
        if len(proxy_pool) > 1:
            from .batch_runner import select_registration_proxy_base
            selected_proxy = select_registration_proxy_base(proxy_pool, args.proxy)
            proxy_pool = [selected_proxy] if selected_proxy else []
        results = [run_email(
            proxy=(proxy_pool[0] if proxy_pool else args.proxy),
            password=args.password,
            mailbox=mailbox,
            phone_pool=phone_pool,
            codex_oauth=False,
            registration_mode=args.registration_mode,
            enroll_2fa=not getattr(args, "no_2fa", False),
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
    )


def _save_registration_results(
    args,
    results,
    effective_count,
    base_dir,
    pipeline_started,
    mailbox_seconds,
    register_seconds,
):
    return registration_commands.save_registration_results(
        args,
        results,
        effective_count,
        base_dir,
        pipeline_started,
        mailbox_seconds,
        register_seconds,
        _registration_command_context(),
    )


def _check_registered_promotions(emails, workers=4, proxy=None, timeout=20):
    return registration_commands.check_registered_promotions(
        emails, workers=workers, proxy=proxy, timeout=timeout
    )


def _run_target_at200(args, base_dir):
    return registration_commands.run_target_at200(args, base_dir, _registration_command_context())


def _account_command_context():
    return account_commands.AccountCommandContext(
        list_paypal_accounts=list_paypal_accounts,
        get_paypal_url=get_paypal_url,
    )


def _import_registered_accounts(args, emails):
    return account_commands.import_registered_accounts(args, emails)


def _print_paypal_links(email=""):
    return account_commands.print_paypal_links(email, _account_command_context())


def _open_paypal_link(email):
    return account_commands.open_paypal_link(email, _account_command_context())


def _refresh_session(args):
    return account_commands.refresh_session(args)


def _mailbox_command_context():
    return mailbox_commands.MailboxCommandContext(upsert_account=upsert_account)


def _view_inbox(args):
    return mailbox_commands.view_inbox(args, _mailbox_command_context())


def _gmail_send(args):
    return mailbox_commands.gmail_send(args)


def _export_codex_json(args):
    return account_commands.export_codex_json(args, _account_command_context())


def _importable_account_rows():
    return account_commands.importable_account_rows(_account_command_context())


def _import_cpa(args):
    return account_commands.import_cpa(args, _account_command_context())


def _check_promotion(args):
    return account_commands.check_promotion(args, _account_command_context())


def _refresh_cpa_quota(args):
    return account_commands.refresh_cpa_quota(args, _account_command_context())


def _quota_usage(args):
    return account_commands.quota_usage(args)


def _payment_link_command_context():
    return payment_link_commands.PaymentLinkCommandContext(
        payment_stage_args=_at_payment_stage_args,
        promotion_proxy_arg=_at_promotion_proxy_arg,
        stage_country_overrides=_payment_stage_country_overrides,
        runtime_config=CFG,
    )


def _generate_ba_link(args):
    return payment_link_commands.generate_ba_link(args, _payment_link_command_context())


def _generate_upi_qr(args):
    return payment_link_commands.generate_upi_qr(args, _payment_link_command_context())


def _payment_command_context():
    return payment_commands.PaymentCommandContext(
        read_email_file=_read_email_file,
        payment_method=_payment_method,
        resolve_access_token=_resolve_payment_access_token,
        payment_stage_args=_at_payment_stage_args,
        promotion_proxy_arg=_at_promotion_proxy_arg,
        stage_country_overrides=_payment_stage_country_overrides,
        payment_country=_payment_country,
        protocol_proxy_pool=_protocol_proxy_pool,
        has_explicit_payment_proxy=_has_explicit_payment_proxy,
        payment_proxy_pools=_payment_proxy_pools,
        runtime_config=CFG,
    )


def _list_payment_methods():
    return payment_commands.list_payment_methods()


def _payment_stage_country_overrides(args, payment_method="paypal"):
    return payment_commands.stage_country_overrides(args, payment_method, CFG)


def _resolve_payment_access_token(args):
    return payment_commands.resolve_access_token(args, stderr=sys.stderr)


def _test_payment_proxies(args):
    return payment_commands.test_payment_proxies(args, _payment_command_context())


def _extract_payment_link(args):
    return payment_commands.extract_payment_link(args, _payment_command_context())


def _resolve_cli_payment_route(args, payment_method):
    try:
        route = payment_commands.resolve_payment_route(
            args,
            payment_method,
            _payment_command_context(),
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
    if not route.get("ok"):
        print(json.dumps(route, ensure_ascii=False, indent=2))
        raise SystemExit(3)
    return route


def _convert_session_json(args):
    return account_commands.convert_session_json(args)


def _auto_pay(args):
    return payment_link_commands.auto_pay(args)


def _batch_auto_pay(args):
    return payment_link_commands.batch_auto_pay(args)


def _list_paypal_ba_queue(args):
    return payment_link_commands.list_paypal_ba_queue(args)


def _process_paypal_ba_queue(args):
    return payment_link_commands.process_paypal_ba_queue(args)


def _one_click_command_context():
    return one_click_commands.OneClickCommandContext(
        load_mailbox_pool=_load_mailbox_pool,
        max_reuse=_one_click_sms_max_reuse,
        mailbox_snapshot=_mailbox_snapshot,
        persist_failure=_persist_one_click_sms_failure,
        upsert_account=upsert_account,
    )


def _one_click_sms(args):
    return one_click_commands.one_click_sms(args, _one_click_command_context())


def _one_click_scan(args):
    return one_click_commands.one_click_scan(args)


def _persist_one_click_sms_failure(data, json_path, email, result):
    return one_click_commands.persist_one_click_sms_failure(
        data, json_path, email, result, _one_click_command_context()
    )


# ─── Omakse handlers ──────────────────────────────────────────────────────────

def _omakse_extract(args):
    return omakse_commands.omakse_extract(args, omakse_commands.OmakseCommandContext(runtime_config=CFG))


def _omakse_us_pay(args):
    return omakse_commands.omakse_us_pay(args, omakse_commands.OmakseCommandContext(runtime_config=CFG))

