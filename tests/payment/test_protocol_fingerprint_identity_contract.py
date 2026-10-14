"""Contract: the protocol fingerprint pool and the Sentinel identity cannot drift.

P2-4.  Two identity generators feed one registration:

* ``FingerprintPool`` picks a :class:`ProtocolEnvironmentProfile`
  (impersonate / UA / ``sec_ch_ua_platform`` / geo), and
* ``auth_headers.sentinel_fingerprint()`` re-derives the platform, hardware,
  touch points and clock that the Sentinel JS environment reports.

They are seeded differently (pool name vs ``device_id``), so nothing but a test
stops them from disagreeing about the OS.  P1-1 made the runner derive
``userAgentDataPlatform``/``secChUaPlatform`` from the profile's
``sec_ch_ua_platform``; these tests pin the whole chain so a later change to
either generator fails loudly instead of shipping a macOS UA on a Windows
platform.

Also pins the P2-4 refactor: the pool now reads the curated markets through
``geo.profiles.geo_profile`` (public, single source) instead of the private
``auth_headers._GEO_PROFILES``, and an **uncurated** country must keep its
*measured* clock rather than silently falling back to UTC.
"""

from __future__ import annotations

import pytest

from sms_tool import auth_headers as ah
from sms_tool.fingerprint_pool import FingerprintPool
from sms_tool.geo import ProxyGeo
from sms_tool.geo.profiles import MARKET_PROFILES, geo_profile
from sms_tool.sentinel.runner import _platform_for_family


@pytest.fixture(autouse=True)
def _isolate_auth_fingerprint():
    """Snapshot/restore the auth fingerprint thread-local.

    These tests call ``set_auth_fingerprint``/``sentinel_fingerprint``, which
    mutate process-wide thread-local identity state.  Without restoring it the
    last profile leaks into unrelated tests (e.g. the OTP strategy test, which
    asserts the shared browser impersonation).
    """
    local = ah._AUTH_FINGERPRINT_LOCAL
    saved = dict(local.__dict__)
    try:
        yield
    finally:
        local.__dict__.clear()
        local.__dict__.update(saved)


def _family(name: str) -> str:
    token = name.lower()
    for family in ("firefox", "chrome", "safari", "edge"):
        if token.startswith(family):
            return family
    return "chrome"


def test_geo_profile_projection_matches_the_legacy_table():
    """The refactor must be byte-for-byte equivalent to ``auth_headers._GEO_PROFILES``."""
    for country in MARKET_PROFILES:
        assert geo_profile(country) == ah._GEO_PROFILES[country]
    # Uncurated markets are ``None`` -- not the UTC default.  Returning the
    # default here would hand a Brazilian exit a UTC clock.
    assert geo_profile("BR") is None
    assert geo_profile("ZZ") is None
    assert geo_profile("jp") == ah._GEO_PROFILES["JP"]


def test_every_pool_profile_platform_matches_the_canonical_auth_table():
    pool = FingerprintPool(mode="round_robin")
    assert pool.size > 0
    for name in pool.names:
        selected = pool.select(name)
        assert selected is not None, name
        canonical = ah.AUTH_FINGERPRINT_PROFILES[name]
        assert selected.impersonate == canonical["impersonate"]
        assert selected.sec_ch_ua_platform == canonical["sec_ch_ua_platform"]


def test_sentinel_platform_agrees_with_the_selected_pool_profile():
    """Pool profile -> auth fingerprint -> Sentinel env -> runner platform, all one value."""
    pool = FingerprintPool(mode="round_robin")
    for name in pool.names:
        ah.set_auth_fingerprint(name)
        env = ah.sentinel_fingerprint()
        canonical_platform = str(ah.AUTH_FINGERPRINT_PROFILES[name]["sec_ch_ua_platform"]).strip().strip('"')
        # The runner's platform for this profile is what the JS environment claims.
        assert _platform_for_family(_family(name), env) == canonical_platform, name
        # navigator.platform must agree with the UA's OS too.
        assert env["navigator_platform"] == ah.hardware_profile_for_family(name)["navigator_platform"], name


def test_uncurated_country_keeps_the_measured_timezone(monkeypatch):
    """The P2-4 behavior guard: an uncurated exit gets its *measured* clock."""
    pool = FingerprintPool(mode="round_robin")
    profile = pool.select(pool.names[0])
    assert profile is not None

    monkeypatch.setattr("sms_tool.paypal_proxy.infer_proxy_country", lambda _proxy: "BR")
    monkeypatch.setattr(
        pool,
        "_resolve_geo",
        lambda _proxy, _hint, need_timezone: ProxyGeo(country="BR", timezone="America/Sao_Paulo", source="probe"),
    )
    aligned = pool._with_geo(profile, "http://u:p@proxy.example:8080")
    assert aligned.country == "BR"
    assert aligned.timezone == "America/Sao_Paulo", "measured clock must survive; UTC fallback is the bug"


def test_curated_country_uses_the_curated_clock(monkeypatch):
    pool = FingerprintPool(mode="round_robin")
    profile = pool.select(pool.names[0])
    assert profile is not None

    monkeypatch.setattr("sms_tool.paypal_proxy.infer_proxy_country", lambda _proxy: "JP")
    aligned = pool._with_geo(profile, "http://u:p@proxy.example:8080")
    assert aligned.country == "JP"
    assert aligned.timezone == "Asia/Tokyo"
    assert aligned.lang == "ja-JP"


@pytest.mark.parametrize("name", ["safari18_0", "safari18_0_ios", "firefox144", "chrome146"])
def test_platform_derivation_never_pairs_a_macos_ua_with_windows(name):
    ah.set_auth_fingerprint(name)
    env = ah.sentinel_fingerprint()
    platform = _platform_for_family(_family(name), env)
    ua = str(env["user_agent"])
    if "Macintosh" in ua and "iPhone" not in ua:
        assert platform == "macOS"
    elif "iPhone" in ua:
        assert platform == "iOS"
    else:
        assert platform == "Windows"
