"""core flags for ``cli.build_parser``."""

import argparse

from ..registration_drivers.base import driver_choices
from ..sms_providers import available_provider_keys


def register(parser: argparse.ArgumentParser) -> None:
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
    parser.add_argument("--registration-driver", choices=driver_choices(), default=None, help="Registration driver (default: protocol)")
    parser.add_argument("--browser-headless", dest="browser_headless", action="store_true", default=None, help="Run Playwright registration headless")
    parser.add_argument("--browser-visible", dest="browser_headless", action="store_false", help="Run Playwright registration with a visible browser")
    parser.add_argument("--registration-batch-id", default=None, help="Stable registration cohort ID stored with active accounts and audit rows")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rebuild-sqlite", action="store_true", help="Rebuild SQLite account index from session JSON files")
    parser.add_argument("--delete-account", action="store_true", help="Delete/archive one or more accounts through the lifecycle adapter")
    parser.add_argument("--mailbox-pool-repaired", action="store_true", help="Acknowledge repaired mailbox credentials and reopen automatic 401 relogin")
    parser.add_argument("--no-session-refresh", action="store_true", help="Do not refresh session before Codex JSON export")
    parser.add_argument("--list-payment-methods", action="store_true", help="List protocol payment methods and adapter availability")
    parser.add_argument("--at", default=None, help="Access Token (JWT) for --generate-ba-link/--generate-upi-qr")
    parser.add_argument("--qr-path", default=None, help="Output PNG path for --generate-upi-qr")
    parser.add_argument("--target-country", default=None, help="Target/order country for PayPal generation; legacy checkout-country alias for UPI")
    parser.add_argument("--email-file", default=None, help="One email per line for batch operations")
    parser.add_argument("--process-paypal-ba-queue", action="store_true", help="Process pending PayPal BA authorization jobs")
    parser.add_argument("--registration-at-only", action="store_true", default=True, help="Compatibility flag; protocol registration is AT-only by default")
    parser.add_argument("--no-2fa", action="store_true", help="Skip TOTP 2FA enrollment after a successful registration")
    parser.add_argument("--phone-reuse", action="store_true", help="Enable phone number reuse: one phone verifies up to N accounts")
    parser.add_argument("--no-phone-reuse", action="store_true", help="Disable phone verification even when smsbower is configured")
    parser.add_argument("--phone-source", default=None, choices=available_provider_keys(), help="Override the SMS provider for registration/one-click SMS")
    parser.add_argument("--max-reuse-count", type=int, default=0, help="Max times a phone can be reused (0=config default or 1)")
    parser.add_argument("--phone-send-cooldown", type=int, default=None, help="Seconds to wait before sending another OTP to the same phone")
