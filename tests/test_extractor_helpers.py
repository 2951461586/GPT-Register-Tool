"""Unit tests for services/protocol-payment/common/extractor_helpers.py.

Batch 5 of the protocol-payment extractor consolidation: the two proxy-error
classifiers were moved here from ideal / twint (and were already byte-identical
in blik) because they are pure and provider-free.  These tests pin the marker
sets so a one-sided edit cannot silently change which failures remove a seed.

The differential proof of equivalence lives in
``runtime/tmp/_proxy_state_diff_verify_b5.py``.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_COMMON = Path(__file__).resolve().parents[1] / "services" / "protocol-payment" / "common"
_SPEC = importlib.util.spec_from_file_location("extractor_helpers_under_test", _COMMON / "extractor_helpers.py")
assert _SPEC is not None and _SPEC.loader is not None
EH = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = EH
_SPEC.loader.exec_module(EH)


class ExtractorHelperTests(unittest.TestCase):
    def test_user_already_paid_marker(self):
        self.assertTrue(EH.is_user_already_paid_error("User is already paid"))
        self.assertFalse(EH.is_user_already_paid_error("something else"))
        self.assertFalse(EH.is_user_already_paid_error(None))

    def test_direct_remove_markers(self):
        for marker in (
            "proxy authentication",
            "proxy auth",
            "resolve proxy",
            "could not resolve proxy",
            "invalid proxy",
            "malformed proxy",
            "unsupported proxy",
            "http 407",
            "status 407",
        ):
            self.assertTrue(EH.is_direct_remove_proxy_error(marker), marker)
        self.assertFalse(EH.is_direct_remove_proxy_error("timeout"))

    def test_health_failure_markers(self):
        for marker in (
            "目标站不可达",
            "proxy-server",
            "connection reset",
            "recv failure",
            "timed out",
            "timeout",
            "curl: (28)",
            "curl: (35)",
            "curl: (56)",
            "http_502",
            "http_503",
            "http_504",
        ):
            self.assertTrue(EH.is_proxy_health_failure(marker), marker)
        self.assertFalse(EH.is_proxy_health_failure("invalid proxy"))

    def test_classifiers_ignore_empty_input(self):
        for value in ("", None):
            self.assertFalse(EH.is_direct_remove_proxy_error(value))
            self.assertFalse(EH.is_proxy_health_failure(value))


if __name__ == "__main__":
    unittest.main()
