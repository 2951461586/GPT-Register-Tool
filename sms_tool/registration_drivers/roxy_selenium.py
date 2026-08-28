"""Selenium state machines for a RoxyBrowser-owned Chrome profile.

The profile is opened by the Roxy API; Selenium only attaches to the returned
``debuggerAddress`` using Roxy's own Chromedriver. No browser process is
launched or discovered locally.
"""

from __future__ import annotations

import time
from typing import Any

from ..phone_proxy import redact_proxy_text


EMAIL_SELECTORS = (
    "input[type='email']", "input[name='email']", "input[name='username']",
    "input#email-input", "input[autocomplete='email']",
)
OTP_SELECTORS = (
    "input[autocomplete='one-time-code']", "input[name='code']",
    "input[inputmode='numeric']", "input[type='tel']",
)


def _apply_browser_automation_mask(driver: Any) -> None:
    """Apply the small, non-invasive automation mask used by the reference flow."""
    script = """
      Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
      if (!window.chrome) window.chrome = {};
      if (!window.chrome.runtime) window.chrome.runtime = {};
    """
    try:
        add_script = getattr(driver, "execute_cdp_cmd", None)
        if callable(add_script):
            add_script("Page.addScriptToEvaluateOnNewDocument", {"source": script})
    except Exception:
        pass
    try:
        driver.execute_script(script)
    except Exception:
        pass


def _build_driver(opened: Any) -> Any:
    """Attach Selenium using Roxy's returned debugger address and driver path."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:
        raise RuntimeError("browser_dependency_missing:selenium") from exc
    raw = getattr(opened, "raw", None)
    if not isinstance(raw, dict) and isinstance(opened, dict):
        raw = opened
    raw = raw if isinstance(raw, dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    webdriver_url = str(
        getattr(opened, "webdriver_url", "")
        or data.get("webdriver") or data.get("webDriver")
        or data.get("webdriverUrl") or data.get("webdriver_url")
        or raw.get("webdriver") or raw.get("webDriver")
        or raw.get("webdriverUrl") or raw.get("webdriver_url") or ""
    ).strip()
    address = str(
        getattr(opened, "debugger_address", "")
        or data.get("http") or data.get("debuggerAddress") or raw.get("http") or raw.get("debuggerAddress") or ""
    ).strip()
    driver_path = str(
        data.get("driver") or data.get("driverPath") or data.get("driver_path")
        or raw.get("driver") or raw.get("driverPath") or ""
    ).strip()
    if address.startswith(("http://", "https://")):
        from urllib.parse import urlsplit
        address = urlsplit(address).netloc
    options = Options()
    options.page_load_strategy = "eager"
    if webdriver_url:
        try:
            from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver
            driver = RemoteWebDriver(command_executor=webdriver_url, options=options)
        except TypeError:
            # Selenium 3 compatibility for installations that have not yet
            # upgraded to the current requirements.txt version.
            from selenium import webdriver as selenium_webdriver
            driver = selenium_webdriver.Remote(command_executor=webdriver_url, desired_capabilities={})
        _apply_browser_automation_mask(driver)
        return driver
    if not address:
        raise RuntimeError("roxy_selenium_debug_address_missing")
    options.add_experimental_option("debuggerAddress", address)
    driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options) if driver_path else webdriver.Chrome(options=options)
    _apply_browser_automation_mask(driver)
    return driver


def _by():
    from selenium.webdriver.common.by import By
    return By


def _visible(element: Any) -> bool:
    try:
        return bool(element.is_displayed() and element.is_enabled())
    except Exception:
        return False


def _find_visible(driver: Any, selectors: tuple[str, ...], timeout: float = 20.0) -> Any:
    by = _by()
    end = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < end:
        for selector in selectors:
            try:
                for element in driver.find_elements(by.CSS_SELECTOR, selector):
                    if _visible(element):
                        return element
            except Exception:
                pass
        time.sleep(0.25)
    raise RuntimeError("roxy_selenium_element_missing")


def _safe_get(driver: Any, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    last: Exception | None = None
    hosts = tuple(item.lower() for item in accept_hosts)
    for attempt in range(max(1, attempts)):
        try:
            driver.set_page_load_timeout(max(10, int(timeout)))
            driver.get(url)
            return
        except Exception as exc:
            last = exc
            try:
                driver.execute_script("window.stop();")
                current = str(driver.current_url or "").lower()
                has_body = bool(driver.execute_script("return !!document.body"))
                if has_body and (not hosts or any(host in current for host in hosts)):
                    return
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise last or RuntimeError("roxy_selenium_navigation_failed")


def _type_email_address(driver: Any, email: str, timeout: float = 20.0) -> None:
    field = _find_visible(driver, EMAIL_SELECTORS, timeout)
    field.clear()
    field.send_keys(str(email))
    value = str(field.get_attribute("value") or "").strip().lower()
    if value != str(email).strip().lower():
        raise RuntimeError("roxy_email_value_verification_failed")


def _submit_nearest_form_for_active_input(driver: Any) -> bool:
    result = driver.execute_script(r"""
      const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
        && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
      const input = [...document.querySelectorAll('input[type=email],input[name=email],input[name=username],input#email-input,input[autocomplete=email]')].find(visible);
      if (!input || !String(input.value || '').includes('@')) return false;
      const form = input.closest('form');
      if (!form) return false;
      const bad = /google|apple|microsoft|github|oauth|sso|oidc|authorize|consent|social/i;
      const buttons = [...form.querySelectorAll('button,input[type=submit]')].filter(visible).filter(el => !bad.test([el.id,el.name,el.type,el.value,el.className].join(' ')));
      const target = buttons.find(el => String(el.type).toLowerCase() === 'submit') || buttons[0];
      if (!target) return false;
      target.click();
      return true;
    """)
    return bool(result)


def _email_state(driver: Any) -> dict[str, Any]:
    try:
        return driver.execute_script(r"""return {
          url: location.href,
          inputs: [...document.querySelectorAll('input')].filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)).map(el => ({
            type: el.type || '', name: el.name || '', id: el.id || '', autocomplete: el.autocomplete || '', value: el.value || ''
          }))
        };""") or {}
    except Exception:
        return {"url": str(getattr(driver, "current_url", "") or "")}


def _is_otp_page(driver: Any) -> bool:
    url = str(getattr(driver, "current_url", "") or "").lower()
    if "email-verification" in url:
        return True
    by = _by()
    try:
        return any(_visible(item) for selector in OTP_SELECTORS for item in driver.find_elements(by.CSS_SELECTOR, selector))
    except Exception:
        return False


def _is_password_page(driver: Any) -> bool:
    by = _by()
    try:
        return any(_visible(item) for item in driver.find_elements(by.CSS_SELECTOR, "input[type='password'],input[name='password']"))
    except Exception:
        return False


def _wait_email_submit_next_state(driver: Any, email: str, timeout: int = 18) -> str:
    end = time.monotonic() + max(1, timeout)
    cleared_at: float | None = None
    recovered = False
    while time.monotonic() < end:
        if _is_password_page(driver):
            return "password"
        if _is_otp_page(driver):
            return "otp"
        state = _email_state(driver)
        values = [str(item.get("value") or "").strip().lower() for item in state.get("inputs", [])]
        if values and not any(value == str(email).strip().lower() for value in values):
            if cleared_at is None:
                cleared_at = time.monotonic()
            if not recovered and time.monotonic() - cleared_at >= 2 and "email=" in str(state.get("url") or ""):
                _type_email_address(driver, email, timeout=5)
                _submit_nearest_form_for_active_input(driver)
                recovered = True
            if time.monotonic() - cleared_at >= 18:
                return "email_page"
        else:
            cleared_at = None
        time.sleep(0.35)
    return "email_page" if _email_state(driver).get("inputs") else "unknown"


def _submit_email_and_wait_next(driver: Any, email: str, attempts: int = 3) -> str:
    last = "unknown"
    for _ in range(max(1, attempts)):
        _type_email_address(driver, email)
        if not _submit_nearest_form_for_active_input(driver):
            raise RuntimeError("roxy_email_submit_form_missing")
        last = _wait_email_submit_next_state(driver, email)
        if last in {"password", "otp", "logged_in"}:
            return last
        time.sleep(0.8)
    raise RuntimeError(f"roxy_email_submit_state_unknown:{last}")


def _type_otp(driver: Any, code: str) -> None:
    by = _by()
    fields = [item for selector in OTP_SELECTORS for item in driver.find_elements(by.CSS_SELECTOR, selector) if _visible(item)]
    if len(fields) == 1:
        fields[0].clear()
        fields[0].send_keys(str(code))
    elif len(fields) >= len(str(code)):
        for item, digit in zip(fields, str(code)):
            item.clear()
            item.send_keys(digit)
    else:
        raise RuntimeError("roxy_otp_input_missing")
    try:
        driver.execute_script("document.activeElement?.dispatchEvent(new Event('change',{bubbles:true}));")
    except Exception:
        pass


def _submit_email_otp(driver: Any) -> bool:
    """Submit the OTP form without accidentally activating its resend intent."""
    return bool(driver.execute_script(r"""
      const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
        && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
      const otp = [...document.querySelectorAll(
        "input[autocomplete='one-time-code'],input[name='code'],input[inputmode='numeric'],input[type='tel']"
      )].find(visible);
      if (!otp) return false;
      const form = otp.closest('form');
      if (!form) return false;
      const isResend = el => {
        const attrs = [el.name, el.value, el.id, el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name')]
          .filter(Boolean).join(' ').toLowerCase();
        return /resend|send.*again|new.*code/.test(attrs);
      };
      const buttons = [...form.querySelectorAll("button,input[type='submit']")]
        .filter(visible).filter(el => !isResend(el));
      const submit = buttons.find(el => String(el.type || '').toLowerCase() === 'submit') || buttons[0];
      if (submit) {
        submit.click();
        return true;
      }
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit();
        return true;
      }
      otp.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', bubbles:true}));
      otp.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', code:'Enter', bubbles:true}));
      return true;
    """))


def _email_otp_page_state(driver: Any) -> dict[str, Any]:
    try:
        return driver.execute_script(r"""return {
          url: location.href,
          inputs: [...document.querySelectorAll('input')].map(el => ({name:el.name||'',type:el.type||'',autocomplete:el.autocomplete||'',inputmode:el.inputMode||'',ariaInvalid:el.getAttribute('aria-invalid')||'',hasValue:!!el.value})),
          errors: [...document.querySelectorAll('[aria-invalid=true],[role=alert],[class*=error i]')].map(el => (el.innerText||el.textContent||'').trim()).filter(Boolean).slice(0,10)
        };""") or {}
    except Exception:
        return {}


def _click_resend_email_otp(driver, timeout: int = 20) -> bool:
    by = _by()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        selectors = ("button[name='intent'][value='resend']", "input[name='intent'][value='resend']", "button[data-testid*='resend' i]")
        for selector in selectors:
            for item in driver.find_elements(by.CSS_SELECTOR, selector):
                if _visible(item):
                    item.click()
                    return True
        time.sleep(0.25)
    return False


def _wait_after_email_otp_submit(driver: Any, timeout: int = 30) -> str:
    end = time.monotonic() + max(1, timeout)
    last: dict[str, Any] = {}
    while time.monotonic() < end:
        if not _is_otp_page(driver):
            return "accepted"
        last = _email_otp_page_state(driver)
        if last.get("errors") or any(str(item.get("ariaInvalid") or "").lower() == "true" for item in last.get("inputs", [])):
            return "invalid"
        time.sleep(0.5)
    return "invalid" if last.get("errors") else "accepted"


def _fill_profile_react_controls(driver: Any, name: str, birthdate: str, age: int) -> str | None:
    """Fill hidden native selects and React Aria birthday widgets when present."""
    parts = str(birthdate or "").split("-")
    if len(parts) != 3:
        return None
    year, month, day = parts
    try:
        result = driver.execute_script(r"""
          const name = String(arguments[0] || '');
          const year = String(arguments[1]), month = String(Number(arguments[2]));
          const month2 = String(arguments[2]).padStart(2, '0');
          const day = String(Number(arguments[3]));
          const day2 = String(arguments[3]).padStart(2, '0');
          const birthday = String(arguments[4]);
          const age = String(arguments[5]);
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
            && !el.disabled && !el.readOnly;
          const attrs = el => [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.type]
            .filter(Boolean).join(' ').toLowerCase();
          const setValue = (el, value) => {
            if (!el) return false;
            const tag = String(el.tagName || '').toLowerCase();
            const proto = tag === 'select' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, String(value)); else el.value = String(value);
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.blur?.();
            return true;
          };
          const inputs = [...document.querySelectorAll('input,select,textarea')];
          let nameField = inputs.find(el => visible(el) && (/(^|\\s)(name|fullname|full_name)(\\s|$)/.test(attrs(el))
            || String(el.autocomplete || '').toLowerCase() === 'name'));
          if (!nameField) nameField = inputs.find(el => visible(el) && /(^|\\s)firstname(\\s|$)/.test(attrs(el)));
          const nameOk = nameField ? setValue(nameField, name) : false;

          const ageField = inputs.find(el => visible(el) && (/(^|\\s)age(\\s|$)/.test(attrs(el))
            || String(el.id || '').toLowerCase().endsWith('-age') || String(el.type || '').toLowerCase() === 'number'));
          if (ageField && setValue(ageField, age)) return {name:nameOk, birth:true, mode:'age'};
          const dateField = inputs.find(el => (visible(el) || String(el.type || '').toLowerCase() === 'date')
            && (String(el.type || '').toLowerCase() === 'date' || /birth(day|date)?/.test(attrs(el))));
          if (dateField && setValue(dateField, birthday)) return {name:nameOk, birth:true, mode:'birthday'};

          const setByAttr = (kind, values) => {
            const candidates = inputs.filter(el => visible(el) && new RegExp('(^|\\s)' + kind + '(\\s|$)').test(attrs(el)));
            for (const el of candidates) {
              for (const value of values) {
                if (el.tagName === 'SELECT' && ![...el.options].some(o => String(o.value) === String(value) || String(o.textContent || '').trim() === String(value))) continue;
                if (setValue(el, value)) return true;
              }
            }
            return false;
          };
          const yOk = setByAttr('year', [year]);
          const mOk = setByAttr('month', [month, month2]);
          const dOk = setByAttr('day', [day, day2]);
          if (yOk && mOk && dOk) return {name:nameOk, birth:true, mode:'ymd'};

          const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select,.react-aria-Select select,select')]
            .filter(el => !el.disabled);
          const nums = el => [...el.options].map(o => Number(o.value)).filter(Number.isFinite);
          const max = el => Math.max(...nums(el), -Infinity), min = el => Math.min(...nums(el), Infinity);
          const has = (el, value) => [...el.options].some(o => String(o.value) === String(value));
          const ys = selects.find(el => has(el, year) && max(el) > 1900);
          const ms = selects.find(el => el !== ys && (has(el, month) || has(el, month2)) && min(el) <= 1 && max(el) <= 12);
          const ds = selects.find(el => el !== ys && el !== ms && (has(el, day) || has(el, day2)) && max(el) >= 28);
          if (ys && ms && ds) {
            setValue(ys, year); setValue(ms, has(ms, month) ? month : month2); setValue(ds, has(ds, day) ? day : day2);
            const hidden = inputs.find(el => String(el.name || '').toLowerCase() === 'birthday');
            if (hidden) setValue(hidden, birthday);
            return {name:nameOk, birth:true, mode:'react_select'};
          }
          const spin = [...document.querySelectorAll('[role=spinbutton][data-type]')].filter(visible);
          if (spin.length >= 3) return {name:nameOk, birth:false, mode:'spinbutton'};
          return {name:nameOk, birth:false, mode:'missing'};
        """, name, year, month, day, birthdate, str(age)) or {}
        if isinstance(result, dict) and result.get("birth") and result.get("name"):
            return str(result.get("mode") or "birthday")
        if not isinstance(result, dict) or result.get("mode") != "spinbutton" or not result.get("name"):
            return None
    except Exception:
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        modifier = Keys.CONTROL
        for selector, value in (
            ('[role="spinbutton"][data-type="year"]', year),
            ('[role="spinbutton"][data-type="month"]', str(int(month)).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(int(day)).zfill(2)),
        ):
            element = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", element)
            element.send_keys(modifier, "a")
            element.send_keys(str(value))
            driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true})); arguments[0].blur();",
                element,
            )
        return "spinbutton"
    except Exception:
        return None


def _wait_for_profile_completion(driver: Any, timeout: int = 30) -> bool:
    """Confirm the profile route left the form before session extraction."""
    end = time.monotonic() + max(1, int(timeout or 1))
    by = _by()
    selectors = (
        "input[name='name']", "input[autocomplete='name']", "input[name='birthdate']",
        "input[name='birthday']", "input[type='date']", "input[name='age']",
        "input[type='number']", "[role='spinbutton'][data-type]",
    )
    while time.monotonic() < end:
        try:
            url = str(getattr(driver, "current_url", "") or "").lower()
            if "chatgpt.com" in url and "/auth/" not in url:
                return True
            visible_profile = any(
                _visible(element)
                for selector in selectors
                for element in driver.find_elements(by.CSS_SELECTOR, selector)
            )
            if not visible_profile:
                return True
            state = _email_otp_page_state(driver)
            if state.get("errors"):
                return False
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _complete_profile_after_otp(driver: Any, name: str, birthdate: str, timeout: int = 60) -> bool:
    """Wait for and submit the post-OTP profile page in the adopted Roxy tab.

    OpenAI commonly leaves the OTP DOM mounted while its SPA routes to
    ``about-you``.  Treating the OTP wait as the end of registration skips this
    required page and produces a token-less session response.
    """
    by = _by()
    end = time.monotonic() + max(1, timeout)
    last_state = "unknown"
    name_selectors = (
        "input[name='name']", "input[name='fullName']", "input[name='full_name']",
        "input[autocomplete='name']", "input[name='firstName']",
    )
    date_selectors = (
        "input[name='birthdate']", "input[name='birthday']", "input[type='date']",
        "input[name='age']", "input#age", "input[id$='-age']", "input[type='number']",
    )
    while time.monotonic() < end:
        try:
            url = str(driver.current_url or "").lower()
            if "chatgpt.com" in url and "/auth/" not in url:
                return False
            name_field = next(
                (item for selector in name_selectors for item in driver.find_elements(by.CSS_SELECTOR, selector) if _visible(item)),
                None,
            )
            date_field = next(
                (item for selector in date_selectors for item in driver.find_elements(by.CSS_SELECTOR, selector) if _visible(item)),
                None,
            )
            react_filled = False
            if name_field is None:
                parts = str(birthdate or "").split("-")
                if len(parts) == 3:
                    today = __import__("datetime").date.today()
                    age = today.year - int(parts[0]) - ((today.month, today.day) < (int(parts[1]), int(parts[2])))
                    if _fill_profile_react_controls(driver, name, birthdate, age) is not None:
                        react_filled = True
                    elif date_field is None:
                        last_state = "otp" if _is_otp_page(driver) else (url or "unknown")
                        time.sleep(0.5)
                        continue
                else:
                    last_state = "otp" if _is_otp_page(driver) else (url or "unknown")
                    time.sleep(0.5)
                    continue
            if name_field is not None and not react_filled:
                name_field.clear()
                name_field.send_keys(str(name))
            if date_field is not None and not react_filled:
                from datetime import date
                parts = [int(item) for item in str(birthdate).split("-")]
                today = date.today()
                age = today.year - parts[0] - ((today.month, today.day) < (parts[1], parts[2]))
                attrs = " ".join(str(date_field.get_attribute(key) or "") for key in ("name", "id", "type")).lower()
                value = str(age) if "age" in attrs or "number" in attrs else str(birthdate)
                date_field.clear()
                date_field.send_keys(value)
            if date_field is None and not react_filled:
                parts = str(birthdate or "").split("-")
                if len(parts) == 3:
                    today = __import__("datetime").date.today()
                    age = today.year - int(parts[0]) - ((today.month, today.day) < (int(parts[1]), int(parts[2])))
                    if _fill_profile_react_controls(driver, name, birthdate, age) is None:
                        time.sleep(0.5)
                        continue
                    react_filled = True
            # Some regional profile forms gate submission on consent boxes.
            driver.execute_script(r"""
              const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                && !el.disabled;
              for (const input of document.querySelectorAll("input[type='checkbox']")) {
                if (!visible(input) || input.checked) continue;
                input.click();
              }
            """)
            submitted = driver.execute_script(r"""
              const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
              const fields = [...document.querySelectorAll("input[name='name'],input[name='fullName'],input[name='full_name'],input[autocomplete='name'],input[name='birthdate'],input[name='birthday'],input[type='date'],input[name='age'],input#age,input[id$='-age'],input[type='number'],select[name='year'],select[name='month'],select[name='day'],[role='spinbutton'][data-type]")]
                .filter(visible);
              const form = fields[0]?.closest('form');
              const submit = [...(form || document).querySelectorAll("button[type='submit'],input[type='submit']")]
                .find(visible);
              if (submit) { submit.click(); return true; }
              if (form && typeof form.requestSubmit === 'function') { form.requestSubmit(); return true; }
              return false;
            """)
            if submitted:
                return True
        except Exception:
            # A short navigation destroys the execution context; the next poll
            # observes the new document rather than treating it as a terminal error.
            pass
        time.sleep(0.5)
    raise RuntimeError(f"roxy_profile_page_timeout:{last_state}")


def _selenium_exception_marker(exc: Exception, proxy: str | None = None) -> str:
    """Keep Selenium's useful class/message while removing proxy credentials."""
    name = type(exc).__name__
    detail = redact_proxy_text(str(exc), proxy).strip()[:500]
    return f"{name}: {detail}" if detail else name


def _is_closed_window_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "no such window", "invalid session id", "session deleted because of page crash",
        "nosuchwindowexception", "invalidsessionidexception", "targetclosederror",
    ))


def _switch_to_live_window(driver: Any, preferred_host: str = "chatgpt.com") -> bool:
    """Adopt a live Selenium window, preferring the ChatGPT callback target."""
    try:
        handles = list(driver.window_handles or [])
    except Exception:
        return False
    fallback = None
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            current = str(driver.current_url or "").lower()
        except Exception:
            continue
        if preferred_host and preferred_host.lower() in current:
            return True
        if fallback is None:
            fallback = handle
    if fallback is None:
        return False
    try:
        driver.switch_to.window(fallback)
        return True
    except Exception:
        return False


def _fetch_json_with_window_recovery(
    driver: Any,
    target: str,
    *,
    timeout_ms: int = 20_000,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Run a Selenium fetch and retry once after callback-window churn."""
    script = """var done=arguments[arguments.length-1]; fetch(arguments[0], {credentials:'include'}).then(async r=>{let t=await r.text();let b={};try{b=JSON.parse(t)}catch(e){b={raw:t.slice(0,500)}};done({status:r.status,body:b})}).catch(e=>done({status:0,body:{error:String(e)}}));"""
    for attempt in range(2):
        try:
            driver.set_script_timeout(max(1, int(timeout_ms / 1000)))
            result = driver.execute_async_script(script, str(target))
            return result if isinstance(result, dict) else {"status": 0, "body": {}}
        except Exception as exc:
            marker = _selenium_exception_marker(exc, proxy)
            if attempt == 0 and _is_closed_window_error(marker) and _switch_to_live_window(driver):
                continue
            return {"status": 0, "body": {"error": marker}}
    return {"status": 0, "body": {}}


__all__ = [
    "_build_driver", "_safe_get", "_submit_email_and_wait_next", "_type_email_address",
    "_submit_nearest_form_for_active_input", "_wait_email_submit_next_state",
    "_type_otp", "_submit_email_otp", "_email_otp_page_state", "_click_resend_email_otp",
    "_wait_after_email_otp_submit", "_complete_profile_after_otp",
    "_fetch_json_with_window_recovery", "_is_closed_window_error", "_switch_to_live_window",
]
