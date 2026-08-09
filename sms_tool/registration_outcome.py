"""
注册结果判定模块。

从 registration.py 解耦出来，包含：
- 账号创建阶段的错误归一化（_create_account_error）
- AT 稳定性探测（_probe_registration_access_token）
- 注册链路是否依赖 refresh_token / 手机验证码的开关（_requires_* 两个小函数）

这些函数与主流程的 register_loop 主入口解耦，便于单独测试或复用。
"""

from collections.abc import Mapping

from .account_liveness import probe_account_liveness
from .config import CFG
from .registration_progress import registration_stage
import time


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
