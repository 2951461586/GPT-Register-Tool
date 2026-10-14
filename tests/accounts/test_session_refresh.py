import json
from unittest.mock import patch

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


def test_auth_session_email_reads_nested_session_user():
    assert session_refresh._auth_session_email({"session": {"user": {"email": "User@Example.com"}}}) == "user@example.com"


def test_browser_refresh_paths_are_removed():
    # Browser-based re-login was deleted; only the protocol path remains.
    assert not hasattr(session_refresh, "_refresh_session_browser")
    assert not hasattr(session_refresh, "_complete_browser_email_login")
    assert "browser" not in session_refresh.refresh_session.__doc__.lower() or "removed" in session_refresh.refresh_session.__doc__.lower()


def test_explicit_session_file_rehydrates_flattened_mailbox_credentials(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "email": "liziai@smailr.com",
        "mailbox": {"email": "liziai@smailr.com", "provider": "smailr"},
    }), encoding="utf-8")
    record = {
        "email": "liziai@smailr.com",
        "mailbox_provider": "smailr",
        "mailbox_source": "purchase",
        "mailbox_token": "mailbox-token",
        "mailbox_refresh_token": "",
    }

    with patch.object(session_refresh, "get_account_record", return_value=record):
        data, json_path = session_refresh._load_seed_session(session_file=str(path))

    assert json_path == str(path)
    assert data["mailbox_provider"] == "smailr"
    assert data["mailbox_source"] == "purchase"
    assert data["mailbox_token"] == "mailbox-token"


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Clock:
    """Deterministic stand-in for the ``time`` module inside the fetch loop."""

    def __init__(self, start=1000.0, step=2.0):
        self.now = start
        self.step = step
        self.waits = []

    def time(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def _session_returning(responses, calls):
    class _FakeSession:
        def __init__(self):
            self.proxies = None

        def get(self, *args, **kwargs):
            calls.append(1)
            return responses[min(len(calls) - 1, len(responses) - 1)]

    return _FakeSession


def test_blocked_exit_is_reported_and_aborts_instead_of_burning_the_budget():
    """A 403 carrying a Retry-After longer than the strategy budget must stop the
    loop at once. Sleeping it out would spend the whole budget and still fail,
    while returning early lets the recovery chain switch to another exit."""
    clock = _Clock(step=0.5)
    calls = []
    detail = {}

    with (
        patch.object(session_refresh, "time", clock),
        patch.object(
            session_refresh.curl_requests,
            "Session",
            _session_returning([_FakeResponse(403, headers={"Retry-After": "900"})], calls),
        ),
    ):
        body = session_refresh._fetch_protocol_auth_session(
            "cookie=1", timeout=30, proxy="http://blocked:1", detail=detail
        )

    assert body == {}
    assert len(calls) == 1, "a long Retry-After must not be slept out"
    # The distinguishing evidence is that no wait happened at all: sleeping the
    # remaining budget would also end the loop after one request, so counting
    # requests cannot tell the two apart.
    assert clock.waits == [], "the loop must break instead of sleeping the budget away"
    assert detail["last_status"] == "403"
    assert detail["http_statuses"] == ["403"]
    assert detail["retry_after"] == 900.0


def test_dead_session_is_reported_as_status_200_without_a_retry_after():
    """Positive control for the blocked-exit case: a 200 that simply carries no
    accessToken is a dead session, and must be reported as such so callers do not
    go looking for another exit."""
    clock = _Clock(step=2.0)
    calls = []
    detail = {}

    with (
        patch.object(session_refresh, "time", clock),
        patch.object(
            session_refresh.curl_requests,
            "Session",
            _session_returning([_FakeResponse(200, payload={"WARNING_BANNER": "x"})], calls),
        ),
    ):
        body = session_refresh._fetch_protocol_auth_session("cookie=1", timeout=30, detail=detail)

    assert body == {}
    assert len(calls) >= 2
    assert detail["last_status"] == "200"
    assert detail["retry_after"] == 0.0


def test_failed_protocol_refresh_reports_which_hop_failed():
    """Without the transport detail a blocked exit and a dead session produced
    the same string, and the recovery chain answered both by sending an OTP."""
    data = {"email": "blocked@example.com", "cookie_header": "__Secure-next-auth.session-token=cookie"}

    def fake_fetch(cookie_header, timeout=300, proxy=None, detail=None):
        if detail is not None:
            detail.update({"last_status": "403", "http_statuses": ["403", "403"], "retry_after": 30.0})
        return {}

    with patch.object(session_refresh, "_fetch_protocol_auth_session", side_effect=fake_fetch):
        result = session_refresh._refresh_session_protocol(
            data, "session.json", "blocked@example.com", 30, persist=False
        )

    assert result["ok"] is False
    assert result["error"] == "auth_session_missing_access_token"
    assert result["last_status"] == "403"
    assert result["http_statuses"] == ["403", "403"]
    assert result["retry_after"] == 30.0


def test_dead_session_refresh_reports_status_200_without_retry_after():
    data = {"email": "dead@example.com", "cookie_header": "__Secure-next-auth.session-token=cookie"}

    def fake_fetch(cookie_header, timeout=300, proxy=None, detail=None):
        if detail is not None:
            detail.update({"last_status": "200", "http_statuses": ["200"], "retry_after": 0.0})
        return {}

    with patch.object(session_refresh, "_fetch_protocol_auth_session", side_effect=fake_fetch):
        result = session_refresh._refresh_session_protocol(
            data, "session.json", "dead@example.com", 30, persist=False
        )

    assert result["last_status"] == "200"
    assert "retry_after" not in result


def test_unreachable_exit_gives_up_quickly_instead_of_burning_the_budget():
    """A dead exit raises instead of answering. Retrying it for the whole budget
    only delays the switch to another exit (measured 124s end to end), so it
    gets a small fixed number of attempts."""
    clock = _Clock(step=1.0)
    calls = []
    detail = {}

    class _DeadSession:
        def __init__(self):
            self.proxies = None

        def get(self, *args, **kwargs):
            calls.append(1)
            raise OSError("Failed to connect to chatgpt.com:443 over proxy 127.0.0.1")

    with (
        patch.object(session_refresh, "time", clock),
        patch.object(session_refresh.curl_requests, "Session", _DeadSession),
    ):
        body = session_refresh._fetch_protocol_auth_session("cookie=1", timeout=120, detail=detail)

    assert body == {}
    assert len(calls) == 3, "a dead exit must not consume the whole strategy budget"
    assert detail["last_status"].startswith("Failed to connect")


def test_http_level_failures_are_not_capped_like_connection_failures():
    """Positive control for the cap above: 403 bursts are observed to clear, so
    an HTTP-level failure keeps retrying within the budget rather than being cut
    off after the connection-failure limit."""
    clock = _Clock(step=1.0)
    calls = []
    detail = {}

    with (
        patch.object(session_refresh, "time", clock),
        patch.object(
            session_refresh.curl_requests,
            "Session",
            _session_returning([_FakeResponse(403, headers={})], calls),
        ),
    ):
        session_refresh._fetch_protocol_auth_session("cookie=1", timeout=60, detail=detail)

    assert len(calls) > 3, "HTTP-level failures must not be cut off by the connection cap"
    assert detail["last_status"] == "403"
