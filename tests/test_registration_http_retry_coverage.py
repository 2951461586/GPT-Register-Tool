"""P2 — the registration lane must reach OpenAI through ``request_with_retry``.

Twelve call sites used to call ``session.get`` / ``session.post`` directly, so
they silently lost both halves of the transport policy:

* transient transport errors (TLS reset, curl 35/56, connect timeout) aborted
  the step on the first try instead of being retried;
* an auth 403/429 did not open the session circuit, so every later call in the
  same registration kept hammering an edge that had already said "back off".

The tests below pin the behavioural half (retry actually happens, the circuit
actually opens).  ``TestNoRawSessionVerbInTheRegistrationLane`` pins the
structural half so a new raw ``session.get`` cannot quietly reappear.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from sms_tool import http_client
from sms_tool.accounts import account_2fa
from sms_tool.sentinel import client as sentinel_client
from sms_tool import registration_preflight


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

TRANSIENT_RESET = "curl: (35) OpenSSL SSL_connect: connection reset by peer"


class _FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.headers = {} if headers is None else dict(headers)

    def json(self):
        return self._payload


class _zero_retry_delay:
    """Take the backoff out of the retry so the tests stay fast.

    ``request_with_retry`` reads ``request_retry_delay()`` as a module global at
    call time, so swapping the function is enough -- no patching of ``time``.
    """

    def __enter__(self):
        self._original = http_client.request_retry_delay
        http_client.request_retry_delay = lambda: 0.0
        return self

    def __exit__(self, *exc_info):
        http_client.request_retry_delay = self._original
        return False


# --------------------------------------------------------------------------
# 2FA: nine call sites
# --------------------------------------------------------------------------
class TestTwoFactorUsesTheTransportRetry(unittest.TestCase):
    def test_transport_error_is_retried_instead_of_failing_enrollment(self):
        with _zero_retry_delay():
            session = _FlakyEnrollSession()
            result = account_2fa.setup_totp_2fa(
                session=session,
                email="retry@example.com",
                access_token="at",
                did="did",
                base_headers={"user-agent": "test"},
                poll_otp_fn=lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("the OTP fallback must not run")
                ),
            )

        self.assertTrue(result.get("ok"), result.get("error"))
        self.assertEqual(result.get("totp_secret"), "JBSWY3DPEHPK3PXP")
        # First attempt hit the transient reset, the retry succeeded.
        self.assertEqual(session.enroll_attempts, 2)

    def test_auth_429_opens_the_circuit_for_the_rest_of_the_session(self):
        session = _ThrottledSession()
        result = account_2fa.setup_totp_2fa(
            session=session,
            email="throttled@example.com",
            access_token="at",
            did="did",
            base_headers={"user-agent": "test"},
            poll_otp_fn=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the OTP fallback must not run")
            ),
        )

        self.assertFalse(result.get("ok"))
        self.assertIn("session_circuit_open", str(result.get("error")))
        # The circuit is checked *before* the request is issued, so the enroll
        # POST never reached the wire.
        self.assertEqual(session.post_calls, [])


class _FlakyEnrollSession:
    """Fails the first MFA enroll with a transient transport error."""

    def __init__(self):
        self.enroll_attempts = 0
        self.post_calls = []

    def get(self, url, **kwargs):
        if url.endswith("/mfa_info"):
            return _FakeResponse({"mfa_enabled": False, "factors": {}})
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        if url.endswith("/mfa/enroll"):
            self.enroll_attempts += 1
            if self.enroll_attempts == 1:
                raise RuntimeError(TRANSIENT_RESET)
            return _FakeResponse({"secret": "JBSWY3DPEHPK3PXP", "session_id": "sid"})
        if url.endswith("/activate_enrollment"):
            return _FakeResponse({"success": True})
        raise AssertionError(url)


class _ThrottledSession:
    """Answers the very first call with 429 so the circuit opens."""

    def __init__(self):
        self.post_calls = []

    def get(self, url, **kwargs):
        if url.endswith("/mfa_info"):
            return _FakeResponse({}, status_code=429)
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        raise AssertionError(f"the circuit must have blocked this POST: {url}")


# --------------------------------------------------------------------------
# Sentinel: one call site
# --------------------------------------------------------------------------
class TestSentinelChallengeUsesTheTransportRetry(unittest.TestCase):
    def test_transport_error_is_retried(self):
        with _zero_retry_delay():
            session = _FlakySentinelSession()
            payload = sentinel_client._challenge(
                session,
                flow="username_password_create",
                device_id="device",
                profile={},
                timeout_seconds=30,
            )

        self.assertEqual(payload["token"], "sentinel-token")
        self.assertEqual(session.attempts, 2)


class _FlakySentinelSession:
    def __init__(self):
        self.attempts = 0

    def post(self, url, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("curl: (56) Failure in receiving network data")
        return _FakeResponse({"token": "sentinel-token"})


# --------------------------------------------------------------------------
# Preflight: two call sites
# --------------------------------------------------------------------------
class TestProxySchemeProbeSurvivesATransientBlip(unittest.TestCase):
    """A single reset used to be answered "socks5 is broken" -> scheme downgrade.

    ``_proxy_scheme_reachable`` is a yes/no probe whose answer selects the
    transport for the whole run, so a transient reset had to be retried rather
    than believed.
    """

    def test_one_reset_does_not_declare_the_scheme_broken(self):
        original_session = registration_preflight.curl_requests.Session
        attempts = []

        class _ProbeSession:
            trust_env = True
            proxies = {}

            def get(self, url, **kwargs):
                attempts.append(url)
                if len(attempts) == 1:
                    raise RuntimeError(TRANSIENT_RESET)
                return _FakeResponse({}, status_code=200)

            def close(self):
                pass

        with _zero_retry_delay():
            registration_preflight.curl_requests.Session = lambda: _ProbeSession()
            try:
                reachable = registration_preflight._proxy_scheme_reachable(
                    "socks5h://user:pw@127.0.0.1:1080", "https://chatgpt.com/robots.txt"
                )
            finally:
                registration_preflight.curl_requests.Session = original_session

        self.assertTrue(reachable)
        self.assertEqual(len(attempts), 2)


# --------------------------------------------------------------------------
# Structural gate
# --------------------------------------------------------------------------
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "request"}

GUARDED_MODULES = (
    "sms_tool/accounts/account_2fa.py",
    "sms_tool/sentinel/client.py",
    "sms_tool/registration_preflight.py",
)


def _raw_session_calls(source: str) -> list[tuple[int, str, str]]:
    """Every ``<something-session>.<verb>(...)`` call in *source*.

    AST rather than a text scan: these modules legitimately mention
    ``session.get`` in docstrings and in the ``request_with_retry`` kwargs, and
    a regex would flag both.
    """
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_VERBS:
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and "session" in owner.id.lower():
            offenders.append((node.lineno, owner.id, func.attr))
    return offenders


class TestNoRawSessionVerbInTheRegistrationLane(unittest.TestCase):
    def test_guarded_modules_route_every_verb_through_retry(self):
        for relative in GUARDED_MODULES:
            source = (REPO_ROOT / relative).read_text(encoding="utf-8")
            self.assertEqual(
                _raw_session_calls(source), [], f"raw session verb in {relative}"
            )

    def test_scanner_flags_a_raw_verb(self):
        """Negative test: the gate above must not be a no-op."""
        sample = (
            "def step(session):\n"
            "    return session.get('https://chatgpt.com', timeout=15)\n"
        )
        self.assertEqual(_raw_session_calls(sample), [(2, "session", "get")])

    def test_scanner_ignores_a_plain_variable(self):
        """And it must not fire on an unrelated object."""
        sample = "def step(profile):\n    return profile.get('user_agent')\n"
        self.assertEqual(_raw_session_calls(sample), [])


if __name__ == "__main__":
    unittest.main()
