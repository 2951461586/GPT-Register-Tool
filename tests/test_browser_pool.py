"""Tests for the browser process pool (``sms_tool/browser_pool.py``)."""

from __future__ import annotations

import threading

import pytest

from sms_tool import browser_pool as pool_module
from sms_tool.browser_pool import (
    BrowserHealth,
    BrowserProcessPool,
    BrowserSlot,
    PoolConfig,
)
from sms_tool.registration_drivers.base import BrowserRegistrationError


class FakeBrowser:
    """Minimal stand-in for a connected browser session.

    Models the two-lifecycle contract the pool now relies on: a *process*
    (launched once per resident browser, torn down only on recycle) and a
    per-account *context* (created/released on every ``session()`` call when
    the resident is reused).
    """

    def __init__(self, recorder: list, fail_with: BaseException | None = None):
        self.recorder = recorder
        self.fail_with = fail_with
        self.process_closed = False
        self.context_count = 0
        self.page = object()

    def __enter__(self):
        self.recorder.append("enter")
        if self.fail_with is not None:
            raise self.fail_with
        return self

    def __exit__(self, exc_type, exc, tb):
        self.recorder.append("exit")
        return False

    def close(self):
        self.process_closed = True
        self.recorder.append("close")

    def release_account_context(self):
        self.context_count += 1
        self.recorder.append("release_context")

    def renew_account_context(self, **kwargs):
        if self.process_closed:
            raise RuntimeError("browser_not_resident")
        self.context_count += 1
        self.recorder.append("renew_context")
        return self


class RecordingFactory:
    """Session factory that records every call and returns a FakeBrowser."""

    def __init__(self, fail_with: BaseException | None = None):
        self.calls: list[dict] = []
        self.events: list[str] = []
        self.browsers: list[FakeBrowser] = []
        self.fail_with = fail_with

    def __call__(self, driver, **kwargs):
        self.calls.append({"driver": driver, **kwargs})
        browser = FakeBrowser(self.events, fail_with=self.fail_with)
        self.browsers.append(browser)
        return browser


def _pool(**overrides) -> BrowserProcessPool:
    config = {"registration": {"browser_process_pool": {"enabled": True, "max_concurrent": 2}}}
    return BrowserProcessPool(config, **overrides)


# --------------------------------------------------------------------------
# PoolConfig
# --------------------------------------------------------------------------

def test_pool_config_is_disabled_by_default():
    cfg = PoolConfig.from_config({})
    assert cfg.enabled is False
    assert (cfg.max_concurrent, cfg.max_uses_per_process) == (4, 10)


def test_pool_config_reads_registration_browser_process_pool():
    cfg = PoolConfig.from_config({
        "registration": {"browser_process_pool": {
            "enabled": True, "max_concurrent": 3, "max_uses_per_process": 7,
        }}
    })
    assert cfg.enabled is True
    assert cfg.max_concurrent == 3
    assert cfg.max_uses_per_process == 7


def test_pool_config_rejects_the_proxy_pool_alias_name():
    # ``browser_pool`` is a *proxy pool alias* elsewhere in the repo.  The
    # process pool must not silently pick it up if someone mis-keys config.
    cfg = PoolConfig.from_config({"registration": {"browser_pool": {"enabled": True}}})
    assert cfg.enabled is False


@pytest.mark.parametrize(
    "payload",
    [
        {"registration": None},
        {"registration": {"browser_process_pool": "yes"}},
        {"registration": {"browser_process_pool": {"max_concurrent": "many"}}},
        {"registration": {"browser_process_pool": {"max_concurrent": True}}},
    ],
)
def test_pool_config_falls_back_on_malformed_values(payload):
    assert PoolConfig.from_config(payload).max_concurrent == 4


def test_pool_config_clamps_zero_concurrency():
    cfg = PoolConfig.from_config({"registration": {"browser_process_pool": {"max_concurrent": 0}}})
    assert cfg.max_concurrent == 1


# --------------------------------------------------------------------------
# slot bookkeeping
# --------------------------------------------------------------------------

def test_slot_recycles_on_failure_and_on_use_limit():
    slot = BrowserSlot(slot_id=0)
    assert slot.needs_recycle(10) is False
    slot.health = BrowserHealth.FAILED
    assert slot.needs_recycle(10) is True
    slot.health = BrowserHealth.HEALTHY
    slot.uses = 10
    assert slot.needs_recycle(10) is True


# --------------------------------------------------------------------------
# session lifecycle
# --------------------------------------------------------------------------

def test_session_yields_browser_and_keeps_process_alive():
    factory = RecordingFactory()
    pool = _pool(session_factory=factory)
    with pool.session(proxy="http://user:pw@1.2.3.4:8080") as (browser, slot):
        assert browser is factory.browsers[0]
        assert slot.slot_id == 0
    # Real reuse: the process is NOT closed between accounts; only the
    # per-account context is released so cookies/storage don't leak.
    assert browser.process_closed is False
    assert browser.context_count == 1
    assert "release_context" in factory.events


def test_pool_reuses_resident_browser_across_same_proxy_accounts():
    factory = RecordingFactory()
    pool = _pool(session_factory=factory)
    proxy = "http://user:pw@1.2.3.4:8080"
    for _ in range(3):
        with pool.session(proxy=proxy, viewport=(1366, 768)) as (_browser, _slot):
            pass
    # One process launched, three per-account contexts created on it.
    assert len(factory.calls) == 1
    assert factory.browsers[0].process_closed is False
    # Each account gets at least one released/created context (isolation).
    assert factory.browsers[0].context_count >= 3
    # The resident survives until it is recycled by max_uses.
    assert pool.stats["resident_browsers"] == 1


def test_pool_relaunches_when_proxy_changes_to_preserve_isolation():
    factory = RecordingFactory()
    pool = _pool(session_factory=factory)
    with pool.session(proxy="http://1.1.1.1:1"):
        pass
    with pool.session(proxy="http://2.2.2.2:2"):
        pass
    # Different egress -> fresh browser process so accounts never share an
    # exit IP through a reused process.
    assert len(factory.calls) == 2
    assert [call["proxy"] for call in factory.calls] == [
        "http://1.1.1.1:1",
        "http://2.2.2.2:2",
    ]


def test_session_forwards_anti_linkage_parameters():
    factory = RecordingFactory()
    pool = _pool(session_factory=factory)
    with pool.session(
        proxy="http://1.2.3.4:8080",
        locale="de-DE",
        timezone_id="Europe/Berlin",
        browser_identity={"driver": "camoufox", "profile_id": "a@b.com"},
        viewport=(1366, 768),
    ) as (_browser, _slot):
        pass

    kwargs = factory.calls[0]
    # browser_identity carries the per-account profile id and viewport carries
    # the fingerprint pool's screen size; dropping either weakens isolation.
    assert kwargs["browser_identity"] == {"driver": "camoufox", "profile_id": "a@b.com"}
    assert kwargs["viewport"] == (1366, 768)
    assert kwargs["locale"] == "de-DE"
    assert kwargs["timezone_id"] == "Europe/Berlin"


def test_closed_pool_refuses_new_sessions():
    pool = _pool(session_factory=RecordingFactory())
    pool.close()
    with pytest.raises(BrowserRegistrationError) as excinfo:
        with pool.session():
            pass
    assert "browser_pool_closed" in str(excinfo.value)


def test_browser_registration_error_marks_slot_failed_and_recycles():
    factory = RecordingFactory(fail_with=BrowserRegistrationError("boom"))
    pool = _pool(session_factory=factory)
    with pytest.raises(BrowserRegistrationError):
        with pool.session():
            pass

    slot = pool._slots[0]
    # recycle_on_error is on by default, so the slot is reset for reuse.
    assert slot.health == BrowserHealth.HEALTHY
    assert slot.uses == 0
    assert slot.generation == 1


def test_unexpected_error_marks_slot_degraded():
    factory = RecordingFactory(fail_with=RuntimeError("kaboom"))
    pool = _pool(session_factory=factory)
    with pytest.raises(RuntimeError):
        with pool.session():
            pass

    slot = pool._slots[0]
    assert slot.health == BrowserHealth.DEGRADED
    assert slot.last_error == "kaboom"


def test_slot_uses_increments_and_healthy_path_resets_error():
    factory = RecordingFactory()
    pool = _pool(session_factory=factory)
    with pool.session() as (_browser, slot):
        slot.last_error = "stale"
    assert pool._slots[0].uses == 1
    assert pool._slots[0].health == BrowserHealth.HEALTHY


def test_concurrency_is_bounded_by_max_concurrent():
    config = {"registration": {"browser_process_pool": {"enabled": True, "max_concurrent": 2}}}
    factory = RecordingFactory()
    pool = BrowserProcessPool(config, session_factory=factory)
    assert len(pool._slots) == 2
    assert pool._semaphore._value == 2


def test_healthy_slot_is_preferred_over_degraded():
    factory = RecordingFactory()
    pool = _pool(session_factory=factory)
    pool._slots[0].health = BrowserHealth.DEGRADED
    with pool.session() as (_browser, slot):
        assert slot.slot_id == 1


def test_stats_snapshot_reports_pool_shape():
    factory = RecordingFactory()
    pool = _pool(driver="camoufox", session_factory=factory)
    stats = pool.stats
    assert stats["driver"] == "camoufox"
    assert stats["enabled"] is True
    assert stats["max_concurrent"] == 2
    assert [s["slot_id"] for s in stats["slots"]] == [0, 1]


def test_pool_timeout_raises_when_no_slot_frees_up(monkeypatch):
    monkeypatch.setattr(pool_module, "_SLOT_TIMEOUT_SECONDS", 0.01)
    config = {"registration": {"browser_process_pool": {"enabled": True, "max_concurrent": 1}}}
    pool = BrowserProcessPool(config, session_factory=RecordingFactory())
    with pool._semaphore:  # hold the only slot
        with pytest.raises(BrowserRegistrationError) as excinfo:
            with pool.session():
                pass
    assert "browser_pool_timeout" in str(excinfo.value)


# --------------------------------------------------------------------------
# wiring: registration entry point routes through the pool when enabled
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_shared_pool():
    from sms_tool.registration_drivers import playwright as pw

    pw._BROWSER_POOL = None
    pw._BROWSER_POOL_KEY = None
    yield
    pw._BROWSER_POOL = None
    pw._BROWSER_POOL_KEY = None


def test_scope_bypasses_pool_when_disabled():
    from sms_tool.registration_drivers import playwright as pw

    factory = RecordingFactory()
    with pw._browser_session_scope(
        driver_name="camoufox", config={}, proxy=None, headless=True,
        timeout_ms=1000, locale="en-US", timezone_id="UTC",
        browser_identity=None, viewport=None, session_factory=factory,
    ) as browser:
        assert isinstance(browser, FakeBrowser)
    assert pw._BROWSER_POOL is None
    assert len(factory.calls) == 1


def test_scope_uses_a_process_wide_pool_when_enabled():
    from sms_tool.registration_drivers import playwright as pw

    config = {"registration": {"browser_process_pool": {"enabled": True, "max_concurrent": 2}}}
    factory = RecordingFactory()
    for proxy in ("http://1.1.1.1:1", "http://2.2.2.2:2"):
        with pw._browser_session_scope(
            driver_name="camoufox", config=config, proxy=proxy, headless=True,
            timeout_ms=1000, locale="en-US", timezone_id="UTC",
            browser_identity={"profile_id": proxy}, viewport=None,
            session_factory=factory,
        ) as browser:
            assert isinstance(browser, FakeBrowser)

    # One pool reused across accounts; per-account proxy still varies.
    assert pw._BROWSER_POOL is not None
    assert [call["proxy"] for call in factory.calls] == ["http://1.1.1.1:1", "http://2.2.2.2:2"]


def test_scope_rebuilds_pool_when_driver_changes():
    from sms_tool.registration_drivers import playwright as pw

    config = {"registration": {"browser_process_pool": {"enabled": True}}}
    factory = RecordingFactory()
    for driver in ("camoufox", "playwright"):
        with pw._browser_session_scope(
            driver_name=driver, config=config, proxy=None, headless=True,
            timeout_ms=1000, locale="en-US", timezone_id="UTC",
            browser_identity=None, viewport=None, session_factory=factory,
        ):
            pass
    assert pw._BROWSER_POOL.driver == "playwright"
