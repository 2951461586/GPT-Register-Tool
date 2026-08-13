from sms_tool import account_2fa
from sms_tool.mailbox_types import MailboxAccount
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_state import RegistrationStateMachine


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if url.endswith("/mfa_info"):
            enabled = len([call for call in self.get_calls if call[0].endswith("/mfa_info")]) > 1
            return FakeResponse({"mfa_enabled": enabled, "factors": {"totp": enabled}})
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if url.endswith("/mfa/enroll"):
            return FakeResponse({"secret": "JBSWY3DPEHPK3PXP", "session_id": "sid"})
        if url.endswith("/activate_enrollment"):
            return FakeResponse({"success": True})
        raise AssertionError(url)


def test_newly_registered_session_uses_inline_totp_enrollment():
    session = FakeSession()
    result = account_2fa.setup_totp_2fa(
        session=session,
        email="icloud@example.com",
        access_token="at",
        did="did",
        base_headers={"user-agent": "test"},
        poll_otp_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("OTP fallback must not run")),
    )
    assert result["ok"] is True
    assert result["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert [url for url, _ in session.post_calls] == [
        "https://chatgpt.com/backend-api/accounts/mfa/enroll",
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment",
    ]


def test_recent_auth_failure_uses_existing_account_reauth_callback():
    class ReauthSession(FakeSession):
        def post(self, url, **kwargs):
            if url.endswith("/mfa/enroll") and kwargs["headers"]["Authorization"] == "Bearer stale-at":
                self.post_calls.append((url, kwargs))
                return FakeResponse({"error": "recent_auth_required"}, status_code=401)
            return super().post(url, **kwargs)

    session = ReauthSession()
    reauth_calls = []
    result = account_2fa.setup_totp_2fa(
        session=session,
        email="icloud@example.com",
        access_token="stale-at",
        did="did",
        base_headers={"user-agent": "test"},
        poll_otp_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback must not run")),
        reauth_login_fn=lambda: reauth_calls.append(True) or "fresh-at",
    )

    assert result["ok"] is True
    assert result["access_token"] == "fresh-at"
    assert reauth_calls == [True]
    enroll_headers = [kwargs["headers"]["Authorization"] for url, kwargs in session.post_calls if url.endswith("/mfa/enroll")]
    assert enroll_headers == ["Bearer stale-at", "Bearer fresh-at"]


def test_icloud_registration_reuses_mailbox_for_reauth_otp(monkeypatch):
    polled = {}

    class MailboxService:
        def poll_otp(self, mailbox, **kwargs):
            polled.update({"mailbox": mailbox, **kwargs})
            return "654321"

    class Operations:
        REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS = "OpenAI"

        @staticmethod
        def _sanitize_text(value):
            return str(value)

    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *args: None),
        operations=Operations(),
    )
    workflow.runtime.success = True
    workflow.runtime.access_token = "at"
    workflow.runtime.email_code = "123456"
    workflow.runtime.mailbox = MailboxAccount(
        email="icloud@example.com",
        provider="icloud_url",
        token="https://mail.example/receive",
    )
    workflow.runtime.mailbox_service = MailboxService()
    workflow.runtime.session = object()
    workflow.runtime.username = "icloud@example.com"
    workflow.runtime.device_id = "did"

    def fake_setup(**kwargs):
        assert kwargs["excluded_otps"] == {"123456"}
        assert kwargs["poll_otp_fn"]("icloud@example.com", issued_after_unix=10, excluded_otps={"123456"}) == "654321"
        return {"ok": True, "totp_secret": "SECRET", "access_token": "fresh-at"}

    monkeypatch.setattr(account_2fa, "setup_totp_2fa", fake_setup)
    workflow.enroll_totp()

    assert workflow.runtime.totp_secret == "SECRET"
    assert workflow.runtime.access_token == "fresh-at"
    assert polled["mailbox"].provider == "icloud_url"
    assert polled["excluded_otps"] == {"123456"}
