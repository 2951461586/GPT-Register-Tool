"""
共享支付抽取器 helper。

本模块只收纳「真·复制」且可安全共享的 helper —— 即纯函数、不依赖任何抽取器
模块级可变状态（如各模块的 `_proxy_redaction_values` / `_proxy_state` / `SCRIPT_DIR` /
锁 / `register_proxy_for_redaction` / `proxy_label` 等）的同名函数。

说明：报告里标记为「≥4 份同名复制」的 6 个 helper 中，只有
`is_user_already_paid_error` 是纯函数且行为在 blik / ideal / twint 三处字节一致；
kakao 根本未定义它。其余 5 个（redact_log_text / proxy_for_country / save_proxy_state /
new_session / load_token）均读写各自模块的私有可变状态，或存在真实分叉（kakao 的
`load_token` 返回 str、`new_session` 仅 1 个参数；blik 的 `new_session` 多了
IDEAL_USE_LOCAL_PROXY_ONLY 守卫；kakao 的 `save_proxy_state` 走原子临时文件、
`proxy_for_country` 额外追加 sid 地区后缀）。若把这些函数单独搬到本模块，它们会引用
本模块的空状态集合，从而静默破坏「脱敏契约 / 代理状态」——因此保留在各抽取器本地，
不在此收口。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "is_direct_remove_proxy_error",
    "is_proxy_health_failure",
    "is_user_already_paid_error",
]


def is_user_already_paid_error(value: Any) -> bool:
    """Checkout 已支付错误判定：跨 blik / ideal / twint 行为完全一致。"""
    # pi-lens-ignore: no-identity-operator-on-literals
    return "user is already paid" in str(value or "").lower()


def is_direct_remove_proxy_error(reason: str) -> bool:
    """代理凭据/可达性硬错误：跨 ideal / twint / blik 行为完全一致。

    ``record_failure_by_stage`` 用它决定“直接摘除 seed”而非只记一票失败。
    """
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "proxy authentication",
            "proxy auth",
            "resolve proxy",
            "could not resolve proxy",
            "invalid proxy",
            "malformed proxy",
            "unsupported proxy",
            "http 407",
            "status 407",
        )
    )


def is_proxy_health_failure(reason: str) -> bool:
    """代理健康类失败（可重试、按阈值摘除）：跨 ideal / twint / blik 一致。"""
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "目标站不可达",
            "proxy-server",
            "connection reset",
            "recv failure",
            "timed out",
            "timeout",
            "connect tunnel failed",
            "proxy connect aborted",
            "proxy tunneling",
            "proxy handshake",
            "connection refused",
            "ssl connect",
            "tls connect",
            "curl: (28)",
            "curl: (35)",
            "curl: (56)",
            "http_502",
            "http_503",
            "http_504",
        )
    )
