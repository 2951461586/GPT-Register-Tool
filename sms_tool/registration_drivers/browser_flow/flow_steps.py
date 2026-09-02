"""流程步骤：浏览器会话作用域（进程池）、OTP 轮询、邮箱 OTP 流程重启。依赖 dom_fields/page_state/form_steps。"""

from __future__ import annotations

import threading
import time

from .dom_fields import _browser_heartbeat
from .form_steps import _fill_email, _fill_password_if_present, _maybe_accept_cookies
from .page_state import _manual_challenge, _wait_for_challenge_clear, _wait_for_registration_state

from ...mailbox_service import MailboxService
from ..base import BrowserRegistrationError
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any


def _poll_browser_otp(
    mailbox_service: MailboxService,
    mailbox: Any,
    *,
    browser: Any,
    page: Any,
    driver_name: str,
    subject_keyword: str,
    timeout: int,
    issued_after_unix: int,
    proxy: str | None,
    excluded_otps: set[str],
) -> str | None:
    # Heartbeat-aware polling applies to every driver, not just the retired
    # cloud ones: a crashed or recycled page is caught between OTP windows
    # instead of silently burning the whole timeout.  ``driver_name`` stays in
    # the signature so callers and tests keep a stable seam.
    deadline = time.monotonic() + max(1, int(timeout or 1))
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        page = _browser_heartbeat(browser, page)
        try:
            otp = mailbox_service.poll_otp(
                mailbox,
                subject_keyword=subject_keyword,
                timeout=min(20, remaining),
                issued_after_unix=issued_after_unix,
                proxy=proxy,
                excluded_otps=excluded_otps,
            )
        except Exception:
            otp = None
        if otp:
            return otp
        page = _browser_heartbeat(browser, page)
    return None


def _restart_email_otp_flow(
    browser: Any,
    page: Any,
    *,
    start_url: str,
    email: str,
    password: str,
    timeout_seconds: int,
    config: Mapping[str, Any] | None = None,
) -> tuple[Any, str]:
    """Rebuild the email step when a remote OTP target enters an error page."""
    select_page = getattr(browser, "select_live_page", None)
    if callable(select_page):
        page = select_page() or page
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=max(5_000, int(timeout_seconds) * 1_000))
    except Exception:
        if callable(select_page):
            page = select_page() or page
            page.goto(start_url, wait_until="domcontentloaded", timeout=max(5_000, int(timeout_seconds) * 1_000))
        else:
            raise
    _maybe_accept_cookies(page)
    if _manual_challenge(page):
        if not _wait_for_challenge_clear(page, max_wait_seconds=30):
            raise BrowserRegistrationError("manual_challenge_required")
    _fill_email(page, email, config=config)
    state = _wait_for_registration_state(page, min(timeout_seconds, 30), browser=browser, config=config)
    if state in {"challenge", "identity_provider"}:
        if state == "challenge" and _wait_for_challenge_clear(page, max_wait_seconds=30):
            state = _wait_for_registration_state(page, min(timeout_seconds, 30), browser=browser, config=config)
        if state in {"challenge", "identity_provider"}:
            raise BrowserRegistrationError("manual_challenge_required" if state == "challenge" else "browser_unexpected_identity_provider")
    if state == "login_password":
        raise BrowserRegistrationError("browser_existing_account")
    if state == "password":
        _fill_password_if_present(page, password, config=config)
        state = _wait_for_registration_state(page, min(timeout_seconds, 30), browser=browser, config=config)
    if state == "challenge":
        if not _wait_for_challenge_clear(page, max_wait_seconds=30):
            raise BrowserRegistrationError("manual_challenge_required")
    if state == "identity_provider":
        raise BrowserRegistrationError("browser_unexpected_identity_provider")
    if state == "login_password":
        raise BrowserRegistrationError("browser_existing_account")
    if state not in {"otp", "authenticated"}:
        raise BrowserRegistrationError("browser_otp_restart_state_unknown")
    return page, state


_BROWSER_POOL_LOCK = threading.Lock()


_BROWSER_POOL: Any = None


_BROWSER_POOL_KEY: tuple[Any, ...] | None = None


@contextmanager
def _browser_session_scope(
    *,
    driver_name: str,
    config: Mapping[str, Any],
    proxy: str | None,
    headless: bool,
    timeout_ms: int,
    locale: str,
    timezone_id: str,
    browser_identity: Mapping[str, Any] | None,
    viewport: tuple[int, int] | None,
    session_factory,
):
    """Yield a connected browser session, routed through the process pool when enabled.

    Two paths:

    * pool disabled (default) -- the session factory is called directly and
      lives exactly as long as the ``with`` block, which is the historical
      behaviour.
    * pool enabled (``registration.browser_process_pool.enabled``) -- slots
      come from a process-wide pool that bounds concurrency and recycles
      degraded browsers.  Per-account values (proxy, locale, timezone,
      identity, viewport) are still passed per session; only the expensive
      browser process is shared.
    """
    from ...browser_pool import BrowserProcessPool, PoolConfig

    if not PoolConfig.from_config(config).enabled:
        session = session_factory(
            driver_name, config=config, proxy=proxy, headless=headless,
            timeout_ms=timeout_ms, locale=locale, timezone_id=timezone_id,
            browser_identity=browser_identity, viewport=viewport,
        )
        with session as browser:
            yield browser
        return

    global _BROWSER_POOL, _BROWSER_POOL_KEY
    # The pool owns the browser processes, so it is keyed only by the knobs
    # that shape a process.  Everything per-account is supplied per session.
    key = (driver_name, headless, timeout_ms)
    with _BROWSER_POOL_LOCK:
        if _BROWSER_POOL is None or _BROWSER_POOL_KEY != key:
            _BROWSER_POOL = BrowserProcessPool(
                config,
                driver=driver_name,
                headless=headless,
                timeout_ms=timeout_ms,
                locale=locale,
                timezone_id=timezone_id,
                session_factory=session_factory,
            )
            _BROWSER_POOL_KEY = key
        pool = _BROWSER_POOL
    with pool.session(
        proxy=proxy,
        locale=locale,
        timezone_id=timezone_id,
        browser_identity=browser_identity,
        viewport=viewport,
    ) as (browser, _slot):
        yield browser
