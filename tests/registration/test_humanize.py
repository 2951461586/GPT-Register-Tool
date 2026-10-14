"""Unit tests for randomized operation pacing (P3 risk-control).

The critical property: **disabled must reproduce the historical fixed sleep
exactly**, so turning the feature off can never break a registration that works
today.  Enabled must stay inside the declared jitter band around that baseline.
"""

import unittest
from unittest.mock import patch

from sms_tool.humanize import (
    HUMANIZE_DELAYS,
    delay,
    humanize_config,
)


def _enabled(**overrides):
    cfg = {"enabled": True}
    cfg.update(overrides)
    return {"registration": {"humanize": cfg}}


class HumanizeConfigTests(unittest.TestCase):
    def test_defaults_disabled(self):
        for cfg in (None, {}, {"registration": {}}):
            self.assertFalse(humanize_config(cfg)["enabled"])
            self.assertEqual(humanize_config(cfg)["factor"], 1.0)

    def test_reads_registration_section(self):
        cfg = humanize_config({"registration": {"humanize": {"enabled": True, "factor": 2.0}}})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["factor"], 2.0)

    def test_invalid_factor_falls_back_to_one(self):
        for bad in ("abc", 0, -3, None):
            self.assertEqual(humanize_config({"registration": {"humanize": {"factor": bad}}})["factor"], 1.0)


class HumanizeDelayTests(unittest.TestCase):
    def _recorded(self, kind="default", config=None, times=40):
        sleeps: list[float] = []
        with patch("sms_tool.humanize.time.sleep", side_effect=lambda s: sleeps.append(s)):
            for _ in range(times):
                delay(kind, config=config)
        return sleeps

    def test_disabled_reproduces_baseline_exactly(self):
        for kind, (baseline, _jitter) in HUMANIZE_DELAYS.items():
            sleeps = self._recorded(kind, config={}, times=5)
            for value in sleeps:
                self.assertEqual(value, baseline, msg=f"kind={kind}")

    def test_enabled_stays_inside_jitter_band(self):
        for kind, (baseline, jitter) in HUMANIZE_DELAYS.items():
            lo = baseline * (1.0 - jitter)
            hi = baseline * (1.0 + jitter)
            for value in self._recorded(kind, config=_enabled()):
                self.assertGreaterEqual(value, lo - 1e-9, msg=f"kind={kind}")
                self.assertLessEqual(value, hi + 1e-9, msg=f"kind={kind}")

    def test_enabled_actually_randomizes(self):
        # Guard against a refactor that silently makes the interval constant.
        sleeps = self._recorded("page_settle", config=_enabled(), times=40)
        self.assertGreater(len(set(sleeps)), 1)

    def test_factor_scales_interval(self):
        base = HUMANIZE_DELAYS["click"][0]
        jitter = HUMANIZE_DELAYS["click"][1]
        for value in self._recorded("click", config=_enabled(factor=2.0)):
            self.assertGreaterEqual(value, base * 2 * (1 - jitter) - 1e-9)
            self.assertLessEqual(value, base * 2 * (1 + jitter) + 1e-9)

    def test_unknown_kind_falls_back_to_default(self):
        baseline, _ = HUMANIZE_DELAYS["default"]
        for value in self._recorded("no_such_kind", config={}, times=3):
            self.assertEqual(value, baseline)

    def test_explicit_baseline_and_jitter_override(self):
        sleeps = self._recorded(
            "default", config=_enabled(), times=1,
        )
        self.assertTrue(sleeps)
        # Explicit override wins over the table.
        with patch("sms_tool.humanize.time.sleep") as sleeper:
            delay("default", config=_enabled(), baseline=0.01, jitter=0.0)
        sleeper.assert_called_once()
        self.assertAlmostEqual(sleeper.call_args[0][0], 0.01, places=6)

    def test_never_negative(self):
        for value in self._recorded("click", config=_enabled(factor=0.0001)):
            self.assertGreaterEqual(value, 0.0)

    def test_returns_seconds_slept(self):
        with patch("sms_tool.humanize.time.sleep"):
            self.assertEqual(delay("click", config={}), HUMANIZE_DELAYS["click"][0])


if __name__ == "__main__":
    unittest.main()
