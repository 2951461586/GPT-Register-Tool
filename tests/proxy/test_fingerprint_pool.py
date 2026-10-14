"""allowed_countries must narrow the geo-bound selection, not empty the pool.

Every built profile carries the placeholder country "US" (geo binds at
``_with_geo`` time from the proxy exit), so the old construction-time filter
turned ``allowed_countries: ["VN"]`` into an empty pool and a silent default
profile. The check now runs on the geo-bound result in ``next()``.
"""

import dataclasses

from sms_tool.fingerprint_pool import FingerprintPool


def _pool(**fp_cfg):
    return FingerprintPool.from_config({"registration": {"fingerprint_pool": fp_cfg}})


def test_allowed_countries_does_not_empty_the_pool():
    pool = _pool(allowed_countries=["VN"])
    assert pool.size > 1


def test_allowed_countries_accepts_matching_geo_bound_selection(monkeypatch):
    pool = _pool(allowed_countries=["VN"])
    monkeypatch.setattr(
        pool,
        "_with_geo",
        lambda profile, proxy: dataclasses.replace(profile, country="VN"),
    )
    assert pool.next(proxy="socks5://vn-exit:1080").country == "VN"


def test_allowed_countries_falls_back_to_closest_mismatch(monkeypatch):
    pool = _pool(allowed_countries=["VN"])
    monkeypatch.setattr(
        pool,
        "_with_geo",
        lambda profile, proxy: dataclasses.replace(profile, country="US"),
    )
    profile = pool.next(proxy="socks5://us-exit:1080")
    # Closest-mismatch fallback (never the implicit default profile), and the
    # pool still surfaces the real geo instead of an empty-pool placeholder.
    assert profile.country == "US"


def test_without_allowed_countries_single_draw_unchanged(monkeypatch):
    pool = _pool()
    draws = []
    monkeypatch.setattr(
        pool,
        "_with_geo",
        lambda profile, proxy: profile,
    )
    for _ in range(2):
        draws.append(pool.next())
    assert all(p.country == "US" for p in draws)
