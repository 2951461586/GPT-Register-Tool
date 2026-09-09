"""Pure-function tests for the decisions lifted out of run_browser_registration.

These used to be reachable only by driving a full fake browser session. The
end-to-end tests in test_external_registration_drivers.py still cover the
assembly; this file covers the branches those runs never hit.
"""

import pytest

from sms_tool.registration_drivers.browser_flow import decisions


@pytest.mark.parametrize(
    "metadata, expected",
    [
        (None, 1),
        ({}, 1),
        ({"attempt": None}, 1),
        ({"attempt": ""}, 1),
        ({"attempt": 0}, 1),
        ({"attempt": -3}, 1),
        ({"attempt": 1}, 1),
        ({"attempt": 4}, 4),
        ({"attempt": "7"}, 7),
        ({"attempt": 2.9}, 2),
        ({"attempt": "abc"}, 1),
        ({"attempt": [1]}, 1),
    ],
)
def test_attempt_number_never_drops_below_one(metadata, expected):
    assert decisions.attempt_number(metadata) == expected


@pytest.mark.parametrize(
    "account_key, attempt, expected",
    [
        ("a@example.com", 1, "a@example.com"),
        ("a@example.com", 2, "a@example.com__retry2"),
        ("a@example.com", 17, "a@example.com__retry17"),
    ],
)
def test_browser_profile_key_isolates_retries(account_key, attempt, expected):
    assert decisions.browser_profile_key(account_key, attempt) == expected


def test_playwright_viewport_only_applies_to_playwright():
    profile = {"screen_width": 1920, "screen_height": 1080}
    assert decisions.playwright_viewport(profile, "playwright") == (1920, 1080)
    for driver in ("camoufox", "cloak", "roxy", "protocol"):
        assert decisions.playwright_viewport(profile, driver) is None


@pytest.mark.parametrize(
    "profile, expected",
    [
        ({}, (1440, 900)),
        ({"screen_width": 0, "screen_height": 0}, (1440, 900)),
        ({"screen_width": None, "screen_height": None}, (1440, 900)),
        ({"screen_width": "1280", "screen_height": "720"}, (1280, 720)),
        ({"screen_width": 800}, (800, 900)),
        ({"screen_height": 600}, (1440, 600)),
    ],
)
def test_playwright_viewport_falls_back_per_axis(profile, expected):
    assert decisions.playwright_viewport(profile, "playwright") == expected


def test_aligned_locale_timezone_keeps_configured_defaults_when_pool_is_silent():
    assert decisions.aligned_locale_timezone({}, "en-US", "America/New_York") == (
        "en-US",
        "America/New_York",
    )
    assert decisions.aligned_locale_timezone(
        {"navigator_language": "", "timezone_iana": ""}, "en-US", "America/New_York"
    ) == ("en-US", "America/New_York")


def test_aligned_locale_timezone_overrides_only_supplied_values():
    # ``geo`` present == exit-geo detection actually measured something, which
    # is the precondition for overriding the caller's values.
    assert decisions.aligned_locale_timezone(
        {"geo": {"country": "DE"}, "navigator_language": "de-DE"},
        "en-US",
        "America/New_York",
    ) == ("de-DE", "America/New_York")
    assert decisions.aligned_locale_timezone(
        {"geo": {"country": "DE"}, "timezone_iana": "Europe/Berlin"},
        "en-US",
        "America/New_York",
    ) == ("en-US", "Europe/Berlin")


def test_aligned_locale_timezone_does_not_clobber_when_geo_is_unknown():
    """A failed exit-geo probe must mean "unknown", not "it is the US".

    The pool falls back to its default (US) profile when detection fails.
    Blindly applying that used to replace a correct caller-supplied locale with
    en-US / America/New_York -- manufacturing the very country/environment
    mismatch the alignment exists to prevent.
    """
    us_default_profile = {"navigator_language": "en-US", "timezone_iana": "America/New_York"}
    assert decisions.aligned_locale_timezone(
        us_default_profile, "pt-BR", "America/Sao_Paulo"
    ) == ("pt-BR", "America/Sao_Paulo")
    # Same profile, but the egress really was measured as the US -> override.
    assert decisions.aligned_locale_timezone(
        dict(us_default_profile, geo={"country": "US"}), "pt-BR", "America/Sao_Paulo"
    ) == ("en-US", "America/New_York")


def test_geo_affinity_country():
    assert decisions.geo_affinity_country({"country": "us"}, True) == "US"
    assert decisions.geo_affinity_country({"country": " gb "}, True) == "GB"
    assert decisions.geo_affinity_country({"country": "us"}, False) == ""
    assert decisions.geo_affinity_country({}, True) == ""
    assert decisions.geo_affinity_country(None, True) == ""
    assert decisions.geo_affinity_country({"country": None}, True) == ""


@pytest.mark.parametrize(
    "success, probe_pending, expected",
    [
        (True, False, ("active", "at_http_200")),
        (False, False, ("failed", "")),
        (True, True, ("at_probe_pending", "at_http_200")),
        (False, True, ("at_probe_pending", "at_probe_pending")),
    ],
)
def test_registration_state_and_basis_stay_consistent(success, probe_pending, expected):
    assert decisions.registration_state_and_basis(success, probe_pending) == expected


@pytest.mark.parametrize(
    "chat_base, page_url, expected",
    [
        ("https://chatgpt.com/", "https://chatgpt.com/", False),
        ("https://chatgpt.com/", "https://chatgpt.com/c/abc", False),
        ("https://chatgpt.com/", "https://auth.openai.com/login", True),
        ("https://chatgpt.com/", "https://CHATGPT.COM/", False),
        ("https://chatgpt.com", "", True),
        ("", "https://auth.openai.com/login", False),
        ("not a url", "https://auth.openai.com/login", False),
    ],
)
def test_needs_chat_base_navigation(chat_base, page_url, expected):
    assert decisions.needs_chat_base_navigation(chat_base, page_url) is expected


# ─────────────────────────── P1-3: pooled screen size ─────────────────────────
#
# The 7 hardware profiles in BROWSER_PROFILE_POOL used to reach Playwright only.
# Camoufox was pinned to a hardcoded 1280x900; provider-owned drivers (roxy/
# cloak) can never take it. These lock that split down.


@pytest.mark.parametrize(
    "driver, expected",
    [
        ("playwright", (1680, 1050)),
        ("camoufox", (1680, 1050)),
        # provider-owned: the anti-detect profile owns screen/UA/platform.
        ("roxy", None),
        ("cloak", None),
        # driver_name is already normalized upstream; no silent case folding.
        ("PLAYWRIGHT", None),
        ("", None),
        (None, None),
    ],
)
def test_browser_screen_size_reaches_only_screen_managed_drivers(driver, expected):
    profile = {"screen_width": 1680, "screen_height": 1050}
    assert decisions.browser_screen_size(profile, driver) == expected


def test_browser_screen_size_falls_back_when_profile_has_no_screen():
    assert decisions.browser_screen_size({}, "playwright") == (
        decisions.DEFAULT_VIEWPORT_WIDTH,
        decisions.DEFAULT_VIEWPORT_HEIGHT,
    )


def test_browser_screen_size_falls_back_per_axis():
    # A profile that only carries one axis must not collapse to (0, 0).
    assert decisions.browser_screen_size({"screen_height": 800}, "camoufox") == (1440, 800)


def test_browser_screen_size_coerces_string_dimensions():
    profile = {"screen_width": "1512", "screen_height": "982"}
    assert decisions.browser_screen_size(profile, "camoufox") == (1512, 982)


def test_screen_and_provider_driver_sets_are_disjoint_and_complete():
    # Every browser driver must be exactly one of: screen-managed or provider-managed.
    assert not (decisions.SCREEN_MANAGED_DRIVERS & decisions.PROVIDER_MANAGED_DRIVERS)
    assert decisions.SCREEN_MANAGED_DRIVERS | decisions.PROVIDER_MANAGED_DRIVERS == {
        "playwright", "camoufox", "roxy", "cloak",
    }


@pytest.mark.parametrize(
    "driver, expected",
    [
        ("playwright", (1440, 900)),
        ("camoufox", None),
        ("roxy", None),
        ("cloak", None),
    ],
)
def test_playwright_viewport_stays_playwright_only(driver, expected):
    # Kept for its existing call sites; it must stay a strict subset of
    # browser_screen_size so the two can never drift apart.
    assert decisions.playwright_viewport({}, driver) == expected


def test_playwright_viewport_delegates_to_browser_screen_size():
    profile = {"screen_width": 2056, "screen_height": 1329}
    assert decisions.playwright_viewport(profile, "playwright") == (
        decisions.browser_screen_size(profile, "playwright")
    )


# ─────────────────── P2-3: exit-country probe strictness ───────────────────


@pytest.mark.parametrize(
    "driver, expected",
    [
        ("roxy", "blocking"),
        ("cloak", "blocking"),
        ("camoufox", "diagnostic"),
        ("playwright", "diagnostic"),
    ],
)
def test_proxy_country_check_mode_defaults(driver, expected):
    assert decisions.proxy_country_check_mode(driver) == expected


def test_proxy_country_check_mode_defaults_with_an_empty_config():
    assert decisions.proxy_country_check_mode("roxy", {}) == "blocking"
    assert decisions.proxy_country_check_mode("camoufox", {}) == "diagnostic"


@pytest.mark.parametrize(
    "mode", ["blocking", "diagnostic", "off"]
)
def test_proxy_country_check_mode_config_overrides_both_ways(mode):
    cfg = {"registration": {"browser_proxy_country_check": mode}}
    assert decisions.proxy_country_check_mode("roxy", cfg) == mode
    assert decisions.proxy_country_check_mode("playwright", cfg) == mode


def test_proxy_country_check_mode_ignores_junk_config():
    # An unrecognised value must fall back to the per-driver default rather
    # than silently disabling the check.
    cfg = {"registration": {"browser_proxy_country_check": "sometimes"}}
    assert decisions.proxy_country_check_mode("roxy", cfg) == "blocking"
    assert decisions.proxy_country_check_mode("camoufox", cfg) == "diagnostic"


def test_proxy_country_check_mode_tolerates_a_broken_registration_section():
    assert decisions.proxy_country_check_mode("roxy", {"registration": "nope"}) == "blocking"
    assert decisions.proxy_country_check_mode("roxy", None) == "blocking"


def test_proxy_country_check_modes_are_the_only_accepted_values():
    assert decisions.PROXY_COUNTRY_CHECK_MODES == {"blocking", "diagnostic", "off"}
