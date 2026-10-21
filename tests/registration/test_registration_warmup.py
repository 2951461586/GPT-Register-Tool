"""Protocol-lane post-signup warm-up (registration.chatgpt_bootstrap.enabled)."""

from __future__ import annotations

import unittest
from typing import Any
from types import SimpleNamespace

from sms_tool.registration import warmup as warmup


class _Session:
    def __init__(self, statuses=None, raises=False):
        self.calls = []
        self._statuses = list(statuses or [])
        self._raises = raises

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raises:
            raise RuntimeError("boom")
        code = self._statuses.pop(0) if self._statuses else 200
        return SimpleNamespace(status_code=code)


def _workflow(config=None, *, success=True, token="at", session=None, country="IN"):
    runtime = SimpleNamespace(
        success=success,
        access_token=token,
        session=session if session is not None else _Session(),
        chat_base="https://chatgpt.example",
        device_id="did-1",
    )
    return SimpleNamespace(runtime=runtime, config=config or {}, fingerprint_country=country)


ON = {"registration": {"chatgpt_bootstrap": {"enabled": True}, "post_registration_dwell_seconds_range": "18,45"}}


def _run(wf, **kwargs):
    sleeps = []
    result = warmup.run_post_signup_warmup(wf, sleep_fn=sleeps.append, uniform_fn=lambda lo, hi: hi, **kwargs)
    return result, sleeps


class WarmupToggleTests(unittest.TestCase):
    def test_default_is_off_and_sends_nothing(self):
        for config in ({}, {"registration": {}}, {"registration": {"chatgpt_bootstrap": {"enabled": False}}}):
            wf = _workflow(config)
            result, sleeps = _run(wf)
            self.assertEqual(result, {"skipped": True, "reason": "disabled"})
            self.assertEqual(wf.runtime.session.calls, [])
            self.assertEqual(sleeps, [])

    def test_authenticated_false_keeps_it_off(self):
        config = {"registration": {"chatgpt_bootstrap": {"enabled": True, "authenticated": False}}}
        self.assertFalse(warmup.warmup_enabled(config))

    def test_string_values_are_parsed_two_sided(self):
        self.assertTrue(warmup.warmup_enabled({"registration": {"chatgpt_bootstrap": {"enabled": "true"}}}))
        self.assertFalse(warmup.warmup_enabled({"registration": {"chatgpt_bootstrap": {"enabled": "false"}}}))

    def test_failed_registration_or_missing_token_is_skipped(self):
        # Annotated so ``**kwargs`` does not collapse to ``dict[str, bool | str]``:
        # each case overrides a *different* keyword, and pyright unions the two
        # literal shapes into one value type.
        cases: tuple[dict[str, Any], ...] = ({"success": False}, {"token": ""})
        for kwargs in cases:
            wf = _workflow(ON, **kwargs)
            result, _ = _run(wf)
            self.assertEqual(result["reason"], "no_session")
            self.assertEqual(wf.runtime.session.calls, [])


class WarmupWalkTests(unittest.TestCase):
    def test_walks_every_path_read_only_with_bearer_and_dwells(self):
        wf = _workflow(ON)
        result, sleeps = _run(wf)
        calls = wf.runtime.session.calls
        self.assertEqual(len(calls), len(warmup.WARMUP_PATHS))
        self.assertTrue(all(url.startswith("https://chatgpt.example/backend-api/") for url, _ in calls))
        self.assertTrue(all(kw["headers"]["Authorization"] == "Bearer at" for _, kw in calls))
        self.assertTrue(all(kw["allow_redirects"] is False for _, kw in calls))
        self.assertIn("timezone_offset_min=330", " ".join(url for url, _ in calls))
        self.assertTrue(result["ok"])
        self.assertEqual(result["ok_count"], len(warmup.WARMUP_PATHS))
        self.assertEqual(result["dwell_seconds"], 45.0)
        self.assertEqual(sleeps[-1], 45.0)

    def test_unknown_country_reports_utc(self):
        wf = _workflow(ON, country="ZZ")
        _run(wf)
        self.assertIn("timezone_offset_min=0", " ".join(url for url, _ in wf.runtime.session.calls))

    def test_consecutive_failures_stop_the_walk_but_still_dwell(self):
        wf = _workflow(ON, session=_Session(statuses=[200, 401, 401, 401]))
        result, sleeps = _run(wf)
        self.assertEqual(result["statuses"], [200, 401, 401, 401])
        self.assertFalse(result["ok"])
        self.assertEqual(sleeps[-1], 45.0)

    def test_transport_exceptions_never_escape(self):
        wf = _workflow(ON, session=_Session(raises=True))
        result, _ = _run(wf)
        self.assertEqual(result["statuses"], [0, 0, 0])
        self.assertFalse(result["ok"])

    def test_dwell_range_parsing(self):
        self.assertEqual(warmup.dwell_range(ON), (18.0, 45.0))
        self.assertEqual(
            warmup.dwell_range({"registration": {"post_registration_dwell_seconds_range": "45,18"}}), (18.0, 45.0)
        )
        self.assertEqual(
            warmup.dwell_range({"registration": {"post_registration_dwell_seconds_range": "x"}}), (0.0, 0.0)
        )
        self.assertEqual(warmup.dwell_range({}), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
