"""one_click flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--one-click-sms",
        action="store_true",
        help="Run Codex OAuth login for selected account(s), complete phone SMS verification, and store RT",
    )
    parser.add_argument(
        "--one-click-scan",
        action="store_true",
        help="Batch OAuth scan accounts for account_deactivated and add-phone/secondary phone verification",
    )
    parser.add_argument(
        "--scan-relogin-mode",
        choices=["auto", "web_session", "codex_oauth"],
        default="auto",
        help="Relogin mode for --one-click-scan --quota-auto-relogin; auto tries RT, web session, protocol email-OTP, then Codex OAuth",
    )
    parser.add_argument(
        "--scan-deep-probe",
        action="store_true",
        help="Allow --one-click-scan to run OAuth/email-OTP deep probing; default is AT probe-only",
    )
