"""Contract tests for the opt-in geo gate wired into ideal/twint.

The gate is OFF by default (a behaviour change must be opted into). These tests
pin both halves: default-OFF is a no-op (no network, no exception), and when the
env flag is on the workflow consults the shared ``common.geo`` implementation.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_PROTO = str(_ROOT / "services" / "protocol-payment")
if _PROTO not in sys.path:
    sys.path.insert(0, _PROTO)

import common.endpoints  # noqa: E402,F401  (registers the package)
from common import geo  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


IDEAL = _load("ideal_gate", _ROOT / "services/protocol-payment/ideal/ideal_qr_extract.py")
TWINT = _load("twint_gate", _ROOT / "services/protocol-payment/twint/twint_extract.py")


class DefaultOffTests(unittest.TestCase):
    def test_ideal_default_off_is_noop(self):
        os.environ.pop("IDEAL_PROXY_GEO_CHECK", None)
        # No network, no state: must return immediately.
        IDEAL.maybe_check_proxy_geo("http://a:1", "http://a:1")

    def test_twint_default_off_is_noop(self):
        os.environ.pop("TWINT_PROXY_GEO_CHECK", None)
        TWINT.maybe_check_proxy_geo("http://a:1", "http://a:1")


class OptInTests(unittest.TestCase):
    def test_ideal_opt_in_calls_shared_geo(self):
        with patch.object(IDEAL, "env_bool", side_effect=lambda n, d=False: n == "IDEAL_PROXY_GEO_CHECK"):
            with patch.object(geo, "ensure_proxy_country") as mock_country:
                IDEAL.maybe_check_proxy_geo("http://a:1", "http://a:1")
                mock_country.assert_called_once()
                self.assertEqual(mock_country.call_args[1]["env_prefix"], "IDEAL")

    def test_twint_opt_in_calls_shared_geo(self):
        with patch.object(TWINT, "env_bool", side_effect=lambda n, d=False: n == "TWINT_PROXY_GEO_CHECK"):
            with patch.object(geo, "ensure_proxy_country") as mock_country:
                TWINT.maybe_check_proxy_geo("http://a:1", "http://a:1")
                mock_country.assert_called_once()
                self.assertEqual(mock_country.call_args[1]["env_prefix"], "TWINT")

    def test_target_check_requires_its_own_flag(self):
        # GEO on but TARGET off → only the country check runs.
        def _env(n, d=False):
            return n == "TWINT_PROXY_GEO_CHECK"

        with patch.object(TWINT, "env_bool", side_effect=_env):
            with patch.object(geo, "ensure_proxy_country"), patch.object(
                geo, "ensure_proxy_targets"
            ) as mock_targets:
                TWINT.maybe_check_proxy_geo("http://a:1", "http://a:1")
                mock_targets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
