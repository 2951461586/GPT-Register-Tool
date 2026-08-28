"""
注册结果判定模块。

从 registration.py 解耦出来，包含：
- 账号创建阶段的错误归一化（_create_account_error）
- AT 稳定性探测（_probe_registration_access_token）
- 注册链路是否依赖 refresh_token / 手机验证码的开关（_requires_* 两个小函数）

这些函数与主流程的 register_loop 主入口解耦，便于单独测试或复用。
"""

import time
from collections.abc import Mapping

from .account_liveness import probe_account_liveness
from .config import CFG
from .error_classification import classify_error
from .registration_progress import registration_stage
from .sanitizer import sanitize as _sanitize, sanitize_text as _sanitize_text
from .utils import _timing_summary


def _create_account_error(create_ok, create_data):
    """从 create_account 响应中提炼出人类可读的错误码/消息。"""
    if create_ok:
        return ""
    create_error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    create_code = str(create_error.get("code") or "").strip()
    create_message = str(create_error.get("message") or "").strip()
    error = "create_account_failed"
    if create_code:
        error += f":{create_code}"
    if create_message:
        error += f": {create_message}"
    return error


def _probe_registration_access_token(
    access_token,
    auth_session,
    proxy=None,
    *,
    cfg=None,
    probe_fn=None,
    stage_fn=None,
    sleep_fn=None,
):
    """
    多轮 AT 稳定性探测。

    连续 count 次探测 access_token 可用性，所有探测都 200 才算 AT 稳定；
    中间任意一轮非 200 立即返回，并附带每轮的 status_code 向量。
    """
    runtime_cfg = cfg if isinstance(cfg, Mapping) else CFG
    registration_value = runtime_cfg.get("registration")
    registration_cfg = registration_value if isinstance(registration_value, Mapping) else {}
    probe_fn = probe_fn or probe_account_liveness
    stage_fn = stage_fn or registration_stage
    sleep_fn = sleep_fn or time.sleep
    try:
        timeout = max(5, min(int(registration_cfg.get("at_probe_timeout_seconds") or 30), 120))
    except (TypeError, ValueError):
        timeout = 30
    try:
        count = max(1, min(int(registration_cfg.get("at_stability_probe_count") or 2), 3))
    except (TypeError, ValueError):
        count = 2
    try:
        delay = max(0.0, min(float(registration_cfg.get("at_stability_probe_delay_seconds") or 10), 60.0))
    except (TypeError, ValueError):
        delay = 10.0
    probes = []
    for index in range(count):
        probe = probe_fn(
            {"access_token": access_token, "auth_session": auth_session or {}},
            proxy=proxy,
            timeout=timeout,
        )
        probes.append(probe)
        if int(probe.get("status_code") or 0) != 200:
            break
        if index + 1 < count and delay:
            stage_fn("access_token_stability_wait")
            sleep_fn(delay)
            stage_fn("access_token_probe")
    result = dict(probes[-1] if probes else {})
    result["stability_probe_count"] = len(probes)
    result["stability_status_codes"] = [int(item.get("status_code") or 0) for item in probes]
    result["stability_window_seconds"] = round(delay * max(0, len(probes) - 1), 3)
    return result


def _retain_registration_checkpoint(success, access_token, at_probe):
    """Keep a post-create checkpoint when only the AT transport probe failed.

    The account and token already exist at this point.  Clearing the checkpoint
    forces the batch retry to submit the signup flow a second time, which turns
    a transient proxy failure into ``invalid_state`` or a duplicate signup.
    """
    if success or not str(access_token or "").strip():
        return False
    probe = at_probe if isinstance(at_probe, Mapping) else {}
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    return status_code == 0


def _registration_requires_refresh_token(runtime_cfg=None):
    """协议注册链路是否要求最终产出的 session 必须包含 refresh_token。"""
    source = runtime_cfg if isinstance(runtime_cfg, Mapping) else CFG
    value = source.get("codex_oauth")
    cfg = value if isinstance(value, Mapping) else {}
    return bool(cfg.get("require_registration_refresh_token", True))


def _registration_requires_phone_verification(phone_pool=None, runtime_cfg=None):
    """协议注册链路是否要求手机二次校验（默认：有 phone_pool 则开启）。"""
    source = runtime_cfg if isinstance(runtime_cfg, Mapping) else CFG
    value = source.get("codex_oauth")
    cfg = value if isinstance(value, Mapping) else {}
    default = bool(phone_pool)
    return bool(cfg.get("require_registration_phone_verification", default))


def _mailbox_snapshot(mailbox):
    if not mailbox:
        return {}
    return {
        "email": getattr(mailbox, "email", ""),
        "password": getattr(mailbox, "password", ""),
        "login_password": getattr(mailbox, "login_password", ""),
        "refresh_token": getattr(mailbox, "refresh_token", ""),
        "access_token": getattr(mailbox, "access_token", ""),
        "source": getattr(mailbox, "source", ""),
        "provider": getattr(mailbox, "provider", ""),
        "order_no": getattr(mailbox, "order_no", ""),
        "token": getattr(mailbox, "token", ""),
        "client_secret": getattr(mailbox, "client_secret", ""),
        "auth_mode": getattr(mailbox, "auth_mode", ""),
        "sender_name": getattr(mailbox, "sender_name", ""),
        "purchase_id": getattr(mailbox, "purchase_id", ""),
        "project_name": getattr(mailbox, "project_name", ""),
        "price": getattr(mailbox, "price", ""),
        "purchase_total_cost": getattr(mailbox, "purchase_total_cost", ""),
        "balance_after": getattr(mailbox, "balance_after", ""),
    }


def _browser_mailbox_snapshot(mailbox):
    """Return the non-secret mailbox metadata allowed in failed browser results.

    ``_mailbox_snapshot`` is also used by internal checkpoints and OAuth
    hand-off code, where mailbox credentials may still be needed. Successful
    browser results retain that snapshot until ``build_session_file`` persists
    the canonical artifact; failures use this allow-list because they are only
    operator-visible workflow payloads.
    """
    if not mailbox:
        return {}
    fields = (
        "email",
        "source",
        "provider",
        "order_no",
        "auth_mode",
        "sender_name",
        "purchase_id",
        "project_name",
        "price",
        "purchase_total_cost",
        "balance_after",
    )
    return {
        key: value
        for key in fields
        if (value := getattr(mailbox, key, "")) not in (None, "")
    }


def _failure_result(error, email="", mailbox=None, password=""):
    result = {"success": False, "error": _sanitize_text(error), "failure_class": classify_error(_sanitize_text(error)), "timing": _timing_summary()}
    if email:
        result["email"] = email
    if password:
        result["password"] = "[REDACTED]"
    mailbox_data = _mailbox_snapshot(mailbox)
    if mailbox_data:
        result["mailbox"] = mailbox_data
    return _sanitize(result)


def _registration_outcome(create_ok, create_data, access_token, at_probe):
    probe = at_probe if isinstance(at_probe, dict) else {}
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    create_error = _create_account_error(create_ok, create_data or {})
    success = bool(str(access_token or "").strip()) and status_code == 200
    if success:
        return True, "", create_error
    if not str(access_token or "").strip():
        return False, create_error or "missing_auth_session_access_token", ""
    if status_code:
        return False, f"access_token_probe_http_{status_code}", create_error
    probe_error = str(probe.get("error") or probe.get("status") or "unknown").strip()
    return False, f"access_token_probe_failed:{probe_error}", create_error


def _oauth_result_summary(result):
    if not isinstance(result, dict):
        return {}
    summary = {key: value for key, value in result.items() if key != "tokens"}
    tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
    if tokens:
        summary["has_access_token"] = bool(tokens.get("access_token"))
        summary["has_refresh_token"] = bool(tokens.get("refresh_token"))
        summary["has_id_token"] = bool(tokens.get("id_token"))
    return summary
