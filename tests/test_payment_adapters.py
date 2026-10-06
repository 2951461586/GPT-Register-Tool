import logging

import pytest

from sms_tool.payment_adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from sms_tool.payment_catalog import PAYMENT_METHODS
from sms_tool.payment_contracts import PaymentRequest, PaymentResult
from sms_tool.payment_link_manager import PAYMENT_ADAPTERS, _reference_root
from sms_tool.pay_link.base import PAYMENT_METHODS as PAYMENT_SPECS, PaymentMethodSpec

_LOGGER_NAME = "sms_tool.payment_link_manager"


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
    result = registry.execute(PaymentRequest.create(payment_method="fake", access_token="at"))
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
