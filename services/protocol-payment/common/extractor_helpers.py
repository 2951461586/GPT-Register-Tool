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


def is_user_already_paid_error(value: Any) -> bool:
    """Checkout 已支付错误判定：跨 blik / ideal / twint 行为完全一致。"""
    return "user is already paid" in str(value or "").lower()
