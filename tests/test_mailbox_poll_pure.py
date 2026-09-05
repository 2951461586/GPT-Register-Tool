"""Behaviour tests for ``sms_tool/mailbox_poll.py`` (2026-09-03, round 7).

88 lines, three production call sites (``mailbox_cfworker.py:143``,
``mailbox_strategies.py:263``, ``providers/smailr_mailbox.py:409``) -- and
**zero real executions**. ``tests/test_smailr_provider.py`` mentions the symbol,
but only to ``patch`` it out with a stub. So the shared OTP polling template that
every mailbox provider goes through has never actually run under test.

It is the deadline / interval / settle-stability template for account
verification. A bug here does not raise: it returns a stale code, or silently
burns the whole timeout window and returns ``None``.

Patch seams:
* ``time`` -- the module does ``import time`` and calls ``time.time()`` /
  ``time.sleep()`` **through the module object**, so the whole attribute can be
  swapped for a fake clock. Every sleep below is measured, never real.
* ``fetch_candidate`` is a plain callable parameter -- no patching needed.
* ``mask_otp`` is left real on purpose: the "no plaintext OTP on stdout"
  assertion is only meaningful if the actual sanitiser runs.

Quirks pinned, not fixed:

* **A candidate with no ``otp`` key still burns a full settle window** before
  being discarded -- the settle loop runs before the "is there actually a code?"
  check.
* **The settle loop can blow the deadline and still return a code.** The inner
  loop stops at the deadline, but the ``return`` afterwards does not re-check it.
* ``excluded_otps`` is normalised with ``str(...).strip()``, so ``None`` in that
  list becomes ``""`` and ends up excluding empty-string candidates.
* ``reraise`` is written ``except reraise or ():`` -- a ``None`` reraise means
  "swallow everything", and a tuple only re-raises those exact types.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from sms_tool import mailbox_poll


class _FakeClock:
    """A clock that only advances when the code under test sleeps.

    ``max_sleeps`` is a runaway guard: if a mutation turns one of the polling
    loops into an infinite loop, this turns it into an AssertionError instead of
    hanging the whole test run.
    """

    def __init__(self, start: float = 1000.0, max_sleeps: int = 500):
        self.now = start
        self.sleeps: list[float] = []
        self._max = max_sleeps

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if len(self.sleeps) >= self._max:
            raise AssertionError(f"runaway polling loop: {len(self.sleeps)} sleeps")
        self.sleeps.append(seconds)
        self.now += seconds


class _Fetcher:
    """Scripted ``fetch_candidate``. The **last** response repeats forever."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self._responses:
            return None
        item = self._responses.pop(0)
        if not self._responses:
            self._responses.append(item)
        if isinstance(item, BaseException):
            raise item
        return item


class _PollCase(unittest.TestCase):
    def run_poll(self, responses, **kwargs):
        fetch = _Fetcher(*responses)
        clock = _FakeClock()
        out = io.StringIO()
        with mock.patch.object(mailbox_poll, "time", clock), redirect_stdout(out):
            result = mailbox_poll._poll_otp_with_settle(fetch, **kwargs)
        return _Outcome(result, clock, out.getvalue(), fetch)


class _Outcome:
    def __init__(self, result, clock, stdout, fetch):
        self.result = result
        self.clock = clock
        self.stdout = stdout
        self.fetch = fetch


DEFAULTS = {"timeout": 10, "interval": 2.0, "settle_seconds": 1.5}


class HappyPathTests(_PollCase):
    def test_returns_the_code_after_one_settle_window(self):
        out = self.run_poll([{"otp": "123456", "received_ts": 1}], **DEFAULTS)
        self.assertEqual(out.result, "123456")
        self.assertEqual(out.clock.sleeps, [1.5], "exactly one settle-length sleep")

    def test_settle_zero_returns_immediately_without_sleeping(self):
        out = self.run_poll([{"otp": "123456", "received_ts": 1}],
                            timeout=10, interval=2.0, settle_seconds=0)
        self.assertEqual(out.result, "123456")
        self.assertEqual(out.clock.sleeps, [])

    def test_zero_timeout_never_polls_at_all(self):
        out = self.run_poll([{"otp": "123456"}], timeout=0, interval=2.0, settle_seconds=1.5)
        self.assertIsNone(out.result)
        self.assertEqual(out.fetch.calls, 0)
        self.assertEqual(out.clock.sleeps, [])

    def test_success_line_is_masked(self):
        """⚠️ Security contract: the raw code must never reach stdout -- the WPF
        host persists every backend line to ``runtime/app_*.log``."""
        out = self.run_poll([{"otp": "123456", "received_ts": 1}], **DEFAULTS)
        self.assertEqual(out.result, "123456")
        self.assertNotIn("123456", out.stdout)
        self.assertIn(mailbox_poll.mask_otp("123456"), out.stdout)


class TimeoutTests(_PollCase):
    def test_no_candidate_ever_arrives(self):
        out = self.run_poll([None], **DEFAULTS)
        self.assertIsNone(out.result)
        self.assertEqual(out.clock.sleeps, [2.0] * 5, "10s / 2s = five polls")
        self.assertIn("timeout", out.stdout)

    def test_an_empty_inbox_is_not_an_error(self):
        """``if candidate and ...`` short-circuits on ``None``. Drop that guard and
        every empty poll raises AttributeError -- swallowed by the broad
        ``except``, but still spammed to the console as an error line. The
        no-mail-yet path must stay quiet."""
        out = self.run_poll([None], **DEFAULTS)
        self.assertNotIn("error", out.stdout)

    def test_custom_interval_is_respected(self):
        out = self.run_poll([None], timeout=2, interval=0.5, settle_seconds=0)
        self.assertIsNone(out.result)
        self.assertEqual(out.clock.sleeps, [0.5] * 4)

    def test_defaults_are_used_when_interval_and_settle_are_omitted(self):
        """``interval=None`` → 2.0, ``settle_seconds=None`` → 1.5."""
        out = self.run_poll([None], timeout=10)
        self.assertEqual(out.clock.sleeps, [2.0] * 5)

    def test_default_settle_is_one_and_a_half_seconds(self):
        out = self.run_poll([{"otp": "123456", "received_ts": 1}], timeout=10)
        self.assertEqual(out.clock.sleeps, [1.5])


class CandidateShapeTests(_PollCase):
    def test_a_candidate_without_an_otp_still_burns_the_settle_window(self):
        """⚠️ Pinned: the settle loop runs *before* the "is there a code?" check,
        so a candidate with no ``otp`` wastes a full settle window every poll."""
        out = self.run_poll([{"received_ts": 1}], **DEFAULTS)
        self.assertIsNone(out.result)
        settles = [s for s in out.clock.sleeps if s == 1.5]
        intervals = [s for s in out.clock.sleeps if s == 2.0]
        self.assertEqual(len(settles), 3)
        self.assertEqual(len(intervals), 3)
        self.assertEqual(out.clock.sleeps, [1.5, 2.0] * 3)

    def test_an_empty_string_otp_is_not_a_result(self):
        out = self.run_poll([{"otp": "", "received_ts": 1}], **DEFAULTS)
        self.assertIsNone(out.result)

    def test_a_non_dict_candidate_is_ignored(self):
        """Truthiness is checked with ``if candidate and ...`` -- a non-empty
        string would pass that, then explode on ``.get``."""
        out = self.run_poll(["not-a-dict"], **DEFAULTS)
        self.assertIn("error", out.stdout)


class ExcludedOtpTests(_PollCase):
    def test_excluded_codes_are_skipped_without_settling(self):
        out = self.run_poll([{"otp": "111111", "received_ts": 1}],
                            excluded_otps=["111111"], **DEFAULTS)
        self.assertIsNone(out.result)
        self.assertEqual(out.clock.sleeps, [2.0] * 5,
                         "an excluded candidate must not enter the settle loop")

    def test_excluded_values_are_stringified_and_stripped(self):
        out = self.run_poll([{"otp": "111111", "received_ts": 1}],
                            excluded_otps=["  111111  "], timeout=4, interval=2.0,
                            settle_seconds=1.5)
        self.assertIsNone(out.result)
        self.assertEqual(out.clock.sleeps, [2.0] * 2)

    def test_none_in_excluded_becomes_the_empty_string(self):
        """⚠️ Pinned: ``str(value or "").strip()`` turns ``None`` into ``""``, so
        listing ``None`` as excluded silently excludes empty-string codes too."""
        out = self.run_poll([{"otp": "", "received_ts": 1}], excluded_otps=[None],
                            timeout=4, interval=2.0, settle_seconds=1.5)
        self.assertIsNone(out.result)
        self.assertEqual(out.clock.sleeps, [2.0] * 2, "no settle -- it was excluded")

    def test_a_later_non_excluded_code_is_returned(self):
        out = self.run_poll([{"otp": "111111", "received_ts": 1},
                             {"otp": "222222", "received_ts": 2}],
                            excluded_otps=["111111"], **DEFAULTS)
        self.assertEqual(out.result, "222222")


class SettleStabilityTests(_PollCase):
    def test_a_newer_candidate_replaces_the_pending_one(self):
        """The whole point of the module: keep waiting while newer codes arrive."""
        out = self.run_poll([{"otp": "111111", "received_ts": 1},
                             {"otp": "222222", "received_ts": 5}], **DEFAULTS)
        self.assertEqual(out.result, "222222")
        self.assertEqual(out.clock.sleeps, [1.5, 1.5],
                         "the settle window is extended once by the newer code")

    def test_an_older_or_equal_candidate_does_not_replace_it(self):
        out = self.run_poll([{"otp": "111111", "received_ts": 5},
                             {"otp": "222222", "received_ts": 5}], **DEFAULTS)
        self.assertEqual(out.result, "111111")
        self.assertEqual(out.clock.sleeps, [1.5])

    def test_custom_is_newer_is_used(self):
        out = self.run_poll([{"otp": "111111", "received_ts": 1},
                             {"otp": "222222", "received_ts": 5}],
                            is_newer=lambda newer, older: False, **DEFAULTS)
        self.assertEqual(out.result, "111111")

    def test_the_settle_loop_is_bounded_by_the_deadline(self):
        """⚠️ Pinned: with an always-newer stream the settle window keeps being
        extended, so the *deadline* is the only thing that ends it."""
        out = self.run_poll([{"otp": "111111", "received_ts": 1},
                             {"otp": "222222", "received_ts": 5}],
                            is_newer=lambda newer, older: True, **DEFAULTS)
        self.assertEqual(out.result, "222222")
        self.assertTrue(out.clock.sleeps)
        self.assertEqual(set(out.clock.sleeps), {1.5})

    def test_code_is_returned_even_when_the_deadline_is_already_blown(self):
        """⚠️ Pinned: the settle loop stops at the deadline, but the ``return``
        afterwards does not re-check it -- so a code found just inside the window
        is still returned after it."""
        out = self.run_poll([{"otp": "123456", "received_ts": 1}],
                            timeout=1, interval=2.0, settle_seconds=1.5)
        self.assertEqual(out.result, "123456")
        self.assertGreater(out.clock.now, 1001.0, "we are past the deadline")


class ReraiseTests(_PollCase):
    def test_listed_exception_types_propagate(self):
        with self.assertRaises(ValueError):
            self.run_poll([ValueError("boom")], reraise=(ValueError,), **DEFAULTS)

    def test_unlisted_exception_types_are_swallowed(self):
        out = self.run_poll([ValueError("boom"), {"otp": "123456", "received_ts": 1}],
                            reraise=(KeyError,), **DEFAULTS)
        self.assertEqual(out.result, "123456")
        self.assertIn("boom", out.stdout)

    def test_no_reraise_swallows_everything(self):
        """``except reraise or ()`` -- a ``None`` reraise matches nothing."""
        out = self.run_poll([RuntimeError("boom"), {"otp": "123456", "received_ts": 1}],
                            **DEFAULTS)
        self.assertEqual(out.result, "123456")

    def test_the_log_prefix_appears_in_the_error_line(self):
        out = self.run_poll([RuntimeError("boom"), {"otp": "123456", "received_ts": 1}],
                            log_prefix="mailbox-7", **DEFAULTS)
        self.assertIn("mailbox-7", out.stdout)


if __name__ == "__main__":
    unittest.main()
