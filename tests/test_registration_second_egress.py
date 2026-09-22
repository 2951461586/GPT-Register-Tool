"""第二出口（VN 主池 + rola/PH 备池）的端到端契约。

背景：注册池原本 30 条**全部 VN**（9http ``geo-VN`` / IPWO ``custom_zone_VN`` /
rola ``country-vn``），出口地区单一 —— 一家供应商或一个地区被拒就是整批同时死
（2026-09-13 的 403 就是这么打穿全池的）。现在 rola 10 条改挂 PH。

这次改动同时暴露两类**静默降级**，本文件把两条都钉住：

1. 重定到**未接线**的地区不报错：协议路径回退 UTC/en-US（实测时区也拿不到就
   保持档案原语言），浏览器路径 ``locale_profile_key_from_geo`` 直接回退
   ``DEFAULT_LOCALE_PROFILE``（``'us'``）—— 美国指纹配菲律宾出口。
2. 供应商若不认重定后的地区码，指纹会照着**声明**地区走而毫无提示。
   ``verify_hint`` 就是那个开关，默认关（开着要给每次选择付一次探测）。

用例 E 是主守卫，用例 F 是它的负向对照 —— 缺了 F，E 可能只是因为
``locale_profile_key_from_geo`` 恒不返回 ``'us'`` 而"恒真"。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

from sms_tool.auth_headers import _AUTH_FINGERPRINT_LOCAL, _GEO_PROFILES, set_fingerprint_geo
from sms_tool.browser_fingerprint_pool import (
    BROWSER_LOCALE_PROFILES,
    COUNTRY_LOCALE_PROFILE_MAP,
    DEFAULT_LOCALE_PROFILE,
    TIMEZONE_NAME_BY_IANA,
    build_browser_environment,
    locale_profile_key_from_geo,
)
from sms_tool.fingerprint_pool import FingerprintPool
from sms_tool.geo.profiles import MARKET_PROFILES
from sms_tool.proxy_entry import infer_region, parse_proxy, retarget_region

# 三条**真实池模板**（照抄 proxy.json 的拼写，含各自的地区标签与密码形态）。
NINEHTTP_VN = "http://VSBFTHZC-geo-VN-sid-mwT3-ttl-5:A90IhxdPeq@global.9http.com:9091"
IPWO_VN = "http://lizi1_custom_zone_VN_sid_74236952_time_5:451203zhy@us.ipwo.net:7878"
# rola 的密码含 ``^`` 与 ``#``（原文即已百分号编码），专门用来打"手拼 URL"的坑。
ROLA_VN = "http://SYS433954tbr_1-country-vn:tWL%5E%23B@gate.rola.vip:2000"

_SECOND_EGRESS = "PH"


@pytest.fixture(autouse=True)
def _restore_auth_fingerprint_local():
    """``set_fingerprint_geo`` 改的是 thread-local，别在用例之间漏出去。"""
    saved = dict(_AUTH_FINGERPRINT_LOCAL.__dict__)
    yield
    _AUTH_FINGERPRINT_LOCAL.__dict__.clear()
    _AUTH_FINGERPRINT_LOCAL.__dict__.update(saved)


@pytest.fixture
def fresh_resolver(monkeypatch):
    """每个用例一套**独立**的 GeoResolver（共享实例带缓存，会串用例）。

    ``fingerprint_pool._resolve_geo`` 在函数内 ``from .geo import
    shared_geo_resolver``，所以打源模块的属性才能生效。
    """
    from sms_tool.geo.resolver import GeoResolver

    resolver = GeoResolver()
    monkeypatch.setattr("sms_tool.geo.shared_geo_resolver", lambda *a, **k: resolver)
    return resolver


# ─────────────────── A. 模板层：重定到 PH 且保留原标签拼写 ───────────────────


@pytest.mark.parametrize(
    ("name", "raw", "expected_tag"),
    [
        ("9http", NINEHTTP_VN, "geo-"),
        ("IPWO", IPWO_VN, "custom_zone_"),
        ("rola", ROLA_VN, "country-"),
    ],
)
def test_each_pool_template_retargets_to_the_second_egress(name, raw, expected_tag):
    retargeted = retarget_region(raw, _SECOND_EGRESS)
    assert retargeted != raw, f"{name} 重定静默无变化（模板没被识别）"
    assert infer_region(retargeted) == _SECOND_EGRESS, f"{name} 重定后地区推断失败"
    # 每家只认自己那套标签：9http 要 geo-、IPWO 要 custom_zone_、rola 要 country-。
    # 统一改写成 region- 会被供应商拒收凭据。
    assert f"{expected_tag}{_SECOND_EGRESS}" in retargeted, f"{name} 标签拼写被改写"


def test_rola_retag_survives_the_urlsplit_path():
    """密码含 ``^#``：必须验走 ``urlsplit`` 的路径。

    ``parse_proxy`` 自己用 ``rsplit("@")``，往返测试**查不出**拼 URL 的坑；
    ``#`` 一旦被当 fragment 吃掉，密码就废了。
    """
    retargeted = retarget_region(ROLA_VN, _SECOND_EGRESS)
    parsed = urlsplit(retargeted)
    assert unquote(parsed.password or "") == "tWL^#B"
    assert parsed.hostname == "gate.rola.vip"
    assert parsed.port == 2000


def test_retag_normalises_the_region_code_to_upper_case_and_rolls_back_equivalently():
    """回滚**不是**逐字节还原 —— 地区码会被规范成大写。

    rola 的国家码大小写不敏感（2026-09-12 实测 ``country-US`` 拿到 US 出口），
    所以 ``country-VN`` 与 ``country-vn`` 功能等价；但回滚脚本不能按"字节一致"
    去断言，否则会以为自己没回滚成功。
    """
    ph = retarget_region(ROLA_VN, _SECOND_EGRESS)
    assert "country-PH" in ph

    back = retarget_region(ph, "VN")
    assert "country-VN" in back
    before, after = parse_proxy(ROLA_VN), parse_proxy(back)
    assert after.username.upper() == before.username.upper()
    assert (after.host, after.port, after.password) == (before.host, before.port, before.password)


# ─────────────────── B. 地理表：PH 两条 lane 都接线 ───────────────────


def test_second_egress_region_is_curated_on_both_lanes():
    assert _SECOND_EGRESS in MARKET_PROFILES
    assert _GEO_PROFILES[_SECOND_EGRESS]["timezone"] == "Asia/Manila"
    assert _GEO_PROFILES[_SECOND_EGRESS]["lang"] == "en-PH"
    assert COUNTRY_LOCALE_PROFILE_MAP[_SECOND_EGRESS] == "ph"
    assert BROWSER_LOCALE_PROFILES["ph"]["timezone_iana"] == "Asia/Manila"
    assert BROWSER_LOCALE_PROFILES["ph"]["navigator_language"] == "en-PH"
    assert TIMEZONE_NAME_BY_IANA["Asia/Manila"], "缺 Windows 时区名 ⇒ timezone_name 变空串"


def test_second_egress_does_not_alias_to_the_default_locale():
    """主守卫：备池地区绝不能落到默认（美国）档案上。"""
    key = locale_profile_key_from_geo({"country": _SECOND_EGRESS})
    assert key == "ph"
    assert key != DEFAULT_LOCALE_PROFILE


def test_an_uncurated_region_still_degrades_so_the_guard_above_is_not_vacuous():
    """负向对照：未接线地区**仍然**降级，证明上一条不是恒真。"""
    assert "BR" not in MARKET_PROFILES
    assert locale_profile_key_from_geo({"country": "BR"}) == DEFAULT_LOCALE_PROFILE
    assert set_fingerprint_geo("BR")["timezone"] == "UTC"


# ─────────────────── C. 两条 lane 的端到端绑定 ───────────────────


def test_protocol_pool_binds_the_second_egress_geo_without_probing(fresh_resolver):
    """已接线地区必须"零探测"命中 —— 否则每批都要多付一次往返。"""
    calls: list = []
    fresh_resolver.probe = lambda *a, **k: calls.append(a)

    profile = FingerprintPool(mode="round_robin").next(retarget_region(ROLA_VN, _SECOND_EGRESS))

    assert profile.country == _SECOND_EGRESS
    assert profile.timezone == "Asia/Manila"
    assert profile.lang == "en-PH"
    assert calls == [], "已接线地区不该触发出口探测"


def test_browser_pool_binds_the_second_egress_geo():
    env = build_browser_environment({"country": _SECOND_EGRESS, "timezone": "Asia/Manila"})
    assert env["locale_profile"] == "ph"
    assert env["navigator_language"] == "en-PH"
    assert env["timezone_iana"] == "Asia/Manila"
    assert env["timezone_name"] == "Singapore Standard Time"


# ─────────────────── D. verify_hint：声明地区 vs 实测地区对账 ───────────────────


def test_verify_hint_makes_the_measurement_win_over_a_wrong_claim(fresh_resolver):
    """供应商不认地区码时，指纹必须跟**实测**走，而不是跟着声明走。"""
    from sms_tool.geo import ProxyGeo

    fresh_resolver.probe = lambda *a, **k: ProxyGeo(
        country="VN", timezone="Asia/Ho_Chi_Minh", source="probe"
    )

    profile = FingerprintPool(mode="round_robin", verify_hint=True).next(
        retarget_region(ROLA_VN, _SECOND_EGRESS)
    )

    assert profile.country == "VN"
    assert profile.timezone == "Asia/Ho_Chi_Minh"


def test_without_verify_hint_the_claim_is_trusted_and_no_probe_happens(fresh_resolver):
    """默认关：声明地区直接采信，且**不**付探测代价（这是既有性能契约）。"""
    calls: list = []
    fresh_resolver.probe = lambda *a, **k: calls.append(a)

    profile = FingerprintPool(mode="round_robin").next(retarget_region(ROLA_VN, _SECOND_EGRESS))

    assert profile.country == _SECOND_EGRESS
    assert calls == []


def test_verify_hint_is_read_from_config():
    on = FingerprintPool.from_config(
        {"registration": {"fingerprint_pool": {"verify_hint": True}}}
    )
    off = FingerprintPool.from_config({"registration": {"fingerprint_pool": {}}})
    assert on._verify_hint is True
    assert off._verify_hint is False


# ─────────────────── E. 浏览器 lane 的契约：它**只实测**，不用声明值 ───────────────────
#
# `detect_proxy_exit_geo` 调 `resolve(key, need_timezone=True, probe=...)` 时**不传 hint**
# ⇒ 解析器里 `not code` 恒真 ⇒ **永远探测**。所以浏览器 lane 与协议 lane 的取值来源不同：
#
#   协议 lane ：默认采信凭据里的地区标签（零成本，`verify_hint` 才验证）
#   浏览器 lane：永远实测（每次多一次往返，但天然免疫"供应商不认地区码"）
#
# 这个差异有两条后果，下面三个用例分别钉住。

PH_PROXY = "http://SYS433954tbr_1-country-PH:tWL%5E%23B@gate.rola.vip:2000"


@pytest.fixture
def browser_geo(monkeypatch):
    """隔离浏览器 lane 的**两层**缓存：模块级 `_GEO_CACHE` + 共享 resolver。

    只清 `_GEO_CACHE` 是不够的 —— 共享 resolver 自带 30min 正向缓存，
    上一条用例测到的国家会被下一条读到（实测踩过，第二行结果直接是错的）。
    """
    from sms_tool import browser_fingerprint_pool as bfp
    from sms_tool.geo import reset_shared_geo_resolver

    monkeypatch.setattr(bfp, "_GEO_CACHE", {})
    reset_shared_geo_resolver()
    yield bfp
    reset_shared_geo_resolver()


def _browser_env(bfp, monkeypatch, measured: dict):
    """把出口探测换成固定返回值，跑完整"探测 → 指纹"链路。"""
    monkeypatch.setattr(bfp, "_query_geo_endpoints", lambda proxy, timeout: measured)
    return bfp.build_browser_environment(bfp.detect_proxy_exit_geo(PH_PROXY))


def test_browser_lane_measures_and_the_measurement_beats_the_declared_region(browser_geo, monkeypatch):
    """声明 PH、实测 VN ⇒ 实测胜出。

    与协议 lane 相反，所以浏览器 lane 不可能被"供应商忽略地区码"骗到。
    """
    env = _browser_env(browser_geo, monkeypatch, {"country": "VN", "timezone": "Asia/Ho_Chi_Minh"})
    assert env["locale_profile"] == "vn"


def test_browser_lane_binds_the_second_egress_when_the_probe_agrees(browser_geo, monkeypatch):
    env = _browser_env(browser_geo, monkeypatch, {"country": "PH", "timezone": "Asia/Manila"})
    assert env["locale_profile"] == "ph"
    assert env["navigator_language"] == "en-PH"
    assert env["timezone_iana"] == "Asia/Manila"


def test_browser_lane_prefers_the_measured_timezone_over_the_curated_table(browser_geo, monkeypatch):
    """实测时区必须**覆盖**规范表里的时区。

    一国多时区是常态（美国就有 4 个），出口实测到的那个才是真的；只按国家取表会让浏览器
    报"出口在芝加哥、时区却是纽约"这种自相矛盾。

    ⚠️ 这条用例的存在理由来自变异测试：最初那两条只断言 `locale_profile` / 国家，
    而实测时区与规范表**恰好相同**，所以把"实测覆盖"整段逻辑禁掉也照样全绿（M5 SURVIVED）。
    这里特意用**与规范表不同**的实测时区，让覆盖逻辑可观察。
    """
    env = _browser_env(browser_geo, monkeypatch, {"country": "US", "timezone": "America/Chicago"})
    assert env["locale_profile"] == "us"
    assert env["timezone_iana"] == "America/Chicago"


def test_browser_lane_falls_back_to_the_default_locale_when_the_probe_fails(browser_geo, monkeypatch):
    """🔴 探测失败 ⇒ 静默回退 `'us'`（美国指纹 + 非美出口）。

    这是两条 lane 会**互相矛盾**的那条路径：同一个 rola 代理，协议 lane 报 PH（采信标签），
    浏览器 lane 报 US（探测失败）。排查"指纹和出口对不上"时先看这里 ——
    要么修探测（网络/超时），要么让浏览器 lane 也吃 `infer_region` 的 hint。
    """
    env = _browser_env(browser_geo, monkeypatch, {})
    assert env["locale_profile"] == DEFAULT_LOCALE_PROFILE
    # 而且**不能**凭空声称一个国家：落盘的 identity context 会记 `geo`，
    # 伪造 `{"country": "US"}` 与"没测到"在下游看来完全不同（前者是一条假证据）。
    # 这条断言来自变异测试：只断言 locale 的话，把失败分支改成伪造 US 也能全绿。
    assert env["geo"] == {}
