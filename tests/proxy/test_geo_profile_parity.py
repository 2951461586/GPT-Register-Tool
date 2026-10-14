"""geo 表合一：协议/浏览器两路档案必须与规范表 (geo/profiles.MARKET_PROFILES) 一致。

2026-09-12 扫描发现两张表各自维护、越南迁移时改两处、US 时区两表分歧
（协议=New York，浏览器=Los Angeles）。运维决策：统一为 New York。两张表现
在都是 ``MARKET_PROFILES`` 的渲染视图——新增市场只改规范表一处。
"""

from sms_tool.auth_headers import _GEO_PROFILES
from sms_tool.browser_fingerprint_pool import (
    BROWSER_LOCALE_PROFILES,
    COUNTRY_LOCALE_PROFILE_MAP,
    TIMEZONE_NAME_BY_IANA,
)
from sms_tool.geo.profiles import MARKET_PROFILES


def test_both_tables_derive_from_the_canonical_table():
    for country, profile in MARKET_PROFILES.items():
        protocol = _GEO_PROFILES.get(country)
        assert protocol is not None, country
        assert protocol["timezone"] == profile["timezone_iana"], country
        assert protocol["lang"] == profile["lang"], country
        assert protocol["lang_full"] == profile["lang_full"], country

        browser = BROWSER_LOCALE_PROFILES.get(country.lower())
        assert browser is not None, country
        assert browser["timezone_iana"] == profile["timezone_iana"], country
        assert browser["navigator_language"] == profile["lang"], country
        assert browser["accept_language"] == profile["lang_full"], country


def test_us_is_new_york_on_both_lanes_per_operator_decision():
    assert _GEO_PROFILES["US"]["timezone"] == "America/New_York"
    assert BROWSER_LOCALE_PROFILES["us"]["timezone_iana"] == "America/New_York"


def test_every_market_has_a_locale_profile_key():
    for country in MARKET_PROFILES:
        assert COUNTRY_LOCALE_PROFILE_MAP.get(country) == country.lower(), country


def test_timezone_names_cover_all_canonical_zones():
    for profile in MARKET_PROFILES.values():
        assert TIMEZONE_NAME_BY_IANA.get(profile["timezone_iana"]), profile["timezone_iana"]
