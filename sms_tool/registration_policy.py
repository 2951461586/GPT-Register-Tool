"""Shared retry decisions, not shared mutable state across unrelated scopes.

HTTP sessions, stage admission and mailbox cooldowns have different owners.
They share classification and backoff rules without sharing credentials/state.

Classification (``is_terminal_registration_error``) lives in
``error_classification`` and the pure backoff math (``transport_backoff``,
``bounded_cooldown``) lives in ``backoff``, so the transport layer can use them
without importing this policy module.
"""

from dataclasses import dataclass

from .error_classification import classify_error, error_text, is_terminal_registration_error
from .failure_registry import ADVICE as _ADVICE
from .failure_registry import FAILURE_CLASSES as _REGISTRY_CLASSES

# 类别→可重试的判定来自注册表（retryable=True 的类）。新增类别在
# failure_registry 改一处，这里与 classify_error 自动跟随。
RETRYABLE_CLASSES = frozenset(
    cls.code for cls in _REGISTRY_CLASSES if cls.attempt_retryable
)


@dataclass(frozen=True)
class RetryDecision:
    failure_class: str
    attempt_retryable: bool
    future_batch_eligible: bool
    guard_action: str
    dropped: bool
    advice: str = ""

    @property
    def retryable(self) -> bool:
        """Compatibility alias for the historical same-attempt decision."""
        return self.attempt_retryable


def registration_retry_decision(error, *, failure_class: str = "") -> RetryDecision:
    text = error_text(error)
    category = failure_class or classify_error(error)
    terminal = is_terminal_registration_error(error)
    advice = next((value for code, value in _ADVICE.items() if code in text), "")
    attempt_retryable = category in RETRYABLE_CLASSES and not terminal

    # These three decisions used to be collapsed into ``retryable``:
    # whether to retry the same account now, whether a later batch may try it,
    # and what durable guard state to retain. Keep the policy pure while the
    # retry guard continues to own mutable cross-batch state (ADR-0009).
    if "email_otp_send_stuck" in text:
        future_batch_eligible = True
        guard_action = "otp_pending"
    elif any(
        marker in text
        for marker in (
            "user_already_exists",
            "identity_provider_mismatch",
            "signup_routed_to_login",
            "auth_session_recovery_expired",
            "auth_session_recovery_exhausted",
            "auth_session_recovery_context_missing",
        )
    ):
        future_batch_eligible = False
        guard_action = "dead_end"
    elif terminal:
        # Terminal means "do not repeat this account now". A small set of
        # transient terminal verdicts remains eligible only after a later
        # batch cooldown; all other terminal markers leave no guard entry.
        future_batch_eligible = any(
            marker in text
            for marker in (
                "http_429",
                "registration_rate_limit",
                "mailbox_endpoint_unavailable",
            )
        )
        guard_action = "cooldown" if future_batch_eligible else "none"
    elif category in {"network", "auth_state", "rate_limit"}:
        future_batch_eligible = True
        guard_action = "cooldown"
    elif category == "mailbox" and not any(
        marker in text
        for marker in (
            "mailbox_auth_invalid",
            "mailbox_endpoint_unavailable",
            "remail_api_auth_invalid",
        )
    ):
        future_batch_eligible = True
        guard_action = "cooldown"
    else:
        future_batch_eligible = False
        guard_action = "none"

    return RetryDecision(
        failure_class=category,
        attempt_retryable=attempt_retryable,
        future_batch_eligible=future_batch_eligible,
        guard_action=guard_action,
        dropped=category == "account",
        advice=advice,
    )
