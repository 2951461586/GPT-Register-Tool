"""core submodule of the former payment_link_manager.py (mechanical split, bodies unchanged)."""

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

from .adapters import _run_regional_wallet_adapter
from .base import PAYMENT_METHODS, _LOGGER, _config_data, _redact_sensitive_text
from .normalize import _classify_exception, _normalize_result
from .persistence import _safe_persist_run
from .registry import PAYMENT_ADAPTERS, _enabled_methods, normalize_payment_method


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
        return _plm._run_wallet_adapter(
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

    from ..payment_capability import payment_method_capability_probe

    return payment_method_capability_probe(
        access_token=access_token,
        payment_method=method,
        auth_context=auth_context,
        proxy=plan.checkout_proxy,
        **options,
    )

