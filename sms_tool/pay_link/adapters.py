"""adapters submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

from __future__ import annotations
import sms_tool.payment_link_manager as _plm
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from ..config import ConfigError, current_config_data, resolve_runtime_config, validate_config
from ..paths import project_path, runtime_file
from ..payment_contracts import PaymentRequest, PaymentResult, payment_history_metadata
from ..payment_catalog import PAYMENT_METHODS as CATALOG_METHODS, normalize_payment_method as normalize_catalog_payment_method, validate_catalog_consistency
from ..payment_adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from ..payment_executor import PaymentExecutionRequest, PaymentFlowExecutor
from ..payment_operation import PaymentOperationConflict, PaymentOperationStore, conflict_result as payment_operation_conflict_result
from ..payment_routing import PaymentRoutePlan, PaymentRoutePlanner, coerce_approve_country as canonical_coerce_approve_country, parse_proxy_pool, payment_proxy_pools as canonical_payment_proxy_pools
from ..sanitizer import sanitize as _canonical_sanitize, sanitize_text as _canonical_sanitize_text
from .. import payment_egress

from .base import PaymentMethodSpec, _DIRECT_CARD_CURRENCY, _LOGGER, _as_bool, _blik_completion, _config_data, _last_json_object, _redact_sensitive_text, _reference_root, _tail


def _run_extractor_subprocess(
    spec: PaymentMethodSpec,
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    timeout: int,
    cleanup_paths: tuple[str, ...] = (),
) -> tuple[_plm.subprocess.CompletedProcess[str] | None, str, dict[str, Any] | None]:
    """Run an extractor CLI, returning ``(proc, combined_output, timeout_error)``.

    Centralizes the run + ``TimeoutExpired`` handling + temp-file cleanup shared by
    the script/direct_card/momo adapters. On timeout returns ``(None, "", err_dict)``;
    otherwise ``(proc, stdout+stderr, None)``. ``cleanup_paths`` are always removed.
    """
    try:
        proc = _plm.subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc, output, None
    except _plm.subprocess.TimeoutExpired:
        return None, "", {
            "ok": False,
            "status": "timed_out",
            "error": f"{spec.label} extractor timed out after {timeout}s",
            "error_code": "extractor_timed_out",
            "error_stage": "adapter_subprocess",
            "retryable": True,
        }
    finally:
        for path in cleanup_paths:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning("failed to remove temporary payment credential file", exc_info=True)



def _run_protocol_script(spec: PaymentMethodSpec, access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    runtime_config = kwargs.pop("runtime_config", None)
    root = _reference_root(runtime_config)
    script = root / spec.script
    if not script.is_file():
        return {"ok": False, "error": f"protocol extractor not found: {script}"}

    try:
        payment_egress.assert_egress_countries(kwargs, runtime_config)
    except payment_egress.EgressCheckError as exc:
        return exc.to_result(spec.key)

    cfg = _plm._protocol_cfg(runtime_config)
    method_cfg = cfg.get("methods", {}).get(spec.key, {}) if isinstance(cfg.get("methods"), Mapping) else {}
    if not isinstance(method_cfg, Mapping):
        method_cfg = {}
    timeout = int(method_cfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 900)
    seed_proxy = str(
        kwargs.get("seed_proxy")
        or proxy
        or kwargs.get("provider_proxy")
        or kwargs.get("checkout_proxy")
        or method_cfg.get("proxy")
        or ""
    ).strip()
    if not seed_proxy:
        return {"ok": False, "error": f"{spec.label} requires a proxy seed"}
    blik_code = str(kwargs.get("blik_code") or "").strip() if spec.key == "blik" else ""
    if spec.key == "blik" and not re.fullmatch(r"\d{6}", blik_code):
        return {"ok": False, "error": "BLIK requires an explicit 6-digit code for this run"}

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    command = [sys.executable, str(script)]
    proxy_file = ""
    if spec.key == "pix":
        # Proxies carry inline credentials, so they travel through the
        # environment (which run_pix reads as PIX_PROXY/PIX_BR_PROXY/
        # PIX_VN_PROXY) instead of argv, where they would show up in the
        # process list.
        env["OPENAI_ACCESS_TOKEN"] = access_token
        env["PIX_PROXY"] = seed_proxy
        command.append("--quiet")
        provider_proxy = str(kwargs.get("provider_proxy") or "").strip()
        promotion_proxy = str(kwargs.get("promotion_proxy") or "").strip()
        if provider_proxy:
            env["PIX_BR_PROXY"] = provider_proxy
        if promotion_proxy:
            env["PIX_VN_PROXY"] = promotion_proxy
    else:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
        with handle:
            handle.write(seed_proxy + "\n")
        proxy_file = handle.name
        if spec.key == "ideal":
            env.update({"PP_TOKEN": access_token, "IDEAL_PROXY_SEED_FILE": proxy_file, "IDEAL_FLOW_MODE": "single"})
        elif spec.key == "kakao":
            # 优先用 Kakao 专用多 Seed 文件(proxy_seeds.txt)获得冗余与失败轮换；
            # 一条 seed 出口/ TLS 抖动进冷却时还能切换下一条。缺失时回退到 manager
            # 传入的单条 stage 代理。
            kakao_seed_pool = script.parent / "proxy_seeds.txt"
            kakao_seed_file = (
                str(kakao_seed_pool)
                if kakao_seed_pool.is_file()
                and kakao_seed_pool.read_text(encoding="utf-8", errors="ignore").strip()
                else proxy_file
            )
            env.update({"KAKAO_TOKEN": access_token, "KAKAO_PROXY_SEED_FILE": kakao_seed_file})
            countries = kwargs.get("stage_proxy_countries") if isinstance(kwargs.get("stage_proxy_countries"), dict) else {}
            checkout_country = str(countries.get("checkout") or kwargs.get("checkout_country") or "KR").strip().upper()
            promotion_country = str(countries.get("promotion") or "VN").strip().upper()
            provider_country = str(countries.get("provider") or kwargs.get("target_country") or "KR").strip().upper()
            env.update({
                "KAKAO_BOOTSTRAP_COUNTRY": checkout_country,
                "KAKAO_PROMOTION_COUNTRY": promotion_country,
                "KAKAO_PROVIDER_COUNTRY": provider_country,
            })
        elif spec.key == "blik":
            env.update({"PP_TOKEN": access_token, "IDEAL_PROXY_SEED_FILE": proxy_file, "IDEAL_FLOW_MODE": "single", "IDEAL_BLIK_CODE": blik_code})
        elif spec.key == "twint":
            env.update({"PP_TOKEN": access_token, "TWINT_PROXY_SEED_FILE": proxy_file, "TWINT_FLOW_MODE": "single"})

    proc, output, timeout_err = _plm._run_extractor_subprocess(
        spec, command, env=env, cwd=str(script.parent), timeout=timeout, cleanup_paths=(proxy_file,),
    )
    if timeout_err:
        return timeout_err
    parsed = _last_json_object(proc.stdout or "")
    if (
        parsed.get("schema") == "protocol_payment.v1"
        and (proc.returncode == 0 or parsed.get("ok") is False)
    ):
        parsed.setdefault("payment_method", spec.key)
        parsed.setdefault("link_type", f"{spec.key}_protocol")
        return parsed
    parsed = parsed if spec.key in {"pix", "kakao"} else {}
    if parsed and spec.key == "kakao":
        parsed.setdefault("payment_method", "kakao")
        parsed.setdefault("url", parsed.get("provider_redirect_url") or "")
        return parsed
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": _redact_sensitive_text(_tail(output)) or f"extractor exited {proc.returncode}",
            "exit_code": proc.returncode,
        }
    parsed = _last_json_object(proc.stdout or "") if spec.key == "pix" else {}
    if parsed:
        parsed["ok"] = bool(parsed.get("long_url") or parsed.get("provider_redirect_url") or parsed.get("pix_qr_code"))
        parsed["url"] = parsed.get("long_url") or parsed.get("provider_redirect_url") or parsed.get("pix_hosted_instructions_url") or ""
        parsed["qr_data"] = parsed.get("pix_qr_code") or ""
        return parsed
    if spec.key == "blik":
        # BLIK 自动提交模式完成支付后没有可分享 URL，成功信号是提取器打印的
        # ``BLIK_RESULT:{...}`` 完成哨兵（status=completed）。不要再从截断日志抓 URL。
        completion = _blik_completion(proc.stdout or "")
        if completion:
            return {
                "ok": True,
                "url": "",
                "status": "completed",
                "operation": "execute_payment",
                "link_type": "blik_protocol_completed",
                "message": completion.get("message") or "BLIK 自动提交完成",
            }
    return {
        "ok": False,
        "error": _redact_sensitive_text(_tail(output)) or "extractor returned no structured result",
        "error_code": "extractor_output_missing",
        "error_stage": "extracting",
        "retryable": True,
        "exit_code": proc.returncode,
    }



def _write_token_file(access_token: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
    with handle:
        handle.write(str(access_token or "").strip() + "\n")
    return handle.name



def _run_wallet_adapter(
    spec: PaymentMethodSpec,
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    from ..wallet_provider import run_wallet_provider
    from ..wallet_transport import ChatGPTStripeWalletTransport

    runtime_config = kwargs.pop("runtime_config", None)
    cfg = _plm._protocol_cfg(runtime_config)
    methods = cfg.get("methods") if isinstance(cfg.get("methods"), Mapping) else {}
    method_cfg = methods.get(spec.key) if isinstance(methods.get(spec.key), Mapping) else {}
    timeout = max(5, int(kwargs.get("timeout_seconds") or method_cfg.get("timeout_seconds") or 900))
    stage_keys = (
        "checkout_proxy", "promotion_proxy", "update_proxy", "stripe_init_proxy",
        "provider_proxy", "payment_method_proxy", "confirm_proxy", "approve_proxy",
        "final_review_proxy", "redirect_proxy",
    )
    transport_context: dict[str, Any] = {
        key: kwargs.get(key) or method_cfg.get(key) or ""
        for key in stage_keys
    }
    transport_context["default_proxy"] = proxy or method_cfg.get("proxy") or ""
    transport_context["payment_route_plan"] = kwargs.get("payment_route_plan")
    transport_context["stage_proxies"] = kwargs.get("stage_proxies")
    transport_context["stage_proxy_countries"] = (
        kwargs.get("stage_proxy_countries")
        if isinstance(kwargs.get("stage_proxy_countries"), Mapping)
        else method_cfg.get("stage_proxy_countries")
        if isinstance(method_cfg.get("stage_proxy_countries"), Mapping)
        else {}
    )
    rotate_setting = (
        kwargs.get("rotate_proxy_sessions")
        if "rotate_proxy_sessions" in kwargs
        else method_cfg.get("rotate_proxy_sessions")
    )
    transport_context["rotate_proxy_sessions"] = (
        spec.key == "gopay" if rotate_setting is None else _as_bool(rotate_setting) is True
    )
    for resolver_key in (
        "proxy_resolver", "approve_proxy_resolver", "final_review_proxy_resolver",
        "poll_proxy_resolver", "follow_redirect_proxy_resolver",
    ):
        resolver = kwargs.get(resolver_key)
        if callable(resolver):
            transport_context[resolver_key] = resolver
    billing = kwargs.get("billing_details") or method_cfg.get("billing_details")
    if not isinstance(billing, dict):
        billing = None
    promotion_setting = (
        kwargs.get("promotion_update")
        if "promotion_update" in kwargs
        else method_cfg.get("promotion_update", method_cfg.get("enable_promotion"))
    )
    require_zero_setting = (
        kwargs.get("require_zero")
        if "require_zero" in kwargs
        else method_cfg.get("require_zero")
    )
    require_zero = spec.key == "gopay" if require_zero_setting is None else _as_bool(require_zero_setting) is True
    transport = kwargs.get("transport")
    if transport is None:
        transport = ChatGPTStripeWalletTransport(timeout=timeout)
    return run_wallet_provider(
        spec.key,
        access_token,
        transport,
        probe_only=bool(kwargs.get("probe_only")),
        billing_details=billing,
        auth_context=auth_context if isinstance(auth_context, dict) else {},
        transport_context=transport_context,
        stripe_publishable_key=str(
            kwargs.get("stripe_publishable_key")
            or method_cfg.get("stripe_publishable_key")
            or os.environ.get("PP_STRIPE_PUBLISHABLE_KEY")
            or ""
        ).strip(),
        require_zero=require_zero,
        promotion_update=_as_bool(promotion_setting),
        max_approve_attempts=int(
            kwargs.get("max_approve_attempts") or method_cfg.get("max_approve_attempts") or 6
        ),
        max_poll_attempts=int(kwargs.get("max_poll_attempts") or method_cfg.get("max_poll_attempts") or 25),
        poll_interval_seconds=float(
            kwargs.get("poll_interval_seconds") or method_cfg.get("poll_interval_seconds") or 2.0
        ),
    )



def _run_regional_wallet_adapter(
    spec: PaymentMethodSpec,
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a regional contract through its injected transport boundary.

    No production transport is selected implicitly.  These catalog methods
    remain disabled until a provider canary establishes the live wire contract.
    """
    from ..regional_payment_adapter import RegionalPaymentAdapter, regional_profile

    transport = kwargs.get("transport")
    if transport is None and bool(kwargs.get("regional_transport_enabled")):
        from ..regional_payment_adapter import ChatGPTStripeRegionalTransport
        transport = ChatGPTStripeRegionalTransport(
            timeout=max(5, int(kwargs.get("timeout_seconds") or 45)),
        )
    if transport is None:
        error = RuntimeError("regional payment adapter requires an injected transport")
        error.error_code = "regional_transport_unconfigured"
        error.error_stage = "adapter_setup"
        error.retryable = False
        raise error
    adapter = RegionalPaymentAdapter(regional_profile(spec.key), transport)
    return adapter.run(
        access_token=access_token,
        billing_country=str(kwargs.get("target_country") or kwargs.get("checkout_country") or spec.country),
        billing_details=kwargs.get("billing_details") if isinstance(kwargs.get("billing_details"), Mapping) else None,
        checkout_request={
            "proxy": proxy,
            "auth_context": dict(auth_context or {}),
            "runtime_config": kwargs.get("runtime_config"),
        },
        probe_only=bool(kwargs.get("probe_only")),
        progress=kwargs.get("adapter_progress"),
    )



def _run_gcash_adapter(
    spec: PaymentMethodSpec,
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    from ..gcash_provider import DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID, run_gcash_provider
    from ..gcash_transport import ChatGPTGCashTransport

    runtime_config = kwargs.pop("runtime_config", None)
    cfg = _plm._protocol_cfg(runtime_config)
    methods = cfg.get("methods") if isinstance(cfg.get("methods"), Mapping) else {}
    method_cfg = methods.get(spec.key) if isinstance(methods.get(spec.key), Mapping) else {}
    timeout = max(5, int(kwargs.get("timeout_seconds") or method_cfg.get("timeout_seconds") or 900))
    transport_context: dict[str, Any] = {
        "checkout_proxy": kwargs.get("checkout_proxy") or method_cfg.get("checkout_proxy") or "",
        "promotion_proxy": kwargs.get("promotion_proxy") or method_cfg.get("promotion_proxy") or "",
        "update_proxy": kwargs.get("update_proxy") or method_cfg.get("update_proxy") or "",
        # The proven GCash route keeps checkout, taxes, resolve and provider start
        # on one exit. Promotion update may use its own exit.
        "provider_proxy": (
            kwargs.get("checkout_proxy") or kwargs.get("provider_proxy")
            or method_cfg.get("provider_proxy") or ""
        ),
        "confirm_proxy": (
            kwargs.get("confirm_proxy")
            or kwargs.get("checkout_proxy")
            or kwargs.get("provider_proxy")
            or method_cfg.get("confirm_proxy")
            or kwargs.get("approve_proxy")
            or ""
        ),
    }
    transport_context["default_proxy"] = proxy or method_cfg.get("proxy") or ""
    transport_context["payment_route_plan"] = kwargs.get("payment_route_plan")
    transport_context["stage_proxies"] = kwargs.get("stage_proxies")
    return run_gcash_provider(
        access_token,
        ChatGPTGCashTransport(timeout=timeout),
        probe_only=bool(kwargs.get("probe_only")),
        auth_context=auth_context if isinstance(auth_context, dict) else {},
        transport_context=transport_context,
        custom_payment_method_type_id=str(
            kwargs.get("custom_payment_method_type_id")
            or method_cfg.get("custom_payment_method_type_id")
            or DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID
        ).strip(),
        require_zero=bool(kwargs.get("require_zero", method_cfg.get("require_zero", True))),
    )



def _run_direct_card(spec: PaymentMethodSpec, access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    """直卡 checkout short-link extractor adapter.

    Drives ``direct_card/direct_card_extract.py`` (a self-contained CLI) through a
    US checkout / promo-update / zero-amount-verify flow and returns its
    ``chatgpt.com/checkout/<entity>/<cs_id>`` long link. The access token is passed
    via a temp ``--credential-file`` so it never reaches the process argv.
    """
    runtime_config = kwargs.pop("runtime_config", None)
    root = _reference_root(runtime_config)
    script = root / spec.script
    if not script.is_file():
        return {"ok": False, "error": f"protocol extractor not found: {script}"}

    try:
        payment_egress.assert_egress_countries(kwargs, runtime_config)
    except payment_egress.EgressCheckError as exc:
        return exc.to_result(spec.key)

    cfg = _plm._protocol_cfg(runtime_config)
    method_cfg = cfg.get("methods", {}).get(spec.key, {}) if isinstance(cfg.get("methods"), Mapping) else {}
    if not isinstance(method_cfg, Mapping):
        method_cfg = {}
    timeout = int(method_cfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 900)

    checkout_proxy = str(
        kwargs.get("checkout_proxy") or proxy or kwargs.get("provider_proxy") or ""
    ).strip()
    if not checkout_proxy:
        return {"ok": False, "error": f"{spec.label} requires a checkout proxy seed"}
    update_proxy = str(
        kwargs.get("promotion_proxy") or kwargs.get("approve_proxy") or checkout_proxy or ""
    ).strip()

    country = str(kwargs.get("target_country") or kwargs.get("checkout_country") or spec.country or "PH").strip().upper()
    currency = str(
        method_cfg.get("currency")
        or (spec.currency if country == spec.country else _DIRECT_CARD_CURRENCY.get(country, spec.currency))
    ).strip().upper()
    countries = kwargs.get("stage_proxy_countries") if isinstance(kwargs.get("stage_proxy_countries"), dict) else {}

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # Proxies carry inline credentials, so they travel through the environment
    # (read by the extractor as DIRECT_CARD_CHECKOUT_PROXY/_UPDATE_PROXY) rather
    # than argv, where they would be visible in the process list.
    env["DIRECT_CARD_CHECKOUT_PROXY"] = checkout_proxy
    env["DIRECT_CARD_UPDATE_PROXY"] = update_proxy
    token_file = _write_token_file(access_token)
    command = [
        sys.executable, str(script),
        "--credential-file", token_file,
        "--billing-country", country,
        "--currency", currency,
        "--skip-proxy-check",
    ]
    checkout_cc = str(countries.get("checkout") or "").strip().upper()
    update_cc = str(countries.get("promotion") or countries.get("update") or "").strip().upper()
    if checkout_cc:
        command.extend(["--checkout-proxy-country", checkout_cc])
    if update_cc:
        command.extend(["--update-proxy-country", update_cc])
    promo = str(method_cfg.get("promo_campaign_id") or "").strip()
    if promo:
        command.extend(["--promo-campaign-id", promo])

    proc, output, timeout_err = _plm._run_extractor_subprocess(
        spec, command, env=env, cwd=str(script.parent), timeout=timeout, cleanup_paths=(token_file,),
    )
    if timeout_err:
        return timeout_err
    parsed = _last_json_object(proc.stdout or "")
    if not parsed:
        return {
            "ok": False,
            "error": _redact_sensitive_text(_tail(output)) or f"extractor exited {proc.returncode}",
            "exit_code": proc.returncode,
        }
    if not parsed.get("ok"):
        return {
            "ok": False,
            "error": _redact_sensitive_text(str(parsed.get("error") or "direct_card extraction failed")),
            "error_code": parsed.get("error_type") or "direct_card_failed",
        }
    long_url = str(parsed.get("long_url") or "").strip()
    if not long_url:
        return {"ok": False, "error": "direct_card extractor returned no checkout URL"}
    return {
        "ok": True,
        "url": long_url,
        "long_url": long_url,
        "cs_id": parsed.get("cs_id") or "",
        "processor_entity": parsed.get("processor_entity") or "",
        "amount": parsed.get("amount_minor"),
        "amount_verification": parsed.get("amount_verification") or "",
        "currency": parsed.get("amount_currency") or currency,
        "target_country": parsed.get("billing_country") or country,
        "link_type": "direct_card_protocol",
    }



def _run_momo(spec: PaymentMethodSpec, access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    """MoMo scannable-QR extractor adapter.

    Drives ``momo/run_momo.py``, which wraps the VN checkout → Stripe init →
    force ₫0 → MoMo PM → confirm → ChatGPT approve → follow-redirect flow and emits
    a single normalized JSON object (``ok``/``url``/``qr_data``/``qr_path``/...). A
    ``data:image`` QR is decoded to a PNG under ``runtime/momo_qr`` by the runner.
    """
    runtime_config = kwargs.pop("runtime_config", None)
    root = _reference_root(runtime_config)
    script = root / spec.script
    if not script.is_file():
        return {"ok": False, "error": f"protocol extractor not found: {script}"}

    try:
        payment_egress.assert_egress_countries(kwargs, runtime_config)
    except payment_egress.EgressCheckError as exc:
        return exc.to_result(spec.key)

    cfg = _plm._protocol_cfg(runtime_config)
    method_cfg = cfg.get("methods", {}).get(spec.key, {}) if isinstance(cfg.get("methods"), Mapping) else {}
    if not isinstance(method_cfg, Mapping):
        method_cfg = {}
    timeout = int(method_cfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 900)
    request_timeout = int(method_cfg.get("request_timeout_seconds") or 25)
    fallback_proxy = str(
        kwargs.get("checkout_proxy") or proxy or kwargs.get("provider_proxy") or method_cfg.get("proxy") or ""
    ).strip()
    stage_proxies = {
        "checkout": str(kwargs.get("checkout_proxy") or fallback_proxy).strip(),
        "promotion": str(kwargs.get("promotion_proxy") or fallback_proxy).strip(),
        "provider": str(
            kwargs.get("provider_proxy") or kwargs.get("stripe_init_proxy") or fallback_proxy
        ).strip(),
        "approve": str(kwargs.get("approve_proxy") or fallback_proxy).strip(),
        "redirect": str(kwargs.get("redirect_proxy") or fallback_proxy).strip(),
    }
    pre_proxy = str(method_cfg.get("pre_proxy") or "off").strip() or "off"
    qr_dir = runtime_file(runtime_config or _config_data(), "momo_qr")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    token_file = _write_token_file(access_token)
    command = [
        sys.executable, str(script),
        "--token-file", token_file,
        "--pre-proxy", pre_proxy,
        "--timeout", str(max(8, request_timeout)),
        "--qr-out-dir", str(qr_dir),
    ]
    if fallback_proxy:
        env["MOMO_PROXY"] = fallback_proxy
    for stage, value in stage_proxies.items():
        if value:
            env[f"MOMO_{stage.upper()}_PROXY"] = value
    strategy = str(kwargs.get("strategy") or method_cfg.get("strategy") or "custom_promo").strip()
    if strategy:
        command.extend(["--strategy", strategy])
    if kwargs.get("probe_only"):
        command.append("--probe-only")
    stripe_profile = method_cfg.get("stripe_profile") if isinstance(method_cfg.get("stripe_profile"), Mapping) else {}
    for env_key, config_key in {
        "MOMO_STRIPE_RUNTIME_VERSION": "runtime_version",
        "MOMO_STRIPE_API_VERSION": "api_version",
        "MOMO_STRIPE_CLIENT_BETAS": "client_betas",
        "MOMO_STRIPE_CONFIRM_FIELDS": "confirm_fields",
    }.items():
        value = stripe_profile.get(config_key)
        if value not in (None, ""):
            env[env_key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    max_proxies = int(method_cfg.get("max_proxies") or 1)
    if max_proxies > 1:
        command.extend(["--max-proxies", str(max_proxies)])

    proc, output, timeout_err = _plm._run_extractor_subprocess(
        spec, command, env=env, cwd=str(script.parent), timeout=timeout, cleanup_paths=(token_file,),
    )
    if timeout_err:
        return timeout_err
    parsed = _last_json_object(proc.stdout or "")
    if not parsed:
        return {
            "ok": False,
            "error": _redact_sensitive_text(_tail(output)) or f"extractor exited {proc.returncode}",
            "exit_code": proc.returncode,
        }
    if not parsed.get("ok") and not parsed.get("error"):
        parsed["error"] = parsed.get("qr_error") or parsed.get("decision_text") or "momo QR extraction failed"
    return parsed

