"""注册结果契约的单一装配点。

协议路径（``registration_handlers.RegistrationEmailWorkflow.finalize``）与浏览器
路径（``registration_drivers/browser_flow/orchestrator.run_browser_registration``）
产出的 result dict 共享同一批核心键。历史上这 25 个键在两条路径里各手写一份，
键集合只靠注释保持同步（orchestrator 的注释原话是"mirror the protocol path"），
漂移只能靠测试兜底。

本模块把共享核心收拢到 :func:`build_registration_result`：

* ``COMMON_RESULT_KEYS`` 是两条路径都必须提供的核心键契约，供契约测试断言；
* 两条路径各自的专有键（协议的 ``quota``/``timing``/``sentinel_version`` 等，
  浏览器的 ``registration_driver``/``browser_diagnostics`` 等）通过 ``extra``
  合入，不进入共享契约。

判定逻辑（成功/失败/何种错误）仍归 :mod:`sms_tool.registration_outcome` 的
``_registration_outcome`` / ``_probe_registration_access_token``，本模块只负责
装配，不重复判定。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sanitizer import sanitize_text
from .registration_policy import registration_retry_decision


# 两条路径共有的核心键。契约测试（tests/test_registration_result_contract.py）
# 断言两条路径产出的 result 都覆盖这一集合；新增共有键时先改这里再改两条路径。
COMMON_RESULT_KEYS = frozenset({
    "access_token",
    "auth_fingerprint_profile",
    "auth_session",
    "birthdate",
    "cookie_header",
    "device_id",
    "email",
    "error",
    "identity_context",
    "mailbox",
    "name",
    "password",
    "plan_type",
    "post_registration_ready",
    "quota_status",
    "register_method",
    "registration_country",
    "registration_mode",
    "registration_state",
    "registration_success_basis",
    "registration_warning",
    "response",
    "session_type",
    "source",
    "success",
    "totp_secret",
    "twofa_enrollment",
    "id_token",
})


def build_registration_result(
    *,
    success: bool,
    registration_mode: str,
    registration_state: str,
    email: str = "",
    error: Any = "",
    register_method: str = "email",
    session_type: str = "web",
    password: str = "",
    name: str = "",
    birthdate: str = "",
    access_token: str = "",
    id_token: str = "",
    auth_session: Any = None,
    cookie_header: str = "",
    device_id: str = "",
    identity_context: Mapping[str, Any] | None = None,
    auth_fingerprint_profile: str = "",
    response: Mapping[str, Any] | None = None,
    quota_status: str = "",
    totp_secret: str = "",
    twofa_enrollment: Mapping[str, Any] | None = None,
    registration_success_basis: str = "",
    registration_country: str = "",
    registration_warning: Any = "",
    post_registration_ready: bool = False,
    mailbox_snapshot: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one registration result dict from already-computed values.

    The caller owns every decision (success, state classification, password
    redaction); this function only fixes the shared key set and the redaction
    of free-text fields. ``extra`` merges path-specific keys last.
    """
    result: dict[str, Any] = {
        "success": bool(success),
        "error": sanitize_text(error),
        "email": email,
        "source": "register",
        "register_method": register_method,
        "session_type": session_type,
        "plan_type": "unknown",
        "password": password,
        "name": name,
        "birthdate": birthdate,
        "response": dict(response or {}),
        "auth_session": auth_session,
        "access_token": access_token or "",
        "id_token": id_token,
        "quota_status": quota_status,
        "totp_secret": totp_secret or "",
        "twofa_enrollment": twofa_enrollment or {"ok": False, "reason": "skipped"},
        "registration_success_basis": registration_success_basis,
        "registration_state": registration_state,
        "registration_country": registration_country,
        "registration_warning": sanitize_text(registration_warning),
        "post_registration_ready": bool(post_registration_ready),
        "cookie_header": cookie_header,
        "registration_mode": registration_mode,
        "device_id": device_id,
        "identity_context": identity_context,
        "auth_fingerprint_profile": auth_fingerprint_profile,
        "mailbox": mailbox_snapshot or {},
    }
    if extra:
        result.update(extra)
    decision = registration_retry_decision(result.get("error"))
    result.setdefault("failure_class", "" if success else decision.failure_class)
    result.setdefault("retryable", not success and decision.retryable)
    result.setdefault("error_advice", "" if success else decision.advice)
    missing = COMMON_RESULT_KEYS - result.keys()
    if missing:  # pragma: no cover - guards future edits to this function
        raise ValueError(f"registration result missing common keys: {sorted(missing)}")
    return result


__all__ = ["COMMON_RESULT_KEYS", "build_registration_result"]
