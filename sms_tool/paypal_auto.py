"""PayPal auto-payment: reverse protocol first, browser fallback.

Flow:
  1. Try reverse-engineered HTTP protocol (paypal_reverse.py)
  2. Fall back to anti-detect browser automation if reverse fails
     Prefers Camoufox (anti-detection Firefox) with GeoIP matching;
     falls back to CloakBrowser if Camoufox is not installed.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

import requests as _requests

from .account_seed import extract_access_token as _extract_access_token
from .account_seed import load_account_seed as _load_seed
from .config import CFG
from .gen_pp_link import generate_pp_link
from .session_refresh import _poll_auth_session, _session_token
from .storage import upsert_account
from .utils import _generate_password, _random_name

# ──────────────────────────── constants ────────────────────────────


def _safe_import_cookie_header(ctx, cookie_header):
    """Safely import cookies into browser context."""
    if not cookie_header:
        return

    cookies = []
    for item in str(cookie_header).split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        if name.startswith("__Host-"):
            continue
        cookie = {
            "name": name,
            "value": value,
            "domain": ".chatgpt.com",
            "path": "/",
        }
        if name.startswith("__Secure-"):
            cookie["secure"] = True
            cookie["httpOnly"] = True
            cookie["sameSite"] = "Lax"
        cookies.append(cookie)

    if cookies:
        try:
            ctx.add_cookies(cookies)
        except Exception as e:
            print(f"[!] Cookie import warning: {e}")


_CHROME_VERSION = "136"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_VERSION}.0.0.0 Safari/537.36"
)

_SMS_CODE_RE = re.compile(r"\b(\d{4,6})\b")


class _PayPalStepError(Exception):
    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")


# ──────────────────────────── public entry ────────────────────────────


def auto_pay(
    email: str = "",
    session_file: str = "",
    proxy: str | None = None,
    headless: bool = False,
    timeout: int = 180,
    reverse_only: bool = False,
) -> dict[str, Any]:
    """Automatically complete PayPal payment for a ChatGPT account.

    Args:
        reverse_only: If True, only use reverse protocol (no browser fallback).
    """
    cfg = CFG.get("paypal_auto") or {}
    if not cfg:
        return {"ok": False, "error": "paypal_auto not configured in config.json"}

    # 1. Load seed session
    data, json_path = _load_seed(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    if target_email:
        data["email"] = target_email

    access_token = _extract_access_token(data)
    if not access_token:
        return {"ok": False, "email": target_email, "error": "missing_access_token"}

    # 2. Get or generate PayPal URL
    paypal = data.get("paypal") or {}
    paypal_url = str(paypal.get("url") or "").strip()
    if not paypal_url:
        print("[*] No PayPal URL found, generating...")
        paypal = generate_pp_link(access_token)
        if not paypal.get("ok") or not paypal.get("url"):
            return {"ok": False, "email": target_email, "error": f"paypal_link_generation_failed: {paypal.get('error', '')}"}
        paypal_url = paypal["url"]
        data["paypal"] = paypal

    print(f"[*] PayPal URL: {paypal_url[:80]}...")

    # 3. Pick card + address + phone
    card, address = _pick_card_and_address(cfg)
    phone, sms_api_url = _pick_phone_and_sms(cfg)
    first_name, last_name = _random_name()
    password = _generate_password()
    alias_email = _generate_alias_email(target_email)

    print(f"[*] Card: ****{card['number'][-4:]}  Name: {first_name} {last_name}  Email: {alias_email}  Phone: {phone}")

    # 4. Try reverse protocol first
    use_reverse = cfg.get("reverse_engineering", True)
    result: dict[str, Any] = {"ok": False, "email": target_email}

    if use_reverse:
        result = _try_reverse_pay(
            paypal_url=paypal_url,
            card=card,
            address=address,
            first_name=first_name,
            last_name=last_name,
            alias_email=alias_email,
            password=password,
            phone=phone,
            sms_api_url=sms_api_url,
            cfg=cfg,
            proxy=proxy,
            cookie_header=data.get("cookie_header", ""),
            timeout=int(cfg.get("reverse_timeout", 60)),
        )

    # 5. Browser fallback (unless reverse_only or reverse succeeded)
    if not result.get("ok") and not reverse_only:
        if use_reverse:
            print(f"[*] Reverse protocol failed ({result.get('error', '')}), trying nodriver...")
        # 5a. Try nodriver first (undetected Chrome)
        result = _try_nodriver_pay(
            paypal_url=paypal_url,
            card=card,
            address=address,
            first_name=first_name,
            last_name=last_name,
            alias_email=alias_email,
            password=password,
            phone=phone,
            sms_api_url=sms_api_url,
            cfg=cfg,
            proxy=proxy,
        )
        # 5b. Fall back to Camoufox/CloakBrowser if nodriver fails
        if not result.get("ok"):
            print(f"[*] nodriver failed ({result.get('error', '')}), falling back to browser")
            result = _try_browser_pay(
                paypal_url=paypal_url,
                card=card,
                address=address,
                first_name=first_name,
                last_name=last_name,
                alias_email=alias_email,
                password=password,
                phone=phone,
                sms_api_url=sms_api_url,
                cfg=cfg,
                proxy=proxy,
                headless=headless,
                cookie_header=data.get("cookie_header", ""),
            )

    # 6. Save results
    if result.get("ok"):
        result.setdefault("email", target_email)
        result.setdefault("paypal_status", "completed")
        data["access_token"] = result.get("access_token", "")
        data["oauth_refresh_token"] = result.get("oauth_refresh_token", "")
        data["refresh_token_status"] = result.get("refresh_token_status", "")
        data["paypal_status"] = result.get("paypal_status", "")
        data["paypal_completed_at"] = int(time.time())
        data["success"] = True
        saved_path = _save_paypal_result(data, json_path)
        result["json_path"] = saved_path
        print(f"[*] Payment completed. Session saved: {saved_path}")
    else:
        data["paypal_status"] = result.get("error", "payment_failed").split(":")[0]
        data["success"] = False
        _save_paypal_result(data, json_path)
        print(f"[!] Payment failed: {result.get('error', '')}")

    return result


# ──────────────────────────── reverse protocol flow ────────────────────────────


def _try_reverse_pay(
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_api_url: str,
    cfg: dict,
    proxy: str | None = None,
    cookie_header: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """Attempt PayPal payment via reverse-engineered HTTP protocol."""
    from .paypal_reverse import try_reverse_pay

    sms_cfg = {
        "api_url": sms_api_url,
        "phone": phone,
        "poll_interval": int(cfg.get("sms_poll_interval", 5)),
        "timeout": int(cfg.get("sms_timeout", 120)),
        "manual_human_verification": bool(cfg.get("manual_human_verification", not use_headless)),
        "human_verification_timeout": int(cfg.get("human_verification_timeout", 300)),
    }

    print("[*] Attempting reverse protocol...")
    result = try_reverse_pay(
        redirect_url=paypal_url,
        card=card,
        address=address,
        first_name=first_name,
        last_name=last_name,
        alias_email=alias_email,
        password=password,
        phone=phone,
        sms_cfg=sms_cfg,
        proxy=proxy,
        cookie_header=cookie_header,
        timeout=timeout,
    )

    if result.get("ok"):
        result.setdefault("paypal_status", "completed")
        result.setdefault("alias_email", alias_email)
        result.setdefault("card_last4", card["number"][-4:])
        result.setdefault("password", password)
        print("[*] Reverse protocol succeeded!")
    else:
        print(f"[!] Reverse protocol failed: {result.get('error', '')}")

    return result


# ──────────────────────────── nodriver fallback flow ────────────────────────────


def _try_nodriver_pay(
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_api_url: str,
    cfg: dict,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Attempt PayPal payment via nodriver (undetected Chrome)."""
    from .nodriver_paypal import run_nodriver_pay

    sms_cfg = {
        "api_url": sms_api_url,
        "phone": phone,
        "poll_interval": int(cfg.get("sms_poll_interval", 5)),
        "timeout": int(cfg.get("sms_timeout", 120)),
    }

    # Normalize proxy
    nd_proxy = proxy
    if nd_proxy and "socks5h://" in nd_proxy:
        nd_proxy = nd_proxy.replace("socks5h://", "socks5://")

    print("[*] Attempting nodriver payment flow...")
    result = run_nodriver_pay(
        paypal_url=paypal_url,
        card=card,
        address=address,
        first_name=first_name,
        last_name=last_name,
        alias_email=alias_email,
        password=password,
        phone=phone,
        sms_cfg=sms_cfg,
        proxy=nd_proxy or "",
        timeout=180,
    )

    if result.get("ok"):
        result.setdefault("paypal_status", "completed")
        result.setdefault("alias_email", alias_email)
        result.setdefault("card_last4", card["number"][-4:])
        result.setdefault("password", password)
        print("[*] nodriver payment succeeded!")
    else:
        print(f"[!] nodriver payment failed: {result.get('error', '')}")

    return result


# ──────────────────────────── browser fallback flow ────────────────────────────


def _try_browser_pay(
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_api_url: str,
    cfg: dict,
    proxy: str | None = None,
    headless: bool = False,
    cookie_header: str = "",
) -> dict[str, Any]:
    """Attempt PayPal payment via anti-detect browser automation.

    Prefers Camoufox (anti-detection Firefox with GeoIP matching);
    falls back to CloakBrowser if Camoufox is not installed.
    """
    sms_cfg = {
        "api_url": sms_api_url,
        "phone": phone,
        "poll_interval": int(cfg.get("sms_poll_interval", 5)),
        "timeout": int(cfg.get("sms_timeout", 120)),
    }
    debug_dir = cfg.get("debug_dir", "runtime/paypal_debug")
    debug_enabled = bool(cfg.get("debug_screenshots", True))
    use_headless = headless or bool(cfg.get("headless", False))
    sms_cfg["manual_human_verification"] = bool(cfg.get("manual_human_verification", not use_headless))
    sms_cfg["human_verification_timeout"] = int(cfg.get("human_verification_timeout", 300))

    # Normalize proxy: socks5h:// -> socks5:// (browser compatibility)
    browser_proxy = proxy
    if browser_proxy and "socks5h://" in browser_proxy:
        browser_proxy = browser_proxy.replace("socks5h://", "socks5://")

    # Determine browser engine: prefer Camoufox, fall back to CloakBrowser
    browser_engine = cfg.get("browser_engine", "camoufox")
    use_camoufox = browser_engine == "camoufox"

    if use_camoufox:
        try:
            from camoufox.sync_api import Camoufox
            from browserforge.fingerprints import Screen
        except ImportError:
            print("[*] Camoufox not installed, falling back to CloakBrowser")
            use_camoufox = False

    if not use_camoufox:
        return _try_browser_pay_cloakbrowser(
            paypal_url, card, address, first_name, last_name,
            alias_email, password, sms_cfg, debug_dir, debug_enabled,
            use_headless, browser_proxy, cookie_header, cfg,
        )

    return _try_browser_pay_camoufox(
        paypal_url, card, address, first_name, last_name,
        alias_email, password, sms_cfg, debug_dir, debug_enabled,
        use_headless, browser_proxy, cookie_header, cfg,
    )


def _try_browser_pay_camoufox(
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    sms_cfg: dict,
    debug_dir: str,
    debug_enabled: bool,
    use_headless: bool,
    browser_proxy: str | None,
    cookie_header: str,
    cfg: dict,
) -> dict[str, Any]:
    """Attempt PayPal payment via Camoufox anti-detect browser."""
    import os
    import tempfile

    from browserforge.fingerprints import Screen
    from camoufox.sync_api import Camoufox

    print("[*] Starting Camoufox anti-detect browser automation...")
    result: dict[str, Any] = {"ok": False, "email": alias_email}

    # Build proxy config for Camoufox
    cf_proxy = None
    if browser_proxy:
        from urllib.parse import urlparse as _urlparse
        pp = _urlparse(browser_proxy)
        cf_proxy = {
            "server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
            "username": pp.username or "",
            "password": pp.password or "",
        }

    # Create temp profile for persistent context
    tmp_profile = tempfile.mkdtemp(prefix="paypal_camoufox_")

    # Camoufox options with anti-detection features
    camoufox_options = {
        "headless": True if use_headless else False,
        "humanize": True,
        "persistent_context": True,
        "user_data_dir": tmp_profile,
        "screen": Screen(max_width=1280, max_height=900),
        "proxy": cf_proxy,
        "geoip": bool(cfg.get("geoip", True)),
        "locale": "en-US",
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }

    step = "init"

    try:
        with Camoufox(**camoufox_options) as ctx:
            # Inject Navigator property overrides for fingerprint consistency
            _inject_navigator_overrides(ctx)

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            result = _run_browser_steps(
                page, ctx, paypal_url, card, address, first_name, last_name,
                alias_email, password, sms_cfg, debug_dir, debug_enabled,
                cookie_header, step,
            )
    except _PayPalStepError as e:
        result = {"ok": False, "error": f"step_{e.step}: {e.detail}", "failed_step": e.step}
    except Exception as e:
        result = {"ok": False, "error": f"step_{step}: {e}", "failed_step": step}
    finally:
        # Cleanup temp profile
        try:
            import shutil
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    return result


def _try_browser_pay_cloakbrowser(
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    sms_cfg: dict,
    debug_dir: str,
    debug_enabled: bool,
    use_headless: bool,
    browser_proxy: str | None,
    cookie_header: str,
    cfg: dict,
) -> dict[str, Any]:
    """Attempt PayPal payment via CloakBrowser (fallback)."""
    try:
        from cloakbrowser import launch
    except ImportError:
        return {"ok": False, "error": "browser_not_installed: pip install camoufox[geoip] browserforge or cloakbrowser"}

    print("[*] Starting CloakBrowser automation (fallback)...")
    result: dict[str, Any] = {"ok": False, "email": alias_email}

    browser = launch(
        headless=use_headless,
        proxy=browser_proxy,
        humanize=True,
        timezone="America/New_York",
        locale="en-US",
    )
    ctx = browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )

    page = ctx.new_page()
    step = "init"

    try:
        result = _run_browser_steps(
            page, ctx, paypal_url, card, address, first_name, last_name,
            alias_email, password, sms_cfg, debug_dir, debug_enabled,
            cookie_header, step,
        )
    except _PayPalStepError as e:
        _screenshot(page, debug_dir, f"error_{e.step}", debug_enabled)
        result = {
            "ok": False,
            "error": f"step_{e.step}: {e.detail}",
            "failed_step": e.step,
        }
    except Exception as e:
        _screenshot(page, debug_dir, f"error_{step}", debug_enabled)
        result = {
            "ok": False,
            "error": f"step_{step}: {e}",
            "failed_step": step,
        }
    finally:
        browser.close()

    return result


# ──────────────────────────── shared browser helpers ────────────────────────────


def _inject_navigator_overrides(ctx) -> None:
    """Inject Navigator property overrides for fingerprint consistency.

    Ensures navigator.language and navigator.languages match the expected
    locale, even if the browser's default differs from the proxy's GeoIP.
    """
    script = """
(() => {
  const language = 'en-US';
  const languages = ['en-US', 'en'];
  const define = (object, property, value) => {
    try {
      Object.defineProperty(object, property, {
        get: () => value,
        configurable: true,
      });
    } catch (_) {}
  };
  define(Navigator.prototype, 'language', language);
  define(Navigator.prototype, 'languages', languages);
})();
"""
    try:
        ctx.add_init_script(script)
    except Exception as e:
        print(f"[*] Navigator override injection failed (non-fatal): {e}")


def _run_browser_steps(
    page,
    ctx,
    paypal_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    sms_cfg: dict,
    debug_dir: str,
    debug_enabled: bool,
    cookie_header: str,
    initial_step: str = "init",
) -> dict[str, Any]:
    """Run the shared PayPal browser automation steps.

    Used by both Camoufox and CloakBrowser paths.
    """
    step = initial_step
    result: dict[str, Any] = {"ok": False, "email": alias_email}

    baseline = _sms_baseline(sms_cfg["api_url"])

    if cookie_header:
        _safe_import_cookie_header(ctx, cookie_header)

    step = "navigate"
    _screenshot(page, debug_dir, "01_navigate_before", debug_enabled)
    page.goto(paypal_url, wait_until="domcontentloaded", timeout=60000)
    _prepare_openai_checkout_paypal(
        page,
        address=address,
        first_name=first_name,
        last_name=last_name,
        phone=sms_cfg.get("phone", ""),
        debug_dir=debug_dir,
        debug_enabled=debug_enabled,
    )
    _wait_for_paypal_load(page)
    _screenshot(page, debug_dir, "02_paypal_loaded", debug_enabled)
    _handle_human_verification_gate(page, sms_cfg, debug_dir, debug_enabled, "human_verification")

    step = "create_account"
    _click_create_account(page)
    _screenshot(page, debug_dir, "03_create_account", debug_enabled)
    _handle_human_verification_gate(page, sms_cfg, debug_dir, debug_enabled, "human_verification")

    step = "country"
    _ensure_country_us(page)
    _screenshot(page, debug_dir, "03_country_us", debug_enabled)
    _handle_human_verification_gate(page, sms_cfg, debug_dir, debug_enabled, "human_verification")

    step = "fill_email"
    _fill_signup_email(page, alias_email)
    _screenshot(page, debug_dir, "04_email_filled", debug_enabled)

    step = "fill_name"
    _fill_signup_name(page, first_name, last_name)
    _screenshot(page, debug_dir, "05_name_filled", debug_enabled)

    step = "phone"
    _fill_phone_if_present(page, sms_cfg["phone"])
    _screenshot(page, debug_dir, "06_phone_filled", debug_enabled)

    step = "password"
    _fill_password(page, password)
    _screenshot(page, debug_dir, "07_password_filled", debug_enabled)

    step = "card"
    _fill_card(page, card)
    _screenshot(page, debug_dir, "08_card_filled", debug_enabled)

    step = "address"
    billing_address = {**address, "first_name": first_name, "last_name": last_name}
    _fill_billing_address(page, billing_address)
    _screenshot(page, debug_dir, "09_address_filled", debug_enabled)

    step = "verify_fields"
    _verify_checkout_fields(page)

    step = "terms"
    _accept_terms(page)
    _screenshot(page, debug_dir, "10_terms_accepted", debug_enabled)

    step = "sms_verify"
    code = _handle_sms_verification(page, sms_cfg, baseline)
    _screenshot(page, debug_dir, "11_sms_verified", debug_enabled)

    step = "submit"
    _submit_payment(page)
    _screenshot(page, debug_dir, "12_payment_submitted", debug_enabled)

    step = "wait_redirect"
    _wait_for_stripe_redirect(page, timeout=60)
    _screenshot(page, debug_dir, "13_redirect_done", debug_enabled)

    step = "refresh_session"
    auth_body = _poll_auth_session(ctx, timeout=120)
    if not auth_body:
        raise _PayPalStepError("refresh_session", "auth_session_poll_timeout")

    new_access = _session_token(auth_body, "accessToken", "access_token")
    new_refresh = _session_token(auth_body, "refreshToken", "refresh_token")

    if not new_access:
        raise _PayPalStepError("refresh_session", "no_access_token_in_response")

    result = {
        "ok": True,
        "access_token": new_access,
        "oauth_refresh_token": new_refresh,
        "refresh_token_status": "oauth_present" if new_refresh else "no_rt",
        "paypal_status": "completed",
        "paypal_completed_at": int(time.time()),
        "card_last4": card["number"][-4:],
        "password": password,
        "alias_email": alias_email,
    }

    return result


# ──────────────────────────── seed loading ────────────────────────────



# Account seed loading and access-token extraction live in sms_tool.account_seed.


_STATE_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def _pick_card_and_address(cfg: dict) -> tuple[dict, dict]:
    cards = cfg.get("cards") or []
    addresses = cfg.get("addresses") or []
    if not cards or not addresses:
        raise RuntimeError("paypal_auto.cards and paypal_auto.addresses must be configured")

    index_file = cfg.get("card_index_file", "runtime/paypal_card_index.txt")
    idx = _read_index(index_file)
    card = cards[idx % len(cards)]
    addr = addresses[idx % len(addresses)]
    _write_index(index_file, idx + 1)

    state = addr.get("state", "")
    if len(state) > 2:
        state = _STATE_ABBREV.get(state.lower(), state[:2].upper())

    return card, {
        "line1": addr.get("line1", ""),
        "city": addr.get("city", ""),
        "state": state,
        "postal_code": addr.get("postal_code", ""),
    }


def _read_index(path: str) -> int:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_index(path: str, value: int):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(value), encoding="utf-8")


# ──────────────────────────── phone / sms selection ────────────────────────────


def _pick_phone_and_sms(cfg: dict) -> tuple[str, str]:
    """Pick a phone number and SMS API URL via round-robin.

    Supports two config formats:
      - New:  "phone_numbers": [{"phone": "...", "sms_api_url": "..."}, ...]
      - Legacy fallback: single "phone_number" + "sms_api_url"
    """
    phone_list = cfg.get("phone_numbers") or []
    if phone_list:
        index_file = cfg.get("phone_index_file", "runtime/paypal_phone_index.txt")
        idx = _read_index(index_file)
        entry = phone_list[idx % len(phone_list)]
        _write_index(index_file, idx + 1)
        return entry["phone"], entry["sms_api_url"]

    # Legacy fallback
    return cfg.get("phone_number", ""), cfg.get("sms_api_url", "")


# ──────────────────────────── SMS polling (shared) ────────────────────────────


def _sms_baseline(api_url: str) -> dict:
    """Record the current SMS state as baseline before starting."""
    result = {"raw": "", "timestamp": 0}
    try:
        r = _requests.get(api_url, timeout=10)
        if r.status_code == 200:
            result["raw"] = r.text.strip()
            result["timestamp"] = time.time()
    except Exception:
        pass
    return result


def _extract_sms_code(text: str) -> str | None:
    """Extract verification code from SMS text, avoiding false positives."""
    if not text:
        return None

    keyword_patterns = [
        re.compile(r"(?:code|otp|verification|verify)[:\s]+(\d{4,6})", re.IGNORECASE),
        re.compile(r"(?:is|:)\s*(\d{4,6})\s*(?:for|to|\.|$)", re.IGNORECASE),
    ]
    for pattern in keyword_patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)

    standalone_pattern = re.compile(r"(?<![0-9-])(?<!20[0-9]{2})(\d{4,6})(?![0-9-])")
    match = standalone_pattern.search(text)
    if match:
        code = match.group(1)
        if 2000 <= int(code) <= 2099 and len(code) == 4:
            return None
        return code

    return None


def _poll_sms_code(api_url: str, baseline: dict, timeout: int = 120, poll_interval: int = 5) -> str | None:
    """Poll SMS API for a new verification code."""
    deadline = time.time() + timeout
    baseline_raw = baseline.get("raw", "")
    attempt = 0

    print(f"[*] Polling SMS (timeout={timeout}s, interval={poll_interval}s)...")

    while time.time() < deadline:
        attempt += 1
        try:
            r = _requests.get(api_url, timeout=10)
            if r.status_code == 200:
                text = r.text.strip()

                if text and text != baseline_raw:
                    code = _extract_sms_code(text)
                    if code:
                        print(f"\n[*] SMS code received (content change): {code}")
                        return code

                if text:
                    code = _extract_sms_code(text)
                    if code and attempt > 2:
                        if not hasattr(_poll_sms_code, '_last_seen') or _poll_sms_code._last_seen != text:
                            _poll_sms_code._last_seen = text
                            print(f"\n[*] SMS code received (new message): {code}")
                            return code

        except Exception as e:
            print(f"[sms poll error: {e}]")

        remaining = int(deadline - time.time())
        print(f". [{attempt}/{timeout//poll_interval}]", end="", flush=True)
        time.sleep(poll_interval)

    print(f"\n[!] SMS poll timeout after {timeout}s")
    return None


# ──────────────────────────── Browser helpers ────────────────────────────


def _wait_for_paypal_load(page, timeout: int = 30000):
    """Wait for PayPal page to finish loading."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    time.sleep(2)


def _prepare_openai_checkout_paypal(
    page,
    *,
    address: dict,
    first_name: str,
    last_name: str,
    phone: str,
    debug_dir: str,
    debug_enabled: bool,
) -> bool:
    """Handle ChatGPT checkout links before continuing on PayPal."""
    if not _is_openai_checkout_url(page.url):
        return False

    print("[*] OpenAI checkout link detected; selecting PayPal in browser")
    deadline = time.time() + 45
    selected = False
    submitted = False
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        if _is_paypal_url(page.url):
            return True

        _fill_openai_checkout_billing(page, address, first_name, last_name, phone)
        if _select_openai_checkout_paypal(page):
            selected = True
            _screenshot(page, debug_dir, "02a_openai_paypal_selected", debug_enabled)
            time.sleep(1)

        if selected and _click_openai_checkout_continue(page):
            submitted = True
            _screenshot(page, debug_dir, "02b_openai_checkout_continue", debug_enabled)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            if _is_paypal_url(page.url):
                return True

        if submitted and not _is_openai_checkout_url(page.url):
            return _is_paypal_url(page.url)
        time.sleep(1)

    return _is_paypal_url(page.url)


def _is_openai_checkout_url(url: str) -> bool:
    value = str(url or "").lower()
    return (
        "chatgpt.com/checkout/" in value
        or "pay.openai.com" in value
        or "checkout.stripe.com/c/pay/" in value
    )


def _is_paypal_url(url: str) -> bool:
    return "paypal.com" in str(url or "").lower()


def _select_openai_checkout_paypal(page) -> bool:
    selectors = [
        '[data-testid="paypal-accordion-item"]',
        '#payment-method-accordion-item-title-paypal',
        'button[data-testid="paypal-accordion-item-button"]',
        'button[aria-label*="PayPal" i]',
        'label:has-text("PayPal")',
        '[role="radio"]:has-text("PayPal")',
        '[role="button"]:has-text("PayPal")',
        'text="PayPal"',
    ]
    return _click_with_fallback(page, selectors, timeout=5000)


def _click_openai_checkout_continue(page) -> bool:
    selectors = [
        'button:has-text("Continue")',
        'button:has-text("Subscribe")',
        'button:has-text("Start free trial")',
        'button:has-text("Start trial")',
        'button:has-text("Pay")',
        'button[type="submit"]',
        '[data-testid="hosted-payment-submit-button"]',
    ]
    return _click_with_fallback(page, selectors, timeout=8000)


def _fill_openai_checkout_billing(page, address: dict, first_name: str, last_name: str, phone: str) -> None:
    full_name = f"{first_name} {last_name}".strip()
    country = str(address.get("country") or "US")
    line1 = str(address.get("line1") or "")
    city = str(address.get("city") or "")
    state = str(address.get("state") or "")
    postal_code = str(address.get("postal_code") or address.get("zip") or "")
    fields = [
        (["#billingName", 'input[name="billingName"]', 'input[autocomplete="billing name"]'], full_name),
        (["#billingAddressLine1", 'input[name="billingAddressLine1"]', 'input[autocomplete="billing address-line1"]'], line1),
        (["#billingLocality", 'input[name="billingLocality"]', 'input[autocomplete="billing address-level2"]'], city),
        (["#billingAdministrativeArea", 'input[name="billingAdministrativeArea"]', 'input[autocomplete="billing address-level1"]'], state),
        (["#billingPostalCode", 'input[name="billingPostalCode"]', 'input[autocomplete="billing postal-code"]'], postal_code),
        (["#phoneNumber", 'input[name="phoneNumber"]', 'input[type="tel"]'], phone),
    ]
    for selectors, value in fields:
        if value:
            _fill_visible_input(page, selectors, value, timeout=1500) or _fill_with_fallback(page, selectors, value, timeout=1500)
    if country:
        _select_with_fallback(
            page,
            ["#billingCountry", 'select[name="billingCountry"]', 'select[autocomplete="billing country"]'],
            country,
            labels=["United States", "United States of America"],
            timeout=1500,
        )


def _is_human_verification_page(page) -> bool:
    """Detect PayPal's visible human-verification challenge page."""
    selectors = [
        'text="Confirm you\'re human"',
        'text="Confirm you are human"',
        'text="Please enable JS and disable any ad blocker"',
        'text="Move the slider all the way to the right"',
        'iframe[src*="captcha"]',
        'iframe[title*="captcha" i]',
    ]
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    try:
        text = str(page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        text = ""
    return (
        "confirm you're human" in text
        or "confirm you are human" in text
        or "move the slider all the way to the right" in text
        or "please enable js and disable any ad blocker" in text
    )


def _handle_human_verification_gate(page, sms_cfg: dict, debug_dir: str, debug_enabled: bool, step: str) -> None:
    """Pause for manual PayPal human verification when allowed."""
    if not _is_human_verification_page(page):
        return

    _screenshot(page, debug_dir, "human_verification_required", debug_enabled)
    allow_manual = bool(sms_cfg.get("manual_human_verification", False))
    timeout = int(sms_cfg.get("human_verification_timeout", 300) or 300)
    if not allow_manual:
        raise _PayPalStepError(step, "paypal_human_verification_required")

    print(f"[*] PayPal human verification required; waiting up to {timeout}s for manual completion...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_human_verification_page(page):
            print("[*] PayPal human verification cleared")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            return
        time.sleep(2)

    raise _PayPalStepError(step, "paypal_human_verification_required")


def _click_with_fallback(page, selectors: list[str], timeout: int = 8000):
    """Try multiple selectors, click the first one found."""
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=3000):
                el.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _fill_with_fallback(page, selectors: list[str], value: str, timeout: int = 8000) -> bool:
    """Try multiple selectors, fill the first one found."""
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=3000):
                _set_field_value(el, value, timeout=timeout)
                if not value or _locator_has_value(el, value):
                    return True
        except Exception:
            continue
    return False


def _set_field_value(locator, value: str, timeout: int = 8000):
    """Set an input value and fire the DOM events PayPal/Stripe listen for."""
    locator.scroll_into_view_if_needed(timeout=timeout)
    locator.click(timeout=timeout)
    try:
        locator.fill(value, timeout=timeout)
    except Exception:
        locator.evaluate(
            """(el, value) => {
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                setter.call(el, value);
            }""",
            value,
        )
    if value and not _locator_has_value(locator, value):
        try:
            locator.press("Control+A", timeout=timeout)
            locator.type(value, timeout=timeout, delay=random.randint(20, 60))
        except Exception:
            pass
    for event_name in ("input", "change", "blur"):
        try:
            locator.dispatch_event(event_name)
        except Exception:
            pass


def _fill_dom_id(page, element_id: str, value: str) -> bool:
    """Fill PayPal checkoutweb controls by their stable DOM id."""
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        try:
            actual = scope.evaluate(
                """({ id, value }) => {
                    const el = document.getElementById(id);
                    if (!el) return null;
                    el.scrollIntoView({ block: "center", inline: "nearest" });
                    const proto =
                        el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype :
                        el instanceof HTMLSelectElement ? HTMLSelectElement.prototype :
                        HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, "value");
                    if (desc && desc.set) {
                        desc.set.call(el, value);
                    } else {
                        el.value = value;
                    }
                    const events = [
                        new FocusEvent("focus", { bubbles: true }),
                        new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }),
                        new Event("change", { bubbles: true }),
                        new KeyboardEvent("keyup", { bubbles: true }),
                        new FocusEvent("blur", { bubbles: true }),
                        new FocusEvent("focusout", { bubbles: true }),
                    ];
                    for (const event of events) el.dispatchEvent(event);
                    return String(el.value || "");
                }""",
                {"id": element_id, "value": value},
            )
            if actual is not None and _value_matches(actual, value):
                return True
        except Exception:
            continue
    return False


def _fill_dom_ids(page, element_ids: list[str], value: str) -> bool:
    for element_id in element_ids:
        if _fill_dom_id(page, element_id, value):
            return True
    return False


def _field_has_any_value(page, element_ids: list[str]) -> bool:
    for element_id in element_ids:
        for scope in [page, *getattr(page, "frames", [])]:
            try:
                value = scope.evaluate(
                    """(id) => {
                        const el = document.getElementById(id);
                        return el ? String(el.value || "").trim() : null;
                    }""",
                    element_id,
                )
                if value:
                    return True
            except Exception:
                continue
    return False


def _visible_field_has_value(page, selectors: list[str], expected: str = "") -> bool:
    for scope in [page, *getattr(page, "frames", [])]:
        for selector in selectors:
            try:
                el = scope.locator(selector).first
                if not el.is_visible(timeout=1000):
                    continue
                value = el.input_value(timeout=1000)
                if expected:
                    if _value_matches(value, expected):
                        return True
                elif str(value or "").strip():
                    return True
            except Exception:
                continue
    return False


def _locator_has_value(locator, expected: str) -> bool:
    """Return True when a text-like control visibly kept the expected value."""
    try:
        actual = locator.input_value(timeout=1000)
    except Exception:
        return True
    return _value_matches(actual, expected)


def _value_matches(actual: str, expected: str) -> bool:
    actual_s = str(actual or "").strip()
    expected_s = str(expected or "").strip()
    if not expected_s:
        return True
    if actual_s == expected_s:
        return True
    actual_digits = re.sub(r"\D+", "", actual_s)
    expected_digits = re.sub(r"\D+", "", expected_s)
    if expected_digits and actual_digits.endswith(expected_digits):
        return True
    return expected_s.lower() in actual_s.lower()


def _fill_by_label_fallback(page, labels: list[str], value: str, timeout: int = 8000) -> bool:
    """Fill PayPal fields whose visible floating label is more stable than CSS attrs."""
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        for label in labels:
            for getter in ("get_by_label", "get_by_placeholder"):
                try:
                    el = getattr(scope, getter)(label, exact=False).first
                    if el.is_visible(timeout=1500):
                        _set_field_value(el, value, timeout=timeout)
                        if not value or _locator_has_value(el, value):
                            return True
                except Exception:
                    continue
        try:
            actual = scope.evaluate(
                """({ labels, value }) => {
                    const wanted = labels.map((v) => String(v || "").toLowerCase()).filter(Boolean);
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none" &&
                            rect.width > 0 && rect.height > 0 && !el.disabled && !el.readOnly;
                    };
                    const textAround = (el) => {
                        const parts = [];
                        for (const attr of ["id", "name", "placeholder", "aria-label", "autocomplete", "data-testid"]) {
                            parts.push(el.getAttribute(attr) || "");
                        }
                        let node = el;
                        for (let i = 0; i < 4 && node; i += 1, node = node.parentElement) {
                            parts.push(node.innerText || "");
                        }
                        const id = el.getAttribute("id");
                        if (id) {
                            const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                            if (label) parts.push(label.innerText || "");
                        }
                        return parts.join(" ").toLowerCase();
                    };
                    const score = (el) => {
                        const haystack = textAround(el);
                        let best = 0;
                        for (const label of wanted) {
                            if (haystack.includes(label)) best = Math.max(best, 40);
                        }
                        const autocomplete = String(el.getAttribute("autocomplete") || "").toLowerCase();
                        const type = String(el.getAttribute("type") || "").toLowerCase();
                        if (wanted.some((v) => v.includes("email")) && type === "email") best = Math.max(best, 90);
                        if (wanted.some((v) => v.includes("phone")) && type === "tel") best = Math.max(best, 90);
                        if (wanted.some((v) => v.includes("card number")) && autocomplete === "cc-number") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("expiration")) && autocomplete === "cc-exp") best = Math.max(best, 100);
                        if (wanted.some((v) => v === "cvv" || v === "cvc") && autocomplete === "cc-csc") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("first name")) && autocomplete === "given-name") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("last name")) && autocomplete === "family-name") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("street")) && autocomplete === "address-line1") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("city")) && autocomplete === "address-level2") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("zip")) && autocomplete === "postal-code") best = Math.max(best, 100);
                        return best;
                    };
                    const inputs = Array.from(document.querySelectorAll("input, textarea"))
                        .filter(visible)
                        .map((el) => ({ el, score: score(el) }))
                        .filter((item) => item.score > 0)
                        .sort((a, b) => b.score - a.score);
                    if (!inputs.length) return null;
                    const el = inputs[0].el;
                    el.scrollIntoView({ block: "center", inline: "nearest" });
                    const proto = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                    setter.call(el, value);
                    for (const name of ["input", "change", "keyup", "blur", "focusout"]) {
                        el.dispatchEvent(new Event(name, { bubbles: true }));
                    }
                    return String(el.value || "");
                }""",
                {"labels": labels, "value": value},
            )
            if actual is not None and _value_matches(actual, value):
                return True
        except Exception:
            continue
    return False


def _fill_by_visible_label_text(page, label: str, value: str) -> bool:
    """Fill a control by an exact visible floating-label text node."""
    for scope in [page, *getattr(page, "frames", [])]:
        try:
            actual = scope.evaluate(
                """({ label, value }) => {
                    const wanted = String(label || "").trim().toLowerCase();
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none" &&
                            rect.width > 0 && rect.height > 0 && !el.disabled && !el.readOnly;
                    };
                    const setValue = (el) => {
                        el.scrollIntoView({ block: "center", inline: "nearest" });
                        const proto = el instanceof HTMLTextAreaElement
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                        if (setter) setter.call(el, value);
                        else el.value = value;
                        for (const name of ["focus", "input", "change", "keyup", "blur", "focusout"]) {
                            el.dispatchEvent(new Event(name, { bubbles: true }));
                        }
                        return String(el.value || "");
                    };
                    const labelEls = Array.from(document.querySelectorAll("label, span, div, p"))
                        .filter((el) => visible(el) && String(el.textContent || "").trim().toLowerCase() === wanted);
                    for (const labelEl of labelEls) {
                        let node = labelEl;
                        for (let depth = 0; depth < 6 && node; depth += 1, node = node.parentElement) {
                            const inputs = Array.from(node.querySelectorAll("input, textarea")).filter(visible);
                            if (inputs.length === 1) return setValue(inputs[0]);
                            if (inputs.length > 1) {
                                const lr = labelEl.getBoundingClientRect();
                                const lx = (lr.left + lr.right) / 2;
                                const ly = (lr.top + lr.bottom) / 2;
                                const containing = inputs
                                    .map((input) => ({ input, rect: input.getBoundingClientRect() }))
                                    .filter(({ rect }) => lx >= rect.left && lx <= rect.right && ly >= rect.top && ly <= rect.bottom)
                                    .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
                                if (containing.length) return setValue(containing[0].input);
                                const below = inputs
                                    .map((input) => {
                                        const r = input.getBoundingClientRect();
                                        const dx = Math.abs((r.left + r.right) / 2 - lx);
                                        const dy = Math.max(0, r.top - lr.top);
                                        return { input, distance: dx + dy, rect: r };
                                    })
                                    .filter((item) => item.rect.bottom >= lr.top - 4)
                                    .sort((a, b) => a.distance - b.distance);
                                if (below.length) return setValue(below[0].input);
                                const sorted = inputs
                                    .map((input) => {
                                        const r = input.getBoundingClientRect();
                                        const dx = Math.abs((r.left + r.right) / 2 - lx);
                                        const dy = Math.abs((r.top + r.bottom) / 2 - ly);
                                        return { input, distance: dx + dy };
                                    })
                                    .sort((a, b) => a.distance - b.distance);
                                return setValue(sorted[0].input);
                            }
                        }
                    }
                    return null;
                }""",
                {"label": label, "value": value},
            )
            if actual is not None and _value_matches(actual, value):
                return True
        except Exception:
            continue
    return False


def _fill_visible_input(page, selectors: list[str], value: str, timeout: int = 8000) -> bool:
    """Click and type into a visible input, then verify its value."""
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        for selector in selectors:
            try:
                el = scope.locator(selector).first
                if not el.is_visible(timeout=1500):
                    continue
                el.scroll_into_view_if_needed(timeout=timeout)
                el.click(timeout=timeout)
                try:
                    el.press("Control+A", timeout=timeout)
                except Exception:
                    pass
                el.type(value, timeout=timeout, delay=random.randint(25, 70))
                for event_name in ("input", "change", "blur", "focusout"):
                    try:
                        el.dispatch_event(event_name)
                    except Exception:
                        pass
                if _locator_has_value(el, value):
                    return True
            except Exception:
                continue
    return False


def _dismiss_overlays(page):
    """Dismiss autocomplete/cookie overlays that can steal focus from the next field."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for selector in [
        'button:has-text("Close")',
        'button:has-text("Accept")',
        '[aria-label="Close"]',
        '.AddressAutocomplete-results',
    ]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=500):
                if "AddressAutocomplete-results" in selector:
                    continue
                el.click(timeout=1000)
                time.sleep(0.3)
        except Exception:
            continue


def _wait_for_checkout_form_after_email(page, timeout: int = 12000) -> bool:
    """Wait until the full PayPal checkoutweb form appears after the email gate."""
    deadline = time.time() + (timeout / 1000)
    selectors = [
        '#cardNumber',
        'input[id="cardNumber"]',
        'input[autocomplete="cc-number"]',
        'text="Pay with debit or credit card"',
        'text="Billing address"',
    ]
    while time.time() < deadline:
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        time.sleep(0.5)
    return False


def _select_with_fallback(page, selectors: list[str], value: str, labels: list[str] | None = None, timeout: int = 8000) -> bool:
    """Select an option by value/text and dispatch change events."""
    labels = labels or []
    wanted = [value, *labels]
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if not el.is_visible(timeout=3000):
                continue
            for option in wanted:
                try:
                    el.select_option(value=option, timeout=timeout)
                    el.dispatch_event("change")
                    return True
                except Exception:
                    pass
            matched = el.evaluate(
                """(select, wanted) => {
                    const lower = wanted.map((v) => String(v || "").toLowerCase()).filter(Boolean);
                    for (const option of select.options || []) {
                        const value = String(option.value || "").toLowerCase();
                        const text = String(option.textContent || "").toLowerCase();
                        if (lower.some((item) => value === item || text === item || text.includes(item))) {
                            select.value = option.value;
                            select.dispatchEvent(new Event("input", { bubbles: true }));
                            select.dispatchEvent(new Event("change", { bubbles: true }));
                            select.dispatchEvent(new Event("blur", { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                }""",
                wanted,
            )
            if matched:
                return True
        except Exception:
            continue
    return False


def _ensure_country_us(page):
    """Set the country/region selector to United States when present."""
    selectors = [
        'select[id="country"]',
        'select[name="country"]',
        'select[autocomplete="country"]',
        'select[aria-label*="Country"]',
        'select[aria-label*="region"]',
        '#country',
    ]
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if not el.is_visible(timeout=1000):
                continue
            current = el.evaluate(
                """(select) => {
                    const value = String(select.value || "").toLowerCase();
                    const text = String(select.options?.[select.selectedIndex]?.textContent || "").toLowerCase();
                    return { value, text };
                }"""
            )
            if current and (current.get("value") in ("us", "usa", "united states") or "united states" in current.get("text", "")):
                return False
        except Exception:
            continue
    if _select_with_fallback(page, selectors, "US", labels=["United States", "United States of America"], timeout=5000):
        print("[*] Country/region set to US")
        time.sleep(2)
        return True
    return False


def _type_human(page, selector: str, text: str, delay_range: tuple = (50, 150)):
    """Type text with human-like delays."""
    el = page.locator(selector).first
    el.click()
    for char in text:
        el.type(char, delay=random.randint(*delay_range))
        time.sleep(random.uniform(0.02, 0.08))


def _screenshot(page, debug_dir: str, name: str, enabled: bool = True):
    if not enabled:
        return
    try:
        p = Path(debug_dir)
        p.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(p / f"{name}.png"), full_page=True)
    except Exception:
        pass


# ──────────────────────────── PayPal page steps ────────────────────────────


def _click_create_account(page):
    """Click 'Create an account' link on PayPal login page."""
    selectors = [
        'text="Create an account"',
        'text="Sign Up"',
        'a:has-text("Create")',
        'a:has-text("Sign up")',
        '[data-testid="signup-link"]',
        'a[href*="signup"]',
        'button:has-text("Create")',
    ]
    card_selectors = [
        'text="Pay with Debit or Credit Card"',
        'text="Pay by Debit or Credit Card"',
        'button:has-text("Debit or Credit")',
        '[data-testid="guest-checkout-button"]',
    ]
    if _click_with_fallback(page, card_selectors, timeout=5000):
        print("[*] Clicked 'Pay with Debit or Credit Card'")
        time.sleep(2)
        return
    if _click_with_fallback(page, selectors, timeout=8000):
        print("[*] Clicked 'Create an account'")
        time.sleep(2)
        return
    print("[*] No create-account button found, assuming already on form")


def _fill_signup_email(page, email: str):
    """Fill email on PayPal signup or guest checkout form."""
    _ensure_country_us(page)
    selectors = [
        'input[id="email"]',
        '#email',
        'input[name="email"]',
        'input[id*="email" i]',
        'input[name*="email" i]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[aria-label*="email" i]',
        'input[placeholder*="email" i]',
        'input[data-testid="email-input"]',
        '[data-testid*="email" i] input',
    ]
    if _fill_dom_ids(page, ["email"], email) or _fill_visible_input(page, selectors, email) or _fill_by_label_fallback(page, ["Email", "Email address"], email) or _fill_with_fallback(page, selectors, email):
        print(f"[*] Email filled: {email}")
        time.sleep(1)
        if not _visible_field_has_value(page, selectors, email):
            raise _PayPalStepError("fill_email", "email field stayed blank after fill")
        # PayPal checkoutweb keeps all fields on one form; avoid the final submit button here.
        if _click_with_fallback(page, [
            'button:has-text("Next")',
            'button:has-text("Continue")',
        ], timeout=3000):
            time.sleep(2)
            if not _wait_for_checkout_form_after_email(page):
                raise _PayPalStepError("fill_email", "checkout form did not appear after email continue")
            if not _visible_field_has_value(page, selectors, email):
                _fill_visible_input(page, selectors, email, timeout=5000)
        return
    raise _PayPalStepError("fill_email", "email field not found")


def _fill_signup_name(page, first_name: str, last_name: str):
    """Fill name fields on PayPal signup."""
    first_selectors = [
        'input[id="first-name"]', 'input[name="firstName"]',
        'input[name="first_name"]', 'input[autocomplete="given-name"]',
        'input[placeholder*="First name" i]', 'input[aria-label*="First name" i]',
        '#firstName', '#first-name',
    ]
    first_ok = _fill_dom_ids(page, ["firstName", "first-name"], first_name) or _fill_by_visible_label_text(page, "First name", first_name) or _fill_visible_input(page, first_selectors, first_name) or _fill_by_label_fallback(page, ["First name", "Given name"], first_name) or _fill_with_fallback(page, first_selectors, first_name)
    time.sleep(random.uniform(0.3, 0.8))

    last_selectors = [
        'input[id="last-name"]', 'input[name="lastName"]',
        'input[name="last_name"]', 'input[autocomplete="family-name"]',
        'input[placeholder*="Last name" i]', 'input[aria-label*="Last name" i]',
        '#lastName', '#last-name',
    ]
    last_ok = _fill_dom_ids(page, ["lastName", "last-name"], last_name) or _fill_by_visible_label_text(page, "Last name", last_name) or _fill_visible_input(page, last_selectors, last_name) or _fill_by_label_fallback(page, ["Last name", "Family name", "Surname"], last_name) or _fill_with_fallback(page, last_selectors, last_name)
    time.sleep(random.uniform(0.3, 0.8))
    if not first_ok:
        print("[!] First name field not filled")
    if not last_ok:
        print("[!] Last name field not filled")
    if first_ok and last_ok:
        print(f"[*] Name filled: {first_name} {last_name}")


def _fill_phone_if_present(page, phone: str):
    """Fill phone number if the field is visible."""
    selectors = [
        'input[id="phone"]', 'input[name="phone"]',
        'input[type="tel"]', 'input[autocomplete="tel"]',
        '#phoneNumber', '#phone',
    ]
    phone_value = re.sub(r"\D+", "", phone or "")
    if phone_value.startswith("1") and len(phone_value) > 10:
        phone_value = phone_value[1:]
    if _fill_dom_ids(page, ["phone", "phoneNumber"], phone_value) or _fill_visible_input(page, selectors, phone_value, timeout=3000) or _fill_by_label_fallback(page, ["Phone number", "Mobile number", "Phone"], phone_value, timeout=3000) or _fill_with_fallback(page, selectors, phone_value, timeout=3000):
        print(f"[*] Phone filled: {phone_value}")
        time.sleep(1)
        _click_with_fallback(page, [
            'button:has-text("Send Code")',
            'button:has-text("Send")',
            'button:has-text("Get Code")',
        ], timeout=3000)
        time.sleep(2)


def _fill_password(page, password: str):
    """Fill password fields on PayPal signup."""
    selectors = [
        'input[id="password"]', 'input[name="password"]',
        'input[type="password"]', '#createPassword', '#password',
    ]
    if _fill_dom_ids(page, ["password", "createPassword"], password) or _fill_visible_input(page, selectors, password) or _fill_by_label_fallback(page, ["Create password", "Password"], password) or _fill_with_fallback(page, selectors, password):
        print(f"[*] Password filled")
        time.sleep(random.uniform(0.3, 0.8))
        confirm_selectors = [
            'input[id="confirm-password"]', 'input[name="confirmPassword"]',
            '#confirmPassword', '#confirm-password',
        ]
        _fill_with_fallback(page, confirm_selectors, password, timeout=3000)
        time.sleep(1)


def _fill_card(page, card: dict):
    """Fill card number, expiry, CVV."""
    number = card.get("number", "")
    exp_month = card.get("exp_month", "")
    exp_year = card.get("exp_year", "")
    cvv = card.get("cvv", "")

    card_selectors = [
        'input[name="cardNumber"]', 'input[id="cardNumber"]',
        'input[autocomplete="cc-number"]', 'input[name="card_number"]',
        'input[placeholder*="Card"]', 'input[placeholder*="card"]',
        '#card-number', 'input[data-testid="card-number-input"]',
    ]
    if not (_fill_dom_ids(page, ["cardNumber"], number) or _fill_visible_input(page, card_selectors, number, timeout=10000) or _fill_by_label_fallback(page, ["Card number", "Credit or debit card number"], number, timeout=10000) or _fill_with_fallback(page, card_selectors, number, timeout=10000)):
        try:
            for frame in page.frames:
                for sel in card_selectors:
                    try:
                        el = frame.locator(sel).first
                        if el.is_visible(timeout=2000):
                            el.click()
                            el.fill(number)
                            print(f"[*] Card number filled (iframe)")
                            break
                    except Exception:
                        continue
        except Exception:
            pass
    else:
        print(f"[*] Card number filled: ****{number[-4:]}")
    time.sleep(random.uniform(0.5, 1.0))

    month_selectors = [
        'select[name*="month"]', 'select[id*="month"]',
        'select[autocomplete="cc-exp-month"]', '#expiration-month',
    ]
    if not _fill_with_fallback(page, month_selectors, "", timeout=3000):
        exp_selectors = [
            'input[name="expirationDate"]', 'input[name*="exp"]',
            'input[autocomplete="cc-exp"]', 'input[placeholder*="MM"]',
            '#expiration-date', '#expiry',
        ]
        exp_str = f"{exp_month}/{exp_year[-2:]}"
        _fill_dom_ids(page, ["cardExpiry"], exp_str) or _fill_visible_input(page, exp_selectors, exp_str, timeout=5000) or _fill_by_label_fallback(page, ["Expiration date", "Expiry date", "MM/YY"], exp_str, timeout=5000) or _fill_with_fallback(page, exp_selectors, exp_str, timeout=5000)
    else:
        try:
            page.locator(month_selectors[0]).first.select_option(value=exp_month)
        except Exception:
            pass
    time.sleep(random.uniform(0.3, 0.8))

    year_selectors = [
        'select[name*="year"]', 'select[id*="year"]',
        'select[autocomplete="cc-exp-year"]', '#expiration-year',
    ]
    try:
        page.locator(year_selectors[0]).first.select_option(value=exp_year)
    except Exception:
        pass
    time.sleep(random.uniform(0.3, 0.8))

    cvv_selectors = [
        'input[name="cvv"]', 'input[name="cvc"]', 'input[name="cvvNumber"]',
        'input[autocomplete="cc-csc"]', 'input[placeholder*="CVV"]',
        'input[placeholder*="CVC"]', '#cvv', '#cvc',
        'input[data-testid="cvv-input"]',
    ]
    _fill_dom_ids(page, ["cardCvv", "cvv", "cvc"], cvv) or _fill_visible_input(page, cvv_selectors, cvv, timeout=5000) or _fill_by_label_fallback(page, ["CVV", "CVC", "Security code"], cvv, timeout=5000) or _fill_with_fallback(page, cvv_selectors, cvv, timeout=5000)
    print(f"[*] CVV filled")
    time.sleep(random.uniform(0.5, 1.0))


def _fill_billing_address(page, address: dict):
    """Fill billing address fields."""
    line1 = address.get("line1", "")
    city = address.get("city", "")
    state = address.get("state", "")
    postal_code = address.get("postal_code", "")
    first_name = address.get("first_name", "")
    last_name = address.get("last_name", "")

    _ensure_country_us(page)

    if first_name or last_name:
        _fill_signup_name(page, first_name, last_name)

    addr_selectors = [
        '#billingLine1', '#billingAddressLine1',
        'input[name="billingLine1"]', 'input[name="billingAddressLine1"]',
        'input[name="line1"]', 'input[name="addressLine1"]',
        'input[name*="billing" i][name*="line1" i]',
        'input[id*="billing" i][id*="line1" i]',
        'input[placeholder*="Street address" i]', 'input[aria-label*="Street address" i]',
        'input[name="streetAddress"]', 'input[autocomplete="address-line1"]',
        '#addressLine1', '#street-address', '#line1',
    ]
    if not (_fill_dom_ids(page, ["billingLine1", "billingAddressLine1", "addressLine1"], line1) or _fill_by_visible_label_text(page, "Street address", line1) or _fill_visible_input(page, addr_selectors, line1, timeout=5000) or _fill_by_label_fallback(page, ["Street address", "Address line 1"], line1, timeout=5000) or _fill_with_fallback(page, addr_selectors, line1, timeout=5000)):
        print("[!] Address line1 field not found")
    _dismiss_overlays(page)
    time.sleep(random.uniform(0.3, 0.8))

    city_selectors = [
        '#billingCity', '#billingLocality',
        'input[name="billingCity"]', 'input[name="billingLocality"]',
        'input[name="city"]', 'input[name="addressCity"]',
        'input[name*="billing" i][name*="city" i]',
        'input[id*="billing" i][id*="city" i]',
        'input[id*="locality" i]', 'input[name*="locality" i]',
        'input[autocomplete="address-level2"]', '#city', '#addressCity',
    ]
    if not (_fill_dom_ids(page, ["billingCity", "billingLocality", "city"], city) or _fill_by_visible_label_text(page, "City", city) or _fill_visible_input(page, city_selectors, city, timeout=5000) or _fill_by_label_fallback(page, ["City", "Town"], city, timeout=5000) or _fill_with_fallback(page, city_selectors, city, timeout=5000)):
        print("[!] Billing city field not found")
    _dismiss_overlays(page)
    time.sleep(random.uniform(0.3, 0.8))

    state_selectors = [
        '#billingState', '#billingAdministrativeArea',
        'select[name="billingState"]', 'select[name="billingAdministrativeArea"]',
        'select[name="state"]', 'select[name="addressState"]',
        'select[name*="billing" i][name*="state" i]',
        'select[name*="administrativeArea" i]',
        'select[id*="billing" i][id*="state" i]',
        'select[id*="AdministrativeArea" i]',
        'select[id*="state"]', '#state',
    ]
    if not _select_with_fallback(page, state_selectors, state, timeout=5000):
        state_text_selectors = [
            '#billingState', '#billingAdministrativeArea',
            'input[name="billingState"]', 'input[name="billingAdministrativeArea"]',
            'input[name="state"]', 'input[name="addressState"]',
            'input[name*="billing" i][name*="state" i]',
            'input[name*="administrativeArea" i]',
            'input[id*="billing" i][id*="state" i]',
            '#state-input',
        ]
        if not (_fill_dom_ids(page, ["billingState", "billingAdministrativeArea", "state"], state) or _fill_visible_input(page, state_text_selectors, state, timeout=3000) or _fill_by_label_fallback(page, ["State", "Province"], state, timeout=3000) or _fill_with_fallback(page, state_text_selectors, state, timeout=3000)):
            print("[!] Billing state field not found")
    time.sleep(random.uniform(0.3, 0.8))

    zip_selectors = [
        '#billingPostalCode',
        'input[name="billingPostalCode"]',
        'input[name="postalCode"]', 'input[name="zip"]',
        'input[name*="billing" i][name*="postal" i]',
        'input[id*="billing" i][id*="postal" i]',
        'input[autocomplete="postal-code"]', '#postalCode', '#zip',
    ]
    if not (_fill_dom_ids(page, ["billingPostalCode", "postalCode", "zip"], postal_code) or _fill_by_visible_label_text(page, "ZIP code", postal_code) or _fill_visible_input(page, zip_selectors, postal_code, timeout=5000) or _fill_by_label_fallback(page, ["ZIP code", "Postal code", "Zip"], postal_code, timeout=5000) or _fill_with_fallback(page, zip_selectors, postal_code, timeout=5000)):
        print("[!] Billing postal code field not found")
    _dismiss_overlays(page)
    print(f"[*] Address filled: {line1}, {city}, {state} {postal_code}")
    time.sleep(random.uniform(0.5, 1.0))


def _verify_checkout_fields(page):
    """Fail fast when PayPal's checkoutweb form still has blank required fields."""
    fields = {
        "email": (["email"], ['input[id="email"]', '#email', 'input[name="email"]', 'input[type="email"]', 'input[placeholder*="email" i]']),
        "phone": (["phone", "phoneNumber"], ['input[id="phone"]', 'input[name="phone"]', 'input[type="tel"]', 'input[placeholder*="phone" i]']),
        "cardNumber": (["cardNumber"], ['input[id="cardNumber"]', '#cardNumber', 'input[name="cardNumber"]', 'input[autocomplete="cc-number"]', 'input[placeholder*="Card" i]']),
        "cardExpiry": (["cardExpiry"], ['input[id="cardExpiry"]', '#cardExpiry', 'input[name="cardExpiry"]', 'input[autocomplete="cc-exp"]', 'input[placeholder*="Expiration" i]', 'input[placeholder*="MM" i]']),
        "cardCvv": (["cardCvv", "cvv", "cvc"], ['input[id="cardCvv"]', '#cardCvv', 'input[name="cardCvv"]', 'input[name="cvv"]', 'input[name="cvc"]', 'input[autocomplete="cc-csc"]', 'input[placeholder*="CVV" i]']),
        "firstName": (["firstName", "first-name"], ['input[id="firstName"]', '#firstName', 'input[name="firstName"]', 'input[autocomplete="given-name"]', 'input[placeholder*="First name" i]']),
        "lastName": (["lastName", "last-name"], ['input[id="lastName"]', '#lastName', 'input[name="lastName"]', 'input[autocomplete="family-name"]', 'input[placeholder*="Last name" i]']),
        "billingLine1": (["billingLine1", "billingAddressLine1", "addressLine1"], ['input[id="billingLine1"]', '#billingLine1', 'input[name="billingLine1"]', 'input[autocomplete="address-line1"]', 'input[placeholder*="Street address" i]']),
        "billingCity": (["billingCity", "billingLocality", "city"], ['input[id="billingCity"]', '#billingCity', 'input[name="billingCity"]', 'input[autocomplete="address-level2"]', 'input[placeholder*="City" i]']),
        "billingPostalCode": (["billingPostalCode", "postalCode", "zip"], ['input[id="billingPostalCode"]', '#billingPostalCode', 'input[name="billingPostalCode"]', 'input[autocomplete="postal-code"]', 'input[placeholder*="ZIP" i]']),
        "password": (["password", "createPassword"], ['input[id="password"]', '#password', 'input[name="password"]', 'input[type="password"]', 'input[placeholder*="password" i]']),
    }
    values = {name: _read_field_value(page, ids, selectors) for name, (ids, selectors) in fields.items()}
    required = ["email", "cardNumber", "cardExpiry", "cardCvv", "firstName", "lastName", "billingLine1", "billingCity", "billingPostalCode", "password"]
    missing = [element_id for element_id in required if not values.get(element_id)]
    if missing:
        raise _PayPalStepError("verify_fields", f"blank PayPal field(s): {', '.join(missing)}")
    masked = dict(values)
    if masked.get("cardNumber"):
        masked["cardNumber"] = f"****{masked['cardNumber'][-4:]}"
    if masked.get("cardCvv"):
        masked["cardCvv"] = "***"
    if masked.get("password"):
        masked["password"] = "***"
    print(f"[*] PayPal fields verified: {masked}")


def _read_field_value(page, element_ids: list[str], selectors: list[str]) -> str:
    for element_id in element_ids:
        for scope in [page, *getattr(page, "frames", [])]:
            try:
                value = scope.evaluate(
                    """(id) => {
                        const el = document.getElementById(id);
                        return el ? String(el.value || "").trim() : null;
                    }""",
                    element_id,
                )
                if value:
                    return value
            except Exception:
                continue
    for scope in [page, *getattr(page, "frames", [])]:
        for selector in selectors:
            try:
                el = scope.locator(selector).first
                if not el.is_visible(timeout=1000):
                    continue
                value = el.input_value(timeout=1000).strip()
                if value:
                    return value
            except Exception:
                continue
    return ""


def _accept_terms(page):
    """Check terms checkbox and click agree."""
    checkbox_selectors = [
        'input[type="checkbox"][name*="agree"]',
        'input[type="checkbox"][name*="terms"]',
        'input[type="checkbox"][id*="agree"]',
        'input[type="checkbox"][id*="terms"]',
        '[data-testid="agreement-checkbox"]',
    ]
    for selector in checkbox_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=2000) and not el.is_checked():
                el.check()
                print("[*] Terms checkbox checked")
                break
        except Exception:
            continue
    time.sleep(1)


def _handle_sms_verification(page, sms_cfg: dict, baseline: str) -> str | None:
    """Handle SMS verification if prompted."""
    code_selectors = [
        'input[name="code"]', 'input[name="smsCode"]',
        'input[name="otpCode"]', 'input[placeholder*="code"]',
        'input[placeholder*="Code"]', '#code', '#otp',
    ]
    needs_sms = False
    for selector in code_selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=3000):
                needs_sms = True
                break
        except Exception:
            continue

    if not needs_sms:
        _click_with_fallback(page, [
            'button:has-text("Send Code")',
            'button:has-text("Send")',
            'button:has-text("Text me")',
        ], timeout=3000)
        time.sleep(2)
        for selector in code_selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=3000):
                    needs_sms = True
                    break
            except Exception:
                continue

    if not needs_sms:
        return None

    print("[*] SMS verification required, polling for code...")
    code = _poll_sms_code(
        sms_cfg["api_url"], baseline,
        timeout=sms_cfg["timeout"],
        poll_interval=sms_cfg["poll_interval"],
    )
    if not code:
        raise _PayPalStepError("sms_verify", "sms_code_timeout")

    for selector in code_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=2000):
                el.fill(code)
                break
        except Exception:
            continue
    time.sleep(1)

    _click_with_fallback(page, [
        'button:has-text("Confirm")',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ], timeout=5000)
    time.sleep(2)
    return code


def _submit_payment(page):
    """Click the final payment/agree button."""
    selectors = [
        'button:has-text("Agree and Continue")',
        'button:has-text("Agree & Continue")',
        'button:has-text("Pay Now")',
        'button:has-text("Continue")',
        'button:has-text("Agree and Create Account")',
        'button:has-text("Agree")',
        'button[type="submit"]',
        '[data-testid="submit-button"]',
        '#payment-submit-btn',
    ]
    if _click_with_fallback(page, selectors, timeout=10000):
        print("[*] Payment submitted")
        time.sleep(3)
    else:
        raise _PayPalStepError("submit", "submit button not found")


def _wait_for_stripe_redirect(page, timeout: int = 60):
    """Wait for redirect back to Stripe or ChatGPT."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        if "checkout.stripe.com" in url or "chatgpt.com" in url:
            print(f"[*] Redirected to: {url[:80]}")
            return
        time.sleep(2)
    raise _PayPalStepError("wait_redirect", f"redirect timeout (current: {page.url[:80]})")


# ──────────────────────────── email alias ────────────────────────────


def _generate_alias_email(base_email: str) -> str:
    """Generate a PayPal alias email (always Gmail) from base mailbox email."""
    gmail_local = ""
    if base_email and "@" in base_email:
        local = base_email.rsplit("@", 1)[0]
        gmail_local = re.sub(r"[^a-zA-Z0-9.]", "", local)[:20]
    if not gmail_local:
        gmail_local = f"buyer{random.randint(1000, 9999)}"
    suffix = random.randint(100, 999)
    return f"{gmail_local}+pp{suffix}@gmail.com"


# ──────────────────────────── result saving ────────────────────────────


def _save_paypal_result(data: dict, json_path: str) -> str:
    """Save payment result to session JSON and SQLite."""
    if not json_path:
        email = (data.get("email") or "unknown").replace("+", "")
        safe = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", email)
        output_dir = Path(CFG.get("output", {}).get("directory", "sessions"))
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = str(output_dir / f"session_{safe}_{int(time.time())}.json")

    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)
    return json_path
