"""CLI boundary for direct payment-link commands driven by an Access Token.

All payment-link generation routes through ``sms_tool.payment.link_manager``
(backed by the external Paylink Extraction Server).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..payment.link_manager import generate_payment_link


@dataclass(frozen=True)
class PaymentLinkCommandContext:
    """Legacy CLI hooks required by payment-link command orchestration."""

    payment_stage_args: Callable[..., tuple[Any, Any, Any, Any]]
    promotion_proxy_arg: Callable[..., Any]
    stage_country_overrides: Callable[..., dict[str, str]]
    runtime_config: Mapping[str, Any]


def _clamped_int(value: Any) -> int:
    """Best-effort non-negative int for CLI limits; malformed input falls back to 0."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _reverse_pay_adapter() -> Callable[..., dict[str, Any]]:
    """Resolve the reverse-pay adapter lazily, at call time."""
    from ..paypal.reverse import try_reverse_pay

    return try_reverse_pay


def _paypal_link_factory(access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Adapter factory for ``paypal.auto_pay`` backed by the unified link manager."""
    return generate_payment_link(access_token, proxy=proxy, payment_method="paypal", **kwargs)


def generate_ba_link(args: Any, ctx: PaymentLinkCommandContext) -> None:
    """Generate PayPal payment link from Access Token via the unified extraction server."""
    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "missing --at (Access Token)"}))
        raise SystemExit(1)

    _paypal_raw = ctx.runtime_config.get("paypal")
    paypal_cfg: dict[str, Any] = _paypal_raw if isinstance(_paypal_raw, dict) else {}
    _paypal_regions = paypal_cfg.get("billing_regions")
    regions: list[Any] = _paypal_regions if isinstance(_paypal_regions, list) else []
    target_country = (getattr(args, "target_country", None) or paypal_cfg.get("target_country") or "US").strip().upper()
    checkout_country = (
        (
            getattr(args, "checkout_country", None)
            or (regions[0] if regions else None)
            or paypal_cfg.get("checkout_country")
            or paypal_cfg.get("billing_country")
            or target_country
            or "US"
        )
        .strip()
        .upper()
    )
    proxy, checkout_proxy, provider_proxy, approve_proxy = ctx.payment_stage_args(args, "paypal")
    promotion_proxy = ctx.promotion_proxy_arg(args, "paypal")
    require_zero = not getattr(args, "no_require_zero", False)
    require_ba_token = bool(getattr(args, "require_ba_token", False))

    result = generate_payment_link(
        access_token=at,
        proxy=proxy,
        payment_method="paypal",
        runtime_config=ctx.runtime_config,
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
    """Generate UPI payment link / QR from Access Token via the unified extraction server."""
    at = (getattr(args, "at", None) or "").strip()
    if not at:
        print(json.dumps({"ok": False, "error": "missing --at (Access Token)"}))
        raise SystemExit(1)

    _upi_raw = ctx.runtime_config.get("upi")
    upi_cfg: dict[str, Any] = _upi_raw if isinstance(_upi_raw, dict) else {}
    _upi_regions = upi_cfg.get("billing_regions")
    regions: list[Any] = _upi_regions if isinstance(_upi_regions, list) else []
    checkout_country = (
        (
            getattr(args, "checkout_country", None)
            or getattr(args, "target_country", None)
            or upi_cfg.get("checkout_country")
            or upi_cfg.get("checkout_billing_country")
            or upi_cfg.get("billing_country")
            or upi_cfg.get("target_country")
            or (regions[0] if regions else None)
            or "IN"
        )
        .strip()
        .upper()
    )
    payment_country = (
        (
            getattr(args, "payment_country", None)
            or upi_cfg.get("payment_country")
            or upi_cfg.get("payment_billing_country")
            or "IN"
        )
        .strip()
        .upper()
    )
    proxy, checkout_proxy, provider_proxy, approve_proxy = ctx.payment_stage_args(args, "upi")
    require_zero = not getattr(args, "no_require_zero", False)
    result = generate_payment_link(
        access_token=at,
        proxy=proxy,
        payment_method="upi",
        runtime_config=ctx.runtime_config,
        checkout_proxy=checkout_proxy,
        provider_proxy=provider_proxy,
        approve_proxy=approve_proxy,
        target_country=checkout_country,
        checkout_country=checkout_country,
        payment_country=payment_country,
        require_zero=require_zero,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def auto_pay(args: Any) -> None:
    """Run automated PayPal payment for a ChatGPT account."""
    from ..paypal import auto_pay as run_auto_pay

    email = (args.email or "").strip()
    session_file = (args.session_file or "").strip()
    if not email and not session_file:
        print("[Error] --email or --session-file is required with --auto-pay")
        return

    reverse_only = getattr(args, "auto_pay_reverse_only", False)
    mode = "reverse-only" if reverse_only else "reverse+browser"
    print(f"[*] Starting auto-pay ({mode}) for: {email or session_file}")
    result = run_auto_pay(
        email=email,
        session_file=session_file,
        proxy=args.proxy,
        headless=args.auto_pay_headless,
        timeout=args.auto_pay_timeout,
        reverse_only=reverse_only,
        link_factory=_paypal_link_factory,
        reverse_pay=_reverse_pay_adapter(),
    )

    if result.get("ok"):
        print("\n[*] Auto-pay completed successfully!")
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
    from ..paypal import auto_pay as run_auto_pay
    from ..storage import list_paypal_accounts

    limit = _clamped_int(args.batch_auto_pay_limit)

    all_accounts = list_paypal_accounts()
    pending = [
        row
        for row in all_accounts
        if row.get("paypal_status") in ("", "missing", "failed", "link_ready") and row.get("access_token")
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
            link_factory=_paypal_link_factory,
            reverse_pay=_reverse_pay_adapter(),
        )
        results.append(result)

        if result.get("ok"):
            print(f"[OK] {email} - Payment completed")
        else:
            print(f"[FAIL] {email} - {result.get('error', 'unknown')}")

        if i < total:
            time.sleep(5)

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
    from ..desktop.ipc import emit_result
    from ..paypal.authorization_queue import list_paypal_ba_authorizations

    items = list_paypal_ba_authorizations(limit=_clamped_int(getattr(args, "paypal_ba_queue_limit", 0)))
    emit_result(
        {"ok": True, "total": len(items), "results": items},
        enabled=bool(getattr(args, "desktop_ipc", False)),
    )


def process_paypal_ba_queue(args: Any) -> None:
    from ..desktop.ipc import emit_event, emit_result
    from ..paypal import auto_pay as run_auto_pay
    from ..paypal.authorization_queue import process_paypal_ba_authorizations

    def authorize(item: dict[str, Any]) -> dict[str, Any]:
        return run_auto_pay(
            email=str(item.get("email") or ""),
            approval_url=str(item.get("approval_url") or ""),
            proxy=getattr(args, "proxy", None),
            headless=bool(getattr(args, "auto_pay_headless", False)),
            timeout=_clamped_int(getattr(args, "auto_pay_timeout", 180)) or 180,
            reverse_only=bool(getattr(args, "auto_pay_reverse_only", False)),
            reverse_pay=_reverse_pay_adapter(),
        )

    def _progress(event: dict[str, Any]) -> None:
        emit_event(event)

    result = process_paypal_ba_authorizations(
        authorize,
        limit=_clamped_int(getattr(args, "paypal_ba_queue_limit", 0)),
        progress=_progress,
    )
    emit_result(result, enabled=bool(getattr(args, "desktop_ipc", False)))
