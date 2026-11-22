"""Unit tests for sms_tool.proxy.bridge (local SOCKS5 bridge lifecycle)."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import struct
import time
import unittest
from unittest.mock import patch

from sms_tool.proxy.bridge import (
    LocalProxyBridge,
    async_proxy_for_browser,
    needs_bridge,
    open_browser_proxy_bridge,
    proxy_for_browser,
)
from sms_tool.proxy.entry import ProxyEntry, parse_proxy


class TestLocalProxyBridgeLifecycle(unittest.TestCase):
    def setUp(self):
        upstream = parse_proxy("socks5://user:pass@127.0.0.1:9999")
        assert upstream is not None, "the fixture proxy must parse"
        self.upstream = upstream

    def test_start_binds_port_and_local_url(self):
        bridge = LocalProxyBridge(upstream=self.upstream)
        try:
            port = bridge.start()
            self.assertGreater(port, 0)
            self.assertEqual(bridge.local_url, f"socks5h://127.0.0.1:{port}")
        finally:
            bridge.stop()

    def test_context_manager_starts_and_stops(self):
        with LocalProxyBridge(upstream=self.upstream) as bridge:
            self.assertGreater(bridge.start(), 0)  # idempotent: already started
        # after exit, no port
        self.assertEqual(bridge._port, 0)

    def test_local_url_requires_start(self):
        bridge = LocalProxyBridge(upstream=self.upstream)
        with self.assertRaises(RuntimeError):
            _ = bridge.local_url

    def test_no_upstream_raises_on_start(self):
        bridge = LocalProxyBridge(upstream=None)
        with self.assertRaises(RuntimeError):
            bridge.start()

    @patch("sms_tool.proxy.bridge.LocalProxyBridge._connect_upstream")
    def test_start_stop_does_not_connect_upstream(self, mock_connect):
        bridge = LocalProxyBridge(upstream=self.upstream)
        try:
            bridge.start()
            mock_connect.assert_not_called()
        finally:
            bridge.stop()


class TestNeedsBridge(unittest.TestCase):
    def test_no_proxy(self):
        self.assertFalse(needs_bridge(""))
        self.assertFalse(needs_bridge(None))

    def test_credentials_need_bridge(self):
        self.assertTrue(needs_bridge("socks5://user:pass@host:1080"))
        self.assertTrue(needs_bridge("host:1080:user:pass"))

    def test_http_needs_bridge(self):
        self.assertTrue(needs_bridge("http://host:8080"))

    def test_plain_socks_does_not(self):
        self.assertFalse(needs_bridge("socks5://host:1080"))
        self.assertFalse(needs_bridge("socks5h://host:1080"))


class TestProxyForBrowser(unittest.TestCase):
    def test_empty_returns_noop(self):
        url, closer = proxy_for_browser("")
        self.assertEqual(url, "")
        closer()  # no-op

    def test_plain_socks_passthrough(self):
        url, closer = proxy_for_browser("socks5://host:1080")
        self.assertEqual(url, "socks5://host:1080")
        closer()

    def test_credential_socks_bridges_to_local(self):
        url, closer = proxy_for_browser("socks5://user:pass@host:1080")
        try:
            self.assertTrue(url.startswith("socks5h://127.0.0.1:"))
            port = int(url.rsplit(":", 1)[1])
            self.assertGreater(port, 0)
        finally:
            closer()

    def test_http_bridges_to_local(self):
        url, closer = proxy_for_browser("http://user:pass@host:8080")
        try:
            self.assertTrue(url.startswith("socks5h://127.0.0.1:"))
        finally:
            closer()


# Long enough to blow any sane assertion budget, short enough that a regression
# fails in seconds instead of hanging the suite.
_HANG_SECONDS = 5.0


class _FakeWriter:
    """Minimal asyncio.StreamWriter stand-in."""

    def __init__(self):
        self.written = b""
        self.closed = False
        self.wait_closed_called = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


class _ScriptedReader:
    """StreamReader stand-in returning canned lines, then EOF."""

    def __init__(self, *lines: bytes):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _HangingReader:
    """Accepts the socket but never answers the CONNECT — the P1-4 hang.

    Deliberately finite (not ``sleep(3600)``): if the timeout is ever removed
    again this has to surface as a slow, failing test, not a hung suite.
    """

    async def readline(self) -> bytes:
        await asyncio.sleep(_HANG_SECONDS)
        return b""


class TestHttpUpstreamConnectTimeout(unittest.TestCase):
    """P1-4: the HTTP CONNECT branch must be bounded like the SOCKS branch."""

    def setUp(self):
        upstream = parse_proxy("http://user:pass@upstream.example:8080")
        assert upstream is not None, "the fixture proxy must parse"
        self.upstream = upstream
        self.bridge = LocalProxyBridge(upstream=self.upstream, connect_timeout=0.05)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_tcp_connect_is_bounded(self):
        async def _never_connects(*args, **kwargs):
            await asyncio.sleep(_HANG_SECONDS)
            return _ScriptedReader(), _FakeWriter()

        async def _scenario():
            with patch("asyncio.open_connection", new=_never_connects):
                started = time.monotonic()
                with self.assertRaises(ConnectionError) as ctx:
                    await self.bridge._connect_http_upstream(self.upstream, "chat.example", 443)
                return str(ctx.exception), time.monotonic() - started

        message, elapsed = self._run(_scenario())
        self.assertIn("connect failed", message)
        self.assertLess(elapsed, 2.0, f"connect was not bounded: {elapsed:.1f}s")

    def test_unanswered_connect_response_is_bounded_and_closes_the_socket(self):
        writer = _FakeWriter()

        async def _accepts_but_stays_silent(*args, **kwargs):
            return _HangingReader(), writer

        async def _scenario():
            with patch("asyncio.open_connection", new=_accepts_but_stays_silent):
                started = time.monotonic()
                with self.assertRaises(ConnectionError):
                    await self.bridge._connect_http_upstream(self.upstream, "chat.example", 443)
                return time.monotonic() - started

        elapsed = self._run(_scenario())
        self.assertLess(elapsed, 2.0, f"CONNECT response was not bounded: {elapsed:.1f}s")
        self.assertTrue(writer.closed, "a timed-out CONNECT must not leak the socket")

    def test_successful_connect_returns_the_streams(self):
        reader = _ScriptedReader(b"HTTP/1.1 200 Connection established\r\n", b"\r\n")
        writer = _FakeWriter()

        async def _ok(*args, **kwargs):
            return reader, writer

        async def _scenario():
            with patch("asyncio.open_connection", new=_ok):
                return await self.bridge._connect_http_upstream(self.upstream, "chat.example", 443)

        got_r, got_w = self._run(_scenario())
        self.assertIs(got_r, reader)
        self.assertIs(got_w, writer)
        self.assertFalse(writer.closed)
        self.assertIn(b"CONNECT chat.example:443 HTTP/1.1", writer.written)
        self.assertIn(b"Proxy-Authorization: Basic ", writer.written)

    def test_non_2xx_closes_the_socket(self):
        reader = _ScriptedReader(b"HTTP/1.1 403 Forbidden\r\n", b"\r\n")
        writer = _FakeWriter()

        async def _forbidden(*args, **kwargs):
            return reader, writer

        async def _scenario():
            with patch("asyncio.open_connection", new=_forbidden):
                with self.assertRaises(ConnectionError) as ctx:
                    await self.bridge._connect_http_upstream(self.upstream, "chat.example", 443)
            return str(ctx.exception)

        message = self._run(_scenario())
        self.assertIn("403", message)
        self.assertTrue(writer.closed)


class TestAsyncProxyForBrowser(unittest.TestCase):
    def test_plain_socks_passthrough_async(self):
        async def _run():
            url, closer = await async_proxy_for_browser("socks5h://host:1080")
            await closer()
            return url

        url = asyncio.run(_run())
        self.assertEqual(url, "socks5h://host:1080")

    def test_credential_socks_bridges_async(self):
        async def _run():
            url, closer = await async_proxy_for_browser("socks5://user:pass@host:1080")
            try:
                return url
            finally:
                await closer()

        url = asyncio.run(_run())
        self.assertTrue(url.startswith("socks5h://127.0.0.1:"))


async def _pipe_pair(
    r1: asyncio.StreamReader, w1: asyncio.StreamWriter, r2: asyncio.StreamReader, w2: asyncio.StreamWriter
) -> None:
    async def _copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(4096)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            try:
                if dst.can_write_eof():
                    dst.write_eof()
            except OSError:
                pass

    await asyncio.gather(_copy(r1, w2), _copy(r2, w1), return_exceptions=True)


async def _start_echo_server() -> tuple[asyncio.Server, int]:
    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            data = await r.read(1024)
            if data:
                w.write(b"ECHO:" + data)
                await w.drain()
        finally:
            w.close()
            await w.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def _start_mock_socks5_proxy(
    *,
    expected_auth: tuple[str, str] | None = None,
    reject_connect: bool = False,
) -> tuple[asyncio.Server, int, list[tuple[str, int]]]:
    seen: list[tuple[str, int]] = []

    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            hdr = await r.readexactly(2)
            methods = await r.readexactly(hdr[1])
            if expected_auth is not None:
                if 0x02 not in methods:
                    w.write(b"\x05\xff")
                    await w.drain()
                    return
                w.write(b"\x05\x02")
                await w.drain()
                ver_u = await r.readexactly(2)
                user = (await r.readexactly(ver_u[1])).decode("utf-8")
                plen = (await r.readexactly(1))[0]
                pwd = (await r.readexactly(plen)).decode("utf-8")
                if (user, pwd) != expected_auth:
                    w.write(b"\x01\x01")
                    await w.drain()
                    return
                w.write(b"\x01\x00")
                await w.drain()
            else:
                w.write(b"\x05\x00")
                await w.drain()

            req = await r.readexactly(4)
            atyp = req[3]
            if atyp == 1:
                host = socket.inet_ntoa(await r.readexactly(4))
            elif atyp == 3:
                ln = (await r.readexactly(1))[0]
                host = (await r.readexactly(ln)).decode("ascii")
            else:
                host = socket.inet_ntop(socket.AF_INET6, await r.readexactly(16))
            port = struct.unpack("!H", await r.readexactly(2))[0]
            seen.append((host, port))
            if reject_connect:
                w.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await w.drain()
                return
            up_r, up_w = await asyncio.open_connection(host, port)
            w.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await w.drain()
            try:
                await _pipe_pair(r, w, up_r, up_w)
            finally:
                up_w.close()
                await up_w.wait_closed()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            w.close()
            await w.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), seen


async def _start_mock_http_proxy(
    *,
    expected_auth: tuple[str, str] | None = None,
) -> tuple[asyncio.Server, int, list[tuple[str, int]]]:
    seen: list[tuple[str, int]] = []
    expected_token = (
        base64.b64encode(f"{expected_auth[0]}:{expected_auth[1]}".encode("utf-8")).decode("ascii")
        if expected_auth
        else ""
    )

    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            first = (await r.readline()).decode("ascii", errors="replace").strip()
            parts = first.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                w.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await w.drain()
                return
            host, port_text = parts[1].rsplit(":", 1)
            port = int(port_text)
            auth_header = ""
            while True:
                line = await r.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                text = line.decode("ascii", errors="replace")
                if text.lower().startswith("proxy-authorization:"):
                    auth_header = text.split(":", 1)[1].strip()
            if expected_token and auth_header != f"Basic {expected_token}":
                w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                await w.drain()
                return
            seen.append((host, port))
            up_r, up_w = await asyncio.open_connection(host, port)
            w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await w.drain()
            try:
                await _pipe_pair(r, w, up_r, up_w)
            finally:
                up_w.close()
                await up_w.wait_closed()
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            w.close()
            await w.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), seen


async def _socks5_client_exchange(
    bridge_port: int, target_host: str, target_port: int, payload: bytes
) -> tuple[int, bytes]:
    r, w = await asyncio.open_connection("127.0.0.1", bridge_port)
    try:
        w.write(b"\x05\x01\x00")
        await w.drain()
        assert await r.readexactly(2) == b"\x05\x00"
        host_bytes = target_host.encode("ascii")
        w.write(bytes([5, 1, 0, 3, len(host_bytes)]) + host_bytes + struct.pack("!H", target_port))
        await w.drain()
        rep = await r.readexactly(4)
        atyp = rep[3]
        if atyp == 1:
            await r.readexactly(4)
        elif atyp == 3:
            ln = (await r.readexactly(1))[0]
            await r.readexactly(ln)
        else:
            await r.readexactly(16)
        await r.readexactly(2)
        if rep[1] != 0:
            return rep[1], b""
        w.write(payload)
        await w.drain()
        if w.can_write_eof():
            w.write_eof()
        received = await r.read(4096)
        return rep[1], received
    finally:
        w.close()
        await w.wait_closed()


class TestTwoHopProxyChaining(unittest.TestCase):
    def _run_chain_case(self, hop_scheme: str, upstream_scheme: str) -> None:
        async def _scenario():
            echo_srv, echo_port = await _start_echo_server()
            if hop_scheme == "socks5":
                hop_srv, hop_port, hop_seen = await _start_mock_socks5_proxy(expected_auth=("hopu", "hopp"))
            else:
                hop_srv, hop_port, hop_seen = await _start_mock_http_proxy(expected_auth=("hopu", "hopp"))
            if upstream_scheme == "socks5":
                up_srv, up_port, up_seen = await _start_mock_socks5_proxy(expected_auth=("upu", "upp"))
            else:
                up_srv, up_port, up_seen = await _start_mock_http_proxy(expected_auth=("upu", "upp"))

            hop_entry = parse_proxy(f"{hop_scheme}://hopu:hopp@127.0.0.1:{hop_port}")
            up_entry = parse_proxy(f"{upstream_scheme}://upu:upp@127.0.0.1:{up_port}")
            try:
                async with LocalProxyBridge(upstream=up_entry, hop_proxy=hop_entry, connect_timeout=3.0) as bridge:
                    code, reply = await _socks5_client_exchange(bridge._port, "127.0.0.1", echo_port, b"ping-two-hop")
                    snap = bridge.traffic_snapshot()
                return code, reply, hop_seen, up_seen, up_port, echo_port, snap
            finally:
                for srv in (echo_srv, hop_srv, up_srv):
                    srv.close()
                    await srv.wait_closed()

        code, reply, hop_seen, up_seen, up_port, echo_port, snap = asyncio.run(_scenario())
        self.assertEqual(code, 0)
        self.assertEqual(reply, b"ECHO:ping-two-hop")
        self.assertEqual(hop_seen, [("127.0.0.1", up_port)])
        self.assertEqual(up_seen, [("127.0.0.1", echo_port)])
        self.assertGreater(snap["upload_bytes"], 0)
        self.assertGreater(snap["download_bytes"], 0)

    def test_socks5_to_socks5_two_hop_chain(self):
        self._run_chain_case("socks5", "socks5")

    def test_http_to_socks5_two_hop_chain(self):
        self._run_chain_case("http", "socks5")

    def test_socks5_to_http_two_hop_chain(self):
        self._run_chain_case("socks5", "http")

    def test_http_to_http_two_hop_chain(self):
        self._run_chain_case("http", "http")

    def test_blocked_host_short_circuits_before_dialing_hop_proxy(self):
        async def _scenario():
            hop_srv, hop_port, hop_seen = await _start_mock_socks5_proxy()
            up_entry = parse_proxy("socks5://127.0.0.1:9999")
            hop_entry = parse_proxy(f"socks5://127.0.0.1:{hop_port}")
            try:
                async with LocalProxyBridge(
                    upstream=up_entry,
                    hop_proxy=hop_entry,
                    blocked_hosts=("dl.google.com",),
                    connect_timeout=2.0,
                ) as bridge:
                    code, reply = await _socks5_client_exchange(bridge._port, "dl.google.com", 443, b"x")
                    snap = bridge.traffic_snapshot()
                return code, reply, hop_seen, snap
            finally:
                hop_srv.close()
                await hop_srv.wait_closed()

        code, reply, hop_seen, snap = asyncio.run(_scenario())
        self.assertEqual(code, 0x02)
        self.assertEqual(reply, b"")
        self.assertEqual(hop_seen, [])
        self.assertEqual(snap["blocked_connections"], 1)

    def test_two_hop_failure_and_logs_never_expose_plaintext_passwords(self):
        async def _scenario():
            hop_srv, hop_port, _ = await _start_mock_socks5_proxy()
            up_srv, up_port, _ = await _start_mock_socks5_proxy(
                expected_auth=("right_user", "right_pass"),
                reject_connect=True,
            )
            hop_entry = parse_proxy(f"socks5://hopuser:SECRET_HOP_PW@127.0.0.1:{hop_port}")
            up_entry = parse_proxy(f"socks5://upuser:SECRET_UP_PW@127.0.0.1:{up_port}")
            try:
                with self.assertLogs("proxy_bridge", level=logging.DEBUG) as logs:
                    async with LocalProxyBridge(upstream=up_entry, hop_proxy=hop_entry, connect_timeout=2.0) as bridge:
                        code, _ = await _socks5_client_exchange(bridge._port, "example.invalid", 443, b"hi")
                return code, "\n".join(logs.output)
            finally:
                for srv in (hop_srv, up_srv):
                    srv.close()
                    await srv.wait_closed()

        code, combined_logs = asyncio.run(_scenario())
        self.assertNotEqual(code, 0)
        self.assertNotIn("SECRET_HOP_PW", combined_logs)
        self.assertNotIn("SECRET_UP_PW", combined_logs)
        self.assertIn("hop ", combined_logs)

    def test_helpers_force_bridge_when_hop_proxy_explicit_or_env(self):
        self.assertTrue(needs_bridge("socks5://host:1080", hop_proxy="socks5://127.0.0.1:7890"))
        url, closer, bridge = open_browser_proxy_bridge(
            "socks5://host:1080",
            hop_proxy="http://127.0.0.1:7890",
        )
        try:
            self.assertTrue(url.startswith("socks5h://127.0.0.1:"))
            assert bridge is not None
            assert isinstance(bridge.hop_proxy, ProxyEntry)
            self.assertEqual(bridge.hop_proxy.port, 7890)
        finally:
            closer()

        with patch.dict(os.environ, {"GPT_PROXY_CHAIN_HOP": "socks5://127.0.0.1:10808"}, clear=False):
            self.assertTrue(needs_bridge("socks5://host:1080"))
            env_url, env_closer = proxy_for_browser("socks5://host:1080")
            try:
                self.assertTrue(env_url.startswith("socks5h://127.0.0.1:"))
            finally:
                env_closer()

            async def _check_async():
                a_url, a_closer = await async_proxy_for_browser("socks5://host:1080")
                try:
                    return a_url
                finally:
                    await a_closer()

            self.assertTrue(asyncio.run(_check_async()).startswith("socks5h://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
