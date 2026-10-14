"""Behaviour tests for ``sms_tool/sms_utils.py`` (2026-09-03, round 7).

102 lines, **zero test files import it** (AST audit). It was extracted from
``paypal_auto.py`` purely to break a circular import, so it has no test of its
own even though both ``paypal_auto`` and ``paypal_reverse`` depend on it.

Why it matters: ``_extract_sms_code`` decides **which digits in an SMS are the
OTP**. A false positive burns a verification attempt; a false negative stalls a
paying flow until timeout. Both cost money, and neither raises -- the function
just returns the wrong string or ``None``.

Patch seams (all module-level bindings inside ``sms_utils`` itself, so
``patch.object(sms_utils, ...)`` is effective):
* ``_requests`` -- the module does ``import requests as _requests``.
* ``time``     -- the module does ``import time`` and calls ``time.time()`` /
  ``time.sleep()`` **through the module object**, so the whole module attribute
  can be swapped for a fake clock (patching ``time.sleep`` globally would be a
  bad idea; patching ``sms_utils.time`` is surgical).

Quirks pinned, not fixed:
* ``_poll_sms_code`` stores ``_last_seen`` **as an attribute on the function**,
  i.e. it survives across calls. Two consecutive polls that see the same message
  return the code only the first time.
* The "same text as baseline" path is gated on ``attempt > 2``; the "text
  changed" path is not gated at all.
* A 4-digit code in 2000..2099 is rejected outright (it looks like a year), but
  a 6-digit code starting with 20 is accepted.
"""
from __future__ import annotations

import unittest
from unittest import mock

from sms_tool import sms_utils


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeRequests:
    """Stands in for the ``requests`` module; replays a scripted response list."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        item = self._responses.pop(0) if self._responses else _FakeResponse(200, "")
        if isinstance(item, BaseException):
            raise item
        return item

    def __getattr__(self, name):  # pragma: no cover - safety net
        raise AssertionError(f"unexpected requests attribute: {name}")


class _FakeClock:
    """A clock that only advances when the code under test sleeps."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ExtractSmsCodeTests(unittest.TestCase):
    """The money function: which digits are the OTP."""

    def test_empty_or_missing_text_gives_none(self):
        for text in ("", None):
            with self.subTest(text=text):
                self.assertIsNone(sms_utils._extract_sms_code(text))

    def test_keyword_pattern_matches_code_otp_verification(self):
        cases = [
            ("Your code is 123456", "123456"),
            ("OTP: 1234", "1234"),
            ("verification 12345", "12345"),
            ("verify: 123456", "123456"),
            ("code 987654", "987654"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(sms_utils._extract_sms_code(text), expected)

    def test_keyword_match_is_case_insensitive(self):
        self.assertEqual(sms_utils._extract_sms_code("CODE 1234"), "1234")
        self.assertEqual(sms_utils._extract_sms_code("Your Otp Is 123456"), "123456")

    def test_case_insensitivity_changes_which_number_wins(self):
        """⚠️ Pinned: ``re.IGNORECASE`` on the keyword pattern is load-bearing.

        Uppercase "CODE" is the normal spelling in real SMS. A case-sensitive
        pattern would miss it, and the fallback ladder would then hand back a
        *different* number -- same message, wrong OTP. (Mutation S03 removed
        IGNORECASE and every existing assertion still passed, because on the
        simple inputs both routes happen to agree.)
        """
        self.assertEqual(sms_utils._extract_sms_code("CODE 1111 is 2222"), "1111")

    def test_long_number_after_is_is_not_a_code(self):
        """⚠️ Pinned: the trailing ``(?:for|to|\\.|$``) boundary is what stops a
        7+ digit order/ref number from being truncated into a plausible OTP.

        Without it, ``(?:is|:)\\s*(\\d{4,6})`` turns "order is 12345678" into
        "123456" -- a false positive that burns a verification attempt. The
        standalone pattern rejects long digit runs on purpose; this boundary is
        what keeps the higher-priority pattern from undoing that. (Mutation S05.)
        """
        for text in ("order is 12345678", "reference is 1234567", "total: 12345678"):
            with self.subTest(text=text):
                self.assertIsNone(sms_utils._extract_sms_code(text))

    def test_keyword_requires_a_separator_before_the_digits(self):
        """``[:\\s]+`` -- "code123456" does not match the keyword pattern, but the
        standalone fallback still finds it. Pinned: both routes agree here."""
        self.assertEqual(sms_utils._extract_sms_code("code123456"), "123456")

    def test_is_or_colon_pattern_is_the_second_choice(self):
        """The first pattern cannot match "code is 493021" (a word follows the
        keyword), so the ``(?:is|:)`` pattern carries it."""
        self.assertEqual(sms_utils._extract_sms_code("Your verification code is 493021."), "493021")
        self.assertEqual(sms_utils._extract_sms_code("login: 4455 for PayPal"), "4455")

    def test_is_pattern_outranks_an_earlier_bare_number(self):
        """The ``(?:is|:)`` pattern runs before the standalone fallback, so it can
        pick a *later* number over an earlier bare one."""
        self.assertEqual(sms_utils._extract_sms_code("your pin 1234 is 5678"), "5678")

    def test_first_matching_pattern_wins(self):
        """Patterns are tried in order and ``search`` is leftmost-first, so a
        keyword hit earlier in the string beats a later ``is <code>`` hit."""
        text = "code 1111 ... is 2222"
        self.assertEqual(sms_utils._extract_sms_code(text), "1111")

    def test_standalone_digits_are_the_last_resort(self):
        self.assertEqual(sms_utils._extract_sms_code("no keywords here 654321"), "654321")

    def test_greedy_match_takes_the_longest_run(self):
        self.assertEqual(sms_utils._extract_sms_code("abc 123456 def"), "123456")

    def test_three_digits_are_never_a_code(self):
        self.assertIsNone(sms_utils._extract_sms_code("pin 123"))

    def test_seven_digit_run_is_rejected(self):
        """``(?![0-9-])`` blocks a match that is part of a longer digit run, and
        no shorter window satisfies it either -- so nothing is returned."""
        self.assertIsNone(sms_utils._extract_sms_code("ref 1234567 end"))

    def test_four_digit_years_2000_to_2099_are_rejected(self):
        """⚠️ Pinned: the year guard is ``len(code) == 4`` only."""
        for code in ("2000", "2024", "2099"):
            with self.subTest(code=code):
                self.assertIsNone(sms_utils._extract_sms_code(f"balance {code}"))

    def test_four_digits_outside_the_year_window_are_accepted(self):
        for code in ("1999", "2100", "4321"):
            with self.subTest(code=code):
                self.assertEqual(sms_utils._extract_sms_code(f"balance {code}"), code)

    def test_six_digit_code_starting_with_20_is_accepted(self):
        """⚠️ The year guard does not apply to 6-digit codes."""
        self.assertEqual(sms_utils._extract_sms_code("code 201234"), "201234")

    def test_dash_separated_runs_are_rejected(self):
        """``(?<![0-9-])`` / ``(?![0-9-])`` together rule out both halves."""
        self.assertIsNone(sms_utils._extract_sms_code("order 1234-5678"))

    def test_text_with_no_digits_gives_none(self):
        self.assertIsNone(sms_utils._extract_sms_code("no code in this message"))


class SmsBaselineTests(unittest.TestCase):
    def test_records_stripped_text_and_a_timestamp(self):
        with mock.patch.object(sms_utils, "_requests", _FakeRequests(_FakeResponse(200, "  hello  "))):
            result = sms_utils._sms_baseline("http://api.test/sms")
        self.assertEqual(result["raw"], "hello")
        self.assertGreater(result["timestamp"], 0)

    def test_non_200_leaves_the_baseline_empty(self):
        with mock.patch.object(sms_utils, "_requests", _FakeRequests(_FakeResponse(503, "boom"))):
            result = sms_utils._sms_baseline("http://api.test/sms")
        self.assertEqual(result["raw"], "")
        self.assertEqual(result["timestamp"], 0)

    def test_transport_errors_are_swallowed(self):
        with mock.patch.object(sms_utils, "_requests", _FakeRequests(ConnectionError("down"))):
            result = sms_utils._sms_baseline("http://api.test/sms")
        self.assertEqual(result, {"raw": "", "timestamp": 0})

    def test_request_uses_a_ten_second_timeout(self):
        fake = _FakeRequests(_FakeResponse(200, "x"))
        with mock.patch.object(sms_utils, "_requests", fake):
            sms_utils._sms_baseline("http://api.test/sms")
        self.assertEqual(fake.calls, [{"url": "http://api.test/sms", "timeout": 10}])


class PollSmsCodeTests(unittest.TestCase):
    def setUp(self):
        self._reset_last_seen()
        self.addCleanup(self._reset_last_seen)

    @staticmethod
    def _reset_last_seen():
        """``_last_seen`` lives on the function object -- see the module docstring."""
        if hasattr(sms_utils._poll_sms_code, "_last_seen"):
            del sms_utils._poll_sms_code._last_seen

    def _poll(self, responses, baseline=None, timeout=120, poll_interval=5):
        """Run ``_poll_sms_code`` with a fake transport and a fake clock."""
        fake = _FakeRequests(*responses)
        clock = _FakeClock()
        args = {
            "api_url": "http://api.test/sms",
            "baseline": baseline if baseline is not None else {"raw": "", "timestamp": 0},
            "timeout": timeout,
            "poll_interval": poll_interval,
        }
        with mock.patch.object(sms_utils, "_requests", fake), \
             mock.patch.object(sms_utils, "time", clock):
            result = sms_utils._poll_sms_code(**args)
        return result, fake, clock

    def test_content_change_returns_the_code_on_the_first_attempt(self):
        result, fake, clock = self._poll([_FakeResponse(200, "Your code is 123456")])
        self.assertEqual(result, "123456")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(clock.sleeps, [], "no sleep is needed when the first poll hits")

    def test_text_equal_to_the_baseline_is_ignored_for_the_first_two_attempts(self):
        """⚠️ Pinned: the "new message" branch is gated on ``attempt > 2``, so a
        stale message that equals the baseline is not mistaken for a new one."""
        responses = [_FakeResponse(200, "Your code is 123456")] * 3
        result, fake, clock = self._poll(responses, baseline={"raw": "Your code is 123456",
                                                              "timestamp": 0})
        self.assertEqual(result, "123456")
        self.assertEqual(len(fake.calls), 3, "attempts 1 and 2 must be skipped")

    def test_same_text_is_not_returned_twice_across_calls(self):
        """⚠️ Pinned: ``_last_seen`` is stored on the function, so it outlives the
        call. The second poll of an unchanged inbox yields nothing."""
        message = _FakeResponse(200, "Your code is 123456")
        baseline = {"raw": "Your code is 123456", "timestamp": 0}
        first, _, _ = self._poll([message] * 3, baseline=baseline)
        self.assertEqual(first, "123456")

        second, fake, clock = self._poll([message] * 30, baseline=baseline)
        self.assertIsNone(second, "the dedupe state leaks across calls -- pinned")
        self.assertEqual(len(fake.calls), 24,
                         "the second call burns the entire 120s window and gives up")
        self.assertEqual(clock.sleeps, [5] * 24)

    def test_timeout_returns_none_and_sleeps_once_per_attempt(self):
        result, fake, clock = self._poll([_FakeResponse(200, "nothing here")], timeout=20,
                                         poll_interval=5)
        self.assertIsNone(result)
        self.assertEqual(len(fake.calls), 4)          # 20s / 5s
        self.assertEqual(clock.sleeps, [5, 5, 5, 5])

    def test_zero_timeout_never_polls(self):
        result, fake, clock = self._poll([_FakeResponse(200, "code 1234")], timeout=0)
        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])
        self.assertEqual(clock.sleeps, [])

    def test_non_200_responses_are_ignored(self):
        result, fake, _ = self._poll([_FakeResponse(500, "code 1234"),
                                      _FakeResponse(200, "code 5678")])
        self.assertEqual(result, "5678")
        self.assertEqual(len(fake.calls), 2)

    def test_transport_errors_are_swallowed_and_polling_continues(self):
        result, fake, _ = self._poll([ConnectionError("down"),
                                      _FakeResponse(200, "code 5678")])
        self.assertEqual(result, "5678")
        self.assertEqual(len(fake.calls), 2)

    def test_empty_response_body_is_not_a_content_change(self):
        result, fake, _ = self._poll([_FakeResponse(200, ""),
                                      _FakeResponse(200, "code 5678")])
        self.assertEqual(result, "5678")
        self.assertEqual(len(fake.calls), 2)

    def test_changed_text_without_a_code_keeps_polling(self):
        result, fake, _ = self._poll([_FakeResponse(200, "welcome"),
                                      _FakeResponse(200, "code 5678")])
        self.assertEqual(result, "5678")
        self.assertEqual(len(fake.calls), 2)

    def test_request_uses_a_ten_second_timeout(self):
        _, fake, _ = self._poll([_FakeResponse(200, "code 1234")])
        self.assertEqual(fake.calls, [{"url": "http://api.test/sms", "timeout": 10}])


if __name__ == "__main__":
    unittest.main()
