#!/usr/bin/env python3
"""Start the SOCKS5 proxy pool server.

Usage:
    python start_proxy_pool.py
    python start_proxy_pool.py --port 18080 --stats-port 18081
    python start_proxy_pool.py --upstreams "socks5://127.0.0.1:7897,socks5://127.0.0.1:17912"

Upstreams come from ``--upstreams`` or, by default, the merged config shards
(``proxy.pool`` in proxy.json when the shard exists). If neither yields a
SOCKS5 upstream the server exits with an error instead of silently serving a
hard-coded default -- the previous fallback to ``socks5://127.0.0.1:7897``
fired whenever the dead ``config.json proxy_pool.upstreams`` key was absent,
which was always.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from sms_tool.phone_proxy import redact_proxy_url
from sms_tool.proxy_pool import Socks5Server, UpstreamProxy
from sms_tool.proxy_health import ProxyHealthTracker

logger = logging.getLogger("proxy_pool")


def _upstreams_from_proxy_cfg(cfg: dict) -> list[UpstreamProxy]:
    """Build upstreams from the merged proxy shard (``proxy.pool``).

    Pool entries are the same strings/dicts the registration path consumes.
    Non-SOCKS5 entries are skipped with a warning instead of being silently
    turned into SOCKS5 upstreams that fail at connect time; entries without a
    usable URL are skipped loudly too.
    """
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    raw_list = proxy_cfg.get("pool") or []
    upstreams: list[UpstreamProxy] = []
    for entry in raw_list:
        if isinstance(entry, str):
            url, label, priority, username, password = entry.strip(), "", 0, "", ""
        elif isinstance(entry, dict):
            url = str(entry.get("url") or entry.get("proxy") or "").strip()
            label = str(entry.get("label") or "")
            try:
                priority = int(entry.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            username = str(entry.get("username") or "")
            password = str(entry.get("password") or "")
        else:
            logger.warning("Ignoring malformed proxy.pool entry: %r", entry)
            continue
        if not url:
            logger.warning("Ignoring proxy.pool entry without url: %r", entry)
            continue
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme not in ("socks5", "socks5h"):
            logger.warning(
                "Skipping non-SOCKS5 proxy.pool entry (%s://): %s",
                scheme or "?",
                redact_proxy_url(url),
            )
            continue
        try:
            upstream = UpstreamProxy.from_url(url, label=label, priority=priority)
        except ValueError as exc:
            logger.error("Refusing proxy.pool entry: %s", exc)
            continue
        if username:
            upstream.username = username
        if password:
            upstream.password = password
        upstreams.append(upstream)
    return upstreams


def _load_upstreams_from_str(upstream_str: str) -> list[UpstreamProxy]:
    upstreams = []
    for raw in upstream_str.split(","):
        url = raw.strip()
        if not url:
            continue
        upstreams.append(UpstreamProxy.from_url(url))
    return upstreams


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SOCKS5 proxy pool server")
    p.add_argument("--host", default=None, help="Listen host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="SOCKS5 listen port (default: 18080)")
    p.add_argument("--stats-port", type=int, default=None, help="HTTP stats port (default: 18081)")
    p.add_argument("--upstreams", default=None, help="Comma-separated upstream socks5:// URLs")
    p.add_argument("--health-interval", type=float, default=30.0, help="Health check interval (seconds)")
    p.add_argument("--connect-timeout", type=float, default=10.0, help="Upstream connect timeout (seconds)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # P1-2: share the on-disk ProxyHealthTracker with the registration/remail
    # paths so the SOCKS5 pool's health becomes visible to the proxies that
    # consume the same egress endpoints.
    if args.upstreams:
        upstreams = _load_upstreams_from_str(args.upstreams)
        health_cfg: dict = {}
    else:
        from sms_tool.config import load_merged_config

        try:
            health_cfg = load_merged_config() or {}
        except Exception as exc:
            logger.error("Failed to load config shards: %s", exc)
            sys.exit(1)
        upstreams = _upstreams_from_proxy_cfg(health_cfg)

    if not upstreams:
        logger.error(
            "No SOCKS5 upstreams configured. Pass --upstreams \"socks5://host:port,...\" "
            "or add proxy.pool entries to the proxy shard (proxy.json)."
        )
        sys.exit(1)

    host = args.host or "127.0.0.1"
    port = args.port or 18080
    stats_port = args.stats_port or 18081
    health_interval = args.health_interval or 30.0
    connect_timeout = args.connect_timeout or 10.0
    max_retries = 2

    health_tracker = ProxyHealthTracker(health_cfg)

    server = Socks5Server(
        listen_host=host,
        listen_port=port,
        upstreams=upstreams,
        stats_port=stats_port,
        health_check_interval=health_interval,
        health_check_timeout=5.0,
        connect_timeout=connect_timeout,
        max_retries=max_retries,
        health_tracker=health_tracker,
    )

    logger.info("Proxy pool config: host=%s port=%d stats=%d upstreams=%d",
                host, port, stats_port, len(upstreams))
    for u in upstreams:
        logger.info("  upstream: %s [%s] user=%s", u.label, u.addr, "***" if u.username else "-")

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, server.request_shutdown)
            loop.add_signal_handler(signal.SIGTERM, server.request_shutdown)
        except NotImplementedError:
            # Windows: only SIGINT works, KeyboardInterrupt handled below
            pass
        await server.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
