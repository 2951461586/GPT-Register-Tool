"""CLI boundary for Omakse server commands (--omakse-extract, --omakse-us-pay).

``omakse_client`` owns the HTTP behavior; this module resolves argparse values
(proxies from config stages) into those calls and formats JSON output.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..diagnostics import safe_print


@dataclass(frozen=True)
class OmakseCommandContext:
    """Legacy CLI hooks required by omakse command orchestration."""

    runtime_config: Mapping[str, Any]


def _paypal_stage_proxies(config: Mapping[str, Any]) -> dict[str, Any]:
    paypal_cfg = config.get("paypal") if isinstance(config.get("paypal"), dict) else {}
    stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    return stage


def omakse_extract(args: Any, ctx: OmakseCommandContext) -> None:
    """Extract PayPal links via the omakse server."""
    from ..omakse_client import extract_links, extract_links_for_account

    # Resolve credentials: explicit --at, or look up by --email
    at = (args.at or "").strip()
    email = (args.email or "").strip()

    # Resolve US proxies
    us_proxies = (args.omakse_us_proxies or "").strip()
    if not us_proxies:
        # Fall back to config stage_proxies.checkout or proxy.default
        us_proxies = _paypal_stage_proxies(ctx.runtime_config).get("checkout") or (ctx.runtime_config.get("proxy") or {}).get("default", "")
        if us_proxies:
            safe_print(f"[*] Using checkout stage proxy as US proxy: {us_proxies}", file=sys.stderr)

    # Resolve promotion proxies
    promo_proxies = (args.omakse_promo_proxies or "").strip()
    if not promo_proxies:
        promo_proxies = _paypal_stage_proxies(ctx.runtime_config).get("promotion") or ""

    if at:
        print(f"[*] Starting omakse link extraction with explicit AT...", file=sys.stderr)
        result = extract_links(
            credentials=at,
            us_proxies=us_proxies,
            promotion_proxies=promo_proxies,
            provider_country=args.omakse_provider_country,
            promotion_country=args.omakse_promo_country,
            concurrency=args.omakse_concurrency,
            max_attempts=args.omakse_max_attempts,
            poll_interval=args.omakse_poll_interval,
            max_poll_seconds=args.omakse_max_poll_seconds,
            base_url=args.omakse_base_url,
            proxy=args.omakse_local_proxy or "",
        )
    elif email:
        print(f"[*] Starting omakse link extraction for {email}...", file=sys.stderr)
        result = extract_links_for_account(
            email=email,
            us_proxies=us_proxies,
            promotion_proxies=promo_proxies,
            provider_country=args.omakse_provider_country,
            promotion_country=args.omakse_promo_country,
            concurrency=args.omakse_concurrency,
            max_attempts=args.omakse_max_attempts,
            poll_interval=args.omakse_poll_interval,
            max_poll_seconds=args.omakse_max_poll_seconds,
            base_url=args.omakse_base_url,
            proxy=args.omakse_local_proxy or "",
        )
    else:
        print("[Error] --omakse-extract requires --at <TOKEN> or --email <EMAIL>", file=sys.stderr)
        raise SystemExit(2)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def omakse_us_pay(args: Any, ctx: OmakseCommandContext) -> None:
    """Run US PayPal protocol payment via the omakse server."""
    from ..omakse_client import run_us_payment_and_wait

    ba_token = (args.ba_token or "").strip()
    if not ba_token:
        print("[Error] --omakse-us-pay requires --ba-token <BA-TOKEN>", file=sys.stderr)
        raise SystemExit(2)

    # Resolve payment proxy: explicit --checkout-proxy, or config stage
    proxy = (args.checkout_proxy or "").strip()
    if not proxy:
        proxy = _paypal_stage_proxies(ctx.runtime_config).get("checkout") or (ctx.runtime_config.get("proxy") or {}).get("default", "")
        if proxy:
            safe_print(f"[*] Using config proxy for US payment: {proxy}", file=sys.stderr)

    if not proxy:
        print("[Error] No proxy available for US payment. Use --checkout-proxy or configure proxy.default", file=sys.stderr)
        raise SystemExit(2)

    result = run_us_payment_and_wait(
        ba_token=ba_token,
        proxy=proxy,
        phone_country=args.omakse_phone_country,
        phone_country_code=args.omakse_phone_cc,
        proxy_region=args.omakse_proxy_region,
        client_id=args.omakse_client_id,
        randomize_device=args.omakse_randomize_device,
        preconfirm_phone=args.omakse_preconfirm_phone,
        send_phone_otp=args.omakse_send_otp,
        load_return_url=args.omakse_load_return_url,
        poll_interval=args.omakse_poll_interval,
        max_poll_seconds=args.omakse_max_poll_seconds,
        base_url=args.omakse_base_url,
        local_proxy=args.omakse_local_proxy or "",
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)
