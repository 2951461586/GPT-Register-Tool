"""quota flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--refresh-cpa-quota", action="store_true", help="Refresh quota status and update SQLite; defaults to local access_token probing")
    parser.add_argument("--refresh-local-quota", action="store_true", help="Refresh quota status locally with saved access_token and update SQLite")
    parser.add_argument("--quota-usage", action="store_true", help="Fetch wham/usage 5h/7d quota for a single account and return structured JSON (no SQLite write)")
    parser.add_argument("--check-promotion", action="store_true", help="Probe accounts/check plan and Plus-trial/discount (优惠) eligibility and persist promotion_status")
    parser.add_argument("--check-promotion-after-registration", action="store_true", help="After registration, probe saved successful accounts for Plus trial/discount eligibility")
    parser.add_argument(
        "--no-payment-eligibility",
        dest="payment_eligibility",
        action="store_false",
        default=True,
        help=(
            "Skip the payment-method enumeration that normally rides along with "
            "--check-promotion (one side-effect-free Checkout + Stripe init per "
            "account, persisted as raw_json.payment_capability and shown next to "
            "the 优惠状态 badge). Pass this to halve the request count per account "
            "when the method list is not needed."
        ),
    )
    parser.add_argument("--quota-mode", choices=["local", "cpa", "auto"], default="local", help="Quota refresh mode: local direct probe, cpa management API, or local with CPA fallback")
    parser.add_argument("--quota-auto-relogin", action="store_true", help="When local quota probe returns 401/token_invalidated, retry login with saved mailbox credentials and persist the new AT")
    parser.add_argument("--quota-relogin-timeout", type=int, default=None, help="Timeout in seconds for --quota-auto-relogin (defaults to account_health config)")
    parser.add_argument("--quota-batch-timeout", type=int, default=None, help="Maximum total seconds for a local quota batch (defaults to account_health config)")
    parser.add_argument("--quota-account-timeout", type=int, default=None, help="Maximum seconds allowed per local quota account (defaults to account_health config)")
    parser.add_argument("--quota-workers", type=int, default=4, help="Concurrent workers for quota refresh")
