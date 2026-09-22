"""The login-method probe decides the existing-login path before any email code.

``/log-in/password`` is the **sibling auth step** of email OTP inside the same
login transaction.  OpenAI lands the generic login transaction on the OTP page
*even for accounts that have a password* -- abai's protocol client documents
exactly that at ``_load_login_password_page`` -- so an OTP page is not evidence
of a passwordless account, and the OTP lane is a measured dead end anyway
(2026-09-14: 5/5 landings on the signup profile step ``/about-you``, 0 NextAuth
sessions).

Three things this file pins down:

1. The probe is **three-valued**.  ``True``/``False`` are the transaction's own
   answers and drive the lane; ``None`` is "could not tell", which must never be
   treated as evidence of absence -- a probe that answers nothing must not
   silently become a terminal verdict.
2. Only a definitive answer, or an explicit refusal to guess, stops the lane.
   A blank 200, a transport failure and a 400 all leave the caller free to keep
   its previous behaviour.
3. The password step is submitted with the password and the result is reported
   on ``login_method``, so the outcome reporter can name how a session was won.
"""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sms_tool import auth_flow


def _source(relative_path):
    return (Path(__file__).resolve().parents[1] / relative_path).read_text(encoding="utf-8")


class LoginPasswordStepSignalTests(unittest.TestCase):
    """``_is_login_password_step`` reads both the URL and the transaction page."""

    def test_the_sibling_step_urls_are_recognised(self):
        for url in (
            "https://auth.openai.com/log-in/password",
            "https://auth.openai.com/log-in/password/",
            "https://auth.openai.com/create-account/password",
        ):
            with self.subTest(url=url):
                self.assertTrue(auth_flow._is_login_password_step(url))

    def test_other_auth_pages_are_not_password_steps(self):
        for url in (
            "https://auth.openai.com/email-verification",
            "https://auth.openai.com/about-you",
            "https://chatgpt.com/log-in/password",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(auth_flow._is_login_password_step(url))

    def test_the_transaction_page_type_is_recognised(self):
        for page_type in ("password", "login_password", "create_account_password"):
            with self.subTest(page_type=page_type):
                self.assertTrue(
                    auth_flow._is_login_password_step("", {"page": {"type": page_type}})
                )

    def test_a_flat_page_type_is_recognised_too(self):
        self.assertTrue(auth_flow._is_login_password_step("", {"page_type": "login_password"}))

    def test_an_email_otp_page_is_not_a_password_step(self):
        payload = {"page": {"type": "email_otp_verification"}}
        self.assertFalse(auth_flow._is_login_password_step("", payload))


class PasswordFormDetectionTests(unittest.TestCase):
    """``_has_password_form`` mirrors abai's detector: a form *with* a password input."""

    def _response(self, text):
        return Mock(status_code=200, text=text)

    def test_a_password_input_is_found(self):
        # abai's detector -- and therefore ours -- reads *quoted* attributes,
        # which is the shape the real login form uses.
        for text in (
            '<form action="/log-in/password"><input type="password" name="password"></form>',
            "<form action='/log-in/password'><input type='password'></form>",
            '<form><input name="password"></form>',
        ):
            with self.subTest(text=text):
                self.assertTrue(auth_flow._has_password_form(self._response(text)))

    def test_a_form_without_a_password_input_is_not(self):
        # This is the shape the email-verification shell has, and the shape that
        # makes ``False`` a meaningful answer rather than a missing page.
        self.assertFalse(
            auth_flow._has_password_form(self._response('<form action="/email-verification"></form>'))
        )

    def test_a_page_without_a_form_is_not(self):
        self.assertFalse(auth_flow._has_password_form(self._response("<html></html>")))
        self.assertFalse(auth_flow._has_password_form(self._response("")))


class ProbeLoginPasswordStepTests(unittest.TestCase):
    """The probe never turns "I could not tell" into a terminal verdict.

    ``continue_url`` defaults to a **live** transaction's next step (the OTP
    page).  It must not default to empty: an empty transaction state short-
    circuits before the request, which would make every HTTP-path assertion
    below unreachable.  The short-circuit has its own class.
    """

    def _probe(
        self,
        response=None,
        error=None,
        payload=None,
        continue_url="https://auth.openai.com/email-verification",
        **kwargs,
    ):
        if error is not None:
            transport = Mock(side_effect=error)
        else:
            transport = Mock(return_value=response)
        with patch.object(auth_flow, "request_with_retry", transport):
            result = auth_flow._probe_login_password_step(
                Mock(),
                "https://auth.openai.com",
                {},
                "https://auth.openai.com/log-in",
                payload=payload,
                continue_url=continue_url,
                **kwargs,
            )
        return result, transport

    def test_a_transaction_page_type_answers_without_a_request(self):
        result, transport = self._probe(payload={"page": {"type": "login_password"}})

        self.assertTrue(result["password_step"])
        self.assertIn("login_password", result["signal"])
        self.assertEqual(result["source"], auth_flow.SOURCE_TRANSACTION_STEP)
        transport.assert_not_called()

    def test_a_transaction_continue_url_answers_without_a_request(self):
        result, transport = self._probe(continue_url="https://auth.openai.com/log-in/password")

        self.assertTrue(result["password_step"])
        self.assertEqual(result["source"], auth_flow.SOURCE_TRANSACTION_STEP)
        transport.assert_not_called()

    def test_the_two_true_sources_are_distinguishable(self):
        """``create_account_password`` is the *new-account* screen, and it is a ``True``.

        This is why a registration lane must never abort on a bare ``True``: the
        transaction's declared step is the same page type for "sign in with your
        password" and "choose a password".  Only the sibling form separates them.
        """
        from_transaction, _ = self._probe(payload={"page": {"type": "create_account_password"}})
        from_form, _ = self._probe(
            response=Mock(status_code=200, text='<form><input type="password"></form>')
        )

        self.assertTrue(from_transaction["password_step"])
        self.assertTrue(from_form["password_step"])
        self.assertEqual(from_transaction["source"], auth_flow.SOURCE_TRANSACTION_STEP)
        self.assertEqual(from_form["source"], auth_flow.SOURCE_SIBLING_FORM)
        self.assertNotEqual(from_transaction["source"], from_form["source"])

    def test_a_password_form_makes_the_step_available(self):
        response = Mock(status_code=200, text='<form><input type="password" name="password"></form>')

        result, transport = self._probe(response=response)

        self.assertTrue(result["password_step"])
        self.assertEqual(result["signal"], "password_form_present")
        self.assertEqual(result["status"], 200)
        transport.assert_called_once()

    def test_a_form_without_a_password_input_is_a_definitive_no(self):
        response = Mock(status_code=200, text='<form action="/email-verification"></form>')

        result, _ = self._probe(response=response)

        self.assertIs(result["password_step"], False)
        self.assertEqual(result["signal"], "password_form_absent")

    def test_a_blank_200_is_not_a_verdict(self):
        """A generic shell says nothing, so it must not read as "no password"."""
        result, _ = self._probe(response=Mock(status_code=200, text="<html></html>"))

        self.assertIsNone(result["password_step"])
        self.assertEqual(result["signal"], "response_has_no_form")

    def test_a_non_200_is_not_a_verdict(self):
        """``/log-in/password`` without the transaction state answers 400."""
        result, _ = self._probe(response=Mock(status_code=400, text=""))

        self.assertIsNone(result["password_step"])
        self.assertEqual(result["signal"], "http_400")

    def test_a_transport_failure_is_not_a_verdict(self):
        result, _ = self._probe(error=RuntimeError("boom"))

        self.assertIsNone(result["password_step"])
        self.assertIn("transport:", result["signal"])


class NoTransactionStateTests(unittest.TestCase):
    """An empty transaction state is answered *before* the doomed GET.

    Measured 2026-09-15: 97 of 101 probes returned ``None``, every one of them
    after paying a GET to ``/log-in/password`` that the transaction could only
    answer 400 -- because the caller deliberately skipped
    ``authorize/continue`` (see ``_login_existing_account_with_email_otp``:
    "Existing account continue: skipped (already at email-verification)").
    Name the reason instead of the symptom, and do not spend the request.
    """

    def _probe(self, payload=None, continue_url="", response=None):
        transport = Mock(return_value=response)
        with patch.object(auth_flow, "request_with_retry", transport):
            result = auth_flow._probe_login_password_step(
                Mock(),
                "https://auth.openai.com",
                {},
                "https://auth.openai.com/log-in",
                payload=payload,
                continue_url=continue_url,
            )
        return result, transport

    def test_an_empty_transaction_state_short_circuits_without_a_request(self):
        result, transport = self._probe()

        self.assertIsNone(result["password_step"])
        self.assertEqual(result["signal"], "no_transaction_state")
        self.assertEqual(result["source"], "")
        self.assertEqual(result["status"], 0)
        transport.assert_not_called()

    def test_a_blank_continue_url_with_a_page_type_still_asks(self):
        """The guard needs *both* halves empty: ``page_type`` alone is state.

        A non-password page type must not short-circuit -- the transaction is
        alive, so the sibling step can still answer, and its 400/blank-200 is
        real information rather than a missing transaction.
        """
        result, transport = self._probe(
            payload={"page": {"type": "email_otp_verification"}},
            response=Mock(status_code=400, text=""),
        )

        transport.assert_called_once()
        self.assertIsNone(result["password_step"])
        self.assertEqual(result["signal"], "http_400")

    def test_a_blank_continue_url_with_a_password_page_type_answers_immediately(self):
        result, transport = self._probe(payload={"page": {"type": "login_password"}})

        self.assertTrue(result["password_step"])
        self.assertEqual(result["source"], auth_flow.SOURCE_TRANSACTION_STEP)
        transport.assert_not_called()

    def test_a_whitespace_continue_url_counts_as_empty(self):
        result, transport = self._probe(continue_url="   ")

        self.assertEqual(result["signal"], "no_transaction_state")
        transport.assert_not_called()


def _lane(*, probe, password="", allow_passwordless=True, transport=None, **overrides):
    """Call the lane with every transport seam stubbed.

    ``transport`` is the scripted ``request_with_retry`` response list; the lane
    issues signin -> authorize -> authorize/continue before the probe is
    consulted, and ``_fetch_session_csrf_token`` is stubbed so it contributes
    none of them.
    """
    responses = transport or [
        Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai", headers={}),
        Mock(status_code=200, url="https://auth.openai.com/log-in", headers={}),
        Mock(status_code=200, url="https://auth.openai.com/log-in", headers={}),
    ]
    seams = {
        "_fetch_session_csrf_token": Mock(return_value="csrf"),
        "request_with_retry": Mock(side_effect=responses),
        "_json_or_raw": Mock(return_value={"url": "https://auth.openai.com/authorize-start"}),
        "_authorize_continue_sentinel": Mock(return_value=({}, "fresh", "fresh-so")),
        "_response_next_url": Mock(return_value=""),
        "_follow_continue_url": Mock(return_value=Mock(url="https://auth.openai.com/log-in")),
        "_print_protocol_diagnostic": Mock(),
        "_probe_login_password_step": Mock(return_value=probe),
    }
    seams.update(overrides)
    with patch.multiple(auth_flow, **seams):
        result = auth_flow._login_existing_account_with_email_otp(
            session=Mock(),
            username="user@example.com",
            mailbox=Mock(),
            did="device-id",
            session_logging_id="logging-id",
            auth_base="https://auth.openai.com",
            chat_base="https://chatgpt.com",
            base_headers={"User-Agent": "test"},
            csrf_token="csrf",
            password=password,
            allow_passwordless=allow_passwordless,
        )
    return result, seams


_PRESENT = {"password_step": True, "signal": "password_form_present", "url": "u", "status": 200}
_ABSENT = {"password_step": False, "signal": "password_form_absent", "url": "u", "status": 200}
_UNKNOWN = {"password_step": None, "signal": "http_400", "url": "u", "status": 400}
#: The shape the registration lane's recovery path actually produced (95 times
#: on 2026-09-15): no transaction state to ask about.
_NO_STATE = {
    "password_step": None,
    "signal": "no_transaction_state",
    "source": "",
    "url": "",
    "status": 0,
}


class LaneProbePolicyTests(unittest.TestCase):
    """The lane stops on a definitive answer and on an explicit refusal to guess."""

    def test_a_password_step_uses_the_password_and_skips_the_email_lane(self):
        password_login = Mock(return_value={"ok": True, "login_method": "password"})
        result, seams = _lane(
            probe=_PRESENT,
            password="StoredPass123",
            _password_login_existing_account=password_login,
            _send_existing_login_otp=Mock(),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["login_method"], "password")
        self.assertEqual(password_login.call_args.args[3], "StoredPass123")

    def test_a_password_step_without_a_password_stops(self):
        """Submitting a generated password would be a guess, so refuse instead."""
        send = Mock()
        result, _ = _lane(probe=_PRESENT, password="", _send_existing_login_otp=send)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "existing_login_password_required")
        self.assertEqual(result["login_method"], "probe")
        send.assert_not_called()

    def test_a_definitive_no_password_step_stops_before_the_email_lane(self):
        send = Mock()
        result, _ = _lane(probe=_ABSENT, password="StoredPass123", _send_existing_login_otp=send)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "existing_login_no_password_step:password_form_absent")
        self.assertEqual(result["login_method"], "probe")
        send.assert_not_called()

    def test_an_inconclusive_probe_keeps_the_email_lane(self):
        """The recovery lane relies on this default: unknown must not terminate."""
        send = Mock(return_value=(False, Mock(status_code=400)))
        result, _ = _lane(probe=_UNKNOWN, _send_existing_login_otp=send)

        self.assertFalse(result["ok"])
        self.assertNotEqual(result["error"], "existing_login_password_step_unknown")
        send.assert_called_once()

    def test_an_inconclusive_probe_stops_when_the_caller_forbids_email(self):
        """For a known-registered address the email lane is a measured dead end."""
        send = Mock()
        result, _ = _lane(
            probe=_UNKNOWN,
            allow_passwordless=False,
            _send_existing_login_otp=send,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "existing_login_password_step_unknown")
        send.assert_not_called()

    def test_a_missing_transaction_state_is_treated_as_inconclusive(self):
        """``no_transaction_state`` is a *reason*, not a verdict.

        It must land in the same branch as any other ``None``: the caller's
        ``allow_passwordless`` decides, and the reason survives only in
        ``password_probe`` for the operator.  Were this ever promoted to a
        terminal answer, the registration lane's recovery path -- which reaches
        it on *every* attempt -- would stop reporting anything else.
        """
        send = Mock()
        result, _ = _lane(probe=_NO_STATE, allow_passwordless=False, _send_existing_login_otp=send)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "existing_login_password_step_unknown")
        self.assertEqual(result["password_probe"]["signal"], "no_transaction_state")
        send.assert_not_called()

    def test_the_probe_reads_the_transaction_continue_url(self):
        """The transaction's own answer must be handed to the probe, not dropped."""
        result, seams = _lane(
            probe=_ABSENT,
            transport=[
                Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai", headers={}),
                Mock(status_code=200, url="https://auth.openai.com/log-in", headers={}),
                Mock(status_code=200, url="https://auth.openai.com/log-in", headers={}),
            ],
        )

        self.assertFalse(result["ok"])
        probe_call = seams["_probe_login_password_step"].call_args
        self.assertEqual(probe_call.kwargs["payload"], {"url": "https://auth.openai.com/authorize-start"})
        self.assertEqual(probe_call.args[3], "https://auth.openai.com/log-in")


_MISSING = object()


class ExistingLoginContinueToggleTests(unittest.TestCase):
    """``registration.existing_login_continue_on_verified_page`` gates the POST.

    Default is **on**: the skip is what made the existing-account lane
    deterministically unwinnable (see ``VerifiedPageContinueTests``), so a
    missing key must not silently restore it.
    """

    def _enabled(self, value=_MISSING, *, cfg=None):
        if cfg is None:
            registration = {} if value is _MISSING else {
                "existing_login_continue_on_verified_page": value
            }
            cfg = {"registration": registration}
        with patch.object(auth_flow, "current_config_data", Mock(return_value=cfg)):
            return auth_flow._existing_login_continue_enabled()

    def test_a_missing_key_defaults_to_on(self):
        self.assertTrue(self._enabled())

    def test_false_spellings_turn_it_off(self):
        for value in (False, 0, "0", "false", "False", "no", "No", "off"):
            with self.subTest(value=value):
                self.assertFalse(self._enabled(value))

    def test_true_spellings_keep_it_on(self):
        for value in (True, 1, "1", "true", "True", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(self._enabled(value))

    def test_an_unreadable_config_keeps_it_on(self):
        with patch.object(auth_flow, "current_config_data", Mock(side_effect=RuntimeError("no config"))):
            self.assertTrue(auth_flow._existing_login_continue_enabled())

    def test_a_non_dict_registration_section_keeps_it_on(self):
        self.assertTrue(self._enabled(cfg={"registration": "not-a-dict"}))
        self.assertTrue(self._enabled(cfg={}))


class VerifiedPageContinueTests(unittest.TestCase):
    """The OTP page must not cost the lane its transaction state.

    Until 2026-09-16 ``_login_existing_account_with_email_otp`` dropped
    ``authorize/continue`` whenever ``authorize`` landed on
    ``/email-verification``.  That landing is what the server actually produces
    (measured 2026-09-16, PID 32088: 21/21 ``login_or_signup`` redirects), so
    the password probe was handed an empty ``continue_url`` on every attempt,
    answered ``None`` every time, and the signup lane's
    ``allow_passwordless=False`` turned that ``None`` into a terminal
    ``existing_login_password_step_unknown`` -- 19/19, zero exceptions.

    abai's ``_submit_login_email`` posts it unconditionally and declares
    ``screen_hint: "login"``, which is the one field our body did not carry.
    These tests pin that we now post it from the verified page **and** that the
    extra request can never become a new way for the lane to fail.
    """

    _SIGNIN_URL = "https://chatgpt.com/api/auth/signin/openai"
    _VERIFIED_URL = "https://auth.openai.com/email-verification"
    _LOGIN_URL = "https://auth.openai.com/log-in"
    _CONTINUE_PATH = "/api/accounts/authorize/continue"

    def _run(
        self,
        *,
        continue_response=None,
        enabled=True,
        transport=None,
        # A ``True`` probe verdict is only actionable when a password is held;
        # without one the lane stops at ``existing_login_password_required``
        # before ever reaching the password login.  Supply one by default so
        # these tests exercise the continue POST rather than that guard.
        password="StoredPass123",
        **overrides,
    ):
        responses = transport or [
            Mock(status_code=200, url=self._SIGNIN_URL, headers={}),
            Mock(status_code=200, url=self._VERIFIED_URL, headers={}),
            continue_response
            if continue_response is not None
            else Mock(status_code=200, url=self._VERIFIED_URL, headers={}),
        ]
        seams = {
            "_existing_login_continue_enabled": Mock(return_value=enabled),
            "_password_login_existing_account": Mock(return_value={"ok": True, "login_method": "password"}),
        }
        seams.update(overrides)
        return _lane(probe=_PRESENT, transport=responses, password=password, **seams)

    def _continue_call(self, seams):
        calls = seams["request_with_retry"].call_args_list
        self.assertEqual(len(calls), 3, "signin + authorize + authorize/continue")
        call = calls[2]
        self.assertTrue(call.args[2].endswith(self._CONTINUE_PATH), call.args[2])
        return call

    def test_the_continue_post_is_sent_from_the_verified_page(self):
        _, seams = self._run()
        self._continue_call(seams)

    def test_the_continue_post_declares_screen_hint_login(self):
        _, seams = self._run()
        body = self._continue_call(seams).kwargs["json"]
        self.assertEqual(body["screen_hint"], "login")
        self.assertEqual(body["username"], {"value": "user@example.com", "kind": "email"})

    def test_the_toggle_restores_the_skip(self):
        _, seams = self._run(enabled=False)
        self.assertEqual(
            len(seams["request_with_retry"].call_args_list),
            2,
            "signin + authorize only; the continue POST must not be sent",
        )

    def test_a_successful_continue_hands_the_state_to_the_probe(self):
        """This is the whole point: the probe must stop answering None."""
        next_url = "https://auth.openai.com/log-in/password"
        _, seams = self._run(_response_next_url=Mock(return_value=next_url))
        self.assertEqual(
            seams["_probe_login_password_step"].call_args.kwargs["continue_url"],
            next_url,
        )

    def test_a_refused_continue_keeps_the_skip_instead_of_failing(self):
        """A refusal is not new information -- the old code never asked."""
        result, _ = self._run(
            continue_response=Mock(status_code=500, url=self._VERIFIED_URL, headers={}),
        )
        self.assertNotIn("existing_login_continue_failed", str(result.get("error") or ""))

    def test_a_transport_error_on_the_verified_page_does_not_fail_the_lane(self):
        result, _ = self._run(
            transport=[
                Mock(status_code=200, url=self._SIGNIN_URL, headers={}),
                Mock(status_code=200, url=self._VERIFIED_URL, headers={}),
                RuntimeError("curl: (28) Operation timed out"),
            ],
        )
        self.assertTrue(result["ok"])

    def test_off_the_verified_page_the_post_carries_no_screen_hint(self):
        """The signup continue body is the contract there; leave it alone."""
        _, seams = _lane(probe=_PRESENT, _existing_login_continue_enabled=Mock(return_value=True))
        body = seams["request_with_retry"].call_args_list[2].kwargs["json"]
        self.assertNotIn("screen_hint", body)

    def test_off_the_verified_page_a_refusal_still_fails_the_lane(self):
        """The pre-existing failure mode must survive untouched."""
        result, _ = _lane(
            probe=_PRESENT,
            transport=[
                Mock(status_code=200, url=self._SIGNIN_URL, headers={}),
                Mock(status_code=200, url=self._LOGIN_URL, headers={}),
                Mock(status_code=500, url=self._LOGIN_URL, headers={}),
            ],
            _existing_login_continue_enabled=Mock(return_value=True),
        )
        self.assertEqual(result["error"], "existing_login_continue_failed:500")


class PasswordLoginTests(unittest.TestCase):
    """``_password_login_existing_account`` submits the password and reports how."""

    def _login(self, *, verify, follow=None, totp=None, **overrides):
        seams = {
            "request_with_retry": Mock(return_value=verify),
            "_json_or_raw": Mock(return_value={}),
            "_response_next_url": Mock(return_value=""),
            "_follow_continue_url": Mock(return_value=follow or Mock(url="")),
            "_complete_existing_login_totp": Mock(return_value=totp or {"ok": True, "data": {}}),
        }
        seams.update(overrides)
        with patch.multiple(auth_flow, **seams):
            result = auth_flow._password_login_existing_account(
                Mock(),
                "https://auth.openai.com",
                {},
                "StoredPass123",
                "https://auth.openai.com/log-in/password",
                "device-id",
            )
        return result, seams

    def test_a_verified_password_reports_the_login_method(self):
        result, seams = self._login(verify=Mock(status_code=200))

        self.assertTrue(result["ok"])
        self.assertEqual(result["login_method"], "password")
        self.assertEqual(
            seams["request_with_retry"].call_args.args[2],
            "https://auth.openai.com/api/accounts/password/verify",
        )
        self.assertEqual(seams["request_with_retry"].call_args.kwargs["json"], {"password": "StoredPass123"})

    def test_a_rejected_password_is_terminal(self):
        """abai refuses to fall back to an email code here, and so must we."""
        result, seams = self._login(verify=Mock(status_code=401))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "existing_login_password_verify_failed:401")
        self.assertEqual(result["login_method"], "password")
        seams["_complete_existing_login_totp"].assert_not_called()

    def test_a_failed_totp_step_is_reported_as_a_password_attempt(self):
        result, _ = self._login(
            verify=Mock(status_code=200),
            totp={"ok": False, "error": "existing_login_totp_verify_failed:401"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["login_method"], "password")

    def test_the_profile_step_landing_is_reported_as_a_failure(self):
        """The same ``/about-you`` wall the email lane hits must not read as success."""
        result, _ = self._login(
            verify=Mock(status_code=200),
            totp={"ok": True, "data": {"continue_url": "https://auth.openai.com/about-you"}},
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("existing_login_landed_on_profile_step"))
        self.assertEqual(result["login_method"], "password")


class ProbePositionGuardTests(unittest.TestCase):
    """The probe already sits between ``authorize/continue`` and the code spend.

    Measured 2026-09-15: ``/log-in/password`` answers 400 until
    ``authorize/continue`` has run, so the probe's only usable position is after
    that POST and before the OTP send.  Both ends are pinned: earlier would ask
    a transaction that does not exist yet, later would restore the cost the
    probe exists to avoid.
    """

    def _lane_body(self):
        # 2026-09-19: the lane was split into four phase functions
        # (``_existing_login_signin/_continue/_probe/_otp``) orchestrated by
        # ``_login_existing_account_with_email_otp``.  The invariant this guard
        # pins -- continue *before* probe *before* OTP send -- now lives across
        # those phase bodies, so concatenate them in pipeline order rather than
        # slicing a single function.
        src = _source("sms_tool/auth_flow.py")
        parts = []
        for name in (
            "_existing_login_signin",
            "_existing_login_continue",
            "_existing_login_probe",
            "_existing_login_otp",
        ):
            start = src.index(f"def {name}(")
            end = src.index("\ndef ", start)
            parts.append(src[start:end])
        return "\n".join(parts)

    def test_the_probe_runs_after_authorize_continue_and_before_the_otp_send(self):
        body = self._lane_body()

        probe_at = body.index("_probe_login_password_step(")
        self.assertLess(body.index("/api/accounts/authorize/continue"), probe_at)
        self.assertLess(probe_at, body.index("_send_existing_login_otp("))

    def test_both_registration_lane_callers_forbid_the_email_lane_for_a_registered_address(self):
        """``allow_passwordless=not s.existing_account`` is what makes the refusals free.

        The probe cannot answer in the recovery path (no transaction state), so
        the caller's flag -- not the probe -- is what stops the code there.
        """
        # 2026-09-19: one caller moved with ``enroll_totp`` into
        # ``registration_finalize.py`` (the P1 finalize/totp split), so the two
        # call sites now live in two files.  The invariant -- *both* lanes pass
        # the flag -- is what matters, not which file each lives in.
        src = _source("sms_tool/registration_handlers.py") + _source("sms_tool/registration_finalize.py")

        self.assertEqual(src.count("allow_passwordless=not s.existing_account"), 2)


if __name__ == "__main__":
    unittest.main()
