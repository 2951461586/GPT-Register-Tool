"""nodriver-based CAPTCHA solver for PayPal.

Uses nodriver (undetected Chrome) to bypass reCAPTCHA challenges
that block Playwright-based approaches. Extracts cookies after
solving so the reverse HTTP protocol can continue.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


def solve_captcha_with_nodriver(
    page_url: str,
    proxy: str = "",
    headless: bool = False,
    timeout: int = 120,
) -> dict[str, Any]:
    """Use nodriver to solve CAPTCHA on a PayPal page.

    Returns dict with:
      - ok: bool
      - cookies: dict[str, str] (cookie name -> value)
      - cookie_header: str (formatted for HTTP header)
      - final_url: str
      - error: str (if failed)
    """
    try:
        import nodriver as uc
    except ImportError:
        return {"ok": False, "error": "nodriver not installed (pip install nodriver)"}

    return uc.loop().run_until_complete(
        _solve_async(uc, page_url, proxy, headless, timeout)
    )


async def _solve_async(
    uc: Any,
    page_url: str,
    proxy: str,
    headless: bool,
    timeout: int,
) -> dict[str, Any]:
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    browser = await uc.start(
        headless=headless,
        proxy=proxy or None,
        lang="en-US",
        browser_args=browser_args,
    )

    try:
        return await _do_solve(browser, page_url, timeout)
    finally:
        browser.stop()


async def _do_solve(browser: Any, page_url: str, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    print(f"[nodriver] Navigating to {page_url[:80]}...")

    page = await browser.get(page_url)
    await asyncio.sleep(5)

    # Check for Cloudflare challenge
    content = await page.get_content()
    if "challenge" in content.lower() or "cloudflare" in content.lower():
        print("[nodriver] Cloudflare challenge detected, attempting verify_cf...")
        try:
            await page.verify_cf()
            print("[nodriver] verify_cf completed")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[nodriver] verify_cf failed: {e}")

    # Re-check content
    content = await page.get_content()
    title = await page.evaluate("document.title")
    print(f"[nodriver] Page title: {title}")
    print(f"[nodriver] Page URL: {page.url}")

    # Look for reCAPTCHA and try to solve
    captcha_found = False
    if "recaptcha" in content.lower() or "captcha" in content.lower():
        print("[nodriver] CAPTCHA detected in page content")
        captcha_found = True

        solved = await _try_solve_recaptcha(page, browser, deadline)
        if solved:
            print("[nodriver] CAPTCHA solved!")
        else:
            print("[nodriver] CAPTCHA solve attempt finished")

        # Wait for page to update
        await asyncio.sleep(5)

    # Check final state
    final_url = page.url
    final_title = await page.evaluate("document.title")
    print(f"[nodriver] Final URL: {final_url[:80]}")
    print(f"[nodriver] Final title: {final_title}")

    # Extract cookies using the browser's CookieJar API
    cookies = await _extract_cookies(browser)
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    print(f"[nodriver] Extracted {len(cookies)} cookies")

    return {
        "ok": len(cookies) > 0,
        "cookies": cookies,
        "cookie_header": cookie_header,
        "final_url": final_url,
        "final_title": final_title,
        "captcha_found": captcha_found,
    }


async def _try_solve_recaptcha(page: Any, browser: Any, deadline: float) -> bool:
    """Try to solve reCAPTCHA by finding and clicking the checkbox."""

    # Method 1: Find reCAPTCHA iframe in frames and click checkbox
    try:
        frames = await page.get_frames()
        for frame in frames:
            frame_url = frame.url or ""
            if "recaptcha" not in frame_url.lower():
                continue
            print(f"[nodriver] Found reCAPTCHA frame: {frame_url[:80]}")

            # Try clicking the checkbox via CSS selector
            try:
                checkbox = await frame.select(
                    "#recaptcha-anchor, .recaptcha-checkbox-border",
                    timeout=10,
                )
                if checkbox:
                    await checkbox.click()
                    print("[nodriver] Clicked reCAPTCHA checkbox")
                    await asyncio.sleep(5)
                    return True
            except Exception as e:
                print(f"[nodriver] Checkbox click via select failed: {e}")

            # Try via JS in the frame
            try:
                result = await frame.evaluate("""
                    (function() {
                        var cb = document.getElementById('recaptcha-anchor');
                        if (cb) { cb.click(); return 'clicked'; }
                        return 'not_found';
                    })()
                """)
                print(f"[nodriver] JS checkbox click: {result}")
                if result == "clicked":
                    await asyncio.sleep(5)
                    return True
            except Exception as e:
                print(f"[nodriver] JS checkbox click failed: {e}")
    except Exception as e:
        print(f"[nodriver] Frame inspection failed: {e}")

    # Method 2: Try verify_cf (Cloudflare-style bypass)
    try:
        await page.verify_cf()
        print("[nodriver] verify_cf succeeded in reCAPTCHA handler")
        return True
    except Exception:
        pass

    # Method 3: Look for any clickable CAPTCHA elements on main page
    try:
        # Sometimes reCAPTCHA is rendered directly, not in an iframe
        for selector in [
            "#recaptcha-anchor",
            ".recaptcha-checkbox",
            "[data-action]",
            ".g-recaptcha",
        ]:
            try:
                el = await page.select(selector, timeout=3)
                if el:
                    await el.click()
                    print(f"[nodriver] Clicked {selector}")
                    await asyncio.sleep(5)
                    return True
            except Exception:
                continue
    except Exception as e:
        print(f"[nodriver] Main page element search failed: {e}")

    return False


async def _extract_cookies(browser: Any) -> dict[str, str]:
    """Extract all cookies from the browser session."""
    cookies = {}
    try:
        jar = browser.cookies
        all_cookies = await jar.get_all()
        for c in all_cookies:
            name = getattr(c, "name", "") or (c[0] if isinstance(c, (tuple, list)) else "")
            value = getattr(c, "value", "") or (c[1] if isinstance(c, (tuple, list)) else "")
            if name:
                cookies[name] = value
        print(f"[nodriver] CookieJar returned {len(cookies)} cookies")
    except Exception as e:
        print(f"[nodriver] CookieJar extraction failed: {e}")

        # Fallback: try via CDP directly on a tab
        try:
            for tab in browser.tabs:
                try:
                    from nodriver import cdp
                    result = await tab.send(cdp.storage.get_cookies())
                    for c in result:
                        if hasattr(c, "name"):
                            cookies[c.name] = c.value
                    if cookies:
                        break
                except Exception:
                    continue
        except Exception as e2:
            print(f"[nodriver] CDP fallback failed: {e2}")

    return cookies
