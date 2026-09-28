#!/usr/bin/env python3
"""Print the unified proxy-pool census (credential-free).

Resolves every egress lane through :mod:`sms_tool.proxy_registry` -- the single
facade over the registration, mailbox and payment resolvers -- and prints the
distinct ``host:port`` endpoints.  Credentials are never printed.

    python scripts/proxy_census.py
    python scripts/proxy_census.py --methods paypal,upi,kakao,momo
    python scripts/proxy_census.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool import proxy_registry as proxy_registry  # noqa: E402
from sms_tool.config import load_merged_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the unified proxy-pool census (credential-free).")
    parser.add_argument("--methods", default="paypal,upi,kakao,momo", help="comma-separated payment methods")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text table")
    args = parser.parse_args(argv)

    methods = tuple(item.strip() for item in args.methods.split(",") if item.strip())
    report = proxy_registry.census(load_merged_config(), methods=methods)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=list))
    else:
        print(proxy_registry.format_census(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
