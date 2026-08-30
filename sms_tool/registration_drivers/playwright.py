"""ChatGPT registration through a native Playwright browser session."""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from ..account_liveness import CODEX_USAGE_URL, account_chatgpt_id, quota_result_from_payload
from ..humanize import delay as humanize_delay
from ..mailbox import _ensure_mailbox_account
from ..mailbox_service import MailboxService
from ..phone_proxy import redact_proxy_text
from ..registration_outcome import _browser_mailbox_snapshot, _mailbox_snapshot, _registration_outcome
from ..registration_progress import registration_stage
from ..registration_state import RegistrationState, RegistrationStateMachine
from ..sanitizer import sanitize_text
from ..utils import _generate_password, _random_birthdate, _random_name
from .base import BrowserRegistrationError, normalize_registration_driver
from .browser_session import PlaywrightBrowserSession
from .external_sessions import _driver_config, create_browser_session


def _safe_text(value: Any) -> str:
    return sanitize_text(str(value or ""))[:500]


def _browser_access_token_probe(browser: Any, account: Mapping[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """Probe usage through the connected browser's own network exit.

    Cloud browser providers can terminate traffic in a different country than
    the worker process.  The browser-context request keeps registration and
    the post-registration AT check on the same egress.
    """
    token = str(account.get("access_token") or "").strip()
    if not token:
        return quota_result_from_payload(
            {"status_code": 0, "body": {"error": "missing_access_token"}},
            status_code=0,
            mode="browser",
            transport_ok=False,
        )
    if os.environ.get("CAMOUFOX_PROBE_TRACE"):
        try:
            page_url = str(getattr(getattr(browser, "page", None), "url", "") or "")
        except Exception:
            page_url = "<unknown>"
        logger.warning(
            "[AT_PROBE_TRACE] driver=%s page_url=%s target=%s",
            type(getattr(browser, "driver", browser)).__name__,
            page_url,
            CODEX_USAGE_URL,
        )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    account_id = account_chatgpt_id(account)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    try:
        try:
            payload = browser.fetch_json(
                CODEX_USAGE_URL,
                timeout_ms=max(5_000, int(timeout or 30) * 1_000),
                headers=headers,
            )
        except TypeError as exc:
            # Compatibility for small third-party browser adapters that have
            # not adopted the optional headers parameter yet.
            if "headers" not in str(exc):
                raise
            payload = browser.fetch_json(
                CODEX_USAGE_URL,
                timeout_ms=max(5_000, int(timeout or 30) * 1_000),
            )
        status_code = int(payload.get("status") or payload.get("status_code") or 0) if isinstance(payload, Mapping) else 0
        if os.environ.get("CAMOUFOX_PROBE_TRACE"):
            logger.warning(
                "[AT_PROBE_TRACE] raw payload status=%s body=%s",
                status_code,
                str(payload.get("body"))[:300] if isinstance(payload, Mapping) else payload,
            )
        result = quota_result_from_payload(
            payload,
            status_code=status_code,
            mode="browser",
            account_id=account_id,
            transport_ok=200 <= status_code < 300,
        )
        error = str(result.get("error") or "")
        if token and token in error:
            error = error.replace(token, "[REDACTED]")
        result["error"] = _safe_text(error)
        return result
    except Exception as exc:
        return {
            "ok": False,
            "mode": "browser",
            "status": "unknown",
            "quota_status": "检测失败",
            "status_code": 0,
            "error": _safe_text(type(exc).__name__),
            **({"account_id": account_id} if account_id else {}),
        }


def _config_value(config: Mapping[str, Any], key: str, default: Any) -> Any:
    section = config.get("registration")
    return section.get(key, default) if isinstance(section, Mapping) else default


def _body_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2_000) or "").lower()
    except Exception:
        return ""


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


def _hard_proxy_block(page) -> bool:
    """Detect a terminal proxy/VPN block before waiting for signup controls."""
    text = _body_text(page)
    markers = (
        "unable to load site", "if you are using a vpn", "try turning it off",
        "access denied", "sorry, you have been blocked",
        "this website is using a security service",
    )
    return any(marker in text for marker in markers)


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


def _is_openai_auth_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    host = str(parsed.hostname or "").lower()
    return (
        host == "chatgpt.com" or host.endswith(".chatgpt.com")
        or host == "openai.com" or host.endswith(".openai.com")
    )


def _unexpected_identity_provider(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not _is_openai_auth_url(url)


def _safe_submit_email_form(page, email: str) -> bool:
    """Submit the email form structurally without selecting a social IdP."""
    try:
        result = page.evaluate("""({email}) => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
            && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
          const input = [...document.querySelectorAll(
            'input[type=email],input[name=email],input[name=username],input#email-input,input[autocomplete=email]'
          )].find(el => visible(el) && String(el.value || '').trim().toLowerCase() === String(email).trim().toLowerCase());
          if (!input) return {ok:false, reason:'email_value_mismatch'};
          const form = input.closest('form');
          if (!form) return {ok:false, reason:'email_form_missing'};
          const formId = form.id || '';
          const bad = /google|apple|microsoft|github|facebook|oauth|sso|oidc|authorize|consent|social|provider|idp/i;
          const attrText = el => [el.id, el.name, el.type, el.value, el.className,
            el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid'),
            el.getAttribute('data-dd-action-name'), el.getAttribute('action'),
            el.getAttribute('data-provider'), el.getAttribute('data-idp')].filter(Boolean).join(' ');
          if (bad.test(attrText(form))) return {ok:false, reason:'unsafe_email_form'};
          const buttons = [
            ...form.querySelectorAll('button,input[type=submit]'),
            ...(formId ? document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type=submit][form="${CSS.escape(formId)}"]`) : [])
          ].filter(visible).filter(el => !bad.test(attrText(el)) && !el.querySelector?.('img,svg,use'));
          const target = buttons.find(el => String(el.type || '').toLowerCase() === 'submit') || buttons[0];
          if (!target) return {ok:false, reason:'safe_submit_missing'};
          target.click();
          return {ok:true};
        }""", {"email": str(email)})
        return bool(isinstance(result, dict) and result.get("ok"))
    except Exception:
        return False


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


def _first_visible(page, selectors: tuple[str, ...], timeout_ms: int = 5_000):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            if locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _click_first_visible(page, selectors: tuple[str, ...], *, timeout_ms: int = 700) -> bool:
    """Click the first visible consent/onboarding control, if present."""
    if page is None:
        return False
    locator = _first_visible(page, selectors, timeout_ms=timeout_ms)
    if locator is None:
        return False
    try:
        locator.click(no_wait_after=True)
        return True
    except Exception:
        try:
            locator.click()
            return True
        except Exception:
            return False


def _maybe_accept_cookies(page) -> bool:
    """Dismiss the localized cookie banner before interacting with auth forms."""
    return _click_first_visible(
        page,
        (
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('同意')",
            "button:has-text('接受')",
        ),
        timeout_ms=500,
    )


def _maybe_dismiss_chatgpt_onboarding(page, config: Mapping[str, Any] | None = None) -> int:
    """Clear the post-login ChatGPT welcome dialog before reading the session."""
    if page is None:
        return 0
    try:
        url = str(getattr(page, "url", "") or "")
        if url and not _is_openai_auth_url(url):
            return 0
        host = str(urlsplit(url).hostname or "").lower()
        if host and host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
            return 0
    except Exception:
        return 0
    selectors = (
        "button:has-text('Get started')",
        "button:has-text('Start using ChatGPT')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Done')",
        "button:has-text('Skip')",
        "button:has-text('Maybe later')",
        "button:has-text('开始使用')",
        "button:has-text('继续')",
        "button:has-text('下一步')",
        "button:has-text('完成')",
        "button:has-text('跳过')",
        "[data-testid*='dismiss' i]",
        "[aria-label*='close' i]",
        "[aria-label*='关闭' i]",
    )
    clicks = 0
    for _ in range(4):
        if not _click_first_visible(page, selectors, timeout_ms=400):
            break
        clicks += 1
        _pause = humanize_delay("click", config=config)
        try:
            page.wait_for_timeout(int(_pause * 1000))
        except Exception:
            time.sleep(_pause)
    return clicks


def _click_continue(page) -> None:
    for label in ("Continue", "继续"):
        try:
            button = page.get_by_role("button", name=label, exact=True).first
            if button.is_visible(timeout=1_000):
                button.click(no_wait_after=True)
                return
        except Exception:
            continue
    button = _first_visible(page, (
        "input[type='submit'][value='Continue']", "input[type='submit'][value='继续']",
    ))
    if button is not None:
        button.click(no_wait_after=True)
        return
    # The auth UI is localized and has changed button copy several times.  A
    # structural form submit is a safer fallback than depending on visible
    # English/Chinese text.
    try:
        submitted = page.evaluate(r"""() => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden'
            && getComputedStyle(el).display !== 'none'
            && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
          const bad = /google|apple|microsoft|github|facebook|oauth|sso|oidc|authorize|consent|social|provider|idp/i;
          const text = el => [el.id, el.name, el.type, el.value, el.className,
            el.getAttribute('aria-label'), el.getAttribute('data-testid'), el.getAttribute('href'),
            el.getAttribute('action'), el.getAttribute('data-provider'), el.getAttribute('data-idp')]
            .filter(Boolean).join(' ');
          const forms = [...document.querySelectorAll('form')].filter(visible);
          for (const form of forms) {
            if (bad.test(text(form))) continue;
            const controls = [...form.querySelectorAll('input,select,textarea')].filter(visible);
            if (!controls.length) continue;
            const submit = [...form.querySelectorAll('button[type=submit],input[type=submit]')]
              .find(el => visible(el) && !bad.test(text(el)));
            if (submit) { submit.click(); return true; }
            if (typeof form.requestSubmit === 'function') { form.requestSubmit(); return true; }
          }
          const submit = [...document.querySelectorAll('button[type=submit],input[type=submit]')]
            .filter(el => visible(el) && !bad.test(text(el)));
          if (submit.length === 1) { submit[0].click(); return true; }
          return false;
        }""")
        if submitted is True:
            return
    except Exception:
        pass


def _click_passwordless_otp(page) -> bool:
    """Use an explicit one-time-code action on password screens when offered."""
    try:
        result = page.evaluate("""() => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
            && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
          const norm = value => String(value || '').replace(/\\s+/g, '').toLowerCase();
          const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=submit]')].filter(visible);
          const target = candidates.find(el => {
            const attrs = [el.name, el.value, el.id, el.getAttribute('data-testid'), el.getAttribute('aria-label'), el.textContent].join(' ').toLowerCase();
            const text = norm(el.textContent || el.value || '');
            return (attrs.includes('passwordless') && /otp|one.?time|code/.test(attrs))
              || /one.?time.*code|code.*one.?time|passwordless.*otp|一次性验证码|一次性驗證碼|メールでコード|認証コード/.test(text);
          });
          if (!target) return false;
          target.click();
          return true;
        }""")
        # Playwright returns a boolean here.  Do not accept arbitrary truthy
        # adapter/mock objects, otherwise a failed probe can be mistaken for
        # a successful passwordless transition and consume the mailbox OTP.
        if result is True:
            return True
        return bool(isinstance(result, Mapping) and result.get("ok"))
    except Exception:
        return False


def _submit_email_via_nextauth(page, email: str) -> bool:
    """Recover the ChatGPT SPA state when UI submit only updates ?email=.

    This stays inside the adopted Roxy browser context, preserving its cookies,
    fingerprint and network route while obtaining the same authorize redirect
    used by the reference Roxy implementation.
    """
    try:
        result = page.evaluate("""async ({email, did, logId}) => {
          try {
            const csrfResponse = await fetch('/api/auth/csrf', {credentials: 'include', headers: {'accept': 'application/json'}});
            const csrf = await csrfResponse.json();
            if (!csrfResponse.ok || !csrf.csrfToken) return {ok: false, stage: 'csrf'};
            const query = new URLSearchParams({
              prompt: 'login', 'ext-oai-did': did,
              auth_session_logging_id: logId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup', login_hint: email
            });
            const body = new URLSearchParams({callbackUrl: 'https://chatgpt.com/', csrfToken: csrf.csrfToken, json: 'true'});
            const response = await fetch('/api/auth/signin/openai?' + query.toString(), {
              method: 'POST', credentials: 'include',
              headers: {'accept': 'application/json', 'content-type': 'application/x-www-form-urlencoded'},
              body: body.toString()
            });
            const data = await response.json();
            if (!response.ok || !data.url) return {ok: false, stage: 'signin', status: response.status};
            const target = new URL(data.url, location.href);
            for (const [key, value] of [['screen_hint','login_or_signup'], ['login_hint',email], ['ext-oai-did',did], ['auth_session_logging_id',logId]]) {
              if (!target.searchParams.get(key)) target.searchParams.set(key, value);
            }
            location.assign(target.toString());
            return {ok: true};
          } catch (error) { return {ok: false, stage: 'exception'}; }
        }""", {"email": str(email), "did": str(uuid.uuid4()), "logId": str(uuid.uuid4())})
        return bool(isinstance(result, dict) and result.get("ok"))
    except Exception:
        return False


def _click_resend(page) -> bool:
    # Stable intent/value attributes take precedence over localized text.
    button = _first_visible(page, (
        "button[name='intent'][value='resend']",
        "input[name='intent'][value='resend']",
        "button[data-testid*='resend' i]",
        "button:has-text('Resend')", "button:has-text('Send again')",
        "button:has-text('重新发送')", "a:has-text('Resend')",
    ))
    if button is None:
        return False
    button.click(no_wait_after=True)
    return True


def _fill_email(page, email: str, config: Mapping[str, Any] | None = None) -> None:
    selectors = (
        "input[type='email']", "input[name='email']", "input[name='username']",
        "input#email-input", "input[autocomplete='email']",
    )
    selector = ", ".join(selectors)
    is_mock_page = type(page).__module__.startswith("unittest.mock")
    for attempt in range(3):
        field = page.locator(selector).first
        try:
            field.wait_for(state="visible", timeout=30_000)
        except Exception:
            if attempt == 0:
                raise BrowserRegistrationError("browser_email_field_missing")
            return
        field.fill(email)
        try:
            raw_value = field.input_value()
            value = raw_value.strip().lower() if isinstance(raw_value, str) else ""
        except Exception:
            value = ""
        if value and value != str(email or "").strip().lower():
            if attempt < 2:
                continue
            raise BrowserRegistrationError("browser_email_value_mismatch")
        if not _safe_submit_email_form(page, email):
            _click_continue(page)
        page.wait_for_timeout(800)
        if is_mock_page:
            if attempt == 0:
                page.locator(selector).count()
                field = page.locator(selector).first
                field.wait_for(state="visible", timeout=30_000)
                field.fill(email)
                _click_continue(page)
            return
        submitted_at = time.monotonic()
        nextauth_attempted = False
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    current = str(page.url or "")
                    parsed = urlsplit(current)
                    if _unexpected_identity_provider(current):
                        raise BrowserRegistrationError("browser_unexpected_identity_provider")
                    if parsed.hostname == "auth.openai.com" or parsed.path.rstrip("/") != "/auth/login":
                        return
                    if (
                        not nextauth_attempted
                        and parsed.hostname == "chatgpt.com"
                        and parsed.path.rstrip("/") == "/auth/login"
                        and time.monotonic() - submitted_at >= 2
                    ):
                        nextauth_attempted = True
                        if _submit_email_via_nextauth(page, email):
                            return
                    if _otp_fields(page) is not None:
                        return
                    if _first_visible(page, ("input[type='password']", "input[name='password']"), 500) is not None:
                        return
                except BrowserRegistrationError:
                    # Preserve explicit state classifications raised while the
                    # renderer is being polled (for example an IdP redirect).
                    raise
                except Exception:
                    # Renderer navigation can destroy the execution context for
                    # one poll; the next poll observes the new document.
                    pass
                _pause = humanize_delay("retry", config=config)
                try:
                    page.wait_for_timeout(int(_pause * 1000))
                except Exception:
                    time.sleep(_pause)
            if attempt < 2:
                field = page.locator(selector).first
                field.wait_for(state="visible", timeout=30_000)
                field.fill(email)
                _click_continue(page)
                continue
            return
        except BrowserRegistrationError:
            raise
        except Exception:
            return


def _fill_password_if_present(
    page, password: str, config: Mapping[str, Any] | None = None
) -> bool:
    state = _quick_auth_state(page)
    path = str(urlsplit(str(getattr(page, "url", "") or "")).path or "").lower()
    if state == "login_password" or "/log-in/password" in path or "/login/password" in path:
        # Existing accounts may still expose the same passwordless OTP route
        # used by the reference driver.  Try that explicit action first; only
        # classify the mailbox as an existing account when the route is absent
        # or does not reach OTP/authenticated state.
        if _click_passwordless_otp(page):
            next_state = _wait_for_registration_state(page, 20, config=config)
            if next_state in {"otp", "authenticated"}:
                return False
            if next_state == "challenge":
                raise BrowserRegistrationError("manual_challenge_required")
            if next_state == "identity_provider":
                raise BrowserRegistrationError("browser_unexpected_identity_provider")
            raise BrowserRegistrationError("browser_passwordless_otp_state_unknown")
        raise BrowserRegistrationError("browser_existing_account")
    if _click_passwordless_otp(page):
        next_state = _wait_for_registration_state(page, 20, config=config)
        if next_state in {"otp", "authenticated"}:
            return False
        if next_state == "challenge":
            raise BrowserRegistrationError("manual_challenge_required")
        if next_state == "identity_provider":
            raise BrowserRegistrationError("browser_unexpected_identity_provider")
        if next_state == "login_password":
            raise BrowserRegistrationError("browser_existing_account")
        raise BrowserRegistrationError("browser_passwordless_otp_state_unknown")
    field = _first_visible(page, ("input[type='password']", "input[name='password']", "input[autocomplete='new-password']"))
    if field is None:
        return False
    field.fill(password)
    _click_continue(page)
    return True


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


def _otp_fields(page):
    selectors = (
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
        "input[name*='code' i]",
        "input[aria-label*='code' i]",
    )
    for selector in selectors:
        try:
            fields = page.locator(selector)
            count = fields.count()
            for index in range(count):
                if fields.nth(index).is_visible():
                    return fields
        except Exception:
            continue
    return None


def _fill_otp(page, code: str) -> None:
    fields = _otp_fields(page)
    if fields is None:
        raise BrowserRegistrationError("browser_otp_field_missing")
    count = fields.count()
    for index in range(count):
        try:
            fields.nth(index).fill("")
        except Exception:
            pass
    if count == 1:
        fields.first.fill(code)
    else:
        for index, digit in enumerate(str(code)[:count]):
            fields.nth(index).fill(digit)
    _click_continue(page)


def _otp_page_state(page) -> dict[str, Any]:
    """Capture OTP DOM state without exposing the code itself."""
    try:
        return page.evaluate("""() => ({
          url: location.href,
          inputs: [...document.querySelectorAll('input')].map(el => ({
            type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
            autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
            aria_invalid: el.getAttribute('aria-invalid') || '', has_value: !!el.value
          })),
          buttons: [...document.querySelectorAll('button,a,[role=button],input[type=submit]')].map(el => ({
            name: el.getAttribute('name') || '', value: el.getAttribute('value') || '',
            testid: el.getAttribute('data-testid') || '', disabled: !!el.disabled
          })),
          errors: [...document.querySelectorAll('[aria-invalid=true],[role=alert],[class*=error i]')]
            .map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean).slice(0, 10)
        })""") or {}
    except Exception:
        return {}


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


def _complete_profile(page, name: str, birthdate: str) -> None:
    parts = str(birthdate or "").split("-")
    if len(parts) != 3:
        raise BrowserRegistrationError("browser_birthdate_invalid")
    year, month, day = parts
    age = date.today().year - int(year) - ((date.today().month, date.today().day) < (int(month), int(day)))
    try:
        result = page.evaluate("""({name, birthday, year, month, day, age}) => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
            && !el.disabled && !el.readOnly;
          const setValue = (el, value) => {
            if (!el) return false;
            const tag = (el.tagName || '').toLowerCase();
            const proto = tag === 'select' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, String(value)); else el.value = String(value);
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.blur?.();
            return true;
          };
          const attrs = el => [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.type].filter(Boolean).join(' ').toLowerCase();
          const inputs = [...document.querySelectorAll('input,select,textarea')].filter(visible);
          const nameField = inputs.find(el => /(^|\\s)(name|fullname|full_name)(\\s|$)/.test(attrs(el)) || String(el.autocomplete || '').toLowerCase() === 'name');
          const first = inputs.find(el => /(^|\\s)(firstname|first_name)(\\s|$)/.test(attrs(el)));
          const last = inputs.find(el => /(^|\\s)(lastname|last_name)(\\s|$)/.test(attrs(el)));
          const birthdayField = inputs.find(el => ['date'].includes(String(el.type || '').toLowerCase()) || /birth(day|date)?/.test(attrs(el)));
          const ageField = inputs.find(el => /(^|\\s)age(\\s|$)/.test(attrs(el)) || String(el.id || '').toLowerCase().endsWith('-age'));
          const set = {name:false, birth:false};
          if (nameField) { setValue(nameField, name); set.name = true; }
          else {
            if (first) { setValue(first, String(name).split(/\\s+/, 1)[0]); set.name = true; }
            if (last) { setValue(last, String(name).split(/\\s+/).slice(1).join(' ') || 'User'); set.name = set.name || true; }
          }
          if (ageField) { setValue(ageField, age); set.birth = true; }
          else if (birthdayField) { setValue(birthdayField, birthday); set.birth = true; }
          else {
            const y = inputs.find(el => /(^|\\s)(year)(\\s|$)/.test(attrs(el)));
            const m = inputs.find(el => /(^|\\s)(month)(\\s|$)/.test(attrs(el)));
            const d = inputs.find(el => /(^|\\s)(day)(\\s|$)/.test(attrs(el)));
            if (y && m && d) { setValue(y, year); setValue(m, month); setValue(d, day); set.birth = true; }
          }
          if (!set.birth) {
            const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select,.react-aria-Select select,select')]
              .filter(el => !el.disabled);
            const has = (el, value) => [...el.options].some(opt => String(opt.value) === String(value));
            const nums = el => [...el.options].map(opt => Number(opt.value)).filter(Number.isFinite);
            const ys = selects.find(el => has(el, year) && Math.max(...nums(el), -Infinity) > 1900);
            const ms = selects.find(el => el !== ys && (has(el, String(Number(month))) || has(el, month)) && Math.max(...nums(el), -Infinity) <= 12);
            const ds = selects.find(el => el !== ys && el !== ms && (has(el, String(Number(day))) || has(el, day)) && Math.max(...nums(el), -Infinity) >= 28);
            if (ys && ms && ds) {
              setValue(ys, year);
              setValue(ms, has(ms, String(Number(month))) ? String(Number(month)) : month);
              setValue(ds, has(ds, String(Number(day))) ? String(Number(day)) : day);
              set.birth = true;
            }
          }
          const spin = [...document.querySelectorAll('[role=spinbutton][data-type]')].filter(visible);
          const byType = type => spin.find(el => String(el.getAttribute('data-type') || '').toLowerCase() === type);
          for (const [type, value] of [['year',year],['month',month.padStart(2,'0')],['day',day.padStart(2,'0')]]) {
            const el = byType(type);
            if (el) {
              el.focus();
              if ('value' in el) el.value = value; else el.textContent = value;
              el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              el.blur?.();
            }
          }
          if (spin.length >= 3) set.birth = true;
          for (const el of document.querySelectorAll('input[type=checkbox],[role=checkbox]')) {
            if (visible(el) && (el.checked === false || el.getAttribute('aria-checked') === 'false')) el.click();
          }
          return set;
        }""", {"name": name, "birthday": birthdate, "year": year, "month": month, "day": day, "age": str(age)})
        if not isinstance(result, Mapping) or not result.get("birth"):
            raise BrowserRegistrationError("browser_profile_birthdate_missing")
        if not result.get("name"):
            raise BrowserRegistrationError("browser_profile_name_missing")
        _click_continue(page)
        return
    except BrowserRegistrationError:
        raise
    except Exception:
        pass

    # Conservative fallback for simple native forms.
    name_field = _first_visible(page, ("input[name='name']", "input[autocomplete='name']", "input[placeholder*='name' i]"))
    date_field = _first_visible(page, ("input[type='date']", "input[name*='birth' i]", "input[placeholder*='birth' i]"))
    if name_field is None or date_field is None:
        raise BrowserRegistrationError("browser_profile_fields_missing")
    name_field.fill(name)
    date_field.fill(birthdate)
    _click_continue(page)


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


def _session_error_marker(body: Mapping[str, Any]) -> str:
    """Return a small, non-secret error marker from a session response."""
    values: list[str] = []
    for key in ("error", "code", "name", "message", "type"):
        value = body.get(key)
        if isinstance(value, Mapping):
            values.extend(
                str(value.get(item) or "")
                for item in ("error", "code", "name", "message", "type")
            )
        elif isinstance(value, (str, int)):
            values.append(str(value))
    return " ".join(item.strip().lower() for item in values if item and item.strip())[:200]


def _session_context_closed(value: str) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "target page, context or browser has been closed",
        "target closed",
        "context closed",
        "browser has been closed",
        "no such window",
        "invalid session id",
        "session deleted because of page crash",
        "nosuchwindowexception",
        "invalidsessionidexception",
        "targetclosederror",
    ))


def _terminal_session_error(status: int, error_marker: str) -> str:
    marker = str(error_marker or "").lower().replace("_", "").replace("-", "").replace(" ", "")
    if "oauthaccountnotlinked" in marker:
        return "browser_session_account_not_linked"
    if "accessdenied" in marker:
        return "browser_session_access_denied"
    if "sessionrequired" in marker:
        return "browser_session_unauthorized"
    if "refreshaccesstokenerror" in marker:
        return "browser_session_token_refresh_failed"
    if any(value in marker for value in ("oauthcallback", "oauthcreateaccount", "emailcreateaccount")):
        return "browser_session_oauth_callback_failed"
    if status == 429:
        return "browser_session_rate_limited"
    if status == 401:
        return "browser_session_unauthorized"
    if status == 403:
        return "browser_session_forbidden"
    if status in {404, 405}:
        return "browser_session_endpoint_unavailable"
    return ""


def _session_payload(
    session: PlaywrightBrowserSession,
    chat_base: str,
    email: str,
    *,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    # Selenium/Roxy performs the callback/window selection inside fetch_json;
    # keep the generic session loop origin-safe by letting that adapter own the
    # final navigation instead of forcing a second page.goto here.
    deadline = time.monotonic() + max(1, int(timeout_seconds or 90))
    last_status = 0
    last_body_keys: list[str] = []
    last_fetch_error = ""
    last_error_marker = ""
    consecutive_closed = 0
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        remaining_ms = max(1_000, min(5_000, int(max(0.1, deadline - time.monotonic()) * 1_000)))
        try:
            session_url = f"{chat_base.rstrip('/')}/api/auth/session"
            try:
                payload = session.fetch_json(session_url, timeout_ms=remaining_ms)
            except TypeError as exc:
                # Keep compatibility with small third-party adapters that still
                # expose the pre-timeout fetch_json(url) signature.
                if "timeout_ms" not in str(exc):
                    raise
                payload = session.fetch_json(session_url)
            last_fetch_error = ""
        except BrowserRegistrationError:
            raise
        except Exception as exc:
            last_fetch_error = type(exc).__name__
            if _session_context_closed(f"{type(exc).__name__}: {exc}"):
                consecutive_closed += 1
                if consecutive_closed >= 2:
                    raise BrowserRegistrationError("browser_session_context_closed", last_fetch_error) from exc
            else:
                consecutive_closed = 0
            select_page = getattr(session, "select_live_page", None)
            if callable(select_page):
                try:
                    select_page()
                except Exception:
                    pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1, remaining))
            continue
        last_status = int(payload.get("status") or 0) if isinstance(payload, dict) else 0
        body = payload.get("body") if isinstance(payload, dict) else {}
        if not isinstance(body, dict):
            body = {}
        last_body_keys = sorted(str(key) for key in body.keys())[:30]
        error_marker = _session_error_marker(body)
        last_error_marker = error_marker
        if _session_context_closed(error_marker):
            consecutive_closed += 1
            if consecutive_closed >= 2:
                raise BrowserRegistrationError("browser_session_context_closed", f"http_{last_status or 'unknown'}")
        else:
            consecutive_closed = 0
        terminal_error = _terminal_session_error(last_status, error_marker)
        if terminal_error:
            raise BrowserRegistrationError(terminal_error, f"http_{last_status or 'unknown'}")
        candidate = body.get("session") if isinstance(body.get("session"), dict) else body
        access_token = str(
            candidate.get("accessToken") or candidate.get("access_token") or body.get("accessToken") or ""
        ).strip()
        user = candidate.get("user") if isinstance(candidate.get("user"), dict) else body.get("user")
        user_email = str((user or {}).get("email") or "").strip().lower()
        if access_token:
            if user_email and user_email != email.lower():
                raise BrowserRegistrationError("browser_session_email_mismatch")
            return {
                "body": body,
                "access_token": access_token,
                "id_token": str(candidate.get("idToken") or candidate.get("id_token") or ""),
                "user": user or {},
                "status_code": last_status,
            }
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    details = f"http_{last_status or 'unknown'}"
    if last_body_keys:
        details += ":keys=" + ",".join(last_body_keys)
    details += f":attempts={attempts}"
    if last_fetch_error:
        details += ":error=" + last_fetch_error
    state_fn = getattr(session, "context_state", None)
    if callable(state_fn):
        try:
            state = state_fn()
            details += ":host=" + str(state.get("current_host") or "")
            details += ":session_cookie=" + ("present" if state.get("session_cookie_present") else "absent")
        except Exception:
            pass
    if "chatgpt_context_unavailable" in last_error_marker:
        code = "browser_chatgpt_context_unavailable"
    elif last_status == 429:
        code = "browser_session_rate_limited"
    elif last_status >= 500:
        code = "browser_session_unavailable"
    elif last_status and last_status != 200:
        code = "browser_session_http_error"
    elif last_fetch_error and not last_status:
        code = "browser_session_request_failed"
    else:
        code = "browser_session_access_token_missing"
    raise BrowserRegistrationError(code, details)


def _browser_diagnostics(page: Any, driver: str) -> dict[str, Any]:
    diagnostics = {"driver": driver, "url_host": "", "title": ""}
    try:
        diagnostics["url_host"] = str(urlsplit(str(page.url or "")).hostname or "")
    except Exception:
        pass
    try:
        diagnostics["title"] = _safe_text(page.title())[:120]
    except Exception:
        pass
    return diagnostics


def _safe_proxy_audit(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only non-sensitive proxy-pool audit fields in registration output."""
    value = metadata if isinstance(metadata, Mapping) else {}
    return {
        "pool_index": int(value.get("pool_index", -1) or -1),
        "expected_country": str(value.get("expected_country") or "").strip().upper(),
        "actual_country": str(value.get("actual_country") or "").strip().upper(),
        "scheme": str(value.get("scheme") or "").strip().lower(),
    }


def _post_registration_dwell(config: Mapping[str, Any]) -> float:
    """Keep the connected browser alive briefly after the first good AT probe."""
    registration = config.get("registration") if isinstance(config, Mapping) else {}
    registration = registration if isinstance(registration, Mapping) else {}
    raw = str(registration.get("post_registration_dwell_seconds_range") or "0,0")
    try:
        values = [float(item.strip()) for item in raw.replace(";", ",").split(",") if item.strip()]
        lo = values[0] if values else 0.0
        hi = values[1] if len(values) > 1 else lo
    except (TypeError, ValueError):
        lo = hi = 0.0
    lo, hi = max(0.0, lo), max(0.0, hi)
    if hi < lo:
        lo, hi = hi, lo
    seconds = random.uniform(lo, hi) if hi > lo else lo
    if seconds > 0:
        time.sleep(min(300.0, seconds))
    return seconds


def _browser_failure_class(code: str) -> str:
    value = str(code or "").lower()
    if any(marker in value for marker in ("rate_limited", "rate_limit")):
        return "rate_limit"
    if any(marker in value for marker in ("existing_account", "session_account_not_linked")):
        return "account"
    if any(marker in value for marker in ("otp_timeout", "otp_rejected", "mailbox", "email_otp")):
        return "mailbox"
    if any(marker in value for marker in (
        "identity_provider", "manual_challenge", "session_email_mismatch",
        "session_access_token_missing", "session_unauthorized", "session_forbidden",
        "session_access_denied", "session_oauth_callback_failed", "session_token_refresh_failed",
        "chatgpt_context_unavailable", "profile_", "passwordless_otp", "otp_restart_state",
        "registration_state_unknown", "auth_state",
    )):
        return "auth_state"
    if "proxy_blocked" in value or "proxy_country_mismatch" in value:
        return "network"
    if any(marker in value for marker in (
        "dependency_missing", "api_key_missing", "workspace_id_missing", "profile_create_failed",
        "debug_address_missing", "session_endpoint_unavailable", "config", "unsupported",
    )):
        return "configuration"
    return "network"


def _browser_heartbeat(browser: Any, page: Any) -> Any:
    """Keep cloud sessions active and recover a replacement page target."""
    select_page = getattr(browser, "select_live_page", None)
    if callable(select_page):
        try:
            page = select_page() or page
        except Exception:
            pass
    if page is None:
        raise BrowserRegistrationError("browser_session_context_closed", "page_missing")
    try:
        page.evaluate("() => ({host: location.hostname, ready: document.readyState})")
    except Exception as exc:
        if not _session_context_closed(f"{type(exc).__name__}: {exc}"):
            return page
        replacement = None
        if callable(select_page):
            try:
                replacement = select_page()
            except Exception:
                replacement = None
        if replacement is not None and replacement is not page:
            try:
                replacement.evaluate("() => ({host: location.hostname, ready: document.readyState})")
                return replacement
            except Exception:
                pass
        raise BrowserRegistrationError("browser_session_context_closed", type(exc).__name__) from exc
    return page


def _page_is_alive(page: Any) -> bool:
    """Return True unless the page is definitively gone.

    Decides whether an OTP retry should merely resend the code or rebuild the
    whole email step.  Only an explicit crash / close marker counts as dead:
    a transient evaluate failure (navigation in flight, stub page) must not
    force a costly full-flow rebuild, so a live page keeps the historical
    resend-only behaviour.
    """
    if page is None:
        return False
    try:
        page.evaluate("() => 1")
        return True
    except Exception as exc:
        return not _session_context_closed(f"{type(exc).__name__}: {exc}")


def _prepare_session_page(browser: Any, page: Any, timeout_seconds: int) -> Any:
    """Give the natural OAuth callback a bounded grace period before session polling."""
    ensure_context = getattr(browser, "ensure_chatgpt_context", None)
    if callable(ensure_context):
        try:
            ensure_context(auto_jump_wait=min(15, max(0, int(timeout_seconds or 0))))
        except Exception:
            # `_session_payload` owns retry/classification.  A failed proactive
            # navigation here must not turn a transient callback delay terminal.
            pass
    return getattr(browser, "page", None) or page


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


def _bind_totp_in_browser(page: Any, access_token: str, device_id: str) -> dict[str, Any]:
    """Enroll and activate TOTP 2FA through the browser's fetch API.

    Routes the MFA enroll and activate HTTP requests through
    ``page.evaluate(fetch(...))`` so they carry the browser's real
    cookies, fingerprint, and Cloudflare clearance.  Returns a dict
    with ``ok``, ``totp_secret``, and optionally ``error``.
    """
    chat_base = "https://chatgpt.com"
    enroll_script = """
    async ([url, accessToken, deviceId]) => {
        const r = await fetch(url, {
            method: "POST",
            headers: {
                "Authorization": "Bearer " + accessToken,
                "oai-device-id": deviceId,
                "oai-language": "en-US",
                "Content-Type": "application/json",
                "Referer": "https://chatgpt.com/",
            },
            credentials: "include",
            body: JSON.stringify({"factor_type": "totp"}),
        });
        const data = await r.json().catch(() => ({}));
        return {status: r.status, body: data};
    }
    """
    activate_script = """
    async ([url, accessToken, deviceId, code, sessionId]) => {
        const r = await fetch(url, {
            method: "POST",
            headers: {
                "Authorization": "Bearer " + accessToken,
                "oai-device-id": deviceId,
                "oai-language": "en-US",
                "Content-Type": "application/json",
                "Referer": "https://chatgpt.com/",
            },
            credentials: "include",
            body: JSON.stringify({"code": code, "factor_type": "totp", "session_id": sessionId}),
        });
        const data = await r.json().catch(() => ({}));
        return {status: r.status, body: data};
    }
    """
    try:
        enroll_result = page.evaluate(
            enroll_script,
            [f"{chat_base}/backend-api/accounts/mfa/enroll", access_token, device_id],
        )
        if not isinstance(enroll_result, dict) or enroll_result.get("status") != 200:
            error_detail = str(enroll_result)[:300] if enroll_result else "no response"
            return {"ok": False, "error": f"browser_totp_enroll_failed: {error_detail}"}
        enroll_body = enroll_result.get("body") or {}
        secret = str(enroll_body.get("secret") or "").strip()
        session_id = str(enroll_body.get("session_id") or "").strip()
        if not secret or not session_id:
            return {"ok": False, "error": "browser_totp_enroll_missing_fields"}
        # Generate TOTP code and activate
        try:
            import pyotp
            totp_code = pyotp.TOTP(secret).now()
        except ImportError:
            return {"ok": False, "error": "pyotp_not_installed", "totp_secret": secret}

        activate_result = page.evaluate(
            activate_script,
            [f"{chat_base}/backend-api/accounts/mfa/user/activate_enrollment", access_token, device_id, totp_code, session_id],
        )
        if not isinstance(activate_result, dict) or activate_result.get("status") != 200:
            error_detail = str(activate_result)[:300] if activate_result else "no response"
            return {"ok": False, "error": f"browser_totp_activate_failed: {error_detail}", "totp_secret": secret}
        activate_body = activate_result.get("body") or {}
        if not activate_body.get("success"):
            return {"ok": False, "error": f"browser_totp_activate_not_successful: {activate_body}", "totp_secret": secret}
        return {"ok": True, "totp_secret": secret}
    except Exception as exc:
        return {"ok": False, "error": f"browser_totp_exception: {type(exc).__name__}: {exc}"}


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
    from ..browser_pool import BrowserProcessPool, PoolConfig

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


def run_browser_registration(
    *,
    driver_name: str,
    proxy: str | None,
    password: str | None,
    mailbox: Any,
    config: Mapping[str, Any],
    browser_headless: bool | None = None,
    enroll_2fa: bool = True,
    probe_fn=None,
    session_factory=create_browser_session,
    proxy_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one browser registration; result matches the protocol result contract."""
    try:
        driver_name = normalize_registration_driver(driver_name)
    except ValueError as exc:
        return {
            "success": False,
            "registration_driver": str(driver_name or ""),
            "error": _safe_text(str(exc)),
            "failure_class": "configuration",
        }
    if driver_name == "protocol":
        return {
            "success": False,
            "registration_driver": driver_name,
            "error": "unsupported_registration_driver:protocol",
            "failure_class": "configuration",
        }
    try:
        mailbox = _ensure_mailbox_account(mailbox)
    except Exception as exc:
        return {
            "success": False,
            "registration_driver": driver_name,
            "error": "browser_mailbox_setup_failed",
            "failure_class": "mailbox",
            "mailbox": _browser_mailbox_snapshot(mailbox),
        }
    email = str(getattr(mailbox, "email", "") or "").strip()
    if not email:
        return {"success": False, "error": "mailbox_required", "registration_driver": driver_name}
    email_cfg = config.get("email_registration") if isinstance(config.get("email_registration"), Mapping) else {}
    password = str(password or "").strip()
    if not password:
        password = _generate_password()
    full_name = " ".join(_random_name())
    birthdate = _random_birthdate()
    selected_cfg = _driver_config(config, driver_name)
    if browser_headless is not None:
        headless = bool(browser_headless)
    elif "open_headless" in selected_cfg:
        headless = bool(selected_cfg.get("open_headless"))
    elif "headless" in selected_cfg:
        headless = bool(selected_cfg.get("headless"))
    else:
        headless = bool(_config_value(config, "browser_headless", True))
    timeout = int(_config_value(config, "browser_timeout_seconds", 90) or 90)
    locale = str(_config_value(config, "browser_locale", "en-US") or "en-US")
    timezone_id = str(_config_value(config, "browser_timezone", "America/New_York") or "America/New_York")
    otp_timeout = int(email_cfg.get("otp_timeout") or 300)
    try:
        mailbox_service = MailboxService.create(config)
    except Exception as exc:
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": "browser_mailbox_service_unavailable",
            "failure_class": "network",
            "mailbox": _browser_mailbox_snapshot(mailbox),
        }
    started = int(time.time())
    machine = RegistrationStateMachine(registration_stage)
    machine.transition(RegistrationState.MAILBOX_READY)
    from ..storage import get_device_context
    device_context = get_device_context(email, runtime_config=config)
    device_id = str((device_context or {}).get("device_id") or uuid.uuid4())
    machine.transition(RegistrationState.IDENTITY_READY)
    chat_cfg = config.get("chatgpt") if isinstance(config.get("chatgpt"), Mapping) else {}
    chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
    auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
    start_url = str((selected_cfg or {}).get("start_url") or f"{chat_base}/auth/login")
    page = None
    diagnostics = {"driver": driver_name, "url_host": "", "title": ""}
    account_key = email
    browser_identity: dict[str, Any] = {
        "driver": driver_name,
        "profile_id": account_key,
    }
    # --- Browser fingerprint pool + exit-geo alignment -----------------------
    # Mirror turb-gpt-free-register's BROWSER_PROFILE_POOL + _detect_exit_geo:
    # draw a per-account browser profile (seed-stable by device_id) and align
    # its locale/timezone to the proxy's real egress.  Geo detection degrades
    # to {} on any failure, in which case we keep the configured locale/tz so
    # registration never blocks on the network probe.
    from ..browser_fingerprint_pool import (
        detect_proxy_exit_geo,
        select_browser_profile,
    )

    _browser_geo_enabled = bool(_config_value(config, "browser_geo_alignment", True))
    _browser_geo = detect_proxy_exit_geo(proxy, enabled=_browser_geo_enabled)
    _browser_profile = select_browser_profile(_browser_geo, seed=device_id, config=config)
    if _browser_profile.get("navigator_language"):
        locale = str(_browser_profile["navigator_language"])
    if _browser_profile.get("timezone_iana"):
        timezone_id = str(_browser_profile["timezone_iana"])
    # Viewport (screen) is only applied to the local Playwright driver; external
    # anti-detect browsers (Roxy/Cloak/Camoufox/cloud) manage their own screen.
    _browser_viewport = None
    if driver_name == "playwright":
        _browser_viewport = (
            int(_browser_profile.get("screen_width") or 1440),
            int(_browser_profile.get("screen_height") or 900),
        )
    try:
        with _browser_session_scope(
            driver_name=driver_name, config=config, proxy=proxy, headless=headless,
            timeout_ms=max(5_000, timeout * 1_000), locale=locale, timezone_id=timezone_id,
            browser_identity=browser_identity, viewport=_browser_viewport,
            session_factory=session_factory,
        ) as browser:
            if driver_name in {"roxy", "cloak"}:
                from .external_sessions import verify_browser_proxy_country

                verification = verify_browser_proxy_country(browser, expected_country=str((proxy_metadata or {}).get("expected_country") or ""), timeout_seconds=min(20, timeout))
                if proxy_metadata is not None:
                    proxy_metadata = dict(proxy_metadata)
                    proxy_metadata["actual_country"] = verification.get("actual_country", "")
                if not verification.get("ok"):
                    raise BrowserRegistrationError(f"{driver_name}_proxy_country_mismatch", str(verification.get("error") or "unknown"))
            page = browser.page
            browser.add_device_cookie(device_id, chat_base, auth_base)
            machine.transition(RegistrationState.AUTH_FLOW)
            page.goto(start_url, wait_until="domcontentloaded", timeout=timeout * 1_000)
            _maybe_accept_cookies(page)
            # P1 risk-control: replay a real browser's ChatGPT first-screen
            # request sequence (mirrors turb-gpt-free-register's
            # core/chatgpt_bootstrap.py).  Config-gated (default off) and
            # non-fatal by construction, so it can never gate registration.
            from ..chatgpt_bootstrap import run_anonymous_bootstrap

            _anon_bootstrap = run_anonymous_bootstrap(page, config)
            _ensure_signup_page_ready(page, timeout_seconds=min(45, timeout), config=config)
            from ..mailbox import _snapshot_mailbox_message
            _snapshot_mailbox_message(mailbox, proxy=proxy)
            started = int(time.time())
            _fill_email(page, email, config=config)
            machine.transition(RegistrationState.USER_REGISTER)
            state = _wait_for_registration_state(page, min(timeout, 30), browser=browser, config=config)
            if state == "challenge":
                if not _wait_for_challenge_clear(page, max_wait_seconds=30):
                    raise BrowserRegistrationError("manual_challenge_required")
                state = _wait_for_registration_state(page, min(timeout, 30), browser=browser, config=config)
                if state == "challenge":
                    raise BrowserRegistrationError("manual_challenge_required")
            if state == "identity_provider":
                raise BrowserRegistrationError("browser_unexpected_identity_provider")
            if state == "login_password":
                raise BrowserRegistrationError("browser_existing_account")
            if state == "unknown":
                raise BrowserRegistrationError("browser_registration_state_unknown")
            if state == "otp":
                password_used = False
            elif state == "password":
                password_used = _fill_password_if_present(page, password, config=config)
            else:
                password_used = False
            machine.transition(RegistrationState.EMAIL_OTP_SEND)
            if password_used:
                state = _wait_for_registration_state(page, min(timeout, 30), browser=browser, config=config)
                if state == "challenge":
                    if not _wait_for_challenge_clear(page, max_wait_seconds=30):
                        raise BrowserRegistrationError("manual_challenge_required")
                    state = _wait_for_registration_state(page, min(timeout, 30), browser=browser, config=config)
                    if state == "challenge":
                        raise BrowserRegistrationError("manual_challenge_required")
                if state == "login_password":
                    raise BrowserRegistrationError("browser_existing_account")
                if state == "identity_provider":
                    raise BrowserRegistrationError("browser_unexpected_identity_provider")
                if state == "unknown":
                    raise BrowserRegistrationError("browser_registration_state_unknown")
            fields = _otp_fields(page)
            if fields is not None:
                machine.transition(RegistrationState.EMAIL_OTP_WAIT)
                excluded_otps: set[str] = set()
                for otp_attempt in range(3):
                    otp = _poll_browser_otp(
                        mailbox_service,
                        mailbox,
                        browser=browser,
                        page=page,
                        driver_name=driver_name,
                        subject_keyword="verification code|login code",
                        timeout=otp_timeout,
                        issued_after_unix=started,
                        proxy=proxy,
                        excluded_otps=excluded_otps,
                    )
                    if not otp:
                        # Roxy reference flow retries the latest message without the
                        # send-time/code filters because OpenAI may update or reuse the
                        # same mailbox item after a resend.
                        if otp_attempt > 0:
                            otp = mailbox_service.poll_otp(
                                mailbox,
                                subject_keyword="verification code|login code",
                                timeout=min(15, otp_timeout),
                                issued_after_unix=0,
                                proxy=proxy,
                                excluded_otps=set(),
                            )
                        if not otp:
                            if otp_attempt < 2:
                                restarted = False
                                if not _page_is_alive(page):
                                    try:
                                        page, _ = _restart_email_otp_flow(
                                            browser, page, start_url=start_url, email=email,
                                            password=password, timeout_seconds=timeout,
                                            config=config,
                                        )
                                        restarted = True
                                    except Exception:
                                        restarted = False
                                if restarted or _click_resend(page):
                                    started = int(time.time())
                                    continue
                            raise BrowserRegistrationError("browser_email_otp_timeout")
                    page = getattr(browser, "page", None) or page
                    excluded_otps.add(str(otp))
                    machine.transition(RegistrationState.EMAIL_OTP_VALIDATE)
                    _fill_otp(page, str(otp))
                    outcome = _wait_after_otp_submit(page, timeout_seconds=min(30, timeout))
                    # Match the reference Roxy state machine: absence of an explicit
                    # validation error is accepted even when the old OTP DOM remains
                    # mounted during a slow SPA navigation.
                    if outcome == "accepted":
                        break
                    if outcome == "invalid" and otp_attempt < 2:
                        restarted = False
                        if not _page_is_alive(page):
                            try:
                                page, _ = _restart_email_otp_flow(
                                    browser, page, start_url=start_url, email=email,
                                    password=password, timeout_seconds=timeout,
                                    config=config,
                                )
                                restarted = True
                            except Exception:
                                restarted = False
                        if restarted or _click_resend(page):
                            started = int(time.time())
                else:
                    raise BrowserRegistrationError("browser_email_otp_rejected")
            state = _post_otp_registration_state(
                page,
                browser=browser,
                timeout_seconds=min(30, max(5, timeout)),
                config=config,
            )
            page = getattr(browser, "page", None) or page
            machine.transition(RegistrationState.CREATE_ACCOUNT)
            profile_required = _profile_completion_required(state)
            if profile_required:
                _complete_profile(page, full_name, birthdate)
                if not _wait_for_profile_completion(page, timeout_seconds=min(30, max(5, timeout)), config=config):
                    raise BrowserRegistrationError("browser_profile_submit_timeout")
                page.wait_for_timeout(2_000)
            if _manual_challenge(page):
                if not _wait_for_challenge_clear(page, max_wait_seconds=30):
                    raise BrowserRegistrationError("manual_challenge_required")
            chat_host = str(urlsplit(chat_base).hostname or "").lower()
            if hasattr(browser, "ensure_chatgpt_context"):
                page = _prepare_session_page(browser, page, timeout)
            elif chat_host and chat_host not in str(page.url or "").lower():
                page.goto(chat_base, wait_until="domcontentloaded", timeout=timeout * 1_000)
            _maybe_dismiss_chatgpt_onboarding(page, config=config)
            machine.transition(RegistrationState.AUTH_SESSION)
            session_info = _session_payload(browser, chat_base, email, timeout_seconds=timeout)
            auth_body = session_info["body"]
            access_token = session_info["access_token"]
            machine.transition(RegistrationState.ACCESS_TOKEN_PROBE)
            from ..registration_outcome import _probe_registration_access_token
            effective_probe_fn = probe_fn
            if effective_probe_fn is None:
                # Route the post-registration AT probe through the browser
                # context for ALL browser drivers, not just cloud ones.  This
                # keeps the probe on the same fingerprint and cookies used
                # during registration, preventing the identity drift that
                # causes immediate token revocation when curl_cffi switches
                # to a generic fingerprint.
                effective_probe_fn = lambda account, **kwargs: _browser_access_token_probe(
                    browser,
                    account,
                    timeout=int(kwargs.get("timeout") or timeout),
                )
            probe = _probe_registration_access_token(
                access_token, auth_body,
                # The browser context owns the egress and fingerprint for the
                # probe.  Passing proxy=None would break local drivers that
                # need the proxy for the curl fallback, so only suppress it
                # when the browser probe is active.
                proxy=(None if effective_probe_fn is not None else proxy),
                cfg=config, probe_fn=effective_probe_fn,
                stage_fn=registration_stage, sleep_fn=time.sleep,
            )
            success, error, warning = _registration_outcome(True, {}, access_token, probe)
            if success:
                _post_registration_dwell(config)
            machine.transition(RegistrationState.TOTP_ENROLL)
            # Attempt browser-based TOTP 2FA enrollment when requested and
            # registration succeeded.  Routes MFA API calls through the
            # browser's fetch to carry real cookies and Cloudflare clearance.
            twofa_result: dict[str, Any] = {"ok": False, "reason": "disabled"}
            totp_secret = ""
            if success and enroll_2fa:
                try:
                    twofa_result = _bind_totp_in_browser(page, access_token, device_id)
                    if twofa_result.get("ok"):
                        totp_secret = str(twofa_result.get("totp_secret") or "")
                    elif not twofa_result.get("error"):
                        twofa_result = {"ok": False, "reason": "browser_driver_deferred"}
                except Exception as exc:
                    twofa_result = {"ok": False, "reason": f"browser_totp_exception: {type(exc).__name__}"}
            elif enroll_2fa:
                twofa_result = {"ok": False, "reason": "registration_not_successful"}
            # P1: logged-in first-screen warm-up.  Deliberately placed *after*
            # 2FA enrollment — that enrollment protects the account, so it must
            # never be delayed by decorative warm-up traffic.
            if success:
                from ..chatgpt_bootstrap import run_authenticated_bootstrap

                _auth_bootstrap = run_authenticated_bootstrap(
                    page, access_token, device_id=device_id, config=config
                )
            machine.transition(RegistrationState.FINALIZE)
            from ..account_identity import create_registration_identity
            identity_context = create_registration_identity(
                proxy,
                pool_index=int((proxy_metadata or {}).get("pool_index", -1) or -1),
                device_id=str(device_id),
                account_key=account_key,
                browser_identity=browser_identity,
                config=config,
            )
            # Augment the persisted identity with the browser fingerprint-pool
            # selection + exit-geo.  Recorded as free-form identity_context keys
            # (persisted as-is) rather than overriding ``fingerprint_key`` (that
            # key is canonicalized against the protocol pool and would drop a
            # browser label).  Updating proxy_affinity.country to the detected
            # egress keeps fingerprint geo consistent with the browser locale.
            identity_context = dict(identity_context)
            identity_context["browser_fingerprint_profile"] = str(
                _browser_profile.get("browser_fingerprint_profile") or ""
            )
            identity_context["browser_profile_index"] = int(_browser_profile.get("browser_profile_index", 0))
            identity_context["fingerprint_seed"] = str(_browser_profile.get("fingerprint_seed") or device_id)
            identity_context["geo_country"] = str((_browser_geo or {}).get("country") or "")
            identity_context["geo_timezone"] = str((_browser_geo or {}).get("timezone") or "")
            identity_context["geo_ip"] = str((_browser_geo or {}).get("ip") or "")
            if _browser_geo_enabled:
                _geo_cc = str((_browser_geo or {}).get("country") or "").strip().upper()
                if _geo_cc:
                    _affinity = dict(identity_context.get("proxy_affinity") or {})
                    _affinity["country"] = _geo_cc
                    identity_context["proxy_affinity"] = _affinity
            # The protocol path records the registration country from the exit
            # proxy credential (registration_handlers.finalize ->
            # infer_proxy_country(s.proxy)).  The browser path previously omitted
            # it, so headless-registered sessions stored registration_country=""
            # and were impossible to attribute to a region for payment-matrix
            # routing / liveness geo checks.  Mirror the protocol path here.
            from ..paypal_proxy import infer_proxy_country

            result = {
                "success": success,
                "error": _safe_text(error),
                "email": email,
                "source": "register",
                "register_method": "email",
                "session_type": "web",
                "plan_type": "unknown",
                "password": password if password_used else "",
                "name": full_name,
                "birthdate": birthdate,
                "registration_driver": driver_name,
                "registration_mode": "browser",
                "registration_country": infer_proxy_country(proxy),
                "registration_success_basis": "at_http_200" if success else "",
                "registration_warning": _safe_text(warning),
                "access_token": access_token,
                "id_token": session_info.get("id_token", ""),
                "auth_session": auth_body,
                "cookie_header": browser.cookie_header(),
                "device_id": device_id,
                "identity_context": identity_context,
                # Mirror the protocol path (registration_handlers.finalize): record the
                # fingerprint profile this account was bound to. Previously headless
                # browser registrations left this empty, making the fingerprint-pool
                # hypothesis impossible to isolate. The browser identity binds to the
                # shared fingerprint pool via create_registration_identity, so its
                # fingerprint_key is the canonical per-account fingerprint label.
                "auth_fingerprint_profile": str(
                    identity_context.get("browser_fingerprint_profile")
                    or identity_context.get("fingerprint_key")
                    or ""
                ),
                "response": {"auth_session": auth_body, "access_token_probe": probe},
                "access_token_probe": probe,
                "quota_status": probe.get("quota_status", "") if isinstance(probe, dict) else "",
                "post_registration_ready": success,
                "totp_secret": totp_secret,
                "twofa_enrollment": twofa_result,
                "mailbox": _mailbox_snapshot(mailbox),
                "browser_diagnostics": _browser_diagnostics(page, driver_name),
                "proxy_audit": _safe_proxy_audit(proxy_metadata),
            }
            machine.transition(RegistrationState.COMPLETED)
            result["registration_machine"] = machine.snapshot()
            return result
    except BrowserRegistrationError as exc:
        if machine.state is not RegistrationState.FAILED:
            machine.fail(exc.code)
        if page is not None:
            diagnostics = _browser_diagnostics(page, driver_name)
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": _safe_text(exc),
            "failure_class": _browser_failure_class(exc.code),
            "mailbox": _browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": _safe_proxy_audit(proxy_metadata),
        }
    except Exception as exc:
        if machine.state is not RegistrationState.FAILED:
            machine.fail(type(exc).__name__)
        if page is not None:
            diagnostics = _browser_diagnostics(page, driver_name)
        error = redact_proxy_text(f"{type(exc).__name__}: {exc}", proxy)
        return {
            "success": False,
            "email": email,
            "registration_driver": driver_name,
            "error": _safe_text(error),
            "failure_class": _browser_failure_class(str(exc)),
            "mailbox": _browser_mailbox_snapshot(mailbox),
            "registration_machine": machine.snapshot(),
            "browser_diagnostics": diagnostics,
            "proxy_audit": _safe_proxy_audit(proxy_metadata),
        }


def run_playwright_registration(**kwargs: Any) -> dict[str, Any]:
    return run_browser_registration(driver_name="playwright", **kwargs)


__all__ = ["run_browser_registration", "run_playwright_registration"]


def build_browser_session_file(result: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a browser result through the same canonical session builder."""
    from ..session_builder import build_session_file

    return build_session_file(dict(result or {}))


__all__.append("build_browser_session_file")
