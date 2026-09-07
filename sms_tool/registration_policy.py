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

RETRYABLE_CLASSES = frozenset({"network", "auth_state"})
_ADVICE = {
    "manual_challenge_required": "需要人工验证，已停止自动重试。",
    "browser_proxy_blocked": "目标拒绝了当前连接，停止重试并检查服务访问政策。",
    "browser_email_field_missing": "未找到邮箱框，请检查页面结构和登录状态。",
    "browser_email_field_not_editable": "邮箱框尚不可编辑，请检查页面加载或人工验证状态。",
    "browser_registration_state_unknown": "页面状态未知，请保留脱敏诊断并人工检查。",
    "browser_email_verification_stuck": "邮箱验证后的页面未跳转，请检查验证状态，勿重复创建账号。",
    "browser_unexpected_identity_provider": "跳转到了意外身份提供方，已停止自动化。",
    "mailbox_auth_invalid": "邮箱凭据已隔离，请修复邮箱池后再试。",
    "mailbox_endpoint_unavailable": "邮箱端点不可用，暂停五分钟后可重新检查。",
    "remail_api_auth_invalid": "邮箱服务认证失败，请检查服务配置。",
    "registration_retry_cooldown": "该邮箱处于冷却期，请等待后重试。",
    "stage_budget_exceeded": "阶段超过预算，请检查耗时和阻塞点。",
}


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
