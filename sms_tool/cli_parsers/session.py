"""session flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--refresh-session", action="store_true", help="Refresh ChatGPT auth session with protocol requests")
    parser.add_argument("--session-file", default=None, help="Session JSON path for account and payment operations")
    parser.add_argument("--refresh-timeout", type=int, default=300, help="Seconds to wait for interactive auth refresh")
    parser.add_argument("--view-inbox", action="store_true", help="Fetch recent mailbox messages for --email/--session-file and print JSON")
    parser.add_argument("--inbox-limit", type=int, default=20, help="Max messages for --view-inbox")
    parser.add_argument("--gmail-send", action="store_true", help="Send mail through a configured/selected Gmail mailbox")
    parser.add_argument("--gmail-send-to", default=None, help="Recipient list for --gmail-send, separated by comma/newline")
    parser.add_argument("--gmail-send-subject", default=None, help="Subject for --gmail-send")
    parser.add_argument("--gmail-send-body", default=None, help="Plain-text body for --gmail-send")
    parser.add_argument("--gmail-send-html", default=None, help="Optional HTML body for --gmail-send")
    parser.add_argument("--gmail-send-self", action="store_true", help="Send --gmail-send to the Gmail mailbox itself")
    parser.add_argument("--convert-session-json", default=None, help="Convert ChatGPT/Codex session JSON file to another import format")
    parser.add_argument("--convert-format", choices=["cpa", "sub2api", "cockpit", "9router", "codex", "axonhub", "codexmanager"], default="cpa", help="Output format for --convert-session-json")
    parser.add_argument("--convert-output", default=None, help="Optional output path for --convert-session-json")
