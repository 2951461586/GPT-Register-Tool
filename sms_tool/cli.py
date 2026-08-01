import argparse
import json
import os
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import formataddr
from pathlib import Path

from .config import CFG
from .mailbox import _load_mailbox_pool, _remail_enabled
from .paths import output_dir, runtime_file
from .registration import _build_session_file, _mailbox_snapshot, run_batch, run_email
from .storage import database_path, get_paypal_url, list_paypal_accounts, mark_paypal_status, rebuild_from_session_dir, upsert_account
from .commands.helpers import (
    read_email_file as _read_email_file,
    unique_emails as _unique_emails,
    payment_method as _payment_method,
    payment_method_label as _payment_method_label,
    public_mail_message as _public_mail_message,
    public_oauth_result as _public_oauth_result,
    mailbox_from_explicit_args as _mailbox_from_explicit_args,
    one_click_sms_max_reuse as _one_click_sms_max_reuse,
)


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


def _proxy_pool_values(args) -> list[str]:
    raw = str(getattr(args, "proxy_pool", "") or "").strip()
    values = [item.strip() for item in re.split(r"[\r\n,;]+", raw) if item.strip()]
    primary = str(getattr(args, "proxy", "") or "").strip()
    if primary:
        values.insert(0, primary)
    if not values:
        configured = (CFG.get("proxy") or {}).get("pool") or []
        if isinstance(configured, str):
            configured = re.split(r"[\r\n,;]+", configured)
        values.extend(str(item or "").strip() for item in configured if str(item or "").strip())
    return list(dict.fromkeys(values))


def _protocol_proxy_pool() -> list[str]:
    protocol = CFG.get("protocol_payments") if isinstance(CFG.get("protocol_payments"), dict) else {}
    configured = protocol.get("proxy_pool") or []
    if isinstance(configured, str):
        configured = re.split(r"[\r\n,;]+", configured)
    return list(dict.fromkeys(str(item or "").strip() for item in configured if str(item or "").strip()))


def _has_explicit_payment_proxy(args) -> bool:
    return bool(getattr(args, "proxy_explicit", False) or any(
        str(getattr(args, name, "") or "").strip()
        for name in ("checkout_proxy", "provider_proxy", "approve_proxy", "promotion_proxy")
    ))


def _registration_phone_pool(args):
    """Create the configured phone pool for registration flows that require SMS."""
    if getattr(args, "no_phone_reuse", False) or getattr(args, "registration_at_only", False):
        return None

    from .phone_reuse import create_phone_pool, has_phone_reuse_config, print_phone_pool_status

    explicit = bool(getattr(args, "phone_reuse", False))
    auto_enable = has_phone_reuse_config()
    if not explicit and not auto_enable:
        return None

    phone_pool = create_phone_pool(
        max_reuse_count=getattr(args, "max_reuse_count", 0),
        send_cooldown_seconds=getattr(args, "phone_send_cooldown", None),
        source_override=getattr(args, "phone_source", None),
    )
    if not phone_pool.phones:
        if explicit:
            print("[Error] --phone-reuse enabled but no phone numbers configured. Add phone_reuse.smsbower.api_key, SMSBOWER_API_KEY, phone_reuse.phone_pool, or paypal_auto.phone_numbers")
            raise SystemExit(2)
        return None

    if auto_enable and not explicit:
        first = phone_pool.phones[0] if phone_pool.phones else None
        source = first.provider if first else "configured"
        print(f"[*] Auto-enabled phone verification ({source} mode)")
    print_phone_pool_status(phone_pool)
    return phone_pool


def _payment_country(payment_method: str, explicit: str = "") -> str:
    value = str(explicit or "").strip().upper()
    if value:
        return value
    from .payment_link_manager import PAYMENT_METHODS

    spec = PAYMENT_METHODS.get(str(payment_method or "paypal").strip().lower().replace("-", "_"))
    return spec.country if spec else "US"


def _at_payment_stage_args(args, payment_method="paypal"):
    proxy = (getattr(args, "proxy", None) or "").strip() or None
    if not getattr(args, "proxy_explicit", False):
        proxy = None
    explicit_checkout = (getattr(args, "checkout_proxy", None) or "").strip() or None
    explicit_provider = (getattr(args, "provider_proxy", None) or "").strip() or None
    explicit_approve = (getattr(args, "approve_proxy", None) or "").strip() or None
    has_country_override = any((
        (getattr(args, "checkout_proxy_country", None) or "").strip(),
        (getattr(args, "approve_proxy_country", None) or "").strip(),
    ))
    if not has_country_override and (proxy or explicit_checkout or explicit_provider or explicit_approve):
        return proxy, explicit_checkout, explicit_provider, explicit_approve

    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    method_cfg = CFG.get(method) if isinstance(CFG.get(method), dict) else {}
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    proxy_default = (CFG.get("proxy") or {}).get("default") or ""

    checkout_proxy = explicit_checkout or method_stage.get("checkout") or paypal_stage.get("checkout") or proxy or proxy_default
    if method == "upi":
        provider_proxy = (
            explicit_provider
            or method_stage.get("provider")
            or method_stage.get("stripe_init")
            or paypal_stage.get("provider")
            or paypal_stage.get("stripe_init")
            or "http://107.150.109.49:11001"
        )
        approve_proxy = (
            explicit_approve
            or method_stage.get("approve")
            or method_stage.get("confirm")
            or paypal_stage.get("approve")
            or paypal_stage.get("confirm")
            or provider_proxy
            or "http://107.150.109.49:11001"
        )
    else:
        provider_proxy = (
            explicit_provider
            or method_stage.get("provider")
            or method_stage.get("stripe_init")
            or paypal_stage.get("provider")
            or paypal_stage.get("stripe_init")
            or proxy_default
        )
        approve_proxy = (
            explicit_approve
            or method_stage.get("approve")
            or method_stage.get("confirm")
            or paypal_stage.get("approve")
            or paypal_stage.get("confirm")
            or provider_proxy
            or proxy_default
        )
    return _apply_stage_country_overrides(args, proxy, checkout_proxy, provider_proxy, approve_proxy)


def _apply_stage_country_overrides(args, proxy, checkout_proxy, provider_proxy, approve_proxy):
    """Apply country-aware session rotation to the three operator-facing stages."""
    from .paypal_proxy import rotate_proxy_session

    def apply(value, option):
        country = (getattr(args, option, None) or "").strip().upper()
        return rotate_proxy_session(value, country) if value and country else value

    return (
        proxy,
        apply(checkout_proxy, "checkout_proxy_country"),
        provider_proxy,
        apply(approve_proxy, "approve_proxy_country"),
    )


def _at_promotion_proxy_arg(args, payment_method="paypal"):
    """Resolve the promotion-update stage proxy (opt-in, promo-eligible region).

    Priority: explicit --promotion-proxy > <method>.stage_proxies.promotion
    > paypal.stage_proxies.promotion. Returns None when unset so the promotion
    stage stays disabled (behaviour unchanged).
    """
    explicit = (getattr(args, "promotion_proxy", None) or "").strip()
    if explicit:
        from .paypal_proxy import rotate_proxy_session
        country = (getattr(args, "promotion_proxy_country", None) or "").strip().upper()
        return rotate_proxy_session(explicit, country) if country else explicit
    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    method_cfg = CFG.get(method) if isinstance(CFG.get(method), dict) else {}
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    resolved = (
        method_stage.get("promotion")
        or method_stage.get("promotion_update")
        or paypal_stage.get("promotion")
        or paypal_stage.get("promotion_update")
    )
    value = (str(resolved).strip() or None) if resolved else None
    if value:
        from .paypal_proxy import rotate_proxy_session
        country = (getattr(args, "promotion_proxy_country", None) or "").strip().upper()
        if country:
            value = rotate_proxy_session(value, country)
    return value


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="ChatGPT Email Registration + PayPal link generation")
    parser.add_argument("--desktop-ipc", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-pool", default="", help="Ordered registration proxy fallbacks, one per line or comma separated")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers for batch registration/link regeneration")
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
    parser.add_argument("--remail-service-mode", choices=["code", "purchase"], default=None, help="ReMail service mode override")
    parser.add_argument("--remail-supply", choices=["private_first", "public_only"], default=None, help="ReMail inventory policy")
    parser.add_argument("--remail-email-suffix", default=None, help="ReMail mailbox domain suffix")
    parser.add_argument("--remail-project-id", type=int, default=None, help="ReMail project ID")
    parser.add_argument("--remail-product-id", type=int, default=None, help="ReMail product ID")
    parser.add_argument("--mailbox-file", default=None, help="Mailbox token file: email---password---refresh_token---access_token---0")
    parser.add_argument("--chatai-mailbox-file", default=None, help="Chatai mailbox token file: email----password----client_id----refresh_token")
    parser.add_argument("--phone-register", action="store_true", help="Register with phone number via SMSBower instead of email")
    parser.add_argument("--smsbower-country", default=None, help="SMSBower country ID for phone registration (default: from config)")
    parser.add_argument("--skip-paypal-link", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--registration-mode", choices=["passwordless", "password", "har", "legacy"], default=None, help="Registration auth mode: passwordless/HAR login_or_signup (default) or legacy password")
    parser.add_argument("--registration-batch-id", default=None, help="Stable registration cohort ID stored with active accounts and audit rows")
    parser.add_argument("--payment-method", "--payment-link-method", choices=["paypal", "gopay", "upi", "ideal", "pix", "kakao", "blik", "twint", "direct_card", "momo"], default=None, help="Protocol payment-link method")
    parser.add_argument("--paypal-generation-type", default=None, help="Override PayPal link generation type: hosted_long_url, paypal_direct, or paypal_direct_zero_due")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rebuild-sqlite", action="store_true", help="Rebuild SQLite account index from session JSON files")
    parser.add_argument("--list-paypal-links", action="store_true", help="List saved PayPal payment links")
    parser.add_argument("--open-paypal-link", action="store_true", help="Open saved PayPal payment link for --email")
    parser.add_argument("--mark-paypal-status", default=None, help="Update saved PayPal status for --email")
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
    parser.add_argument("--regenerate-paypal-link", action="store_true", help="Regenerate PayPal link for --email and update SQLite/session JSON")
    parser.add_argument("--generate-ba-link", action="store_true", help="Generate PayPal BA link directly from Access Token")
    parser.add_argument("--generate-upi-qr", action="store_true", help="Generate India UPI hosted payment link and QR directly from Access Token")
    parser.add_argument("--extract-payment-link", action="store_true", help="Extract a protocol payment link through the unified manager")
    parser.add_argument("--payment-batch-id", default=None, help="Stable cohort ID for a resumable protocol-payment batch")
    parser.add_argument("--no-jit-at-refresh", action="store_true", help="Probe the saved AT but do not run email OTP OAuth on HTTP 401")
    parser.add_argument("--payment-probe-only", action="store_true", help="Run the JIT AT/eligibility gate without creating a payment method")
    parser.add_argument("--payment-matrix", default=None, help="Payment eligibility matrix as JSON text/path; defaults to protocol_payments.matrix")
    parser.add_argument("--payment-canary", type=int, default=0, help="Limit a payment batch to the first N unique accounts")
    parser.add_argument("--payment-retries", type=int, default=1, help="Retries for classified transient payment failures")
    parser.add_argument("--list-payment-methods", action="store_true", help="List protocol payment methods and adapter availability")
    parser.add_argument("--at", default=None, help="Access Token (JWT) for --generate-ba-link/--generate-upi-qr")
    parser.add_argument("--qr-path", default=None, help="Output PNG path for --generate-upi-qr")
    parser.add_argument("--target-country", default=None, help="Target/order country for PayPal generation; legacy checkout-country alias for UPI")
    parser.add_argument("--checkout-country", "--billing-country", dest="checkout_country", default=None, help="Hosted/UPI checkout billing country/currency, e.g. US or JP")
    parser.add_argument("--payment-country", default=None, help="UPI local payment-method country, e.g. IN")
    parser.add_argument("--checkout-proxy", default=None, help="Stage 1 proxy for checkout (JP/TH exit)")
    parser.add_argument("--provider-proxy", default=None, help="Stage 2 proxy for Stripe init/PM/confirm (target country exit)")
    parser.add_argument("--stripe-init-proxy", default=None, help="Explicit Stripe init proxy (falls back to provider proxy)")
    parser.add_argument("--payment-method-proxy", default=None, help="Explicit payment-method creation proxy")
    parser.add_argument("--confirm-proxy", default=None, help="Explicit Stripe confirm proxy")
    parser.add_argument("--approve-proxy", default=None, help="Stage 3 proxy for ChatGPT approve (target country exit)")
    parser.add_argument("--redirect-proxy", default=None, help="Explicit final provider redirect proxy")
    parser.add_argument("--promotion-proxy", default=None, help="Promotion-update proxy (promo-eligible region exit, e.g. VN/TH) for /checkout/update to make the checkout 0-due")
    parser.add_argument("--checkout-proxy-country", choices=["US", "GB", "DE", "JP", "BR", "TR", "VN", "ID", "IN", "NL", "KR", "PL", "CH", "PH"], default=None, help="Rotate checkout proxy credentials to this exit country")
    parser.add_argument("--approve-proxy-country", choices=["US", "GB", "DE", "JP", "BR", "TR", "VN", "ID", "IN", "NL", "KR", "PL", "CH", "PH"], default=None, help="Rotate approve proxy credentials to this exit country")
    parser.add_argument("--promotion-proxy-country", "--update-proxy-country", dest="promotion_proxy_country", choices=["US", "GB", "DE", "JP", "BR", "TR", "VN", "ID", "IN", "NL", "KR", "PL", "CH", "PH"], default=None, help="Rotate checkout/update proxy credentials to this exit country")
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
    parser.add_argument("--session-file", default=None, help="Session JSON path for --refresh-session or --regenerate-paypal-link")
    parser.add_argument("--email-file", default=None, help="One email per line for batch PayPal link regeneration")
    parser.add_argument("--refresh-timeout", type=int, default=300, help="Seconds to wait for interactive auth refresh")
    parser.add_argument("--view-inbox", action="store_true", help="Fetch recent mailbox messages for --email/--session-file and print JSON")
    parser.add_argument("--inbox-limit", type=int, default=20, help="Max messages for --view-inbox")
    parser.add_argument("--gmail-send", action="store_true", help="Send mail through a configured/selected Gmail mailbox")
    parser.add_argument("--gmail-send-to", default=None, help="Recipient list for --gmail-send, separated by comma/newline")
    parser.add_argument("--gmail-send-subject", default=None, help="Subject for --gmail-send")
    parser.add_argument("--gmail-send-body", default=None, help="Plain-text body for --gmail-send")
    parser.add_argument("--gmail-send-html", default=None, help="Optional HTML body for --gmail-send")
    parser.add_argument("--gmail-send-self", action="store_true", help="Send --gmail-send to the Gmail mailbox itself")
    parser.add_argument("--browser-refresh-session", action="store_true", help="Use the old browser-based refresh flow")
    parser.add_argument("--headless-refresh", action="store_true", help="Run browser refresh headless; visible browser is default")
    parser.add_argument("--auto-pay", action="store_true", help="Automate PayPal payment (reverse protocol first, browser fallback)")
    parser.add_argument("--auto-pay-reverse-only", action="store_true", help="Use reverse protocol only, no browser fallback")
    parser.add_argument("--auto-pay-headless", action="store_true", help="Run auto-pay browser headless")
    parser.add_argument("--auto-pay-timeout", type=int, default=180, help="Seconds to wait for auto-pay completion")
    parser.add_argument("--batch-auto-pay", action="store_true", help="Run auto-pay for all pending accounts in SQLite")
    parser.add_argument("--batch-auto-pay-limit", type=int, default=0, help="Max accounts to process in batch (0=all)")
    parser.add_argument("--one-click-sms", action="store_true", help="Run Codex OAuth login for selected account(s), complete phone SMS verification, and store RT")
    parser.add_argument("--one-click-scan", action="store_true", help="Batch OAuth scan accounts for account_deactivated and add-phone/secondary phone verification")
    parser.add_argument("--no-scan-workspace-status", action="store_true", help="Deprecated compatibility flag; --one-click-scan no longer performs workspace checks")
    parser.add_argument("--scan-switch-workspace-id", default=None, help="Deprecated compatibility flag; no longer used")
    parser.add_argument("--scan-fallback-workspace-ids", default=None, help="Deprecated compatibility flag; no longer used")
    parser.add_argument("--scan-auto-switch-workspace", action="store_true", help="Deprecated compatibility flag; no longer used")
    parser.add_argument("--scan-relogin-mode", choices=["auto", "web_session", "codex_oauth"], default="auto", help="Relogin mode for --one-click-scan --quota-auto-relogin; auto tries ChatGPT web session before Codex OAuth")
    
    parser.add_argument("--convert-session-json", default=None, help="Convert ChatGPT/Codex session JSON file to another import format")
    parser.add_argument("--convert-format", choices=["cpa", "sub2api", "cockpit", "9router", "codex", "axonhub", "codexmanager"], default="cpa", help="Output format for --convert-session-json")
    parser.add_argument("--convert-output", default=None, help="Optional output path for --convert-session-json")
    parser.add_argument("--registration-at-only", action="store_true", help="Registration stores ChatGPT AT only; skip Codex OAuth RT and phone verification")
    parser.add_argument("--phone-reuse", action="store_true", help="Enable phone number reuse: one phone verifies up to N accounts")
    parser.add_argument("--no-phone-reuse", action="store_true", help="Disable phone verification even when smsbower is configured")
    parser.add_argument("--phone-source", default=None, choices=["smsbower", "phone_pool"], help="Override phone source for registration/one-click SMS")
    parser.add_argument("--max-reuse-count", type=int, default=0, help="Max times a phone can be reused (0=config default or 1)")
    parser.add_argument("--phone-send-cooldown", type=int, default=None, help="Seconds to wait before sending another OTP to the same phone")
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
    if args.import_cpa and not args.register_and_import:
        _import_cpa(args)
        return
    if args.refresh_cpa_quota or args.refresh_local_quota:
        _refresh_cpa_quota(args)
        return
    if getattr(args, "quota_usage", False):
        _quota_usage(args)
        return
    if args.export_codex_json:
        _export_codex_json(args)
        return
    if args.regenerate_paypal_link:
        _regenerate_paypal_link(args)
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
    if args.one_click_sms:
        _one_click_sms(args)
        return
    if args.one_click_scan:
        _one_click_scan(args)
        return
    
    if args.convert_session_json:
        _convert_session_json(args)
        return

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
        for i in range(effective_count):
            print(f"\n{'='*60}")
            print(f"[*] Phone registration {i+1}/{effective_count}")
            print(f"{'='*60}")
            result = run_phone_register(
                proxy=registration_proxy,
                password=args.password,
                codex_oauth=not args.registration_at_only,
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
        )
        return

    register_started = time.time()
    if effective_count > 1:
        proxy_pool = _proxy_pool_values(args)
        results = run_batch(
            count=effective_count,
            proxy=args.proxy,
            proxy_pool=proxy_pool,
            mailboxes=mailboxes,
            workers=args.workers,
            phone_pool=phone_pool,
            codex_oauth=not args.registration_at_only,
            registration_mode=args.registration_mode,
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
            codex_oauth=not args.registration_at_only,
            registration_mode=args.registration_mode,
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
    from .storage import record_registration_audit

    batch_id = str(getattr(args, "registration_batch_id", "") or "")
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
        data["batch_id"] = batch_id
        if not data.get("success", False):
            failed_email = data.get("email") or data.get("phone") or "unknown"
            failed_error = str(data.get("error") or "registration_failed")
            print(f"[!] Registration failed for {failed_email}: {failed_error[:500]}")
            record_registration_audit(
                data,
                batch_id=batch_id,
                state="terminal" if "account_deactivated" in failed_error.lower() else "failed",
            )
            if "account_deactivated" in failed_error.lower():
                try:
                    from .mailbox_remail import record_dead_remail_account
                    record_dead_remail_account(data, reason="account_deactivated")
                except Exception:
                    pass
            if failed_error in ("phone_already_registered_or_login_redirect",):
                print(f"    Skipped: phone number already registered, not saving to database")
            continue
        data["registration_state"] = "pending"
        record_registration_audit(data, batch_id=batch_id, state="pending")
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
        session_data["batch_id"] = batch_id
        session_data["registration_state"] = "active"
        if upsert_account(session_data, json_path=out_path):
            db_saved_count += 1
        record_registration_audit(data, batch_id=batch_id, state="active")
        saved_count += 1
        if session_data.get("email"):
            import_emails.append(session_data["email"])
        print(f"[*] Saved session: {out_path}")

    success_count = sum(1 for r in results if r and r.get("success"))
    print(f"[*] SQLite index: {database_path()} ({db_saved_count} record(s) upserted)")
    print(f"\n[*] Done. {success_count}/{effective_count} registered successfully, {saved_count} session file(s) saved.")
    quality = None
    if getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None):
        from .mailbox_remail import record_remail_batch_quality
        quality = record_remail_batch_quality(batch_id, results, requested=effective_count)
        print(
            f"[*] ReMail quality: deactivated={quality['account_deactivated']}/"
            f"{quality['requested']} halt={quality['halt_replenishment']}"
        )

    if getattr(args, "import_cpa", False):
        _import_registered_accounts(args, import_emails)
    return {
        "batch_id": batch_id,
        "success": success_count,
        "session_saved": saved_count,
        "db_saved": db_saved_count,
        "quality": quality,
    }


def _run_target_at200(args, base_dir):
    """Bounded ReMail replenishment mode for a stable AT-200 target."""
    if not (getattr(args, "buy_remail_mailbox", False) or getattr(args, "remail_service_mode", None)):
        print("[Error] --target-at200 requires --buy-remail-mailbox or --remail-service-mode")
        raise SystemExit(2)
    target = max(1, int(args.target_at200 or 1))
    max_purchases = max(target, int(args.max_mailbox_purchases or target * 2))
    max_cost = max(0.0, float(args.max_remail_cost or 0.0))
    if not getattr(args, "registration_batch_id", None):
        args.registration_batch_id = f"target_at200_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    original_count = args.count
    purchased = 0
    active = 0
    spent = 0.0
    rounds = []
    halted = False
    started = time.time()
    phone_pool = _registration_phone_pool(args)
    try:
        while active < target and purchased < max_purchases and not halted:
            quantity = min(target - active, max_purchases - purchased)
            args.count = quantity
            mailboxes = _load_mailbox_pool(args)
            if not mailboxes:
                break
            purchased += len(mailboxes)
            for mailbox in mailboxes:
                try:
                    spent += float(getattr(mailbox, "price", 0) or 0)
                except (TypeError, ValueError):
                    pass
            if max_cost and spent > max_cost:
                halted = True
                break
            round_started = time.time()
            results = run_batch(
                count=len(mailboxes),
                proxy=args.proxy,
                proxy_pool=_proxy_pool_values(args),
                mailboxes=mailboxes,
                workers=args.workers,
                phone_pool=phone_pool,
                codex_oauth=not args.registration_at_only,
                registration_mode=args.registration_mode,
            )
            saved = _save_registration_results(
                args,
                results,
                effective_count=len(mailboxes),
                base_dir=base_dir,
                pipeline_started=round_started,
                mailbox_seconds=0,
                register_seconds=time.time() - round_started,
            ) or {}
            gained = int(saved.get("success") or 0)
            active += gained
            quality = saved.get("quality") if isinstance(saved.get("quality"), dict) else {}
            halted = bool(quality.get("halt_replenishment"))
            rounds.append({
                "requested": quantity,
                "mailboxes": len(mailboxes),
                "active": gained,
                "deactivated": int(quality.get("account_deactivated") or 0),
                "halted": halted,
            })
    finally:
        args.count = original_count
    report = {
        "ok": active >= target,
        "batch_id": args.registration_batch_id,
        "target_at200": target,
        "active": active,
        "purchased": purchased,
        "max_purchases": max_purchases,
        "estimated_cost": round(spent, 4),
        "max_cost": max_cost,
        "supplier_halted": halted,
        "elapsed_seconds": round(time.time() - started, 2),
        "rounds": rounds,
    }
    report_path = runtime_file(CFG, f"registration_target_{args.registration_batch_id}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(3)


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
        sub2api_auth_mode=getattr(args, "sub2api_auth_mode", "") or "",
        sub2api_verify_after_import=getattr(args, "sub2api_verify_after_import", None),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


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
            sub2api_auth_mode=getattr(args, "sub2api_auth_mode", "") or "",
            sub2api_verify_after_import=getattr(args, "sub2api_verify_after_import", None),
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
    import contextlib
    import sys

    from .codex_oauth import _mailbox_from_data
    from .desktop_ipc import emit_result
    from .mailbox import _fetch_mailbox_messages, _mailbox_from_config
    from .session_refresh import _load_seed_session

    def output(payload):
        emit_result(payload, enabled=bool(getattr(args, "desktop_ipc", False)))

    with contextlib.redirect_stdout(sys.stderr):
        data, json_path = _load_seed_session(email=args.email or "", session_file=args.session_file or "")
        mailbox = _mailbox_from_explicit_args(args)
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
                upsert_account(data, json_path=json_path)
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
        "messages": [_public_mail_message(item) for item in messages],
    })


def _gmail_send(args):
    import contextlib
    import sys

    from .codex_oauth import _mailbox_from_data
    from .mailbox import _mailbox_from_config
    from .mailbox_gmail import is_gmail_mailbox, send_gmail_message
    from .session_refresh import _load_seed_session

    with contextlib.redirect_stdout(sys.stderr):
        data, _ = _load_seed_session(email=args.email or "", session_file=args.session_file or "")
        mailbox = _mailbox_from_explicit_args(args)
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


def _importable_account_rows():
    rows = []
    for row in list_paypal_accounts():
        email = str(row.get("email") or "").strip()
        access_token = str(row.get("access_token") or "").strip()
        if email and access_token:
            rows.append(row)
    return rows


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
            sub2api_auth_mode=getattr(args, "sub2api_auth_mode", "") or "",
            sub2api_verify_after_import=getattr(args, "sub2api_verify_after_import", None),
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
            sub2api_auth_mode=getattr(args, "sub2api_auth_mode", "") or "",
            sub2api_verify_after_import=getattr(args, "sub2api_verify_after_import", None),
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
            sub2api_auth_mode=getattr(args, "sub2api_auth_mode", "") or "",
            sub2api_verify_after_import=getattr(args, "sub2api_verify_after_import", None),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)
    try:
        from .cpa_import import refresh_cpa_quota_statuses
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


def _refresh_cpa_quota(args):
    from .cpa_import import refresh_cpa_quota_statuses, refresh_local_quota_statuses
    from .storage import list_paypal_accounts

    emails = _read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    emails = _unique_emails(emails)
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in list_paypal_accounts()]
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
            relogin_on_401=False,
        )
        if quota_mode == "auto" and result.get("failed", 0):
            fallback = refresh_cpa_quota_statuses(
                emails=[item.get("email") for item in result.get("results", []) if not item.get("ok")],
                workers=max(1, int(args.quota_workers or args.workers or 4)),
                api_url=args.cpa_api_url or "",
                api_token=args.cpa_api_token or "",
                timeout=max(5, int(args.refresh_timeout or 30)),
            )
            result["fallback_cpa"] = fallback
            if fallback.get("success", 0):
                result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _quota_usage(args):
    """Fetch wham/usage 5h/7d quota for a single account and return structured JSON."""
    from .cpa_import import probe_local_codex_quota, _format_wham_usage_label
    from .storage import get_account_record

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
    probe = probe_local_codex_quota(account, proxy=proxy, timeout=timeout)
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


def _generate_ba_link(args):
    """Generate PayPal BA link from Access Token."""
    from .gen_pp_link import generate_pp_link

    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "missing --at (Access Token)"}))
        raise SystemExit(1)

    paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    generation_type = (getattr(args, "paypal_generation_type", None) or paypal_cfg.get("link_generation_type") or "").strip().lower().replace("-", "_")
    hosted_types = {"long", "long_link", "hosted", "hosted_long", "hosted_long_url", "stripe_hosted", "chatgpt_checkout", "chatgpt_checkout_link", "checkout_link", "short_checkout", "chatgpt_short_link"}
    checkout_country = None
    if generation_type in hosted_types:
        target_country = (getattr(args, "target_country", None) or paypal_cfg.get("target_country") or "US").strip().upper()
        checkout_country = (
            getattr(args, "checkout_country", None)
            or (regions[0] if regions else None)
            or paypal_cfg.get("checkout_country")
            or paypal_cfg.get("billing_country")
            or target_country
            or "US"
        ).strip().upper()
    else:
        target_country = (getattr(args, "target_country", None) or paypal_cfg.get("target_country") or "GB").strip().upper()
    proxy, checkout_proxy, provider_proxy, approve_proxy = _at_payment_stage_args(args, "paypal")
    promotion_proxy = _at_promotion_proxy_arg(args, "paypal")
    require_zero = not getattr(args, "no_require_zero", False)
    require_ba_token = bool(getattr(args, "require_ba_token", False))

    result = generate_pp_link(
        access_token=at,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        promotion_proxy=promotion_proxy,
        target_country=target_country,
        checkout_country=checkout_country,
        require_zero=require_zero,
        require_ba_token=require_ba_token,
        paypal_generation_type=getattr(args, "paypal_generation_type", None),
        stage_proxy_countries=_payment_stage_country_overrides(args),
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _generate_upi_qr(args):
    """Generate UPI hosted payment link and QR code from Access Token."""
    from .gen_pp_link import generate_upi_qr_link

    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "missing --at (Access Token)"}))
        raise SystemExit(1)

    upi_cfg = CFG.get("upi") if isinstance(CFG.get("upi"), dict) else {}
    regions = upi_cfg.get("billing_regions") if isinstance(upi_cfg.get("billing_regions"), list) else []
    checkout_country = (
        getattr(args, "checkout_country", None)
        or getattr(args, "target_country", None)
        or upi_cfg.get("checkout_country")
        or upi_cfg.get("checkout_billing_country")
        or upi_cfg.get("billing_country")
        or upi_cfg.get("target_country")
        or (regions[0] if regions else None)
        or "IN"
    ).strip().upper()
    payment_country = (
        getattr(args, "payment_country", None)
        or upi_cfg.get("payment_country")
        or upi_cfg.get("payment_method_country")
        or "IN"
    ).strip().upper()
    proxy, checkout_proxy, provider_proxy, approve_proxy = _at_payment_stage_args(args, "upi")
    require_zero = not getattr(args, "no_require_zero", False)
    result = generate_upi_qr_link(
        access_token=at,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        target_country=checkout_country,
        checkout_country=checkout_country,
        payment_country=payment_country,
        require_zero=require_zero,
        qr_path=getattr(args, "qr_path", None),
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _list_payment_methods():
    from .payment_link_manager import supported_payment_methods

    print(json.dumps({"ok": True, "methods": supported_payment_methods()}, ensure_ascii=False, indent=2))


def _payment_stage_country_overrides(args):
    return {
        key: value
        for key, value in {
            "checkout": (getattr(args, "checkout_proxy_country", None) or "").strip().upper(),
            "approve": (getattr(args, "approve_proxy_country", None) or "").strip().upper(),
            "promotion": (getattr(args, "promotion_proxy_country", None) or "").strip().upper(),
        }.items()
        if value
    }


def _resolve_payment_access_token(args):
    at = (getattr(args, "at", None) or "").strip()
    if at:
        return at, None

    email = (getattr(args, "email", None) or "").strip()
    session_file = (getattr(args, "session_file", None) or "").strip()
    if not email and not session_file:
        return "", None

    import contextlib
    from .session_refresh import _load_seed_session

    with contextlib.redirect_stdout(sys.stderr):
        data, _ = _load_seed_session(email=email, session_file=session_file)
    if not isinstance(data, dict):
        return "", None

    def nested(mapping, *keys):
        value = mapping
        for key in keys:
            if not isinstance(value, dict):
                return ""
            value = value.get(key)
        return str(value or "").strip()

    access_token = next((value for value in (
        str(data.get("access_token") or "").strip(),
        str(data.get("accessToken") or "").strip(),
        nested(data, "auth_session", "access_token"),
        nested(data, "auth_session", "accessToken"),
        nested(data, "session", "access_token"),
        nested(data, "session", "accessToken"),
    ) if value), "")
    return access_token, data


def _test_payment_proxies(args):
    from .paypal_proxy import probe_proxy, redact_proxy_url, rotate_proxy_session, select_proxy_from_pool

    method = _payment_method(args)
    _, checkout_proxy, _, approve_proxy = _at_payment_stage_args(args, method)
    promotion_proxy = _at_promotion_proxy_arg(args, method)
    countries = _payment_stage_country_overrides(args)
    default_country = _payment_country(method, getattr(args, "target_country", ""))
    use_pool = not _has_explicit_payment_proxy(args) and bool(_protocol_proxy_pool())
    stage_values = {
        "checkout": checkout_proxy,
        "approve": approve_proxy,
        "update": promotion_proxy,
    }
    stage_country_keys = {"checkout": "checkout", "approve": "approve", "update": "promotion"}
    stages = {}
    for stage, proxy in stage_values.items():
        expected = countries.get(stage_country_keys[stage], "") or default_country
        candidate = proxy or ""
        attempts = []
        result = None
        if use_pool:
            candidate, attempts = select_proxy_from_pool(_protocol_proxy_pool(), expected, stage)
            if not candidate:
                stages[stage] = {
                    "ok": False,
                    "stage": stage,
                    "expected_country": expected,
                    "error": "payment_proxy_pool_unavailable",
                    "proxy": "DIRECT",
                    "attempts": attempts,
                }
                continue
            stages[stage] = {**attempts[-1], "proxy": redact_proxy_url(candidate), "attempts": attempts}
            continue
        for attempt in range(1, 4):
            if attempt > 1 and candidate:
                candidate = rotate_proxy_session(candidate, expected)
            result = probe_proxy(candidate, expected_country=expected, stage=stage)
            attempts.append({"attempt": attempt, "ok": result.ok, "error": result.error})
            if result.ok:
                break
        stages[stage] = {**result.to_dict(), "proxy": redact_proxy_url(candidate), "attempts": attempts}
    ok = all(bool(item.get("ok")) for item in stages.values())
    print(json.dumps({"ok": ok, "payment_method": method, "stages": stages}, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit(3)


def _extract_payment_link(args):
    """Extract any supported protocol payment link from an AT or saved account."""
    from .desktop_ipc import emit_result
    from .payment_link_manager import generate_payment_link

    def output(payload):
        emit_result(payload, enabled=bool(getattr(args, "desktop_ipc", False)))

    method = _payment_method(args)
    email_file = str(getattr(args, "email_file", None) or "").strip()
    if email_file:
        if method == "blik" and not getattr(args, "payment_probe_only", False):
            output({
                "ok": False,
                "error": "BLIK is single-account only; use --email or --session-file with --blik-code",
            })
            raise SystemExit(2)
        from .payment_batch import run_payment_batch

        emails = _read_email_file(email_file)
        if getattr(args, "email", None):
            emails.insert(0, str(args.email).strip())
        if not emails:
            output({"ok": False, "error": "email file contains no accounts"})
            raise SystemExit(1)
        proxy, checkout_proxy, provider_proxy, approve_proxy = _at_payment_stage_args(args, method)
        stage_countries = _payment_stage_country_overrides(args)
        target_country = _payment_country(method, getattr(args, "target_country", ""))
        payment_kwargs = {
            "checkout_proxy": checkout_proxy,
            "provider_proxy": provider_proxy,
            "stripe_init_proxy": getattr(args, "stripe_init_proxy", None),
            "payment_method_proxy": getattr(args, "payment_method_proxy", None),
            "confirm_proxy": getattr(args, "confirm_proxy", None),
            "approve_proxy": approve_proxy,
            "redirect_proxy": getattr(args, "redirect_proxy", None),
            "promotion_proxy": _at_promotion_proxy_arg(args, method),
            "stage_proxy_countries": stage_countries,
            "target_country": target_country,
            "checkout_country": str(getattr(args, "checkout_country", "") or target_country).strip().upper(),
            "require_zero": not getattr(args, "no_require_zero", False),
        }
        try:
            report = run_payment_batch(
                emails,
                payment_method=method,
                workers=getattr(args, "workers", 1),
                batch_id=getattr(args, "payment_batch_id", "") or "",
                proxy=proxy,
                payment_kwargs=payment_kwargs,
                jit_refresh=not getattr(args, "no_jit_at_refresh", False),
                probe_only=bool(getattr(args, "payment_probe_only", False)),
                matrix=getattr(args, "payment_matrix", None),
                canary=getattr(args, "payment_canary", 0),
                retries=getattr(args, "payment_retries", 1),
                timeout=getattr(args, "refresh_timeout", 30),
            )
        except RuntimeError as exc:
            output({"ok": False, "error": str(exc)})
            raise SystemExit(3)
        output(report)
        counts = report.get("counts", {})
        if (
            getattr(args, "payment_probe_only", False) and not counts.get("authenticated")
        ) or (
            not getattr(args, "payment_probe_only", False) and not counts.get("completed")
        ):
            raise SystemExit(3)
        return

    at = ""
    auth_context = None
    if not str(getattr(args, "at", None) or "").strip() and (
        getattr(args, "email", None) or getattr(args, "session_file", None)
    ):
        from .payment_auth import ensure_payment_access_token, public_payment_auth_result

        legacy_at, legacy_context = _resolve_payment_access_token(args)
        auth = ensure_payment_access_token(
            email=str(getattr(args, "email", None) or ""),
            session_file=str(getattr(args, "session_file", None) or ""),
            proxy=getattr(args, "proxy", None),
            timeout=min(max(10, int(getattr(args, "refresh_timeout", 30) or 30)), 300),
            relogin_on_401=not getattr(args, "no_jit_at_refresh", False),
        )
        if not auth.get("ok"):
            if auth.get("error") == "missing_access_token" and legacy_at:
                at, auth_context = legacy_at, legacy_context
            else:
                print(json.dumps(public_payment_auth_result(auth), ensure_ascii=False, indent=2))
                raise SystemExit(3)
        else:
            at = str(auth.get("access_token") or "")
            auth_context = auth.get("auth_context")
        if auth.get("ok") and getattr(args, "payment_probe_only", False):
            print(json.dumps(public_payment_auth_result(auth), ensure_ascii=False, indent=2))
            return
    else:
        at, auth_context = _resolve_payment_access_token(args)
    if not at:
        print(json.dumps({
            "ok": False,
            "error": "selected account has no Access Token" if (getattr(args, "email", None) or getattr(args, "session_file", None)) else "missing --at (Access Token)",
        }, ensure_ascii=False))
        raise SystemExit(1)
    proxy, checkout_proxy, provider_proxy, approve_proxy = _at_payment_stage_args(args, method)
    stage_countries = _payment_stage_country_overrides(args)
    target_country = _payment_country(method, getattr(args, "target_country", ""))
    checkout_country = str(getattr(args, "checkout_country", "") or target_country).strip().upper()
    if not _has_explicit_payment_proxy(args):
        defaults = _protocol_proxy_pool()
        if defaults:
            from .paypal_proxy import rotate_proxy_session, select_proxy_from_pool
            proxy, attempts = select_proxy_from_pool(defaults, target_country, "payment")
            if not proxy:
                print(json.dumps({
                    "ok": False,
                    "error": "payment_proxy_pool_unavailable",
                    "target_country": target_country,
                    "attempts": attempts,
                }, ensure_ascii=False, indent=2))
                raise SystemExit(3)
            checkout_proxy = rotate_proxy_session(proxy, stage_countries.get("checkout") or checkout_country)
            provider_proxy = rotate_proxy_session(proxy, target_country)
            approve_proxy = rotate_proxy_session(proxy, stage_countries.get("approve") or target_country)
    kwargs = {
        "checkout_proxy": checkout_proxy,
        "provider_proxy": provider_proxy,
        "stripe_init_proxy": getattr(args, "stripe_init_proxy", None),
        "payment_method_proxy": getattr(args, "payment_method_proxy", None),
        "confirm_proxy": getattr(args, "confirm_proxy", None),
        "approve_proxy": approve_proxy,
        "redirect_proxy": getattr(args, "redirect_proxy", None),
        "promotion_proxy": (
            rotate_proxy_session(proxy, stage_countries.get("promotion") or target_country)
            if proxy and not _has_explicit_payment_proxy(args) and _protocol_proxy_pool()
            else _at_promotion_proxy_arg(args, method)
        ),
        "stage_proxy_countries": stage_countries,
        "require_zero": not getattr(args, "no_require_zero", False),
    }
    if target_country:
        kwargs["target_country"] = target_country
    if checkout_country:
        kwargs["checkout_country"] = checkout_country
    if method == "paypal":
        kwargs["require_ba_token"] = bool(getattr(args, "require_ba_token", False))
    if method == "upi":
        kwargs["payment_country"] = (getattr(args, "payment_country", None) or "IN").strip().upper()
        kwargs["qr_path"] = getattr(args, "qr_path", None)
    if method == "blik":
        kwargs["blik_code"] = getattr(args, "blik_code", None)

    result = generate_payment_link(
        access_token=at,
        proxy=proxy,
        payment_method=method,
        auth_context=auth_context,
        paypal_generation_type=getattr(args, "paypal_generation_type", None),
        **kwargs,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _regenerate_paypal_link(args):
    from .paypal_links import regenerate_paypal_link

    email = (args.email or "").strip()
    emails = _read_email_file(args.email_file)
    payment_method = _payment_method(args)
    payment_proxy = args.proxy
    payment_checkout_proxy = getattr(args, "checkout_proxy", None)
    payment_provider_proxy = getattr(args, "provider_proxy", None)
    payment_approve_proxy = getattr(args, "approve_proxy", None)
    if not _has_explicit_payment_proxy(args) and _protocol_proxy_pool():
        from .paypal_proxy import select_proxy_from_pool

        country = _payment_country(payment_method, getattr(args, "target_country", ""))
        payment_proxy, attempts = select_proxy_from_pool(_protocol_proxy_pool(), country, "payment")
        if not payment_proxy:
            print(json.dumps({"ok": False, "error": "payment_proxy_pool_unavailable", "attempts": attempts}, ensure_ascii=False, indent=2))
            raise SystemExit(3)
        payment_checkout_proxy = payment_provider_proxy = payment_approve_proxy = payment_proxy
    if emails:
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
                proxy=payment_proxy,
                payment_method=payment_method,
                paypal_generation_type=args.paypal_generation_type,
                checkout_proxy=payment_checkout_proxy,
                provider_proxy=payment_provider_proxy,
                approve_proxy=payment_approve_proxy,
                promotion_proxy=getattr(args, "promotion_proxy", None),
                require_zero=not getattr(args, "no_require_zero", False),
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
        proxy=payment_proxy,
        payment_method=payment_method,
        paypal_generation_type=args.paypal_generation_type,
        checkout_proxy=payment_checkout_proxy,
        provider_proxy=payment_provider_proxy,
        approve_proxy=payment_approve_proxy,
        promotion_proxy=getattr(args, "promotion_proxy", None),
        require_zero=not getattr(args, "no_require_zero", False),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)




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
            checkout_proxy=getattr(args, "checkout_proxy", None),
            provider_proxy=getattr(args, "provider_proxy", None),
            approve_proxy=getattr(args, "approve_proxy", None),
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







def _convert_session_json(args):
    from .session_converter import convert_json_file

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

    explicit_mailboxes = {}
    if getattr(args, "chatai_mailbox_file", None) or getattr(args, "mailbox_file", None):
        explicit_mailboxes = {
            str(getattr(mailbox, "email", "") or "").strip().lower(): mailbox
            for mailbox in _load_mailbox_pool(args)
            if str(getattr(mailbox, "email", "") or "").strip()
        }

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
        mailbox = explicit_mailboxes.get(email.strip().lower())
        if mailbox is not None:
            data["mailbox"] = _mailbox_snapshot(mailbox)
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
        workspace_check=False,
        switch_workspace_id="",
        fallback_workspace_ids=[],
        auto_switch_workspace=False,
        quota_relogin_on_401=bool(args.quota_auto_relogin),
        relogin_mode=args.scan_relogin_mode,
    )
    if summary.get("failed", 0):
        raise SystemExit(3)




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


# ─── Omakse handlers ──────────────────────────────────────────────────────────

def _omakse_extract(args):
    """Extract PayPal links via the omakse server."""
    from .omakse_client import extract_links, extract_links_for_account

    # Resolve credentials: explicit --at, or look up by --email
    at = (args.at or "").strip()
    email = (args.email or "").strip()

    # Resolve US proxies
    us_proxies = (args.omakse_us_proxies or "").strip()
    if not us_proxies:
        # Fall back to config stage_proxies.checkout or proxy.default
        paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
        stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
        us_proxies = stage.get("checkout") or (CFG.get("proxy") or {}).get("default", "")
        if us_proxies:
            print(f"[*] Using checkout stage proxy as US proxy: {us_proxies}", file=sys.stderr)

    # Resolve promotion proxies
    promo_proxies = (args.omakse_promo_proxies or "").strip()
    if not promo_proxies:
        paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
        stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
        promo_proxies = stage.get("promotion") or ""

    if at:
        print(f"[*] Starting omakse link extraction with explicit AT...", file=sys.stderr)
        result = extract_links(
            credentials=at,
            us_proxies=us_proxies,
            promotion_proxies=promo_proxies,
            provider_country=args.omakse_provider_country,
            promotion_country=args.omakse_promo_country,
            concurrency=args.omakse_concurrency,
            max_attempts=args.omakse_max_attempts,
            poll_interval=args.omakse_poll_interval,
            max_poll_seconds=args.omakse_max_poll_seconds,
            base_url=args.omakse_base_url,
            proxy=args.omakse_local_proxy or "",
        )
    elif email:
        print(f"[*] Starting omakse link extraction for {email}...", file=sys.stderr)
        result = extract_links_for_account(
            email=email,
            us_proxies=us_proxies,
            promotion_proxies=promo_proxies,
            provider_country=args.omakse_provider_country,
            promotion_country=args.omakse_promo_country,
            concurrency=args.omakse_concurrency,
            max_attempts=args.omakse_max_attempts,
            poll_interval=args.omakse_poll_interval,
            max_poll_seconds=args.omakse_max_poll_seconds,
            base_url=args.omakse_base_url,
            proxy=args.omakse_local_proxy or "",
        )
    else:
        print("[Error] --omakse-extract requires --at <TOKEN> or --email <EMAIL>", file=sys.stderr)
        raise SystemExit(2)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def _omakse_us_pay(args):
    """Run US PayPal protocol payment via the omakse server."""
    from .omakse_client import run_us_payment_and_wait

    ba_token = (args.ba_token or "").strip()
    if not ba_token:
        print("[Error] --omakse-us-pay requires --ba-token <BA-TOKEN>", file=sys.stderr)
        raise SystemExit(2)

    # Resolve payment proxy: explicit --checkout-proxy, or config stage
    proxy = (args.checkout_proxy or "").strip()
    if not proxy:
        paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
        stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
        proxy = stage.get("checkout") or (CFG.get("proxy") or {}).get("default", "")
        if proxy:
            print(f"[*] Using config proxy for US payment: {proxy}", file=sys.stderr)

    if not proxy:
        print("[Error] No proxy available for US payment. Use --checkout-proxy or configure proxy.default", file=sys.stderr)
        raise SystemExit(2)

    result = run_us_payment_and_wait(
        ba_token=ba_token,
        proxy=proxy,
        phone_country=args.omakse_phone_country,
        phone_country_code=args.omakse_phone_cc,
        proxy_region=args.omakse_proxy_region,
        client_id=args.omakse_client_id,
        randomize_device=args.omakse_randomize_device,
        preconfirm_phone=args.omakse_preconfirm_phone,
        send_phone_otp=args.omakse_send_otp,
        load_return_url=args.omakse_load_return_url,
        poll_interval=args.omakse_poll_interval,
        max_poll_seconds=args.omakse_max_poll_seconds,
        base_url=args.omakse_base_url,
        local_proxy=args.omakse_local_proxy or "",
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)

