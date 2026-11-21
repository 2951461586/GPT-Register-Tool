"""Tests for browser data saver and proxy bridge host blocking / traffic accounting (P1-1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from sms_tool.proxy.bridge import (
    LocalProxyBridge,
    _REP_NOT_ALLOWED,
    _host_matches_blocked,
    open_browser_proxy_bridge,
)
from sms_tool.proxy.entry import parse_proxy
from sms_tool.registration_drivers.data_saver import (
    BrowserDataSaverConfig,
    StaticAssetCache,
    install_playwright_data_saver,
    is_cacheable_static_asset,
    is_protected_url,
    should_abort_request,
)


def _socks5_connect_domain(host: str, port: int) -> bytes:
    host_b = host.encode("ascii")
    return (
        b"\x05\x01\x00"  # greeting: v5, 1 method, no-auth
        + b"\x05\x01\x00\x03"
        + bytes([len(host_b)])
        + host_b
        + port.to_bytes(2, "big")
    )


class _AsyncBytesReader:
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def read(self, n: int = -1) -> bytes:
        if not self._buf:
            return b""
        chunk = bytes(self._buf[:n] if n > 0 else self._buf)
        del self._buf[: len(chunk)]
        return chunk


class _AsyncBytesWriter:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def can_write_eof(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_host_matches_blocked_uses_domain_boundaries():
    rules = ("dl.google.com", "gvt1.com")
    assert _host_matches_blocked("dl.google.com", rules) is True
    assert _host_matches_blocked("DL.GOOGLE.COM.", rules) is True
    assert _host_matches_blocked("rr8---sn.gvt1.com", rules) is True
    assert _host_matches_blocked("notgvt1.com", rules) is False
    assert _host_matches_blocked("gvt1.com.evil.example", rules) is False
    assert _host_matches_blocked("chatgpt.com", rules) is False


def test_proxy_bridge_blocks_host_before_upstream_and_counts_it():
    upstream = parse_proxy("http://user:pass@upstream.example:8080")
    assert upstream is not None
    bridge = LocalProxyBridge(upstream=upstream, blocked_hosts=("dl.google.com", "gvt1.com"))
    reader: Any = _AsyncBytesReader(_socks5_connect_domain("rr1.gvt1.com", 443))
    writer: Any = _AsyncBytesWriter()

    with patch.object(bridge, "_connect_upstream") as connect_mock:
        asyncio.run(bridge._handle_client(reader, writer))

    connect_mock.assert_not_called()
    # Greeting reply (05 00) + SOCKS5 reply (05 02 = not allowed)
    assert bytes(writer.written[:4]) == bytes([0x05, 0x00, 0x05, _REP_NOT_ALLOWED])
    snap = bridge.traffic_snapshot()
    assert snap["blocked_connections"] == 1
    assert snap["connections"] == 0
    assert snap["total_bytes"] == 0
    assert snap["destinations"][0]["target"] == "rr1.gvt1.com:443"


def test_proxy_bridge_retries_transient_connect_and_tracks_bidirectional_bytes():
    upstream = parse_proxy("http://user:pass@upstream.example:8080")
    assert upstream is not None
    bridge = LocalProxyBridge(upstream=upstream, connect_retries=2)
    reader: Any = _AsyncBytesReader(_socks5_connect_domain("cdn.example.test", 443) + b"hello-up")
    writer: Any = _AsyncBytesWriter()
    up_r = _AsyncBytesReader(b"hello-down-payload")
    up_w = _AsyncBytesWriter()
    attempts = {"n": 0}

    async def flaky_connect(host, port, atyp):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("transient reset")
        return up_r, up_w

    with patch.object(bridge, "_connect_upstream", side_effect=flaky_connect):
        asyncio.run(bridge._handle_client(reader, writer))

    assert attempts["n"] == 2
    assert bytes(up_w.written) == b"hello-up"
    snap = bridge.traffic_snapshot()
    assert snap["connections"] == 1
    assert snap["connect_retry_count"] == 1
    assert snap["recovered_connection_count"] == 1
    assert snap["upload_bytes"] == len(b"hello-up")
    assert snap["download_bytes"] == len(b"hello-down-payload")
    assert snap["total_bytes"] == len(b"hello-up") + len(b"hello-down-payload")


def test_open_browser_proxy_bridge_force_bridge_wraps_plain_socks():
    url, closer, bridge = open_browser_proxy_bridge(
        "socks5://127.0.0.1:9999",
        blocked_hosts=("dl.google.com",),
        connect_retries=2,
        force_bridge=True,
    )
    try:
        assert bridge is not None
        assert url.startswith("socks5h://127.0.0.1:")
        assert bridge.blocked_hosts == ("dl.google.com",)
        assert bridge.connect_retries == 2
    finally:
        closer()


def test_data_saver_rules_protect_auth_sentinel_and_captcha():
    cfg = BrowserDataSaverConfig(enabled=True)
    assert is_protected_url("https://sentinel.openai.com/sentinel/sdk.js") is True
    assert is_protected_url("https://chatgpt.com/cdn-cgi/challenge-platform/h/b") is True
    assert should_abort_request("https://chatgpt.com/cdn-cgi/challenge-platform/img.png", "image", cfg) == (False, "")
    assert should_abort_request("https://auth.openai.com/awe/api/v2/rum?x=1", "fetch", cfg)[0] is True
    assert should_abort_request("https://cdn.oaistatic.com/banner.webp", "image", cfg) == (True, "type:image")
    assert should_abort_request("https://cdn.oaistatic.com/assets/app-123.js", "script", cfg) == (False, "")

    assert is_cacheable_static_asset("https://cdn.oaistatic.com/assets/chunk-1.js", "script") is True
    assert is_cacheable_static_asset("https://chatgpt.com/_next/static/chunks/main.js", "script") is True
    assert is_cacheable_static_asset("https://chatgpt.com/backend-api/sentinel/req", "fetch") is False
    assert is_cacheable_static_asset("https://auth.openai.com/api/accounts/user/register", "script") is False
    assert is_cacheable_static_asset("https://cdn.oaistatic.com/assets/chunk-1.js", "script", method="POST") is False
    assert is_cacheable_static_asset("https://evil.example/assets/chunk-1.js", "script") is False


def test_static_asset_cache_round_trip_and_playwright_route(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cfg = BrowserDataSaverConfig(
        enabled=True,
        asset_cache_enabled=True,
        asset_cache_dir=str(cache_dir),
    )

    registered_handler = {}

    class _FakeContext:
        def route(self, pattern, handler):
            registered_handler["pattern"] = pattern
            registered_handler["handler"] = handler

    handle = install_playwright_data_saver(_FakeContext(), cfg)
    assert handle is not None
    assert registered_handler["pattern"] == "**/*"
    handler = registered_handler["handler"]

    # 1. Blocked image -> abort
    aborted = []
    route_img = SimpleNamespace(
        request=SimpleNamespace(url="https://cdn.oaistatic.com/pic.png", resource_type="image", method="GET"),
        abort=lambda: aborted.append("img"),
        continue_=lambda: pytest.fail("should abort image"),
    )
    handler(route_img)
    assert aborted == ["img"]

    # 2. Static JS miss -> fetch + cache + fulfill
    asset_url = "https://cdn.oaistatic.com/assets/app-abc.js"
    fulfilled: list[dict] = []
    fetched_resp = SimpleNamespace(
        status=200,
        headers={"content-type": "application/javascript", "set-cookie": "secret=1"},
        body=lambda: b"console.log('ok');",
    )
    route_miss = SimpleNamespace(
        request=SimpleNamespace(url=asset_url, resource_type="script", method="GET"),
        abort=lambda: pytest.fail("must not abort core script"),
        continue_=lambda: pytest.fail("should fetch for cache"),
        fetch=lambda: fetched_resp,
        fulfill=lambda **kw: fulfilled.append(kw),
    )
    handler(route_miss)
    assert len(fulfilled) == 1 and fulfilled[0]["response"] is fetched_resp

    # 3. Same static JS on a second request -> served from disk cache (no fetch!)
    fulfilled_hit: list[dict] = []
    route_hit = SimpleNamespace(
        request=SimpleNamespace(url=asset_url, resource_type="script", method="GET"),
        abort=lambda: pytest.fail("must not abort"),
        continue_=lambda: pytest.fail("should fulfill from cache"),
        fetch=lambda: pytest.fail("cache hit must not hit network"),
        fulfill=lambda **kw: fulfilled_hit.append(kw),
    )
    handler(route_hit)
    assert len(fulfilled_hit) == 1
    assert fulfilled_hit[0]["body"] == b"console.log('ok');"
    assert "set-cookie" not in fulfilled_hit[0]["headers"]
    assert fulfilled_hit[0]["headers"]["content-type"] == "application/javascript"

    snap = handle.snapshot()
    assert snap["aborted_requests"] == 1
    assert snap["cache_hits"] == 1
    assert snap["cache_recorded"] == 1
    assert snap["bytes_saved"] == len(b"console.log('ok');")
    assert "[DataSaver]" in handle.summary_line()


def test_data_saver_config_from_config_defaults_off_and_respects_env(monkeypatch):
    monkeypatch.delenv("BROWSER_DATA_SAVER", raising=False)
    assert BrowserDataSaverConfig.from_config({}).enabled is False
    assert (
        BrowserDataSaverConfig.from_config({"registration": {"browser_data_saver": {"enabled": True}}}).enabled is True
    )

    monkeypatch.setenv("BROWSER_DATA_SAVER", "1")
    assert BrowserDataSaverConfig.from_config({}).enabled is True

    cache = StaticAssetCache(Path("."), max_age_seconds=0)
    assert cache.put("https://chatgpt.com/api/auth/session", status=200, headers={}, body=b"{}") is False
