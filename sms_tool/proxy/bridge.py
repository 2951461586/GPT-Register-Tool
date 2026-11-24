"""In-process local proxy bridge: one-hop or two-hop upstream proxy -> local SOCKS5 endpoint.

Given an upstream proxy (http/https/socks5, optionally authenticated) — and
optionally a first-hop relay ``hop_proxy`` (for example a local Clash/Xray
SOCKS5/HTTP listener when the host cannot dial an overseas residential proxy
directly) — :class:`LocalProxyBridge` starts a local
``socks5h://127.0.0.1:<port>`` endpoint that browser sessions
(nodriver / Playwright / Camoufox / manual config) can point to without caring
about the upstream format, hop chaining, or credentials.

It reuses the SOCKS5 and HTTP CONNECT handshake machinery in ``proxy_pool`` so
a single unified pure-asyncio bridge covers one-hop and two-hop chaining across
all HTTP/SOCKS5 combinations.

Usage::

    from sms_tool.proxy.entry import parse_proxy
    from sms_tool.proxy.bridge import LocalProxyBridge

    with LocalProxyBridge(
        upstream=parse_proxy("host:port:user:pass"),
        hop_proxy=parse_proxy("socks5://127.0.0.1:7890"),
    ) as bridge:
        browser_url = bridge.local_url   # "socks5h://127.0.0.1:12345"
        ...
    # bridge auto-stopped on context exit
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .entry import ProxyEntry, _SOCKS_DEFAULT_PORT, _normalize_scheme, parse_proxy
from .pool import (
    _ATYP_DOMAIN,
    _ATYP_IPV4,
    _ATYP_IPV6,
    _CMD_CONNECT,
    _NO_AUTH,
    _REP_ADDR_NOT_SUPPORTED,
    _REP_CMD_NOT_SUPPORTED,
    _REP_CONN_REFUSED,
    _REP_SUCCEEDED,
    _SOCKS5_VER,
    UpstreamProxy,
    _build_socks5_reply,
    _http_connect_handshake,
    _read_exact,
    _read_socks5_addr,
    _socks5_handshake,
)

logger = logging.getLogger("proxy_bridge")

_HTTP_SCHEMES = {"http", "https"}
_HTTP_DEFAULT_PORT = 8080
#: RFC 1928 reply code: connection not allowed by ruleset.
_REP_NOT_ALLOWED = 0x02
_HOP_ENV_KEYS = ("GPT_PROXY_CHAIN_HOP", "PROXY_CHAIN_HOP")
_MAX_DEST_STATS = 512
_DEAD_IP_TTL_SECONDS = 60.0


def _format_host_port(host: str, port: int) -> str:
    clean = str(host or "").strip()
    if ":" in clean and not clean.startswith("["):
        return f"[{clean}]:{port}"
    return f"{clean}:{port}"


def _addr_type(host: str) -> int:
    try:
        socket.inet_aton(host)
        return _ATYP_IPV4
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return _ATYP_IPV6
    except OSError:
        return _ATYP_DOMAIN


def _default_port_for_scheme(scheme: str) -> int:
    return _HTTP_DEFAULT_PORT if scheme in _HTTP_SCHEMES else _SOCKS_DEFAULT_PORT


def _as_upstream_proxy(entry: ProxyEntry) -> UpstreamProxy:
    scheme = _normalize_scheme(entry.scheme) or "http"
    return UpstreamProxy(
        host=entry.host,
        port=entry.port or _default_port_for_scheme(scheme),
        username=entry.username,
        password=entry.password,
        label=entry.masked,
        scheme=scheme,
    )


def _resolve_hop_proxy(hop_proxy: Any, *, allow_env: bool = False) -> ProxyEntry | None:
    """Normalize an explicit ``hop_proxy`` or optional environment chain hop."""
    if isinstance(hop_proxy, ProxyEntry):
        return hop_proxy
    raw = str(hop_proxy or "").strip() if hop_proxy is not None else ""
    if not raw and allow_env and hop_proxy is None:
        for key in _HOP_ENV_KEYS:
            candidate = str(os.environ.get(key) or "").strip()
            if candidate:
                raw = candidate
                break
    if not raw:
        return None
    parsed = parse_proxy(raw)
    if parsed is None:
        raise ValueError("proxy_bridge: invalid hop_proxy")
    return parsed


def _normalize_blocked_hosts(hosts: Any) -> tuple[str, ...]:
    if not hosts:
        return ()
    if isinstance(hosts, str):
        raw = [part.strip() for part in hosts.replace(",", "\n").splitlines()]
    else:
        raw = [str(part or "").strip() for part in hosts]
    cleaned: list[str] = []
    for item in raw:
        host = item.lower().strip(".")
        if host and host not in cleaned:
            cleaned.append(host)
    return tuple(cleaned)


def _host_matches_blocked(host: str, rules: tuple[str, ...]) -> bool:
    """Exact domain or subdomain-boundary match (never partial label prefix)."""
    candidate = str(host or "").strip().lower().rstrip(".")
    if not candidate or not rules:
        return False
    return any(candidate == rule or candidate.endswith("." + rule) for rule in rules)


@dataclass
class _DestinationStats:
    upload_bytes: int = 0
    download_bytes: int = 0
    connections: int = 0
    failed_connections: int = 0
    blocked_connections: int = 0
    retries: int = 0
    recovered_connections: int = 0

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


async def _close_writer(w: asyncio.StreamWriter) -> None:
    """Best-effort close; Windows raises if the peer already reset the socket."""
    try:
        w.close()
    except Exception:
        return
    try:
        await w.wait_closed()
    except Exception:
        pass


@dataclass
class LocalProxyBridge:
    """Expose a single upstream proxy as a local SOCKS5 endpoint.

    Two lifecycle modes are supported:

    - **async** (inside an existing event loop, e.g. a nodriver ``uc.loop()``):
      ``await bridge.async_start()`` … ``await bridge.async_stop()``, or the
      ``async with`` context manager.  The local server runs on the caller's
      loop.
    - **sync** (standalone / CLI / tests): ``bridge.start()`` … ``bridge.stop()``
      run the local server on a dedicated background thread+loop so it actually
      serves connections without blocking the caller.
    """

    upstream: ProxyEntry | None = None
    hop_proxy: ProxyEntry | str | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 0  # 0 => pick a free port
    connect_timeout: float = 10.0
    pipe_buf_size: int = 65536
    blocked_hosts: tuple[str, ...] = ()
    connect_retries: int = 0

    _server: asyncio.Server | None = field(default=None, init=False, repr=False)
    _port: int = field(default=0, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stats_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _dest_stats: dict[str, _DestinationStats] = field(default_factory=dict, init=False, repr=False)
    _active_tasks: dict[int, asyncio.Task[Any]] = field(default_factory=dict, init=False, repr=False)
    _dead_ips: dict[tuple[str, int, str], float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.blocked_hosts = _normalize_blocked_hosts(self.blocked_hosts)
        self.connect_retries = max(0, min(int(self.connect_retries or 0), 3))
        if isinstance(self.upstream, str):
            parsed_upstream = parse_proxy(self.upstream)
            if parsed_upstream is None:
                raise ValueError("proxy_bridge: invalid upstream proxy")
            self.upstream = parsed_upstream
        self.hop_proxy = _resolve_hop_proxy(self.hop_proxy, allow_env=False)

    def _route_label(self) -> str:
        upstream = self.upstream
        upstream_label = upstream.masked if isinstance(upstream, ProxyEntry) else "<none>"
        if isinstance(self.hop_proxy, ProxyEntry):
            return f"hop {self.hop_proxy.masked} -> upstream {upstream_label}"
        return f"upstream {upstream_label}"

    def _dest_entry(self, target: str) -> _DestinationStats:
        entry = self._dest_stats.get(target)
        if entry is None:
            if len(self._dest_stats) >= _MAX_DEST_STATS:
                oldest_key = next(iter(self._dest_stats))
                self._dest_stats.pop(oldest_key, None)
            entry = _DestinationStats()
            self._dest_stats[target] = entry
        return entry

    def traffic_snapshot(self) -> dict[str, Any]:
        """Credential-free per-destination transport bytes and block/retry counters."""
        with self._stats_lock:
            items = [
                {
                    "target": target,
                    "upload_bytes": stat.upload_bytes,
                    "download_bytes": stat.download_bytes,
                    "total_bytes": stat.total_bytes,
                    "connections": stat.connections,
                    "failed_connections": stat.failed_connections,
                    "blocked_connections": stat.blocked_connections,
                    "retries": stat.retries,
                    "recovered_connections": stat.recovered_connections,
                }
                for target, stat in self._dest_stats.items()
            ]
        items.sort(key=lambda row: (row["total_bytes"], row["blocked_connections"], row["connections"]), reverse=True)
        upload = sum(row["upload_bytes"] for row in items)
        download = sum(row["download_bytes"] for row in items)
        return {
            "upload_bytes": upload,
            "download_bytes": download,
            "total_bytes": upload + download,
            "connections": sum(row["connections"] for row in items),
            "failed_connections": sum(row["failed_connections"] for row in items),
            "blocked_connections": sum(row["blocked_connections"] for row in items),
            "connect_retry_count": sum(row["retries"] for row in items),
            "recovered_connection_count": sum(row["recovered_connections"] for row in items),
            "destinations": items,
        }

    # ── async lifecycle (run on the caller's loop) ─────────────────────────

    async def async_start(self) -> int:
        """Start the local server on the *current running* loop.  Returns port."""
        if self._server is not None:
            return self._port
        if self.upstream is None:
            raise RuntimeError("proxy_bridge: no upstream proxy configured")
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._on_client, self.listen_host, self.listen_port)
        socket_name = self._server.sockets[0].getsockname()
        self._port = int(socket_name[1])
        logger.info(
            "proxy_bridge listening on %s:%s -> %s (async)",
            self.listen_host,
            self._port,
            self._route_label(),
        )
        return self._port

    async def async_stop(self) -> None:
        """Stop the local server and cancel active relays (keeps the caller's loop intact)."""
        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("proxy_bridge async_stop error: %s", exc)
        pending = [task for task in list(self._active_tasks.values()) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._active_tasks.clear()
        self._server = None
        self._loop = None
        self._port = 0
        logger.info("proxy_bridge stopped (async)")

    async def __aenter__(self) -> "LocalProxyBridge":
        await self.async_start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.async_stop()

    # ── sync lifecycle (standalone background thread + loop) ───────────────

    def start(self) -> int:
        """Start the local SOCKS5 server on a background thread.  Returns port.

        The event loop is created **and torn down inside the server thread**.
        Doing the setup in the calling thread instead
        (``loop.run_until_complete(start_server(...))``) raises
        ``RuntimeError: Cannot run the event loop while another loop is running``
        whenever the caller already owns one — which is exactly what happens
        when Playwright's sync API holds the thread, or when a pooled Camoufox
        session keeps its loop alive across a batch retry.  That failure also
        leaves the ``start_server`` coroutine un-awaited, so the resulting
        RuntimeWarning points at an unrelated frame and hides the real cause.
        """
        if self._server is not None:
            return self._port
        if self.upstream is None:
            raise RuntimeError("proxy_bridge: no upstream proxy configured")
        route_label = self._route_label()

        ready = threading.Event()
        failure: list[BaseException] = []

        def _serve() -> None:
            # Always build a dedicated loop *inside the server thread*.  Reusing a
            # loop handed in by the caller would run it on two threads at once
            # (caller's run_forever + ours) and is exactly what produced the
            # "Cannot run the event loop while another loop is running" crash.
            inner = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(inner)
                self._loop = inner
                server = inner.run_until_complete(
                    asyncio.start_server(self._on_client, self.listen_host, self.listen_port)
                )
                self._server = server
                self._port = int(server.sockets[0].getsockname()[1])
                logger.info(
                    "proxy_bridge listening on %s:%s -> %s (sync)",
                    self.listen_host,
                    self._port,
                    route_label,
                )
                ready.set()
                inner.run_forever()
            except BaseException as exc:  # noqa: BLE001 - surface to the caller
                failure.append(exc)
                ready.set()
            finally:
                self._shutdown_loop(inner)

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()
        if not ready.wait(timeout=15):
            raise RuntimeError("proxy_bridge: server did not become ready in time")
        if failure:
            raise failure[0]
        if not self._port:
            raise RuntimeError("proxy_bridge: failed to bind a local port")
        return self._port

    @property
    def local_url(self) -> str:
        """``socks5h://127.0.0.1:<port>`` for the browser to point to."""
        if not self._port:
            raise RuntimeError("proxy_bridge: not started")
        return f"socks5h://{_format_host_port(self.listen_host, self._port)}"

    def stop(self) -> None:
        """Stop the local server and its background thread/loop."""
        loop = self._loop
        server = self._server
        if server is not None and loop is not None and loop.is_running():
            # Let the background loop close the server so wait_closed is awaited.
            try:
                future = asyncio.run_coroutine_threadsafe(self._close_server(server), loop)
                future.result(timeout=3)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("proxy_bridge stop close error: %s", exc)
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # pragma: no cover - defensive
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        # The loop is fully shut down inside the server thread (see _serve's
        # finally -> _shutdown_loop); calling run_until_complete here would hit
        # a closed loop, so teardown must NOT happen on the caller thread.
        self._loop = None
        self._server = None
        self._port = 0
        logger.info("proxy_bridge stopped")

    @staticmethod
    def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
        """Drain and close a loop.  Only ever called from the owning thread.

        ``run_until_complete`` must not be called from an arbitrary thread while
        another loop is running there, so loop teardown lives here (inside the
        server thread) instead of in :meth:`stop`.
        """
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("proxy_bridge loop shutdown: %s", exc)
        finally:
            try:
                loop.close()
            except Exception:  # pragma: no cover - defensive
                pass

    @staticmethod
    async def _close_server(server: asyncio.Server) -> None:
        server.close()
        await server.wait_closed()

    def __enter__(self) -> "LocalProxyBridge":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── client handler ─────────────────────────────────────────────────────

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._active_tasks[id(current)] = current
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("proxy_bridge client error: %s", exc)
        finally:
            if current is not None:
                self._active_tasks.pop(id(current), None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # greeting
        try:
            hdr = await asyncio.wait_for(_read_exact(reader, 2), timeout=10)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            return
        ver, nmethods = hdr[0], hdr[1]
        if ver != _SOCKS5_VER:
            return
        try:
            await asyncio.wait_for(_read_exact(reader, nmethods), timeout=10)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            return
        writer.write(bytes([_SOCKS5_VER, _NO_AUTH]))  # local: no auth needed
        await writer.drain()

        # CONNECT request
        try:
            req_hdr = await asyncio.wait_for(_read_exact(reader, 4), timeout=10)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            return
        ver, cmd, _, atyp = req_hdr
        if ver != _SOCKS5_VER:
            return
        if cmd != _CMD_CONNECT:
            writer.write(_build_socks5_reply(_REP_CMD_NOT_SUPPORTED))
            await writer.drain()
            return
        try:
            dest_host, dest_port = await asyncio.wait_for(_read_socks5_addr(reader, atyp), timeout=10)
        except (asyncio.TimeoutError, ValueError, ConnectionError, asyncio.IncompleteReadError):
            writer.write(_build_socks5_reply(_REP_ADDR_NOT_SUPPORTED))
            await writer.drain()
            return

        target = f"{dest_host}:{dest_port}"
        if self.blocked_hosts and _host_matches_blocked(dest_host, self.blocked_hosts):
            with self._stats_lock:
                self._dest_entry(target).blocked_connections += 1
            logger.debug("proxy_bridge blocked host %s before upstream connect", target)
            writer.write(_build_socks5_reply(_REP_NOT_ALLOWED))
            await writer.drain()
            return

        with self._stats_lock:
            self._dest_entry(target).connections += 1

        # connect upstream (bounded retry before forwarding any client data)
        up_r: asyncio.StreamReader | None = None
        up_w: asyncio.StreamWriter | None = None
        last_exc: Exception | None = None
        max_attempts = 1 + self.connect_retries
        for attempt in range(max_attempts):
            try:
                up_r, up_w = await asyncio.wait_for(
                    self._connect_upstream(dest_host, dest_port, atyp),
                    timeout=self.connect_timeout,
                )
                if attempt > 0:
                    with self._stats_lock:
                        self._dest_entry(target).recovered_connections += 1
                break
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    with self._stats_lock:
                        self._dest_entry(target).retries += 1
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                break
            except Exception as exc:
                last_exc = exc
                break

        if up_r is None or up_w is None:
            with self._stats_lock:
                self._dest_entry(target).failed_connections += 1
            logger.debug(
                "proxy_bridge upstream connect failed for %s: %s",
                target,
                last_exc,
            )
            writer.write(_build_socks5_reply(_REP_CONN_REFUSED))
            await writer.drain()
            return

        writer.write(_build_socks5_reply(_REP_SUCCEEDED))
        await writer.drain()

        try:
            await self._relay(reader, writer, up_r, up_w, target=target)
        finally:
            try:
                up_w.close()
                await up_w.wait_closed()
            except Exception:
                pass

    # ── upstream connection ────────────────────────────────────────────────

    async def _open_tcp_with_failover(
        self,
        host: str,
        port: int,
        *,
        use_tls: bool = False,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Dial ``host:port`` with multi-A-record dead-IP failover and optional TLS."""
        timeout = self.connect_timeout
        ssl_ctx = ssl.create_default_context() if use_tls else None
        loop = asyncio.get_running_loop()
        addrs: list[str] = []
        if _addr_type(host) == _ATYP_DOMAIN:
            try:
                infos = await asyncio.wait_for(
                    loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                    timeout=min(timeout, 5.0),
                )
                for info in infos:
                    ip = str(info[4][0])
                    if ip not in addrs:
                        addrs.append(ip)
            except (OSError, asyncio.TimeoutError):
                addrs = []
        if len(addrs) <= 1:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    host,
                    port,
                    ssl=ssl_ctx,
                    server_hostname=host if use_tls else None,
                ),
                timeout=timeout,
            )
        now = time.monotonic()
        healthy = [ip for ip in addrs if self._dead_ips.get((host, port, ip), 0.0) <= now]
        ordered = healthy + [ip for ip in addrs if ip not in healthy]
        per_ip_timeout = max(2.0, min(timeout, timeout / min(len(ordered), 2)))
        last_exc: Exception | None = None
        for ip in ordered[:4]:
            try:
                conn = await asyncio.wait_for(
                    asyncio.open_connection(
                        ip,
                        port,
                        ssl=ssl_ctx,
                        server_hostname=host if use_tls else None,
                    ),
                    timeout=per_ip_timeout,
                )
                self._dead_ips.pop((host, port, ip), None)
                return conn
            except (asyncio.TimeoutError, OSError) as exc:
                self._dead_ips[(host, port, ip)] = time.monotonic() + _DEAD_IP_TTL_SECONDS
                last_exc = exc
        raise ConnectionError(f"all resolved gateway IPs failed for {host}:{port}: {last_exc}") from last_exc

    async def _open_proxy_hop(
        self,
        entry: ProxyEntry,
        dest_host: str,
        dest_port: int,
        dest_atyp: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        scheme = _normalize_scheme(entry.scheme) or "http"
        if scheme in _HTTP_SCHEMES:
            return await self._connect_http_upstream(entry, dest_host, dest_port)
        port = entry.port or _SOCKS_DEFAULT_PORT
        try:
            r, w = await self._open_tcp_with_failover(entry.host, port, use_tls=False)
        except (asyncio.TimeoutError, OSError) as exc:
            raise ConnectionError(
                f"SOCKS5 upstream connect failed within {self.connect_timeout}s: {entry.masked}"
            ) from exc
        try:
            await _socks5_handshake(
                r, w, _as_upstream_proxy(entry), dest_host, dest_port, dest_atyp, self.connect_timeout
            )
        except (
            asyncio.CancelledError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
            ValueError,
        ):
            await _close_writer(w)
            raise
        return r, w

    async def _tunnel_on_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        entry: ProxyEntry,
        dest_host: str,
        dest_port: int,
        dest_atyp: int,
    ) -> None:
        scheme = _normalize_scheme(entry.scheme) or "http"
        upstream = _as_upstream_proxy(entry)
        if scheme == "https":
            loop = asyncio.get_running_loop()
            ssl_ctx = ssl.create_default_context()
            protocol = getattr(writer, "_protocol")
            transport = await asyncio.wait_for(
                loop.start_tls(writer.transport, protocol, ssl_ctx, server_hostname=entry.host),
                timeout=self.connect_timeout,
            )
            setattr(writer, "_transport", transport)
        if scheme in _HTTP_SCHEMES:
            await _http_connect_handshake(reader, writer, upstream, dest_host, dest_port, self.connect_timeout)
            return
        await _socks5_handshake(reader, writer, upstream, dest_host, dest_port, dest_atyp, self.connect_timeout)

    async def _connect_upstream(
        self, dest_host: str, dest_port: int, dest_atyp: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        upstream = self.upstream
        if upstream is None:
            raise RuntimeError("no upstream")
        if self.hop_proxy is None:
            return await self._open_proxy_hop(upstream, dest_host, dest_port, dest_atyp)

        hop = self.hop_proxy
        assert isinstance(hop, ProxyEntry)
        upstream_scheme = _normalize_scheme(upstream.scheme) or "http"
        upstream_port = upstream.port or _default_port_for_scheme(upstream_scheme)
        upstream_atyp = _addr_type(upstream.host)
        reader, writer = await self._open_proxy_hop(hop, upstream.host, upstream_port, upstream_atyp)
        try:
            await self._tunnel_on_stream(reader, writer, upstream, dest_host, dest_port, dest_atyp)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError) as exc:
            await _close_writer(writer)
            raise ConnectionError(
                f"chained upstream handshake failed via {hop.masked} -> {upstream.masked} ({exc})"
            ) from exc
        except asyncio.CancelledError:
            await _close_writer(writer)
            raise
        return reader, writer

    async def _connect_http_upstream(
        self, upstream: ProxyEntry, dest_host: str, dest_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """HTTP CONNECT tunnel to the destination through an http(s) upstream."""
        scheme = _normalize_scheme(upstream.scheme) or "http"
        port = upstream.port or (443 if scheme == "https" else _HTTP_DEFAULT_PORT)
        timeout = self.connect_timeout
        try:
            r, w = await self._open_tcp_with_failover(upstream.host, port, use_tls=(scheme == "https"))
        except (asyncio.TimeoutError, OSError) as exc:
            raise ConnectionError(f"HTTP upstream connect failed within {timeout}s: {upstream.masked}") from exc
        try:
            authority = _format_host_port(dest_host, dest_port)
            lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
            if upstream.username:
                token = _basic_auth_header(upstream.username, upstream.password)
                lines.append(f"Proxy-Authorization: Basic {token}")
            lines.append("Proxy-Connection: keep-alive")
            lines.append("")
            lines.append("")
            w.write("\r\n".join(lines).encode("ascii", errors="replace"))
            await asyncio.wait_for(w.drain(), timeout=timeout)
            status_line = await asyncio.wait_for(r.readline(), timeout=timeout)
            status_text = status_line.decode("ascii", errors="replace")
            code = int(status_text.split(" ", 2)[1]) if len(status_text.split(" ", 2)) > 1 else 0
            while True:
                line = await asyncio.wait_for(r.readline(), timeout=timeout)
                if line in (b"\r\n", b"\n", b""):
                    break
        except (asyncio.TimeoutError, ValueError, ConnectionError, OSError) as exc:
            await _close_writer(w)
            raise ConnectionError(f"HTTP CONNECT failed within {timeout}s: {upstream.masked} ({exc})") from exc
        except asyncio.CancelledError:
            await _close_writer(w)
            raise
        if not (200 <= code < 300):
            await _close_writer(w)
            raise ConnectionError(f"HTTP CONNECT failed: {status_text.strip()}")
        return r, w

    # ── relay ──────────────────────────────────────────────────────────────

    async def _relay(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
        up_r: asyncio.StreamReader,
        up_w: asyncio.StreamWriter,
        *,
        target: str = "",
    ) -> None:
        async def _pipe(src, dst, direction: str) -> None:
            try:
                while True:
                    data = await src.read(self.pipe_buf_size)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
                    if target:
                        nbytes = len(data)
                        with self._stats_lock:
                            stat = self._dest_entry(target)
                            if direction == "up":
                                stat.upload_bytes += nbytes
                            else:
                                stat.download_bytes += nbytes
            except (asyncio.CancelledError, ConnectionError, OSError):
                pass
            finally:
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except Exception:
                    pass

        t1 = asyncio.create_task(_pipe(client_r, up_w, "up"))
        t2 = asyncio.create_task(_pipe(up_r, client_w, "down"))
        await asyncio.gather(t1, t2, return_exceptions=True)
        for t in (t1, t2):
            if not t.done():
                t.cancel()


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8", errors="replace")).decode("ascii")
    return token


# ──────────────────── browser-facing helpers ──────────────────────────────────
#
# Browsers (Chromium / nodriver / Camoufox) cannot consume a SOCKS5 proxy with
# embedded credentials, and they also drop the socks5h remote-DNS semantics.
# These helpers bridge an arbitrary upstream proxy (and optional first-hop
# chain relay) to a local ``socks5h://127.0.0.1:<port>`` that any browser can
# use, restoring remote DNS.


def needs_bridge(proxy: Any, *, hop_proxy: Any = None) -> bool:
    """True when ``proxy`` must be bridged for browser consumption.

    Bridging is needed when the proxy carries credentials, is http(s), or is
    chained through a first-hop relay ``hop_proxy`` — none of which a browser
    can consume directly as a SOCKS proxy.
    """
    if not str(proxy or "").strip():
        return False
    entry = parse_proxy(proxy)
    if entry is None:
        return False
    if _resolve_hop_proxy(hop_proxy, allow_env=True) is not None:
        return True
    if entry.username:
        return True
    return entry.scheme in {"http", "https"}


def open_browser_proxy_bridge(
    proxy: Any,
    *,
    hop_proxy: Any = None,
    blocked_hosts: tuple[str, ...] | list[str] = (),
    connect_retries: int = 0,
    force_bridge: bool = False,
) -> tuple[str, Any, LocalProxyBridge | None]:
    """Return ``(browser_proxy_url, closer, bridge)`` for synchronous callers.

    When ``force_bridge`` is True (for example when browser data-saver host
    blocking or proxy-level traffic accounting is active), or when ``hop_proxy``
    is configured, even credential-free SOCKS5 proxies are routed through
    :class:`LocalProxyBridge`.
    """
    value = str(proxy or "").strip()
    if not value:
        return "", _noop, None
    entry = parse_proxy(value)
    if entry is None:
        return value, _noop, None
    resolved_hop = _resolve_hop_proxy(hop_proxy, allow_env=True)
    if not force_bridge and resolved_hop is None and not entry.username and entry.scheme in {"socks5", "socks5h"}:
        if entry.scheme == "socks5":
            return entry.url, _noop, None
        return value, _noop, None
    bridge = LocalProxyBridge(
        upstream=entry,
        hop_proxy=resolved_hop,
        blocked_hosts=_normalize_blocked_hosts(blocked_hosts),
        connect_retries=connect_retries,
    )
    bridge.start()
    return bridge.local_url, bridge.stop, bridge


def proxy_for_browser(
    proxy: Any,
    *,
    env_prefix: str = "PROXY",
    hop_proxy: Any = None,
    blocked_hosts: tuple[str, ...] | list[str] = (),
    connect_retries: int = 0,
    force_bridge: bool = False,
) -> tuple[str, Any]:
    """Return ``(browser_proxy_url, closer)`` for a synchronous caller.

    ``closer()`` stops any bridge that was started (no-op if none).  The caller
    must invoke ``closer()`` when the browser session is finished.
    """
    del env_prefix
    url, closer, _bridge = open_browser_proxy_bridge(
        proxy,
        hop_proxy=hop_proxy,
        blocked_hosts=blocked_hosts,
        connect_retries=connect_retries,
        force_bridge=force_bridge,
    )
    return url, closer


async def async_proxy_for_browser(
    proxy: Any,
    *,
    env_prefix: str = "PROXY",
    hop_proxy: Any = None,
) -> tuple[str, Any]:
    """Async variant of :func:`proxy_for_browser` for use inside a running loop.

    ``closer`` is an awaitable coroutine function that stops the bridge.
    """
    del env_prefix
    value = str(proxy or "").strip()
    if not value:
        return "", _noop_async
    entry = parse_proxy(value)
    if entry is None:
        return value, _noop_async
    resolved_hop = _resolve_hop_proxy(hop_proxy, allow_env=True)
    if resolved_hop is None and not entry.username and entry.scheme in {"socks5", "socks5h"}:
        if entry.scheme == "socks5":
            return entry.url, _noop_async
        return value, _noop_async

    bridge = LocalProxyBridge(upstream=entry, hop_proxy=resolved_hop)
    await bridge.async_start()
    return bridge.local_url, bridge.async_stop


def _noop() -> None:
    """No-op closer (no bridge was started)."""


async def _noop_async() -> None:
    """No-op async closer (no bridge was started)."""
