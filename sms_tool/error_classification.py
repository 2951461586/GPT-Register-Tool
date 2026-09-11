"""Shared failure classification for batch-safe account handling.

分类词汇（各 FailureClass 的标记、硬停标记、优先序）的**单一事实源**是
``sms_tool/failure_registry.py``——本模块保留全部公共元组名与
``classify_error``/``is_terminal_registration_error`` 语义，但每个元组都从
注册表派生（2026-09-12 扫描：新增一种失败曾要改 3-5 个文件，现在只改注册表）。
"""

from __future__ import annotations

import json

from .failure_registry import FAILURE_CLASSES, TERMINAL_ERROR_MARKERS as _TERMINAL_MARKERS

# 注册表顺序即匹配优先序（cancelled 最先）。以下公共元组名被多个模块与测试
# 直接引用，作为兼容视图保留。
_CANCELLED_CLASS, _INTERNAL_CLASS, _CONFIGURATION_CLASS, _ACCOUNT_CLASS, \
    _MAILBOX_CLASS, _RATE_LIMIT_CLASS, _NETWORK_CLASS, _AUTH_STATE_CLASS = FAILURE_CLASSES

NETWORK_ERROR_MARKERS = _NETWORK_CLASS.markers

ACCOUNT_ERROR_MARKERS = _ACCOUNT_CLASS.markers

MAILBOX_ERROR_MARKERS = _MAILBOX_CLASS.markers

AUTH_STATE_ERROR_MARKERS = _AUTH_STATE_CLASS.markers

RATE_LIMIT_ERROR_MARKERS = _RATE_LIMIT_CLASS.markers

CANCELLED_ERROR_MARKERS = _CANCELLED_CLASS.markers

# A transport failure that escapes to the unexpected-exception handler is
# wrapped as ``registration_internal_error:<Type>:<msg>``, and because INTERNAL
# is tested before NETWORK the wrapper used to win -- demoting a retryable
# network error to a terminal-looking internal one. Observed 2026-09-12:
# ``registration_internal_error:RuntimeError:Failed to perform, curl: (35) ...``
# was recorded as ``internal`` (not retryable) although its own text classifies
# as ``network`` (retryable).
#
# Only *curl error codes* may demote ``internal``, and that restriction is the
# whole point: the generic network markers ("connection", "timeout", "proxy",
# "dns") also occur inside genuine Python exception text --
# ``NameError: name 'connection_pool' is not defined`` is a bug, and answering
# "retry the network" to it is worse than leaving it internal. A curl code
# cannot appear in a Python exception message, so it is unambiguous.
#
# Derived from NETWORK_ERROR_MARKERS rather than hardcoded, so adding a new
# ``curl: (NN)`` marker automatically extends the demotion.
CURL_TRANSPORT_MARKERS = tuple(
    marker for marker in NETWORK_ERROR_MARKERS if marker.startswith("curl:")
)

INTERNAL_ERROR_MARKERS = _INTERNAL_CLASS.markers

CONFIGURATION_ERROR_MARKERS = _CONFIGURATION_CLASS.markers

# Hard stops: retrying cannot change the outcome. Kept beside classify_error
# because this is pure classification -- the transport layer needs it and must
# not import registration policy to get it. 单一事实源在 failure_registry。
TERMINAL_ERROR_MARKERS = _TERMINAL_MARKERS


def error_text(value) -> str:
    if isinstance(value, dict):
        parts = []
        for key in ("error", "error_code", "message", "body", "status", "scan_status", "quota_status"):
            item = value.get(key)
            if item:
                parts.append(str(item))
        for key in ("refresh", "oauth", "relogin", "token_probe"):
            item = value.get(key)
            if isinstance(item, dict):
                parts.append(error_text(item))
        if not parts:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str)[:1000])
            except Exception:
                pass
        return " ".join(parts).lower()
    return str(value or "").lower()


def classify_error(value) -> str:
    text = error_text(value)
    # See CURL_TRANSPORT_MARKERS: a wrapped transport failure is still a
    # transport failure, but only a curl code is allowed to prove it.
    internal_match = any(marker in text for marker in INTERNAL_ERROR_MARKERS)
    curl_match = any(marker in text for marker in CURL_TRANSPORT_MARKERS)
    for cls in FAILURE_CLASSES:
        if cls.code == "internal":
            # internal 的例外 demotion（curl 码证明 transport）仍在此实现。
            if internal_match and not curl_match:
                return "internal"
            continue
        if any(marker in text for marker in cls.markers):
            return cls.code
    return "unknown"


def is_terminal_registration_error(value) -> bool:
    """True when the error is a hard stop: no retry can change the outcome.

    Pure classification, so it lives here instead of in ``registration_policy``.
    That is what lets the transport layer (``http_client``) consult it without
    a top-level import back edge into the policy layer.
    """
    return any(marker in error_text(value) for marker in TERMINAL_ERROR_MARKERS)
