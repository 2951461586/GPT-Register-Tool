"""Canonical per-market geo profiles (单一事实源).

2026-09-12 扫描发现协议路径（``auth_headers._GEO_PROFILES``）与浏览器路径
（``browser_fingerprint_pool.BROWSER_LOCALE_PROFILES``）各自维护一张市场表，
越南迁移时要改两处，且 US 时区两表分歧（协议=New York，浏览器=Los Angeles）。
运维决策（2026-09-13）：**统一为 America/New_York**。

两张表现在都从本模块的 ``MARKET_PROFILES`` 派生：

* 协议渲染器 → ``auth_headers._GEO_PROFILES``（timezone/lang/lang_full）
* 浏览器渲染器 → ``browser_fingerprint_pool.BROWSER_LOCALE_PROFILES``
  （navigator_language/accept_language/timezone_iana/…）

新增市场要改**两处**（协议 lane 只需本表；浏览器 lane 还要补硬编码表）：

1. 本模块 ``MARKET_PROFILES`` —— ``_GEO_PROFILES`` 与
   ``BROWSER_LOCALE_PROFILES`` 都从这里派生，加一行两条 lane 同时生效。
2. ``browser_fingerprint_pool`` 里**非派生的** ``COUNTRY_LOCALE_PROFILE_MAP``
   （直接由本表生成，2026-10-26 起）与 ``TIMEZONE_NAME_BY_IANA``
   （由本模块 ``WINDOWS_TIMEZONE_BY_IANA`` 派生）。
   ``tests/proxy/test_geo_profile_parity.py`` 会把两处漏项都判红。

时区偏移与 Windows 时区名不进 ``MARKET_PROFILES``：偏移在运行时按 IANA 名
重算（DST 安全，见 browser_fingerprint_pool P1-4 注释），Windows 名在本模块
``WINDOWS_TIMEZONE_BY_IANA`` 里（2026-10-26 从 browser_fingerprint_pool 上移，
因为 50 个市场需要的 IANA 集合已经让那张表不再是"浏览器路径的私事"）。

## 2026-10-26：市场表 15 → 50（对齐参考项目）

参考项目 ``fingerprint.py:_COUNTRY_PROFILES`` 覆盖 50 个国家、每国带
**语言优先级列表**和**多时区权重**。本项目原只有 15 个市场，且非策展国家走
``set_fingerprint_geo`` 时只覆盖了时区、语言保留 ``en-US`` —— 于是 ID/TH/TR/
KR/BR/MX 出口会拿到"正确时区 + 美国英语"的指纹。支付 lane
（``payment_methods.json``）早已覆盖 ID/TH/TR/KR/BR/ES，注册 lane 却只有
15 国，两条 lane 的国家集合对不上。

三处扩展，全部向后兼容：

* ``timezones`` —— ``(IANA, weight)`` 权重表；``timezone_iana`` 仍是
  **首选时区**（既有 15 国的值一字未改，parity 测试与各国断言继续成立）。
* ``languages`` —— 语言优先级列表；``lang`` 是首项，``lang_full`` 由
  ``accept_language()`` 派生（既有 15 国的 ``lang_full`` 逐字节不变）。
* ``WINDOWS_TIMEZONE_BY_IANA`` —— 新增时区的 Windows 名，供浏览器
  ``Intl.DateTimeFormat().resolvedOptions().timeZoneName`` 使用。

``market_timezone(country, rng)`` 是给"只有国家提示、没有实测时区"的调用方
抽取多时区用的；默认路径仍读 ``timezone_iana``，所以行为不变。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

#: ISO 3166-1 alpha-2（大写）→ 出口市场档案。
#:
#: 每个条目必须包含：
#:   ``timezone_iana``  首选/主时区（确定性，parity 测试按它比对两条 lane）
#:   ``timezones``       ``(IANA, weight)`` 权重表，第一项必须等于 ``timezone_iana``
#:   ``languages``       语言优先级列表，第一项必须等于 ``lang``
#: ``lang`` / ``lang_full`` 由 ``_finalize()`` 在导入时写入，避免手写 q 值漂移。
MARKET_PROFILES: dict[str, dict[str, Any]] = {
    # ── 北美 ──────────────────────────────────────────────────────────────
    "US": {
        "timezone_iana": "America/New_York",
        "timezones": (
            ("America/New_York", 0.4),
            ("America/Los_Angeles", 0.3),
            ("America/Chicago", 0.2),
            ("America/Denver", 0.1),
        ),
        "languages": ("en-US", "en"),
    },
    "CA": {
        "timezone_iana": "America/Toronto",
        "timezones": (
            ("America/Toronto", 0.6),
            ("America/Vancouver", 0.3),
            ("America/Edmonton", 0.1),
        ),
        "languages": ("en-CA", "en-US", "en"),
    },
    "MX": {
        "timezone_iana": "America/Mexico_City",
        "timezones": (
            ("America/Mexico_City", 0.8),
            ("America/Tijuana", 0.2),
        ),
        "languages": ("es-MX", "es", "en-US", "en"),
    },
    # ── 南美 ──────────────────────────────────────────────────────────────
    "BR": {
        "timezone_iana": "America/Sao_Paulo",
        "timezones": (
            ("America/Sao_Paulo", 0.7),
            ("America/Manaus", 0.2),
            ("America/Fortaleza", 0.1),
        ),
        "languages": ("pt-BR", "pt", "en-US", "en", "es"),
    },
    "AR": {
        "timezone_iana": "America/Argentina/Buenos_Aires",
        "timezones": (("America/Argentina/Buenos_Aires", 1.0),),
        "languages": ("es-AR", "es", "en-US", "en"),
    },
    "CL": {
        "timezone_iana": "America/Santiago",
        "timezones": (("America/Santiago", 1.0),),
        "languages": ("es-CL", "es", "en-US", "en"),
    },
    "CO": {
        "timezone_iana": "America/Bogota",
        "timezones": (("America/Bogota", 1.0),),
        "languages": ("es-CO", "es", "en-US", "en"),
    },
    # ── 欧洲 ──────────────────────────────────────────────────────────────
    "GB": {
        "timezone_iana": "Europe/London",
        "timezones": (("Europe/London", 1.0),),
        "languages": ("en-GB", "en", "en-US"),
    },
    "DE": {
        "timezone_iana": "Europe/Berlin",
        "timezones": (("Europe/Berlin", 1.0),),
        "languages": ("de-DE", "de", "en-US", "en"),
    },
    "FR": {
        "timezone_iana": "Europe/Paris",
        "timezones": (("Europe/Paris", 1.0),),
        "languages": ("fr-FR", "fr", "en-US", "en"),
    },
    "IT": {
        "timezone_iana": "Europe/Rome",
        "timezones": (("Europe/Rome", 1.0),),
        "languages": ("it-IT", "it", "en-US", "en", "fr"),
    },
    "ES": {
        "timezone_iana": "Europe/Madrid",
        "timezones": (("Europe/Madrid", 1.0),),
        "languages": ("es-ES", "es", "en-US", "en", "fr"),
    },
    "NL": {
        "timezone_iana": "Europe/Amsterdam",
        "timezones": (("Europe/Amsterdam", 1.0),),
        "languages": ("nl-NL", "nl", "en-US", "en"),
    },
    "BE": {
        "timezone_iana": "Europe/Brussels",
        "timezones": (("Europe/Brussels", 1.0),),
        "languages": ("nl-BE", "fr-BE", "nl", "fr", "en-US", "en"),
    },
    "CH": {
        "timezone_iana": "Europe/Zurich",
        "timezones": (("Europe/Zurich", 1.0),),
        "languages": ("de-CH", "fr-CH", "de", "fr", "it", "en-US", "en"),
    },
    "SE": {
        "timezone_iana": "Europe/Stockholm",
        "timezones": (("Europe/Stockholm", 1.0),),
        "languages": ("sv-SE", "sv", "en-US", "en"),
    },
    "NO": {
        "timezone_iana": "Europe/Oslo",
        "timezones": (("Europe/Oslo", 1.0),),
        "languages": ("nb-NO", "nb", "en-US", "en"),
    },
    "DK": {
        "timezone_iana": "Europe/Copenhagen",
        "timezones": (("Europe/Copenhagen", 1.0),),
        "languages": ("da-DK", "da", "en-US", "en"),
    },
    "FI": {
        "timezone_iana": "Europe/Helsinki",
        "timezones": (("Europe/Helsinki", 1.0),),
        "languages": ("fi-FI", "fi", "sv", "en-US", "en"),
    },
    "PL": {
        "timezone_iana": "Europe/Warsaw",
        "timezones": (("Europe/Warsaw", 1.0),),
        "languages": ("pl-PL", "pl", "en-US", "en"),
    },
    "RU": {
        "timezone_iana": "Europe/Moscow",
        "timezones": (
            ("Europe/Moscow", 0.7),
            ("Asia/Yekaterinburg", 0.15),
            ("Asia/Novosibirsk", 0.15),
        ),
        "languages": ("ru-RU", "ru", "en-US", "en"),
    },
    "UA": {
        "timezone_iana": "Europe/Kyiv",
        "timezones": (("Europe/Kyiv", 1.0),),
        "languages": ("uk-UA", "uk", "ru", "en-US", "en"),
    },
    "CZ": {
        "timezone_iana": "Europe/Prague",
        "timezones": (("Europe/Prague", 1.0),),
        "languages": ("cs-CZ", "cs", "en-US", "en"),
    },
    "AT": {
        "timezone_iana": "Europe/Vienna",
        "timezones": (("Europe/Vienna", 1.0),),
        "languages": ("de-AT", "de", "en-US", "en"),
    },
    "GR": {
        "timezone_iana": "Europe/Athens",
        "timezones": (("Europe/Athens", 1.0),),
        "languages": ("el-GR", "el", "en-US", "en"),
    },
    "PT": {
        "timezone_iana": "Europe/Lisbon",
        "timezones": (("Europe/Lisbon", 1.0),),
        "languages": ("pt-PT", "pt", "en-US", "en"),
    },
    # ── 亚洲 ──────────────────────────────────────────────────────────────
    "JP": {
        "timezone_iana": "Asia/Tokyo",
        "timezones": (("Asia/Tokyo", 1.0),),
        "languages": ("ja-JP", "ja", "en-US", "en"),
    },
    "KR": {
        "timezone_iana": "Asia/Seoul",
        "timezones": (("Asia/Seoul", 1.0),),
        "languages": ("ko-KR", "ko", "en-US", "en", "ja"),
    },
    "CN": {
        "timezone_iana": "Asia/Shanghai",
        "timezones": (("Asia/Shanghai", 1.0),),
        "languages": ("zh-CN", "zh", "en-US", "en"),
    },
    "HK": {
        "timezone_iana": "Asia/Hong_Kong",
        "timezones": (("Asia/Hong_Kong", 1.0),),
        "languages": ("zh-HK", "zh-TW", "zh", "en-US", "en"),
    },
    "TW": {
        "timezone_iana": "Asia/Taipei",
        "timezones": (("Asia/Taipei", 1.0),),
        "languages": ("zh-TW", "zh", "en-US", "en"),
    },
    "SG": {
        "timezone_iana": "Asia/Singapore",
        "timezones": (("Asia/Singapore", 1.0),),
        "languages": ("en-SG", "en-US", "en"),
    },
    "MY": {
        "timezone_iana": "Asia/Kuala_Lumpur",
        "timezones": (("Asia/Kuala_Lumpur", 1.0),),
        "languages": ("ms-MY", "ms", "zh-CN", "zh", "en-US", "en"),
    },
    "TH": {
        "timezone_iana": "Asia/Bangkok",
        "timezones": (("Asia/Bangkok", 1.0),),
        "languages": ("th-TH", "th", "en-US", "en"),
    },
    "VN": {
        "timezone_iana": "Asia/Ho_Chi_Minh",
        "timezones": (("Asia/Ho_Chi_Minh", 1.0),),
        "languages": ("vi-VN", "vi", "en-US", "en"),
    },
    "ID": {
        "timezone_iana": "Asia/Jakarta",
        "timezones": (("Asia/Jakarta", 1.0),),
        "languages": ("id-ID", "id", "en-US", "en"),
    },
    "PH": {
        "timezone_iana": "Asia/Manila",
        "timezones": (("Asia/Manila", 1.0),),
        "languages": ("en-PH", "en", "en-US"),
    },
    "IN": {
        "timezone_iana": "Asia/Kolkata",
        "timezones": (("Asia/Kolkata", 1.0),),
        # 备注（保留自 2026-09-18）：印度只有一个时区（Asia/Kolkata，
        # UTC+05:30，无夏令时），不存在"一国多时区"的歧义；``en-IN`` 是
        # Chrome 在印度出口上的常见首选语言。``Asia/Calcutta`` 是 IANA 历史
        # 别名，geo 库偶尔返回别名，故在 WINDOWS_TIMEZONE_BY_IANA 里两条都登记。
        "languages": ("en-IN", "en", "en-US"),
    },
    "PK": {
        "timezone_iana": "Asia/Karachi",
        "timezones": (("Asia/Karachi", 1.0),),
        "languages": ("en-US", "en", "ur-PK", "ur"),
    },
    "BD": {
        "timezone_iana": "Asia/Dhaka",
        "timezones": (("Asia/Dhaka", 1.0),),
        "languages": ("bn-BD", "bn", "en-US", "en"),
    },
    "IL": {
        "timezone_iana": "Asia/Jerusalem",
        "timezones": (("Asia/Jerusalem", 1.0),),
        "languages": ("he-IL", "he", "en-US", "en", "ar"),
    },
    "TR": {
        "timezone_iana": "Europe/Istanbul",
        "timezones": (("Europe/Istanbul", 1.0),),
        "languages": ("tr-TR", "tr", "en-US", "en"),
    },
    "SA": {
        "timezone_iana": "Asia/Riyadh",
        "timezones": (("Asia/Riyadh", 1.0),),
        "languages": ("ar-SA", "ar", "en-US", "en"),
    },
    "AE": {
        "timezone_iana": "Asia/Dubai",
        "timezones": (("Asia/Dubai", 1.0),),
        "languages": ("ar-AE", "ar", "en-US", "en"),
    },
    # ── 大洋洲 ────────────────────────────────────────────────────────────
    "AU": {
        "timezone_iana": "Australia/Sydney",
        "timezones": (
            ("Australia/Sydney", 0.6),
            ("Australia/Melbourne", 0.2),
            ("Australia/Brisbane", 0.2),
        ),
        "languages": ("en-AU", "en-US", "en"),
    },
    "NZ": {
        "timezone_iana": "Pacific/Auckland",
        "timezones": (("Pacific/Auckland", 1.0),),
        "languages": ("en-NZ", "en-US", "en", "mi"),
    },
    # ── 非洲 ──────────────────────────────────────────────────────────────
    "ZA": {
        "timezone_iana": "Africa/Johannesburg",
        "timezones": (("Africa/Johannesburg", 1.0),),
        "languages": ("en-ZA", "en-US", "en", "af"),
    },
    "EG": {
        "timezone_iana": "Africa/Cairo",
        "timezones": (("Africa/Cairo", 1.0),),
        "languages": ("ar-EG", "ar", "en-US", "en"),
    },
    "NG": {
        "timezone_iana": "Africa/Lagos",
        "timezones": (("Africa/Lagos", 1.0),),
        "languages": ("en-NG", "en-US", "en", "ha", "ig", "yo"),
    },
    "KE": {
        "timezone_iana": "Africa/Nairobi",
        "timezones": (("Africa/Nairobi", 1.0),),
        "languages": ("en-KE", "en-US", "en", "sw"),
    },
}

#: IANA 时区名 → Windows 时区 ID（CLDR ``windowsZones`` 的 territory="001"）。
#:
#: 浏览器通过 ``Intl.DateTimeFormat().resolvedOptions().timeZoneName`` 报这个
#: 名字；写一个不存在的 ID 就自相矛盾。2026-10-26 从
#: ``browser_fingerprint_pool.TIMEZONE_NAME_BY_IANA`` 上移到这里：50 个市场
#: 引用的 IANA 集合已经超出"浏览器路径私事"的范围，而 ``geo/profiles.py``
#: 是市场表的单一权威，时区名跟着市场表走才不会再漏。
#:
#: 注意 ``Asia/Manila`` 在 CLDR 里映射到 ``Singapore Standard Time``（Windows
#: 没有菲律宾专属时区）；``Asia/Bangkok`` / ``Asia/Saigon`` 的 long name 是
#: ``Indochina Time``——两者与 ``Asia/Ho_Chi_Minh`` 同值、同 UTC+7。
WINDOWS_TIMEZONE_BY_IANA: dict[str, str] = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Hong_Kong": "China Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "Asia/Seoul": "Korea Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Kuala_Lumpur": "Singapore Standard Time",
    "Asia/Bangkok": "Indochina Time",
    "Asia/Ho_Chi_Minh": "Indochina Time",
    "Asia/Saigon": "Indochina Time",
    "Asia/Manila": "Singapore Standard Time",
    "Asia/Jakarta": "SE Asia Standard Time",
    "Asia/Kolkata": "India Standard Time",
    "Asia/Calcutta": "India Standard Time",
    "Asia/Karachi": "Pakistan Standard Time",
    "Asia/Dhaka": "Bangladesh Standard Time",
    "Asia/Jerusalem": "Israel Standard Time",
    "Asia/Riyadh": "Arab Standard Time",
    "Asia/Dubai": "Arabian Standard Time",
    "Asia/Yekaterinburg": "Ekaterinburg Standard Time",
    "Asia/Novosibirsk": "N. Central Asia Standard Time",
    "Europe/Istanbul": "Turkey Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Rome": "W. Europe Standard Time",
    "Europe/Madrid": "Romance Standard Time",
    "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Brussels": "Romance Standard Time",
    "Europe/Zurich": "W. Europe Standard Time",
    "Europe/Stockholm": "W. Europe Standard Time",
    "Europe/Oslo": "W. Europe Standard Time",
    "Europe/Copenhagen": "W. Europe Standard Time",
    "Europe/Helsinki": "FLE Standard Time",
    "Europe/Warsaw": "Central European Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "Europe/Kyiv": "FLE Standard Time",
    "Europe/Prague": "Central Europe Standard Time",
    "Europe/Vienna": "W. Europe Standard Time",
    "Europe/Athens": "GTB Standard Time",
    "Europe/Lisbon": "GMT Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Toronto": "Eastern Standard Time",
    "America/Vancouver": "Pacific Standard Time",
    "America/Edmonton": "Mountain Standard Time",
    "America/Mexico_City": "Central Standard Time (Mexico)",
    "America/Tijuana": "Pacific Standard Time (Mexico)",
    "America/Sao_Paulo": "E. South America Standard Time",
    "America/Manaus": "SA Western Standard Time",
    "America/Fortaleza": "SA Eastern Standard Time",
    "America/Bahia": "Bahia Standard Time",
    "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "America/Santiago": "Pacific SA Standard Time",
    "America/Bogota": "SA Pacific Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "Australia/Melbourne": "AUS Eastern Standard Time",
    "Australia/Brisbane": "E. Australia Standard Time",
    "Pacific/Auckland": "New Zealand Standard Time",
    "Africa/Johannesburg": "South Africa Standard Time",
    "Africa/Cairo": "Egypt Standard Time",
    "Africa/Lagos": "W. Central Africa Standard Time",
    "Africa/Nairobi": "E. Africa Standard Time",
}

#: 旧记录里出现过 "account_deatived" 这类历史拼写，市场代码同理可能出现小写
#: 或历史别名；规范查找一律走大写。没有别的别名表——新市场直接加一行。
DEFAULT_TZ_FALLBACK = {"timezone_iana": "UTC", "lang": "en-US", "lang_full": "en-US,en;q=0.9"}


def _weight(value: Any) -> float:
    """Coerce a table weight, defaulting to 0.0 instead of raising.

    Weights are literals in this module, but a typo in a future market entry
    must not break import of a module that 60+ call sites depend on.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def accept_language(languages: Sequence[str]) -> str:
    """Render a language priority list as an ``Accept-Language`` header value.

    ``["de-DE", "de", "en-US", "en"]`` → ``"de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"``

    The q-value ladder starts at 0.9 for the second entry and drops 0.1 per
    position, which is exactly how the 15 hand-written strings this replaces
    were built. Keeping one derivation is what stops a 50-market table from
    accumulating 50 slightly different q ladders.
    """
    items = [str(item).strip() for item in languages if str(item).strip()]
    if not items:
        return DEFAULT_TZ_FALLBACK["lang_full"]
    rendered = [items[0]]
    for index, language in enumerate(items[1:], start=1):
        quality = max(0.1, 1.0 - 0.1 * index)
        rendered.append(f"{language};q={quality:.1f}")
    return ",".join(rendered)


def _finalize() -> None:
    """Fill ``lang`` / ``lang_full`` from ``languages`` and validate the shape."""
    for country, profile in MARKET_PROFILES.items():
        languages = tuple(str(item) for item in profile["languages"])
        timezones = tuple(profile["timezones"])
        if not languages:
            raise ValueError(f"{country}: languages must not be empty")
        if not timezones:
            raise ValueError(f"{country}: timezones must not be empty")
        if str(timezones[0][0]) != str(profile["timezone_iana"]):
            raise ValueError(
                f"{country}: timezones[0] ({timezones[0][0]}) must equal timezone_iana ({profile['timezone_iana']})"
            )
        profile["languages"] = languages
        profile["timezones"] = timezones
        profile["lang"] = languages[0]
        profile["lang_full"] = accept_language(languages)


_finalize()


def market_profile(country: str) -> dict[str, Any]:
    """Return the canonical profile for ``country`` (case-insensitive).

    Unknown markets degrade to the UTC/en-US default rather than to a
    half-filled dict; callers that measured the exit pass the measured
    timezone through ``set_fingerprint_geo`` so only the language falls back.
    """
    return MARKET_PROFILES.get(str(country or "").strip().upper(), dict(DEFAULT_TZ_FALLBACK))


def market_entry(country: str) -> dict[str, Any] | None:
    """Return the full market entry (timezones/languages), or ``None`` when uncurated."""
    return MARKET_PROFILES.get(str(country or "").strip().upper())


def geo_profile(country: str) -> dict[str, str] | None:
    """Protocol-lane rendering of a market, or ``None`` when the market is uncurated.

    The protocol fingerprint lane needs ``{timezone, lang, lang_full}`` while the
    browser lane needs the full ``MARKET_PROFILES`` entry; this is the protocol
    projection so ``fingerprint_pool`` does not reach into a private table.

    ``None`` (not the UTC default) is returned on purpose: callers must be able
    to tell "this country has a curated clock" from "unknown country, use the
    measured timezone".  Returning the fallback here would silently hand a
    Brazilian exit a UTC clock -- the exact mismatch the geo resolver exists to
    prevent.
    """
    entry = MARKET_PROFILES.get(str(country or "").strip().upper())
    if not entry:
        return None
    return {
        "timezone": str(entry["timezone_iana"]),
        "lang": str(entry["lang"]),
        "lang_full": str(entry["lang_full"]),
    }


def market_timezone(country: str, rng: random.Random | None = None) -> str:
    """Pick one of the market's timezones by weight (primary when uncurated).

    Used by callers that know the exit *country* but did not measure the exact
    zone. ``timezone_iana`` stays the deterministic answer everywhere else, so
    this is opt-in and cannot change the default path.
    """
    profile = MARKET_PROFILES.get(str(country or "").strip().upper())
    if not profile:
        return ""
    timezones = profile["timezones"]
    if len(timezones) == 1:
        return str(timezones[0][0])
    generator = rng or random.Random()
    choice = generator.random()
    cumulative = 0.0
    for iana, weight in timezones:
        cumulative += _weight(weight)
        if choice <= cumulative:
            return str(iana)
    return str(timezones[-1][0])


__all__ = [
    "DEFAULT_TZ_FALLBACK",
    "MARKET_PROFILES",
    "WINDOWS_TIMEZONE_BY_IANA",
    "accept_language",
    "geo_profile",
    "market_entry",
    "market_profile",
    "market_timezone",
]
