"""页面状态判定与等待（验证码 / OTP / 资料补全 / 注册态轮询）。依赖 dom_fields。"""

from __future__ import annotations

import time

from . import dom_fields

from ...humanize import delay as humanize_delay
from ..base import BrowserRegistrationError
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def _raise_if_registration_cancelled() -> None:
    from ...registration_cancel import registration_cancel_requested

    if registration_cancel_requested():
        raise BrowserRegistrationError("registration_cancelled")


def _manual_challenge(page) -> bool:
    text = dom_fields._body_text(page)
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
    selector = dom_fields.EDITABLE_EMAIL_SELECTOR
    while time.monotonic() < deadline:
        _raise_if_registration_cancelled()
        if dom_fields._hard_proxy_block(page):
            raise BrowserRegistrationError("browser_proxy_blocked")
        if _manual_challenge(page):
            if not _wait_for_challenge_clear(
                page,
                max_wait_seconds=min(30, max(1, int(deadline - time.monotonic()))),
            ):
                raise BrowserRegistrationError("manual_challenge_required")
            continue
        try:
            field = page.locator(selector).first
            if field.is_visible() and field.is_editable():
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
    if dom_fields._hard_proxy_block(page):
        raise BrowserRegistrationError("browser_proxy_blocked")
    if _manual_challenge(page):
        raise BrowserRegistrationError("manual_challenge_required")
    try:
        if page.locator(", ".join(dom_fields.EMAIL_SELECTORS)).first.is_visible():
            raise BrowserRegistrationError("browser_email_field_not_editable")
    except BrowserRegistrationError:
        raise
    except Exception:
        pass
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
        _raise_if_registration_cancelled()
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
    if dom_fields._is_openai_auth_url(url):
        host = str(urlsplit(url).hostname or "").lower()
        if (host == "chatgpt.com" or host.endswith(".chatgpt.com")) and "/auth/" not in path:
            return "authenticated"
    if state.get("email"):
        return "email"
    return "unknown"


def _email_verification_route(page: Any) -> bool:
    """Return whether the adopted page is still on OpenAI's verification route."""
    try:
        parsed = urlsplit(str(getattr(page, "url", "") or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").lower().rstrip("/")
        return host == "auth.openai.com" and path.endswith("/email-verification")
    except Exception:
        return False


def _advance_email_verification(page) -> bool:
    """Click one explicit verification continuation control, if present.

    The verification SPA occasionally keeps the OTP route mounted after a
    successful code submission. Only well-known continuation labels are
    eligible; social-login and generic submit controls are deliberately
    excluded.
    """
    return dom_fields._click_first_visible(
        page,
        (
            "button:has-text('Continue')",
            "button:has-text('Continue to ChatGPT')",
            "button:has-text('Verify')",
            "button:has-text('Done')",
            "button:has-text('继续')",
            "button:has-text('完成')",
        ),
        timeout_ms=500,
    )


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
        _raise_if_registration_cancelled()
        if browser is not None:
            try:
                page = dom_fields._browser_heartbeat(browser, page)
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
                if dom_fields._unexpected_identity_provider(current_url):
                    return "identity_provider"
                if _email_verification_route(page):
                    # Keep the initial OTP state separate from the post-submit
                    # route. This lets the caller perform one explicit
                    # continuation/reprobe instead of collapsing into unknown.
                    return "email_verification"
                if (
                    (current_host == "chatgpt.com" or current_host.endswith(".chatgpt.com"))
                    and "/auth/" not in current_path
                ):
                    return "authenticated"
                if any(marker in current_path for marker in ("about-you", "profile", "create-account")):
                    profile_field = dom_fields._first_visible(
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
            if dom_fields._unexpected_identity_provider(str(page.url or "")):
                return "identity_provider"
        except Exception:
            pass
        if dom_fields._first_visible(page, ("input[type='password']", "input[name='password']")) is not None:
            return "password"
        if dom_fields._otp_fields(page) is not None and not wait_for_otp_transition:
            return "otp"
        if dom_fields._first_visible(
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
            parsed = urlsplit(str(page.url or ""))
            host = (parsed.hostname or "").lower()
            if (host == "chatgpt.com" or host.endswith(".chatgpt.com")) and "/auth/" not in parsed.path.lower():
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
    if state == "email_verification":
        raise BrowserRegistrationError("browser_email_verification_stuck")
    if state == "email_verification_stuck":
        raise BrowserRegistrationError("browser_email_verification_stuck")
    raise BrowserRegistrationError("browser_registration_state_unknown")


def _post_otp_registration_state(
    page: Any,
    *,
    browser: Any = None,
    timeout_seconds: int = 30,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Re-probe the destination after OTP before deciding on profile work."""
    # OTP callbacks can take longer than the initial 30s SPA transition. The
    # caller supplies the stage budget; cap only the upper bound to prevent a
    # malformed config from blocking a worker indefinitely.
    probe_timeout = max(1, min(int(timeout_seconds or 1), 120))
    state = _wait_for_registration_state(
        page,
        probe_timeout,
        browser=browser,
        wait_for_otp_transition=True,
        config=config,
    )
    if state not in {"otp", "email_verification"}:
        return state

    # A patched or legacy waiter may still report the old OTP state.  Inspect
    # the adopted page once more so a completed callback is not mistaken for
    # a missing profile form (especially with the Roxy Selenium page adapter).
    page = getattr(browser, "page", None) or page
    try:
        if _manual_challenge(page):
            return "challenge"
        current_url = str(getattr(page, "url", "") or "")
        if dom_fields._unexpected_identity_provider(current_url):
            return "identity_provider"
        if _email_verification_route(page):
            if _advance_email_verification(page):
                # Live batches show the post-OTP SPA can sit on its
                # email-verification route well past 20s before routing on;
                # a 20s budget misclassified slow-but-healthy signups as
                # stuck. Allow up to 45s (still bounded by the stage budget).
                deadline = time.monotonic() + min(45, probe_timeout)
                while time.monotonic() < deadline:
                    state = _wait_for_registration_state(
                        page,
                        min(5, max(1, int(deadline - time.monotonic()))),
                        browser=browser,
                        wait_for_otp_transition=True,
                        config=config,
                    )
                    if state != "email_verification":
                        return state
                return "email_verification_stuck"
            return "email_verification_stuck"
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


def _wait_after_otp_submit(
    page, timeout_seconds: int = 30, *, require_transition: bool = False
) -> str:
    """Wait for OTP acceptance, optionally requiring destination evidence."""
    deadline = time.monotonic() + max(1, int(timeout_seconds or 30))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _raise_if_registration_cancelled()
        if dom_fields._otp_fields(page) is None:
            return "accepted"
        if require_transition:
            state = _quick_auth_state(page)
            if state in {"authenticated", "profile"}:
                return "accepted"
        last = dom_fields._otp_page_state(page)
        if any(str(item.get("aria_invalid") or "").lower() == "true" for item in last.get("inputs", [])):
            return "invalid"
        if last.get("errors"):
            return "invalid"
        page.wait_for_timeout(500)
    if dom_fields._otp_fields(page) is None:
        return "accepted"
    if any(str(item.get("aria_invalid") or "").lower() == "true" for item in last.get("inputs", [])) or last.get("errors"):
        return "invalid"
    return "pending" if require_transition else "accepted"


def _wait_for_profile_completion(
    page: Any, timeout_seconds: int = 30, config: Mapping[str, Any] | None = None
) -> bool:
    """Confirm that the profile form has routed away before fetching a session."""
    if not callable(getattr(page, "evaluate", None)):
        return True
    deadline = time.monotonic() + max(1, int(timeout_seconds or 1))
    while time.monotonic() < deadline:
        _raise_if_registration_cancelled()
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
