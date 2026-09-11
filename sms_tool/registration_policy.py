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
    cls.code for cls in _REGISTRY_CLASSES if cls.retryable
)


@dataclass(frozen=True)
class RetryDecision:
    failure_class: str
    retryable: bool
    advice: str = ""


def registration_retry_decision(error, *, failure_class: str = "") -> RetryDecision:
    text = error_text(error)
    category = failure_class or classify_error(error)
    terminal = is_terminal_registration_error(error)
    advice = next((value for code, value in _ADVICE.items() if code in text), "")
    return RetryDecision(category, category in RETRYABLE_CLASSES and not terminal, advice)
