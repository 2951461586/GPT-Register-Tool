"""The registration preflight must bound an all-fail run and say what it did.

Before this, ``preflight_registration_before_mailbox`` walked the whole pool with
no output and no early exit. On 2026-09-13 20:46 a 30-candidate, three-provider
pool where every route was dead produced **10m51s of a silent black screen**, then
``exit 2`` with nothing on disk -- the desktop log recorded only
``exited with code 2``, so there was no way to tell "all routes dead" from
"the batch never started".

The guards asserted here are deliberately one-sided: they can only *shorten* an
all-fail run. Successful routes are retained, while every candidate within the
budget is checked so an IP-only probe cannot admit an OpenAI-unreachable route.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool import cli


def _args(**overrides):
    values = {"proxy": "http://first.example:8080", "proxy_explicit": False, "proxy_pool": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**registration):
    return {"registration": {"driver": "protocol", **registration}, "proxy": {}}


def _run(pool, probe, config=None, monkeypatch=None):
    """Drive the real preflight with a stubbed network probe."""
    monkeypatch.setattr(cli, "CFG", config or _config())
    with patch.object(cli, "_proxy_pool_values", return_value=pool), patch(
        "sms_tool.registration.registration_network_preflight", side_effect=probe
    ):
        return cli._preflight_registration_before_mailbox(_args())


def test_a_dead_host_is_skipped_after_the_cap(monkeypatch):
    """Three failures on one host must stop the loop probing that host.

    The pool interleaves providers, so without this a provider-wide outage costs
    one full probe per candidate instead of one per provider.
    """
    pool = [f"http://dead.example:8080"] * 5 + ["http://live.example:8080"]
    probed: list[str] = []

    def probe(proxy, **_kwargs):
        probed.append(proxy)
        if "dead" in proxy:
            raise RuntimeError("connection_reset")
        return {"ok": True, "proxy": proxy}

    result = _run(pool, probe, _config(preflight_max_consecutive_failures_per_host=3), monkeypatch)

    assert result["ok"] is True
    # 🔴 2026-09-18 起探测是并发的，**调用顺序不再有保证**（完成序取决于线程调度）。
    # 这里钉住的是「坏出口只吃 cap 个探测」这个**计数**契约，不是调用顺序 ——
    # 顺序层面只有 `successful_routes` 还保证候选序（见
    # `test_registration_preflight_concurrency.py`）。
    assert probed.count("http://dead.example:8080") == 3
    assert probed.count("http://live.example:8080") == 1


def test_every_host_dead_still_fails_but_bounded(monkeypatch):
    """An all-dead pool must raise the stable error without probing everything."""
    pool = [f"http://dead-{n}.example:8080" for n in range(4)] * 5
    probed: list[str] = []

    def probe(proxy, **_kwargs):
        probed.append(proxy)
        raise RuntimeError("connection_reset")

    with pytest.raises(RuntimeError) as excinfo:
        _run(pool, probe, _config(preflight_max_consecutive_failures_per_host=2), monkeypatch)

    assert str(excinfo.value).startswith("registration_preflight_failed:no_healthy_route:")
    # 4 hosts x 2 allowed failures = 8 probes, not 20.
    assert len(probed) == 8


def test_wall_clock_budget_stops_the_walk(monkeypatch, capsys):
    """The budget is the guard that does not depend on pool shape.

    Each candidate burns four fake seconds (the loop reads the clock once for the
    start, once for the budget check, and twice around the probe), so a 10s
    budget -- the clamp floor -- must stop well before a 40-host pool is walked.
    """
    pool = [f"http://host-{n}.example:8080" for n in range(40)]
    probed: list[str] = []
    ticks = iter(range(0, 100_000))

    def probe(proxy, **_kwargs):
        probed.append(proxy)
        raise RuntimeError("connection_reset")

    monkeypatch.setattr(
        "sms_tool.commands.registration.time",
        types.SimpleNamespace(time=lambda: next(ticks)),
    )

    with pytest.raises(RuntimeError):
        _run(
            pool,
            probe,
            _config(preflight_budget_seconds=10.0, preflight_max_consecutive_failures_per_host=50),
            monkeypatch,
        )

    assert 0 < len(probed) < len(pool)
    assert "注册预检超时" in capsys.readouterr().out


def test_progress_is_reported_for_every_probe(monkeypatch, capsys):
    """The operator must see which route is being tried and how it went."""
    pool = ["http://dead.example:8080", "http://live.example:8080"]

    def probe(proxy, **_kwargs):
        if "dead" in proxy:
            raise RuntimeError("connection_reset")
        return {"ok": True, "proxy": proxy}

    _run(pool, probe, _config(), monkeypatch)

    out = capsys.readouterr().out
    assert "注册预检" in out
    assert "1/2 dead.example:8080 失败" in out
    assert "2/2 live.example:8080 可用" in out


def test_all_candidates_are_checked_and_only_openai_healthy_routes_survive(monkeypatch):
    pool = [
        "http://healthy-a.example:8080",
        "http://healthy-b.example:8080",
        "http://dead.example:8080",
    ]
    args = _args()
    probed = []

    def probe(proxy, **_kwargs):
        probed.append(proxy)
        if "dead" in proxy:
            raise RuntimeError("chatgpt_unreachable")
        return {"ok": True, "proxy": proxy}

    monkeypatch.setattr(cli, "CFG", _config())
    with patch.object(cli, "_proxy_pool_values", return_value=pool), patch(
        "sms_tool.registration.registration_network_preflight", side_effect=probe
    ):
        result = cli._preflight_registration_before_mailbox(args)

    assert result["ok"] is True
    # 并发 ⇒ 调用顺序无保证；「每个候选都被探过」与「只有 OpenAI 健康的活下来」
    # 这两条才是契约。`args.proxy` / `args.proxy_pool` 的顺序仍然确定（见下两行）。
    assert sorted(probed) == sorted(pool)
    assert args.proxy == pool[0]
    assert args.proxy_pool.splitlines() == pool[:2]


def test_progress_lines_never_leak_proxy_credentials(monkeypatch, capsys):
    """A progress line is operator-visible; it must not carry the password."""
    pool = ["http://user:hunter2@dead.example:8080"]

    def probe(proxy, **_kwargs):
        raise RuntimeError("connection_reset")

    with pytest.raises(RuntimeError):
        _run(pool, probe, _config(), monkeypatch)

    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "dead.example:8080" in out


def test_limits_fall_back_to_defaults_on_garbage_config():
    """A hand-edited shard must not be able to disable the guards by accident."""
    from sms_tool.commands.registration import _preflight_limits

    assert _preflight_limits({}) == (3, 180.0)
    assert _preflight_limits({"registration": {"preflight_budget_seconds": "nonsense"}}) == (3, 180.0)
    assert _preflight_limits({"registration": {"preflight_max_consecutive_failures_per_host": 0}}) == (1, 180.0)
    assert _preflight_limits({"registration": None}) == (3, 180.0)
