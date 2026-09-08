"""P1-1: the protocol path must apply the fingerprint pool's resolved geo.

``_apply_protocol_fingerprint`` used to take only ``profile.name`` from the pool
and drop the geo half, while the geo actually bound to the account came from
``infer_proxy_country`` -- a regex over the proxy username.  A residential proxy
carries no region token, so it resolved to ``""`` and the account kept a US
clock on a non-US exit: the pool had already measured the exit and the result
was thrown away.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool.auth_headers import _AUTH_FINGERPRINT_LOCAL, set_fingerprint_geo
from sms_tool.registration_handlers import (
    _apply_protocol_fingerprint,
    _new_registration_session,
)

# Not in auth_headers._GEO_PROFILES (only AU/CA/DE/FR/GB/JP/SG/US are curated),
# so resolving it used to land on the UTC/en-US default even when measured.
_UNCURATED = "BR"


@pytest.fixture(autouse=True)
def _restore_auth_fingerprint_local():
    """set_fingerprint_geo mutates a thread-local; do not leak it between tests."""
    saved = dict(_AUTH_FINGERPRINT_LOCAL.__dict__)
    yield
    _AUTH_FINGERPRINT_LOCAL.__dict__.clear()
    _AUTH_FINGERPRINT_LOCAL.__dict__.update(saved)


class _RecordingOps:
    """Stand-in for the bound RegistrationOperations facade."""

    def __init__(self):
        self.geo_calls: list[tuple] = []

    def set_fingerprint_geo(self, country="", **kwargs):
        self.geo_calls.append((country, kwargs))


def _profile(**overrides):
    data = {
        "name": "firefox144",
        "country": _UNCURATED,
        "timezone": "America/Sao_Paulo",
        "lang": "pt-BR",
        "lang_full": "pt-BR,pt;q=0.9",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_pool_geo_is_applied_instead_of_the_template_guess():
    ops = _RecordingOps()
    with (
        patch("sms_tool.fingerprint_pool.shared_fingerprint_pool",
              return_value=SimpleNamespace(size=1, next=lambda proxy: _profile())),
        patch("sms_tool.auth_headers.set_auth_fingerprint") as set_fp,
        patch("sms_tool.paypal_proxy.infer_proxy_country", return_value="") as infer,
    ):
        _apply_protocol_fingerprint(ops, {}, "http://user:pass@host:8080")

    set_fp.assert_called_once_with("firefox144")
    infer.assert_not_called()  # the measurement already answered this
    country, kwargs = ops.geo_calls[0]
    assert country == _UNCURATED
    assert kwargs["timezone"] == "America/Sao_Paulo"
    assert kwargs["lang"] == "pt-BR"
    assert kwargs["lang_full"] == "pt-BR,pt;q=0.9"


def test_falls_back_to_the_template_guess_when_the_pool_is_empty():
    ops = _RecordingOps()
    with (
        patch("sms_tool.fingerprint_pool.shared_fingerprint_pool",
              return_value=SimpleNamespace(size=0, next=lambda proxy: None)),
        patch("sms_tool.paypal_proxy.infer_proxy_country", return_value="JP"),
    ):
        _apply_protocol_fingerprint(ops, {}, "http://host:8080")
    assert ops.geo_calls == [("JP", {})]


def test_falls_back_when_the_pool_raises():
    ops = _RecordingOps()
    with (
        patch("sms_tool.fingerprint_pool.shared_fingerprint_pool",
              side_effect=RuntimeError("pool unavailable")),
        patch("sms_tool.paypal_proxy.infer_proxy_country", return_value="US"),
    ):
        _apply_protocol_fingerprint(ops, {}, "http://host:8080")
    assert ops.geo_calls[0][0] == "US"


class TestRegistrationSession:
    """P1-8: an explicit proxy must not be overridden by the environment."""

    def test_explicit_proxy_pins_the_session_and_disables_trust_env(self):
        session = _new_registration_session("http://user:pass@host:8080")
        assert session.proxies == {
            "http": "http://user:pass@host:8080",
            "https": "http://user:pass@host:8080",
        }
        assert session.trust_env is False

    def test_no_proxy_leaves_trust_env_alone(self):
        # With nothing configured an env-provided proxy still applies; the fix
        # is only about protecting an explicit choice from being overridden.
        session = _new_registration_session("")
        assert session.trust_env is True
        assert not session.proxies


def test_uncurated_country_defaults_to_utc_without_a_measurement():
    """The behaviour this replaced: an uncurated country lost its real clock."""
    assert set_fingerprint_geo(_UNCURATED)["timezone"] == "UTC"


def test_measured_timezone_overrides_the_uncurated_default():
    applied = set_fingerprint_geo(
        _UNCURATED, timezone="America/Sao_Paulo", lang="pt-BR",
    )
    assert applied["timezone"] == "America/Sao_Paulo"
    assert applied["lang"] == "pt-BR"


def test_blank_overrides_do_not_clobber_the_curated_entry():
    applied = set_fingerprint_geo("JP", timezone="", lang=None, lang_full="   ")
    assert applied["timezone"] == "Asia/Tokyo"
    assert applied["lang"] == "ja-JP"
