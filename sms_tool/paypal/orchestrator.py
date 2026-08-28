"""PayPal auto-payment orchestration: strategy selection and result persistence.

Extracted from ``sms_tool.paypal_auto``. Tries the reverse-engineered HTTP
protocol first, then nodriver, then an anti-detect browser (Camoufox with a
CloakBrowser fallback), and finally persists the outcome to the session file.
"""

from __future__ import annotations

import time
from typing import Any

from ..account_seed import extract_access_token as _extract_access_token
from ..account_seed import load_account_seed as _load_seed
from ..config import CFG
from ..gen_pp_link import generate_pp_link
from ..paypal_fingerprints import PAYPAL_USER_AGENT as _USER_AGENT
from ..paypal_reverse import try_reverse_pay
from ..utils import _generate_password, _random_name
from .config_picker import (
    _generate_alias_email,
    _pick_card_and_address,
    _pick_phone_and_sms,
    _save_paypal_result,
)
from .errors import _PayPalStepError
from .flow_steps import _run_browser_steps
from .session import _inject_navigator_overrides, _screenshot

def auto_pay(
    email: str = "",
    session_file: str = "",
    approval_url: str = "",
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
    paypal_url = str(approval_url or paypal.get("url") or "").strip()
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

    print(f"[*] Card: [REDACTED]  Name: {first_name} {last_name}  Email: {alias_email}  Phone: {phone}")

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

    # Normalize proxy: bridge credential/http(s) upstreams to a local socks5h
    # endpoint the browser can consume; also restores remote-DNS semantics.
    from .proxy_bridge import proxy_for_browser

    nd_proxy, close_bridge = proxy_for_browser(proxy)

    print("[*] Attempting nodriver payment flow...")
    try:
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
    finally:
        close_bridge()

    if result.get("ok"):
        result.setdefault("paypal_status", "completed")
        result.setdefault("alias_email", alias_email)
        result.setdefault("card_last4", card["number"][-4:])
        result.setdefault("password", password)
        print("[*] nodriver payment succeeded!")
    else:
        print(f"[!] nodriver payment failed: {result.get('error', '')}")

    return result

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

    # Normalize proxy: bridge credential/http(s) upstreams to a local socks5h
    # endpoint the browser can consume; also restores remote-DNS semantics.
    from .proxy_bridge import proxy_for_browser

    browser_proxy, close_bridge = proxy_for_browser(proxy)

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

    try:
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
    finally:
        close_bridge()

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
