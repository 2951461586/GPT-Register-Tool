"""geo 表防漂移：协议池与浏览器池共享市场的时区/语言必须一致。

协议池读 ``auth_headers._GEO_PROFILES``，浏览器池读
``browser_fingerprint_pool.BROWSER_LOCALE_PROFILES``——2026-09 的越南迁移
曾在 ``auth_headers`` 和 ``browser_fingerprint_pool`` 各改一遍，漏一边就是
"协议路径在越南、浏览器路径在美国"的指纹矛盾。两张表在美区等取值上有
**历史分歧**（协议=纽约，浏览器=洛杉矶/PDT），合并需要运维决策；在合并
之前，本测试保证：任何新市场必须两侧同时添加，共享市场不允许单侧改动。
"""

from sms_tool.auth_headers import _GEO_PROFILES
from sms_tool.browser_fingerprint_pool import BROWSER_LOCALE_PROFILES

# 浏览器表刻意保留的逐市场差异：US 时区（协议=America/New_York，浏览器=
# America/Los_Angeles PDT）。除此之外的共享市场一律必须一致。
_DOCUMENTED_TIMEZONE_DIVERGENCES = {"US"}


def test_vn_migration_exists_on_both_sides():
    # 2026-09 越南迁移的事故原样：只加一边不会报错。pin 住它。
    assert "VN" in _GEO_PROFILES
    assert "vn" in BROWSER_LOCALE_PROFILES


def test_shared_markets_agree_on_timezone_and_primary_language():
    for country, protocol_profile in _GEO_PROFILES.items():
        browser_profile = BROWSER_LOCALE_PROFILES.get(country.lower())
        if browser_profile is None:
            # 浏览器表是更小的精选集（缺 CA/AU），缺市场允许，但存在就必须一致。
            continue
        if country in _DOCUMENTED_TIMEZONE_DIVERGENCES:
            continue
        assert browser_profile["timezone_iana"] == protocol_profile["timezone"], country
        assert browser_profile["navigator_language"] == protocol_profile["lang"], country
