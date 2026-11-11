"""Per-account TLS ClientHello override (``sms_tool/tls_fingerprint.py``)."""

from __future__ import annotations

import pytest
from curl_cffi.const import CurlOpt

from sms_tool import tls_fingerprint as tls


@pytest.fixture(autouse=True)
def _clean_binding():
    """Reset both the explicit binding *and* the ambient auth-headers state.

    ``apply_to_session`` deliberately falls back to the thread's device seed and
    selected profile, which is how ``_new_registration_session`` gets the right
    order without an explicit bind. A test that wants "no binding" therefore has
    to clear that ambient state too: under ``-n 4 --dist loadfile`` a worker
    reuses one thread across files, so a registration test that left
    ``device_seed`` + ``firefox144`` behind made
    ``test_no_binding_leaves_the_session_untouched`` fail on CI (it passed when
    the file ran first). ``chrome146`` is the neutral profile because this module
    does not permute the Chrome family, so the derivation stays inactive.
    """
    from sms_tool.auth_headers import set_auth_fingerprint, set_fingerprint_device

    def _reset() -> None:
        tls.clear_tls_fingerprint()
        set_fingerprint_device("")
        set_auth_fingerprint("chrome146")

    _reset()
    yield
    _reset()


def _config(**overrides):
    return {"registration": {"tls_fingerprint": dict(overrides)}}


class TestPermuteExtensionOrder:
    def test_set_is_preserved_and_order_changes(self):
        base = tls.VERIFIED_EXTENSION_ORDERS["firefox"]
        order = tls.permute_extension_order(base, "acct-1")
        assert sorted(order.split("-")) == sorted(base.split("-"))
        assert order != base

    def test_server_name_stays_first(self):
        base = tls.VERIFIED_EXTENSION_ORDERS["firefox"]
        for seed in ("a", "b", "c", "d"):
            assert tls.permute_extension_order(base, seed).split("-")[0] == "0"

    def test_padding_stays_last(self):
        base = "0-23-65281-10-11-21"
        for seed in ("a", "b", "c"):
            parts = tls.permute_extension_order(base, seed).split("-")
            assert parts[-1] == "21"
            assert parts[0] == "0"

    def test_same_seed_same_order_and_different_seeds_differ(self):
        base = tls.VERIFIED_EXTENSION_ORDERS["firefox"]
        assert tls.permute_extension_order(base, "acct-1") == tls.permute_extension_order(base, "acct-1")
        assert tls.permute_extension_order(base, "acct-1") != tls.permute_extension_order(base, "acct-2")

    def test_degenerate_inputs_return_empty(self):
        assert tls.permute_extension_order("", "seed") == ""
        assert tls.permute_extension_order("0-1", "seed") == ""
        assert tls.permute_extension_order("0-1-2", "") == ""


class TestBuild:
    def test_firefox_gets_a_per_account_order(self):
        first = tls.build(profile="firefox144", seed="acct-1")
        second = tls.build(profile="firefox144", seed="acct-2")
        assert first.active and second.active
        assert first.rotated
        assert first.extension_order != second.extension_order
        assert "rotated" in first.summary()

    def test_unverified_families_keep_the_preset_order(self):
        # Safari's list cannot be permuted (BoringSSL rejects the ClientHello)
        # and Chrome's preset already permutes per handshake.  Both must fall
        # through rather than inherit the Firefox list.
        for profile in ("safari18_0", "safari18_0_ios", "chrome146"):
            fingerprint = tls.build(profile=profile, seed="acct-1")
            assert not fingerprint.active
            assert fingerprint.reason == "family-not-verifiable"
            assert "preset-order" in fingerprint.summary()

    def test_master_switch_disables_the_override(self):
        fingerprint = tls.build(profile="firefox144", seed="acct-1", config=_config(enabled=False))
        assert not fingerprint.active
        assert fingerprint.reason == "disabled"

    def test_rotate_switch_keeps_the_preset_order(self):
        fingerprint = tls.build(profile="firefox144", seed="acct-1", config=_config(rotate_extensions=False))
        assert not fingerprint.active
        assert fingerprint.reason == "rotate-off"

    def test_missing_seed_is_not_an_error(self):
        fingerprint = tls.build(profile="firefox144", seed="")
        assert not fingerprint.active
        assert fingerprint.reason == "no-seed"

    def test_seed_salt_reshuffles_every_account(self):
        plain = tls.build(profile="firefox144", seed="acct-1")
        salted = tls.build(profile="firefox144", seed="acct-1", config=_config(seed_salt="v2"))
        assert plain.extension_order != salted.extension_order

    def test_string_booleans_are_accepted(self):
        fingerprint = tls.build(profile="firefox144", seed="acct-1", config=_config(enabled="off"))
        assert not fingerprint.active
        assert tls.TlsFingerprintConfig.from_config(_config(rotate_extensions="yes")).rotate_extensions


class _FakeSession:
    def __init__(self, options: dict | None = None) -> None:
        self.curl_options: dict = dict(options or {})


class _FrozenSession:
    """A session whose ``curl_options`` cannot be replaced (a non-dict proxy etc.)."""

    curl_options: dict = {}

    def __setattr__(self, name: str, value: object) -> None:
        raise RuntimeError("read only")


class TestApplyToSession:
    def test_installs_the_option_on_the_session(self):
        tls.set_tls_fingerprint("acct-1", profile="firefox144")
        session = _FakeSession()
        fingerprint = tls.apply_to_session(session)
        assert fingerprint.active
        assert session.curl_options[CurlOpt.TLS_EXTENSION_ORDER] == fingerprint.extension_order

    def test_merges_instead_of_replacing_existing_options(self):
        tls.set_tls_fingerprint("acct-1", profile="firefox144")
        session = _FakeSession({"sentinel": "keep"})
        tls.apply_to_session(session)
        assert session.curl_options["sentinel"] == "keep"
        assert CurlOpt.TLS_EXTENSION_ORDER in session.curl_options

    def test_no_binding_leaves_the_session_untouched(self):
        session = _FakeSession()
        fingerprint = tls.apply_to_session(session)
        assert not fingerprint.active
        assert session.curl_options == {}

    def test_a_session_that_rejects_the_option_is_not_fatal(self):
        tls.set_tls_fingerprint("acct-1", profile="firefox144")
        fingerprint = tls.apply_to_session(_FrozenSession())
        assert not fingerprint.active
        assert fingerprint.reason.startswith("install-failed:")

    def test_current_fingerprint_rebuilds_when_the_profile_changes(self):
        from sms_tool.auth_headers import set_auth_fingerprint, set_fingerprint_device

        set_fingerprint_device("acct-1")
        set_auth_fingerprint("firefox144")
        first = tls.current_tls_fingerprint()
        set_auth_fingerprint("chrome146")
        second = tls.current_tls_fingerprint()
        assert first.active
        assert not second.active


class TestVerifiedTable:
    def test_only_measured_families_are_listed(self):
        # Guards the two decisions that were measured the hard way: Firefox is
        # safe to permute, Safari is not (``curl: (35) Invalid TLS extensions``).
        assert "firefox" in tls.VERIFIED_EXTENSION_ORDERS
        assert "safari" not in tls.VERIFIED_EXTENSION_ORDERS

    def test_firefox_order_matches_the_preset_shape(self):
        order = tls.VERIFIED_EXTENSION_ORDERS["firefox"].split("-")
        assert order[0] == "0"
        assert "21" not in order  # Firefox sends no padding extension
        assert len(order) >= 10
