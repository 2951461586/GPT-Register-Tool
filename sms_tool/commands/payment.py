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
from pathlib import Path
from typing import Any

from ..payment_routing import (
    PaymentRoutePlanner,
    method_payment_config as canonical_method_payment_config,
    parse_proxy_pool as canonical_parse_proxy_pool,
    payment_proxy_pools as canonical_payment_proxy_pools,
)


@dataclass(frozen=True)
class PaymentCommandContext:
    """Legacy CLI hooks required by payment command orchestration."""

    read_email_file: Callable[[str], list[str]]
    payment_method: Callable[[Any], str]
    resolve_access_token: Callable[[Any], tuple[str, Any]]
    payment_stage_args: Callable[..., tuple[Any, Any, Any, Any]]
    promotion_proxy_arg: Callable[..., Any]
    stage_country_overrides: Callable[..., dict[str, str]]
    payment_country: Callable[..., str]
    protocol_proxy_pool: Callable[[], list[str]]
    has_explicit_payment_proxy: Callable[[Any], bool]
    payment_proxy_pools: Callable[[str], Mapping[str, Any]] | None = None
    runtime_config: Mapping[str, Any] | None = None


def protocol_proxy_pool(config: Mapping[str, Any]) -> list[str]:
    protocol = config.get("protocol_payments") if isinstance(config.get("protocol_payments"), dict) else {}
    configured = protocol.get("proxy_pool") or []
    return parse_proxy_pool(configured)


def parse_proxy_pool(value: Any) -> list[str]:
    """Normalize a proxy-pool option from comma/newline text or a sequence."""
    return canonical_parse_proxy_pool(value)


def payment_proxy_pools(config: Mapping[str, Any], payment_method: str) -> dict[str, list[str]]:
    """Return method-owned checkout/approve proxy pools.

    The canonical location is ``protocol_payments.methods.<method>``.  A
    legacy top-level method section is merged first so existing single-value
    configurations continue to work without inheriting another method's pool.
    """
    return canonical_payment_proxy_pools(config, payment_method)


def has_explicit_payment_proxy(args: Any) -> bool:
    return bool(getattr(args, "proxy_explicit", False) or any(
        parse_proxy_pool(getattr(args, name, None))
        for name in (
            "checkout_proxy", "checkout_proxy_pool", "provider_proxy",
            "approve_proxy", "approve_proxy_pool", "promotion_proxy",
        )
    ))


def payment_country(payment_method: str, explicit: str = "") -> str:
    value = str(explicit or "").strip().upper()
    if value:
        return value

    from ..payment_link_manager import PAYMENT_METHODS

    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    try:
        from ..config import CFG
        protocol = CFG.get("protocol_payments") if isinstance(CFG.get("protocol_payments"), Mapping) else {}
        methods = protocol.get("methods") if isinstance(protocol.get("methods"), Mapping) else {}
        configured = methods.get(method) if isinstance(methods.get(method), Mapping) else {}
        countries = configured.get("stage_proxy_countries") if isinstance(configured.get("stage_proxy_countries"), Mapping) else {}
        canonical = str(countries.get("checkout") or configured.get("checkout_country") or "").strip().upper()
        if canonical:
            return canonical
    except Exception:
        pass
    spec = PAYMENT_METHODS.get(method)
    return spec.country if spec else "US"


def _method_payment_config(
    config: Mapping[str, Any],
    payment_method: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the method-owned payment config and merged stage proxies.

    ``protocol_payments.methods`` is the canonical owner.  A method's legacy
    top-level section remains a compatibility source for that same method, but
    a non-PayPal method must never inherit PayPal's stage routes.
    """
    method_cfg = canonical_method_payment_config(config, payment_method)
    stages = method_cfg.get("stage_proxies")
    return method_cfg, dict(stages) if isinstance(stages, Mapping) else {}


def _explicit_proxy_arg(args: Any) -> str | None:
    raw = str(getattr(args, "proxy", None) or "").strip()
    if not raw or not getattr(args, "proxy_explicit", False):
        return None

    from ..proxy_entry import resolve_proxy_value

    resolved = resolve_proxy_value(raw)
    if not resolved:
        raise ValueError("invalid --proxy value")
    return resolved


def payment_stage_args(
    args: Any,
    payment_method: str,
    config: Mapping[str, Any],
    *,
    apply_country_overrides: Callable[..., tuple[Any, Any, Any, Any]] | None = None,
) -> tuple[Any, Any, Any, Any]:
    explicit_proxy = _explicit_proxy_arg(args)
    explicit_checkout = (getattr(args, "checkout_proxy", None) or "").strip() or None
    explicit_provider = (getattr(args, "provider_proxy", None) or "").strip() or None
    explicit_approve = (getattr(args, "approve_proxy", None) or "").strip() or None
    method_cfg, method_stage = _method_payment_config(config, payment_method)
    configured_proxy = str(method_cfg.get("proxy") or "").strip() or None

    # An explicit shared proxy deliberately replaces configured stage routes.
    # Explicit per-stage options still win independently when no shared proxy
    # was supplied.
    proxy = explicit_proxy or configured_proxy
    if explicit_proxy:
        checkout_proxy = explicit_checkout
        provider_proxy = explicit_provider
        approve_proxy = explicit_approve
    else:
        checkout_proxy = (
            explicit_checkout
            or method_stage.get("checkout")
            or method_cfg.get("checkout_proxy")
            or None
        )
        provider_proxy = (
            explicit_provider
            or method_stage.get("provider")
            or method_stage.get("stripe_init")
            or method_cfg.get("provider_proxy")
            or method_cfg.get("stripe_init_proxy")
            or None
        )
        approve_proxy = (
            explicit_approve
            or method_stage.get("approve")
            or method_stage.get("confirm")
            or method_cfg.get("approve_proxy")
            or method_cfg.get("confirm_proxy")
            or provider_proxy
            or None
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

    method_cfg, method_stage = _method_payment_config(config, payment_method)
    resolved = (
        method_stage.get("promotion")
        or method_stage.get("promotion_update")
        or method_stage.get("update")
        or method_cfg.get("promotion_proxy")
        or method_cfg.get("update_proxy")
    )
    value = (str(resolved).strip() or None) if resolved else None
    if value:
        from ..paypal_proxy import rotate_proxy_session

        country = (getattr(args, "promotion_proxy_country", None) or "").strip().upper()
        if country:
            value = rotate_proxy_session(value, country)
    return value


def stage_country_overrides(
    args: Any,
    payment_method: str = "paypal",
    config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    method = str(payment_method or "paypal").strip().lower().replace("-", "_")
    configured: Mapping[str, Any] = {}
    if config is not None:
        method_cfg, _ = _method_payment_config(config, method)
        value = method_cfg.get("stage_proxy_countries")
        configured = value if isinstance(value, Mapping) else {}

    countries = {
        str(key or "").strip().lower(): str(value or "").strip().upper()
        for key, value in configured.items()
        if str(value or "").strip()
    }
    if "promotion" not in countries:
        countries["promotion"] = (
            countries.get("promotion_update")
            or countries.get("update")
            or ("TH" if method == "gopay" else "")
        )
    for alias in ("promotion_update", "update"):
        countries.pop(alias, None)

    cli_values = {
        "checkout": (getattr(args, "checkout_proxy_country", None) or "").strip().upper(),
        "approve": (getattr(args, "approve_proxy_country", None) or "").strip().upper(),
        "promotion": (getattr(args, "promotion_proxy_country", None) or "").strip().upper(),
    }
    countries.update({key: value for key, value in cli_values.items() if value})
    return {key: value for key, value in countries.items() if value}


def resolve_payment_route(
    args: Any,
    payment_method: str,
    context: PaymentCommandContext,
) -> dict[str, Any]:
    """Compile one immutable route plan before authentication starts."""
    proxy, checkout_proxy, provider_proxy, approve_proxy = context.payment_stage_args(args, payment_method)
    # The desktop proxy-pool workflow deliberately does not accept a manual
    # exit-country override.  The planner derives the required country from
    # the payment method and probes the pool before any side effect.
    countries = {} if getattr(args, "auto_proxy_country", False) else context.stage_country_overrides(args, payment_method)
    target_country = context.payment_country(payment_method, getattr(args, "target_country", ""))
    checkout_country = str(getattr(args, "checkout_country", "") or target_country).strip().upper()
    promotion_proxy = context.promotion_proxy_arg(args, payment_method)
    configured_pools = context.payment_proxy_pools(payment_method) if callable(context.payment_proxy_pools) else {}
    configured_pools = configured_pools if isinstance(configured_pools, Mapping) else {}
    explicit_checkout_route = any(
        str(getattr(args, name, None) or "").strip()
        for name in ("checkout_proxy", "provider_proxy", "stripe_init_proxy", "payment_method_proxy", "confirm_proxy")
    )
    explicit_approve_route = any(
        str(getattr(args, name, None) or "").strip()
        for name in ("approve_proxy", "promotion_proxy")
    )
    stage_pools = {
        "checkout": parse_proxy_pool(getattr(args, "checkout_proxy_pool", None))
        or ([] if explicit_checkout_route else parse_proxy_pool(configured_pools.get("checkout"))),
        "approve": parse_proxy_pool(getattr(args, "approve_proxy_pool", None))
        or ([] if explicit_approve_route else parse_proxy_pool(configured_pools.get("approve"))),
    }

    source = dict(context.runtime_config or {})
    protocol = source.get("protocol_payments") if isinstance(source.get("protocol_payments"), Mapping) else {}
    source["protocol_payments"] = {**dict(protocol), "proxy_pool": context.protocol_proxy_pool()}
    options: dict[str, Any] = {
        "target_country": target_country,
        "checkout_country": checkout_country,
        "approve_country": countries.get("approve") or "",
        "stage_proxy_countries": countries,
        "checkout_proxy_pool": stage_pools["checkout"],
        "approve_proxy_pool": stage_pools["approve"],
        "use_protocol_proxy_pool": True,
        "auto_proxy_country": bool(getattr(args, "auto_proxy_country", False)),
    }
    explicit_stages: dict[str, Any] = {}
    if not stage_pools["checkout"] and checkout_proxy:
        options["checkout_proxy"] = checkout_proxy
        explicit_stages.update({"auth_gate": checkout_proxy, "checkout": checkout_proxy})
    if not stage_pools["checkout"] and provider_proxy:
        options["provider_proxy"] = provider_proxy
        explicit_stages.update({
            "stripe_init": provider_proxy,
            "payment_method": provider_proxy,
            "confirm": provider_proxy,
            "redirect": provider_proxy,
            "poll": provider_proxy,
        })
    if not stage_pools["approve"] and approve_proxy:
        options["approve_proxy"] = approve_proxy
        explicit_stages["approve"] = approve_proxy
    if not stage_pools["approve"] and promotion_proxy:
        options["promotion_proxy"] = promotion_proxy
        explicit_stages["promotion"] = promotion_proxy
    elif not stage_pools["approve"] and approve_proxy:
        explicit_stages["promotion"] = approve_proxy
    if explicit_stages:
        options["stage_proxies"] = explicit_stages
    if bool(getattr(args, "proxy_explicit", False)) and proxy:
        options["proxy"] = proxy
        options["checkout_proxy_pool"] = []
        options["approve_proxy_pool"] = []

    try:
        plan = PaymentRoutePlanner(source).plan(
            payment_method,
            options=options,
            default_proxy=proxy,
        )
    except RuntimeError as exc:
        message = str(exc)
        stage = "approve" if "approve" in message else "checkout" if "checkout" in message else "payment"
        return {
            "ok": False,
            "error": "payment_proxy_pool_unavailable",
            "error_stage": f"{stage}_proxy_pool",
            "pool_stage": stage,
            "target_country": target_country,
            "attempts": [],
            "stage_pool_attempts": {},
        }

    adapter = plan.to_adapter_options()
    if provider_proxy and not stage_pools["checkout"]:
        adapter["provider_proxy"] = provider_proxy
    if promotion_proxy and not stage_pools["approve"]:
        promotion_country = str(countries.get("promotion") or "").strip().upper()
        if promotion_country:
            from ..paypal_proxy import rotate_proxy_session

            promotion_proxy = rotate_proxy_session(promotion_proxy, promotion_country)
        adapter["promotion_proxy"] = promotion_proxy
    stage_pool_attempts = {
        stage: [dict(item) for item in attempts]
        for stage, attempts in plan.attempts.items()
        if attempts
    }
    attempts = [item for values in stage_pool_attempts.values() for item in values]
    return {
        "ok": True,
        "proxy": plan.proxy_for("auth_gate") or plan.checkout_proxy,
        "checkout_proxy": adapter.get("checkout_proxy"),
        "provider_proxy": adapter.get("provider_proxy"),
        "approve_proxy": adapter.get("approve_proxy"),
        "promotion_proxy": adapter.get("promotion_proxy"),
        "stage_countries": countries,
        "checkout_proxy_pool": stage_pools["checkout"],
        "approve_proxy_pool": stage_pools["approve"],
        "target_country": target_country,
        "checkout_country": checkout_country,
        "used_pool": any(stage_pools.values()) or bool(context.protocol_proxy_pool()),
        "attempts": attempts,
        "stage_pool_attempts": stage_pool_attempts,
        "payment_route_plan": plan,
        "route_plan": plan.public_dict(),
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


def _method_dependency_map() -> dict[str, tuple[str, ...]]:
    """Importable packages each payment method's adapter requires (offline probe)."""
    curl = ("curl_cffi",)
    return {
        "paypal": curl,
        "upi": ("curl_cffi", "qrcode", "PIL"),
        "ideal": curl,
        "pix": curl,
        "kakao": curl,
        "blik": curl,
        "twint": curl,
        "direct_card": curl,
        "momo": curl,
        "gopay": curl,
        "gcash": curl,
        "grabpay": curl,
    }


def _probe_dependencies(packages: tuple[str, ...]) -> dict[str, bool]:
    from importlib.util import find_spec

    available = {}
    for package in packages:
        try:
            available[package] = find_spec(package) is not None
        except (ImportError, ValueError):
            available[package] = False
    return available


def list_payment_methods() -> None:
    from ..payment_link_manager import supported_payment_methods

    methods = supported_payment_methods()
    dependency_map = _method_dependency_map()
    for method in methods:
        if isinstance(method, dict):
            key = str(method.get("key") or "").strip().lower()
            if key in dependency_map:
                method["dependencies"] = _probe_dependencies(dependency_map[key])
    print(json.dumps({"ok": True, "methods": methods}, ensure_ascii=False, indent=2))


def test_payment_proxies(args: Any, context: PaymentCommandContext) -> None:
    from ..paypal_proxy import (
        probe_proxy,
        proxy_state_from_config,
        redact_proxy_url,
        rotate_proxy_session,
        select_proxy_from_pool,
    )
    from ..config import current_config_data

    proxy_state = proxy_state_from_config(current_config_data())
    # This is an interactive diagnostic, not a payment attempt. Keep each
    # network leg bounded so a dead proxy cannot leave the modal apparently
    # frozen for several minutes across scheme and provider fallbacks.
    probe_timeout = 3.0
    method = context.payment_method(args)
    proxy, checkout_proxy, _, approve_proxy = context.payment_stage_args(args, method)
    promotion_proxy = context.promotion_proxy_arg(args, method)
    countries = {} if getattr(args, "auto_proxy_country", False) else context.stage_country_overrides(args, method)
    default_country = context.payment_country(method, getattr(args, "target_country", ""))
    pool = context.protocol_proxy_pool()
    configured_pools = (
        context.payment_proxy_pools(method)
        if callable(context.payment_proxy_pools)
        else {}
    )
    configured_pools = configured_pools if isinstance(configured_pools, Mapping) else {}
    stage_pools = {
        "checkout": parse_proxy_pool(getattr(args, "checkout_proxy_pool", None))
        or parse_proxy_pool(configured_pools.get("checkout")),
        "approve": parse_proxy_pool(getattr(args, "approve_proxy_pool", None))
        or parse_proxy_pool(configured_pools.get("approve")),
    }
    use_pool = (
        not context.has_explicit_payment_proxy(args)
        and not (proxy or checkout_proxy)
        and bool(pool)
    )
    stage_values = {
        "checkout": checkout_proxy or proxy,
        "approve": approve_proxy or proxy,
        "update": promotion_proxy or proxy,
    }
    stage_country_keys = {"checkout": "checkout", "approve": "approve", "update": "promotion"}
    stages: dict[str, Any] = {}
    for stage, proxy in stage_values.items():
        expected = countries.get(stage_country_keys[stage], "") or default_country
        # GoPay's promotion stage is a provider contract rather than a user
        # setting; retain the canonical backend rule in automatic mode.
        if getattr(args, "auto_proxy_country", False) and method == "gopay":
            if stage == "approve":
                expected = "JP"
            elif stage == "update":
                expected = "TH"
        candidate = proxy or ""
        attempts = []
        result = None
        stage_pool = stage_pools.get("approve" if stage in {"approve", "update"} else "checkout") or (
            pool if use_pool else []
        )
        if stage_pool:
            candidate, attempts = select_proxy_from_pool(
                stage_pool, expected, stage, state=proxy_state, timeout=probe_timeout
            )
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
            stages[stage] = {
                **attempts[-1],
                "proxy": redact_proxy_url(candidate),
                "attempts": attempts,
            }
            continue
        for attempt in range(1, 3):
            if attempt > 1 and candidate:
                candidate = rotate_proxy_session(candidate, expected)
            result = probe_proxy(
                candidate,
                expected_country=expected,
                stage=stage,
                state=proxy_state,
                timeout=probe_timeout,
            )
            attempts.append({"attempt": attempt, "ok": result.ok, "error": result.error})
            if result.ok:
                break
        stages[stage] = {
            **result.to_dict(),
            "proxy": redact_proxy_url(candidate),
            "attempts": attempts,
        }
    ok = all(bool(item.get("ok")) for item in stages.values())
    payload = {"ok": ok, "payment_method": method, "stages": stages}
    desktop_ipc = bool(getattr(args, "desktop_ipc", False))
    if desktop_ipc:
        from ..desktop_ipc import emit_result

        emit_result(payload, enabled=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # A failed probe is a valid desktop result (rendered per stage); only the
    # plain CLI path signals it through a non-zero exit for scripting.
    if not ok and not desktop_ipc:
        raise SystemExit(3)


def extract_payment_link(args: Any, context: PaymentCommandContext) -> None:
    """Extract a supported protocol payment link from an AT or saved account."""
    from ..desktop_ipc import emit_event, emit_result
    from ..payment_link_manager import generate_payment_link

    def output(payload: Any) -> None:
        emit_result(payload, enabled=bool(getattr(args, "desktop_ipc", False)))

    method = context.payment_method(args)

    def payment_progress(event: Mapping[str, Any]) -> None:
        emit_event({"domain": "payment", **dict(event or {})})

    def selected_route() -> dict[str, Any]:
        try:
            route = resolve_payment_route(args, method, context)
        except ValueError as exc:
            output({"ok": False, "error": str(exc)})
            raise SystemExit(2) from exc
        if not route.get("ok"):
            output(route)
            raise SystemExit(3)
        return route

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
        token_map = {}
        token_map_file = str(getattr(args, "payment_token_map", None) or "").strip()
        if token_map_file:
            try:
                loaded = json.loads(Path(token_map_file).read_text(encoding="utf-8-sig"))
                token_map = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, TypeError):
                token_map = {}
        route = selected_route()
        payment_kwargs = {
            "checkout_proxy": route["checkout_proxy"],
            "checkout_proxy_pool": route.get("checkout_proxy_pool") or [],
            "provider_proxy": route["provider_proxy"],
            "stripe_init_proxy": getattr(args, "stripe_init_proxy", None),
            "payment_method_proxy": getattr(args, "payment_method_proxy", None),
            "confirm_proxy": getattr(args, "confirm_proxy", None),
            "approve_proxy": route["approve_proxy"],
            "approve_proxy_pool": route.get("approve_proxy_pool") or [],
            "redirect_proxy": getattr(args, "redirect_proxy", None),
            "promotion_proxy": route["promotion_proxy"],
            "stage_proxy_countries": route["stage_countries"],
            "auto_proxy_country": bool(getattr(args, "auto_proxy_country", False)),
            "target_country": route["target_country"],
            "checkout_country": route["checkout_country"],
            "require_zero": not getattr(args, "no_require_zero", False),
        }
        try:
            report = run_payment_batch(
                emails,
                payment_method=method,
                workers=getattr(args, "workers", 1),
                batch_id=getattr(args, "payment_batch_id", "") or "",
                proxy=route["proxy"],
                payment_kwargs=payment_kwargs,
                jit_refresh=not getattr(args, "no_jit_at_refresh", False),
                probe_only=bool(getattr(args, "payment_probe_only", False)),
                matrix=getattr(args, "payment_matrix", None),
                canary=getattr(args, "payment_canary", 0),
                retries=getattr(args, "payment_retries", 3),
                timeout=getattr(args, "refresh_timeout", 30),
                progress=payment_progress,
                access_tokens=token_map,
                resume_checkpoint=bool(getattr(args, "payment_resume_checkpoint", False)),
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

    route: dict[str, Any] | None = None
    at = ""
    auth_context = None
    if not str(getattr(args, "at", None) or "").strip() and (
        getattr(args, "email", None) or getattr(args, "session_file", None)
    ):
        from ..payment_auth import ensure_payment_access_token, public_payment_auth_result

        legacy_at, legacy_context = context.resolve_access_token(args)
        route = selected_route()
        auth = ensure_payment_access_token(
            email=str(getattr(args, "email", None) or ""),
            session_file=str(getattr(args, "session_file", None) or ""),
            proxy=route["proxy"],
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
    if route is None:
        route = selected_route()

    kwargs = {
        "payment_route_plan": route.get("payment_route_plan"),
        "checkout_proxy": route["checkout_proxy"],
        "checkout_proxy_pool": route.get("checkout_proxy_pool") or [],
        "provider_proxy": route["provider_proxy"],
        "stripe_init_proxy": getattr(args, "stripe_init_proxy", None),
        "payment_method_proxy": getattr(args, "payment_method_proxy", None),
        "confirm_proxy": getattr(args, "confirm_proxy", None),
        "approve_proxy": route["approve_proxy"],
        "approve_proxy_pool": route.get("approve_proxy_pool") or [],
        "redirect_proxy": getattr(args, "redirect_proxy", None),
        "promotion_proxy": route["promotion_proxy"],
        "stage_proxy_countries": route["stage_countries"],
        "auto_proxy_country": bool(getattr(args, "auto_proxy_country", False)),
        "require_zero": not getattr(args, "no_require_zero", False),
        "probe_only": bool(getattr(args, "payment_probe_only", False)),
    }
    if route["target_country"]:
        kwargs["target_country"] = route["target_country"]
    if route["checkout_country"]:
        kwargs["checkout_country"] = route["checkout_country"]
    if method == "paypal":
        kwargs["require_ba_token"] = bool(getattr(args, "require_ba_token", False))
    if method == "upi":
        kwargs["payment_country"] = (getattr(args, "payment_country", None) or "IN").strip().upper()
        kwargs["qr_path"] = getattr(args, "qr_path", None)
    if method == "blik":
        kwargs["blik_code"] = getattr(args, "blik_code", None)

    result = generate_payment_link(
        access_token=at,
        proxy=route["proxy"],
        payment_method=method,
        auth_context=auth_context,
        paypal_generation_type=getattr(args, "paypal_generation_type", None),
        progress=payment_progress,
        **kwargs,
    )
    if method == "paypal" and result.get("ok") and result.get("url"):
        try:
            from ..paypal_authorization_queue import enqueue_paypal_ba_authorization

            queued = enqueue_paypal_ba_authorization(
                email=str(getattr(args, "email", None) or ""),
                approval_url=str(result.get("url") or ""),
                batch_id=str(getattr(args, "payment_batch_id", None) or ""),
            )
            result["authorization_queued"] = True
            result["authorization_queue_id"] = queued.get("id", "")
            result["authorization_status"] = queued.get("status", "pending")
        except ValueError:
            pass
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)
