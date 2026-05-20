#!/usr/bin/env python3
"""Start the SOCKS5 proxy pool server.

Usage:
    python start_proxy_pool.py
    python start_proxy_pool.py --port 18080 --stats-port 18081
    python start_proxy_pool.py --upstreams "socks5://127.0.0.1:7897,socks5://127.0.0.1:17912"
    python start_proxy_pool.py --config config.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from sms_tool.proxy_pool import Socks5Server, UpstreamProxy

logger = logging.getLogger("proxy_pool")


def _load_upstreams_from_config(path: str) -> list[UpstreamProxy]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    pool_cfg = cfg.get("proxy_pool") or {}
    raw_list = pool_cfg.get("upstreams") or []
    upstreams: list[UpstreamProxy] = []
    for entry in raw_list:
        if isinstance(entry, str):
            upstreams.append(UpstreamProxy.from_url(entry))
        elif isinstance(entry, dict):
            url = entry.get("url") or entry.get("proxy") or ""
            upstreams.append(UpstreamProxy.from_url(
                url, label=entry.get("label", ""),
                priority=entry.get("priority", 0),
            ))
            if entry.get("username"):
                upstreams[-1].username = entry["username"]
            if entry.get("password"):
                upstreams[-1].password = entry["password"]
    return upstreams


def _load_upstreams_from_str(upstream_str: str) -> list[UpstreamProxy]:
    return [UpstreamProxy.from_url(u.strip()) for u in upstream_str.split(",") if u.strip()]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SOCKS5 proxy pool server")
    p.add_argument("--host", default=None, help="Listen host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="SOCKS5 listen port (default: 18080)")
    p.add_argument("--stats-port", type=int, default=None, help="HTTP stats port (default: 18081)")
    p.add_argument("--config", default=None, help="Path to config.json")
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

    # resolve config path
    config_path = args.config
    if not config_path:
        for candidate in ("config.json", os.path.join("sms_tool", "..", "config.json")):
            if os.path.isfile(candidate):
                config_path = candidate
                break

    # load pool settings from config
    pool_cfg: dict = {}
    if config_path and os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            full_cfg = json.load(f)
        pool_cfg = full_cfg.get("proxy_pool") or {}

    host = args.host or pool_cfg.get("listen_host", "127.0.0.1")
    port = args.port or pool_cfg.get("listen_port", 18080)
    stats_port = args.stats_port or pool_cfg.get("stats_port", 18081)
    health_interval = args.health_interval or pool_cfg.get("health_check_interval", 30)
    connect_timeout = args.connect_timeout or pool_cfg.get("connect_timeout", 10)
    max_retries = pool_cfg.get("max_retries", 2)

    # load upstreams
    if args.upstreams:
        upstreams = _load_upstreams_from_str(args.upstreams)
    elif config_path and os.path.isfile(config_path):
        upstreams = _load_upstreams_from_config(config_path)
    else:
        upstreams = [UpstreamProxy.from_url("socks5://127.0.0.1:7897", "clash-default")]

    if not upstreams:
        logger.error("No upstream proxies configured. Use --upstreams or config.json proxy_pool.upstreams")
        sys.exit(1)

    server = Socks5Server(
        listen_host=host,
        listen_port=port,
        upstreams=upstreams,
        stats_port=stats_port,
        health_check_interval=health_interval,
        health_check_timeout=5.0,
        connect_timeout=connect_timeout,
        max_retries=max_retries,
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
