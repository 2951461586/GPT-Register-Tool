"""Failure vocabulary registry (失败词汇单一注册表).

2026-09-12 扫描发现"新增一种失败"要改 3-5 个文件：分类标记散在
``error_classification``，操作建议在 ``registration_policy``，OTP 封禁信号在
``registration_pulse``，批处理类集合在 ``batch_runner``——彼此靠子串字面量
保持一致。本模块把它们收拢为**一份有序数据**：

* ``FAILURE_CLASSES``：分类的优先序即元组顺序（cancelled 最先、auth_state
  最后），``error_classification.classify_error`` 按此顺序匹配；
* ``TERMINAL_ERROR_MARKERS``：硬停标记（跨类，见 ``is_terminal_registration_error``）；
* ``ADVICE``：操作者建议，按 code 是否出现在错误文本中匹配；
* ``OTP_BAN_MARKERS``：OTP 投递被 IP 级封禁的签名（pulse 调度用）；
* ``BATCH_RETRY_CLASSES`` / ``BATCH_DROPPED_CLASSES``：批处理对类别的
  重试/掉号语义。

依赖无关：``error_classification``（被 http_client 引用）必须保持轻导入。
历史拼写与子串格式是**线上数据**（session/progress 里已存在的文本），逐字
保留、不要"顺手规范化"。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureClass:
    """One failure class: its markers, precedence, and batch semantics."""

    code: str
    markers: tuple[str, ...]
    retryable: bool = False
    # batch_runner: a failure of this class is retried with a fresh pool proxy.
    batch_retry: bool = False
    # batch_runner: a failure of this class marks the account dropped.
    batch_dropped: bool = False


FAILURE_CLASSES: tuple[FailureClass, ...] = (
    FailureClass("cancelled", (
        "registration_cancelled",
        "cancelled_by_user",
    )),
    FailureClass("internal", (
        "registration_internal_error",
        "nameerror",
        "attributeerror",
        "typeerror",
        "keyerror",
        "importerror",
        "indexerror",
        "unboundlocalerror",
        "notimplementederror",
        "recursionerror",
        " is not defined",
    )),
    FailureClass("configuration", (
        "unsupported_registration_driver",
        "missing_dependency",
        "invalid_configuration",
        "configuration_error",
    )),
    FailureClass("account", (
        "account_deactivated",
        "account deactivated",
        "account has been deactivated",
        "deleted or deactivated",
        "registration_disallowed",
        "invalid_grant",
        "authenticationfailed",
        "invalid credentials",
        "wrong_email_otp_code",
        "password_verify_failed",
        "phone_recently_used",
        "unsupported_phone_number",
        "fraud_guard",
        "token_invalidated",
    ), batch_dropped=True),
    FailureClass("mailbox", (
        "mailbox_auth_invalid",
        "mailbox_endpoint_unavailable",
        "remail_api_auth_invalid",
        "outlook otp timeout",
        "email_otp_poll_timeout",
        "mailbox otp timeout",
        "mailbox_transport_unavailable",
        "relogin_mailbox_transport_failed",
        "remail poll transport",
    ), batch_retry=True),
    FailureClass("rate_limit", (
        "rate_limit_exceeded",
        "registration_rate_limited",
        "registration_rate_limit_circuit_open",
        "too many requests",
        "http_429",
    ), batch_retry=True),
    FailureClass("network", (
        "tls",
        "ssl",
        "sslerror",
        "eof occurred",
        "connection",
        "connect error",
        "timeout",
        "timed out",
        "proxy",
        "socks",
        "dns",
        "name resolution",
        "winerror 10060",
        "curl: (35)",
        "curl: (28)",
        "curl: (6)",
        "curl: (7)",
        "remote disconnected",
        "connection reset",
        "connection aborted",
        "session_circuit_open",
        "max retries exceeded",
        "/sentinel/req",
        "sentinel quickjs",
        "sentinel_extract_failed",
        "cloudflare",
        "just a moment",
    ), retryable=True, batch_retry=True),
    FailureClass("auth_state", (
        "browser_email_field_not_editable",
        "invalid_auth_step",
        "invalid_state",
        "sign-in session is no longer valid",
        "signup_auth_state",
        "browser_registration_state_unknown",
        "browser_email_verification_stuck",
        "browser_auth_state",
        # Added 2026-09-13 from the live failure log: these four were answered
        # as ``unknown`` (or, for the last one, ``network``) and therefore read
        # as *terminal*, so the retry guard never accumulated a cooldown for
        # them. Measured over 09-08..09-13: ``unknown`` covered 18 of 179
        # failures, all of them ``missing_auth_session_access_token``.
        #
        # ``missing_auth_session_access_token`` is the collapsed outcome when
        # an already-registered address cannot be logged back in;
        # ``registration_outcome._registration_outcome`` already documents that
        # it hides a retryable ``invalid_state`` behind a name that suggests a
        # code defect. ``browser_passwordless_otp_state_unknown`` and
        # ``browser_email_value_mismatch`` are raised by
        # ``browser_flow/form_steps.py`` when the page is in an unexpected
        # state. ``browser_profile_submit_timeout`` was already classified
        # ``auth_state`` by the browser lane (``session._browser_failure_class``
        # matches ``profile_``) and is listed here so the shared classifier
        # agrees instead of being stolen by the bare ``timeout`` marker.
        "missing_auth_session_access_token",
        "browser_passwordless_otp_state_unknown",
        "browser_email_value_mismatch",
        "browser_profile_submit_timeout",
    ), retryable=True, batch_retry=True),
)

# classify_error 找不到任何标记时的类别。
UNKNOWN_CLASS = "unknown"

# 硬停：重试无法改变结果（与类别正交——http_429 属 rate_limit、
# mailbox_auth_invalid 属 mailbox，但都是硬停）。
TERMINAL_ERROR_MARKERS: tuple[str, ...] = (
    "manual_challenge_required",
    "browser_proxy_blocked",
    "mailbox_auth_invalid",
    "mailbox_endpoint_unavailable",
    "remail_api_auth_invalid",
    "invalid_grant",
    "registration_cancelled",
    "session_circuit_open",
    "registration_rate_limit",
    "http_429",
    "stage_budget_exceeded",
)

# 操作者建议：code 出现在错误文本中即命中（registration_policy 消费）。
ADVICE: dict[str, str] = {
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

# OTP 投递被 IP 级封禁的签名（registration_pulse 消费，决定换池重排）。
OTP_BAN_MARKERS: tuple[str, ...] = (
    "otp_not_received",
    "otp_timeout",
    "email_otp_timeout",
    "mailbox_otp_not_received",
    "no_otp",
    "otp_poll_timeout",
)


def failure_class(code: str) -> FailureClass:
    """Return the ``FailureClass`` for ``code``; raises KeyError when absent."""
    for cls in FAILURE_CLASSES:
        if cls.code == code:
            return cls
    raise KeyError(code)


# 批处理语义的派生集合（batch_runner 消费）：batch_retry = 换一个池代理重试；
# batch_dropped = 记掉号。
BATCH_RETRY_CLASSES = frozenset(cls.code for cls in FAILURE_CLASSES if cls.batch_retry)
BATCH_DROPPED_CLASSES = frozenset(cls.code for cls in FAILURE_CLASSES if cls.batch_dropped)


__all__ = [
    "FailureClass",
    "FAILURE_CLASSES",
    "UNKNOWN_CLASS",
    "TERMINAL_ERROR_MARKERS",
    "ADVICE",
    "OTP_BAN_MARKERS",
    "failure_class",
]
