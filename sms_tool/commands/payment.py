"""CLI boundary for protocol-payment commands.

The payment domain modules own checkout, provider, and persistence behavior.
This module only translates ``argparse`` values into those domain calls and
formats command results.  ``PaymentCommandContext`` keeps the legacy CLI's
replaceable hooks explicit so callers and tests do not depend on module globals.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaymentCommandContext:
    """Legacy CLI hooks required by payment command orchestration."""

    read_email_file: Callable[[str], list[str]]
    payment_method: Callable[[Any], str]
    resolve_access_token: Callable[[Any], tuple[str, Any]]
    payment_stage_args: Callable[..., tuple[Any, Any, Any, Any]]
    promotion_proxy_arg: Callable[..., Any]
    stage_country_overrides: Callable[[Any], dict[str, str]]
    payment_country: Callable[..., str]
    protocol_proxy_pool: Callable[[], list[str]]
    has_explicit_payment_proxy: Callable[[Any], bool]


def regenerate_workers(
    args: Any,
    payment_method: str,
    total: int,
    config: Mapping[str, Any],
) -> int:
    requested = max(1, int(getattr(args, "workers", 1) or 1))
    cfg = config.get(payment_method) if isinstance(config.get(payment_method), dict) else {}
    if payment_method == "paypal":
        cfg = config.get("paypal") if isinstance(config.get("paypal"), dict) else {}
    configured = cfg.get("max_regenerate_workers", cfg.get("regenerate_workers"))
    try:
        cap = int(configured)
    except (TypeError, ValueError):
        cap = 4
    return max(1, min(requested, max(1, cap), total))


def regenerate_delay_seconds(payment_method: str, config: Mapping[str, Any]) -> float:
    cfg = config.get(payment_method) if isinstance(config.get(payment_method), dict) else {}
    if payment_method == "paypal":
        cfg = config.get("paypal") if isinstance(config.get("paypal"), dict) else {}
    try:
        return max(0.0, float(cfg.get("regenerate_delay_seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def protocol_proxy_pool(config: Mapping[str, Any]) -> list[str]:
    protocol = config.get("protocol_payments") if isinstance(config.get("protocol_payments"), dict) else {}
    configured = protocol.get("proxy_pool") or []
    if isinstance(configured, str):
        configured = re.split(r"[\r\n,;]+", configured)
    return list(dict.fromkeys(
        str(item or "").strip()
        for item in configured
        if str(item or "").strip()
    ))


def has_explicit_payment_proxy(args: Any) -> bool:
    return bool(getattr(args, "proxy_explicit", False) or any(
        str(getattr(args, name, "") or "").strip()
        for name in ("checkout_proxy", "provider_proxy", "approve_proxy", "promotion_proxy")
    ))


def payment_country(payment_method: str, explicit: str = "") -> str:
    value = str(explicit or "").strip().upper()
    if value:
        return value

    from ..payment_link_manager import PAYMENT_METHODS

    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    spec = PAYMENT_METHODS.get(method)
    return spec.country if spec else "US"


def payment_stage_args(
    args: Any,
    payment_method: str,
    config: Mapping[str, Any],
    *,
    apply_country_overrides: Callable[..., tuple[Any, Any, Any, Any]] | None = None,
) -> tuple[Any, Any, Any, Any]:
    proxy = (getattr(args, "proxy", None) or "").strip() or None
    if not getattr(args, "proxy_explicit", False):
        proxy = None
    explicit_checkout = (getattr(args, "checkout_proxy", None) or "").strip() or None
    explicit_provider = (getattr(args, "provider_proxy", None) or "").strip() or None
    explicit_approve = (getattr(args, "approve_proxy", None) or "").strip() or None
    has_country_override = any((
        (getattr(args, "checkout_proxy_country", None) or "").strip(),
        (getattr(args, "approve_proxy_country", None) or "").strip(),
    ))
    if not has_country_override and (proxy or explicit_checkout or explicit_provider or explicit_approve):
        return proxy, explicit_checkout, explicit_provider, explicit_approve

    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    method_cfg = config.get(method) if isinstance(config.get(method), dict) else {}
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    paypal_cfg = config.get("paypal") if isinstance(config.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    proxy_default = (config.get("proxy") or {}).get("default") or ""

    checkout_proxy = explicit_checkout or method_stage.get("checkout") or paypal_stage.get("checkout") or proxy or proxy_default
    if method == "upi":
        provider_proxy = (
            explicit_provider
            or method_stage.get("provider")
            or method_stage.get("stripe_init")
            or paypal_stage.get("provider")
            or paypal_stage.get("stripe_init")
            or "http://107.150.109.49:11001"
        )
        approve_proxy = (
            explicit_approve
            or method_stage.get("approve")
            or method_stage.get("confirm")
            or paypal_stage.get("approve")
            or paypal_stage.get("confirm")
            or provider_proxy
            or "http://107.150.109.49:11001"
        )
    else:
        provider_proxy = (
            explicit_provider
            or method_stage.get("provider")
            or method_stage.get("stripe_init")
            or paypal_stage.get("provider")
            or paypal_stage.get("stripe_init")
            or proxy_default
        )
        approve_proxy = (
            explicit_approve
            or method_stage.get("approve")
            or method_stage.get("confirm")
            or paypal_stage.get("approve")
            or paypal_stage.get("confirm")
            or provider_proxy
            or proxy_default
        )
    apply_overrides = apply_country_overrides or apply_stage_country_overrides
    return apply_overrides(
        args,
        proxy,
        checkout_proxy,
        provider_proxy,
        approve_proxy,
    )


def apply_stage_country_overrides(
    args: Any,
    proxy: Any,
    checkout_proxy: Any,
    provider_proxy: Any,
    approve_proxy: Any,
) -> tuple[Any, Any, Any, Any]:
    from ..paypal_proxy import rotate_proxy_session

    def apply(value: Any, option: str) -> Any:
        country = (getattr(args, option, None) or "").strip().upper()
        return rotate_proxy_session(value, country) if value and country else value

    return (
        proxy,
        apply(checkout_proxy, "checkout_proxy_country"),
        provider_proxy,
        apply(approve_proxy, "approve_proxy_country"),
    )


def promotion_proxy_arg(
    args: Any,
    payment_method: str,
    config: Mapping[str, Any],
) -> Any:
    """Resolve the optional promotion-update stage proxy."""
    explicit = (getattr(args, "promotion_proxy", None) or "").strip()
    if explicit:
        from ..paypal_proxy import rotate_proxy_session

        country = (getattr(args, "promotion_proxy_country", None) or "").strip().upper()
        return rotate_proxy_session(explicit, country) if country else explicit

    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    method_cfg = config.get(method) if isinstance(config.get(method), dict) else {}
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    paypal_cfg = config.get("paypal") if isinstance(config.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    resolved = (
        method_stage.get("promotion")
        or method_stage.get("promotion_update")
        or paypal_stage.get("promotion")
        or paypal_stage.get("promotion_update")
    )
    value = (str(resolved).strip() or None) if resolved else None
    if value:
        from ..paypal_proxy import rotate_proxy_session

        country = (getattr(args, "promotion_proxy_country", None) or "").strip().upper()
        if country:
            value = rotate_proxy_session(value, country)
    return value


def stage_country_overrides(args: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "checkout": (getattr(args, "checkout_proxy_country", None) or "").strip().upper(),
            "approve": (getattr(args, "approve_proxy_country", None) or "").strip().upper(),
            "promotion": (getattr(args, "promotion_proxy_country", None) or "").strip().upper(),
        }.items()
        if value
    }


def resolve_access_token(args: Any, *, stderr: Any = None) -> tuple[str, Any]:
    at = (getattr(args, "at", None) or "").strip()
    if at:
        return at, None

    email = (getattr(args, "email", None) or "").strip()
    session_file = (getattr(args, "session_file", None) or "").strip()
    if not email and not session_file:
        return "", None

    from ..session_refresh import _load_seed_session

    with contextlib.redirect_stdout(stderr or sys.stderr):
        data, _ = _load_seed_session(email=email, session_file=session_file)
    if not isinstance(data, dict):
        return "", None

    def nested(mapping: Any, *keys: str) -> str:
        value = mapping
        for key in keys:
            if not isinstance(value, dict):
                return ""
            value = value.get(key)
        return str(value or "").strip()

    access_token = next((value for value in (
        str(data.get("access_token") or "").strip(),
        str(data.get("accessToken") or "").strip(),
        nested(data, "auth_session", "access_token"),
        nested(data, "auth_session", "accessToken"),
        nested(data, "session", "access_token"),
        nested(data, "session", "accessToken"),
    ) if value), "")
    return access_token, data


def list_payment_methods() -> None:
    from ..payment_link_manager import supported_payment_methods

    print(json.dumps({"ok": True, "methods": supported_payment_methods()}, ensure_ascii=False, indent=2))


def test_payment_proxies(args: Any, context: PaymentCommandContext) -> None:
    from ..paypal_proxy import probe_proxy, redact_proxy_url, rotate_proxy_session, select_proxy_from_pool

    method = context.payment_method(args)
    _, checkout_proxy, _, approve_proxy = context.payment_stage_args(args, method)
    promotion_proxy = context.promotion_proxy_arg(args, method)
    countries = context.stage_country_overrides(args)
    default_country = context.payment_country(method, getattr(args, "target_country", ""))
    pool = context.protocol_proxy_pool()
    use_pool = not context.has_explicit_payment_proxy(args) and bool(pool)
    stage_values = {
        "checkout": checkout_proxy,
        "approve": approve_proxy,
        "update": promotion_proxy,
    }
    stage_country_keys = {"checkout": "checkout", "approve": "approve", "update": "promotion"}
    stages: dict[str, Any] = {}
    for stage, proxy in stage_values.items():
        expected = countries.get(stage_country_keys[stage], "") or default_country
        candidate = proxy or ""
        attempts = []
        result = None
        if use_pool:
            candidate, attempts = select_proxy_from_pool(pool, expected, stage)
            if not candidate:
                stages[stage] = {
                    "ok": False,
                    "stage": stage,
                    "expected_country": expected,
                    "error": "payment_proxy_pool_unavailable",
                    "proxy": "DIRECT",
                    "attempts": attempts,
                }
                continue
            stages[stage] = {**attempts[-1], "proxy": redact_proxy_url(candidate), "attempts": attempts}
            continue
        for attempt in range(1, 4):
            if attempt > 1 and candidate:
                candidate = rotate_proxy_session(candidate, expected)
            result = probe_proxy(candidate, expected_country=expected, stage=stage)
            attempts.append({"attempt": attempt, "ok": result.ok, "error": result.error})
            if result.ok:
                break
        stages[stage] = {**result.to_dict(), "proxy": redact_proxy_url(candidate), "attempts": attempts}
    ok = all(bool(item.get("ok")) for item in stages.values())
    print(json.dumps({"ok": ok, "payment_method": method, "stages": stages}, ensure_ascii=False, indent=2))
    if not ok:
        raise SystemExit(3)


def extract_payment_link(args: Any, context: PaymentCommandContext) -> None:
    """Extract a supported protocol payment link from an AT or saved account."""
    from ..desktop_ipc import emit_result
    from ..payment_link_manager import generate_payment_link

    def output(payload: Any) -> None:
        emit_result(payload, enabled=bool(getattr(args, "desktop_ipc", False)))

    method = context.payment_method(args)
    email_file = str(getattr(args, "email_file", None) or "").strip()
    if email_file:
        if method == "blik" and not getattr(args, "payment_probe_only", False):
            output({
                "ok": False,
                "error": "BLIK is single-account only; use --email or --session-file with --blik-code",
            })
            raise SystemExit(2)

        from ..payment_batch import run_payment_batch

        emails = context.read_email_file(email_file)
        if getattr(args, "email", None):
            emails.insert(0, str(args.email).strip())
        if not emails:
            output({"ok": False, "error": "email file contains no accounts"})
            raise SystemExit(1)
        proxy, checkout_proxy, provider_proxy, approve_proxy = context.payment_stage_args(args, method)
        stage_countries = context.stage_country_overrides(args)
        target_country = context.payment_country(method, getattr(args, "target_country", ""))
        payment_kwargs = {
            "checkout_proxy": checkout_proxy,
            "provider_proxy": provider_proxy,
            "stripe_init_proxy": getattr(args, "stripe_init_proxy", None),
            "payment_method_proxy": getattr(args, "payment_method_proxy", None),
            "confirm_proxy": getattr(args, "confirm_proxy", None),
            "approve_proxy": approve_proxy,
            "redirect_proxy": getattr(args, "redirect_proxy", None),
            "promotion_proxy": context.promotion_proxy_arg(args, method),
            "stage_proxy_countries": stage_countries,
            "target_country": target_country,
            "checkout_country": str(getattr(args, "checkout_country", "") or target_country).strip().upper(),
            "require_zero": not getattr(args, "no_require_zero", False),
        }
        try:
            report = run_payment_batch(
                emails,
                payment_method=method,
                workers=getattr(args, "workers", 1),
                batch_id=getattr(args, "payment_batch_id", "") or "",
                proxy=proxy,
                payment_kwargs=payment_kwargs,
                jit_refresh=not getattr(args, "no_jit_at_refresh", False),
                probe_only=bool(getattr(args, "payment_probe_only", False)),
                matrix=getattr(args, "payment_matrix", None),
                canary=getattr(args, "payment_canary", 0),
                retries=getattr(args, "payment_retries", 1),
                timeout=getattr(args, "refresh_timeout", 30),
            )
        except RuntimeError as exc:
            output({"ok": False, "error": str(exc)})
            raise SystemExit(3)
        output(report)
        counts = report.get("counts", {})
        if (
            getattr(args, "payment_probe_only", False) and not report.get("ok")
        ) or (
            not getattr(args, "payment_probe_only", False) and not counts.get("completed")
        ):
            raise SystemExit(3)
        return

    at = ""
    auth_context = None
    if not str(getattr(args, "at", None) or "").strip() and (
        getattr(args, "email", None) or getattr(args, "session_file", None)
    ):
        from ..payment_auth import ensure_payment_access_token, public_payment_auth_result

        legacy_at, legacy_context = context.resolve_access_token(args)
        auth = ensure_payment_access_token(
            email=str(getattr(args, "email", None) or ""),
            session_file=str(getattr(args, "session_file", None) or ""),
            proxy=getattr(args, "proxy", None),
            timeout=min(max(10, int(getattr(args, "refresh_timeout", 30) or 30)), 300),
            relogin_on_401=not getattr(args, "no_jit_at_refresh", False),
        )
        if not auth.get("ok"):
            if auth.get("error") == "missing_access_token" and legacy_at:
                at, auth_context = legacy_at, legacy_context
            else:
                print(json.dumps(public_payment_auth_result(auth), ensure_ascii=False, indent=2))
                raise SystemExit(3)
        else:
            at = str(auth.get("access_token") or "")
            auth_context = auth.get("auth_context")
    else:
        at, auth_context = context.resolve_access_token(args)
    if not at:
        print(json.dumps({
            "ok": False,
            "error": "selected account has no Access Token" if (
                getattr(args, "email", None) or getattr(args, "session_file", None)
            ) else "missing --at (Access Token)",
        }, ensure_ascii=False))
        raise SystemExit(1)

    proxy, checkout_proxy, provider_proxy, approve_proxy = context.payment_stage_args(args, method)
    stage_countries = context.stage_country_overrides(args)
    target_country = context.payment_country(method, getattr(args, "target_country", ""))
    checkout_country = str(getattr(args, "checkout_country", "") or target_country).strip().upper()
    used_pool = False
    if not context.has_explicit_payment_proxy(args):
        defaults = context.protocol_proxy_pool()
        if defaults:
            from ..paypal_proxy import rotate_proxy_session, select_proxy_from_pool

            proxy, attempts = select_proxy_from_pool(defaults, target_country, "payment")
            if not proxy:
                print(json.dumps({
                    "ok": False,
                    "error": "payment_proxy_pool_unavailable",
                    "target_country": target_country,
                    "attempts": attempts,
                }, ensure_ascii=False, indent=2))
                raise SystemExit(3)
            checkout_proxy = rotate_proxy_session(proxy, stage_countries.get("checkout") or checkout_country)
            provider_proxy = rotate_proxy_session(proxy, target_country)
            approve_proxy = rotate_proxy_session(proxy, stage_countries.get("approve") or target_country)
            used_pool = True

    promotion_proxy = context.promotion_proxy_arg(args, method)
    if used_pool:
        from ..paypal_proxy import rotate_proxy_session

        promotion_proxy = rotate_proxy_session(proxy, stage_countries.get("promotion") or target_country)
    kwargs = {
        "checkout_proxy": checkout_proxy,
        "provider_proxy": provider_proxy,
        "stripe_init_proxy": getattr(args, "stripe_init_proxy", None),
        "payment_method_proxy": getattr(args, "payment_method_proxy", None),
        "confirm_proxy": getattr(args, "confirm_proxy", None),
        "approve_proxy": approve_proxy,
        "redirect_proxy": getattr(args, "redirect_proxy", None),
        "promotion_proxy": promotion_proxy,
        "stage_proxy_countries": stage_countries,
        "require_zero": not getattr(args, "no_require_zero", False),
        "probe_only": bool(getattr(args, "payment_probe_only", False)),
    }
    if target_country:
        kwargs["target_country"] = target_country
    if checkout_country:
        kwargs["checkout_country"] = checkout_country
    if method == "paypal":
        kwargs["require_ba_token"] = bool(getattr(args, "require_ba_token", False))
    if method == "upi":
        kwargs["payment_country"] = (getattr(args, "payment_country", None) or "IN").strip().upper()
        kwargs["qr_path"] = getattr(args, "qr_path", None)
    if method == "blik":
        kwargs["blik_code"] = getattr(args, "blik_code", None)

    result = generate_payment_link(
        access_token=at,
        proxy=proxy,
        payment_method=method,
        auth_context=auth_context,
        paypal_generation_type=getattr(args, "paypal_generation_type", None),
        **kwargs,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)
