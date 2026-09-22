"""omakse flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
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
