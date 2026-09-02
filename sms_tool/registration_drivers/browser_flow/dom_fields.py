"""通用 locator / 点击 / 文本原语。完全独立，不依赖本包其它模块。"""

from __future__ import annotations

from ...sanitizer import sanitize_text
from ..base import BrowserRegistrationError
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def _safe_text(value: Any) -> str:
    return sanitize_text(str(value or ""))[:500]


def _config_value(config: Mapping[str, Any], key: str, default: Any) -> Any:
    section = config.get("registration")
    return section.get(key, default) if isinstance(section, Mapping) else default


def _body_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2_000) or "").lower()
    except Exception:
        return ""


def _hard_proxy_block(page) -> bool:
    """Detect a terminal proxy/VPN block before waiting for signup controls."""
    text = _body_text(page)
    markers = (
        "unable to load site", "if you are using a vpn", "try turning it off",
        "access denied", "sorry, you have been blocked",
        "this website is using a security service",
    )
    return any(marker in text for marker in markers)


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
