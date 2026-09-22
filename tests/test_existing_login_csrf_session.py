"""The existing-account login lane must mint its NextAuth CSRF token in-session.

Observed live 2026-09-13 (``runtime/_rerun_409_stdout2.txt``): three mailboxes
out of three, on a healthy egress, all ended the same way::

    Existing account authorize: 200 https://chatgpt.com/auth/login?callbackUrl=...
    Existing account continue:  409   code=invalid_state
      cookie_presence: {client_auth_session: false, cookie_count: 7,
                        login_session: false, nextauth_state: false,
                        oai_did: true, oai_login_csrf: false}
    Existing account OTP send:  /api/accounts/email-otp/resend 409 invalid_state
    Email OTP validate:         /api/accounts/email-otp/validate 409 invalid_state

In that same run, the same mailbox, on the same exit, validated a *signup* OTP
with ``200`` -- so the account, the egress and the mailbox were all fine.

Root cause: ``registration_handlers.fetch_auth_session`` builds a brand new
``s.login_session`` and still passes the *main* session's ``s.csrf_token``.
NextAuth binds ``csrfToken`` to the ``__Host-next-auth.csrf-token`` cookie of
the same client, so the signin POST was rejected, the authorize request bounced
to ``chatgpt.com/auth/login`` instead of auth.openai.com, and every later call
answered ``409 invalid_state`` -- which read as an OTP problem.

The same function has a second call site (``reauth_existing_account``) that
passes ``s.session`` -- self-consistent, and that one works. The phone lane
(``phone_registration.py:121-124``) has always minted the token on the session
it posts with. This lane was the odd one out.

Four things this file pins down:

1. The token is minted on the session that will be used to post.
2. A mint failure falls back to the caller's token -- never a hard error.
3. Landing on ``chatgpt.com/auth/login`` stops before the doomed
   ``authorize/continue``, with a precise error that stays retryable.
4. The authorize URL carries the context the signup lane fills in.
"""

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from sms_tool import auth_flow
from sms_tool.error_classification import classify_error
from sms_tool.registration_policy import registration_retry_decision


CHAT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"
USERNAME = "someone@icloud.com"

CALLER_TOKEN = "token-minted-on-the-main-session"
SESSION_TOKEN = "token-minted-on-the-login-session"

# The exact shape production landed on, captured 2026-09-13.
LOGIN_LANDING = f"{CHAT_BASE}/auth/login?callbackUrl=https%3A%2F%2Fchatgpt.com%2F"
# The *other* chatgpt.com URL this function accepts, used as the control: the
# guard must not swallow it.
CALLBACK_LANDING = f"{CHAT_BASE}/api/auth/callback/openai?code=abc"

GUARD_PREFIX = "existing_login_signin_not_established:invalid_state:"


class _Response:
    """Minimal stand-in for a curl_cffi response."""

    def __init__(self, *, status_code=200, url="", body=None, headers=None):
        self.status_code = status_code
        self.url = url
        self._body = {} if body is None else body
        self.headers = {} if headers is None else headers

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


class _Recorder:
    """Fake ``request_with_retry``: records every call, delegates the reply."""

    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def __call__(self, session, method, url, **kwargs):
        record = {"session": session, "method": method, "url": url, "kwargs": kwargs}
        self.calls.append(record)
        return self._handler(record)

    def urls(self):
        return [record["url"] for record in self.calls]


def _drive(*, mint_token, authorize_landing, caller_token=CALLER_TOKEN,
           signin_authorize_url=f"{AUTH_BASE}/api/accounts/authorize"):
    """Run the real function against a scripted transport.

    Returns ``(result, signin_payloads, authorize_urls, recorder)``.
    """
    session = object()
    signin_payloads: list[dict] = []
    authorize_urls: list[str] = []

    def handler(record):
        url = record["url"]
        if url == f"{CHAT_BASE}/api/auth/csrf":
            if mint_token is None:
                raise RuntimeError("csrf endpoint down")
            return _Response(body={"csrfToken": mint_token})
        if url.startswith(f"{CHAT_BASE}/api/auth/signin/openai"):
            signin_payloads.append(parse_qs(record["kwargs"]["data"]))
            return _Response(body={"url": signin_authorize_url})
        if url.startswith(f"{AUTH_BASE}/api/accounts/authorize"):
            authorize_urls.append(url)
            return _Response(url=authorize_landing)
        raise AssertionError(f"unexpected call: {record['method']} {url}")

    recorder = _Recorder(handler)
    with patch.object(auth_flow, "request_with_retry", recorder), patch.object(
        auth_flow, "_authorize_continue_sentinel", lambda *a, **k: ({}, "", "")
    ):
        result = auth_flow._login_existing_account_with_email_otp(
            session=session,
            username=USERNAME,
            mailbox=None,
            did="did-1",
            session_logging_id="slog-1",
            auth_base=AUTH_BASE,
            chat_base=CHAT_BASE,
            base_headers={},
            csrf_token=caller_token,
            proxy="",
        )
    return result, signin_payloads, authorize_urls, recorder


class CsrfMintingTests(unittest.TestCase):
    """``_fetch_session_csrf_token`` mints on the session it is handed."""

    def test_token_is_minted_on_the_session_that_will_be_used(self):
        session = object()
        recorder = _Recorder(lambda record: _Response(body={"csrfToken": SESSION_TOKEN}))

        with patch.object(auth_flow, "request_with_retry", recorder):
            token = auth_flow._fetch_session_csrf_token(
                session, CHAT_BASE, {"accept": "application/json"}, "did-1", "slog-1"
            )

        self.assertEqual(token, SESSION_TOKEN)
        # Prime first, then read the token: the same order the signup lane uses,
        # and the reason the cookie and the token end up on the same client.
        self.assertEqual(recorder.urls(), [f"{CHAT_BASE}/", f"{CHAT_BASE}/api/auth/csrf"])
        self.assertTrue(all(record["session"] is session for record in recorder.calls))

    def test_a_transport_failure_yields_no_token(self):
        def handler(record):
            raise RuntimeError("connection reset")

        with patch.object(auth_flow, "request_with_retry", _Recorder(handler)):
            token = auth_flow._fetch_session_csrf_token(
                object(), CHAT_BASE, {}, "did-1", "slog-1"
            )

        self.assertEqual(token, "")

    def test_a_non_json_body_yields_no_token(self):
        class _NoJson:
            status_code = 200
            text = "<html>sign in</html>"

            def json(self):
                raise ValueError("not json")

        with patch.object(auth_flow, "request_with_retry", _Recorder(lambda record: _NoJson())):
            token = auth_flow._fetch_session_csrf_token(
                object(), CHAT_BASE, {}, "did-1", "slog-1"
            )

        self.assertEqual(token, "")


class SigninTokenSourceTests(unittest.TestCase):
    """The signin POST must carry a token minted on its own session."""

    def test_signin_uses_the_session_minted_token_not_the_callers(self):
        _, signin_payloads, _, _ = _drive(
            mint_token=SESSION_TOKEN, authorize_landing=LOGIN_LANDING
        )

        self.assertEqual(len(signin_payloads), 1)
        self.assertEqual(signin_payloads[0]["csrfToken"], [SESSION_TOKEN])
        self.assertNotEqual(signin_payloads[0]["csrfToken"], [CALLER_TOKEN])

    def test_a_failed_mint_falls_back_to_the_callers_token(self):
        _, signin_payloads, _, _ = _drive(
            mint_token=None, authorize_landing=LOGIN_LANDING
        )

        self.assertEqual(len(signin_payloads), 1)
        self.assertEqual(signin_payloads[0]["csrfToken"], [CALLER_TOKEN])


class LandingPredicateTests(unittest.TestCase):
    """The control for the guard: it must fire on *one* landing, not on any.

    This has to assert the predicate directly. The end-to-end callback case
    below cannot serve as the control -- that URL is accepted by the
    "already signed in" branch *before* the guard is reached, so it would pass
    even if the guard matched every chatgpt.com URL.
    """

    def test_only_the_login_page_counts_as_a_login_landing(self):
        self.assertTrue(auth_flow._is_chatgpt_auth_login_landing(LOGIN_LANDING))
        self.assertFalse(auth_flow._is_chatgpt_auth_login_landing(CALLBACK_LANDING))
        self.assertFalse(auth_flow._is_chatgpt_auth_login_landing(f"{AUTH_BASE}/log-in"))
        self.assertFalse(auth_flow._is_chatgpt_auth_login_landing(""))


class LoginLandingGuardTests(unittest.TestCase):
    """Landing on the chatgpt login page must stop before ``continue``."""

    def test_login_landing_stops_before_the_doomed_continue(self):
        result, _, _, recorder = _drive(
            mint_token=SESSION_TOKEN, authorize_landing=LOGIN_LANDING
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith(GUARD_PREFIX))
        self.assertNotIn(f"{AUTH_BASE}/api/accounts/authorize/continue", recorder.urls())
        self.assertFalse([url for url in recorder.urls() if "email-otp" in url])

    def test_the_callback_landing_still_returns_ok(self):
        """Regression guard for the pre-existing "already signed in" branch."""
        result, _, _, recorder = _drive(
            mint_token=SESSION_TOKEN, authorize_landing=CALLBACK_LANDING
        )

        self.assertTrue(result["ok"])
        self.assertNotIn(f"{AUTH_BASE}/api/accounts/authorize/continue", recorder.urls())

    def test_the_guard_error_stays_a_retryable_auth_state(self):
        result, _, _, _ = _drive(
            mint_token=SESSION_TOKEN, authorize_landing=LOGIN_LANDING
        )

        # Naming it after the failure it pre-empts is deliberate: a bare new
        # name would classify as ``unknown`` (terminal) and silently drop the
        # retry the current 409 path still gets.
        self.assertEqual(classify_error(result["error"]), "auth_state")
        self.assertTrue(registration_retry_decision(result["error"]).retryable)


class AuthorizeContextTests(unittest.TestCase):
    """The authorize URL must carry the context the signup lane fills in."""

    def test_authorize_url_carries_the_login_context(self):
        _, _, authorize_urls, _ = _drive(
            mint_token=SESSION_TOKEN, authorize_landing=LOGIN_LANDING
        )

        self.assertEqual(len(authorize_urls), 1)
        query = parse_qs(urlparse(authorize_urls[0]).query)
        self.assertEqual(query.get("auth_session_logging_id"), ["slog-1"])
        self.assertEqual(query.get("login_hint"), [USERNAME])
        self.assertEqual(query.get("device_id"), ["did-1"])
        self.assertEqual(query.get("ext-oai-did"), ["did-1"])
        self.assertTrue(query.get("ccaps"))


if __name__ == "__main__":
    unittest.main()
