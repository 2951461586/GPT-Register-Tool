"""paypal flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--paypal-generation-type", default=None, help="Override PayPal link generation type: hosted_long_url, paypal_direct, or paypal_direct_zero_due")
    parser.add_argument("--list-paypal-links", action="store_true", help="List saved PayPal payment links")
    parser.add_argument("--open-paypal-link", action="store_true", help="Open saved PayPal payment link for --email")
    parser.add_argument("--generate-ba-link", action="store_true", help="Generate PayPal BA link directly from Access Token")
    parser.add_argument("--generate-upi-qr", action="store_true", help="Generate India UPI hosted payment link and QR directly from Access Token")
    parser.add_argument("--auto-pay", action="store_true", help="Automate PayPal payment (reverse protocol first, browser fallback)")
    parser.add_argument("--auto-pay-reverse-only", action="store_true", help="Use reverse protocol only, no browser fallback")
    parser.add_argument("--auto-pay-headless", action="store_true", help="Run auto-pay browser headless")
    parser.add_argument("--auto-pay-timeout", type=int, default=180, help="Seconds to wait for auto-pay completion")
    parser.add_argument("--batch-auto-pay", action="store_true", help="Run auto-pay for all pending accounts in SQLite")
    parser.add_argument("--batch-auto-pay-limit", type=int, default=0, help="Max accounts to process in batch (0=all)")
    parser.add_argument("--list-paypal-ba-queue", action="store_true", help="List durable PayPal BA authorization queue")
    parser.add_argument("--paypal-ba-queue-limit", type=int, default=0, help="Max PayPal BA authorization jobs (0=all)")
