#!/usr/bin/env python3
"""Probe the configured proxy pool against the ChatGPT edge (read-only).

A residential pool always contains some exits that Cloudflare answers with a
403 challenge for ``chatgpt.com``.  ``Socks5Server``'s own health probe cannot
see that: it opens a tunnel to ``cloudflare.com:443`` and closes it without
sending an HTTP request, so a blocked exit is marked healthy.  Registration
only finds out per account, through ``registration_network_preflight``.

This script asks the same question *before* a run, for the whole pool, and
writes the usable subset out so it can be pinned as the registration pool.

    python scripts/proxy_pool_probe.py                       # probe proxy.pool
    python scripts/proxy_pool_probe.py --workers 8 --json
    python scripts/proxy_pool_probe.py --pool "http://u:p@h:1,http://u:p@h:2"
    python scripts/proxy_pool_probe.py --file proxies.txt --out runtime/pool_clean.txt
    python scripts/proxy_pool_probe.py --require-clean        # exit 2 if none clean

Read-only with respect to accounts and mailboxes: it performs one anonymous GET
per proxy.  Credentials are redacted in all stdout.  ``--out`` *does* contain
credentials (that is what a usable pool file is) -- it must stay out of Git; the
default location, ``runtime/``, is already gitignored.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.config import load_merged_config  # noqa: E402
from sms_tool.proxy_edge_probe import (  # noqa: E402
    BLOCKED,
    CLEAN,
    DEAD,
    DEGRADED,
    DEFAULT_CHAT_BASE,
    EdgeVerdict,
    probe_openai_edge,
)
from sms_tool.proxy_registry import resolve_registration  # noqa: E402
from sms_tool.proxy_routing import parse_lane_proxy_pool  # noqa: E402

_ORDER = {CLEAN: 0, DEGRADED: 1, BLOCKED: 2, DEAD: 3}


def _chat_base(config: dict) -> str:
    chatgpt = config.get("chatgpt")
    if not isinstance(chatgpt, dict):
        chatgpt = {}
    return str(chatgpt.get("chat_base_url") or DEFAULT_CHAT_BASE).rstrip("/") or DEFAULT_CHAT_BASE


def load_pool(args: argparse.Namespace, config: dict) -> list[str]:
    """Resolve the pool to probe: explicit ``--pool`` / ``--file``, else the lane."""
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


def probe_pool(
    pool: list[str],
    *,
    chat_base: str,
    timeout: float,
    workers: int,
    probe=probe_openai_edge,
) -> list[EdgeVerdict]:
    """Probe every entry, preserving input order.

    ``probe`` is injectable so the ordering/aggregation contract is testable
    without a network.  It is called as ``probe(proxy, chat_base=..., timeout=...)``.
    """

    def _one(proxy: str) -> EdgeVerdict:
        return probe(proxy, chat_base=chat_base, timeout=timeout)

    try:
        worker_count = int(workers)
    except (TypeError, ValueError):
        worker_count = 1
    if worker_count <= 1 or len(pool) <= 1:
        return [_one(proxy) for proxy in pool]
    max_workers = max(1, min(worker_count, len(pool), 64))
    with ThreadPoolExecutor(max_workers=max_workers) as pool_exec:
        return list(pool_exec.map(_one, pool))


def summarize(verdicts: list[EdgeVerdict]) -> dict[str, int]:
    counts = {CLEAN: 0, DEGRADED: 0, BLOCKED: 0, DEAD: 0}
    for verdict in verdicts:
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
    return counts


def clean_proxies(verdicts: list[EdgeVerdict], pool: list[str]) -> list[str]:
    """Raw (credential-bearing) URLs whose verdict is ``clean``, in input order."""
    by_redacted = {v.proxy: v.status for v in verdicts}
    out: list[str] = []
    from sms_tool.phone_proxy import redact_proxy_url

    for proxy in pool:
        if by_redacted.get(redact_proxy_url(proxy)) == CLEAN:
            out.append(proxy)
    return out


def format_report(verdicts: list[EdgeVerdict]) -> str:
    lines: list[str] = []
    for verdict in sorted(verdicts, key=lambda v: _ORDER.get(v.status, 9)):
        detail = f"http {verdict.http_status}" if verdict.http_status else (verdict.error or "-")
        if verdict.blocked_by_cloudflare:
            detail += " cloudflare-challenge"
        lines.append(f"  [{verdict.status:>8}] {verdict.proxy:<46} {detail} ({verdict.elapsed_ms}ms)")
    counts = summarize(verdicts)
    lines.append(
        f"probed={len(verdicts)} clean={counts[CLEAN]} degraded={counts[DEGRADED]} "
        f"blocked={counts[BLOCKED]} dead={counts[DEAD]}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the proxy pool against the ChatGPT edge (read-only).")
    parser.add_argument("--pool", default="", help="comma/newline separated proxies (overrides config)")
    parser.add_argument("--file", default="", help="file with one proxy per line (overrides config)")
    parser.add_argument("--lane", default="protocol_registration", help="config lane when no --pool/--file")
    parser.add_argument("--chat-base", default="", help="override the ChatGPT base URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-probe timeout seconds")
    parser.add_argument("--workers", type=int, default=8, help="concurrent probes (1 = sequential)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text table")
    parser.add_argument("--out", default="", help="write the clean pool here (CONTAINS CREDENTIALS)")
    parser.add_argument("--require-clean", action="store_true", help="exit 2 when no proxy is clean")
    args = parser.parse_args(argv)

    config = load_merged_config()
    pool = load_pool(args, config)
    if not pool:
        print("no proxies resolved (check proxy.pool / --pool / --file)", file=sys.stderr)
        return 1

    chat_base = args.chat_base or _chat_base(config)
    started = time.monotonic()
    verdicts = probe_pool(pool, chat_base=chat_base, timeout=args.timeout, workers=args.workers)
    counts = summarize(verdicts)

    if args.json:
        print(
            json.dumps(
                {
                    "chat_base": chat_base,
                    "counts": counts,
                    "verdicts": [v.to_dict() for v in verdicts],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"chat base : {chat_base}")
        print(f"pool size : {len(pool)}   elapsed={time.monotonic() - started:.1f}s")
        print(format_report(verdicts))

    if args.out:
        clean = clean_proxies(verdicts, pool)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# clean proxies for {chat_base} (auto, {time.strftime('%Y-%m-%d %H:%M:%S')}) "
            f"clean/total={len(clean)}/{len(pool)}\n"
        )
        out_path.write_text(header + "\n".join(clean) + "\n", encoding="utf-8")
        print(
            f"wrote {len(clean)} clean entries -> {out_path} (contains credentials; keep out of Git)", file=sys.stderr
        )

    if args.require_clean and counts[CLEAN] == 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
