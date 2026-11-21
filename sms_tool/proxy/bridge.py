"""In-process local proxy bridge: one upstream proxy -> local SOCKS5 endpoint.

Given a single upstream proxy (http/https/socks5, optionally authenticated),
:class:`LocalProxyBridge` starts a local ``socks5h://127.0.0.1:<port>`` endpoint
that browser sessions (nodriver / Playwright / manual config) can point to
without caring about the upstream format or credentials.

It reuses the SOCKS5 protocol machinery in ``proxy_pool`` for socks-family
upstreams, and implements an HTTP CONNECT tunnel for http/https upstreams so a
single unified bridge covers all common cases.

Usage::

    from sms_tool.proxy.entry import parse_proxy
    from sms_tool.proxy.bridge import LocalProxyBridge

    with LocalProxyBridge(upstream=parse_proxy("host:port:user:pass")) as bridge:
        browser_url = bridge.local_url   # "socks5h://127.0.0.1:12345"
        ...
    # bridge auto-stopped on context exit
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Any

from .entry import ProxyEntry, _SOCKS_DEFAULT_PORT, _normalize_scheme
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
    Socks5Server,
    UpstreamProxy,
    _build_socks5_reply,
    _read_exact,
    _read_socks5_addr,
)

logger = logging.getLogger("proxy_bridge")

_HTTP_SCHEMES = {"http", "https"}
#: RFC 1928 reply code: connection not allowed by ruleset.
_REP_NOT_ALLOWED = 0x02


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

    def __post_init__(self) -> None:
        self.blocked_hosts = _normalize_blocked_hosts(self.blocked_hosts)
        self.connect_retries = max(0, min(int(self.connect_retries or 0), 3))

    def _dest_entry(self, target: str) -> _DestinationStats:
        entry = self._dest_stats.get(target)
        if entry is None:
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
            "proxy_bridge listening on %s:%s -> upstream %s (async)",
            self.listen_host,
            self._port,
            self.upstream.masked,
        )
        return self._port

    async def async_stop(self) -> None:
        """Stop the local server (keeps the caller's loop intact)."""
        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("proxy_bridge async_stop error: %s", exc)
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
        upstream = self.upstream

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
                    "proxy_bridge listening on %s:%s -> upstream %s (sync)",
                    self.listen_host,
                    self._port,
                    upstream.masked,
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
        return f"socks5h://{self.listen_host}:{self._port}"

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
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("proxy_bridge client error: %s", exc)
        finally:
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
        await _read_exact(reader, nmethods)
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
            dest_host, dest_port = await _read_socks5_addr(reader, atyp)
        except (ValueError, asyncio.IncompleteReadError):
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

    async def _connect_upstream(
        self, dest_host: str, dest_port: int, dest_atyp: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        upstream = self.upstream
        if upstream is None:
            raise RuntimeError("no upstream")
        scheme = _normalize_scheme(upstream.scheme) or "http"
        if scheme in _HTTP_SCHEMES:
            return await self._connect_http_upstream(upstream, dest_host, dest_port)
        # socks-family: reuse proxy_pool's authenticated SOCKS5 handshake.
        # _connect_through_upstream is a Socks5Server instance method that only
        # depends on ``connect_timeout``, so a light connector instance is enough.
        fake = UpstreamProxy(
            host=upstream.host,
            port=upstream.port,
            username=upstream.username,
            password=upstream.password,
            label=upstream.masked,
        )
        connector = Socks5Server(connect_timeout=self.connect_timeout)
        return await connector._connect_through_upstream(fake, dest_host, dest_port, dest_atyp)

    async def _connect_http_upstream(
        self, upstream: ProxyEntry, dest_host: str, dest_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """HTTP CONNECT tunnel to the destination through an http(s) upstream.

        P1-4: every await below is bounded by ``connect_timeout``.  The socks
        branch always delegated to ``Socks5Server``, which honours it; this one
        used raw ``open_connection`` / ``readline``, so an upstream that
        accepted the TCP socket but never answered the CONNECT parked this
        coroutine -- and the client handler awaiting it -- forever, leaking one
        task and one socket per stuck request until the process was restarted.
        """
        port = upstream.port or _SOCKS_DEFAULT_PORT
        timeout = self.connect_timeout
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(upstream.host, port), timeout=timeout)
        except (asyncio.TimeoutError, OSError) as exc:
            raise ConnectionError(f"HTTP upstream connect failed within {timeout}s: {upstream.masked}") from exc
        try:
            authority = f"{dest_host}:{dest_port}"
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
    import base64

    token = base64.b64encode(f"{username}:{password}".encode("utf-8", errors="replace")).decode("ascii")
    return token


# ──────────────────── browser-facing helpers ──────────────────────────────────
#
# Browsers (Chromium / nodriver / Camoufox) cannot consume a SOCKS5 proxy with
# embedded credentials, and they also drop the socks5h remote-DNS semantics.
# These helpers bridge an arbitrary upstream proxy to a local
# ``socks5h://127.0.0.1:<port>`` that any browser can use, restoring remote DNS.


def needs_bridge(proxy: Any) -> bool:
    """True when ``proxy`` must be bridged for browser consumption.

    Bridging is needed when the proxy is absent (no), carries credentials, or
    is http(s) — all of which a browser cannot consume directly as a SOCKS proxy.
    """
    from .entry import parse_proxy

    if not str(proxy or "").strip():
        return False
    entry = parse_proxy(proxy)
    if entry is None:
        return False
    if entry.username:
        return True
    return entry.scheme in {"http", "https"}


def open_browser_proxy_bridge(
    proxy: Any,
    *,
    blocked_hosts: tuple[str, ...] | list[str] = (),
    connect_retries: int = 0,
    force_bridge: bool = False,
) -> tuple[str, Any, LocalProxyBridge | None]:
    """Return ``(browser_proxy_url, closer, bridge)`` for synchronous callers.

    When ``force_bridge`` is True (for example when browser data-saver host
    blocking or proxy-level traffic accounting is active), even credential-free
    SOCKS5 proxies are routed through :class:`LocalProxyBridge`.
    """
    from .entry import parse_proxy

    value = str(proxy or "").strip()
    if not value:
        return "", _noop, None
    entry = parse_proxy(value)
    if entry is None:
        return value, _noop, None
    if not force_bridge and not entry.username and entry.scheme in {"socks5", "socks5h"}:
        if entry.scheme == "socks5":
            return entry.url, _noop, None
        return value, _noop, None
    bridge = LocalProxyBridge(
        upstream=entry,
        blocked_hosts=_normalize_blocked_hosts(blocked_hosts),
        connect_retries=connect_retries,
    )
    bridge.start()
    return bridge.local_url, bridge.stop, bridge


def proxy_for_browser(
    proxy: Any,
    *,
    env_prefix: str = "PROXY",
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
        blocked_hosts=blocked_hosts,
        connect_retries=connect_retries,
        force_bridge=force_bridge,
    )
    return url, closer


async def async_proxy_for_browser(proxy: Any, *, env_prefix: str = "PROXY") -> tuple[str, Any]:
    """Async variant of :func:`proxy_for_browser` for use inside a running loop.

    ``closer`` is an awaitable coroutine function that stops the bridge.
    """
    from .entry import parse_proxy

    value = str(proxy or "").strip()
    if not value:
        return "", _noop_async
    entry = parse_proxy(value)
    if entry is None or not entry.username and entry.scheme in {"socks5", "socks5h"}:
        if entry is not None and entry.scheme == "socks5":
            return entry.url, _noop_async
        return value, _noop_async

    bridge = LocalProxyBridge(upstream=entry)
    await bridge.async_start()
    return bridge.local_url, bridge.async_stop


def _noop() -> None:
    """No-op closer (no bridge was started)."""


async def _noop_async() -> None:
    """No-op async closer (no bridge was started)."""
