"""页面状态判定与等待（验证码 / OTP / 资料补全 / 注册态轮询）。依赖 dom_fields。"""

from __future__ import annotations

import time

from .dom_fields import _body_text, _browser_heartbeat, _first_visible, _hard_proxy_block, _is_openai_auth_url, _otp_fields, _otp_page_state, _unexpected_identity_provider

from ...humanize import delay as humanize_delay
from ..base import BrowserRegistrationError
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def _manual_challenge(page) -> bool:
    text = _body_text(page)
    markers = (
        "verify you are human", "captcha", "security challenge", "unusual activity",
        "checking your browser", "just a moment", "performing security verification",
        "验证您是真人", "安全验证", "人机验证",
    )
    if any(marker in text for marker in markers):
        return True
    try:
        return page.locator(
            "iframe[src*='challenge'], iframe[src*='captcha'], iframe[src*='turnstile'], "
            "iframe[src*='challenges.cloudflare.com'], [data-testid*='captcha'], "
            "[class*='cf-chl'], [id*='turnstile']"
        ).count() > 0
    except Exception:
        return False


def _ensure_signup_page_ready(
    page, *, timeout_seconds: int = 45, config: Mapping[str, Any] | None = None
) -> None:
    """Wait for either the email form or a classified proxy/challenge result."""
    if not callable(getattr(page, "locator", None)):
        return
    deadline = time.monotonic() + max(5, int(timeout_seconds or 45))
    selector = (
        "input[type='email'], input[name='email'], input[name='username'], "
        "input#email-input, input[autocomplete='email']"
    )
    while time.monotonic() < deadline:
        if _hard_proxy_block(page):
            raise BrowserRegistrationError("browser_proxy_blocked")
        if _manual_challenge(page):
            if not _wait_for_challenge_clear(
                page,
                max_wait_seconds=min(30, max(1, int(deadline - time.monotonic()))),
            ):
                raise BrowserRegistrationError("manual_challenge_required")
            continue
        try:
            if page.locator(selector).first.is_visible():
                return
        except Exception:
            pass
        # P3: randomize the settle interval so every account in a batch does
        # not share one identical timing signature.
        _settle = humanize_delay("page_settle", config=config)
        try:
            page.wait_for_timeout(int(_settle * 1000))
        except Exception:
            time.sleep(_settle)
    if _hard_proxy_block(page):
        raise BrowserRegistrationError("browser_proxy_blocked")
    if _manual_challenge(page):
        raise BrowserRegistrationError("manual_challenge_required")
    raise BrowserRegistrationError("browser_email_field_missing")


def _wait_for_challenge_clear(page, max_wait_seconds: int = 30, *, poll_interval: float = 2.0) -> bool:
    """Poll for a Cloudflare / Turnstile challenge to clear automatically.

    Cloudflare's JS challenge typically resolves within 5–10 seconds.
    Instead of failing immediately, wait up to ``max_wait_seconds`` and
    return ``True`` when the challenge disappears.  Returns ``False`` if
    the challenge persists past the deadline.
    """
    deadline = time.monotonic() + max(1, int(max_wait_seconds))
    while time.monotonic() < deadline:
        if not _manual_challenge(page):
            return True
        try:
            page.wait_for_timeout(int(poll_interval * 1000))
        except Exception:
            break
    return not _manual_challenge(page)


def _quick_auth_state(page) -> str:
    """Probe the current auth state in one renderer round trip."""
    try:
        state = page.evaluate(r"""() => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
          const inputs = [...document.querySelectorAll('input')].filter(visible);
          const attrs = el => [el.type, el.name, el.id, el.autocomplete, el.inputMode,
            el.getAttribute('aria-label'), el.getAttribute('placeholder')].filter(Boolean).join(' ').toLowerCase();
          const numeric = inputs.filter(el => /numeric|tel|number/.test(attrs(el)));
          const otp = inputs.some(el => {
            const value = attrs(el);
            return value.includes('one-time-code') || /(^|\s)(otp|code|verification_code|email_otp)(\s|$)/.test(value)
              || (/numeric|tel/.test(value) && /otp|code|verification/.test(value));
          }) || (numeric.length >= 4 && numeric.length <= 8);
          const password = inputs.find(el => String(el.type || '').toLowerCase() === 'password' || attrs(el).includes('password'));
          const profile = inputs.some(el => /(^|\s)(name|fullname|full_name|firstname|lastname|age|birth|birthday|birthdate|year|month|day)(\s|$)/.test(attrs(el)))
            || !!document.querySelector('[role=spinbutton][data-type],.react-aria-Select,[data-testid="hidden-select-container"] select');
          const body = String(document.body?.innerText || '').toLowerCase().slice(0, 3000);
          const challenge = /verify you are human|captcha|security challenge|checking your browser|just a moment|安全验证|人机验证/.test(body)
            || !!document.querySelector('iframe[src*="challenge"],iframe[src*="captcha"],iframe[src*="turnstile"],iframe[src*="challenges.cloudflare.com"],[class*="cf-chl"],[id*="turnstile"]');
          return {
            url: location.href, challenge, otp, profile,
            password: !!password,
            passwordAutocomplete: password?.autocomplete || '',
            email: inputs.some(el => String(el.type || '').toLowerCase() === 'email' || attrs(el).includes('autocomplete email'))
          };
        }""")
    except Exception:
        return "unknown"
    if not isinstance(state, Mapping):
        return "unknown"
    url = str(state.get("url") or "")
    path = str(urlsplit(url).path or "").lower()
    if state.get("challenge"):
        return "challenge"
    if state.get("password") and (
        "/log-in/password" in path or "/login/password" in path
        or str(state.get("passwordAutocomplete") or "").lower() == "current-password"
    ):
        return "login_password"
    if state.get("otp"):
        return "otp"
    if state.get("password"):
        return "password"
    if state.get("profile") and any(item in path for item in ("about-you", "profile", "create-account")):
        return "profile"
    if _is_openai_auth_url(url):
        host = str(urlsplit(url).hostname or "").lower()
        if (host == "chatgpt.com" or host.endswith(".chatgpt.com")) and "/auth/" not in path:
            return "authenticated"
    if state.get("email"):
        return "email"
    return "unknown"


def _wait_for_registration_state(
    page,
    timeout_seconds: int = 30,
    *,
    browser: Any = None,
    wait_for_otp_transition: bool = False,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Wait for a recognized registration state.

    An accepted OTP can leave its inputs mounted while the auth SPA routes to
    ``about-you`` or ChatGPT.  At that point callers need the destination
    state, not the stale OTP state, before deciding whether profile data may
    be submitted.
    """
    deadline = time.monotonic() + max(1, int(timeout_seconds or 30))
    while time.monotonic() < deadline:
        if browser is not None:
            try:
                page = _browser_heartbeat(browser, page)
            except BrowserRegistrationError:
                raise
            except Exception:
                pass
        if wait_for_otp_transition:
            # OTP controls can remain mounted for a short period after the
            # SPA has already routed. Prefer the destination URL/controls
            # while waiting instead of treating those stale inputs as state.
            try:
                if _manual_challenge(page):
                    return "challenge"
                current_url = str(getattr(page, "url", "") or "")
                parsed = urlsplit(current_url)
                current_host = str(parsed.hostname or "").lower()
                current_path = str(parsed.path or "").lower()
                if _unexpected_identity_provider(current_url):
                    return "identity_provider"
                if (
                    (current_host == "chatgpt.com" or current_host.endswith(".chatgpt.com"))
                    and "/auth/" not in current_path
                ):
                    return "authenticated"
                if any(marker in current_path for marker in ("about-you", "profile", "create-account")):
                    profile_field = _first_visible(
                        page,
                        (
                            "input[name='name']", "input[autocomplete='name']",
                            "input[name*='birth' i]", "input[type='date']",
                            "input[name='age']", "input[type='number']",
                            "[role='spinbutton'][data-type]",
                            "[data-testid='hidden-select-container'] select",
                        ),
                        timeout_ms=250,
                    )
                    if profile_field is not None:
                        return "profile"
            except Exception:
                pass
        quick = _quick_auth_state(page)
        if quick in {"challenge", "login_password", "password", "profile", "authenticated"}:
            return quick
        if quick == "otp" and not wait_for_otp_transition:
            return quick
        if _manual_challenge(page):
            return "challenge"
        try:
            if _unexpected_identity_provider(str(page.url or "")):
                return "identity_provider"
        except Exception:
            pass
        if _first_visible(page, ("input[type='password']", "input[name='password']")) is not None:
            return "password"
        if _otp_fields(page) is not None and not wait_for_otp_transition:
            return "otp"
        if _first_visible(
            page,
            (
                "input[name='name']", "input[autocomplete='name']",
                "input[name*='birth' i]", "input[type='date']",
                "input[name='age']", "input[type='number']",
                "[role='spinbutton'][data-type]",
                "[data-testid='hidden-select-container'] select",
            ),
        ) is not None:
            return "profile"
        try:
            if "chatgpt.com" in str(page.url or "").lower() and "/auth/" not in str(page.url or "").lower():
                return "authenticated"
        except Exception:
            pass
        _pause = humanize_delay("state_probe", config=config)
        try:
            page.wait_for_timeout(int(_pause * 1000))
        except Exception:
            time.sleep(_pause)
    return "unknown"


def _profile_completion_required(state: str) -> bool:
    """Classify the post-OTP state before touching profile controls."""
    if state == "profile":
        return True
    if state == "authenticated":
        return False
    if state == "challenge":
        raise BrowserRegistrationError("manual_challenge_required")
    if state == "identity_provider":
        raise BrowserRegistrationError("browser_unexpected_identity_provider")
    if state == "login_password":
        raise BrowserRegistrationError("browser_existing_account")
    raise BrowserRegistrationError("browser_registration_state_unknown")


def _post_otp_registration_state(
    page: Any,
    *,
    browser: Any = None,
    timeout_seconds: int = 30,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Re-probe the destination after OTP before deciding on profile work."""
    probe_timeout = min(30, max(1, int(timeout_seconds or 1)))
    state = _wait_for_registration_state(
        page,
        probe_timeout,
        browser=browser,
        wait_for_otp_transition=True,
        config=config,
    )
    if state != "otp":
        return state

    # A patched or legacy waiter may still report the old OTP state.  Inspect
    # the adopted page once more so a completed callback is not mistaken for
    # a missing profile form (especially with the Roxy Selenium page adapter).
    page = getattr(browser, "page", None) or page
    try:
        if _manual_challenge(page):
            return "challenge"
        current_url = str(getattr(page, "url", "") or "")
        if _unexpected_identity_provider(current_url):
            return "identity_provider"
        parsed = urlsplit(current_url)
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").lower()
        if (host == "chatgpt.com" or host.endswith(".chatgpt.com")) and "/auth/" not in path:
            return "authenticated"
        if any(marker in path for marker in ("about-you", "profile", "create-account")):
            quick = _quick_auth_state(page)
            if quick == "profile":
                return quick
    except Exception:
        pass
    return "unknown"


def _wait_after_otp_submit(page, timeout_seconds: int = 30) -> str:
    """Return accepted unless the OTP page reports an explicit validation error."""
    deadline = time.monotonic() + max(1, int(timeout_seconds or 30))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if _otp_fields(page) is None:
            return "accepted"
        last = _otp_page_state(page)
        if any(str(item.get("aria_invalid") or "").lower() == "true" for item in last.get("inputs", [])):
            return "invalid"
        if last.get("errors"):
            return "invalid"
        page.wait_for_timeout(500)
    if _otp_fields(page) is None:
        return "accepted"
    if any(str(item.get("aria_invalid") or "").lower() == "true" for item in last.get("inputs", [])) or last.get("errors"):
        return "invalid"
    return "accepted"


def _wait_for_profile_completion(
    page: Any, timeout_seconds: int = 30, config: Mapping[str, Any] | None = None
) -> bool:
    """Confirm that the profile form has routed away before fetching a session."""
    if not callable(getattr(page, "evaluate", None)):
        return True
    deadline = time.monotonic() + max(1, int(timeout_seconds or 1))
    while time.monotonic() < deadline:
        state = _quick_auth_state(page)
        if state in {"authenticated", "otp", "email"}:
            return True
        if state == "challenge":
            raise BrowserRegistrationError("manual_challenge_required")
        _settle = humanize_delay("page_settle", config=config)
        try:
            page.wait_for_timeout(int(_settle * 1000))
        except Exception:
            time.sleep(_settle)
    return _quick_auth_state(page) in {"authenticated", "otp", "email"}
