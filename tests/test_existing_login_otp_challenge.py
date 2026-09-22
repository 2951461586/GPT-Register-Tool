"""The existing-login OTP send must distinguish a challenge from an ack.

Live failure, 2026-09-14 (``runtime/_rerun_409_stdout4.txt``), 3/3 accounts::

    Existing account login: starting email OTP flow
    Existing account authorize: 200 https://auth.openai.com/email-verification
    Existing account continue: 200
    Existing account continue follow: 200 https://auth.openai.com/email-verification
    Existing account OTP send: /api/accounts/email-otp/resend 200 {"success": true}
    Email OTP validate: /api/accounts/email-otp/validate 401
        {"error": {"message": "Login failed...", "code": "login_failed"}}

``/api/accounts/email-otp/resend`` answered ``200`` with a bare acknowledgement
and ``_send_existing_login_otp`` returned on it immediately, so
``/api/accounts/email-otp/send`` was never tried.  That endpoint is the one that
returns the real login challenge -- recorded on 2026-09-12 in
``runtime/_retention/20260912-014722/mailbox_imports/icloud3_rest_run.log``::

    Existing account OTP send: /api/accounts/email-otp/send 200
        {"continue_url": "https://auth.openai.com/email-verification",
         "method": "GET",
         "page": {"type": "email_otp_verification", ...,
                  "payload": {"email_verification_mode": "logi...}}
    Access token probe: HTTP 200          <-- the flow then succeeded

Both the docstring that justified the old ordering and the test that locked it
in conflated ``/api/accounts/passwordless/send-otp`` (which does answer
``409 invalid_state``) with ``/api/accounts/email-otp/send`` (which does not).

Superseded 2026-09-14 (evening): falling through from ``resend`` to ``send``
still posted **both** endpoints on every transaction whose ``resend`` answered
the bare ack.  Counting the whole of ``runtime/logs/backend_stdout.log`` showed
``resend`` was called 24 times and carried a challenge **0** times (15x bare
ack, 6x ``409 sign-in session is no longer valid``, 3x ``429``), while ``send``
carried it **18/18** -- and every ``429`` landed on the useless endpoint, so
three runs aborted with ``existing_login_otp_send_failed:429`` without ever
reaching ``send``.  ``_send_existing_login_otp`` now posts ``send`` first and
keeps ``resend`` only as the 400/404/405 fallback.  The ordering contract lives
in ``tests/test_registration_relogin_totp.py``.
"""

from sms_tool import auth_flow


def test_continue_url_establishes_the_challenge():
    assert auth_flow._otp_challenge_established(
        {"continue_url": "https://auth.openai.com/email-verification"}
    )


def test_page_type_email_otp_verification_establishes_the_challenge():
    assert auth_flow._otp_challenge_established(
        {"page": {"type": "email_otp_verification"}}
    )


def test_top_level_email_verification_mode_establishes_the_challenge():
    assert auth_flow._otp_challenge_established({"email_verification_mode": "login"})


def test_nested_email_verification_mode_establishes_the_challenge():
    assert auth_flow._otp_challenge_established(
        {"page": {"payload": {"email_verification_mode": "login"}}}
    )


def test_bare_success_acknowledgement_is_not_a_challenge():
    """The exact 2026-09-14 body -- accepting this is the bug."""
    assert not auth_flow._otp_challenge_established({"success": True})


def test_empty_and_non_dict_bodies_are_not_challenges():
    assert not auth_flow._otp_challenge_established({})
    assert not auth_flow._otp_challenge_established(None)
    assert not auth_flow._otp_challenge_established("email_otp_verification")
    assert not auth_flow._otp_challenge_established([])


def test_blank_and_mismatched_markers_are_not_challenges():
    assert not auth_flow._otp_challenge_established({"continue_url": "   "})
    assert not auth_flow._otp_challenge_established({"email_verification_mode": ""})
    # ``page`` must be a mapping, not the marker string itself.
    assert not auth_flow._otp_challenge_established({"page": "email_otp_verification"})
    # A different verification step is not the login email-OTP challenge.
    assert not auth_flow._otp_challenge_established({"page": {"type": "mfa_challenge"}})
    assert not auth_flow._otp_challenge_established(
        {"page": {"payload": {"email_verification_mode": ""}}}
    )
