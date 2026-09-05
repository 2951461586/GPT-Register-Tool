from sms_tool.registration_drivers.browser_flow.recovery import (
    is_navigation_retryable,
    probe_pending,
    stage_timeout,
)
from sms_tool.proxy_health import ProxyHealthTracker
from sms_tool.registration_retry_guard import RegistrationRetryGuard
from sms_tool.registration_drivers import driver_capabilities


def test_navigation_abort_is_retryable():
    assert is_navigation_retryable("Error: Page.goto: NS_ERROR_ABORT")
    assert is_navigation_retryable("curl: (35) SSL_ERROR_SYSCALL")


def test_stage_timeout_is_bounded_and_configurable():
    config = {"registration": {"stage_timeouts": {"auth_flow": 75}}}
    assert stage_timeout(config, "auth_flow", 30) == 75
    assert stage_timeout(config, "auth_flow", 30, maximum=60) == 60
    assert stage_timeout({}, "auth_flow", 30) == 30


def test_transport_unknown_probe_is_deferred():
    assert probe_pending("at", {"status_code": 0}, False)
    assert not probe_pending("at", {"status_code": 401}, False)
    assert not probe_pending("", {"status_code": 0}, False)


def test_proxy_health_tracker_ranks_and_sanitizes_endpoint(tmp_path):
    tracker = ProxyHealthTracker({}, path=tmp_path / "proxy-health.json")
    tracker.record("http://user:pass@proxy-a.test:8000", ok=False, error="timeout")
    tracker.record("http://proxy-b.test:8000", ok=True)
    assert ProxyHealthTracker.key("http://user:pass@proxy-a.test:8000").startswith("proxy-a.test:8000#sid-")
    assert tracker.rank([
        "http://user:pass@proxy-a.test:8000",
        "http://proxy-b.test:8000",
    ])[0].endswith("proxy-b.test:8000")


def test_retry_guard_cools_down_repeated_same_class_failures(tmp_path):
    guard = RegistrationRetryGuard({}, path=tmp_path / "retry-guard.json", threshold=2, cooldown_seconds=120)
    assert not guard.check("a@example.com")["deferred"]
    guard.record("a@example.com", failure_class="auth_state", error="unknown")
    assert not guard.check("a@example.com")["deferred"]
    guard.record("a@example.com", failure_class="auth_state", error="unknown")
    state = guard.check("a@example.com")
    assert state["deferred"]
    assert state["consecutive"] == 2
    guard.record("a@example.com", success=True)
    assert not guard.check("a@example.com")["deferred"]


def test_driver_capabilities_describe_camoufox_context_reuse_limit():
    capabilities = driver_capabilities("camoufox")
    assert capabilities["is_browser"]
    assert capabilities["supports_browser_fetch"]
    assert not capabilities["supports_context_reuse"]
