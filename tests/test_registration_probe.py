"""Read-only registration-status probe: classification and no-side-effect run."""

from __future__ import annotations

from unittest.mock import patch

from sms_tool import registration_probe as rp


class _Resp:
    def __init__(self, status_code=200, payload=None, url="", headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.url = url
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeCookies:
    def set(self, *args, **kwargs):  # noqa: ARG002 - cookie seeding is irrelevant here
        return None


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.closed = False

    def close(self):
        self.closed = True


def _responses(landing_url: str, payload=None):
    return [
        _Resp(),  # prime
        _Resp(payload={"csrfToken": "csrf-fixture"}),
        _Resp(payload={"url": "https://auth.openai.com/api/accounts/authorize?x=1"}),
        _Resp(url=landing_url, payload=payload or {}),
    ]


def _probe_with_landing(landing_url: str, payload=None):
    with patch("sms_tool.auth_flow.deps.request_with_retry", side_effect=_responses(landing_url, payload)):
        return rp.probe_registration("User@Example.com", session=_FakeSession())


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------
def test_classify_create_account_is_unregistered():
    status, reason = rp.classify_registration_landing("https://auth.openai.com/create-account/password")
    assert (status, reason) == (rp.STATUS_UNREGISTERED, "create_account_password")


def test_classify_login_password_is_registered():
    status, reason = rp.classify_registration_landing("https://auth.openai.com/log-in/password")
    assert (status, reason) == (rp.STATUS_REGISTERED, "login_password")


def test_classify_mfa_page_type_is_registered():
    status, reason = rp.classify_registration_landing(
        "https://auth.openai.com/email-verification", page_type="mfa_challenge"
    )
    assert (status, reason) == (rp.STATUS_REGISTERED, "mfa_challenge")


def test_classify_passwordless_signup_mode_is_unregistered():
    status, reason = rp.classify_registration_landing(
        "https://auth.openai.com/email-verification", verification_mode="passwordless_signup"
    )
    assert (status, reason) == (rp.STATUS_UNREGISTERED, "passwordless_signup")


def test_classify_otp_without_mode_is_unknown():
    status, reason = rp.classify_registration_landing("https://auth.openai.com/email-verification")
    assert (status, reason) == (rp.STATUS_UNKNOWN, "email_verification_without_mode")


def test_classify_chatgpt_login_landing_is_unknown():
    status, _ = rp.classify_registration_landing("https://chatgpt.com/auth/login")
    assert status == rp.STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# End-to-end (patched transport)
# ---------------------------------------------------------------------------
def test_probe_reports_unregistered_on_create_account_landing():
    result = _probe_with_landing("https://auth.openai.com/create-account/password")
    assert result["status"] == rp.STATUS_UNREGISTERED
    assert result["email"] == "user@example.com"


def test_probe_reports_registered_on_login_landing():
    result = _probe_with_landing("https://auth.openai.com/log-in/password", {"page": {"type": "login_password"}})
    assert result["status"] == rp.STATUS_REGISTERED
    assert result["page_type"] == "login_password"


def test_probe_transport_failure_is_unknown_not_registered():
    with patch("sms_tool.auth_flow.deps.request_with_retry", side_effect=RuntimeError("proxy down")):
        result = rp.probe_registration("user@example.com", session=_FakeSession())

    assert result["status"] == rp.STATUS_UNKNOWN
    assert "proxy down" in result["error"]


def test_probe_never_posts_authorize_continue_or_otp():
    calls: list[tuple[str, str]] = []
    responses = _responses("https://auth.openai.com/log-in/password")

    def fake_retry(session, method, url, **kwargs):  # noqa: ARG001
        calls.append((method.lower(), url))
        return responses.pop(0)

    with patch("sms_tool.auth_flow.deps.request_with_retry", side_effect=fake_retry):
        rp.probe_registration("user@example.com", session=_FakeSession())

    assert all("authorize/continue" not in url for _, url in calls)
    assert all("email-otp" not in url for _, url in calls)
    assert all("user/register" not in url for _, url in calls)
    assert all("create_account" not in url for _, url in calls)
