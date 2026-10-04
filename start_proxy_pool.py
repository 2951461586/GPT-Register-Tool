#!/usr/bin/env python3
"""Start the proxy pool server (SOCKS5 listener, SOCKS5 *or* HTTP upstreams).

Usage:
    python start_proxy_pool.py
    python start_proxy_pool.py --port 18080 --stats-port 18081
    python start_proxy_pool.py --upstreams "socks5://127.0.0.1:7897,socks5://127.0.0.1:17912"

Upstreams come from ``--upstreams`` or, by default, the merged config shards
(``proxy.pool`` in proxy.json when the shard exists). If neither yields a
usable upstream the server exits with an error instead of silently serving a
hard-coded default -- the previous fallback to ``socks5://127.0.0.1:7897``
fired whenever the dead ``config.json proxy_pool.upstreams`` key was absent,
which was always.

Upstream scheme is per-entry: ``socks5``/``socks5h`` use the SOCKS5 handshake,
``http``/``https`` use an HTTP CONNECT tunnel. Clients always speak SOCKS5 to
the listener either way. Accepting HTTP matters because every residential
provider we buy hands out ``http://`` endpoints -- a SOCKS5-only filter emptied
a 30-entry pool down to zero upstreams and exited.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from sms_tool.phone_proxy import redact_proxy_url
from sms_tool.proxy_edge_probe import edge_health
from sms_tool.proxy_health import ProxyHealthTracker
from sms_tool.proxy_pool import Socks5Server, UPSTREAM_SCHEMES, UpstreamProxy

logger = logging.getLogger("proxy_pool")


def _upstreams_from_proxy_cfg(cfg: dict) -> list[UpstreamProxy]:
    """Build upstreams from the merged proxy shard (``proxy.pool``).

    Pool entries are the same strings/dicts the registration path consumes.
    Entries whose scheme the pool cannot dial are skipped with a warning instead
    of being silently coerced into SOCKS5 upstreams that fail at connect time;
    entries without a usable URL are skipped loudly too.
    """
    proxy_cfg = cfg.get("proxy")
    if not isinstance(proxy_cfg, dict):
        proxy_cfg = {}
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
        if scheme and scheme not in UPSTREAM_SCHEMES:
            logger.warning(
                "Skipping proxy.pool entry with an unsupported scheme (%s://): %s",
                scheme,
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
    p.add_argument(
        "--upstreams",
        default=None,
        help="Comma-separated upstream URLs (socks5://, socks5h://, http://, https://)",
    )
    p.add_argument("--health-interval", type=float, default=30.0, help="Health check interval (seconds)")
    p.add_argument(
        "--health-edge-probe",
        action="store_true",
        help=(
            "Verify the ChatGPT edge with a real HTTPS request instead of only "
            "completing a tunnel, so an exit Cloudflare answers with 403 is marked "
            "unhealthy. Off by default (the tunnel-only probe is unchanged)."
        ),
    )
    p.add_argument(
        "--health-timeout",
        type=float,
        default=None,
        help="Per-probe timeout (seconds; default 5 tunnel / 15 edge)",
    )
    p.add_argument("--connect-timeout", type=float, default=10.0, help="Upstream connect timeout (seconds)")
    p.add_argument(
        "--sticky-session-ttl",
        type=float,
        default=0.0,
        help=(
            "Pin a client that offers SOCKS5 username/password auth to one upstream for "
            "this many seconds (0 = disabled, per-connection round-robin). Use this when "
            "a long flow must not hop exits mid-way; clients without credentials are "
            "unaffected."
        ),
    )
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
            'No usable upstreams configured. Pass --upstreams "socks5://host:port,..." '
            "or add proxy.pool entries to the proxy shard (proxy.json). "
            "Supported upstream schemes: %s.",
            ", ".join(UPSTREAM_SCHEMES),
        )
        sys.exit(1)

    host = args.host or "127.0.0.1"
    port = args.port or 18080
    stats_port = args.stats_port or 18081
    health_interval = args.health_interval or 30.0
    health_timeout = args.health_timeout or (15.0 if args.health_edge_probe else 5.0)
    edge_probe = edge_health if args.health_edge_probe else None
    connect_timeout = args.connect_timeout or 10.0
    sticky_ttl = args.sticky_session_ttl or 0.0
    max_retries = 2

    health_tracker = ProxyHealthTracker(health_cfg)

    server = Socks5Server(
        listen_host=host,
        listen_port=port,
        upstreams=upstreams,
        stats_port=stats_port,
        health_check_interval=health_interval,
        health_check_timeout=health_timeout,
        connect_timeout=connect_timeout,
        max_retries=max_retries,
        health_tracker=health_tracker,
        sticky_session_ttl=sticky_ttl,
        edge_probe=edge_probe,
    )

    logger.info("Proxy pool config: host=%s port=%d stats=%d upstreams=%d", host, port, stats_port, len(upstreams))
    for u in upstreams:
        logger.info("  upstream: %s [%s] user=%s", u.label, u.addr, "***" if u.username else "-")

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, server.request_shutdown)
            loop.add_signal_handler(signal.SIGTERM, server.request_shutdown)
        except NotImplementedError:
            # Windows: asyncio cannot install these handlers at all.  CTRL+C still
            # arrives as KeyboardInterrupt, which asyncio.run() turns into the
            # `except KeyboardInterrupt` below, so shutdown is not lost -- only the
            # signal-driven variant is.  Say so once rather than swallowing it
            # silently.
            logger.debug("asyncio signal handlers unavailable on this platform; relying on KeyboardInterrupt")
        await server.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
