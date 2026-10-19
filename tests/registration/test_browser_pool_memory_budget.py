"""The browser-pool RSS watchdog: recommendation, warning, and the off default.

Why this exists
---------------
``max_total_rss_mb`` defaults to 0 (disabled). That was a deliberate decision
(ADR-0013): the watchdog recycles a *live* browser, so enabling it for existing
configs without a canary can interrupt a registration. The gap was that nothing
told the operator what to set it to -- a disabled safety valve with no number
next to it never gets turned on.

Measured 2026-10-21 on this machine (3 reps): one headless Chromium process tree
settles at ~478 MB, four at ~1.9 GB. Camoufox is Gecko and heavier, so the
recommendation is ``max_concurrent * 512`` MB (2048 for the default 4-way pool),
surfaced in ``config.example.json``, the pool warning, and ``stats()``.

These tests pin: the default stays off, the recommendation is derived (not a
stale constant), the warning fires once per pool only when the valve is off with
more than one browser, and stats carries the recommendation either way (``stats`` is a property).
"""

from __future__ import annotations

import logging

from sms_tool.browser_pool import BrowserProcessPool, PoolConfig


def config(concurrent: int = 4, cap: int | None = None) -> dict:
    pool = {"enabled": True, "max_concurrent": concurrent}
    if cap is not None:
        pool["max_total_rss_mb"] = cap
    return {"registration": {"browser_process_pool": pool}}


def test_the_watchdog_stays_off_by_default():
    assert PoolConfig.from_config(config()).max_total_rss_mb == 0


def test_the_recommendation_is_derived_from_concurrency():
    assert PoolConfig.from_config(config(concurrent=4)).recommended_max_total_rss_mb() == 2048
    assert PoolConfig.from_config(config(concurrent=1)).recommended_max_total_rss_mb() == 512
    assert PoolConfig.recommended_max_total_rss_mb(PoolConfig(max_concurrent=2)) == 1024


def test_the_example_config_carries_the_recommended_cap():
    import json
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "config.example.json"
    data = json.loads(example.read_text(encoding="utf-8"))
    pool = data["registration"]["browser_process_pool"]
    expected = PoolConfig.from_config(config(concurrent=pool["max_concurrent"]))
    assert pool["max_total_rss_mb"] == expected.recommended_max_total_rss_mb()


def test_a_disabled_watchdog_warns_once_with_the_number(caplog):
    caplog.set_level(logging.WARNING, logger="sms_tool.browser_pool")
    pool = BrowserProcessPool(config(concurrent=4), rss_sampler=lambda: 0.0)
    pool.stats
    pool.stats

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, "one line per pool, not per acquire"
    assert "2048" in warnings[0].getMessage()
    assert "478" in warnings[0].getMessage()


def test_an_enabled_watchdog_does_not_warn(caplog):
    caplog.set_level(logging.WARNING, logger="sms_tool.browser_pool")
    BrowserProcessPool(config(concurrent=4, cap=2048), rss_sampler=lambda: 0.0)
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_a_single_browser_pool_does_not_warn(caplog):
    caplog.set_level(logging.WARNING, logger="sms_tool.browser_pool")
    BrowserProcessPool(config(concurrent=1), rss_sampler=lambda: 0.0)
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_stats_reports_the_recommendation_with_and_without_a_cap():
    disabled = BrowserProcessPool(config(concurrent=4), rss_sampler=lambda: 0.0)
    assert disabled.stats["max_total_rss_mb"] == 0
    assert disabled.stats["recommended_max_total_rss_mb"] == 2048

    enabled = BrowserProcessPool(config(concurrent=4, cap=1536), rss_sampler=lambda: 1.0)
    assert enabled.stats["max_total_rss_mb"] == 1536
    assert enabled.stats["recommended_max_total_rss_mb"] == 2048
