"""P0-4: a bridged Camoufox launch must be told where it actually exits.

Camoufox's own GeoIP is disabled behind a local SOCKS5 bridge (it would
geolocate ``127.0.0.1``), so the launch has to be handed the egress locale and
timezone.  The orchestrator normally supplies them via ``self.locale``; a
session built directly — CLI, scripts, the browser pool — has no such alignment
and would silently launch ``en-US`` on any egress, which is exactly the
"Brazilian IP, US Eastern clock" contradiction.
"""

from __future__ import annotations

import unittest

from sms_tool import geo
from sms_tool.geo import ProxyGeo
from sms_tool.geo.resolver import reset_shared_geo_resolver
from sms_tool.registration_drivers.external_sessions.managed import (
    CamoufoxBrowserSession,
)

_BRIDGED = CamoufoxBrowserSession._bridged_geo_locale_timezone


class BridgedGeoLocaleTimezoneTests(unittest.TestCase):
    def setUp(self):
        reset_shared_geo_resolver()

    def tearDown(self):
        reset_shared_geo_resolver()

    def test_no_measurement_leaves_the_callers_values_alone(self):
        # Nothing cached: "we do not know" must not be answered with "the US".
        out = _BRIDGED("http://u:p@egress.example:8080", "", "", "pt-BR", "America/Sao_Paulo")
        self.assertEqual(out, ("pt-BR", "America/Sao_Paulo"))

    def test_no_proxy_leaves_the_callers_values_alone(self):
        out = _BRIDGED("", "", "", "en-US", "America/New_York")
        self.assertEqual(out, ("en-US", "America/New_York"))

    def test_measured_egress_overrides_the_session_defaults(self):
        proxy = "http://u:p@egress.example:8080"
        geo.remember_proxy_geo(proxy, ProxyGeo(country="JP", timezone="Asia/Tokyo"))
        out = _BRIDGED(proxy, "", "", "en-US", "America/New_York")
        self.assertEqual(out, ("ja-JP", "Asia/Tokyo"))

    def test_explicit_driver_config_wins(self):
        # ``configured_*`` is what the operator pinned in the driver config;
        # the caller already folded it into ``locale``/``timezone`` before
        # calling (``configured_locale or self.locale``), so a pinned value is
        # passed in unchanged and must survive.
        proxy = "http://u:p@egress.example:8080"
        geo.remember_proxy_geo(proxy, ProxyGeo(country="JP", timezone="Asia/Tokyo"))
        # Both pinned -> nothing is replaced.
        self.assertEqual(
            _BRIDGED(proxy, "ja-JP", "Asia/Tokyo", "ja-JP", "Asia/Tokyo"),
            ("ja-JP", "Asia/Tokyo"),
        )
        # An operator deliberately keeping en-US on a Japanese egress is their
        # call; only the unpinned timezone gets the measurement.
        out = _BRIDGED(proxy, "en-US", "", "en-US", "America/New_York")
        self.assertEqual(out, ("en-US", "Asia/Tokyo"))

    def test_measured_timezone_wins_over_the_profile_default(self):
        # The locale table only covers a handful of markets; an uncurated
        # country still has a measured clock, which is the valuable half.
        proxy = "http://u:p@egress.example:8080"
        geo.remember_proxy_geo(
            proxy, ProxyGeo(country="BR", timezone="America/Sao_Paulo")
        )
        _, timezone = _BRIDGED(proxy, "", "", "en-US", "America/New_York")
        self.assertEqual(timezone, "America/Sao_Paulo")

    def test_never_probes_the_network(self):
        # A browser launch must not block on (or fail because of) a geo probe.
        proxy = "http://u:p@egress.example:8080"
        resolver = geo.shared_geo_resolver()
        resolver.probe = _ExplodingProbe()
        out = _BRIDGED(proxy, "", "", "en-US", "America/New_York")
        self.assertEqual(out, ("en-US", "America/New_York"))
        self.assertEqual(resolver._cache.get(resolver._cache_key(proxy)), None)


class _ExplodingProbe:
    def __call__(self, proxy, *, timeout, endpoints, need_timezone):
        raise AssertionError("launch path must never probe")


# --- launch options -------------------------------------------------------
# The locale/timezone half was only half of P0-4: the timezone used to be
# assigned onto the Playwright context, which has no such attribute, so a
# bridged launch silently kept the browser's default clock.

import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from sms_tool.registration_drivers.external_sessions import managed  # noqa: E402
from sms_tool.registration_drivers.external_sessions.managed import (  # noqa: E402
    CamoufoxBrowserSession,
)


@pytest.fixture
def fake_camoufox(monkeypatch):
    context = MagicMock()
    context.pages = [MagicMock()]
    manager = MagicMock()
    manager.__enter__.return_value = context
    factory = MagicMock(return_value=manager)
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", SimpleNamespace(Camoufox=factory))
    monkeypatch.setitem(sys.modules, "browserforge.fingerprints", SimpleNamespace(Screen=MagicMock()))
    monkeypatch.setattr(managed, "apply_playwright_stealth", lambda *a, **k: {})
    # Force the bridged branch without starting a real local SOCKS5 server.
    monkeypatch.setattr("sms_tool.proxy_bridge.needs_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "sms_tool.proxy_bridge.proxy_for_browser",
        lambda *a, **k: ("socks5h://127.0.0.1:54321", lambda: None),
    )
    return factory


def _launch(factory, **session_kwargs):
    session = CamoufoxBrowserSession(**session_kwargs)
    session.__enter__()
    try:
        return factory.call_args.kwargs
    finally:
        session.close()


def test_bridged_launch_passes_timezone_as_a_launch_option(fake_camoufox):
    kwargs = _launch(
        fake_camoufox,
        config={},
        proxy="http://user:pw@egress.example:8080",
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
    )
    assert kwargs["timezone_id"] == "America/Sao_Paulo"
    assert kwargs["locale"] == "pt-BR"


def test_webrtc_is_blocked_by_default(fake_camoufox):
    # An unspoofed ICE candidate leaks the real egress IP; the geo probe only
    # looks at the HTTP exit, so it would never notice.
    kwargs = _launch(fake_camoufox, config={}, locale="en-US", timezone_id="UTC")
    assert kwargs["block_webrtc"] is True


def test_webrtc_blocking_can_be_disabled(fake_camoufox):
    kwargs = _launch(
        fake_camoufox,
        config={"registration": {"drivers": {"camoufox": {"block_webrtc": False}}}},
        locale="en-US",
        timezone_id="UTC",
    )
    assert "block_webrtc" not in kwargs


if __name__ == "__main__":
    unittest.main()
