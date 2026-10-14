from sms_tool.registration_drivers.browser_flow.context import prepare_browser_context


def test_browser_context_applies_driver_overrides():
    context = prepare_browser_context({
        "registration": {"browser_headless": False, "drivers": {"camoufox": {"headless": True, "start_url": "https://example.test"}}},
        "chatgpt": {"chat_base_url": "https://chat.example", "auth_base_url": "https://auth.example"},
        "email_registration": {"otp_timeout": 42},
    }, "camoufox", None)
    assert context.headless is True
    assert context.start_url == "https://example.test"
    assert context.otp_timeout == 42


def test_browser_context_honors_explicit_headless_argument():
    context = prepare_browser_context({}, "playwright", False)
    assert context.headless is False
    assert context.start_url == "https://chatgpt.com/auth/login"
