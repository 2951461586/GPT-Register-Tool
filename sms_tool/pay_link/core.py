"""Core state machine for payment-link extraction and capability probing."""

from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Mapping

from ..config import ConfigError, validate_config
from ..operation_journal import (
    PaymentOperationConflict,
    PaymentOperationStore,
    conflict_result as payment_operation_conflict_result,
)
from ..payment.contracts import PaymentRequest
from ..payment.errors import annotate_error, error_stage_of
from ..payment.executor import PaymentExecutionRequest, PaymentFlowExecutor
from ..payment.routing import PaymentRoutePlan, PaymentRoutePlanner
from .base import PAYMENT_METHODS, _LOGGER, _config_data, _offline_refusal, _redact_sensitive_text
from .normalize import _classify_exception, _normalize_result
from .persistence import _safe_persist_run
from .registry import (
    PAYMENT_ADAPTERS,
    _enabled_methods,
    normalize_payment_method,
    probe_team_trial_eligibility,
)

#: Failures the validation / route-planning preamble normalises instead of
#: letting escape. Both public entry points share this tuple so a pool outage
#: cannot be a normalised result in one and a raw exception in the other.
_PLANNING_FAILURES = (ConfigError, ValueError, TypeError, OSError, RuntimeError)

#: Opt-in switch for the page-context checkout fallback. See
#: :func:`_browser_fallback_transport`.
CAPABILITY_BROWSER_FALLBACK_ENV = "PP_CAPABILITY_BROWSER_FALLBACK"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _browser_fallback_transport() -> Any:
    """The opt-in browser fallback for the checkout create gate, else ``None``."""
    if str(os.environ.get(CAPABILITY_BROWSER_FALLBACK_ENV, "")).strip().lower() not in _TRUTHY:
        return None
    from ..payment.capability_browser import browser_fallback_transport

    return browser_fallback_transport()


def _annotate_planning_failure(exc: Exception) -> Exception:
    """Attach the preamble stage the raise site did not name."""
    if not error_stage_of(exc):
        annotate_error(
            exc,
            error_stage=("validation" if isinstance(exc, (ValueError, ConfigError)) else "proxy_setup"),
        )
    return exc


def _dispatch_payment_request(request: PaymentExecutionRequest) -> Mapping[str, Any]:
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
    """Execute one payment-link extraction or capability probe through the common state machine."""
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
            raise ValueError(f"payment method disabled by protocol_payments.enabled_methods: {method}")
        offline_error = _offline_refusal(method, probe_only=bool(options.get("probe_only")))
        if offline_error is not None:
            raise offline_error
        if plan is not None and plan.payment_method != method:
            raise ValueError(f"payment route plan method mismatch: {plan.payment_method} != {method}")
        if plan is None:
            plan = PaymentRoutePlanner(source).plan(
                method,
                options=options,
                default_proxy=proxy,
            )
    except _PLANNING_FAILURES as exc:
        planning_error = _annotate_planning_failure(exc)
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
        result.update(
            {
                "payment_method": method_name,
                "operation": operation_name,
                "manager_state": result["status"],
            }
        )
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
        return _dispatch_payment_request(request)

    executor = PaymentFlowExecutor(
        run_adapter,
        normalizer=(lambda result: _normalize_result(spec, result)) if spec else None,
        exception_classifier=_classify_exception,
        error_sanitizer=_redact_sensitive_text,
        progress=transactional_progress,
    )
    try:
        result = executor.run(
            PaymentExecutionRequest(
                payment_method=method_name,
                access_token=str(access_token or ""),
                route_plan=plan,
                auth_context=dict(auth_context or {}),
                runtime_config=source,
                options=routed_options,
                operation=operation_name,
                operation_id=payment_operation.operation_id,
                idempotency_key_hash=payment_operation.idempotency_key_hash,
            )
        )
        payment_operation.finish(result)
    except Exception:
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
    """Run the shared Checkout observation without any link-execution adapter."""
    method = normalize_payment_method(payment_method)
    source = _config_data(runtime_config)
    options = dict(kwargs)
    plan = options.pop("payment_route_plan", None)
    try:
        if not method:
            raise ValueError(f"unsupported payment method: {payment_method}")
        if not isinstance(plan, PaymentRoutePlan):
            plan = PaymentRoutePlanner(source).plan(
                method,
                options=options,
                default_proxy=proxy,
            )
        elif plan.payment_method != method:
            raise ValueError(f"payment route plan method mismatch: {plan.payment_method} != {method}")
    except _PLANNING_FAILURES as exc:
        from ..payment.capability import capability_probe_failure

        return capability_probe_failure(method or payment_method, _annotate_planning_failure(exc))

    assert isinstance(plan, PaymentRoutePlan)
    options.update(plan.to_adapter_options())
    options.pop("probe_only", None)

    if method == "team_trial":
        return probe_team_trial_eligibility(
            access_token=access_token,
            proxy=str(plan.checkout_proxy or "").strip(),
            auth_context=auth_context,
            timeout=options.get("timeout", 30),
        )

    from ..payment.capability import payment_method_capability_probe

    fallback = _browser_fallback_transport()
    if fallback is not None:
        options.setdefault("fallback_transport", fallback)

    return payment_method_capability_probe(
        access_token=access_token,
        payment_method=method,
        auth_context=auth_context,
        proxy=plan.checkout_proxy,
        **options,
    )
