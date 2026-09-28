"""Contract tests for ``scripts/proxy_pool_probe.py``.

Loads the script by path (it is not an importable package module) and pins the
pure helpers: pool resolution, in-order concurrent probing, aggregation and the
clean-subset extraction.  No network is used -- ``probe_pool`` takes an injected
probe callable for exactly this reason.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("proxy_pool_probe_script", ROOT / "scripts" / "proxy_pool_probe.py")
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load scripts/proxy_pool_probe.py")
probe_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_mod)

from sms_tool.proxy_edge_probe import BLOCKED, CLEAN, DEAD, DEGRADED, EdgeVerdict  # noqa: E402


def _verdict(proxy, status):
    return EdgeVerdict(proxy=proxy, status=status)


def _args(pool="", file="", lane="protocol_registration"):
    return argparse.Namespace(pool=pool, file=file, lane=lane)


def test_load_pool_prefers_explicit_pool():
    pool = probe_mod.load_pool(_args(pool="http://u:p@a.example:1,http://u:p@b.example:2"), {})
    assert pool == ["http://u:p@a.example:1", "http://u:p@b.example:2"]


def test_load_pool_reads_file(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("http://u:p@a.example:1\n# comment\nhttp://u:p@b.example:2\n", encoding="utf-8")
    pool = probe_mod.load_pool(_args(file=str(path)), {})
    assert pool == ["http://u:p@a.example:1", "http://u:p@b.example:2"]


def test_load_pool_falls_back_to_the_registration_lane():
    config = {"proxy": {"lanes": {"protocol_registration": ["http://u:p@lane.example:3"]}}}
    assert probe_mod.load_pool(_args(), config) == ["http://u:p@lane.example:3"]


def test_load_pool_missing_file_exits():
    with pytest.raises(SystemExit):
        probe_mod.load_pool(_args(file="/nonexistent/nope.txt"), {})


def test_probe_pool_preserves_input_order_and_injects_chat_base():
    pool = [f"http://u:p@h{i}.example:1" for i in range(6)]
    seen: list[str] = []

    def fake_probe(proxy, *, chat_base, timeout):
        seen.append(proxy)
        return _verdict(proxy, CLEAN)

    verdicts = probe_mod.probe_pool(pool, chat_base="https://chatgpt.com", timeout=1.0, workers=4, probe=fake_probe)
    assert [v.proxy for v in verdicts] == pool
    assert sorted(seen) == sorted(pool)


def test_probe_pool_sequential_when_workers_is_one():
    pool = ["http://u:p@a.example:1", "http://u:p@b.example:2"]
    verdicts = probe_mod.probe_pool(pool, chat_base="x", timeout=1.0, workers=1, probe=lambda p, **_: _verdict(p, DEAD))
    assert [v.status for v in verdicts] == [DEAD, DEAD]


def test_probe_pool_tolerates_a_non_int_workers_value():
    pool = ["http://u:p@a.example:1", "http://u:p@b.example:2"]
    verdicts = probe_mod.probe_pool(
        pool,
        chat_base="x",
        timeout=1.0,
        workers="two",
        probe=lambda p, **_: _verdict(p, CLEAN),  # type: ignore[arg-type]
    )
    assert len(verdicts) == 2


def test_summarize_counts_every_status():
    verdicts = [
        _verdict("a", CLEAN),
        _verdict("b", CLEAN),
        _verdict("c", BLOCKED),
        _verdict("d", DEAD),
        _verdict("e", DEGRADED),
    ]
    assert probe_mod.summarize(verdicts) == {CLEAN: 2, DEGRADED: 1, BLOCKED: 1, DEAD: 1}


def test_clean_proxies_keeps_raw_urls_in_order():
    raw = ["http://u:p@a.example:1", "http://u:p@b.example:2", "http://u:p@c.example:3"]
    from sms_tool.phone_proxy import redact_proxy_url

    verdicts = [
        _verdict(redact_proxy_url(raw[0]), CLEAN),
        _verdict(redact_proxy_url(raw[1]), BLOCKED),
        _verdict(redact_proxy_url(raw[2]), CLEAN),
    ]
    assert probe_mod.clean_proxies(verdicts, raw) == [raw[0], raw[2]]


def test_format_report_is_credential_free_and_summarizes():
    raw = "http://user:Hunter2@a.example:1"
    from sms_tool.phone_proxy import redact_proxy_url

    verdicts = [_verdict(redact_proxy_url(raw), CLEAN), _verdict("DIRECT", DEAD)]
    report = probe_mod.format_report(verdicts)
    assert "Hunter2" not in report
    assert "probed=2 clean=1" in report
    assert "a.example:1" in report
