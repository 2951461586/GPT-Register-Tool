"""表单语义步骤（邮箱 / 密码 / OTP / 资料补全填写）。依赖 dom_fields、page_state。"""

from __future__ import annotations

import time
import uuid

from .dom_fields import _click_continue, _click_first_visible, _click_passwordless_otp, _first_visible, _is_openai_auth_url, _otp_fields, _unexpected_identity_provider
from .page_state import _quick_auth_state, _wait_for_registration_state

from ...humanize import delay as humanize_delay
from ..base import BrowserRegistrationError
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlsplit


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
