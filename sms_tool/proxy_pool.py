"""Async SOCKS5 proxy pool server with upstream rotation and health checks.

Zero external dependencies -- pure asyncio + struct + socket.
Supports socks5h (remote DNS) by passing hostnames un-resolved to upstreams.

Usage:
    from sms_tool.proxy_pool import Socks5Server, UpstreamProxy
    server = Socks5Server("127.0.0.1", 18080, upstreams)
    asyncio.run(server.run())
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy_health import ProxyHealthTracker

logger = logging.getLogger("proxy_pool")

# ──────────────────── SOCKS5 protocol constants ────────────────────

_SOCKS5_VER = 0x05
_NO_AUTH = 0x00
_USERPASS = 0x02
_NO_ACCEPTABLE = 0xFF

_CMD_CONNECT = 0x01
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04

_REP_SUCCEEDED = 0x00
_REP_GENERAL_FAILURE = 0x01
_REP_CONN_REFUSED = 0x05
_REP_CMD_NOT_SUPPORTED = 0x07
_REP_ADDR_NOT_SUPPORTED = 0x08

# ──────────────────── Data classes ────────────────────


@dataclass
class UpstreamProxy:
    host: str
    port: int
    username: str = ""
    password: str = ""
    label: str = ""
    priority: int = 0  # lower = higher priority; highest-priority tier selected first
    healthy: bool = True
    last_check: float = 0.0
    fail_count: int = 0
    success_count: int = 0
    total_connections: int = 0

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def proxy_url(self) -> str:
        """Reconstruct a ``scheme://[user:pass@]host:port`` URL for this upstream.

        P1-2: this is the string fed into ``ProxyHealthTracker`` so the SOCKS5
        pool's health (which lives only in this process's memory) becomes visible to
        the registration/remail paths that consume the same egress proxies via the
        shared on-disk tracker. ``ProxyHealthTracker.key()`` derives the same
        ``host:port#sid-hash`` it would from a raw proxy URL.
        """
        auth = ""
        if self.username:
            pwd = f":{self.password}" if self.password else ""
            auth = f"{self.username}{pwd}@"
        return f"socks5://{auth}{self.host}:{self.port}"

    @classmethod
    def from_url(cls, url: str, label: str = "", priority: int = 0) -> UpstreamProxy:
        """Parse a proxy URL into UpstreamProxy.

        Delegates to the single-authority ``proxy_entry`` parser: standard
        ``scheme://user:pass@host:port``, the Kookeey / ippeak non-standard
        4-segment ``host:port:user:pass`` format, IPv6 literals and no-auth
        forms. Malformed input raises ``ValueError`` -- a garbage URL used to
        fall through to a permissive ``urlparse`` fallback and become a live
        ``127.0.0.1:1080`` upstream, which is exactly the failure a pool
        server must refuse loudly instead of serving.
        """
        from .proxy_entry import parse_proxy

        entry = parse_proxy(url, default_scheme="socks5")
        if entry is None:
            raise ValueError(f"unparseable proxy url: {url!r}")
        if not label:
            # Keep the legacy label convention: URL form -> raw URL;
            # bare 4-segment form -> user@host:port.
            label = url if "://" in url else f"{entry.username}@{entry.host}:{entry.port}"
        return cls(
            host=entry.host,
            port=entry.port,
            username=entry.username,
            password=entry.password,
            label=label,
            priority=priority,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "addr": self.addr,
            "priority": self.priority,
            "healthy": self.healthy,
            "fail_count": self.fail_count,
            "success_count": self.success_count,
            "total_connections": self.total_connections,
            "last_check": (
                datetime.fromtimestamp(self.last_check, tz=timezone.utc).isoformat()
                if self.last_check > 0
                else None
            ),
        }


@dataclass
class PoolStats:
    total_connections: int = 0
    active_connections: int = 0
    total_errors: int = 0
    uptime_start: float = field(default_factory=time.time)


# ──────────────────── SOCKS5 helpers ────────────────────


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    data = await reader.readexactly(n)
    if len(data) != n:
        raise ConnectionError("incomplete read")
    return data


async def _read_socks5_addr(
    reader: asyncio.StreamReader, addr_type: int
) -> tuple[str, int]:
    if addr_type == _ATYP_IPV4:
        raw = await _read_exact(reader, 4)
        host = socket.inet_ntoa(raw)
    elif addr_type == _ATYP_DOMAIN:
        length = (await _read_exact(reader, 1))[0]
        host = (await _read_exact(reader, length)).decode("ascii", errors="replace")
    elif addr_type == _ATYP_IPV6:
        raw = await _read_exact(reader, 16)
        host = socket.inet_ntop(socket.AF_INET6, raw)
    else:
        raise ValueError(f"unsupported ATYP: {addr_type:#x}")
    port_raw = await _read_exact(reader, 2)
    port = struct.unpack("!H", port_raw)[0]
    return host, port


def _encode_socks5_addr(host: str, port: int) -> bytes:
    """Encode host:port into SOCKS5 address bytes."""
    buf = bytearray()
    try:
        raw = socket.inet_aton(host)
        buf.append(_ATYP_IPV4)
        buf.extend(raw)
    except OSError:
        try:
            raw = socket.inet_pton(socket.AF_INET6, host)
            buf.append(_ATYP_IPV6)
            buf.extend(raw)
        except OSError:
            domain = host.encode("ascii")
            buf.append(_ATYP_DOMAIN)
            buf.append(len(domain))
            buf.extend(domain)
    buf.extend(struct.pack("!H", port))
    return bytes(buf)


def _build_socks5_reply(reply: int, bind_host: str = "0.0.0.0", bind_port: int = 0) -> bytes:
    return bytes([_SOCKS5_VER, reply, 0x00]) + _encode_socks5_addr(bind_host, bind_port)


# ──────────────────── Server ────────────────────


class Socks5Server:
    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 18080,
        upstreams: list[UpstreamProxy] | None = None,
        stats_port: int = 18081,
        health_check_interval: float = 30.0,
        health_check_timeout: float = 5.0,
        connect_timeout: float = 10.0,
        max_retries: int = 2,
        pipe_buf_size: int = 65536,
        health_check_target_host: str = "cloudflare.com",
        health_check_target_port: int = 443,
        health_check_fail_threshold: int = 3,
        health_tracker: ProxyHealthTracker | None = None,
    ) -> None:
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._upstreams: list[UpstreamProxy] = upstreams or []
        self._stats_port = stats_port
        self._health_check_interval = health_check_interval
        self._health_check_timeout = health_check_timeout
        # P0-3: the health probe used to hit connectivity-check.gstatic.com, which
        # has nothing to do with OpenAI/Cloudflare reachability. ChatGPT sits behind
        # Cloudflare, so we probe cloudflare.com instead -- a successful CONNECT
        # there is a far better signal that the proxy can actually serve traffic.
        self._health_check_target_host = health_check_target_host
        self._health_check_target_port = health_check_target_port
        # P0-3: death only after this many *consecutive* failures; recovery needs
        # the same count of consecutive successes (see _apply_health hysteresis).
        # A single blip no longer kills an upstream, and one lucky success no
        # longer whitelists a bad one.
        self._health_check_fail_threshold = max(1, int(health_check_fail_threshold))
        # P1-2: optional shared health tracker (ProxyHealthTracker). When set, every
        # health event funnelled through _apply_health is mirrored into the tracker's
        # on-disk state so the registration/remail paths see the SOCKS5 pool's
        # outcomes. Left None to keep the pool purely in-memory (default/tests).
        self._health_tracker: ProxyHealthTracker | None = health_tracker
        self._connect_timeout = connect_timeout
        self._max_retries = max_retries
        self._pipe_buf_size = pipe_buf_size
        self._rr_idx = 0
        self._stats = PoolStats()
        self._server: asyncio.Server | None = None
        self._stats_server: asyncio.Server | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._active_tasks: set[asyncio.Task[None]] = set()

    # ── lifecycle ──

    async def run(self) -> None:
        await self.start()
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, self._listen_host, self._listen_port
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("SOCKS5 proxy pool listening on %s", addrs)

        if self._stats_port > 0:
            self._stats_server = await asyncio.start_server(
                self._on_stats, self._listen_host, self._stats_port
            )
            logger.info("Stats endpoint on %s:%d", self._listen_host, self._stats_port)

        if self._upstreams:
            self._health_task = asyncio.create_task(self._health_check_loop())
        else:
            logger.warning("No upstream proxies configured -- server will reject all connections")

    async def stop(self) -> None:
        logger.info("Shutting down proxy pool...")
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._stats_server:
            self._stats_server.close()
            await self._stats_server.wait_closed()
        # wait for active connections (max 5s)
        if self._active_tasks:
            logger.info("Waiting for %d active connections...", len(self._active_tasks))
            done, _ = await asyncio.wait(self._active_tasks, timeout=5.0)
            for t in self._active_tasks - done:
                t.cancel()
        logger.info("Proxy pool stopped")

    def request_shutdown(self) -> None:
        self._shutdown.set()

    # ── client handler ──

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task:
            self._active_tasks.add(task)
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._stats.total_errors += 1
            logger.debug("connection error: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if task:
                self._active_tasks.discard(task)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Phase 1: greeting
        hdr = await asyncio.wait_for(_read_exact(reader, 2), timeout=10)
        ver, nmethods = hdr[0], hdr[1]
        if ver != _SOCKS5_VER:
            return
        await _read_exact(reader, nmethods)  # consume method list
        writer.write(bytes([_SOCKS5_VER, _NO_AUTH]))
        await writer.drain()

        # Phase 2: CONNECT request
        req_hdr = await asyncio.wait_for(_read_exact(reader, 4), timeout=10)
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

        # Phase 3: pick upstream and connect (with retry across upstreams)
        tried: set[str] = set()
        up_r = up_w = None
        upstream = None
        max_attempts = len(self._upstreams) + 1
        for _attempt in range(max_attempts):
            upstream = self._pick_upstream(exclude=tried)
            if upstream is None:
                break
            try:
                up_r, up_w = await asyncio.wait_for(
                    self._connect_through_upstream(upstream, dest_host, dest_port, atyp),
                    timeout=self._connect_timeout,
                )
                break  # success
            except Exception as exc:
                # P0-3: route live-failures through the same hysteresis as the
                # health probe, so a single blip in real traffic does not instantly
                # kill an upstream (and recovery must still earn consecutive success).
                # P1-2: the error string is mirrored into the shared tracker too.
                self._apply_health(upstream, False, error=str(exc)[:120])
                upstream.total_connections += 1
                self._stats.total_errors += 1
                tried.add(upstream.addr)
                logger.warning(
                    "upstream %s connect failed for %s:%d (attempt %d/%d): %s",
                    upstream.label, dest_host, dest_port, _attempt + 1, max_attempts, exc,
                )
                upstream = None
                continue

        if upstream is None or up_r is None:
            writer.write(_build_socks5_reply(_REP_GENERAL_FAILURE))
            await writer.drain()
            return

        upstream.success_count += 1
        upstream.total_connections += 1
        self._stats.total_connections += 1
        self._stats.active_connections += 1

        # Phase 4: success reply and relay
        writer.write(_build_socks5_reply(_REP_SUCCEEDED))
        await writer.drain()

        logger.debug(
            "relay %s:%d via %s [%s]",
            dest_host, dest_port, upstream.label, upstream.addr,
        )
        try:
            await self._relay(reader, writer, up_r, up_w)
        finally:
            self._stats.active_connections -= 1
            try:
                up_w.close()
                await up_w.wait_closed()
            except Exception:
                pass

    # ── upstream selection ──

    def _apply_health(self, upstream: UpstreamProxy, success: bool, error: str = "") -> None:
        """Update an upstream's health with hysteresis (P0-3).

        Death: an upstream goes unhealthy only after ``fail_threshold`` *consecutive*
        failures. Recovery: once unhealthy, it must reach ``fail_count == 0`` (i.e.
        several consecutive successes) before it is trusted again -- a single lucky
        success does not whitelist a bad proxy, and a single blip does not kill a
        good one.

        P1-2: when a shared ``health_tracker`` is configured, the same event is
        mirrored into it so the registration/remail paths observe this proxy's health.
        The SOCKS5 pool's own selection logic still uses in-memory state only.
        """
        if success:
            upstream.fail_count = max(0, upstream.fail_count - 1)
        else:
            upstream.fail_count = min(self._health_check_fail_threshold, upstream.fail_count + 1)
        if upstream.healthy:
            upstream.healthy = upstream.fail_count < self._health_check_fail_threshold
        else:
            upstream.healthy = upstream.fail_count == 0
        upstream.last_check = time.time()

        if self._health_tracker is not None:
            self._health_tracker.record(upstream.proxy_url, ok=success, error=error)

    def _pick_upstream(self, exclude: set[str] | None = None) -> UpstreamProxy | None:
        if not self._upstreams:
            return None
        exclude = exclude or set()
        healthy = [u for u in self._upstreams if u.healthy and u.addr not in exclude]
        if healthy:
            # priority-based selection: pick from the lowest priority number tier,
            # round-robin within that tier.
            min_pri = min(u.priority for u in healthy)
            tier = [u for u in healthy if u.priority == min_pri]
            idx = self._rr_idx % len(tier)
            self._rr_idx += 1
            return tier[idx]
        # Half-open: nothing healthy. Instead of fail-open-resurrecting the entire
        # pool (which would spray every dead upstream with live traffic at once),
        # probe a single candidate -- the least-bad, rotating among ties -- so a
        # recovery is discovered incrementally rather than by mass-resurrecting.
        candidates = sorted(
            (u for u in self._upstreams if u.addr not in exclude),
            key=lambda u: (u.fail_count, u.priority),
        )
        if not candidates:
            return None
        candidate = candidates[self._rr_idx % len(candidates)]
        self._rr_idx += 1
        return candidate

    # ── upstream SOCKS5 handshake ──

    async def _connect_through_upstream(
        self,
        upstream: UpstreamProxy,
        dest_host: str,
        dest_port: int,
        dest_atyp: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        r, w = await asyncio.open_connection(upstream.host, upstream.port)

        try:
            # greeting to upstream
            if upstream.username and upstream.password:
                w.write(bytes([_SOCKS5_VER, 0x02, _NO_AUTH, _USERPASS]))
            else:
                w.write(bytes([_SOCKS5_VER, 0x01, _NO_AUTH]))
            await w.drain()

            resp = await asyncio.wait_for(_read_exact(r, 2), timeout=self._connect_timeout)
            method = resp[1]

            if method == _USERPASS:
                # RFC 1929 username/password auth
                user = upstream.username.encode()
                pwd = upstream.password.encode()
                auth = bytearray([0x01, len(user)])
                auth.extend(user)
                auth.append(len(pwd))
                auth.extend(pwd)
                w.write(bytes(auth))
                await w.drain()
                auth_resp = await asyncio.wait_for(
                    _read_exact(r, 2), timeout=self._connect_timeout
                )
                if auth_resp[1] != 0x00:
                    raise ConnectionError("upstream auth failed")
            elif method == _NO_ACCEPTABLE:
                raise ConnectionError("upstream: no acceptable auth method")

            # CONNECT request to upstream -- preserve original ATYP for remote DNS
            connect_req = bytearray([_SOCKS5_VER, _CMD_CONNECT, 0x00])
            connect_req.append(dest_atyp)
            if dest_atyp == _ATYP_DOMAIN:
                domain = dest_host.encode("ascii")
                connect_req.append(len(domain))
                connect_req.extend(domain)
            elif dest_atyp == _ATYP_IPV4:
                connect_req.extend(socket.inet_aton(dest_host))
            elif dest_atyp == _ATYP_IPV6:
                connect_req.extend(socket.inet_pton(socket.AF_INET6, dest_host))
            connect_req.extend(struct.pack("!H", dest_port))
            w.write(bytes(connect_req))
            await w.drain()

            reply = await asyncio.wait_for(_read_exact(r, 4), timeout=self._connect_timeout)
            if reply[1] != _REP_SUCCEEDED:
                raise ConnectionError(f"upstream CONNECT reply: {reply[1]:#x}")
            # consume bind address
            bind_atyp = reply[3]
            if bind_atyp == _ATYP_IPV4:
                await _read_exact(r, 4)
            elif bind_atyp == _ATYP_DOMAIN:
                ln = (await _read_exact(r, 1))[0]
                await _read_exact(r, ln)
            elif bind_atyp == _ATYP_IPV6:
                await _read_exact(r, 16)
            await _read_exact(r, 2)  # bind port

        except Exception:
            w.close()
            await w.wait_closed()
            raise

        return r, w

    # ── bidirectional relay ──

    async def _relay(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
        up_r: asyncio.StreamReader,
        up_w: asyncio.StreamWriter,
    ) -> None:
        async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(self._pipe_buf_size)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (asyncio.CancelledError, ConnectionError, OSError):
                pass
            finally:
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except Exception:
                    pass

        t1 = asyncio.create_task(_pipe(client_r, up_w))
        t2 = asyncio.create_task(_pipe(up_r, client_w))
        await asyncio.gather(t1, t2, return_exceptions=True)
        for t in (t1, t2):
            if not t.done():
                t.cancel()

    # ── health check ──

    async def _health_check_loop(self) -> None:
        # P0-3: probe the configured target (cloudflare.com:443 by default) instead
        # of connectivity-check.gstatic.com -- a green check now means the proxy can
        # actually reach ChatGPT's edge (Cloudflare sits in front of it), so one probe
        # serves both connectivity and edge-reachability. Each result is fed through
        # self._apply_health, which enforces the consecutive-failure threshold and the
        # hysteresis recovery -- no single blip kills an upstream, and no single lucky
        # success whitelists a dead one.
        test_host = self._health_check_target_host
        test_port = self._health_check_target_port
        while True:
            try:
                await asyncio.sleep(self._health_check_interval)
                for upstream in self._upstreams:
                    was_healthy = upstream.healthy
                    success = False
                    probe_error = ""
                    try:
                        r, w = await asyncio.wait_for(
                            asyncio.open_connection(upstream.host, upstream.port),
                            timeout=self._health_check_timeout,
                        )
                        try:
                            # minimal SOCKS5 handshake
                            w.write(bytes([_SOCKS5_VER, 0x01, _NO_AUTH]))
                            await w.drain()
                            resp = await asyncio.wait_for(
                                _read_exact(r, 2), timeout=self._health_check_timeout
                            )
                            if resp[1] == _NO_AUTH:
                                domain = test_host.encode("ascii")
                                req = bytearray([_SOCKS5_VER, _CMD_CONNECT, 0x00, _ATYP_DOMAIN])
                                req.append(len(domain))
                                req.extend(domain)
                                req.extend(struct.pack("!H", test_port))
                                w.write(bytes(req))
                                await w.drain()
                                reply = await asyncio.wait_for(
                                    _read_exact(r, 4), timeout=self._health_check_timeout
                                )
                                success = reply[1] == _REP_SUCCEEDED
                        finally:
                            w.close()
                            await w.wait_closed()
                    except Exception as _probe_exc:
                        success = False
                        probe_error = str(_probe_exc)[:120]

                    # P1-2: a shared tracker (if configured) sees the same event.
                    self._apply_health(upstream, success, error=probe_error if not success else "")

                    if upstream.healthy != was_healthy:
                        level = logging.INFO if upstream.healthy else logging.WARNING
                        logger.log(
                            level,
                            "upstream %s [%s]: %s → %s (fail_count=%d)",
                            upstream.label,
                            upstream.addr,
                            "healthy" if was_healthy else "unhealthy",
                            "healthy" if upstream.healthy else "unhealthy",
                            upstream.fail_count,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("health check error: %s", exc)

    # ── stats HTTP endpoint ──

    async def _on_stats(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # read HTTP request line + headers (minimal parsing)
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            # consume remaining headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break

            req_str = request_line.decode("ascii", errors="replace")
            path = req_str.split(" ")[1] if len(req_str.split(" ")) > 1 else "/"

            if path == "/health":
                any_healthy = any(u.healthy for u in self._upstreams)
                status = 200 if any_healthy else 503
                body = json.dumps({"healthy": any_healthy}).encode()
            else:
                status = 200
                body = json.dumps(self._stats_json(), indent=2).encode()

            header = (
                f"HTTP/1.1 {status} {'OK' if status == 200 else 'Service Unavailable'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
            writer.write(header + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    def _stats_json(self) -> dict[str, Any]:
        return {
            "status": "running",
            "uptime_seconds": round(time.time() - self._stats.uptime_start, 1),
            "active_connections": self._stats.active_connections,
            "total_connections": self._stats.total_connections,
            "total_errors": self._stats.total_errors,
            "upstreams": [u.to_dict() for u in self._upstreams],
        }
