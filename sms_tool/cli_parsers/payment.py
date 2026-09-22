"""payment flags for ``cli.build_parser``."""

import argparse

from ..payment_catalog import PAYMENT_CATALOG


def _payment_method_choices() -> tuple[str, ...]:
    return tuple(PAYMENT_CATALOG.aliases)


PAYMENT_PROXY_COUNTRIES = ["US", "GB", "DE", "JP", "BR", "TR", "TH", "VN", "ID", "IN", "NL", "KR", "PL", "CH", "PH"]


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--payment-method", "--payment-link-method", choices=_payment_method_choices(), default=None, help="Protocol payment-link method")
    parser.add_argument("--extract-payment-link", action="store_true", help="Extract a protocol payment link through the unified manager")
    parser.add_argument("--payment-batch-id", default=None, help="Batch ID; reused only together with --payment-resume-checkpoint")
    parser.add_argument("--payment-resume-checkpoint", action="store_true", help="Explicitly resume matching accounts from an existing payment batch checkpoint")
    parser.add_argument("--no-jit-at-refresh", action="store_true", help="Probe the saved AT but do not run email OTP OAuth on HTTP 401")
    parser.add_argument("--payment-probe-only", action="store_true", help="Create Checkout and run Stripe capability detection without creating a payment method")
    parser.add_argument("--payment-matrix", default=None, help="Payment eligibility matrix as JSON text/path; defaults to protocol_payments.matrix")
    parser.add_argument("--payment-canary", type=int, default=0, help="Limit a payment batch to the first N unique accounts")
    parser.add_argument("--payment-retries", type=int, default=3, help="Retries for classified transient payment failures")
    parser.add_argument("--payment-token-map", default=None, help=argparse.SUPPRESS)
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
    parser.add_argument("--checkout-proxy-country", choices=PAYMENT_PROXY_COUNTRIES, default=None, help="Rotate checkout proxy credentials to this exit country")
    parser.add_argument("--approve-proxy-country", choices=PAYMENT_PROXY_COUNTRIES, default=None, help="Rotate approve proxy credentials to this exit country")
    parser.add_argument("--promotion-proxy-country", "--update-proxy-country", dest="promotion_proxy_country", choices=PAYMENT_PROXY_COUNTRIES, default=None, help="Rotate checkout/update proxy credentials to this exit country")
    parser.add_argument("--auto-proxy-country", action="store_true", help="Let the payment router probe each proxy and match the backend-required exit country")
    parser.add_argument("--test-payment-proxies", action="store_true", help="Probe checkout/approve/update proxy exits and print JSON")
    parser.add_argument("--no-require-zero", action="store_true", help="Allow non-zero amount (default: require 0)")
    parser.add_argument("--require-ba-token", action="store_true", help="Require a PayPal BA approve URL/token; fail instead of returning hosted fallback")
    parser.add_argument("--blik-code", default=None, help="Six-digit BLIK code; supplying it explicitly executes the BLIK payment")
