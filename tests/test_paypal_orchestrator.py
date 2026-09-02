"""Behaviour tests for ``sms_tool/paypal/orchestrator.py`` (round-5 audit target).

Why this file exists
--------------------
``orchestrator`` decides *how* a real charge is attempted (reverse protocol ->
nodriver -> Camoufox/CloakBrowser) and, crucially, *what gets written back to
the session file* afterwards.  Two things make it worth pinning:

1. **The fallback chain.** A failed strategy must leave ``result`` untouched for
   the next one; ``reverse_only`` must hard-stop before any browser is launched.
   Getting this wrong means either a browser is launched when it shouldn't be,
   or a browser is launched twice for one payment.

2. **The persistence branch.** On success *and* on failure the session file is
   rewritten, and on failure ``paypal_status`` is derived by ``split(":")[0]`` —
   so an error string without a colon silently becomes the whole message.  That
   is what ends up in the operator's account record.

Network, browser launches and the seed-file loader are all replaced with small
seams; the orchestration logic itself runs for real.
"""

from __future__ import annotations

import sys

import pytest

from sms_tool.paypal import orchestrator

# Captured before the fixtures replace it, so the "real behaviour" tests can
# restore the un-stubbed implementation.
_REAL_TRY_REVERSE_PAY = orchestrator._try_reverse_pay


# ─────────────────────────────── seams ───────────────────────────────────────


class Recorder:
    """Collects every call the orchestrator makes across a fake boundary."""

    def __init__(self):
        self.calls = []

    def __call__(self, name, result=None, error=None):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if error:
                raise error
            return result
        return _fn


@pytest.fixture
def rec(monkeypatch):
    """Patch every external boundary of auto_pay with recording stubs."""
    r = Recorder()
    monkeypatch.setattr(orchestrator, "_load_seed",
                        lambda **k: ({"email": "buyer@example.com",
                                      "paypal": {"url": "https://paypal.example/approve"},
                                      "access_token": "token-fixture"},
                                     "sessions/fixture.json"))
    monkeypatch.setattr(orchestrator, "_extract_access_token", lambda data: data.get("access_token"))
    monkeypatch.setattr(orchestrator, "_pick_card_and_address", r("card", ({"number": "4242"}, {})))
    monkeypatch.setattr(orchestrator, "_pick_phone_and_sms", r("phone", ("5550100134", "https://sms.example")))
    monkeypatch.setattr(orchestrator, "_random_name", r("name", ("Ada", "Lovelace")))
    monkeypatch.setattr(orchestrator, "_generate_password", r("password", "pw-fixture"))
    monkeypatch.setattr(orchestrator, "_generate_alias_email", r("alias", "alias@example.com"))
    monkeypatch.setattr(orchestrator, "_save_paypal_result", r("save", "sessions/saved.json"))
    monkeypatch.setattr(orchestrator, "generate_pp_link",
                        r("gen_link", {"ok": True, "url": "https://paypal.example/generated"}))
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", r("reverse", {"ok": True}))
    monkeypatch.setattr(orchestrator, "_try_nodriver_pay", r("nodriver", {"ok": False}))
    monkeypatch.setattr(orchestrator, "_try_browser_pay", r("browser", {"ok": False}))
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1_700_000_000)
    return r


PAY_CFG = {"reverse_engineering": True, "sms_poll_interval": 5,
           "sms_timeout": 120, "human_verification_timeout": 300}


@pytest.fixture
def configured(monkeypatch):
    """CFG.get("paypal_auto") returns a minimal paypal_auto block."""
    monkeypatch.setattr(orchestrator.CFG, "get",
                        lambda key, default=None: dict(PAY_CFG) if key == "paypal_auto" else default)


def with_pay_cfg(monkeypatch, **overrides):
    block = {**PAY_CFG, **overrides}
    monkeypatch.setattr(orchestrator.CFG, "get",
                        lambda key, default=None: dict(block) if key == "paypal_auto" else default)


def names(rec):
    return [name for name, _args, _kwargs in rec.calls]


def args_of(rec, name):
    return [args for n, args, _kwargs in rec.calls if n == name]


# ─────────────────────────── the configuration gate ──────────────────────────


def test_auto_pay_refuses_to_run_without_a_paypal_auto_config(monkeypatch):
    monkeypatch.setattr(orchestrator.CFG, "get", lambda key, default=None: None)
    assert orchestrator.auto_pay() == {
        "ok": False, "error": "paypal_auto not configured in config.json"}


def test_auto_pay_does_not_launch_anything_when_unconfigured(monkeypatch, rec):
    monkeypatch.setattr(orchestrator.CFG, "get", lambda key, default=None: None)
    orchestrator.auto_pay()
    assert names(rec) == []


# ──────────────────────────── the happy path ─────────────────────────────────


def test_auto_pay_returns_ok_and_persists_on_success(rec, configured):
    result = orchestrator.auto_pay(email="Buyer@Example.com")
    assert result["ok"] is True
    assert "save" in names(rec)


def test_auto_pay_skips_the_browser_engines_when_reverse_succeeds(rec, configured):
    orchestrator.auto_pay()
    assert names(rec).count("reverse") == 1
    assert "nodriver" not in names(rec)
    assert "browser" not in names(rec)


def test_auto_pay_normalizes_the_email_before_use(rec, configured):
    orchestrator.auto_pay(email="  Buyer@Example.COM  ")
    assert args_of(rec, "alias") == [("buyer@example.com",)]


def test_auto_pay_writes_the_normalized_email_back_onto_the_seed(rec, configured):
    saved = []
    orchestrator._save_paypal_result = lambda data, path: saved.append(dict(data)) or "p.json"
    orchestrator.auto_pay(email="Buyer@Example.COM")
    assert saved[0]["email"] == "buyer@example.com"


def test_auto_pay_writes_the_tokens_back_onto_the_seed(rec, configured):
    """_save_paypal_result receives the mutated seed as its first argument."""
    saved = []
    rec.calls.clear()
    monkeypatched = orchestrator._save_paypal_result

    def _capture(data, path):
        saved.append(dict(data))
        return "sessions/saved.json"

    orchestrator._save_paypal_result = _capture
    try:
        orchestrator.auto_pay()
    finally:
        orchestrator._save_paypal_result = monkeypatched

    assert saved and saved[0]["success"] is True
    assert saved[0]["paypal_status"] == "completed"
    assert saved[0]["paypal_completed_at"] == 1_700_000_000


def test_auto_pay_reuses_an_existing_paypal_url(rec, configured):
    orchestrator.auto_pay()
    assert "gen_link" not in names(rec)


def test_auto_pay_generates_a_url_when_the_seed_has_none(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_load_seed",
                        lambda **k: ({"email": "b@example.com", "access_token": "t"}, "p.json"))
    orchestrator.auto_pay()
    assert "gen_link" in names(rec)


def test_auto_pay_aborts_when_the_link_cannot_be_generated(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_load_seed",
                        lambda **k: ({"email": "b@example.com", "access_token": "t"}, "p.json"))
    monkeypatch.setattr(orchestrator, "generate_pp_link",
                        rec("gen_link", {"ok": False, "error": "boom"}))
    result = orchestrator.auto_pay()
    assert result["ok"] is False
    assert "paypal_link_generation_failed" in result["error"]
    assert "reverse" not in names(rec)


def test_auto_pay_aborts_when_the_seed_has_no_access_token(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_load_seed",
                        lambda **k: ({"email": "b@example.com"}, "p.json"))
    result = orchestrator.auto_pay()
    assert result == {"ok": False, "email": "b@example.com", "error": "missing_access_token"}


# ────────────────────────── the fallback chain ───────────────────────────────


def test_auto_pay_tries_nodriver_then_browser_when_reverse_fails(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", rec("reverse", {"ok": False, "error": "x"}))
    monkeypatch.setattr(orchestrator, "_try_nodriver_pay", rec("nodriver", {"ok": False}))
    monkeypatch.setattr(orchestrator, "_try_browser_pay", rec("browser", {"ok": True}))
    assert orchestrator.auto_pay()["ok"] is True
    assert names(rec) == ["card", "phone", "name", "password", "alias",
                          "reverse", "nodriver", "browser", "save"]


def test_auto_pay_stops_after_nodriver_succeeds(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", rec("reverse", {"ok": False}))
    monkeypatch.setattr(orchestrator, "_try_nodriver_pay", rec("nodriver", {"ok": True}))
    orchestrator.auto_pay()
    assert "browser" not in names(rec)


def test_auto_pay_reverse_only_never_launches_a_browser(rec, configured, monkeypatch):
    """reverse_only is a hard stop: the browser engines must not be touched."""
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", rec("reverse", {"ok": False, "error": "x"}))
    result = orchestrator.auto_pay(reverse_only=True)
    assert result["ok"] is False
    assert "nodriver" not in names(rec)
    assert "browser" not in names(rec)


def test_auto_pay_skips_reverse_entirely_when_disabled(rec, configured, monkeypatch):
    with_pay_cfg(monkeypatch, reverse_engineering=False)
    monkeypatch.setattr(orchestrator, "_try_nodriver_pay", rec("nodriver", {"ok": True}))
    orchestrator.auto_pay()
    assert "reverse" not in names(rec)
    assert "nodriver" in names(rec)


def test_auto_pay_survives_a_reverse_strategy_that_raises(rec, configured, monkeypatch):
    """AUDIT POINT: an exception from a strategy is NOT contained."""
    monkeypatch.setattr(orchestrator, "_try_reverse_pay",
                        rec("reverse", error=RuntimeError("reverse exploded")))
    with pytest.raises(RuntimeError):
        orchestrator.auto_pay()
    assert "save" not in names(rec)  # nothing persisted after the crash


# ───────────────────────── the failure persistence branch ────────────────────


def test_auto_pay_derives_paypal_status_from_the_error_prefix(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_try_reverse_pay",
                        rec("reverse", {"ok": False, "error": "step_card: selector missing"}))
    saved = []
    orchestrator._save_paypal_result = lambda data, path: saved.append(dict(data)) or "p.json"
    result = orchestrator.auto_pay(reverse_only=True)

    assert result["ok"] is False
    assert saved[0]["paypal_status"] == "step_card"
    assert saved[0]["success"] is False


def test_auto_pay_uses_the_whole_error_when_it_has_no_colon(rec, configured, monkeypatch):
    """AUDIT POINT: ``split(":")[0]`` leaks the full message when there's no colon.

    The account record then carries an arbitrary free-text status instead of a
    stable token, so downstream filtering on paypal_status breaks.
    """
    monkeypatch.setattr(orchestrator, "_try_reverse_pay",
                        rec("reverse", {"ok": False, "error": "card_declined_by_issuer"}))
    saved = []
    orchestrator._save_paypal_result = lambda data, path: saved.append(dict(data)) or "p.json"
    orchestrator.auto_pay(reverse_only=True)
    assert saved[0]["paypal_status"] == "card_declined_by_issuer"


def test_auto_pay_falls_back_to_payment_failed_for_an_empty_error(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", rec("reverse", {"ok": False}))
    saved = []
    orchestrator._save_paypal_result = lambda data, path: saved.append(dict(data)) or "p.json"
    orchestrator.auto_pay(reverse_only=True)
    assert saved[0]["paypal_status"] == "payment_failed"


def test_auto_pay_persists_the_failure_even_when_no_strategy_runs(rec, configured, monkeypatch):
    """reverse_only with reverse disabled -> result stays {ok: False} -> saved."""
    with_pay_cfg(monkeypatch, reverse_engineering=False)
    saved = []
    orchestrator._save_paypal_result = lambda data, path: saved.append(dict(data)) or "p.json"
    orchestrator.auto_pay(reverse_only=True)
    assert saved[0]["success"] is False


def test_auto_pay_defaults_the_success_fields_when_the_strategy_omits_them(rec, configured, monkeypatch):
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", rec("reverse", {"ok": True}))
    saved = []
    orchestrator._save_paypal_result = lambda data, path: saved.append(dict(data)) or "p.json"
    result = orchestrator.auto_pay()
    assert result["paypal_status"] == "completed"
    assert saved[0]["email"] == "buyer@example.com"


# ───────────────────────── _try_reverse_pay internals ────────────────────────


# The unbound `use_headless` on line 180 was fixed on 2026-09-02. The
# tests below now assert the working behaviour; two of them are explicit
# regression guards so the strategy cannot silently die again.


def test_try_reverse_pay_builds_the_sms_config_from_the_config_block(monkeypatch):
    seen = {}
    monkeypatch.setattr(orchestrator, "try_reverse_pay",
                        lambda **kwargs: seen.update(kwargs) or {"ok": True})
    orchestrator._try_reverse_pay(
        paypal_url="u", card={"number": "4242"}, address={}, first_name="A",
        last_name="B", alias_email="a@b.c", password="p", phone="555",
        sms_api_url="https://sms.example",
        cfg={"sms_poll_interval": 7, "sms_timeout": 42, "human_verification_timeout": 9})
    assert seen["sms_cfg"] == {
        "api_url": "https://sms.example", "phone": "555", "poll_interval": 7,
        "timeout": 42, "manual_human_verification": False,
        "human_verification_timeout": 9,
    }


def test_try_reverse_pay_does_not_raise_name_error(monkeypatch):
    """REGRESSION GUARD for a fixed bug (2026-09-02).

    Line 180 read ``use_headless``, which was never in scope in this function -
    it only existed as a local of ``_try_browser_pay``. Every reverse-protocol
    payment therefore raised NameError on its first line, before
    ``try_reverse_pay`` was ever called. ``auto_pay`` has no handler for that,
    so the *preferred* payment strategy was 100% dead and the NameError
    propagated instead of falling back to nodriver or the browser engines.
    """
    monkeypatch.setattr(orchestrator, "try_reverse_pay", lambda **k: {"ok": True})
    result = orchestrator._try_reverse_pay(
        paypal_url="u", card={"number": "4242"}, address={}, first_name="A",
        last_name="B", alias_email="a@b.c", password="p", phone="555",
        sms_api_url="https://sms.example", cfg={})
    assert result["ok"] is True
    # A successful reverse run fills in the session defaults.
    assert result["paypal_status"] == "completed"
    assert result["card_last4"] == "4242"


def test_try_reverse_pay_honours_an_explicit_manual_verification_flag(monkeypatch):
    """An explicit config value must win over the headless-derived default."""
    seen = {}
    monkeypatch.setattr(orchestrator, "try_reverse_pay",
                        lambda **kwargs: seen.update(kwargs) or {"ok": True})
    orchestrator._try_reverse_pay(
        paypal_url="u", card={"number": "4242424242424242"}, address={}, first_name="", last_name="",
        alias_email="", password="", phone="", sms_api_url="https://sms.example",
        cfg={"manual_human_verification": True})
    assert seen["sms_cfg"]["manual_human_verification"] is True


def test_try_reverse_pay_reaches_the_protocol_client(monkeypatch):
    """The preferred strategy must actually be attempted, not die on line 1."""
    called = []
    monkeypatch.setattr(orchestrator, "try_reverse_pay",
                        lambda **k: called.append(k) or {"ok": True})
    orchestrator._try_reverse_pay(
        paypal_url="u", card={"number": "4242424242424242"}, address={}, first_name="", last_name="",
        alias_email="", password="", phone="", sms_api_url="", cfg={})
    assert len(called) == 1, (
        "try_reverse_pay was never invoked - the reverse strategy is dead again"
    )


def test_try_reverse_pay_defaults_to_a_300_second_verification_window(monkeypatch):
    seen = {}
    monkeypatch.setattr(orchestrator, "try_reverse_pay",
                        lambda **kwargs: seen.update(kwargs) or {"ok": True})
    orchestrator._try_reverse_pay(
        paypal_url="u", card={"number": "4242424242424242"}, address={}, first_name="", last_name="",
        alias_email="", password="", phone="", sms_api_url="", cfg={})
    assert seen["sms_cfg"]["human_verification_timeout"] == 300
    assert seen["sms_cfg"]["manual_human_verification"] is False


def test_try_reverse_pay_sets_the_success_defaults(monkeypatch):
    monkeypatch.setattr(orchestrator, "try_reverse_pay", lambda **k: {"ok": True})
    out = orchestrator._try_reverse_pay(
        paypal_url="u", card={"number": "4242424242424242"}, address={}, first_name="",
        last_name="", alias_email="a@b.c", password="p", phone="", sms_api_url="", cfg={})
    assert out["paypal_status"] == "completed"
    assert out["alias_email"] == "a@b.c"
    assert out["card_last4"] == "4242"
    assert out["password"] == "p"


def test_try_reverse_pay_is_the_first_strategy_the_orchestrator_reaches(rec, configured, monkeypatch):
    """End-to-end: a working reverse strategy short-circuits the browser engines.

    With the real `_try_reverse_pay` restored, a successful reverse protocol must
    be recorded and persisted, and neither nodriver nor a browser may launch.
    (While `use_headless` was unbound this raised NameError and nothing at all
    was persisted - no record that a payment was ever attempted.)
    """
    monkeypatch.setattr(orchestrator, "_try_reverse_pay", _REAL_TRY_REVERSE_PAY)
    monkeypatch.setattr(orchestrator, "try_reverse_pay", lambda **k: {"ok": True})
    result = orchestrator.auto_pay()
    assert result["ok"] is True, (
        "the preferred reverse strategy failed - check `_try_reverse_pay` still "
        "reaches the protocol client"
    )
    assert "nodriver" not in names(rec)
    assert "browser" not in names(rec)
    assert "save" in names(rec)


# ─────────────────────────── nodriver / browser seams ────────────────────────


def test_try_nodriver_pay_closes_the_proxy_bridge_even_on_success(monkeypatch):
    closed = []
    import types

    # `_try_nodriver_pay` imports both modules lazily *inside* the function, and
    # from `sms_tool.` (two dots), not `sms_tool.paypal.`. Injecting the
    # `sms_tool.paypal.*` names silently does nothing: the real modules load,
    # see that nodriver is absent and return ok=False, so the assertion below
    # fails for a reason that has nothing to do with what it is testing.
    # Patch the name the import statement actually resolves.
    fake_mod = types.SimpleNamespace(
        proxy_for_browser=lambda proxy: ("socks5://127.0.0.1:1", lambda: closed.append(True)))
    monkeypatch.setitem(sys.modules, "sms_tool.proxy_bridge", fake_mod)

    nodriver = types.SimpleNamespace(run_nodriver_pay=lambda **k: {"ok": True})
    monkeypatch.setitem(sys.modules, "sms_tool.nodriver_paypal", nodriver)

    out = orchestrator._try_nodriver_pay(
        paypal_url="u", card={"number": "4242"}, address={}, first_name="", last_name="",
        alias_email="", password="", phone="", sms_api_url="", cfg={})
    assert out["ok"] is True
    assert closed == [True]


def test_try_nodriver_pay_closes_the_proxy_bridge_even_when_the_run_raises(monkeypatch):
    closed = []
    import types

    fake_mod = types.SimpleNamespace(
        proxy_for_browser=lambda proxy: ("socks5://127.0.0.1:1", lambda: closed.append(True)))
    monkeypatch.setitem(sys.modules, "sms_tool.proxy_bridge", fake_mod)

    def _boom(**k):
        raise RuntimeError("nodriver exploded")

    monkeypatch.setitem(sys.modules, "sms_tool.nodriver_paypal",
                        types.SimpleNamespace(run_nodriver_pay=_boom))

    with pytest.raises(RuntimeError):
        orchestrator._try_nodriver_pay(
            paypal_url="u", card={}, address={}, first_name="", last_name="",
            alias_email="", password="", phone="", sms_api_url="", cfg={})
    assert closed == [True]


def test_try_browser_pay_cloakbrowser_reports_a_clear_error_when_not_installed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_cloakbrowser(name, *args, **kwargs):
        if name == "cloakbrowser":
            raise ImportError("No module named 'cloakbrowser'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_cloakbrowser)
    out = orchestrator._try_browser_pay_cloakbrowser(
        paypal_url="u", card={"number": "4242"}, address={}, first_name="",
        last_name="", alias_email="", password="", sms_cfg={}, debug_dir="",
        debug_enabled=False, use_headless=False, browser_proxy=None,
        cookie_header="", cfg={})
    assert out["ok"] is False
    assert "browser_not_installed" in out["error"]


def _capture_sms_cfg(monkeypatch, cfg_block, headless):
    """Drive _try_browser_pay and return the sms_cfg it handed to the engine."""
    import types

    captured = {}

    def _capture(paypal_url, card, address, first_name, last_name, alias_email,
                 password, sms_cfg, *rest):
        captured["sms_cfg"] = dict(sms_cfg)
        return {"ok": False, "error": "x"}

    monkeypatch.setattr(orchestrator, "_try_browser_pay_cloakbrowser", _capture)
    monkeypatch.setitem(__import__("sys").modules, "sms_tool.paypal.proxy_bridge",
                        types.SimpleNamespace(proxy_for_browser=lambda p: (None, lambda: None)))

    # _try_browser_pay reads everything from its `cfg` argument (not CFG).
    # Pin browser_engine to cloakbrowser: the Camoufox default would launch a
    # real browser process, which these tests must never do.
    orchestrator._try_browser_pay(
        paypal_url="u", card={}, address={}, first_name="", last_name="",
        alias_email="", password="", phone="", sms_api_url="",
        cfg={**cfg_block, "browser_engine": "cloakbrowser"}, headless=headless)
    return captured["sms_cfg"]


def test_try_browser_pay_sms_config_picks_up_the_configured_flags(monkeypatch):
    sms_cfg = _capture_sms_cfg(
        monkeypatch,
        {"sms_poll_interval": 5, "sms_timeout": 120, "debug_dir": "d",
         "debug_screenshots": False, "human_verification_timeout": 77},
        headless=True)
    assert sms_cfg["poll_interval"] == 5
    assert sms_cfg["timeout"] == 120
    assert sms_cfg["human_verification_timeout"] == 77


def test_try_browser_pay_disallows_manual_verification_when_headless(monkeypatch):
    """A headless run cannot wait on a human, so the gate must be off."""
    sms_cfg = _capture_sms_cfg(monkeypatch, {"sms_poll_interval": 5, "sms_timeout": 120},
                               headless=True)
    assert sms_cfg["manual_human_verification"] is False


def test_try_browser_pay_allows_manual_verification_when_headed(monkeypatch):
    """The inverse: a headed run defaults to allowing manual intervention."""
    sms_cfg = _capture_sms_cfg(monkeypatch, {"sms_poll_interval": 5, "sms_timeout": 120},
                               headless=False)
    assert sms_cfg["manual_human_verification"] is True
    assert sms_cfg["human_verification_timeout"] == 300
