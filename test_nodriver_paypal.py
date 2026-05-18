#!/usr/bin/env python3
"""Test nodriver for PayPal CAPTCHA handling.

Uses nodriver (undetected Chrome) to open the PayPal authorization link
and attempt to bypass CAPTCHA challenges that block Playwright-based approaches.
"""

import asyncio
import json
import sys
import time

import nodriver as uc


PAYPAL_URL = (
    "https://pm-redirects.stripe.com/authorize/"
    "acct_1HOrSwC6h1nxGoI3/"
    "sa_nonce_UXZ8flKTIhz2MtDIbf4frBQNWC9jWXj"
    "?useWebAuthSession=true&followRedirectsInSDK=true"
)

PROXY = "socks5://127.0.0.1:17912"  # JP exit


async def main():
    print("[*] Starting nodriver (undetected Chrome)...")

    browser = await uc.start(
        headless=False,
        proxy=PROXY,
        lang="en-US",
        browser_args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )

    print("[*] Navigating to PayPal authorization link...")
    page = await browser.get(PAYPAL_URL)

    # Wait for page to settle
    await asyncio.sleep(5)

    # Take initial screenshot
    await page.save_screenshot("runtime/nodriver_01_initial.png")
    print(f"[*] Page title: {await page.evaluate('document.title')}")
    print(f"[*] Page URL: {page.url}")

    # Check for Cloudflare challenge
    content = await page.get_content()
    if "challenge" in content.lower() or "cloudflare" in content.lower():
        print("[*] Cloudflare challenge detected, attempting cf_verify...")
        try:
            await page.cf_verify()
            print("[*] cf_verify completed")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[!] cf_verify failed: {e}")

    # Check for reCAPTCHA
    if "recaptcha" in content.lower() or "captcha" in content.lower():
        print("[*] CAPTCHA detected in page content")

        # Try to find reCAPTCHA iframe and click checkbox
        try:
            frames = await page.get_frames()
            for frame in frames:
                frame_url = frame.url or ""
                if "recaptcha" in frame_url.lower():
                    print(f"[*] Found reCAPTCHA frame: {frame_url[:80]}")
                    # Try to click the checkbox
                    try:
                        checkbox = await frame.select(".recaptcha-checkbox-border, #recaptcha-anchor")
                        if checkbox:
                            await checkbox.click()
                            print("[*] Clicked reCAPTCHA checkbox")
                            await asyncio.sleep(5)
                    except Exception as e:
                        print(f"[!] Checkbox click failed: {e}")
        except Exception as e:
            print(f"[!] Frame inspection failed: {e}")

    # Take post-screenshot
    await page.save_screenshot("runtime/nodriver_02_after_captcha.png")
    content = await page.get_content()
    print(f"[*] Final URL: {page.url}")
    print(f"[*] Content length: {len(content)}")

    # Check if we made it through
    if "paypal.com" in page.url.lower():
        print("[*] Successfully reached PayPal!")
        await page.save_screenshot("runtime/nodriver_03_paypal.png")

        # Look for payment form elements
        try:
            # Check for "Pay with Debit or Credit Card" button
            card_btn = await page.find("Pay with Debit or Credit Card", timeout=5)
            if card_btn:
                print("[*] Found card payment button")
        except Exception:
            pass

    elif "stripe.com" in page.url.lower():
        print("[*] Still on Stripe redirect page")
    else:
        print(f"[*] Unexpected page: {page.url}")

    # Save page HTML for analysis
    with open("runtime/nodriver_page.html", "w", encoding="utf-8") as f:
        f.write(content)
    print("[*] Page HTML saved to runtime/nodriver_page.html")

    # Keep browser open for inspection
    print("[*] Browser will stay open for 60 seconds for inspection...")
    await asyncio.sleep(60)

    browser.stop()
    print("[*] Done")


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
