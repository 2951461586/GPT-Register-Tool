"""CLI boundary for direct payment-link commands driven by an Access Token.

``gen_pp_link``/``upi_link``/``paypal_auto`` own protocol behavior; this module
resolves argparse values (country fallbacks, stage proxies) into those calls.
``PaymentLinkCommandContext`` keeps the legacy CLI's replaceable hooks explicit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaymentLinkCommandContext:
    """Legacy CLI hooks required by payment-link command orchestration."""

    payment_stage_args: Callable[..., tuple[Any, Any, Any, Any]]
    promotion_proxy_arg: Callable[..., Any]
    stage_country_overrides: Callable[..., dict[str, str]]
    runtime_config: Mapping[str, Any]


def generate_ba_link(args: Any, ctx: PaymentLinkCommandContext) -> None:
    """Generate PayPal BA link from Access Token."""
    from ..gen_pp_link import generate_pp_link

    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "missing --at (Access Token)"}))
        raise SystemExit(1)

    paypal_cfg = ctx.runtime_config.get("paypal") if isinstance(ctx.runtime_config.get("paypal"), dict) else {}
    regions = paypal_cfg.get("billing_regions") if isinstance(paypal_cfg.get("billing_regions"), list) else []
    generation_type = (getattr(args, "paypal_generation_type", None) or paypal_cfg.get("link_generation_type") or "").strip().lower().replace("-", "_")
    hosted_types = {"long", "long_link", "hosted", "hosted_long", "hosted_long_url", "stripe_hosted", "chatgpt_checkout", "chatgpt_checkout_link", "checkout_link", "short_checkout", "chatgpt_short_link"}
    checkout_country = None
    if generation_type in hosted_types:
        target_country = (getattr(args, "target_country", None) or paypal_cfg.get("target_country") or "US").strip().upper()
        checkout_country = (
            getattr(args, "checkout_country", None)
            or (regions[0] if regions else None)
            or paypal_cfg.get("checkout_country")
            or paypal_cfg.get("billing_country")
            or target_country
            or "US"
        ).strip().upper()
    else:
        target_country = (getattr(args, "target_country", None) or paypal_cfg.get("target_country") or "GB").strip().upper()
    proxy, checkout_proxy, provider_proxy, approve_proxy = ctx.payment_stage_args(args, "paypal")
    promotion_proxy = ctx.promotion_proxy_arg(args, "paypal")
    require_zero = not getattr(args, "no_require_zero", False)
    require_ba_token = bool(getattr(args, "require_ba_token", False))

    result = generate_pp_link(
        access_token=at,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        promotion_proxy=promotion_proxy,
        target_country=target_country,
        checkout_country=checkout_country,
        require_zero=require_zero,
        require_ba_token=require_ba_token,
        paypal_generation_type=getattr(args, "paypal_generation_type", None),
        stage_proxy_countries=ctx.stage_country_overrides(args),
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def generate_upi_qr(args: Any, ctx: PaymentLinkCommandContext) -> None:
    """Generate UPI hosted payment link and QR code from Access Token."""
    from ..gen_pp_link import generate_upi_qr_link

    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "missing --at (Access Token)"}))
        raise SystemExit(1)

    upi_cfg = ctx.runtime_config.get("upi") if isinstance(ctx.runtime_config.get("upi"), dict) else {}
    regions = upi_cfg.get("billing_regions") if isinstance(upi_cfg.get("billing_regions"), list) else []
    checkout_country = (
        getattr(args, "checkout_country", None)
        or getattr(args, "target_country", None)
        or upi_cfg.get("checkout_country")
        or upi_cfg.get("checkout_billing_country")
        or upi_cfg.get("billing_country")
        or upi_cfg.get("target_country")
        or (regions[0] if regions else None)
        or "IN"
    ).strip().upper()
    payment_country = (
        getattr(args, "payment_country", None)
        or upi_cfg.get("payment_country")
        or upi_cfg.get("payment_method_country")
        or "IN"
    ).strip().upper()
    proxy, checkout_proxy, provider_proxy, approve_proxy = ctx.payment_stage_args(args, "upi")
    require_zero = not getattr(args, "no_require_zero", False)
    result = generate_upi_qr_link(
        access_token=at,
        proxy=proxy,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        target_country=checkout_country,
        checkout_country=checkout_country,
        payment_country=payment_country,
        require_zero=require_zero,
        qr_path=getattr(args, "qr_path", None),
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def auto_pay(args: Any) -> None:
    """Run automated PayPal payment for a ChatGPT account."""
    from ..paypal_auto import auto_pay as run_auto_pay

    email = (args.email or "").strip()
    session_file = (args.session_file or "").strip()
    if not email and not session_file:
        print("[Error] --email or --session-file is required with --auto-pay")
        return

    reverse_only = getattr(args, 'auto_pay_reverse_only', False)
    mode = "reverse-only" if reverse_only else "reverse+browser"
    print(f"[*] Starting auto-pay ({mode}) for: {email or session_file}")
    result = run_auto_pay(
        email=email,
        session_file=session_file,
        proxy=args.proxy,
        headless=args.auto_pay_headless,
        timeout=args.auto_pay_timeout,
        reverse_only=reverse_only,
    )

    if result.get("ok"):
        print(f"\n[*] Auto-pay completed successfully!")
        print(f"    Email: {result.get('email', '')}")
        print(f"    Alias: {result.get('alias_email', '')}")
        print("    Card: [REDACTED]")
        print(f"    Status: {result.get('paypal_status', '')}")
        print(f"    Session: {result.get('json_path', '')}")
    else:
        print(f"\n[!] Auto-pay failed: {result.get('error', 'unknown error')}")
        if result.get("failed_step"):
            print(f"    Failed step: {result['failed_step']}")

    print(json.dumps(result, ensure_ascii=False, indent=2))


def batch_auto_pay(args: Any) -> None:
    """Run automated PayPal payment for all pending accounts."""
    from ..paypal_auto import auto_pay as run_auto_pay
    from ..storage import list_paypal_accounts

    limit = max(0, int(args.batch_auto_pay_limit or 0))

    # Get accounts with pending PayPal status
    all_accounts = list_paypal_accounts()
    pending = [
        row for row in all_accounts
        if row.get("paypal_status") in ("", "missing", "failed", "link_ready")
        and row.get("access_token")
    ]

    if limit > 0:
        pending = pending[:limit]

    if not pending:
        print("[*] No pending accounts found for auto-pay")
        return

    total = len(pending)
    print(f"[*] Batch auto-pay: {total} account(s) to process")
    print("=" * 60)

    results = []
    for i, row in enumerate(pending, 1):
        email = row.get("email", "")
        print(f"[{i}/{total}] Processing: {email}")
        print("-" * 40)

        result = run_auto_pay(
            email=email,
            proxy=args.proxy,
            headless=args.auto_pay_headless,
            timeout=args.auto_pay_timeout,
        )
        results.append(result)

        if result.get("ok"):
            print(f"[OK] {email} - Payment completed")
        else:
            print(f"[FAIL] {email} - {result.get('error', 'unknown')}")

        # Small delay between accounts
        if i < total:
            time.sleep(5)

    # Summary
    print("" + "=" * 60)

    print("Batch Auto-Pay Summary:")
    print("=" * 60)
    ok_count = sum(1 for r in results if r.get("ok"))
    fail_count = total - ok_count
    print(f"  Total: {total}")
    print(f"  Success: {ok_count}")
    print(f"  Failed: {fail_count}")

    if fail_count > 0:
        print("Failed accounts:")

        for r in results:
            if not r.get("ok"):
                print(f"  - {r.get('email', 'unknown')}: {r.get('error', 'unknown')}")


def list_paypal_ba_queue(args: Any) -> None:
    from ..desktop_ipc import emit_result
    from ..paypal_authorization_queue import list_paypal_ba_authorizations

    items = list_paypal_ba_authorizations(
        limit=max(0, int(getattr(args, "paypal_ba_queue_limit", 0) or 0))
    )
    emit_result(
        {"ok": True, "total": len(items), "results": items},
        enabled=bool(getattr(args, "desktop_ipc", False)),
    )


def process_paypal_ba_queue(args: Any) -> None:
    from ..desktop_ipc import emit_event, emit_result
    from ..paypal_authorization_queue import process_paypal_ba_authorizations
    from ..paypal_auto import auto_pay as run_auto_pay

    def authorize(item: dict[str, Any]) -> dict[str, Any]:
        return run_auto_pay(
            email=str(item.get("email") or ""),
            approval_url=str(item.get("approval_url") or ""),
            proxy=getattr(args, "proxy", None),
            headless=bool(getattr(args, "auto_pay_headless", False)),
            timeout=int(getattr(args, "auto_pay_timeout", 180) or 180),
            reverse_only=bool(getattr(args, "auto_pay_reverse_only", False)),
        )

    result = process_paypal_ba_authorizations(
        authorize,
        limit=max(0, int(getattr(args, "paypal_ba_queue_limit", 0) or 0)),
        progress=lambda event: emit_event(event),
    )
    emit_result(result, enabled=bool(getattr(args, "desktop_ipc", False)))
