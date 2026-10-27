#!/usr/bin/env python3
"""Screen a proxy pool's IP intelligence before a run (read-only).

Companion to ``scripts/proxy_pool_probe.py``. That script answers "does this
exit reach the ChatGPT edge?" (Cloudflare 403/429/challenge). This one answers
the other half — "is this exit a stable residential line in the country we
think it is?" — which is what decides whether a batch burns accounts on a
datacenter IP.

    python scripts/proxy_screen.py                             # screen proxy.pool for VN
    python scripts/proxy_screen.py --country PH --workers 8
    python scripts/proxy_screen.py --file proxies.txt --quality-min 70 --out runtime/pool_residential.txt
    python scripts/proxy_screen.py --pool "http://u:p@h:1,http://u:p@h:2" --json

Read-only with respect to accounts and mailboxes: two geo lookups per proxy.
Credentials are redacted in every line this prints; ``--out`` intentionally
contains them (it is a usable pool file) and must stay out of Git — the default
location, ``runtime/``, is already gitignored.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.config import load_merged_config  # noqa: E402
from sms_tool.proxy.registry import resolve_registration  # noqa: E402
from sms_tool.proxy.routing import parse_lane_proxy_pool  # noqa: E402
from sms_tool.proxy.screening import (  # noqa: E402
    aggregate_by_asn,
    format_report,
    screen_pool,
    summarize,
)


def load_pool(args: argparse.Namespace, config: dict) -> list[str]:
    """Resolve the pool to screen: explicit ``--pool`` / ``--file``, else the lane."""
    if args.pool:
        return parse_lane_proxy_pool(args.pool)
    if args.file:
        try:
            lines = Path(args.file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SystemExit(f"cannot read --file {args.file}: {exc}") from exc
        # Pool files may carry a ``#`` header/comment line; parsing one as a
        # proxy would inject a bogus upstream and hide the real ones.
        text = "\n".join(line for line in lines if not line.strip().startswith("#"))
        return parse_lane_proxy_pool(text)
    return resolve_registration(config, lane=args.lane)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen the proxy pool's IP intelligence (read-only).")
    parser.add_argument("--pool", default="", help="comma/newline separated proxies (overrides config)")
    parser.add_argument("--file", default="", help="file with one proxy per line (overrides config)")
    parser.add_argument("--lane", default="protocol_registration", help="config lane when no --pool/--file")
    parser.add_argument("--country", default="", help="target ISO country; empty accepts any")
    parser.add_argument("--workers", type=int, default=8, help="concurrent proxies (1 = sequential)")
    parser.add_argument("--timeout", type=float, default=6.0, help="per-pass timeout seconds")
    parser.add_argument("--quality-min", type=int, default=0, help="minimum residential quality score (0-100)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    parser.add_argument("--out", default="", help="write eligible proxies here (CONTAINS CREDENTIALS)")
    parser.add_argument("--report-out", default="", help="write the full JSON report (masked) here")
    parser.add_argument("--require-eligible", action="store_true", help="exit 2 when nothing is eligible")
    args = parser.parse_args(argv)

    config = load_merged_config()
    pool = load_pool(args, config)
    if not pool:
        print("no proxies resolved (check proxy.pool / --pool / --file)", file=sys.stderr)
        return 1

    started = time.monotonic()
    samples = screen_pool(
        pool,
        country=args.country,
        workers=args.workers,
        quality_min=args.quality_min,
        timeout=args.timeout,
    )
    # Teach the persistent ASN ledger which configured exit left through which
    # ASN. Without this the screening result lived only in this process's geo
    # cache and the next registration batch could not use it.
    from sms_tool.proxy.asn_registry import AsnRegistry

    try:
        AsnRegistry.from_config(config).configure(
            [
                {
                    "proxy": sample.proxy,
                    "asn": sample.asn,
                    "isp": sample.isp,
                    "network_type": sample.network_type,
                    "country": sample.country,
                }
                for sample in samples
            ]
        )
    except Exception as exc:
        print(f"ASN ledger update failed: {type(exc).__name__}", file=sys.stderr)
    counts = summarize(samples)
    elapsed = time.monotonic() - started

    if args.json:
        print(
            json.dumps(
                {
                    "country": args.country.upper(),
                    "quality_min": args.quality_min,
                    "counts": counts,
                    "asns": aggregate_by_asn(samples),
                    "samples": [sample.to_dict() for sample in samples],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"target country : {args.country.upper() or 'any'}")
        print(f"pool size      : {len(pool)}   elapsed={elapsed:.1f}s")
        print(format_report(samples))
        print(
            f"screened={counts['total']} eligible={counts['eligible']} "
            f"rejected={counts['rejected']} asns={counts['asns']}"
        )

    if args.report_out:
        report_path = Path(args.report_out)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "country": args.country.upper(),
                    "counts": counts,
                    "asns": aggregate_by_asn(samples),
                    "samples": [sample.to_dict() for sample in samples],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote masked report -> {report_path}", file=sys.stderr)

    if args.out:
        eligible = [sample.proxy for sample in samples if sample.eligible]
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# screened eligible proxies (country={args.country.upper() or 'any'}, "
            f"quality_min={args.quality_min}, {time.strftime('%Y-%m-%d %H:%M:%S')}) "
            f"eligible/total={len(eligible)}/{len(pool)}\n"
        )
        out_path.write_text(header + "\n".join(eligible) + "\n", encoding="utf-8")
        print(
            f"wrote {len(eligible)} eligible entries -> {out_path} (contains credentials; keep out of Git)",
            file=sys.stderr,
        )

    if args.require_eligible and counts["eligible"] == 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
