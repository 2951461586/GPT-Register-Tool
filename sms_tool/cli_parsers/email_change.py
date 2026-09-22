"""email_change flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--change-email", action="store_true", help="Batch change ChatGPT protocol email addresses")
    parser.add_argument("--change-email-provider", choices=["remail", "cfworker", "smailr", "icloud", "outlook", "hotmail"], default=None)
    parser.add_argument("--change-email-mailbox-file", default=None, help="Credential pool for persistent target email providers")
    parser.add_argument("--change-email-workers", type=int, default=None)
    parser.add_argument("--change-email-timeout", type=int, default=180)
    parser.add_argument("--change-email-otp-timeout", type=int, default=300)
    parser.add_argument("--change-email-service-mode", choices=["code", "purchase"], default="purchase")
    parser.add_argument("--change-email-smailr-domain", default="")
