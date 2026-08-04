import sys
import types
from unittest.mock import MagicMock, patch

from sms_tool import session_refresh


def test_protocol_candidate_can_be_returned_without_persistence():
    data = {"email": "ok@example.com", "cookie_header": "__Secure-next-auth.session-token=cookie"}
    auth_session = {
        "accessToken": "new_at",
        "refreshToken": "rt_new",
        "user": {"email": "ok@example.com"},
    }
    with (
        patch.object(session_refresh, "_fetch_protocol_auth_session", return_value=auth_session),
        patch.object(session_refresh, "_save_refreshed") as save,
    ):
        result = session_refresh._refresh_session_protocol(
            data,
            "session.json",
            "ok@example.com",
            30,
            persist=False,
        )

    assert result["ok"]
    assert not result["persisted"]
    assert result["data"]["access_token"] == "new_at"
    assert result["data"]["oauth_refresh_token"] == "rt_new"
    save.assert_not_called()


def test_browser_candidate_uses_proxy_and_stays_unpersisted():
    auth_session = {"accessToken": "new_at", "user": {"email": "ok@example.com"}}

    class Response:
        status = 200

        @staticmethod
        def json():
            return auth_session

    class Request:
        @staticmethod
        def get(*args, **kwargs):
            return Response()

    class Page:
        url = "https://chatgpt.com/"

        @staticmethod
        def goto(*args, **kwargs):
            return None

    class Context:
        request = Request()

        @staticmethod
        def new_page():
            return Page()

        @staticmethod
        def cookies():
            return []

        @staticmethod
        def add_cookies(*args, **kwargs):
            return None

    class Browser:
        @staticmethod
        def new_context(**kwargs):
            return Context()

        @staticmethod
        def close():
            return None

    launch = MagicMock(return_value=Browser())
    cloakbrowser = types.SimpleNamespace(launch=launch)
    with (
        patch.dict(sys.modules, {"cloakbrowser": cloakbrowser}),
        patch.object(session_refresh, "_save_refreshed") as save,
    ):
        result = session_refresh._refresh_session_browser(
            {"email": "ok@example.com"},
            "session.json",
            "ok@example.com",
            30,
            True,
            proxy="socks5://127.0.0.1:1080",
            persist=False,
            automated_login=True,
        )

    assert result["ok"]
    assert result["data"]["access_token"] == "new_at"
    assert launch.call_args.kwargs["proxy"] == "socks5://127.0.0.1:1080"
    save.assert_not_called()


def test_auth_session_email_reads_nested_session_user():
    assert session_refresh._auth_session_email({"session": {"user": {"email": "User@Example.com"}}}) == "user@example.com"


def test_browser_page_classifies_deactivated_account_text():
    body = MagicMock()
    body.inner_text.return_value = "This account has been deactivated."
    page = MagicMock(url="https://auth.openai.com/log-in/password")
    page.locator.return_value = body

    assert session_refresh._browser_login_page_error(page) == "account_deactivated"
