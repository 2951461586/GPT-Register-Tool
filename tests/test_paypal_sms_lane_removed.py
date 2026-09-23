"""The static phone pool is gone from the PayPal lane, and it says so.

Why this file exists
--------------------
PayPal checkout had its own number source: ``paypal_auto.phone_numbers``
(round-robin list of ``{phone, sms_api_url}``) with a single-number legacy
fallback. It was removed on 2026-09-22 along with the rest of the static
phone-pool mode, because the URLs it handed out pointed at activations this
codebase never created -- so nothing ever completed or cancelled them.

The removal has a failure mode worth testing that is *not* "SMS stops working":

    An empty ``api_url`` used to fall straight through to the poll loop.
    ``requests.get("")`` raises, the baseline stays empty, and the loop spins
    for the full ``timeout`` -- 120s by default -- before reporting
    ``sms_code_timeout``.

That message names the wrong cause. The code was never going to arrive, because
nobody had configured where to get a number. So every gate that *can* tell a
gate is present must check the source **before** polling, and the one gate that
cannot tell (nodriver clicks "Send Code" blind) must **not** raise, or it would
fail the majority of payments that never ask for SMS at all.

Both halves of that asymmetry are pinned below, along with a structural guard
that no module reads the removed keys again.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from unittest import mock

import pytest

from sms_tool import nodriver_paypal, paypal_reverse
from sms_tool.paypal import config_picker, flow_steps
from sms_tool.paypal.errors import _PayPalStepError
from sms_tool.sms_utils import NO_NUMBER_SOURCE, NO_NUMBER_SOURCE_MESSAGE

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SMS_TOOL = REPO_ROOT / "sms_tool"

# The keys the static phone-pool mode was configured through. Every one of them
# is dead now: nothing may read them.
REMOVED_KEYS = {
    "phone_pool",
    "phone_number",
    "phone_numbers",
    "sms_api_url",
    "phone_index_file",
}

# ``str.get`` / ``dict.get`` style accessors. A config read in this codebase is
# ``cfg.get("<key>")`` or ``cfg["<key>"]``; the guard below matches exactly
# those two shapes and nothing else.
_READ_ACCESSORS = {"get", "pop", "setdefault"}

# Only reads performed *on a config object* count. Without this scoping the
# guard produces two false positives that are both correct code:
#
#   ``account_scan._has_verified_phone``  -> ``data.get("phone_number")``
#       a field of the *session record*, not a config key
#   ``registration.run_phone``            -> ``kwargs.get("phone_pool")``
#       a *parameter* carrying the pool object built by
#       ``phone_reuse.create_phone_pool``; the name collision with the removed
#       config key is coincidental
#
# Neither is a config read, and neither would be fixed by deleting anything.
# Exempting those two files instead would turn two false positives into two
# blind spots, so the receiver is matched by name.
_CONFIG_RECEIVER = re.compile(r"(?:^|_)(?:cfg|config|conf|section|block)$", re.IGNORECASE)


def _receiver_name(node: ast.AST) -> str:
    """Best-effort name of the mapping a read is performed on.

    ``self.sms_cfg`` yields ``"sms_cfg"`` (the last attribute), not ``"self"``
    -- otherwise every read through ``self`` would be invisible.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _receiver_name(node.value)
    return ""


def _config_reads(path: pathlib.Path) -> list[tuple[int, str]]:
    """``(lineno, key)`` for every config read of a removed key.

    Scoped to config receivers on purpose. ``paypal_reverse`` legitimately
    contains the literal ``"phone_number"`` -- as a PayPal *form field* name in
    ``_step_phone`` (``input[name="phone_number"]``) -- and that one is not a
    read at all, so it never reaches this scan.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []

    def _literal_key(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in _READ_ACCESSORS):
                continue
            if not _CONFIG_RECEIVER.search(_receiver_name(func.value)):
                continue
            key = _literal_key(node.args[0])
            if key in REMOVED_KEYS:
                hits.append((node.lineno, key))
        elif isinstance(node, ast.Subscript):
            if not _CONFIG_RECEIVER.search(_receiver_name(node.value)):
                continue
            key = _literal_key(node.slice)
            if key in REMOVED_KEYS:
                hits.append((node.lineno, key))
    return hits


class TestRemovedConfigKeys:
    """Structural: no module reads the removed keys any more."""

    def test_no_module_reads_a_removed_key(self):
        offenders: list[str] = []
        for path in sorted(SMS_TOOL.rglob("*.py")):
            for lineno, key in _config_reads(path):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} -> {key!r}")
        assert offenders == [], (
            "the static phone pool was removed; these reads bring it back: "
            + ", ".join(offenders))

    def test_the_guard_actually_fires_on_a_reintroduced_read(self, tmp_path):
        """Mutation check: the scan above is only worth having if a re-added
        ``cfg.get("phone_numbers")`` would turn it red. A guard that cannot go
        red is decoration."""
        probe = tmp_path / "probe.py"
        probe.write_text("def f(cfg):\n    return cfg.get('phone_numbers') or []\n",
                         encoding="utf-8")
        assert _config_reads(probe) == [(2, "phone_numbers")]

    def test_the_guard_ignores_a_paypal_form_field_name(self, tmp_path):
        """The false positive this scan was written to avoid."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "def f(form):\n"
            "    fields = ['phone', 'phoneNumber', 'phone_number']\n"
            "    return fields\n",
            encoding="utf-8")
        assert _config_reads(probe) == []

    def test_the_guard_ignores_a_session_record_field(self, tmp_path):
        """``account_scan._has_verified_phone`` reads ``data["phone_number"]``
        -- a session-record field. Deleting it would be wrong."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "def f(data):\n"
            "    return str((data or {}).get('phone') or (data or {}).get('phone_number') or '')\n",
            encoding="utf-8")
        assert _config_reads(probe) == []

    def test_the_guard_ignores_a_forwarded_parameter(self, tmp_path):
        """``registration.run_phone`` forwards ``kwargs["phone_pool"]`` -- the
        pool *object*, not the removed config key."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "def run_phone(*args, **kwargs):\n"
            "    return run_email(phone_pool=kwargs.get('phone_pool'))\n",
            encoding="utf-8")
        assert _config_reads(probe) == []

    def test_the_guard_catches_a_read_through_self(self, tmp_path):
        """``self.sms_cfg`` must resolve to ``sms_cfg``, not ``self`` --
        otherwise every read inside a client class is invisible."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "class C:\n"
            "    def f(self):\n"
            "        return self.sms_cfg.get('phone_numbers')\n",
            encoding="utf-8")
        assert _config_reads(probe) == [(3, "phone_numbers")]


class TestExportSurface:
    """``_pick_phone_and_sms`` must not survive as an importable symbol."""

    def test_the_picker_is_gone_from_its_own_module(self):
        assert not hasattr(config_picker, "_pick_phone_and_sms")

    def test_the_picker_is_gone_from_the_package_namespace(self):
        import sms_tool.paypal as paypal_pkg
        assert not hasattr(paypal_pkg, "_pick_phone_and_sms")

    def test_the_picker_is_gone_from_the_compatibility_shell(self):
        import sms_tool.paypal_auto as shell
        assert not hasattr(shell, "_pick_phone_and_sms")

    def test_the_picker_is_not_listed_in_dunder_all(self):
        import sms_tool.paypal as paypal_pkg
        assert "_pick_phone_and_sms" not in paypal_pkg.__all__

    def test_the_card_picker_survives(self):
        """The removal was surgical: card/address selection still works."""
        assert hasattr(config_picker, "_pick_card_and_address")


class _FakeLocator:
    def __init__(self, visible: bool):
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):  # noqa: ARG002 - API shape
        return self._visible

    def fill(self, value):
        self.filled = value


class _FakePage:
    """Every locator reports visible, so the SMS gate is detected at once."""

    def locator(self, selector):  # noqa: ARG002 - API shape
        return _FakeLocator(True)


class TestFlowStepsGate:
    """``flow_steps`` can tell a gate is present, so it must fail fast."""

    def test_a_gate_without_a_source_raises_before_polling(self, monkeypatch):
        polled = []
        monkeypatch.setattr(flow_steps, "_poll_sms_code",
                            lambda *a, **k: polled.append((a, k)))
        with pytest.raises(_PayPalStepError) as excinfo:
            flow_steps._handle_sms_verification(
                _FakePage(), {"timeout": 120, "poll_interval": 5}, baseline="")
        assert excinfo.value.step == "sms_verify"
        assert excinfo.value.detail == NO_NUMBER_SOURCE
        assert polled == [], "an empty api_url must not reach the poll loop"

    def test_a_configured_source_still_polls(self, monkeypatch):
        """The guard must not break the path it is guarding."""
        polled = []
        monkeypatch.setattr(flow_steps, "_poll_sms_code",
                            lambda url, baseline, **k: polled.append(url) or "123456")
        monkeypatch.setattr(flow_steps, "_click_with_fallback", lambda *a, **k: None)
        page = _FakePage()
        # Filling the code needs a locator that reports not-visible on the
        # second pass; _FakePage returns visible for everything, so stub the
        # post-fill helpers instead of the page.
        monkeypatch.setattr(flow_steps.time, "sleep", lambda *a, **k: None)
        code = flow_steps._handle_sms_verification(
            page, {"api_url": "https://sms.example", "timeout": 120,
                   "poll_interval": 5}, baseline="")
        assert code == "123456"
        assert polled == ["https://sms.example"]


class TestReverseGate:
    """``paypal_reverse`` detects the gate via ``_sms_input_present``."""

    def _client(self, sms_cfg):
        return paypal_reverse.PayPalReverseClient(
            redirect_url="https://paypal.example/approve", card={}, address={},
            first_name="", last_name="", alias_email="", password="",
            phone="", sms_cfg=sms_cfg)

    def test_a_gate_without_a_source_raises_before_polling(self, monkeypatch):
        client = self._client({"api_url": ""})
        client._current_html = '<input name="smsCode">'
        polled = []
        monkeypatch.setattr(client, "_poll_sms",
                            lambda *a, **k: polled.append(a) or "123456")
        with pytest.raises(paypal_reverse._NeedBrowserFallback) as excinfo:
            client._handle_sms()
        assert excinfo.value.step == "sms"
        assert NO_NUMBER_SOURCE_MESSAGE in str(excinfo.value)
        assert polled == []

    def test_no_gate_means_no_error_even_without_a_source(self, monkeypatch):
        """The page has no code field, so nothing is required -- a missing
        source must not fail a payment that never needed SMS."""
        client = self._client({"api_url": ""})
        client._current_html = "<form>no sms here</form>"
        assert client._handle_sms() is None


class TestNodriverGate:
    """nodriver cannot tell whether a gate is present -- so it must not raise.

    ``_handle_sms`` clicks "Send Code" blind; it never probes the page for a
    code field. Raising on a missing source would therefore fail *every*
    payment, including the majority that never ask for SMS. It reports and
    carries on instead, which is the opposite of what ``flow_steps`` and
    ``paypal_reverse`` do -- deliberately, and for the reason above.
    """

    def test_a_missing_source_returns_none_instead_of_raising(self, monkeypatch, capsys):
        result = asyncio.run(
            nodriver_paypal._handle_sms(None, "", {}, deadline=0))
        assert result is None
        assert NO_NUMBER_SOURCE_MESSAGE in capsys.readouterr().out

    def test_the_message_names_the_cause(self, monkeypatch, capsys):
        asyncio.run(nodriver_paypal._handle_sms(None, "", {}, deadline=0))
        out = capsys.readouterr().out
        assert "no phone number source" in out

    def test_it_does_not_touch_the_page_when_there_is_no_source(self):
        """``page=None`` would raise AttributeError on any page access, so a
        clean return proves the early exit happens first."""
        assert asyncio.run(nodriver_paypal._handle_sms(None, "", {}, deadline=0)) is None

    def test_it_does_not_reach_the_network(self, monkeypatch):
        """No HTTP call may happen for an unconfigured source."""
        calls = []
        with mock.patch("requests.get", lambda *a, **k: calls.append(a)):
            asyncio.run(nodriver_paypal._handle_sms(None, "", {}, deadline=0))
        assert calls == []
