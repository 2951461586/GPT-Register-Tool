import logging

import pytest

from sms_tool.payment.adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from sms_tool.payment.catalog import PAYMENT_METHODS
from sms_tool.payment.contracts import PaymentRequest, PaymentResult
from sms_tool.payment.link_manager import PAYMENT_ADAPTERS, _reference_root
from sms_tool.pay_link.base import PAYMENT_METHODS as PAYMENT_SPECS, PaymentMethodSpec

_LOGGER_NAME = "sms_tool.payment.link_manager"


class _Proc:
    """Minimal CompletedProcess stand-in: the log line only reads returncode."""

    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stdout = ""


def _terminal_lines(caplog, spec: PaymentMethodSpec, proc, parsed, timeout_err):
    from sms_tool.pay_link import adapters

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        adapters._log_extractor_terminal(spec, proc, parsed, timeout_err)
    return [record.getMessage() for record in caplog.records if record.name == _LOGGER_NAME]


def test_adapter_registry_routes_methods_and_rejects_duplicates():
    registry = PaymentAdapterRegistry()
    adapter = FunctionPaymentAdapter("fake", ("fake",), lambda **kwargs: {"ok": True})
    registry.register(adapter)
    # A payment adapter registry, not a SQL cursor: the shared SQL-injection
    # heuristic matches any ``.execute(<call>)`` sink.
    result = registry.execute(  # pi-lens-ignore: python-sql-injection
        PaymentRequest.create(payment_method="fake", access_token="at")
    )
    assert isinstance(result, PaymentResult)
    assert result.ok
    with pytest.raises(ValueError):
        registry.register(FunctionPaymentAdapter("other", ("fake",), lambda **kwargs: {}))


def test_default_registry_covers_catalog_exactly_once():
    assert set(PAYMENT_ADAPTERS.methods()) == set(PAYMENT_METHODS)
    for method, definition in PAYMENT_METHODS.items():
        assert PAYMENT_ADAPTERS.get(method).key == definition.adapter


@pytest.mark.parametrize("method", sorted(PAYMENT_METHODS))
def test_catalog_script_paths_exist(method):
    definition = PAYMENT_METHODS[method]
    if definition.script:
        assert (_reference_root() / definition.script).is_file()


def test_extractor_terminal_line_covers_every_outcome(caplog):
    """Exactly one INFO line per extractor run, and each outcome is distinct.

    The distinction matters, not just the presence: before this, a run whose
    gated stage quietly succeeded was byte-identical in the logs to one that
    never reached it, which is what blocked the 2026-10-05 Sentinel re-review.
    """
    spec = PAYMENT_SPECS["ideal"]
    cases = [
        ("timeout", None, {}, {"error_code": "extractor_timeout"}, "ok=timeout", "error_code=extractor_timeout"),
        ("silent", _Proc(), {}, None, "ok=no_terminal_contract", "contract=absent"),
        ("ok", _Proc(), {"schema": "protocol_payment.v1", "ok": True}, None, "ok=ok", "contract=protocol_payment.v1"),
        (
            "failed",
            _Proc(1),
            {"schema": "protocol_payment.v1", "ok": False, "error_code": "generic_decline"},
            None,
            "ok=failed",
            "error_code=generic_decline",
        ),
        # direct_card's cancellation print carries ok but no schema; it must not
        # be reported as silence.
        ("non-contract", _Proc(), {"ok": False, "error_type": "Cancelled"}, None, "ok=failed", "contract=absent"),
    ]
    for label, proc, parsed, timeout_err, *expected in cases:
        lines = _terminal_lines(caplog, spec, proc, parsed, timeout_err)
        caplog.clear()
        assert len(lines) == 1, f"{label}: expected one line, got {lines}"
        assert lines[0].startswith("extractor terminal: "), label
        assert "payment_method=ideal" in lines[0], label
        for fragment in expected:
            assert fragment in lines[0], f"{label}: {fragment!r} missing from {lines[0]!r}"


def test_extractor_terminal_line_reports_the_contracts_own_method(caplog):
    """A method mismatch must stay visible rather than being masked by spec.key."""
    spec = PAYMENT_SPECS["ideal"]
    lines = _terminal_lines(
        caplog, spec, _Proc(), {"schema": "protocol_payment.v1", "payment_method": "twint", "ok": True}, None
    )
    assert "payment_method=twint" in lines[0]


def test_extractor_terminal_line_never_carries_payload_text(caplog):
    """The whole point of logging three fields: `error` and `url` must not leak.

    The terminal object's `error` is provider prose that can embed a URL, and
    `url` is the artifact itself. A log line a reviewer has to distrust is worse
    than no log line, so this asserts the absence, not just the presence.
    """
    spec = PAYMENT_SPECS["ideal"]
    secret_url = "https://pay.openai.com/c/pay/ZZSECRETZZ"
    lines = _terminal_lines(
        caplog,
        spec,
        _Proc(),
        {
            "schema": "protocol_payment.v1",
            "ok": False,
            "error_code": "generic_decline",
            "error": f"declined at {secret_url}",
            "url": secret_url,
            "artifacts": {"qr_data": "ZZSECRETZZ", "cs_id": "cs_ZZSECRETZZ"},
            "cs_id": "cs_ZZSECRETZZ",
        },
        None,
    )
    assert len(lines) == 1
    assert "error_code=generic_decline" in lines[0]
    for leaked in ("ZZSECRETZZ", "pay.openai.com", "declined", "cs_id", "qr_data"):
        assert leaked not in lines[0], f"{leaked!r} leaked into the terminal log line"


def test_finish_extractor_emits_the_terminal_line(monkeypatch, caplog):
    """The funnel itself must emit it, so a new extractor cannot skip it."""
    from sms_tool.pay_link import adapters

    spec = PAYMENT_SPECS["ideal"]
    seen = []
    monkeypatch.setattr(
        adapters,
        "_run_extractor_subprocess",
        lambda *a, **k: (_Proc(), "", None),
    )
    monkeypatch.setattr(adapters, "_log_extractor_terminal", lambda *a, **k: seen.append(a))
    adapters._finish_extractor(spec, _reference_root() / spec.script, ["python", "x"], env={}, timeout=5)
    assert len(seen) == 1, "_finish_extractor must report every run through one call"
    # and the timeout branch must report too, or a hung extractor is silent
    monkeypatch.setattr(
        adapters,
        "_run_extractor_subprocess",
        lambda *a, **k: (None, "", {"error_code": "extractor_timeout"}),
    )
    adapters._finish_extractor(spec, _reference_root() / spec.script, ["python", "x"], env={}, timeout=5)
    assert len(seen) == 2


def test_build_kakao_env_prefers_explicit_proxy_file_and_routes_runtime_dirs(tmp_path):
    from sms_tool.pay_link import adapters

    script_dir = tmp_path / "kakao"
    script_dir.mkdir(parents=True)
    seed_pool = script_dir / "proxy_seeds.txt"
    seed_pool.write_text("http://fallback:8080\n", encoding="utf-8")
    script = script_dir / "kakao_extract.py"
    script.write_text("", encoding="utf-8")

    explicit_env = adapters._build_kakao_env("tok", "http://explicit:8080", "/tmp/explicit_seed.txt", script, {})
    assert explicit_env["KAKAO_PROXY_SEED_FILE"] == "/tmp/explicit_seed.txt"
    assert explicit_env["KAKAO_LOG_DIR"].endswith("logs")
    assert explicit_env["KAKAO_PROXY_STATE_FILE"].endswith("proxy_state.json")

    fallback_env = adapters._build_kakao_env("tok", "", "", script, {})
    assert fallback_env["KAKAO_PROXY_SEED_FILE"] == str(seed_pool)


# ── P3-2: the Sentinel mint shares the extractor's deadline ─────────────────


def test_deadline_remaining_is_never_below_one():
    import time

    from sms_tool.pay_link import adapters

    assert adapters._deadline_remaining(time.monotonic() - 100) == 1
    assert adapters._deadline_remaining(time.monotonic() + 30) >= 29


@pytest.mark.parametrize(
    ("configured", "mint_seconds", "expected_mint", "expected_child"),
    [
        # A short run: the mint is capped by the remaining budget, not by 60s.
        (5, 50, 5, 1),
        # A normal run: the mint keeps its own 60s ceiling and the child gets
        # exactly what the mint did not spend.
        (100, 10, 60, 90),
        # A long run: the mint still never exceeds its own 60s ceiling.
        (600, 10, 60, 590),
    ],
)
def test_protocol_script_bounds_mint_and_child_by_one_deadline(
    monkeypatch, tmp_path, configured, mint_seconds, expected_mint, expected_child
):
    from sms_tool.pay_link import adapters

    clock = {"now": 1000.0}
    captured: dict[str, int] = {}
    ctx = adapters._ExtractorContext(
        runtime_config=None,
        script=tmp_path / "extractor.py",
        method_cfg={},
        timeout=configured,
    )

    def fake_sentinel(proxy, *, timeout=60):
        captured["mint"] = timeout
        clock["now"] += mint_seconds
        return {}

    def fake_finish(spec, script, command, *, env, timeout, cleanup_paths=()):
        captured["child"] = timeout
        return _Proc(), "", {"schema": "protocol_payment.v1", "ok": False}, None

    monkeypatch.setattr(adapters.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(adapters, "_prepare_extractor", lambda spec, kwargs: ctx)
    monkeypatch.setattr(adapters, "_sentinel_env", fake_sentinel)
    monkeypatch.setattr(adapters, "_finish_extractor", fake_finish)
    monkeypatch.setattr(adapters, "_runtime_temp_dir", lambda: tmp_path)

    adapters._run_protocol_script(PAYMENT_SPECS["ideal"], "at", "socks5h://127.0.0.1:1080")

    assert captured["mint"] == expected_mint
    assert captured["child"] == expected_child


def test_direct_card_bounds_mint_and_child_by_one_deadline(monkeypatch, tmp_path):
    from sms_tool.pay_link import adapters

    clock = {"now": 2000.0}
    captured: dict[str, int] = {}
    ctx = adapters._ExtractorContext(
        runtime_config=None,
        script=tmp_path / "direct_card_extract.py",
        method_cfg={},
        timeout=120,
    )
    token_file = tmp_path / "token.txt"
    token_file.write_text("at\n", encoding="utf-8")

    def fake_sentinel(proxy, *, timeout=60):
        captured["mint"] = timeout
        clock["now"] += 25
        return {}

    def fake_finish(spec, script, command, *, env, timeout, cleanup_paths=()):
        captured["child"] = timeout
        return _Proc(), "", {"ok": True, "long_url": "https://chatgpt.com/checkout/x/y"}, None

    monkeypatch.setattr(adapters.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(adapters, "_prepare_extractor", lambda spec, kwargs: ctx)
    monkeypatch.setattr(adapters, "_sentinel_env", fake_sentinel)
    monkeypatch.setattr(adapters, "_finish_extractor", fake_finish)
    monkeypatch.setattr(adapters, "_write_token_file", lambda access_token: str(token_file))

    adapters._run_direct_card(PAYMENT_SPECS["direct_card"], "at", "http://checkout.example:1")

    assert captured["mint"] == 60
    assert captured["child"] == 95
