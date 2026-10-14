"""Observability + credential-safety tests for the mailbox OTP dispatch log.

``sms_tool/mailbox.py::_poll_email_otp`` dispatches to a provider-specific
poller (cfworker / remail / graph) chosen at runtime.  Before 2026-09-11 that
choice was invisible: a run pinned at the 300s OTP timeout produced no record
of *which* backend was being waited on, so "the mail never arrived" and "the
poller never looked" could not be told apart.

Adding this log carries the opposite risk.  The function holds a proxy
candidate list whose entries look like ``http://user:pass@host:port`` -- the
mailbox proxy is the first candidate.  Logging candidates wholesale would print
live mailbox credentials into ``runtime/logs``.  So the assertions below check
both halves: the record exists, and it is credential-free.
"""
from __future__ import annotations

import dataclasses
import logging
import unittest
from unittest import mock

from sms_tool import mailbox, mailbox_service, mailbox_strategies


class _FakeMailbox:
    def __init__(self, provider="icloud"):
        self.provider = provider
        self.email = "user@example.com"


class _RecordingHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


def _record_blob(record: logging.LogRecord) -> str:
    """Everything a log line could leak: the message plus every attribute.

    Asserting only on ``getMessage()`` would miss a credential parked in a
    structured extra -- precisely where a future edit would put one.
    """
    parts = [record.getMessage()]
    parts.extend(str(value) for value in vars(record).values())
    return " ".join(parts)


class MailboxOtpDispatchLogTests(unittest.TestCase):
    def setUp(self):
        self.records: list[logging.LogRecord] = []
        self.handler = _RecordingHandler(self.records)
        self.logger = logging.getLogger(mailbox.__name__)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def _dispatch(self, provider="icloud", proxy=None, poller_result="123456"):
        def fake_poller(target, **kwargs):
            return poller_result

        with mock.patch.object(
            mailbox_strategies, "resolve_otp_poller", return_value=fake_poller
        ), mock.patch.object(mailbox, "_email_cfg", return_value={}), mock.patch.object(
            mailbox, "_provider_otp_issued_after", return_value=0
        ), mock.patch.object(
            mailbox,
            "_mailbox_proxy_candidates",
            return_value=[proxy] if proxy else [],
        ), mock.patch.object(
            mailbox, "_resolve_mailbox_proxy", return_value=proxy
        ):
            return mailbox._poll_email_otp(_FakeMailbox(provider), timeout=300, proxy=proxy)

    def _dispatch_events(self):
        return [
            record
            for record in self.records
            if str(getattr(record, "event", "") or "") == "mailbox_otp_poll_dispatch"
        ]

    def test_dispatch_logs_provider_poller_and_budget(self):
        self.assertEqual(self._dispatch(provider="icloud"), "123456")
        events = self._dispatch_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].provider, "icloud")
        self.assertEqual(events[0].poller, "fake_poller")
        self.assertEqual(events[0].otp_timeout_s, 300)

    def test_dispatch_logs_candidate_count_instead_of_candidates(self):
        proxy = "http://alice:s3cret@proxy.example.com:8080"
        self._dispatch(provider="remail", proxy=proxy)
        events = self._dispatch_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].proxy_candidate_count, 1)

    def test_dispatch_log_never_carries_proxy_credentials(self):
        proxy = "http://alice:s3cret@proxy.example.com:8080"
        self._dispatch(provider="remail", proxy=proxy)
        events = self._dispatch_events()
        self.assertTrue(events)
        for record in events:
            blob = _record_blob(record)
            self.assertNotIn("s3cret", blob)
            self.assertNotIn("alice", blob)
            self.assertNotIn("proxy.example.com", blob)


class _FakeAdapter:
    def poll_otp(self, mailbox, **kwargs):
        return "123456"


class _FakeRegistry:
    def resolve_poller(self, mailbox, config):
        return _FakeAdapter()


class MailboxServiceDispatchLogTests(unittest.TestCase):
    """Registration reaches the OTP wait through ``MailboxService.poll_otp``.

    ``registration_handlers.wait_email_otp`` passes ``poll_otp_fn=self.poll_otp``,
    so a hook on ``mailbox._poll_email_otp`` never fires during a real run --
    it was green in tests and invisible in production (2026-09-11).  These
    assertions target the path registration actually uses.
    """

    def setUp(self):
        self.records: list[logging.LogRecord] = []
        self.handler = _RecordingHandler(self.records)
        self.logger = logging.getLogger(mailbox_service.__name__)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def _poll(self, proxy=None, issued_after_unix=0):
        service = dataclasses.replace(
            mailbox_service.MailboxService.create(), providers=_FakeRegistry()
        )
        with mock.patch.object(
            mailbox, "_mailbox_proxy_candidates", return_value=[proxy] if proxy else []
        ), mock.patch.object(mailbox, "_email_cfg", return_value={}), mock.patch.object(
            mailbox, "_provider_otp_issued_after", return_value=issued_after_unix
        ), mock.patch.object(
            mailbox, "_resolve_mailbox_proxy", return_value=proxy
        ):
            return service.poll_otp(
                _FakeMailbox("icloud_url"),
                timeout=300,
                issued_after_unix=issued_after_unix,
                proxy=proxy,
            )

    def _events(self):
        return [
            record
            for record in self.records
            if str(getattr(record, "event", "") or "") == "mailbox_otp_poll_dispatch"
        ]

    def test_registration_path_logs_dispatch(self):
        self.assertEqual(self._poll(), "123456")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].provider, "icloud_url")
        self.assertEqual(events[0].otp_timeout_s, 300)

    def test_dispatch_logs_the_issued_after_floor(self):
        """The floor that silently discards a real OTP as 'too old'.

        2026-09-11: the mail was stamped 09:34:50 while the local send request
        was 09:35:02, so the correct code was dropped and the run burned the
        full window.  Without this field that is invisible.
        """
        self._poll(issued_after_unix=1789110000)
        events = self._events()
        self.assertEqual(events[0].otp_issued_after_unix, 1789110000)

    def test_dispatch_log_never_carries_proxy_credentials(self):
        proxy = "http://alice:s3cret@proxy.example.com:8080"
        self._poll(proxy=proxy)
        events = self._events()
        self.assertTrue(events)
        for record in events:
            blob = _record_blob(record)
            self.assertNotIn("s3cret", blob)
            self.assertNotIn("alice", blob)
            self.assertNotIn("proxy.example.com", blob)


if __name__ == "__main__":
    unittest.main()
