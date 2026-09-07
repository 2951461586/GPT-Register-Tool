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
    assert decisions.aligned_locale_timezone(
        {"navigator_language": "de-DE"}, "en-US", "America/New_York"
    ) == ("de-DE", "America/New_York")
    assert decisions.aligned_locale_timezone(
        {"timezone_iana": "Europe/Berlin"}, "en-US", "America/New_York"
    ) == ("en-US", "Europe/Berlin")


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
