"""Canonical per-market geo profiles (单一事实源).

2026-09-12 扫描发现协议路径（``auth_headers._GEO_PROFILES``）与浏览器路径
（``browser_fingerprint_pool.BROWSER_LOCALE_PROFILES``）各自维护一张市场表，
越南迁移时要改两处，且 US 时区两表分歧（协议=New York，浏览器=Los Angeles）。
运维决策（2026-09-13）：**统一为 America/New_York**。

两张表现在都从本模块的 ``MARKET_PROFILES`` 派生：

* 协议渲染器 → ``auth_headers._GEO_PROFILES``（timezone/lang/lang_full）
* 浏览器渲染器 → ``browser_fingerprint_pool.BROWSER_LOCALE_PROFILES``
  （navigator_language/accept_language/timezone_iana/…）

新增市场只改这里一处，两条 lane 同时生效。时区偏移与 Windows 时区名不进
本表：偏移在运行时按 IANA 名重算（DTP 安全，见 browser_fingerprint_pool
P1-4 注释），Windows 名在浏览器渲染器的 ``TIMEZONE_NAME_BY_IANA`` 里。
"""

from __future__ import annotations

# 国家代码（ISO 3166-1 alpha-2，大写）→ 出口市场档案。
MARKET_PROFILES: dict[str, dict[str, str]] = {
    "US": {"timezone_iana": "America/New_York", "lang": "en-US", "lang_full": "en-US,en;q=0.9"},
    "CA": {"timezone_iana": "America/Toronto", "lang": "en-CA", "lang_full": "en-CA,en-US;q=0.9,en;q=0.8"},
    "GB": {"timezone_iana": "Europe/London", "lang": "en-GB", "lang_full": "en-GB,en;q=0.9,en-US;q=0.8"},
    "DE": {"timezone_iana": "Europe/Berlin", "lang": "de-DE", "lang_full": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"},
    "FR": {"timezone_iana": "Europe/Paris", "lang": "fr-FR", "lang_full": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"},
    "NL": {"timezone_iana": "Europe/Amsterdam", "lang": "nl-NL", "lang_full": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7"},
    "JP": {"timezone_iana": "Asia/Tokyo", "lang": "ja-JP", "lang_full": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"},
    "SG": {"timezone_iana": "Asia/Singapore", "lang": "en-SG", "lang_full": "en-SG,en-US;q=0.9,en;q=0.8"},
    "AU": {"timezone_iana": "Australia/Sydney", "lang": "en-AU", "lang_full": "en-AU,en-US;q=0.9,en;q=0.8"},
    "VN": {"timezone_iana": "Asia/Ho_Chi_Minh", "lang": "vi-VN", "lang_full": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"},
    "CN": {"timezone_iana": "Asia/Shanghai", "lang": "zh-CN", "lang_full": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"},
    "HK": {"timezone_iana": "Asia/Hong_Kong", "lang": "zh-HK", "lang_full": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6"},
    "TW": {"timezone_iana": "Asia/Taipei", "lang": "zh-TW", "lang_full": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"},
}

# 旧记录里出现过 "account_deatived" 这类历史拼写，市场代码同理可能出现小写
# 或历史别名；规范查找一律走大写。没有别的别名表——新市场直接加一行。
DEFAULT_TZ_FALLBACK = {"timezone_iana": "UTC", "lang": "en-US", "lang_full": "en-US,en;q=0.9"}


def market_profile(country: str) -> dict[str, str]:
    """Return the canonical profile for ``country`` (case-insensitive)."""
    return MARKET_PROFILES.get(str(country or "").strip().upper(), dict(DEFAULT_TZ_FALLBACK))


__all__ = ["MARKET_PROFILES", "DEFAULT_TZ_FALLBACK", "market_profile"]
