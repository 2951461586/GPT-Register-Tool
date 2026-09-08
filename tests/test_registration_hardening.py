"""Offline regressions for registration lifecycle and configuration fixes."""

from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import MagicMock, patch

import pytest

from sms_tool.browser_pool import BrowserProcessPool
from sms_tool.proxy_entry import rotate_session
from sms_tool.registration_drivers import external_sessions as es
from sms_tool.registration_drivers.external_sessions import managed
from sms_tool.registration_drivers.browser_flow import form_steps
from sms_tool.registration_drivers.browser_flow.session import _bind_totp_in_browser


def test_pool_annotations_resolve():
    assert "session_factory" in get_type_hints(BrowserProcessPool.__init__)


@pytest.mark.parametrize("separator", ["-", "_"])
def test_rotate_session_preserves_provider_template(separator):
    username = f"user{separator}sid{separator}Ab123{separator}t{separator}10"
    proxy = f"http://{username}:pass@proxy.example:8080"
    with patch("sms_tool.proxy_entry._random_session_id", return_value="New12"):
        rotated = rotate_session(proxy)
    assert rotated == proxy.replace("Ab123", "New12")


def test_rotate_session_does_not_match_partial_marker():
    proxy = "http://user_xsid_Ab123_t_10:pass@proxy.example:8080"
    assert rotate_session(proxy) == proxy


@pytest.fixture
def fake_camoufox(monkeypatch):
    context = MagicMock()
    context.pages = [MagicMock()]
    manager = MagicMock()
    manager.__enter__.return_value = context
    factory = MagicMock(return_value=manager)
    monkeypatch.setitem(__import__("sys").modules, "camoufox.sync_api", SimpleNamespace(Camoufox=factory))
    monkeypatch.setitem(__import__("sys").modules, "browserforge.fingerprints", SimpleNamespace(Screen=MagicMock()))
    monkeypatch.setattr(managed, "apply_playwright_stealth", lambda *a, **k: {})
    return factory, manager


def test_camoufox_owned_profile_is_removed_on_close(fake_camoufox):
    factory, manager = fake_camoufox
    session = es.CamoufoxBrowserSession(config={})
    session.__enter__()
    profile = Path(factory.call_args.kwargs["user_data_dir"])
    assert profile.is_dir()
    session.close()
    session.close()
    assert not profile.exists()
    manager.__exit__.assert_called_once()


def test_camoufox_user_profile_is_never_removed(fake_camoufox, tmp_path):
    marker = tmp_path / "user-data.txt"
    marker.write_text("keep", encoding="utf-8")
    session = es.CamoufoxBrowserSession(
        config={"registration": {"drivers": {"camoufox": {"user_data_dir": str(tmp_path)}}}}
    )
    with session:
        pass
    assert marker.read_text(encoding="utf-8") == "keep"


def test_camoufox_failed_launch_cleans_owned_profile_even_with_keep_open(fake_camoufox):
    factory, manager = fake_camoufox
    manager.__enter__.side_effect = RuntimeError("fake launch failure")
    session = es.CamoufoxBrowserSession(
        config={"registration": {"drivers": {"camoufox": {"keep_browser_open": True}}}}
    )
    with pytest.raises(es.BrowserRegistrationError):
        session.__enter__()
    assert not Path(factory.call_args.kwargs["user_data_dir"]).exists()


def test_camoufox_keep_open_preserves_owned_profile(fake_camoufox):
    factory, _ = fake_camoufox
    session = es.CamoufoxBrowserSession(
        config={"registration": {"drivers": {"camoufox": {"keep_browser_open": True}}}}
    )
    try:
        session.__enter__()
        profile = Path(factory.call_args.kwargs["user_data_dir"])
        session.close()
        assert profile.exists()
    finally:
        session.driver_config["keep_browser_open"] = False
        session.close()
    assert not profile.exists()


def test_totp_uses_configured_origin_for_both_requests():
    page = MagicMock()
    page.evaluate.side_effect = [
        {"status": 200, "body": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "test"}},
        {"status": 200, "body": {"success": True}},
    ]
    result = _bind_totp_in_browser(page, "test-token", "test-device", chat_base="https://chat.example/")
    assert result["ok"]
    assert page.evaluate.call_count == 2
    for call in page.evaluate.call_args_list:
        script, args = call.args
        assert args[0].startswith("https://chat.example/backend-api/")
        assert "https://chatgpt.com" not in script


def test_totp_requests_carry_an_in_page_deadline():
    """P0-4: ``page.evaluate`` is not governed by Playwright's default timeout.

    Without an in-page abort the MFA enroll/activate fetches could block a
    registration worker forever; the budget has to reach the page, not just
    the Python side.
    """
    page = MagicMock()
    page.evaluate.side_effect = [
        {"status": 200, "body": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "test"}},
        {"status": 200, "body": {"success": True}},
    ]
    assert _bind_totp_in_browser(
        page, "tok", "did", chat_base="https://chat.example/", budget_ms=1234,
    )["ok"]
    assert page.evaluate.call_count == 2
    for call in page.evaluate.call_args_list:
        script, args = call.args
        assert "AbortController" in script
        assert "signal: controller.signal" in script
        assert args[-1] == 1234


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_totp_deadline_falls_back_on_invalid_budget(bad):
    page = MagicMock()
    page.evaluate.side_effect = [
        {"status": 200, "body": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "test"}},
        {"status": 200, "body": {"success": True}},
    ]
    assert _bind_totp_in_browser(
        page, "tok", "did", chat_base="https://chat.example/", budget_ms=bad,
    )["ok"]
    for call in page.evaluate.call_args_list:
        assert call.args[1][-1] == 20_000


def test_nextauth_email_submit_carries_an_in_page_deadline():
    page = MagicMock()
    page.evaluate.return_value = {"ok": True}
    assert form_steps._submit_email_via_nextauth(page, "a@example.com", budget_ms=4321) is True
    script, payload = page.evaluate.call_args.args
    assert "AbortController" in script
    assert payload["budgetMs"] == 4321


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_nextauth_deadline_falls_back_on_invalid_budget(bad):
    """P0-4: an invalid nextauth budget must not degrade into an instant abort.

    The budget reaches the page as ``budgetMs`` and drives
    ``setTimeout(() => controller.abort(), budgetMs)``.  A zero/negative/NaN
    value would abort the csrf and signin requests before they ever leave the
    page, so a misconfigured budget surfaces as "email submit failed" rather
    than a timeout -- the failure mode this guard exists to prevent.
    """
    page = MagicMock()
    page.evaluate.return_value = {"ok": True}
    assert form_steps._submit_email_via_nextauth(page, "a@example.com", budget_ms=bad) is True
    _, payload = page.evaluate.call_args.args
    assert payload["budgetMs"] == 20_000
