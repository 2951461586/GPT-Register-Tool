from unittest.mock import MagicMock, patch

import pytest

from sms_tool.registration_drivers.base import BrowserRegistrationError
from sms_tool.registration_drivers.browser_flow import dom_fields, form_steps


def test_email_selector_excludes_disabled_and_readonly_controls():
    for selector in dom_fields.EDITABLE_EMAIL_SELECTOR.split(", "):
        assert ":not(:disabled)" in selector
        assert ":not([readonly])" in selector
        assert ":not([aria-disabled='true'])" in selector


def test_fill_race_is_bounded_and_classified_without_forcing_input():
    page = MagicMock()
    page.locator.return_value.first.fill.side_effect = TimeoutError("disabled during hydration")
    with patch.object(form_steps.page_state, "_manual_challenge", return_value=False):
        with pytest.raises(BrowserRegistrationError, match="browser_email_field_not_editable"):
            form_steps._fill_email(page, "test@example.com")
    page.locator.return_value.first.fill.assert_called_once_with("test@example.com", timeout=5_000)
    page.evaluate.assert_not_called()


def test_fallback_selectors_share_one_wait_budget():
    page = MagicMock()
    page.locator.return_value.first.wait_for.side_effect = TimeoutError()
    selectors = tuple(f"input[name='field{i}']" for i in range(8))
    assert dom_fields._first_visible(page, selectors, timeout_ms=1000) is None
    calls = page.locator.return_value.first.wait_for.call_args_list
    assert sum(call.kwargs["timeout"] for call in calls) <= 1000
