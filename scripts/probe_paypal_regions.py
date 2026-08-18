#!/usr/bin/env python3
"""Probe which checkout regions expose PayPal as a payment method.

This is a *diagnostic* tool (not link generation). For each account x checkout
country it runs only the first two stages of the extractor:

  Stage 1: ChatGPT checkout  (billing_details.country = <region>)
  Stage 2: Stripe payment_pages/init

and records, WITHOUT aborting:
  - paypal_available : "paypal" present in Stripe ``payment_method_types``
  - amount / currency : from ``stripe_amount_details`` (amount == 0 => free trial)
  - payment_method_types : full list returned by Stripe
  - error : any failure encountered during the probe

Traditional expectation: US checkout -> PayPal payment method available;
JP / TR checkout -> 0-due free-trial promo. This probe lets us confirm that
empirically across the mailbox-pool accounts.

Usage (from project root):
  python -m scripts.probe_paypal_regions                 # all pool accounts, default regions
  python -m scripts.probe_paypal_regions --emails a@x,b@y
  python -m scripts.probe_paypal_regions --regions US,JP,TR
  python -m scripts.probe_paypal_regions --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from sms_tool.account_seed import extract_access_token, load_account_seed
from sms_tool.gen_pp_link import (
    CHATGPT_TIMEOUT,
    CURRENCY_MAP,
    DEFAULT_CONFIG_PATH,
    DEFAULT_STRIPE_PK,
    DEFAULT_TIMEOUT,
    STRIPE_VERSION,
    _checkout_post,
    _load_json,
    _new_session,
    _proxies_from_config,
    proxy_for_country_template,
    rotate_proxy_session,
    stripe_amount_details,
)
from sms_tool.storage import list_paypal_accounts

# Regions that have both billing data and a currency mapping in gen_pp_link.
DEFAULT_REGIONS = ("US", "JP", "TR", "GB", "DE", "AU", "SG", "TH", "CA", "NZ", "IE", "FR")


def _create_checkout(access_token: str, checkout_country: str, currency: str,
                     proxy: str, cookie_header: str, with_promo: bool = True) -> dict[str, Any]:
    """Stage 1: create a ChatGPT checkout session for the given billing country."""
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": checkout_country, "currency": currency},
        "checkout_ui_mode": "custom",
    }
    if with_promo:
        body["promo_campaign"] = {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False}
    r = _checkout_post(
        "https://chatgpt.com/backend-api/payments/checkout",
        body, access_token, cookie_header, proxy, CHATGPT_TIMEOUT,
    )
    if r.status_code == 401:
        raise RuntimeError("access_token invalid or expired (401)")
    if r.status_code == 429:
        raise RuntimeError(f"rate limited (429) retry-after={r.headers.get('Retry-After', '')}")
    r.raise_for_status()
    data = r.json()
    cs_id = data.get("checkout_session_id") or data.get("id", "")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"unexpected checkout response: {json.dumps(data, ensure_ascii=False)[:200]}")
    pk = data.get("publishable_key") or ""
    return {
        "cs_id": cs_id,
        "stripe_pk": pk if str(pk).startswith("pk_") else DEFAULT_STRIPE_PK,
    }


def _stripe_init(cs_id: str, stripe_pk: str, proxy: str) -> dict[str, Any]:
    """Stage 2: Stripe payment_pages/init. Returns the raw init payload."""
    stripe = _new_session(proxy)
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
    }
    r = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/init", data=body, timeout=DEFAULT_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"stripe init failed: {r.status_code} {r.text[:300]}")
    return r.json()


def probe_region(access_token: str, region: str, checkout_proxy: str,
                 provider_proxy: str, cookie_header: str, with_promo: bool = True) -> dict[str, Any]:
    currency = CURRENCY_MAP.get(region, "USD")
    result: dict[str, Any] = {
        "region": region,
        "currency_sent": currency,
        "paypal_available": None,
        "payment_method_types": None,
        "amount": None,
        "amount_currency": None,
        "amount_source": None,
        "zero_due": None,
        "cs_id": "",
        "error": "",
    }
    try:
        checkout = _create_checkout(access_token, region, currency, checkout_proxy, cookie_header, with_promo)
        result["cs_id"] = checkout["cs_id"]
        init = _stripe_init(checkout["cs_id"], checkout["stripe_pk"], provider_proxy)
        pm_types = [str(t).lower() for t in (init.get("payment_method_types") or [])]
        result["payment_method_types"] = pm_types
        result["paypal_available"] = "paypal" in pm_types
        amount_info = stripe_amount_details(init)
        result["amount"] = amount_info.get("amount")
        result["amount_currency"] = amount_info.get("currency")
        result["amount_source"] = amount_info.get("source")
        if amount_info.get("amount") is not None:
            result["zero_due"] = amount_info.get("amount") == 0
    except Exception as exc:  # noqa: BLE001 - probe records every failure
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _region_proxy(template: str, region: str) -> str:
    """Build a per-region egress proxy from a provider credential template.

    Swaps the template's country code (kookeey password tail ``-CC`` or a
    ``region-XX`` username) to the target country AND rotates the sticky session
    id so each region gets a fresh IP (a fixed sid pins every region to one exit).
    """
    return rotate_proxy_session(proxy_for_country_template(template, region))


def _resolve_accounts(emails: list[str]) -> list[dict[str, str]]:
    accounts: list[dict[str, str]] = []
    if emails:
        for email in emails:
            data, _ = load_account_seed(email=email)
            accounts.append({
                "email": email,
                "access_token": extract_access_token(data),
                "cookie_header": str(data.get("cookie_header") or ""),
            })
        return accounts
    for row in list_paypal_accounts():
        accounts.append({
            "email": row.get("email", ""),
            "access_token": str(row.get("access_token") or "").strip(),
            "cookie_header": "",
        })
    return accounts


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe checkout regions for PayPal availability")
    parser.add_argument("--emails", default="", help="Comma-separated account emails (default: all pool accounts)")
    parser.add_argument("--regions", default="", help="Comma-separated regions (default: built-in set)")
    parser.add_argument("--json", dest="json_out", default="", help="Write full JSON results to this path")
    parser.add_argument("--no-promo", action="store_true", help="Omit the plus-1-month-free promo (elicits non-zero amount / PayPal)")
    parser.add_argument("--proxy-template", default="", help="proxy credential template (kookeey password tail -CC or region-XX username); routes each region through its own egress IP")
    args = parser.parse_args()

    emails = [e.strip() for e in args.emails.split(",") if e.strip()]
    regions = [r.strip().upper() for r in args.regions.split(",") if r.strip()] or list(DEFAULT_REGIONS)
    with_promo = not args.no_promo
    proxy_template = args.proxy_template.strip()

    cfg = _load_json(DEFAULT_CONFIG_PATH)
    stage = _proxies_from_config(cfg)
    default_checkout_proxy = stage["checkout"]
    default_provider_proxy = stage["provider"]
    if proxy_template:
        print("[proxy] per-region routing via template (fresh sid per region)", file=sys.stderr)
    else:
        print(f"[proxy] checkout={default_checkout_proxy or 'DIRECT'} provider={default_provider_proxy or 'DIRECT'}", file=sys.stderr)

    accounts = _resolve_accounts(emails)
    accounts = [a for a in accounts if a["access_token"]]
    if not accounts:
        print("[error] no accounts with access tokens found", file=sys.stderr)
        return 1

    all_results: dict[str, list[dict[str, Any]]] = {}
    for acct in accounts:
        email = acct["email"]
        print(f"\n=== Account: {email} ===", file=sys.stderr)
        rows: list[dict[str, Any]] = []
        for region in regions:
            if proxy_template:
                region_proxy = _region_proxy(proxy_template, region)
                checkout_proxy = region_proxy
                provider_proxy = region_proxy
            else:
                checkout_proxy = default_checkout_proxy
                provider_proxy = default_provider_proxy
            row = probe_region(
                acct["access_token"], region, checkout_proxy, provider_proxy, acct["cookie_header"], with_promo,
            )
            rows.append(row)
            if row["error"]:
                status = f"ERROR {row['error'][:60]}"
            else:
                pp = "PayPal=YES" if row["paypal_available"] else "PayPal=no "
                amt = row["amount"]
                zero = "0-due" if row["zero_due"] else (f"amt={amt}" if amt is not None else "amt=?")
                status = f"{pp} {zero} {row['amount_currency'] or ''}"
            print(f"  {region:<3} {status}", file=sys.stderr)
        all_results[email] = rows

    # Summary matrix
    print("\n===== REGION x PayPal SUPPORT MATRIX =====")
    header = f"{'region':<8}" + "".join(f"{e.split('@')[0][:16]:<18}" for e in all_results)
    print(header)
    for region in regions:
        line = f"{region:<8}"
        for email in all_results:
            row = next((r for r in all_results[email] if r["region"] == region), None)
            if row is None:
                cell = "-"
            elif row["error"]:
                cell = "ERR"
            else:
                pp = "PP" if row["paypal_available"] else "--"
                zd = "/0" if row["zero_due"] else ""
                cell = f"{pp}{zd}"
            line += f"{cell:<18}"
        print(line)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"\n[json] written to {args.json_out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
