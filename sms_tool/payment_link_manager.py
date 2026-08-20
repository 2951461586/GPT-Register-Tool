"""Unified state machine for protocol payment-link extraction.

Native PayPal/UPI flows stay in :mod:`sms_tool.gen_pp_link`; GoPay and GrabPay
use the shared wallet provider, while GCash owns its custom-payment-method
adapter; iDEAL/PIX/Kakao Pay/BLIK/TWINT run the
vendored protocol extractors under ``services/protocol-payment``.
"""

from __future__ import annotations

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

from .config import ConfigError, current_config_data, resolve_runtime_config, validate_config
from .paths import project_path, runtime_file
from .payment_contracts import PaymentRequest, PaymentResult, payment_history_metadata
from .payment_catalog import (
    PAYMENT_METHODS as CATALOG_METHODS,
    normalize_payment_method as normalize_catalog_payment_method,
    validate_catalog_consistency,
)
from .payment_adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from .payment_executor import PaymentExecutionRequest, PaymentFlowExecutor
from .payment_operation import (
    PaymentOperationConflict,
    PaymentOperationStore,
    conflict_result as payment_operation_conflict_result,
)
from .payment_routing import (
    PaymentRoutePlan,
    PaymentRoutePlanner,
    coerce_approve_country as canonical_coerce_approve_country,
    parse_proxy_pool,
    payment_proxy_pools as canonical_payment_proxy_pools,
)
from .sanitizer import sanitize as _canonical_sanitize, sanitize_text as _canonical_sanitize_text
from . import payment_egress


# Deprecated monkeypatch hook. Production callers inject RuntimeConfig or use
# the current application scope.
CFG: dict[str, Any] = {}

_LOGGER = logging.getLogger(__name__)

# Protocol rule: GoPay approve/final-review must egress from an allowed
# country.  The catalog may override the allowlist through
# ``approve_countries`` (top-level default or per-method); when the catalog
# does not constrain GoPay, the historical desktop default JP/TR applies.
GOPAY_DEFAULT_APPROVE_COUNTRIES = ("JP", "TR")


def _config_data(runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if runtime_config is not None:
        return resolve_runtime_config(runtime_config).data
    if CFG:
        merged = dict(current_config_data())
        merged.update(CFG)
        return merged
    return current_config_data()


@dataclass(frozen=True)
class PaymentMethodSpec:
    key: str
    label: str
    country: str
    currency: str
    adapter: str
    script: str = ""
    artifact_validator: str = "http_url"


PAYMENT_METHODS = {
    key: PaymentMethodSpec(
        key,
        definition.label,
        definition.country,
        definition.currency,
        {"native_paypal": "native", "native_upi": "native"}.get(definition.adapter, definition.adapter),
        definition.script,
        definition.artifact_validator,
    )
    for key, definition in CATALOG_METHODS.items()
}

_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "unknown", "timed_out"})
_NON_SUCCESS_TERMINAL_STATES = _TERMINAL_STATES - {"completed"}
_TRANSITIONS = {
    "created": {"validating"} | _NON_SUCCESS_TERMINAL_STATES,
    "validating": {"preparing_proxy"} | _NON_SUCCESS_TERMINAL_STATES,
    "preparing_proxy": {"running"} | _NON_SUCCESS_TERMINAL_STATES,
    "running": {"extracting"} | _NON_SUCCESS_TERMINAL_STATES,
    "extracting": set(_TERMINAL_STATES),
    **{state: set() for state in _TERMINAL_STATES},
}

_STATE_LOCK = threading.Lock()
_BLIK_RESULT_RE = re.compile(r"BLIK_RESULT:(\{.*\})")

def build_default_payment_registry() -> PaymentAdapterRegistry:
    """Build and validate the complete adapter composition for the catalog."""
    registry = PaymentAdapterRegistry()

    def methods_for(adapter_key: str) -> tuple[str, ...]:
        return tuple(
            key for key, definition in CATALOG_METHODS.items()
            if definition.adapter == adapter_key
        )

    def paypal_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        from .gen_pp_link import generate_pp_link
        runtime_config = kwargs.pop("runtime_config", None)
        kwargs.pop("payment_method", None)
        return generate_pp_link(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            paypal_generation_type=kwargs.pop("paypal_generation_type", None),
            runtime_config=runtime_config,
            **_select_kwargs(kwargs, {
                "checkout_proxy", "provider_proxy", "stripe_init_proxy", "payment_method_proxy",
                "confirm_proxy", "approve_proxy", "promotion_proxy", "target_country",
                "checkout_country", "require_zero", "require_ba_token", "stage_proxy_countries",
                "max_checkout_retries", "max_stage_retries",
            }),
        )

    def upi_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        from .gen_pp_link import generate_upi_qr_link
        runtime_config = kwargs.pop("runtime_config", None)
        kwargs.pop("payment_method", None)
        return generate_upi_qr_link(
            access_token=access_token,
            proxy=proxy,
            auth_context=auth_context,
            runtime_config=runtime_config,
            **_select_kwargs(kwargs, {
                "checkout_proxy", "provider_proxy", "approve_proxy", "target_country",
                "checkout_country", "payment_country", "require_zero", "qr_path",
            }),
        )

    def wallet_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return _run_wallet_adapter(PAYMENT_METHODS[str(kwargs.pop("payment_method"))], access_token, proxy=proxy, auth_context=auth_context, **kwargs)

    def gcash_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("payment_method", None)
        return _run_gcash_adapter(PAYMENT_METHODS["gcash"], access_token, proxy=proxy, auth_context=auth_context, **kwargs)

    def script_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        spec = PAYMENT_METHODS[str(kwargs.pop("payment_method"))]
        return _run_protocol_script(spec, access_token, proxy=proxy, **kwargs)

    def direct_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return _run_direct_card(PAYMENT_METHODS["direct_card"], access_token, proxy=proxy, **kwargs)

    def momo_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return _run_momo(PAYMENT_METHODS["momo"], access_token, proxy=proxy, **kwargs)

    def regional_wallet_runner(*, access_token: str, proxy: Any = None, auth_context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        method = str(kwargs.pop("payment_method"))
        return _run_regional_wallet_adapter(
            PAYMENT_METHODS[method],
            access_token,
            proxy=proxy,
            auth_context=auth_context,
            **kwargs,
        )

    registry.register(FunctionPaymentAdapter("native_paypal", methods_for("native_paypal"), paypal_runner))
    registry.register(FunctionPaymentAdapter("native_upi", methods_for("native_upi"), upi_runner))
    registry.register(FunctionPaymentAdapter("wallet", methods_for("wallet"), wallet_runner))
    registry.register(FunctionPaymentAdapter("gcash_custom", methods_for("gcash_custom"), gcash_runner))
    registry.register(FunctionPaymentAdapter("script", methods_for("script"), script_runner))
    registry.register(FunctionPaymentAdapter("direct_card", methods_for("direct_card"), direct_runner))
    registry.register(FunctionPaymentAdapter("momo", methods_for("momo"), momo_runner))
    registry.register(FunctionPaymentAdapter("regional_wallet", methods_for("regional_wallet"), regional_wallet_runner))
    registry.validate_methods(set(PAYMENT_METHODS))
    validate_catalog_consistency(adapter_methods=set(registry.methods()))
    return registry


PAYMENT_ADAPTERS = build_default_payment_registry()


def normalize_payment_method(value: Any) -> str:
    method = normalize_catalog_payment_method(value)
    return method if method in PAYMENT_METHODS else ""


def payment_proxy_pools(
    payment_method: Any,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Read method-owned Checkout and Approve proxy pools from configuration."""
    return canonical_payment_proxy_pools(_config_data(runtime_config), payment_method)


def payment_method_label(value: Any) -> str:
    method = normalize_payment_method(value)
    return PAYMENT_METHODS[method].label if method else str(value or "")


def supported_payment_methods() -> list[dict[str, Any]]:
    root = _reference_root()
    registered = set(PAYMENT_ADAPTERS.methods())
    output = []
    for spec in PAYMENT_METHODS.values():
        available = spec.key in registered and (not spec.script or (root / spec.script).is_file())
        output.append({
            "key": spec.key,
            "label": spec.label,
            "country": spec.country,
            "currency": spec.currency,
            "adapter": spec.adapter,
            "available": available,
        })
    return output


def register_payment_adapter(adapter: Any) -> Any:
    """Register an adapter at the payment seam; useful for new methods/tests."""
    PAYMENT_ADAPTERS.register(adapter)
    return adapter


def generate_payment_link(
    access_token: str,
    proxy: Any = None,
    payment_method: Any = "paypal",
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute one protocol-payment flow through the common router and state machine."""
    source = _config_data(runtime_config)
    method = normalize_payment_method(payment_method)
    method_name = method or str(payment_method or "").strip().lower()
    options = dict(kwargs)
    operation_id = str(options.pop("operation_id", "") or uuid.uuid4().hex).strip()
    idempotency_key = str(options.pop("idempotency_key", "") or operation_id).strip()
    supplied_plan = options.pop("payment_route_plan", None)
    planning_error: Exception | None = None
    plan = supplied_plan if isinstance(supplied_plan, PaymentRoutePlan) else None

    try:
        validate_config(source, workflow="protocol_payments")
        if not method:
            raise ValueError(f"unsupported payment method: {payment_method}")
        if method not in _enabled_methods(source):
            raise ValueError(
                f"payment method disabled by protocol_payments.enabled_methods: {method}"
            )
        if plan is not None and plan.payment_method != method:
            raise ValueError(
                f"payment route plan method mismatch: {plan.payment_method} != {method}"
            )
        if plan is None:
            plan = PaymentRoutePlanner(source).plan(
                method,
                options=options,
                default_proxy=proxy,
            )
    except (ConfigError, ValueError, TypeError, OSError, RuntimeError) as exc:
        if not getattr(exc, "error_stage", ""):
            try:
                exc.error_stage = "validation" if isinstance(exc, (ValueError, ConfigError)) else "proxy_setup"
            except (AttributeError, TypeError):
                _LOGGER.debug("could not annotate payment planning error", exc_info=True)
        planning_error = exc
        plan = PaymentRoutePlan.empty(method_name)

    assert plan is not None
    spec = PAYMENT_METHODS.get(method)
    routed_options = {**options, **plan.to_adapter_options()}
    if isinstance(options.get("stage_proxy_countries"), Mapping):
        routed_options["stage_proxy_countries"] = dict(options["stage_proxy_countries"])
    routed_options["payment_route_plan"] = plan
    routed_options["paypal_generation_type"] = paypal_generation_type
    operation_name = "payment_method_capability_probe" if bool(options.get("probe_only")) else "extract_link"
    try:
        payment_operation = PaymentOperationStore.from_config(source).begin(
            payment_method=method_name,
            operation=operation_name,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
        )
    except PaymentOperationConflict as exc:
        result = payment_operation_conflict_result(exc)
        result.update({
            "payment_method": method_name,
            "operation": operation_name,
            "manager_state": result["status"],
        })
        _safe_persist_run(result)
        return result

    def transactional_progress(event: dict[str, Any]) -> None:
        payload = dict(event or {})
        stage = str(payload.get("stage") or "adapter")
        state = str(payload.get("state") or payload.get("status") or "running")
        potential_side_effect = operation_name != "payment_method_capability_probe" and (
            stage == "adapter"
            or stage in {"payment_method", "confirm", "approve", "poll", "redirect", "provider", "artifact"}
        )
        payment_operation.checkpoint(
            stage,
            state,
            side_effect_started=True if potential_side_effect else None,
            error_code=str(payload.get("error_code") or ""),
        )
        if progress is not None:
            progress(payload)

    routed_options["adapter_progress"] = transactional_progress

    for record in plan.coercions:
        _LOGGER.warning(
            "payment method %s approve country %s is not in the allowed set; coerced to %s",
            method,
            record.get("original"),
            record.get("coerced"),
        )

    def run_adapter(request: PaymentExecutionRequest) -> Mapping[str, Any]:
        if planning_error is not None:
            raise planning_error
        if spec is None:
            raise ValueError(f"unsupported payment method: {payment_method}")
        if bool(request.options.get("probe_only")):
            return probe_payment_method(
                access_token=request.access_token,
                payment_method=request.payment_method,
                auth_context=dict(request.auth_context),
                proxy=request.route_plan.checkout_proxy,
                runtime_config=request.runtime_config,
                **dict(request.options),
            )
        adapter_request = PaymentRequest.create(
            payment_method=request.payment_method,
            access_token=request.access_token,
            proxy=request.route_plan.checkout_proxy,
            auth_context=request.auth_context,
            runtime_config=request.runtime_config,
            options=request.options,
        )
        return PAYMENT_ADAPTERS.execute_mapping(adapter_request)

    executor = PaymentFlowExecutor(
        run_adapter,
        normalizer=(lambda result: _normalize_result(spec, result)) if spec else None,
        exception_classifier=_classify_exception,
        error_sanitizer=_redact_sensitive_text,
        progress=transactional_progress,
    )
    try:
        result = executor.run(PaymentExecutionRequest(
            payment_method=method_name,
            access_token=str(access_token or ""),
            route_plan=plan,
            auth_context=dict(auth_context or {}),
            runtime_config=source,
            options=routed_options,
            operation=operation_name,
            operation_id=payment_operation.operation_id,
            idempotency_key_hash=payment_operation.idempotency_key_hash,
        ))
        payment_operation.finish(result)
    except BaseException:
        payment_operation.fail_unknown("executor", "payment_executor_aborted")
        raise
    _safe_persist_run(result)
    return result


def probe_payment_method(
    access_token: str,
    payment_method: Any,
    *,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the real pre-side-effect path using one precomputed route plan."""
    method = normalize_payment_method(payment_method)
    if not method:
        raise ValueError(f"unsupported payment method: {payment_method}")

    source = _config_data(runtime_config)
    options = dict(kwargs)
    plan = options.pop("payment_route_plan", None)
    if not isinstance(plan, PaymentRoutePlan):
        plan = PaymentRoutePlanner(source).plan(
            method,
            options=options,
            default_proxy=proxy,
        )
    elif plan.payment_method != method:
        raise ValueError(
            f"payment route plan method mismatch: {plan.payment_method} != {method}"
        )
    options.update(plan.to_adapter_options())
    options.pop("probe_only", None)

    if method == "gopay":
        if "timeout_seconds" not in options and options.get("timeout") is not None:
            options["timeout_seconds"] = options["timeout"]
        return _run_wallet_adapter(
            PAYMENT_METHODS[method],
            access_token,
            proxy=plan.checkout_proxy,
            auth_context=auth_context,
            runtime_config=source,
            probe_only=True,
            **options,
        )

    if method in {"qris", "bizum", "naver_pay"}:
        return _run_regional_wallet_adapter(
            PAYMENT_METHODS[method],
            access_token,
            proxy=plan.checkout_proxy,
            auth_context=auth_context,
            runtime_config=source,
            probe_only=True,
            **options,
        )

    from .payment_capability import payment_method_capability_probe

    return payment_method_capability_probe(
        access_token=access_token,
        payment_method=method,
        auth_context=auth_context,
        proxy=plan.checkout_proxy,
        **options,
    )


def _run_extractor_subprocess(
    spec: PaymentMethodSpec,
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    timeout: int,
    cleanup_paths: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str] | None, str, dict[str, Any] | None]:
    """Run an extractor CLI, returning ``(proc, combined_output, timeout_error)``.

    Centralizes the run + ``TimeoutExpired`` handling + temp-file cleanup shared by
    the script/direct_card/momo adapters. On timeout returns ``(None, "", err_dict)``;
    otherwise ``(proc, stdout+stderr, None)``. ``cleanup_paths`` are always removed.
    """
    try:
        proc = subprocess.run(
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
    except subprocess.TimeoutExpired:
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

    cfg = _protocol_cfg(runtime_config)
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

    proc, output, timeout_err = _run_extractor_subprocess(
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


_DIRECT_CARD_CURRENCY = {
    "PH": "PHP", "US": "USD", "GB": "GBP", "JP": "JPY", "DE": "EUR", "FR": "EUR",
    "IE": "EUR", "NL": "EUR", "AU": "AUD", "CA": "CAD", "SG": "SGD", "IN": "INR",
    "TR": "TRY", "BR": "BRL", "KR": "KRW", "PL": "PLN", "CH": "CHF", "VN": "VND",
    "NZ": "NZD",
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
    from .wallet_provider import run_wallet_provider
    from .wallet_transport import ChatGPTStripeWalletTransport

    runtime_config = kwargs.pop("runtime_config", None)
    cfg = _protocol_cfg(runtime_config)
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
    from .regional_payment_adapter import RegionalPaymentAdapter, regional_profile

    transport = kwargs.get("transport")
    if transport is None and bool(kwargs.get("regional_transport_enabled")):
        from .regional_payment_adapter import ChatGPTStripeRegionalTransport
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
    from .gcash_provider import DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID, run_gcash_provider
    from .gcash_transport import ChatGPTGCashTransport

    runtime_config = kwargs.pop("runtime_config", None)
    cfg = _protocol_cfg(runtime_config)
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

    cfg = _protocol_cfg(runtime_config)
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

    proc, output, timeout_err = _run_extractor_subprocess(
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

    cfg = _protocol_cfg(runtime_config)
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

    proc, output, timeout_err = _run_extractor_subprocess(
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


def _normalize_result(spec: PaymentMethodSpec, result: Any) -> dict[str, Any]:
    is_mapping = isinstance(result, dict)
    data = dict(result) if is_mapping else {
        "ok": False,
        "error": str(result),
        "error_code": "invalid_adapter_result",
        "error_stage": "adapter_contract",
    }
    if is_mapping and not data and "ok" not in data:
        data.update({
            "ok": False,
            "error": f"{spec.label} extractor returned an invalid result contract",
            "error_code": "invalid_adapter_result",
            "error_stage": "adapter_contract",
        })
    data.setdefault("payment_method", spec.key)
    data.setdefault("method", spec.key)
    data.setdefault("target_country", spec.country)
    data.setdefault("currency", spec.currency)
    data.setdefault("link_type", f"{spec.key}_protocol")
    if not data.get("url"):
        data["url"] = data.get("long_url") or data.get("provider_redirect_url") or data.get("checkout_url") or data.get("upi_uri") or ""
    data.setdefault("operation", "extract_link")
    completed_payment = (
        spec.key == "blik"
        and str(data.get("status") or "").lower() == "completed"
        and data.get("operation") == "execute_payment"
        and data.get("link_type") == "blik_protocol_completed"
    )
    explicit_terminal = _explicit_terminal_state(data)
    if "ok" not in data:
        data["ok"] = False
        if not explicit_terminal:
            data.setdefault("error", f"{spec.label} extractor returned an invalid result contract")
            data.setdefault("error_code", "invalid_adapter_result")
            data.setdefault("error_stage", "adapter_contract")
    if explicit_terminal:
        data["ok"] = False
        data["status"] = explicit_terminal
        if explicit_terminal == "unknown":
            data.setdefault("requires_reconciliation", True)
        data.setdefault("error_code", {
            "cancelled": "payment_link_cancelled",
            "unknown": "payment_outcome_unknown",
            "timed_out": "payment_link_timed_out",
        }[explicit_terminal])
        data.setdefault("error", {
            "cancelled": f"{spec.label} extraction was cancelled",
            "unknown": f"{spec.label} extraction outcome is unknown",
            "timed_out": f"{spec.label} extraction timed out",
        }[explicit_terminal])
    capability_probe = data.get("operation") == "payment_method_capability_probe"
    validator = spec.artifact_validator
    artifact_ok = bool(data.get("url") or data.get("qr_data") or data.get("qr_path"))
    if validator in {"http_url", "paypal_ba_url", "provider_redirect", "checkout_url"} and data.get("url"):
        artifact_ok = str(data.get("url") or "").lower().startswith(("http://", "https://"))
    elif validator == "url_or_qr":
        artifact_ok = bool(data.get("url") or data.get("qr_data") or data.get("qr_path"))
    elif validator == "completion":
        artifact_ok = str(data.get("status") or "").lower() == "completed"
    if (
        data.get("ok")
        and not completed_payment
        and not capability_probe
        and not artifact_ok
    ):
        data["ok"] = False
        data["error"] = f"{spec.label} extractor returned no link or QR data"
        data["error_code"] = "adapter_result_missing_artifact"
        data["error_stage"] = "normalization"
    _normalize_error_contract(data)
    if explicit_terminal == "cancelled" and data.get("error_code") == "payment_link_extraction_failed":
        data["error_code"] = "payment_link_cancelled"
    elif explicit_terminal == "timed_out" and data.get("error_code") == "payment_link_extraction_failed":
        data["error_code"] = "payment_link_timed_out"
    return data


def _explicit_terminal_state(data: dict[str, Any]) -> str:
    """Return a non-success terminal state explicitly reported by an adapter."""
    if _as_bool(data.get("outcome_unknown")) is True or _as_bool(data.get("requires_reconciliation")) is True:
        return "unknown"

    for key in ("terminal_state", "state", "status", "outcome", "error_code", "error_type", "decision"):
        state = _canonical_terminal_state(data.get(key))
        if state:
            return state

    exit_code = data.get("exit_code")
    try:
        numeric_exit_code = int(exit_code)
    except (TypeError, ValueError):
        numeric_exit_code = 0
    if numeric_exit_code in {124}:
        return "timed_out"
    if numeric_exit_code in {-2, 130, -1073741510, 3221225786}:
        return "cancelled"

    status = _normalized_contract_value(data.get("status") or data.get("state"))
    has_artifact = bool(data.get("url") or data.get("qr_data") or data.get("qr_path"))
    if not data.get("ok") and not has_artifact and status in {
        "pending", "processing", "submitted", "requires_action", "awaiting_confirmation",
    }:
        return "unknown"
    return ""


def _canonical_terminal_state(value: Any) -> str:
    normalized = _normalized_contract_value(value)
    if normalized in {
        "cancelled", "canceled", "cancelled_by_user", "canceled_by_user", "interrupted",
        "keyboard_interrupt", "keyboardinterrupt",
    } or normalized.endswith("_cancelled") or normalized.endswith("_canceled"):
        return "cancelled"
    if normalized in {"timed_out", "timeout", "timeout_expired", "extractor_timeout"} or (
        normalized.endswith("_timed_out") or normalized.endswith("_timeout")
    ):
        return "timed_out"
    if normalized in {"unknown", "outcome_unknown", "payment_outcome_unknown", "indeterminate", "inconclusive"} or (
        normalized.endswith("_outcome_unknown")
    ):
        return "unknown"
    return ""


def _normalized_contract_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _normalize_error_contract(data: dict[str, Any]) -> None:
    """Ensure every adapter result has stable retry and error-stage fields."""
    if data.get("ok"):
        data["retryable"] = False
        data["error_stage"] = ""
        return

    terminal_state = _explicit_terminal_state(data) or "failed"
    stage = data.get("error_stage") or data.get("stage") or data.get("failed_step")
    default_stage = "adapter_contract" if data.get("error_code") == "invalid_adapter_result" else "adapter"
    if data.get("error_code") in {"checkout_not_zero_due", "nonzero_offer", "paypal_payment_method_unavailable"}:
        default_stage = "eligibility"
    data["error_stage"] = str(stage or default_stage).strip() or default_stage
    data.setdefault("error", "payment-link extraction failed")
    data.setdefault("error_code", "payment_link_extraction_failed")

    explicit_retryable = _as_bool(data.get("retryable"))
    if explicit_retryable is None:
        explicit_retryable = _as_bool(data.get("retry_safe"))
    if terminal_state in {"cancelled", "unknown"}:
        data["retryable"] = False
    elif explicit_retryable is not None:
        data["retryable"] = explicit_retryable
    elif terminal_state == "timed_out":
        data["retryable"] = True
    else:
        data["retryable"] = _is_retryable_failure(data)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return None


def _is_retryable_failure(data: dict[str, Any]) -> bool:
    try:
        status_code = int(data.get("status_code") or data.get("http_status") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code == 429 or 500 <= status_code <= 599:
        return True
    code = _normalized_contract_value(data.get("error_code") or data.get("error_type"))
    retryable_codes = {
        "connection_error", "connect_timeout", "read_timeout", "network_error",
        "proxy_error", "proxy_unavailable", "rate_limited", "service_unavailable",
    }
    return code in retryable_codes


def _result_terminal_state(data: dict[str, Any]) -> str:
    return "completed" if data.get("ok") else (_explicit_terminal_state(data) or "failed")


def _classify_exception(exc: Exception) -> tuple[str, str, bool]:
    explicit_state = _canonical_terminal_state(
        getattr(exc, "status", "") or getattr(exc, "terminal_state", "")
    )
    custom_code = str(
        getattr(exc, "error_code", "")
        or getattr(exc, "code", "")
        or ""
    )
    if explicit_state:
        default_code = {
            "cancelled": "payment_link_cancelled",
            "unknown": "payment_outcome_unknown",
            "timed_out": "payment_link_timed_out",
        }[explicit_state]
        explicit_retryable = _as_bool(getattr(exc, "retryable", None))
        if explicit_state in {"cancelled", "unknown"}:
            explicit_retryable = False
        elif explicit_retryable is None:
            explicit_retryable = explicit_state == "timed_out"
        return explicit_state, custom_code or default_code, bool(explicit_retryable)
    if _as_bool(getattr(exc, "outcome_unknown", None)) is True:
        return "unknown", custom_code or "payment_outcome_unknown", False
    names = {_normalized_contract_value(cls.__name__) for cls in type(exc).mro()}
    if names & {"cancellederror", "cancelled_error", "canceled_error"}:
        return "cancelled", "payment_link_cancelled", False
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)) or any("timeout" in name for name in names):
        return "timed_out", "payment_link_timed_out", True
    retryable = _as_bool(getattr(exc, "retryable", None)) is True
    return "failed", custom_code or "payment_link_manager_failed", retryable


def _manager_error_stage(state: str) -> str:
    return {
        "created": "validation",
        "validating": "validation",
        "preparing_proxy": "proxy_setup",
        "running": "adapter",
        "extracting": "normalization",
    }.get(state, "manager")


def _select_kwargs(values: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in allowed and value is not None}


def _protocol_cfg(runtime_config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    source = _config_data(runtime_config)
    value = source.get("protocol_payments")
    return value if isinstance(value, Mapping) else {}


def allowed_approve_countries(payment_method: Any) -> tuple[str, ...]:
    """Return the approve-country allowlist for a method (empty = unconstrained).

    The catalog ``approve_countries`` value wins; GoPay falls back to the
    historical JP/TR default when the catalog does not constrain it.
    """
    method = normalize_payment_method(payment_method)
    definition = CATALOG_METHODS.get(method)
    if definition is not None and definition.approve_countries:
        return tuple(definition.approve_countries)
    if method == "gopay":
        return GOPAY_DEFAULT_APPROVE_COUNTRIES
    return ()


def coerce_approve_country(payment_method: Any, country: Any) -> tuple[str, bool]:
    """Enforce the GoPay approve-country protocol rule.

    Returns ``(effective_country, coerced)``.  Only GoPay is constrained: an
    explicit approve country outside the allowlist (catalog
    ``approve_countries``, falling back to JP/TR) is forced to JP, or to the
    first allowed entry when JP itself is not allowed.  Other methods and
    blank values pass through unchanged so their existing defaults apply.
    """
    method = normalize_payment_method(payment_method)
    value = str(country or "").strip().upper()
    if method != "gopay" or not value:
        return value, False
    allowed = allowed_approve_countries(method)
    if not allowed or value in allowed:
        return value, False
    coerced = "JP" if "JP" in allowed else allowed[0]
    _LOGGER.warning(
        "payment method %s approve country %s is not in the allowed set (%s); coerced to %s",
        method,
        value,
        ",".join(allowed),
        coerced,
    )
    return coerced, True


def _resolve_proxy_pool_routes(
    method: str,
    proxy: Any,
    kwargs: Mapping[str, Any],
    runtime_config: Mapping[str, Any] | None = None,
    *,
    coercion_records: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Compatibility wrapper around the canonical payment route planner."""
    values = dict(kwargs)
    source = _config_data(runtime_config)
    configured_countries = values.get("stage_proxy_countries")
    configured_countries = dict(configured_countries) if isinstance(configured_countries, Mapping) else {}
    approve_input = str(
        configured_countries.get("approve") or values.get("approve_country") or ""
    ).strip().upper()
    pre_coercions: list[dict[str, Any]] = []
    if approve_input:
        approve_country, changed = coerce_approve_country(method, approve_input)
        if changed:
            configured_countries["approve"] = approve_country
            values["stage_proxy_countries"] = configured_countries
            if str(values.get("approve_country") or "").strip():
                values["approve_country"] = approve_country
            pre_coercions.append({
                "field": "approve_country",
                "original": approve_input,
                "coerced": approve_country,
            })
    supplied = values.get("payment_route_plan")
    if isinstance(supplied, PaymentRoutePlan):
        plan = supplied
    else:
        plan = PaymentRoutePlanner(source).plan(
            method,
            options=values,
            default_proxy=proxy,
        )
    if plan.payment_method != method:
        raise ValueError(f"payment route plan method mismatch: {plan.payment_method} != {method}")

    countries_supplied = isinstance(values.get("stage_proxy_countries"), Mapping)
    routed = {**values, **plan.to_adapter_options()}
    routed.pop("checkout_proxy_pool", None)
    routed.pop("approve_proxy_pool", None)
    routed.pop("stage_proxy_pools", None)
    routed.pop("stage_routes", None)
    if not countries_supplied and not plan.coercions:
        routed.pop("stage_proxy_countries", None)

    records = [*pre_coercions, *plan.coercions]
    for record in records:
        if coercion_records is not None:
            coercion_records.append(dict(record))
        original = str(record.get("original") or "")
        coerced = str(record.get("coerced") or "")
        if record not in pre_coercions:
            _LOGGER.warning(
                "payment method %s approve country %s is not in the allowed set; coerced to %s",
                method,
                original,
                coerced,
            )
        countries = dict(routed.get("stage_proxy_countries") or {})
        countries["approve"] = coerced
        routed["stage_proxy_countries"] = countries
        if str(values.get("approve_country") or "").strip():
            routed["approve_country"] = coerced
    return plan.checkout_proxy or proxy, routed


def _enabled_methods(runtime_config: Mapping[str, Any] | None = None) -> set[str]:
    raw = _protocol_cfg(runtime_config).get("enabled_methods")
    if isinstance(raw, str):
        values = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return set(PAYMENT_METHODS)
    return {method for value in values if (method := normalize_payment_method(value))}


def _reference_root(runtime_config: Mapping[str, Any] | None = None) -> Path:
    configured = _protocol_cfg(runtime_config).get("reference_root") or "services/protocol-payment"
    return project_path(configured)


def _state_path() -> Path:
    configured = str(_protocol_cfg().get("state_file") or "").strip()
    return project_path(configured) if configured else runtime_file(_config_data(), "payment_link_runs.jsonl")


def _persist_run(result: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {}
    for key, value in result.items():
        lowered = key.lower()
        # Key 黑名单：原始子进程输出、token、proxy 已知不能落盘；
        # card_* / card_last4 / pan 同样是敏感凭据 —— 浏览器支付路径
        # 会把卡号末四位塞进返回 dict，这里按 key 名拦截，避免进 jsonl。
        if (
            lowered in {"raw_output", "raw_output_tail"}
            or "token" in lowered
            or "proxy" in lowered
            or lowered.startswith("card_")
            or lowered in {"card", "pan", "cardnumber", "card_number"}
        ):
            continue
        record[key] = value
    record = payment_history_metadata(record)
    record = _redact_sensitive_values(record)
    with _STATE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _safe_persist_run(result: dict[str, Any]) -> None:
    try:
        _persist_run(result)
    except (OSError, TypeError, ValueError) as exc:
        result["persistence_warning"] = f"payment run state was not persisted: {type(exc).__name__}"


def _last_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index in reversed([i for i, char in enumerate(text) if char == "{"]):
        try:
            value, end = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(value, dict) and not text[index + end :].strip():
            return value
    return {}


def _tail(text: str, limit: int = 1200) -> str:
    value = str(text or "").strip()
    return value[-limit:]


def _blik_completion(stdout: str) -> dict[str, Any]:
    """Parse the BLIK auto-submit completion sentinel from stdout.

    BLIK 自动提交模式完成支付后没有可分享 URL，成功信号是 ``print_result_url`` 打印的
    ``BLIK_RESULT:{...}`` 结构化行（status=completed）。返回最后一个完成哨兵，否则空 dict。
    """
    for raw in reversed(_BLIK_RESULT_RE.findall(stdout or "")):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if (
            isinstance(value, dict)
            and value.get("ok") is True
            and str(value.get("payment_method") or "").lower() == "blik"
            and str(value.get("status") or "").lower() == "completed"
            and value.get("link_type") == "blik_protocol_completed"
        ):
            return value
    return {}


def _mask_ba_token(token: str) -> str:
    return "[REDACTED]" if token else ""


def _redact_sensitive_text(value: str) -> str:
    return _canonical_sanitize_text(value)


def _redact_sensitive_values(value: Any) -> Any:
    """Mask credentials anywhere inside a persisted payment-run value.

    ``ba_token`` 键本身已被 :func:`_persist_run` 的键名过滤丢弃，但 approve URL
    （如 ``.../agreements/approve?ba_token=BA-...``）会以 ``url``/``fallback_url`` 字段
    保留，需按值脱敏后再落盘。日志和错误文本还可能包含 Bearer/JWT、代理认证或
    其他命名凭据，因此统一递归清洗。仅影响持久化记录，不改动返回给调用方的结果。
    """
    return _canonical_sanitize(value)
