"""Unit tests for sms_tool.proxy_bridge (local SOCKS5 bridge lifecycle)."""

from __future__ import annotations

import asyncio
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