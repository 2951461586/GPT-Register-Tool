"""P0-B S0/S1: the in-flow edge-challenge judgement and its three invariants.

``plan-2026-10-05-inflow-challenge-handoff.md`` §6 splits the work so that the
first two stages change **no behaviour**:

* **S0** — a read-only judgement (``edge_challenge_verdict``) plus
  ``registration.edge_challenge_discrimination`` (default **on**), which only
  decides whether a 403/429 failure *name* carries a trailing
  ``:edge_challenge`` segment.  The circuit still opens exactly where it opened
  before; rotation and browser handoff are separate, default-**off** switches.
* **S1** — the §3.5 invariants pinned by tests, one mutation each.

Why the judgement exists at all: the cost is asymmetric.  A challenge that
arrives *after* preflight happens after a mailbox and its OTP were already
spent, so a false negative loses a mailbox slot permanently while a false
positive costs one extra request.  That is why the vocabulary is three-valued
(``challenge`` / ``not_challenge`` / ``unknown``) and why "we could not tell"
must never be collapsed into a guess.

Classes inherit ``unittest.TestCase`` deliberately: this suite mixes plain
pytest functions and TestCase classes, and a bare class silently collects zero
tests.
"""

from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest.mock import Mock, patch

from sms_tool import http_client
from sms_tool.error_classification import classify_error, is_terminal_registration_error
from sms_tool.failure_registry import FAILURE_CLASSES
from sms_tool.proxy_edge_probe import (
    BLOCKED,
    EDGE_CHALLENGE,
    EDGE_CHALLENGE_VERDICTS,
    EDGE_NOT_CHALLENGE,
    EDGE_UNKNOWN,
    classify_edge_response,
    edge_challenge_verdict,
)

_DEACTIVATED_BODY = '{"error": {"code": "account_deactivated", "message": "account has been deactivated"}}'


class _Response:
    """Minimal transport reply; attributes are set only when meaningful."""

    def __init__(self, status_code=403, *, text=None, content=None, headers=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        if text is not None:
            self.text = text
        if content is not None:
            self.content = content


class _Session:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        return self._response


class EdgeChallengeVerdictTests(unittest.TestCase):
    """S0: the pure judgement's truth table and totality."""

    def test_the_vocabulary_is_three_valued(self):
        self.assertEqual(EDGE_CHALLENGE_VERDICTS, (EDGE_CHALLENGE, EDGE_NOT_CHALLENGE, EDGE_UNKNOWN))

    def test_only_403_and_429_get_a_yes_or_no_answer(self):
        """Rule 1: a 2xx OTP body legitimately contains "challenge" -- do not guess."""
        for status in (200, 201, 204, 302, 400, 401, 404, 500, 503):
            with self.subTest(status=status):
                response = _Response(status, text="Just a moment... cf-chl", headers={"cf-mitigated": "challenge"})
                self.assertEqual(edge_challenge_verdict(response), EDGE_UNKNOWN)

    def test_a_statusless_response_is_unknown(self):
        self.assertEqual(edge_challenge_verdict(_Response(0)), EDGE_UNKNOWN)
        self.assertEqual(edge_challenge_verdict(object()), EDGE_UNKNOWN)

    def test_a_403_with_cloudflare_markers_is_a_challenge(self):
        header_form = _Response(403, headers={"cf-mitigated": "challenge"})
        body_form = _Response(403, text="<html>Just a moment...</html>")
        self.assertEqual(edge_challenge_verdict(header_form), EDGE_CHALLENGE)
        self.assertEqual(edge_challenge_verdict(body_form), EDGE_CHALLENGE)

    def test_a_bare_403_is_a_challenge_because_the_probe_reads_it_as_a_refusal(self):
        """Rule 2: the judgement is ``classify_edge_response``'s, and its contract
        is "any 403 at this edge is a refusal" -- reachable but useless for
        registration, which is exactly the rotate-the-exit trigger."""
        response = _Response(403)
        self.assertEqual(classify_edge_response(403, "", {}), BLOCKED)
        self.assertEqual(edge_challenge_verdict(response), EDGE_CHALLENGE)

    def test_a_deactivated_account_is_not_a_challenge(self):
        """Rule 3: rotating the egress cannot change an ``account_deactivated`` answer."""
        self.assertEqual(edge_challenge_verdict(_Response(403, text=_DEACTIVATED_BODY)), EDGE_NOT_CHALLENGE)
        self.assertEqual(edge_challenge_verdict(_Response(429, text="account_deatived")), EDGE_NOT_CHALLENGE)

    def test_a_plain_429_stays_with_the_rate_limit_handler(self):
        """A plain rate-limit reply means the origin answered -- not a CF block."""
        self.assertEqual(edge_challenge_verdict(_Response(429, text="too many requests")), EDGE_NOT_CHALLENGE)

    def test_a_429_that_carries_challenge_markers_is_observable(self):
        """Observation only: §3.5 forbids rotating on a ``rate_limit`` class."""
        response = _Response(429, text="Just a moment...", headers={"cf-mitigated": "challenge"})
        self.assertEqual(edge_challenge_verdict(response), EDGE_CHALLENGE)

    def test_bytes_content_is_preferred_and_bounded(self):
        response = _Response(403, content=b"Just a moment...")
        self.assertEqual(edge_challenge_verdict(response), EDGE_CHALLENGE)
        # The deactivation marker beyond the bound must not be seen: the
        # judgement reads a bounded prefix, not the whole interstitial.
        long_body = b"x" * 64 + b"account_deactivated"
        self.assertEqual(edge_challenge_verdict(_Response(403, content=long_body), body_limit=8), EDGE_CHALLENGE)

    def test_it_never_raises_on_a_hostile_response(self):
        class Exploding:
            @property
            def status_code(self):
                raise RuntimeError("boom")

            @property
            def content(self):
                raise RuntimeError("boom")

            @property
            def headers(self):
                raise RuntimeError("boom")

        self.assertEqual(edge_challenge_verdict(Exploding()), EDGE_UNKNOWN)


class EdgeChallengeDiscriminationGateTests(unittest.TestCase):
    """S0: ``registration.edge_challenge_discrimination`` (default on)."""

    def _enabled(self, registration):
        with patch.object(http_client, "CFG", {"registration": registration}):
            return http_client.edge_challenge_discrimination_enabled()

    def test_default_is_on_when_the_key_is_absent(self):
        self.assertTrue(self._enabled({}))

    def test_a_missing_or_non_mapping_section_is_on(self):
        with patch.object(http_client, "CFG", {}):
            self.assertTrue(http_client.edge_challenge_discrimination_enabled())
        with patch.object(http_client, "CFG", {"registration": "oops"}):
            self.assertTrue(http_client.edge_challenge_discrimination_enabled())

    def test_false_spellings_turn_it_off(self):
        for value in (False, 0, "0", "false", "False", "no", "No", "off"):
            with self.subTest(value=value):
                self.assertFalse(self._enabled({"edge_challenge_discrimination": value}))

    def test_true_spellings_keep_it_on(self):
        for value in (True, 1, "1", "true", "True", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(self._enabled({"edge_challenge_discrimination": value}))

    def test_a_frozen_registration_section_is_read(self):
        """Production config sections are ``mappingproxy``, not ``dict``."""
        frozen = MappingProxyType({"edge_challenge_discrimination": False})
        with patch.object(http_client, "CFG", {"registration": frozen}):
            self.assertFalse(http_client.edge_challenge_discrimination_enabled())


class EdgeChallengeSuffixTests(unittest.TestCase):
    """S0: the suffix is observation only -- the breaker opens where it always did."""

    def _circuit_state(self, session):
        return http_client._session_circuit(session)

    def test_a_challenge_labelled_403_carries_the_suffix(self):
        session = _Session(_Response(403, headers={"cf-mitigated": "challenge", "Retry-After": "12"}))
        self.assertEqual(http_client.request_with_retry(session, "get", "https://x.test").status_code, 403)
        with self.assertRaises(http_client.SessionCircuitOpen) as caught:
            http_client.request_with_retry(session, "get", "https://x.test")
        self.assertTrue(caught.exception.edge_challenge)
        self.assertTrue(str(caught.exception).endswith(":edge_challenge"))
        self.assertEqual(str(caught.exception), "session_circuit_open:http_403:retry_after=12s:edge_challenge")

    def test_a_deactivated_403_keeps_the_legacy_message_exactly(self):
        session = _Session(_Response(403, text=_DEACTIVATED_BODY, headers={"Retry-After": "12"}))
        http_client.request_with_retry(session, "get", "https://x.test")
        with self.assertRaises(http_client.SessionCircuitOpen) as caught:
            http_client.request_with_retry(session, "get", "https://x.test")
        self.assertFalse(caught.exception.edge_challenge)
        self.assertEqual(str(caught.exception), "session_circuit_open:http_403:retry_after=12s")

    def test_turning_discrimination_off_restores_the_legacy_message(self):
        session = _Session(_Response(403, headers={"cf-mitigated": "challenge", "Retry-After": "12"}))
        with patch.object(http_client, "CFG", {"registration": {"edge_challenge_discrimination": False}}):
            http_client.request_with_retry(session, "get", "https://x.test")
            with self.assertRaises(http_client.SessionCircuitOpen) as caught:
                http_client.request_with_retry(session, "get", "https://x.test")
        self.assertEqual(str(caught.exception), "session_circuit_open:http_403:retry_after=12s")

    def test_a_plain_429_keeps_the_legacy_message(self):
        session = _Session(_Response(429, text="too many requests", headers={"Retry-After": "30"}))
        http_client.request_with_retry(session, "get", "https://x.test")
        with self.assertRaises(http_client.SessionCircuitOpen) as caught:
            http_client.request_with_retry(session, "get", "https://x.test")
        self.assertEqual(str(caught.exception), "session_circuit_open:http_429:retry_after=30s")

    def test_a_verdict_crash_never_becomes_a_transport_crash(self):
        """The observation must not be able to fail a request that would have worked."""
        exploding = Mock()
        exploding.status_code = 403
        exploding.content = b""
        exploding.headers = {}
        with patch.object(http_client, "edge_challenge_verdict", side_effect=RuntimeError("boom")):
            self.assertFalse(http_client._is_edge_challenge(exploding))


class CircuitInvariantTests(unittest.TestCase):
    """S1: §3.5's three invariants, one mutation each."""

    # --- Invariant 1: the circuit is written only *after* the challenge path
    # --- has failed, never before it.
    def test_a_challenge_labelled_403_still_opens_the_circuit(self):
        """Mutation caught: letting the observation suppress the breaker.

        S0 must change the *name* only.  If the challenge path were allowed to
        skip the circuit write, a challenged session would keep hammering the
        blocked exit -- and §3.5's ordering ("circuit written only after the
        challenge path failed") would be violated in the other direction.
        """
        session = _Session(_Response(403, headers={"cf-mitigated": "challenge", "Retry-After": "12"}))
        http_client.request_with_retry(session, "get", "https://x.test")
        state = http_client._session_circuit(session)
        self.assertGreater(state["blocked_until"], 0)
        self.assertTrue(state["edge_challenge"])
        with self.assertRaises(http_client.SessionCircuitOpen):
            http_client.request_with_retry(session, "get", "https://x.test")

    def test_the_read_only_helper_does_not_touch_the_circuit(self):
        """Mutation caught: making the judgement a control-flow decision."""
        session = _Session(_Response(403, headers={"cf-mitigated": "challenge"}))
        before = dict(http_client._session_circuit(session))
        http_client._is_edge_challenge(_Response(403, headers={"cf-mitigated": "challenge"}))
        self.assertEqual(http_client._session_circuit(session), before)

    # --- Invariant 2: after rotating the exit the circuit must be cleared --
    # --- including the challenge label, or the *next* exit inherits it.
    def test_clear_session_circuit_resets_the_challenge_label(self):
        """Mutation caught: clearing only ``blocked_until``/``retry_after``.

        A stale ``edge_challenge`` would make the next exit's circuit claim a
        challenge that never happened there, and the A/B's
        ``edge_challenge_count`` would over-count.
        """
        session = _Session(_Response(403, headers={"cf-mitigated": "challenge", "Retry-After": "12"}))
        http_client.request_with_retry(session, "get", "https://x.test")
        self.assertTrue(http_client._session_circuit(session)["edge_challenge"])
        http_client.clear_session_circuit(session)
        state = http_client._session_circuit(session)
        self.assertFalse(state["edge_challenge"])
        self.assertEqual(state["blocked_until"], 0)
        self.assertEqual(state["status_code"], 0)

    def test_a_cleared_circuit_labels_the_next_403_freshly(self):
        session = _Session(_Response(403, headers={"cf-mitigated": "challenge", "Retry-After": "12"}))
        http_client.request_with_retry(session, "get", "https://x.test")
        http_client.clear_session_circuit(session)
        session._response = _Response(403, text=_DEACTIVATED_BODY, headers={"Retry-After": "5"})
        http_client.request_with_retry(session, "get", "https://x.test")
        with self.assertRaises(http_client.SessionCircuitOpen) as caught:
            http_client.request_with_retry(session, "get", "https://x.test")
        self.assertFalse(caught.exception.edge_challenge)
        self.assertEqual(str(caught.exception), "session_circuit_open:http_403:retry_after=5s")

    # --- Invariant 3: no new failure class -- the suffix must not re-classify.
    def test_the_suffix_does_not_change_the_failure_class(self):
        """Mutation caught: registering ``edge_challenge`` as a failure class/code."""
        legacy = "session_circuit_open:http_403:retry_after=900s"
        labelled = f"{legacy}:edge_challenge"
        self.assertEqual(classify_error(labelled), classify_error(legacy))
        self.assertNotEqual(classify_error(labelled), "unknown")

    def test_the_suffix_does_not_change_terminality(self):
        legacy = "session_circuit_open:http_403:retry_after=900s"
        self.assertEqual(
            is_terminal_registration_error(f"{legacy}:edge_challenge"),
            is_terminal_registration_error(legacy),
        )

    def test_no_failure_class_owns_an_edge_challenge_marker(self):
        """``edge_challenge`` is a suffix, never a class or a marker."""
        for failure_class in FAILURE_CLASSES:
            for marker in failure_class.markers:
                self.assertNotIn("edge_challenge", marker.lower())
        self.assertNotIn("edge_challenge", classify_error.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
