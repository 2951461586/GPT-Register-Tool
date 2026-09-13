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
    _http_connect_authority,
    _http_connect_handshake,
)
from sms_tool.proxy_health import ProxyHealthTracker


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

    def test_from_url_raises_on_unparseable(self):
        # A garbage upstream used to fall through to a permissive urlparse
        # fallback and become a live 127.0.0.1:1080 entry; it must raise.
        for bad in ("", "socks5://", "://", "http://[bad"):
            with self.assertRaises(ValueError):
                UpstreamProxy.from_url(bad)

    def test_from_url_four_segment(self):
        u = UpstreamProxy.from_url("gate.kookeey.info:1000:9408785-edbd645b:54ad4d54-JP")
        self.assertEqual(u.host, "gate.kookeey.info")
        self.assertEqual(u.port, 1000)
        self.assertTrue(u.username)

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

    def test_fail_open_half_open_returns_single_candidate(self):
        """P0-3 half-open: when the whole pool is unhealthy, _pick_upstream returns
        a single candidate for incremental probing instead of mass-resurrecting every
        dead upstream (which would spray all of them with live traffic at once)."""
        u1 = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=False, fail_count=2)
        u2 = UpstreamProxy(host="2.2.2.2", port=1080, label="b", healthy=False, fail_count=2)
        server = self._make_server([u1, u2])

        pick = server._pick_upstream()
        self.assertIsNotNone(pick)
        self.assertIn(pick, (u1, u2))
        # The pool is NOT mass-resurrected: both upstreams stay unhealthy, only one
        # candidate is handed back for a probe.
        self.assertFalse(u1.healthy)
        self.assertFalse(u2.healthy)

    def test_fail_open_half_open_rotates_candidates(self):
        """Across successive half-open picks the candidates rotate among the dead
        set, so recovery of each upstream is discovered incrementally."""
        u1 = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=False, fail_count=2)
        u2 = UpstreamProxy(host="2.2.2.2", port=1080, label="b", healthy=False, fail_count=2)
        server = self._make_server([u1, u2])

        picks = [server._pick_upstream().label for _ in range(4)]
        # Both candidates appear, round-robin among the dead set.
        self.assertEqual(sorted(set(picks)), ["a", "b"])

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


class TestHealthStrategy(unittest.TestCase):
    """P0-3: consecutive-failure threshold + hysteresis recovery."""

    def _make_server(self, fail_threshold=3, target="cloudflare.com", port=443):
        return Socks5Server(
            "127.0.0.1", 0, [], stats_port=0,
            health_check_target_host=target,
            health_check_target_port=port,
            health_check_fail_threshold=fail_threshold,
        )

    def test_constructor_wires_probe_target_and_threshold(self):
        server = self._make_server(fail_threshold=5, target="example.com", port=8443)
        self.assertEqual(server._health_check_target_host, "example.com")
        self.assertEqual(server._health_check_target_port, 8443)
        self.assertEqual(server._health_check_fail_threshold, 5)

    def test_healthy_survives_single_failure(self):
        server = self._make_server(fail_threshold=3)
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=True)
        server._apply_health(u, False)
        self.assertTrue(u.healthy)
        self.assertEqual(u.fail_count, 1)

    def test_death_only_after_threshold_consecutive_failures(self):
        server = self._make_server(fail_threshold=3)
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=True)
        server._apply_health(u, False)  # 1
        self.assertTrue(u.healthy)
        server._apply_health(u, False)  # 2
        self.assertTrue(u.healthy)
        server._apply_health(u, False)  # 3 -> threshold reached
        self.assertFalse(u.healthy)
        self.assertEqual(u.fail_count, 3)

    def test_recovery_requires_consecutive_successes(self):
        server = self._make_server(fail_threshold=3)
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=False, fail_count=3)
        # One lucky success is not enough (hysteresis): fail_count decrements by 1,
        # but the upstream stays unhealthy until it reaches fail_count == 0.
        server._apply_health(u, True)
        self.assertEqual(u.fail_count, 2)
        self.assertFalse(u.healthy)
        server._apply_health(u, True)
        self.assertEqual(u.fail_count, 1)
        self.assertFalse(u.healthy)
        server._apply_health(u, True)
        self.assertEqual(u.fail_count, 0)
        self.assertTrue(u.healthy)

    def test_recovery_interrupted_by_failure_resets_progress(self):
        server = self._make_server(fail_threshold=3)
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=False, fail_count=3)
        server._apply_health(u, True)  # -> 2
        server._apply_health(u, True)  # -> 1
        self.assertFalse(u.healthy)
        server._apply_health(u, False)  # -> 2 again
        self.assertEqual(u.fail_count, 2)
        self.assertFalse(u.healthy)

    def test_success_on_healthy_does_not_mark_unhealthy(self):
        server = self._make_server(fail_threshold=3)
        u = UpstreamProxy(host="1.1.1.1", port=1080, label="a", healthy=True, fail_count=0)
        server._apply_health(u, True)
        self.assertTrue(u.healthy)
        self.assertEqual(u.fail_count, 0)


class TestHealthSyncP1_2(unittest.TestCase):
    """P1-2 one-way sync: SOCKS5 pool health events mirror into ProxyHealthTracker."""

    def _make_server_with_tracker(self, fail_threshold=3):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        tracker = ProxyHealthTracker({}, path=d / "registration_proxy_health.json")
        server = Socks5Server(
            "127.0.0.1", 0, [], stats_port=0,
            health_check_fail_threshold=fail_threshold,
            health_tracker=tracker,
        )
        return server, tracker

    def test_proxy_url_format_no_auth(self):
        u = UpstreamProxy(host="1.2.3.4", port=1080)
        self.assertEqual(u.proxy_url, "socks5://1.2.3.4:1080")

    def test_proxy_url_format_with_auth(self):
        u = UpstreamProxy(host="1.2.3.4", port=1080, username="u", password="p")
        self.assertEqual(u.proxy_url, "socks5://u:p@1.2.3.4:1080")

    def test_proxy_url_key_matches_tracker_key(self):
        u = UpstreamProxy(host="1.2.3.4", port=1080, username="u", password="p")
        self.assertTrue(
            ProxyHealthTracker.key(u.proxy_url).startswith("1.2.3.4:1080#sid-")
        )

    def test_apply_health_failure_mirrors_to_tracker(self):
        server, tracker = self._make_server_with_tracker()
        u = UpstreamProxy(host="1.2.3.4", port=1080, label="a", healthy=True)
        server._apply_health(u, False, error="probe timeout")
        data = tracker._read()
        self.assertEqual(int(data["1.2.3.4:1080"]["failure"]), 1)
        self.assertEqual(data["1.2.3.4:1080"]["last_error"], "probe timeout")

    def test_apply_health_threshold_mirrors_cooldown(self):
        server, tracker = self._make_server_with_tracker()
        u = UpstreamProxy(host="1.2.3.4", port=1080, label="a", healthy=True)
        for _ in range(3):
            server._apply_health(u, False, error="x")
        row = tracker._read()["1.2.3.4:1080"]
        self.assertGreaterEqual(int(row["failure"]), 3)
        self.assertGreater(float(row["cooldown_until"]), 0)

    def test_apply_health_success_resets_cooldown_in_tracker(self):
        server, tracker = self._make_server_with_tracker()
        u = UpstreamProxy(host="1.2.3.4", port=1080, label="a", healthy=True)
        for _ in range(3):
            server._apply_health(u, False, error="x")
        server._apply_health(u, True)
        row = tracker._read()["1.2.3.4:1080"]
        self.assertEqual(float(row["cooldown_until"]), 0)

    def test_apply_health_without_tracker_is_noop(self):
        server = Socks5Server("127.0.0.1", 0, [], stats_port=0)
        u = UpstreamProxy(host="1.2.3.4", port=1080, label="a", healthy=True)
        # Must not raise even when no tracker is configured.
        server._apply_health(u, False, error="x")
        self.assertEqual(u.fail_count, 1)


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
    def test_upstreams_from_proxy_cfg(self):
        from start_proxy_pool import _upstreams_from_proxy_cfg

        cfg = {
            "proxy": {
                "pool": [
                    {"url": "socks5://127.0.0.1:7897", "label": "clash"},
                    {"url": "socks5://10.0.0.1:1080", "label": "remote", "username": "u", "password": "p"},
                    "socks5://plain:1080",
                ]
            }
        }
        upstreams = _upstreams_from_proxy_cfg(cfg)

        self.assertEqual(len(upstreams), 3)
        self.assertEqual(upstreams[0].host, "127.0.0.1")
        self.assertEqual(upstreams[0].label, "clash")
        self.assertEqual(upstreams[1].username, "u")
        self.assertEqual(upstreams[1].password, "p")
        self.assertEqual(upstreams[2].host, "plain")

    def test_upstreams_from_proxy_cfg_skips_unsupported_scheme_and_empty(self):
        """HTTP upstreams are accepted now; only unknown schemes are skipped.

        A SOCKS5-only filter here emptied a 30-entry residential pool (all
        ``http://``) down to zero upstreams, and the server exited.
        """
        from start_proxy_pool import _upstreams_from_proxy_cfg

        cfg = {
            "proxy": {
                "pool": [
                    "http://1.2.3.4:8080",
                    "socks5://5.6.7.8:1080",
                    "ftp://9.9.9.9:21",
                    {"label": "no-url"},
                    {"bogus": True},
                ]
            }
        }
        upstreams = _upstreams_from_proxy_cfg(cfg)
        self.assertEqual([u.addr for u in upstreams], ["1.2.3.4:8080", "5.6.7.8:1080"])
        self.assertEqual([u.scheme for u in upstreams], ["http", "socks5"])

    def test_upstreams_priority_from_proxy_cfg(self):
        from start_proxy_pool import _upstreams_from_proxy_cfg

        cfg = {
            "proxy": {
                "pool": [
                    {"url": "socks5://127.0.0.1:17912", "label": "jp", "priority": 0},
                    {"url": "socks5://127.0.0.1:7897", "label": "clash", "priority": 1},
                ]
            }
        }
        upstreams = _upstreams_from_proxy_cfg(cfg)
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


class _FakeHttpProxy:
    """A stand-in HTTP CONNECT endpoint: records the request, then tunnels."""

    def __init__(self, status=b"HTTP/1.1 200 Connection established", payload=b"TUNNELED"):
        self.status = status
        self.payload = payload
        self.requests: list[tuple[str, list[str]]] = []
        self._server = None
        self.port = 0

    async def start(self):
        self._server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _on_conn(self, reader, writer):
        request_line = (await reader.readline()).decode("ascii", "replace").strip()
        headers = []
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line.decode("ascii", "replace").strip())
        self.requests.append((request_line, headers))
        writer.write(self.status + b"\r\n\r\n")
        await writer.drain()
        if self.payload:
            writer.write(self.payload)
            await writer.drain()
        writer.close()

    async def close(self):
        self._server.close()
        await self._server.wait_closed()


class TestHttpConnectUpstream(unittest.TestCase):
    """P3: the pool must be able to dial ``http://`` upstreams.

    Every residential provider we buy hands out ``http://`` endpoints, so a
    SOCKS5-only pool emptied a 30-entry pool file down to zero upstreams.
    """

    def test_authority_brackets_ipv6_only(self):
        self.assertEqual(_http_connect_authority("cloudflare.com", 443), "cloudflare.com:443")
        self.assertEqual(_http_connect_authority("2001:db8::1", 443), "[2001:db8::1]:443")

    def test_from_url_keeps_the_http_scheme(self):
        u = UpstreamProxy.from_url("http://user:pw@proxy.example:8080")
        self.assertEqual(u.scheme, "http")
        self.assertEqual((u.host, u.port), ("proxy.example", 8080))
        self.assertEqual(u.proxy_url, "http://user:pw@proxy.example:8080")

    def test_socks5_stays_the_default_scheme(self):
        self.assertEqual(UpstreamProxy(host="1.2.3.4", port=1080).scheme, "socks5")
        self.assertEqual(UpstreamProxy.from_url("socks5h://1.2.3.4:1080").scheme, "socks5h")

    def test_handshake_sends_connect_and_basic_auth(self):
        async def _run():
            fake = await _FakeHttpProxy().start()
            try:
                r, w = await asyncio.open_connection("127.0.0.1", fake.port)
                try:
                    await _http_connect_handshake(
                        r, w,
                        UpstreamProxy(host="h", port=1, username="u", password="p", scheme="http"),
                        "cloudflare.com", 443, 5.0,
                    )
                    self.assertEqual(await asyncio.wait_for(r.read(8), 5.0), b"TUNNELED")
                finally:
                    w.close()
                request_line, headers = fake.requests[0]
            finally:
                await fake.close()
            return request_line, headers

        request_line, headers = asyncio.run(_run())
        self.assertEqual(request_line, "CONNECT cloudflare.com:443 HTTP/1.1")
        self.assertIn("Host: cloudflare.com:443", headers)
        # base64("u:p")
        self.assertIn("Proxy-Authorization: Basic dTpw", headers)

    def test_no_credentials_sends_no_auth_header(self):
        async def _run():
            fake = await _FakeHttpProxy().start()
            try:
                r, w = await asyncio.open_connection("127.0.0.1", fake.port)
                try:
                    await _http_connect_handshake(
                        r, w, UpstreamProxy(host="h", port=1, scheme="http"),
                        "cloudflare.com", 443, 5.0,
                    )
                finally:
                    w.close()
                headers = fake.requests[0][1]
            finally:
                await fake.close()
            return headers

        headers = asyncio.run(_run())
        self.assertFalse(any(h.lower().startswith("proxy-authorization") for h in headers))

    def test_non_2xx_reply_raises(self):
        async def _run():
            fake = await _FakeHttpProxy(
                status=b"HTTP/1.1 407 Proxy Authentication Required", payload=b""
            ).start()
            try:
                r, w = await asyncio.open_connection("127.0.0.1", fake.port)
                try:
                    with self.assertRaises(ConnectionError) as ctx:
                        await _http_connect_handshake(
                            r, w, UpstreamProxy(host="h", port=1, scheme="http"),
                            "cloudflare.com", 443, 5.0,
                        )
                finally:
                    w.close()
            finally:
                await fake.close()
            return str(ctx.exception)

        self.assertIn("HTTP 407", asyncio.run(_run()))

    def test_handshake_dispatches_on_scheme(self):
        """socks5 still sends 0x05; http sends a CONNECT line."""
        async def _run():
            seen: asyncio.Queue[bytes] = asyncio.Queue()

            async def _record(reader, writer):
                await seen.put(await reader.read(64))
                writer.close()

            srv = await asyncio.start_server(_record, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            server = Socks5Server("127.0.0.1", 0, [], stats_port=0)
            out = {}
            try:
                for scheme in ("socks5", "http"):
                    r, w = await asyncio.open_connection("127.0.0.1", port)
                    try:
                        with self.assertRaises(Exception):
                            await server._handshake(
                                r, w,
                                UpstreamProxy(host="127.0.0.1", port=port, scheme=scheme),
                                "cloudflare.com", 443, _ATYP_DOMAIN, 2.0,
                            )
                    finally:
                        w.close()
                    out[scheme] = await asyncio.wait_for(seen.get(), 2.0)
            finally:
                srv.close()
                await srv.wait_closed()
            return out

        out = asyncio.run(_run())
        self.assertEqual(out["socks5"][:1], b"\x05")
        self.assertTrue(out["http"].startswith(b"CONNECT "), out["http"])

    def test_socks5_client_tunnels_through_an_http_upstream(self):
        """End-to-end: SOCKS5 in, HTTP CONNECT out, bytes both ways."""
        async def _run():
            fake = await _FakeHttpProxy().start()
            server = Socks5Server(
                "127.0.0.1", 0,
                [UpstreamProxy(host="127.0.0.1", port=fake.port, scheme="http", label="http-up")],
                stats_port=0,
                health_check_interval=3600.0,
            )
            await server.start()
            listen_port = server._server.sockets[0].getsockname()[1]
            try:
                r, w = await asyncio.open_connection("127.0.0.1", listen_port)
                try:
                    w.write(bytes([_SOCKS5_VER, 0x01, _NO_AUTH]))
                    await w.drain()
                    self.assertEqual((await r.readexactly(2))[1], _NO_AUTH)

                    domain = b"cloudflare.com"
                    req = bytearray([_SOCKS5_VER, _CMD_CONNECT, 0x00, _ATYP_DOMAIN])
                    req.append(len(domain))
                    req.extend(domain)
                    req.extend(struct.pack("!H", 443))
                    w.write(bytes(req))
                    await w.drain()

                    reply = await asyncio.wait_for(r.readexactly(4), 5.0)
                    self.assertEqual(reply[1], 0x00)  # SUCCEEDED
                    await asyncio.wait_for(r.readexactly(6), 5.0)  # bound addr + port
                    payload = await asyncio.wait_for(r.read(8), 5.0)
                finally:
                    w.close()
            finally:
                await server.stop()
                await fake.close()
            return payload, fake.requests[0]

        payload, (request_line, _headers) = asyncio.run(_run())
        self.assertEqual(payload, b"TUNNELED")
        self.assertEqual(request_line, "CONNECT cloudflare.com:443 HTTP/1.1")


if __name__ == "__main__":
    unittest.main()
