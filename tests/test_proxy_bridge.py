"""Unit tests for sms_tool.proxy_bridge (local SOCKS5 bridge lifecycle)."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from sms_tool.proxy_bridge import (
    LocalProxyBridge,
    async_proxy_for_browser,
    needs_bridge,
    proxy_for_browser,
)
from sms_tool.proxy_entry import parse_proxy


class TestLocalProxyBridgeLifecycle(unittest.TestCase):
    def setUp(self):
        self.upstream = parse_proxy("socks5://user:pass@127.0.0.1:9999")

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

    @patch("sms_tool.proxy_bridge.LocalProxyBridge._connect_upstream")
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
        self.upstream = parse_proxy("http://user:pass@upstream.example:8080")
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


if __name__ == "__main__":
    unittest.main()