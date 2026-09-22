"""Unified result contract for plan and account-liveness checks."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..sanitizer import drop_sensitive_fields

class HealthCheckKind(str, Enum):
    PLAN = "plan"
    DEEP_LIVENESS = "deep_liveness"


class HealthState(str, Enum):
    HEALTHY = "healthy"
    TOKEN_INVALID = "token_invalid"
    RECOVERED = "recovered"
    DEACTIVATED = "deactivated"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AccountHealthResult:
    email: str
    check: str
    state: str
    ok: bool
    status_code: int = 0
    quota_status: str = ""
    plan_type: str = ""
    promotion_status: str = ""
    recoverable: bool = False
    recovered: bool = False
    terminal: bool = False
    persisted: bool = False
    error: str = ""
    attempts: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    checked_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["details"] = sanitize_health_details(self.details)
        value["attempts"] = list(self.attempts)
        return value

    def with_persisted(self, persisted: bool) -> "AccountHealthResult":
        value = self.to_dict()
        value["persisted"] = bool(persisted)
        value["attempts"] = tuple(value.get("attempts") or ())
        return AccountHealthResult(**value)


def plan_health_result(email: str, probe: Mapping[str, Any]) -> AccountHealthResult:
    ok = bool(probe.get("ok"))
    status_code = int(probe.get("status_code") or 0)
    token_invalid = status_code == 401 or str(probe.get("error") or "") == "token_invalid"
    state = HealthState.HEALTHY if ok else HealthState.TOKEN_INVALID if token_invalid else HealthState.FAILED
    return AccountHealthResult(
        email=_email(email),
        check=HealthCheckKind.PLAN.value,
        state=state.value,
        ok=ok,
        status_code=status_code,
        plan_type=str(probe.get("current_plan_type") or ""),
        promotion_status=str(probe.get("promotion_status") or ""),
        recoverable=token_invalid,
        error="" if ok else str(probe.get("error") or "plan_check_failed")[:300],
        details=probe,
    )


def liveness_health_result(
    email: str,
    probe: Mapping[str, Any],
    *,
    recovery: Mapping[str, Any] | None = None,
    final_probe: Mapping[str, Any] | None = None,
) -> AccountHealthResult:
    initial = dict(probe or {})
    recovery_data = dict(recovery or {})
    final = dict(final_probe or initial)
    recovered = bool(recovery_data.get("ok")) and int(final.get("status_code") or 0) == 200
    terminal = bool(
        recovery_data.get("terminal")
        or str(recovery_data.get("error") or initial.get("error") or "").lower() == "account_deactivated"
    )
    status_code = int(final.get("status_code") or initial.get("status_code") or 0)
    token_invalid = status_code == 401 or str(initial.get("status") or "") == "token_invalid"
    ok = status_code == 200 and bool(final.get("ok"))
    if terminal:
        state = HealthState.DEACTIVATED
    elif recovered:
        state = HealthState.RECOVERED
    elif ok:
        state = HealthState.HEALTHY
    elif token_invalid:
        state = HealthState.TOKEN_INVALID
    else:
        state = HealthState.FAILED
    attempts = tuple(
        str(item.get("mode") or "")
        for item in recovery_data.get("attempts", [])
        if isinstance(item, Mapping) and item.get("mode")
    )
    if recovery_data.get("mode") and not attempts:
        attempts = (str(recovery_data.get("mode")),)
    return AccountHealthResult(
        email=_email(email),
        check=HealthCheckKind.DEEP_LIVENESS.value,
        state=state.value,
        ok=ok,
        status_code=status_code,
        quota_status=str(final.get("quota_status") or initial.get("quota_status") or ""),
        recoverable=token_invalid and not terminal,
        recovered=recovered,
        terminal=terminal,
        error="" if ok else str(recovery_data.get("error") or final.get("error") or initial.get("error") or "")[:300],
        attempts=attempts,
        details={"initial_probe": initial, "recovery": recovery_data, "final_probe": final},
    )


def sanitize_health_details(value: Any) -> Any:
    """Compatibility name for the shared persisted-diagnostic sanitizer."""
    return drop_sensitive_fields(value, max_string_length=1000)


def resolve_account_health_budgets(
    config: Mapping[str, Any] | None,
    *,
    relogin_timeout: Any = None,
    batch_timeout: Any = None,
    account_timeout: Any = None,
) -> dict[str, int]:
    """Resolve health workflow budgets once for every entrypoint."""
    health = config.get("account_health") if isinstance(config, Mapping) else {}
    health = health if isinstance(health, Mapping) else {}

    def resolve(explicit: Any, key: str, default: int, minimum: int) -> int:
        raw = explicit if explicit is not None else health.get(key)
        try:
            return max(minimum, int(raw if raw is not None else default))
        except (TypeError, ValueError):
            return default

    return {
        "relogin_timeout": resolve(
            relogin_timeout,
            "relogin_timeout_seconds",
            300,
            30,
        ),
        "batch_timeout": resolve(
            batch_timeout,
            "batch_timeout_seconds",
            900,
            30,
        ),
        "account_timeout": resolve(
            account_timeout,
            "account_timeout_seconds",
            360,
            30,
        ),
    }


def _email(value: str) -> str:
    return str(value or "").strip().lower()


__all__ = [
    "AccountHealthResult",
    "HealthCheckKind",
    "HealthState",
    "liveness_health_result",
    "plan_health_result",
    "resolve_account_health_budgets",
    "sanitize_health_details",
]
