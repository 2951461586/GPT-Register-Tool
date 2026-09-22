"""codex flags for ``cli.build_parser``."""

import argparse


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--export-codex-json", action="store_true", help="Export paid account session as Codex JSON")
    parser.add_argument("--import-cpa", action="store_true", help="Import an existing AT-only session JSON into CPA/SUB2API")
    parser.add_argument("--register-and-import", action="store_true", help="Register new account(s), then import only the successful registrations into CPA/SUB2API")
    parser.add_argument("--import-target", choices=["cpa", "sub2api", "cliproxyapi"], default="cpa", help="Target for --import-cpa and 401 re-import")
    parser.add_argument("--cpa-domain-filter", default=None, help="Only process CPA accounts under this email domain")
    parser.add_argument("--codex-export-dir", default=None, help="Directory for Codex JSON exports")
    parser.add_argument("--cpa-api-url", default=None, help="CPA API base URL, defaults to cpa/cpa_mode.api_url in config.json")
    parser.add_argument("--cpa-api-token", default=None, help="CPA API token, defaults to cpa/cpa_mode.api_token in config.json")
