"""P0-B S2: rotate the run's exit once on a Cloudflare challenge, then retry.

``plan-2026-10-05-inflow-challenge-handoff.md`` §3.1/§3.3/§3.5.  The switch is
``registration.edge_challenge_rotate_exit`` (default **off**): it changes control
flow -- one extra state-changing request on a different egress -- so it earns a
default only after its own A/B (``p0-2b-inflow-challenge-handoff``).

What this file pins, and why each half is load-bearing:

* **Off is byte-identical.**  The challenge is still *observed* (the hit counter
  is the A/B's ``edge_challenge_count``), but no request is retried and the
  circuit opens exactly where it always did.  An observation that only exists
  when the treatment is on measures nothing.
* **One rotation, one retry, and the retry is verbatim.**  A challenge means
  "this exit was refused", not "the request was wrong"; the cap is one per
  request so "did it help?" stays attributable (§3.3).
* **§3.5's ordering.**  The circuit is written by ``request_with_retry`` *after*
  the challenge path had its chance, so a rotation that succeeds leaves no
  circuit behind, and a rotation that fails falls through to the old behaviour.
  A hook that moved the exit while an open circuit stayed behind would have the
  retry raise ``SessionCircuitOpen`` before it left the machine.
* **A single-slot exit short-circuits honestly.**  A provider without a sticky
  session id leaves the URL unchanged; that must be counted as a *failed*
  rotation, or the ``rotate`` arm is an ``observe`` arm wearing a label.

The handler half is exercised against a real ``RegistrationEmailWorkflow``,
because the rotation owns the runtime's proxy string, the session's ``proxies``
and the audit counters -- the transport deliberately owns none of those.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import Mock

from sms_tool import http_client
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_result import safe_proxy_audit

_ROTATABLE = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
#: A provider with no sticky session id: ``rotate_session`` cannot change it.
_SINGLE_SLOT = "http://proxy.example:8080"


class _Response:
    def __init__(self, status_code, *, text="", headers=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.text = text


class _Session:
    """Transport session whose replies are scripted in order."""

    def __init__(self, responses, proxy=_ROTATABLE):
        self._responses = list(responses)
        self.calls = 0
        self.proxies = {"http": proxy, "https": proxy}

    def get(self, _url, **_kwargs):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


def _workflow(*, rotate_exit: bool, proxy: str = _ROTATABLE):
    machine = Mock()
    machine.snapshot.return_value = {"state": "running"}
    workflow = RegistrationEmailWorkflow(
        machine,
        config={"registration": {"edge_challenge_rotate_exit": rotate_exit}},
        operations=Mock(),
    )
    workflow.runtime.proxy = proxy
    return workflow


def _challenge():
    return _Response(403, text="<html>Just a moment...</html>", headers={"cf-mitigated": "challenge"})


def _ok():
    return _Response(200, text="{}")


class RotateExitTransportTests(unittest.TestCase):
    """The transport half: observe, maybe rotate, retry once, then the circuit."""

    def _drive(self, *, rotate_exit, responses, proxy=_ROTATABLE, install_hook=True):
        workflow = _workflow(rotate_exit=rotate_exit, proxy=proxy)
        session = _Session(responses, proxy=proxy)
        if install_hook:
            workflow._install_edge_challenge_hook(session)
        response = http_client.request_with_retry(session, "get", "https://auth.openai.com/x", attempts=1)
        return workflow, session, response

    def test_off_observes_without_retrying(self):
        workflow, session, response = self._drive(rotate_exit=False, responses=[_challenge()])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.calls, 1)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_hits"], 1)
        self.assertNotIn("edge_challenge_rotations", workflow.proxy_metadata)
        # The circuit still opens exactly where it did before the switch existed.
        self.assertGreater(http_client._session_circuit(session)["blocked_until"], 0)
        self.assertTrue(http_client._session_circuit(session)["edge_challenge"])

    def test_on_rotates_once_and_returns_the_retry(self):
        workflow, session, response = self._drive(rotate_exit=True, responses=[_challenge(), _ok()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.calls, 2)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_hits"], 1)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotations"], 1)
        self.assertEqual(workflow.runtime.proxy, session.proxies["https"])
        self.assertNotEqual(session.proxies["https"], _ROTATABLE)

    def test_a_successful_rotation_leaves_no_circuit_behind(self):
        """§3.5 invariant 1: the breaker is written only after the challenge path failed."""
        _workflow_, session, _response = self._drive(rotate_exit=True, responses=[_challenge(), _ok()])
        state = http_client._session_circuit(session)
        self.assertEqual(state["blocked_until"], 0)
        self.assertEqual(state["status_code"], 0)
        # And the session is genuinely usable again, not merely labelled clear.
        self.assertEqual(http_client.request_with_retry(session, "get", "https://auth.openai.com/x").status_code, 200)

    def test_a_retry_that_is_still_challenged_opens_the_circuit(self):
        workflow, session, response = self._drive(rotate_exit=True, responses=[_challenge(), _challenge()])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.calls, 2)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotations"], 1)
        with self.assertRaises(http_client.SessionCircuitOpen) as caught:
            http_client.request_with_retry(session, "get", "https://auth.openai.com/x")
        self.assertTrue(caught.exception.edge_challenge)
        self.assertTrue(str(caught.exception).endswith(":edge_challenge"))

    def test_rotation_is_capped_at_one_per_request(self):
        """Two challenges in one call must not become two rotations."""
        workflow, session, _response = self._drive(
            rotate_exit=True, responses=[_challenge(), _challenge(), _challenge()]
        )
        self.assertEqual(session.calls, 2)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotations"], 1)

    def test_a_single_slot_exit_cannot_rotate_and_says_so(self):
        workflow, session, response = self._drive(rotate_exit=True, responses=[_challenge()], proxy=_SINGLE_SLOT)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.calls, 1)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotate_failed"], 1)
        self.assertNotIn("edge_challenge_rotations", workflow.proxy_metadata)
        self.assertEqual(session.proxies["https"], _SINGLE_SLOT)

    def test_a_session_without_the_hook_only_observes(self):
        _workflow_, session, response = self._drive(rotate_exit=True, responses=[_challenge()], install_hook=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.calls, 1)
        self.assertGreater(http_client._session_circuit(session)["blocked_until"], 0)

    def test_a_non_challenge_403_is_never_retried(self):
        """A deactivated account is not an exit problem: rotating cannot help."""
        workflow, session, response = self._drive(
            rotate_exit=True,
            responses=[_Response(403, text='{"code": "account_deactivated"}')],
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.calls, 1)
        self.assertNotIn("edge_challenge_hits", workflow.proxy_metadata)
        self.assertFalse(http_client._session_circuit(session)["edge_challenge"])

    def test_an_unknown_verdict_rotates_through_the_policy(self):
        """§7-5 reachability: ``request_with_retry`` only enters the policy on
        403/429, so the ``unknown`` branch is exercised directly here.  It must
        rotate (cheap side) and be counted under its own key."""
        workflow = _workflow(rotate_exit=True)
        session = _Session([_ok()])
        workflow._install_edge_challenge_hook(session)
        response, is_challenge, rotated = http_client._apply_edge_challenge_policy(
            session,
            _Response(500, text="upstream exploded"),
            session.get,
            "https://auth.openai.com/x",
            {},
            rotate_allowed=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(is_challenge)
        self.assertTrue(rotated)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_unknown"], 1)
        self.assertNotIn("edge_challenge_hits", workflow.proxy_metadata)

    def test_a_hook_that_explodes_degrades_to_observe(self):
        workflow = _workflow(rotate_exit=True)
        session = _Session([_challenge()])
        session.__dict__[http_client.EDGE_CHALLENGE_HOOK_ATTR] = Mock(side_effect=RuntimeError("boom"))
        response = http_client.request_with_retry(session, "get", "https://auth.openai.com/x", attempts=1)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.calls, 1)
        self.assertGreater(http_client._session_circuit(session)["blocked_until"], 0)


class RotateExitHandlerTests(unittest.TestCase):
    """The handler half: it owns the exit, the session and the audit counters."""

    def test_the_switch_defaults_off(self):
        self.assertFalse(_workflow(rotate_exit=False)._edge_challenge_rotate_exit_enabled())
        self.assertTrue(_workflow(rotate_exit=True)._edge_challenge_rotate_exit_enabled())

    def test_true_spellings_turn_it_on(self):
        for value in (True, 1, "1", "true", "True", "yes", "Yes", "on"):
            with self.subTest(value=value):
                machine = Mock()
                workflow = RegistrationEmailWorkflow(
                    machine, config={"registration": {"edge_challenge_rotate_exit": value}}, operations=Mock()
                )
                self.assertTrue(workflow._edge_challenge_rotate_exit_enabled())

    def test_hits_are_counted_even_when_rotation_is_off(self):
        workflow = _workflow(rotate_exit=False)
        session = _Session([_ok()])
        self.assertFalse(workflow._on_edge_challenge(session, "challenge", True))
        self.assertEqual(workflow.proxy_metadata["edge_challenge_hits"], 1)
        self.assertEqual(workflow.runtime.proxy, _ROTATABLE)

    def test_rotation_moves_the_proxy_the_session_and_the_counter(self):
        workflow = _workflow(rotate_exit=True)
        session = _Session([_ok()])
        self.assertTrue(workflow._on_edge_challenge(session, "challenge", True))
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotations"], 1)
        self.assertNotEqual(workflow.runtime.proxy, _ROTATABLE)
        self.assertEqual(session.proxies["https"], workflow.runtime.proxy)
        # Same provider, same region, same sid *length* -- only the sticky
        # session id moved (``rotate_session`` randomises it, so the value
        # itself is not reproducible).
        self.assertTrue(workflow.runtime.proxy.startswith("http://user-region-US-sid-"))
        self.assertTrue(workflow.runtime.proxy.endswith("-t-5:secret@proxy.example:443"))
        self.assertEqual(len(workflow.runtime.proxy), len(_ROTATABLE))

    def test_rotation_clears_a_circuit_already_on_the_session(self):
        """§3.5 invariant 2, tested directly: a new exit must not inherit the breaker."""
        workflow = _workflow(rotate_exit=True)
        session = _Session([_ok()])
        state = http_client._session_circuit(session)
        state.update({"blocked_until": time.time() + 900, "status_code": 403, "retry_after": 900.0})
        self.assertTrue(workflow._on_edge_challenge(session, "challenge", True))
        self.assertEqual(http_client._session_circuit(session)["blocked_until"], 0)
        # The retry would otherwise raise before it ever left the machine.
        self.assertEqual(http_client.request_with_retry(session, "get", "https://auth.openai.com/x").status_code, 200)

    def test_a_single_slot_exit_reports_a_failure_and_keeps_the_proxy(self):
        workflow = _workflow(rotate_exit=True, proxy=_SINGLE_SLOT)
        session = _Session([_ok()], proxy=_SINGLE_SLOT)
        self.assertFalse(workflow._on_edge_challenge(session, "challenge", True))
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotate_failed"], 1)
        self.assertEqual(workflow.runtime.proxy, _SINGLE_SLOT)
        self.assertEqual(session.proxies["https"], _SINGLE_SLOT)

    def test_the_allow_rotate_flag_is_respected(self):
        """The transport owns the once-per-request cap; the handler must obey it."""
        workflow = _workflow(rotate_exit=True)
        session = _Session([_ok()])
        self.assertFalse(workflow._on_edge_challenge(session, "challenge", False))
        self.assertNotIn("edge_challenge_rotations", workflow.proxy_metadata)

    def test_unknown_is_counted_separately_and_still_rotates(self):
        """§7-5: an unreadable verdict is spent on the cheap side, but never
        merged into the count the A/B's manipulation check reads."""
        workflow = _workflow(rotate_exit=True)
        session = _Session([_ok()])
        self.assertTrue(workflow._on_edge_challenge(session, "unknown", True))
        self.assertEqual(workflow.proxy_metadata["edge_challenge_unknown"], 1)
        self.assertNotIn("edge_challenge_hits", workflow.proxy_metadata)
        self.assertEqual(workflow.proxy_metadata["edge_challenge_rotations"], 1)

    def test_unknown_hits_are_counted_even_with_rotation_off(self):
        workflow = _workflow(rotate_exit=False)
        session = _Session([_ok()])
        self.assertFalse(workflow._on_edge_challenge(session, "unknown", True))
        self.assertEqual(workflow.proxy_metadata["edge_challenge_unknown"], 1)
        self.assertNotIn("edge_challenge_hits", workflow.proxy_metadata)


class EdgeChallengeAuditTests(unittest.TestCase):
    """The counters must reach ``proxy_audit`` -- the A/B's manipulation check."""

    def test_defaults_are_zero_not_missing(self):
        audit = safe_proxy_audit({})
        self.assertEqual(audit["edge_challenge_hits"], 0)
        self.assertEqual(audit["edge_challenge_unknown"], 0)
        self.assertEqual(audit["edge_challenge_rotations"], 0)
        self.assertEqual(audit["edge_challenge_rotate_failed"], 0)

    def test_counters_are_allow_listed_and_coerced(self):
        audit = safe_proxy_audit(
            {
                "edge_challenge_hits": "3",
                "edge_challenge_unknown": 2,
                "edge_challenge_rotations": 1,
                "edge_challenge_rotate_failed": -2,
                "expected_country": "us",
                "actual_country": "US",
            }
        )
        self.assertEqual(audit["edge_challenge_hits"], 3)
        self.assertEqual(audit["edge_challenge_unknown"], 2)
        self.assertEqual(audit["edge_challenge_rotations"], 1)
        # A negative count is not a count; clamp rather than propagate.
        self.assertEqual(audit["edge_challenge_rotate_failed"], 0)
        self.assertEqual(audit["expected_country"], "US")

    def test_the_handler_metadata_is_what_the_audit_reports(self):
        workflow = _workflow(rotate_exit=True)
        session = _Session([_ok()])
        workflow._on_edge_challenge(session, "challenge", True)
        audit = safe_proxy_audit(workflow.proxy_metadata)
        self.assertEqual(audit["edge_challenge_hits"], 1)
        self.assertEqual(audit["edge_challenge_rotations"], 1)


if __name__ == "__main__":
    unittest.main()
