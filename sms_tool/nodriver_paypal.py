"""nodriver-based PayPal payment flow.

Uses nodriver (undetected Chrome) to complete the entire PayPal
authorization: navigate → CAPTCHA → create account → fill card →
SMS verify → submit → extract auth tokens.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

_DEBUG_DIR = "runtime/nd_pay_debug"


def _debug_screenshot(page, name: str):
    """Save a debug screenshot."""
    try:
        Path(_DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        path = f"{_DEBUG_DIR}/{name}.png"
        page.save_screenshot(path)
    except Exception:
        pass


async def _debug_page(page, label: str):
    """Print page state for debugging."""
    try:
        title = await page.evaluate("document.title")
        url = page.url
        content = await page.get_content()
        print(f"[nd-pay:{label}] title={title} url={url[:80]} len={len(content)}")
        _debug_screenshot(page, label)
    except Exception as e:
        print(f"[nd-pay:{label}] debug error: {e}")


def run_nodriver_pay(
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_cfg: dict,
    proxy: str = "",
    timeout: int = 180,
) -> dict[str, Any]:
    """Complete PayPal payment via nodriver. Returns result dict."""
    try:
        import nodriver as uc
    except ImportError:
        return {"ok": False, "error": "nodriver not installed"}

    return uc.loop().run_until_complete(
        _run(uc, paypal_url, card, address, first_name, last_name,
             alias_email, password, phone, sms_cfg, proxy, timeout)
    )


async def _run(
    uc: Any,
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_cfg: dict,
    proxy: str,
    timeout: int,
) -> dict[str, Any]:
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    browser = await uc.start(
        headless=False,
        proxy=proxy or None,
        lang="en-US",
        browser_args=browser_args,
    )

    try:
        return await _do_pay(
            browser, paypal_url, card, address,
            first_name, last_name, alias_email, password,
            phone, sms_cfg, timeout,
        )
    finally:
        browser.stop()


async def _do_pay(
    browser: Any,
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_cfg: dict,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout

    # ── Step 1: Navigate ──
    print("[nd-pay] Navigating to PayPal...")
    page = await browser.get(paypal_url)
    await asyncio.sleep(5)

    # ── Step 2: Handle CAPTCHA / Cloudflare ──
    content = await page.get_content()
    if "challenge" in content.lower() or "cloudflare" in content.lower():
        print("[nd-pay] Cloudflare challenge, trying verify_cf...")
        try:
            await page.verify_cf()
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[nd-pay] verify_cf failed: {e}")

    title = await page.evaluate("document.title")
    print(f"[nd-pay] Page: {title} | {page.url[:80]}")
    await _debug_page(page, "01_initial")

    # If still on CAPTCHA page, wait for it to resolve
    content = await page.get_content()
    if "captcha" in content.lower() and "pay with paypal" not in content.lower():
        print("[nd-pay] Waiting for CAPTCHA resolution...")
        for _ in range(30):
            await asyncio.sleep(2)
            content = await page.get_content()
            if "pay with paypal" in content.lower():
                break

    # ── Step 3: Click "Create Account" ──
    print("[nd-pay] Looking for Create Account button...")
    create_clicked = False
    try:
        # Try finding by text content
        el = await page.find("创建账户", timeout=5)
        if el:
            await el.click()
            create_clicked = True
            print("[nd-pay] Clicked 创建账户")
    except Exception:
        pass

    if not create_clicked:
        try:
            el = await page.find("Create Account", timeout=3)
            if el:
                await el.click()
                create_clicked = True
                print("[nd-pay] Clicked Create Account")
        except Exception:
            pass

    if not create_clicked:
        try:
            el = await page.find("Create an account", timeout=3)
            if el:
                await el.click()
                create_clicked = True
                print("[nd-pay] Clicked Create an account")
        except Exception:
            pass

    if not create_clicked:
        print("[nd-pay] No Create Account button found, trying direct form fill")
    else:
        await asyncio.sleep(3)
    await _debug_page(page, "02_after_create_account")

    # ── Step 4: Fill email ──
    print(f"[nd-pay] Filling email: {alias_email}")
    await _fill_field(page, alias_email,
                      selectors=["#email", "input[name='login_email']", "input[type='email']"],
                      clear=True)

    # ── Step 5: Click Next / Submit email ──
    await _click_button(page, [
        "下一页", "Next", "下一步", "Continue", "Continue with email",
    ])
    await asyncio.sleep(3)
    await _debug_page(page, "03_after_email_next")

    # ── Step 6: Fill password ──
    print(f"[nd-pay] Filling password...")
    await _fill_field(page, password,
                      selectors=["#password", "input[name='login_password']", "input[name='password']",
                                 "input[name='createPassword']", "input[type='password']"],
                      clear=True)

    # ── Step 7: Fill name ──
    print(f"[nd-pay] Filling name: {first_name} {last_name}")
    await _fill_field(page, first_name,
                      selectors=["input[name='firstName']", "input[name='first_name']",
                                 "#firstName", "#first_name"],
                      clear=True)
    await _fill_field(page, last_name,
                      selectors=["input[name='lastName']", "input[name='last_name']",
                                 "#lastName", "#last_name"],
                      clear=True)
    await _debug_page(page, "04_after_password_name")

    # ── Step 8: Fill phone ──
    print(f"[nd-pay] Filling phone: {phone}")
    await _fill_field(page, phone,
                      selectors=["input[name='phoneNumber']", "input[name='phone']",
                                 "input[name='login_phone']", "input[type='tel']"],
                      clear=True)

    # ── Step 9: Fill card ──
    print(f"[nd-pay] Filling card: ****{card['number'][-4:]}")
    await _fill_field(page, card["number"],
                      selectors=["input[name='cardNumber']", "input[name='card_number']",
                                 "#cardNumber", "input[autocomplete='cc-number']"],
                      clear=True)
    await _fill_field(page, card["exp_month"],
                      selectors=["input[name='expMonth']", "input[name='exp_month']",
                                 "input[name='cardExpiry']", "input[autocomplete='cc-exp-month']"],
                      clear=True)
    await _fill_field(page, card["exp_year"],
                      selectors=["input[name='expYear']", "input[name='exp_year']",
                                 "input[autocomplete='cc-exp-year']"],
                      clear=True)
    await _fill_field(page, card["cvv"],
                      selectors=["input[name='cvv']", "input[name='cvvNumber']",
                                 "#cvv", "input[autocomplete='cc-csc']"],
                      clear=True)

    await _debug_page(page, "05_after_card")

    # ── Step 10: Fill billing address ──
    addr_line = address.get("line1", "")
    city = address.get("city", "")
    state = address.get("state", "")
    postal = address.get("postal_code", "")

    print(f"[nd-pay] Filling address: {addr_line}, {city}, {state} {postal}")
    await _fill_field(page, addr_line,
                      selectors=["input[name='line1']", "input[name='address1']",
                                 "input[name='billingAddress.line1']"],
                      clear=True)
    await _fill_field(page, city,
                      selectors=["input[name='city']", "input[name='billingAddress.city']"],
                      clear=True)
    await _fill_field(page, postal,
                      selectors=["input[name='postalCode']", "input[name='postal_code']",
                                 "input[name='billingAddress.postalCode']"],
                      clear=True)

    await _debug_page(page, "06_after_address")

    # ── Step 11: Accept terms ──
    print("[nd-pay] Accepting terms...")
    try:
        checkbox = await page.select("input[type='checkbox']", timeout=3)
        if checkbox:
            await checkbox.click()
            await asyncio.sleep(1)
    except Exception:
        pass

    # ── Step 12: Handle SMS verification ──
    print(f"[nd-pay] Handling SMS verification (phone: {phone})...")
    sms_code = await _handle_sms(page, phone, sms_cfg, deadline)
    if sms_code:
        print(f"[nd-pay] SMS code: {sms_code}")

    await _debug_page(page, "07_after_sms")

    # ── Step 13: Submit payment ──
    print("[nd-pay] Submitting payment...")
    await _click_button(page, [
        "Agree & Pay", "Agree and Pay", "同意并付款", "同意并支付",
        "Pay", "Pay Now", "Submit", "确认",
        "同意并继续", "Continue", "下一页",
    ])
    await asyncio.sleep(5)

    # ── Step 14: Wait for redirect ──
    print("[nd-pay] Waiting for redirect...")
    final_url = await _wait_for_redirect(page, deadline)

    # ── Step 5: Extract cookies and result ──
    cookies = {}
    try:
        jar = browser.cookies
        all_cookies = await jar.get_all()
        for c in all_cookies:
            if hasattr(c, "name"):
                cookies[c.name] = c.value
    except Exception as e:
        print(f"[nd-pay] Cookie extraction failed: {e}")

    print(f"[nd-pay] Final URL: {final_url[:80]}")
    print(f"[nd-pay] Extracted {len(cookies)} cookies")

    # Check if we got redirected back to Stripe/ChatGPT
    ok = "chatgpt.com" in final_url or "stripe.com" in final_url
    if ok:
        # Try to extract access token from URL fragment
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(final_url)
        fragment = parsed.fragment or ""
        params = parse_qs(fragment)
        access_token = ""
        for key in ("access_token", "accessToken"):
            if key in params:
                access_token = params[key][0]
        if access_token and access_token.startswith("eyJ"):
            return {
                "ok": True,
                "access_token": access_token,
                "final_url": final_url,
                "cookies": cookies,
            }

    return {
        "ok": ok,
        "final_url": final_url,
        "cookies": cookies,
        "error": "" if ok else f"unexpected final URL: {final_url[:80]}",
    }


# ── Helper functions ──


async def _fill_field(page, value: str, selectors: list[str], clear: bool = True):
    """Fill a form field by trying multiple CSS selectors."""
    for sel in selectors:
        try:
            el = await page.select(sel, timeout=3)
            if el:
                if clear:
                    await el.clear_input()
                await el.send_keys(str(value))
                return True
        except Exception:
            continue

    # Fallback: try via JS
    for sel in selectors:
        try:
            await page.evaluate(f"""
                (function() {{
                    var el = document.querySelector('{sel}');
                    if (el) {{
                        el.value = '{value}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    return false;
                }})()
            """)
            return True
        except Exception:
            continue

    return False


async def _click_button(page, texts: list[str]):
    """Click a button by finding it via text content."""
    for text in texts:
        try:
            el = await page.find(text, timeout=3)
            if el:
                await el.click()
                print(f"[nd-pay] Clicked: {text}")
                return True
        except Exception:
            continue

    # Fallback: try submit buttons
    for sel in ["button[type='submit']", "input[type='submit']"]:
        try:
            el = await page.select(sel, timeout=2)
            if el:
                await el.click()
                print(f"[nd-pay] Clicked submit button")
                return True
        except Exception:
            continue

    return False


async def _handle_sms(page, phone: str, sms_cfg: dict, deadline: float) -> str | None:
    """Handle SMS verification: click send code, poll for code, fill it in."""
    import requests as _requests

    api_url = sms_cfg.get("api_url", "")
    poll_interval = sms_cfg.get("poll_interval", 5)
    sms_timeout = sms_cfg.get("timeout", 120)

    if not api_url:
        print("[nd-pay] No SMS API URL configured")
        return None

    # Take baseline
    baseline_raw = ""
    try:
        r = _requests.get(api_url, timeout=10)
        if r.status_code == 200:
            baseline_raw = r.text.strip()
    except Exception:
        pass

    # Click "Send Code" button
    await _click_button(page, [
        "发送验证码", "Send Code", "获取验证码", "发送", "Send",
        "Send SMS", "发送短信", "Send verification code",
    ])
    await asyncio.sleep(2)

    # Poll for SMS code
    sms_deadline = time.time() + sms_timeout
    code_pattern = re.compile(r"\b(\d{4,6})\b")

    print(f"[nd-pay] Polling SMS (timeout={sms_timeout}s)...")
    while time.time() < sms_deadline and time.time() < deadline:
        try:
            r = _requests.get(api_url, timeout=10)
            if r.status_code == 200:
                current_raw = r.text.strip()
                if current_raw != baseline_raw and current_raw:
                    match = code_pattern.search(current_raw)
                    if match:
                        code = match.group(1)
                        # Fill code
                        await _fill_field(page, code,
                                          selectors=["input[name='code']", "input[name='otp']",
                                                     "input[name='smsCode']", "input[type='text']",
                                                     "input[name='phoneCode']"],
                                          clear=True)
                        return code
        except Exception:
            pass
        await asyncio.sleep(poll_interval)

    return None


async def _wait_for_redirect(page, deadline: float, check_interval: float = 2) -> str:
    """Wait for page to redirect to Stripe/ChatGPT."""
    while time.time() < deadline:
        url = page.url
        if "chatgpt.com" in url or "stripe.com" in url:
            return url
        # Also check if we're on a confirmation page
        content = await page.get_content()
        if "redirecting" in content.lower() or "processing" in content.lower():
            await asyncio.sleep(check_interval)
            continue
        await asyncio.sleep(check_interval)

    return page.url
