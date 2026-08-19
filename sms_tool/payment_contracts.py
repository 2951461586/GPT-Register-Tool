"""Typed result contracts and retry gates for protocol-payment operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypedDict


class PaymentTerminalState(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed_out"


class PaymentResultDict(TypedDict, total=False):
    ok: bool
    status: str
    payment_method: str
    operation: str
    error: str
    error_code: str
    error_stage: str
    retryable: bool
    side_effect_started: bool
    requires_reconciliation: bool
    url: str
    operation_id: str
    idempotency_key_hash: str


@dataclass(frozen=True)
class PaymentRequest:
    payment_method: str
    access_token: str = field(repr=False)
    proxy: Any = None
    auth_context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    runtime_config: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)

    @classmethod
    def create(
        cls,
        *,
        payment_method: str,
        access_token: str,
        proxy: Any = None,
        auth_context: Mapping[str, Any] | None = None,
        runtime_config: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> "PaymentRequest":
        return cls(
            payment_method=str(payment_method or ""),
            access_token=str(access_token or ""),
            proxy=proxy,
            auth_context=MappingProxyType(dict(auth_context or {})),
            runtime_config=MappingProxyType(dict(runtime_config or {})),
            options=MappingProxyType(dict(options or {})),
        )


@dataclass(frozen=True)
class PaymentError:
    code: str = ""
    stage: str = ""
    message: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class StageOutcome:
    status: str
    side_effect_started: bool = False
    requires_reconciliation: bool = False


_SIDE_EFFECT_STAGES = {
    "approve",
    "approval",
    "confirm",
    "confirmation",
    "payment_confirm",
    "payment_submit",
    "provider_redirect",
    "redirect",
    "redirect_follow",
    "follow_redirect",
    "poll",
    "blik_submit",
    "blik_confirm",
}
_IN_FLIGHT_STATUSES = {
    "submitted",
    "processing",
    "requires_action",
    "awaiting_confirmation",
}


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


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


@dataclass(frozen=True)
class PaymentResult:
    ok: bool
    status: str
    payment_method: str
    operation: str
    error: PaymentError
    outcome: StageOutcome
    url: str = ""
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        payment_method: str = "",
        operation: str = "extract_link",
        terminal_state: str = "",
    ) -> "PaymentResult":
        raw = dict(value or {})
        ok = _as_bool(raw.get("ok")) is True
        status = _normalized(raw.get("status") or terminal_state)
        if not status:
            status = PaymentTerminalState.COMPLETED.value if ok else PaymentTerminalState.FAILED.value

        method = str(raw.get("payment_method") or payment_method or "").strip()
        operation_name = str(raw.get("operation") or operation or "extract_link").strip()
        error_stage = _normalized(raw.get("error_stage") or raw.get("stage") or raw.get("failed_step"))
        reconciliation = _as_bool(raw.get("requires_reconciliation")) is True or status == "unknown"

        explicit_side_effect = _as_bool(raw.get("side_effect_started"))
        inferred_side_effect = (
            error_stage in _SIDE_EFFECT_STAGES
            or (operation_name == "execute_payment" and (ok or status in _IN_FLIGHT_STATUSES))
        )
        side_effect_started = bool(explicit_side_effect) if explicit_side_effect is not None else inferred_side_effect
        side_effect_started = side_effect_started or reconciliation

        retryable = _as_bool(raw.get("retryable")) is True
        if ok or side_effect_started or reconciliation or status in {"unknown", "cancelled", "completed"}:
            retryable = False

        default_error_code = {
            "cancelled": "payment_link_cancelled",
            "unknown": "payment_outcome_unknown",
            "timed_out": "payment_link_timed_out",
        }.get(status, "payment_link_extraction_failed")
        if not raw and not ok:
            default_error_code = "invalid_adapter_result"
        default_stage = "adapter_contract" if not raw and not ok else (error_stage or "adapter")
        error = PaymentError(
            code=str(raw.get("error_code") or ("" if ok else default_error_code)).strip(),
            stage="" if ok else default_stage,
            message=str(raw.get("error") or ("" if ok else "payment-link extraction failed")).strip(),
            retryable=retryable,
        )
        outcome = StageOutcome(
            status=status,
            side_effect_started=side_effect_started,
            requires_reconciliation=reconciliation,
        )
        known = {
            "ok", "status", "payment_method", "operation", "error", "error_code",
            "error_stage", "retryable", "side_effect_started", "requires_reconciliation", "url",
        }
        extras = MappingProxyType({key: item for key, item in raw.items() if key not in known})
        return cls(
            ok=ok,
            status=status,
            payment_method=method,
            operation=operation_name,
            error=error,
            outcome=outcome,
            url=str(raw.get("url") or "").strip(),
            extra=extras,
        )

    def to_dict(self) -> PaymentResultDict:
        result: dict[str, Any] = dict(self.extra)
        result.update({
            "ok": self.ok,
            "status": self.outcome.status,
            "payment_method": self.payment_method,
            "operation": self.operation,
            "error": self.error.message,
            "error_code": self.error.code,
            "error_stage": self.error.stage,
            "retryable": self.error.retryable,
            "side_effect_started": self.outcome.side_effect_started,
            "requires_reconciliation": self.outcome.requires_reconciliation,
            "url": self.url,
        })
        return result  # type: ignore[return-value]


def payment_retry_allowed(result: Mapping[str, Any] | None) -> bool:
    """Return true only for an explicitly retryable pre-side-effect failure."""
    contract = PaymentResult.from_mapping(result)
    return not contract.ok and contract.error.retryable


def payment_history_metadata(value: Any) -> Any:
    """Replace persisted payment artifacts with presence-only metadata."""
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            is_artifact_value = item is None or isinstance(item, str)
            is_url = is_artifact_value and (lowered == "url" or lowered.endswith("_url"))
            is_qr_artifact = is_artifact_value and lowered in {"qr_data", "qr_path"}
            if is_url or is_qr_artifact:
                output[f"{key}_present"] = bool(str(item or "").strip())
            else:
                output[key] = payment_history_metadata(item)
        return output
    if isinstance(value, (list, tuple)):
        return [payment_history_metadata(item) for item in value]
    return value
