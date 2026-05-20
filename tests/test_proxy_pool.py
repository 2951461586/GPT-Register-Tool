"""Unit tests for sms_tool.proxy_pool."""

from __future__ import annotations

import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sms_tool.proxy_pool import (
    _ATYP_DOMAIN,
    _ATYP_IPV4,
    _ATYP_IPV6,
    _CMD_CONNECT,
    _NO_AUTH,
    _NO_ACCEPTABLE,
    _SOCKS5_VER,
    _USERPASS,
    UpstreamProxy,
    Socks5Server,
    _build_socks5_reply,
    _encode_socks5_addr,
)


class TestUpstreamProxy(unittest.TestCase):
    def test_from_url_basic(self):
        u = UpstreamProxy.from_url("socks5://127.0.0.1:7897")
        self.assertEqual(u.host, "127.0.0.1")
        self.assertEqual(u.port, 7897)
        self.assertEqual(u.username, "")
        self.assertEqual(u.password, "")

    def test_from_url_with_auth(self):
        u = UpstreamProxy.from_url("socks5://user:pass@proxy.example.com:1080")
        self.assertEqual(u.host, "proxy.example.com")
        self.assertEqual(u.port, 1080)
        self.assertEqual(u.username, "user")
        self.assertEqual(u.password, "pass")

    def test_from_url_label(self):
        u = UpstreamProxy.from_url("socks5://1.2.3.4:1080", label="my-proxy")
        self.assertEqual(u.label, "my-proxy")

    def test_from_url_default_label(self):
        u = UpstreamProxy.from_url("socks5://1.2.3.4:1080")
        self.assertEqual(u.label, "socks5://1.2.3.4:1080")

    def test_from_url_socks5h(self):
        u = UpstreamProxy.from_url("socks5h://127.0.0.1:17912")
        self.assertEqual(u.host, "127.0.0.1")
        self.assertEqual(u.port, 17912)

    def test_addr_property(self):
        u = UpstreamProxy(host="10.0.0.1", port=3128)
        self.assertEqual(u.addr, "10.0.0.1:3128")

    def test_to_dict(self):
        u = UpstreamProxy(host="1.2.3.4", port=1080, label="test", success_count=5)
        d = u.to_dict()
        self.assertEqual(d["label"], "test")
        self.assertEqual(d["addr"], "1.2.3.4:1080")
        self.assertTrue(d["healthy"])
        self.assertEqual(d["success_count"], 5)
        self.assertIsNone(d["last_check"])

    def test_from_url_priority(self):
        u = UpstreamProxy.from_url("socks5://1.2.3.4:1080", label="p", priority=2)
        self.assertEqual(u.priority, 2)

    def test_default_priority(self):
        u = UpstreamProxy(host="1.1.1.1", port=1080)
        self.assertEqual(u.priority, 0)

    def test_to_dict_includes_priority(self):
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="x", priority=3)
        self.assertEqual(u.to_dict()["priority"], 3)


class TestSocks5AddrEncoding(unittest.TestCase):
    def test_encode_ipv4(self):
        data = _encode_socks5_addr("192.168.1.1", 8080)
        self.assertEqual(data[0], _ATYP_IPV4)
        self.assertEqual(len(data), 1 + 4 + 2)  # ATYP + 4 bytes IP + 2 bytes port
        port = struct.unpack("!H", data[5:7])[0]
        self.assertEqual(port, 8080)

    def test_encode_ipv6(self):
        data = _encode_socks5_addr("::1", 443)
        self.assertEqual(data[0], _ATYP_IPV6)
        self.assertEqual(len(data), 1 + 16 + 2)

    def test_encode_domain(self):
        data = _encode_socks5_addr("example.com", 443)
        self.assertEqual(data[0], _ATYP_DOMAIN)
        self.assertEqual(data[1], len("example.com"))
        domain = data[2:2 + len("example.com")].decode()
        self.assertEqual(domain, "example.com")
        port = struct.unpack("!H", data[2 + len("example.com"):])[0]
        self.assertEqual(port, 443)


class TestSocks5Reply(unittest.TestCase):
    def test_reply_structure(self):
        reply = _build_socks5_reply(0x00, "0.0.0.0", 0)
        self.assertEqual(reply[0], _SOCKS5_VER)
        self.assertEqual(reply[1], 0x00)  # SUCCEEDED
        self.assertEqual(reply[2], 0x00)  # RSV


class TestPickUpstream(unittest.TestCase):
    def _make_server(self, upstreams):
        return Socks5Server("127.0.0.1", 0, upstreams, stats_port=0)

    def test_round_robin(self):
        u1 = UpstreamProxy(host="1.1.1.1", port=1080, label="a")
        u2 = UpstreamProxy(host="2.2.2.2", port=1080, label="b")
        server = self._make_server([u1, u2])

        picks = [server._pick_upstream().label for _ in range(4)]
        self.assertEqual(picks, ["a", "b", "a", "b"])

    def test_skips_unhealthy(self):
        u1 = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=False)
        u2 = UpstreamProxy(host="2.2.2.2", port=1080, label="b", healthy=True)
        server = self._make_server([u1, u2])

        pick = server._pick_upstream()
        self.assertEqual(pick.label, "b")

    def test_fail_open_all_unhealthy(self):
        u1 = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=False)
        u2 = UpstreamProxy(host="2.2.2.2", port=1080, label="b", healthy=False)
        server = self._make_server([u1, u2])

        pick = server._pick_upstream()
        self.assertIsNotNone(pick)
        # after fail-open, both should be reset to healthy
        self.assertTrue(u1.healthy)
        self.assertTrue(u2.healthy)

    def test_no_upstreams(self):
        server = self._make_server([])
        self.assertIsNone(server._pick_upstream())

    def test_single_upstream(self):
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="only")
        server = self._make_server([u])
        for _ in range(5):
            self.assertEqual(server._pick_upstream().label, "only")

    def test_priority_prefers_higher(self):
        """Lower priority number = higher priority, selected first."""
        u0 = UpstreamProxy(host="1.1.1.1", port=1080, label="hi", priority=0)
        u1 = UpstreamProxy(host="2.2.2.2", port=1080, label="lo", priority=1)
        server = self._make_server([u0, u1])

        picks = [server._pick_upstream().label for _ in range(4)]
        # should always pick the higher-priority (priority=0) upstream
        self.assertEqual(picks, ["hi", "hi", "hi", "hi"])

    def test_priority_round_robin_within_tier(self):
        """Same priority = round-robin within that tier."""
        u0a = UpstreamProxy(host="1.1.1.1", port=1080, label="a", priority=0)
        u0b = UpstreamProxy(host="2.2.2.2", port=1080, label="b", priority=0)
        u1 = UpstreamProxy(host="3.3.3.3", port=1080, label="c", priority=1)
        server = self._make_server([u0a, u0b, u1])

        picks = [server._pick_upstream().label for _ in range(4)]
        self.assertEqual(picks, ["a", "b", "a", "b"])

    def test_priority_fallback_to_lower_tier(self):
        """When all higher-priority upstreams are unhealthy, fall back to lower tier."""
        u0 = UpstreamProxy(host="1.1.1.1", port=1080, label="hi", priority=0, healthy=False)
        u1 = UpstreamProxy(host="2.2.2.2", port=1080, label="lo", priority=1)
        server = self._make_server([u0, u1])

        pick = server._pick_upstream()
        self.assertEqual(pick.label, "lo")


class TestStatsJson(unittest.TestCase):
    def test_stats_json_structure(self):
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="test-upstream")
        server = Socks5Server("127.0.0.1", 0, [u], stats_port=0)
        stats = server._stats_json()

        self.assertIn("status", stats)
        self.assertEqual(stats["status"], "running")
        self.assertIn("uptime_seconds", stats)
        self.assertIn("active_connections", stats)
        self.assertIn("total_connections", stats)
        self.assertIn("total_errors", stats)
        self.assertIn("upstreams", stats)
        self.assertEqual(len(stats["upstreams"]), 1)
        self.assertEqual(stats["upstreams"][0]["label"], "test-upstream")


class TestConfigLoading(unittest.TestCase):
    def test_load_upstreams_from_config(self):
        import json
        import tempfile
        from start_proxy_pool import _load_upstreams_from_config

        cfg = {
            "proxy_pool": {
                "upstreams": [
                    {"url": "socks5://127.0.0.1:7897", "label": "clash"},
                    {"url": "socks5://10.0.0.1:1080", "label": "remote", "username": "u", "password": "p"},
                    "socks5://plain:1080",
                ]
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            upstreams = _load_upstreams_from_config(f.name)

        self.assertEqual(len(upstreams), 3)
        self.assertEqual(upstreams[0].host, "127.0.0.1")
        self.assertEqual(upstreams[0].label, "clash")
        self.assertEqual(upstreams[1].username, "u")
        self.assertEqual(upstreams[1].password, "p")
        self.assertEqual(upstreams[2].host, "plain")

    def test_load_upstreams_priority_from_config(self):
        import json
        import tempfile
        from start_proxy_pool import _load_upstreams_from_config

        cfg = {
            "proxy_pool": {
                "upstreams": [
                    {"url": "socks5://127.0.0.1:17912", "label": "jp", "priority": 0},
                    {"url": "socks5://127.0.0.1:7897", "label": "clash", "priority": 1},
                ]
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            upstreams = _load_upstreams_from_config(f.name)

        self.assertEqual(upstreams[0].priority, 0)
        self.assertEqual(upstreams[1].priority, 1)

    def test_load_upstreams_from_str(self):
        from start_proxy_pool import _load_upstreams_from_str

        upstreams = _load_upstreams_from_str("socks5://a:1,socks5://b:2")
        self.assertEqual(len(upstreams), 2)
        self.assertEqual(upstreams[0].host, "a")
        self.assertEqual(upstreams[1].host, "b")


class TestSocks5Handshake(unittest.TestCase):
    """Integration test: mock upstream and verify full handshake."""

    def test_handle_client_connect(self):
        async def _run():
            # Mock upstream that accepts SOCKS5
            upstream_r = asyncio.StreamReader()
            # upstream greeting response (NO_AUTH)
            upstream_r.feed_data(bytes([_SOCKS5_VER, _NO_AUTH]))
            # upstream CONNECT reply (success, IPv4 bind 0.0.0.0:0)
            upstream_r.feed_data(
                bytes([_SOCKS5_VER, 0x00, 0x00, _ATYP_IPV4])
                + bytes([0, 0, 0, 0])
                + struct.pack("!H", 0)
            )
            upstream_r.feed_eof()

            upstream_w = AsyncMock()
            upstream_w.write = MagicMock()
            upstream_w.drain = AsyncMock()
            upstream_w.close = MagicMock()
            upstream_w.wait_closed = AsyncMock()
            upstream_w.can_write_eof = MagicMock(return_value=True)
            upstream_w.write_eof = MagicMock()

            # Build server with one upstream
            u = UpstreamProxy(host="127.0.0.1", port=1080, label="mock")
            server = Socks5Server("127.0.0.1", 0, [u], stats_port=0)

            with patch.object(server, "_connect_through_upstream", return_value=(upstream_r, upstream_w)):
                # Client reader: SOCKS5 greeting + CONNECT request for example.com:443
                client_r = asyncio.StreamReader()
                client_r.feed_data(bytes([_SOCKS5_VER, 0x01, _NO_AUTH]))  # greeting
                domain = b"example.com"
                client_r.feed_data(
                    bytes([_SOCKS5_VER, _CMD_CONNECT, 0x00, _ATYP_DOMAIN, len(domain)])
                    + domain
                    + struct.pack("!H", 443)
                )
                client_r.feed_eof()

                client_w = AsyncMock()
                client_w.write = MagicMock()
                client_w.drain = AsyncMock()
                client_w.close = MagicMock()
                client_w.wait_closed = AsyncMock()
                client_w.can_write_eof = MagicMock(return_value=True)
                client_w.write_eof = MagicMock()

                await server._handle_client(client_r, client_w)

            # Verify client received greeting response + success reply
            writes = [call.args[0] for call in client_w.write.call_args_list]
            self.assertTrue(len(writes) >= 2)
            # first write: greeting response
            self.assertEqual(writes[0][0], _SOCKS5_VER)
            self.assertEqual(writes[0][1], _NO_AUTH)
            # second write: CONNECT reply
            self.assertEqual(writes[1][0], _SOCKS5_VER)
            self.assertEqual(writes[1][1], 0x00)  # SUCCEEDED

            # Verify stats
            self.assertEqual(server._stats.total_connections, 1)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
