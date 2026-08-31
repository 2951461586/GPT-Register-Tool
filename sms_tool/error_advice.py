"""Map internal error codes to user-actionable advice.

Many error paths raise a bare ``RuntimeError("code")`` or a custom exception whose
``__str__`` is only the raw code, so the UI shows an unactionable string. This table
turns a code into a concrete next step. Codes not yet documented fall back to a
generic message — extending this table (not changing call sites) is the fix for
"error X is unactionable".
"""
from __future__ import annotations

from typing import Optional

_ERROR_ADVICE: dict[str, str] = {
    "browser_proxy_blocked": (
        "代理出口被目标识别为机房/数据中心 IP。换一条出口国不同的代理，或改用住宅/移动代理，"
        "不要反复重试同一代理。"
    ),
    "manual_challenge_required": (
        "注册被风控要求人工完成 CAPTCHA/验证。需要人工介入处理该账号，自动化无法继续；"
        "换账号或走人工通道。"
    ),
    "browser_email_field_missing": (
        "页面没有找到邮箱输入框，可能页面结构变化或登录态异常，检查登录状态后重试。"
    ),
    "browser_unexpected_identity_provider": (
        "注册流程跳转到了意外的身份提供方，停止该账号的自动化并人工排查。"
    ),
    "account_health_queue_full": (
        "账号健康队列已满。降低并发或稍后重试；不要继续堆任务，否则会持续失败。"
    ),
    "oauth_state_mismatch": (
        "OAuth state 不匹配，可能是并发撞车、时钟漂移或回调被改写。重新发起 OAuth 流程，"
        "避免同一账号并行登录。"
    ),
    "account_trial_ineligible": "该账号不具备试用资格（非 eligible），换可用额度的账号。",
    "card_only_full_price": "该账号只能全价购买，无 0 元试用资格；确认业务是否接受全价。",
    "approve_result_blocked": (
        "Approve 被风控拦截，重建 checkout 或换出口国/代理后重试。"
    ),
}


def advice_for(error_code: str) -> Optional[str]:
    """Return the advice string for ``error_code`` or ``None`` if undocumented."""
    return _ERROR_ADVICE.get(error_code)


def format_advice(error_code: str, detail: Optional[str] = None) -> str:
    """Render a human-readable, actionable message for ``error_code``."""
    advice = _ERROR_ADVICE.get(error_code)
    if advice is None:
        base = f"未知错误码 {error_code!r}（尚未登记可操作建议）"
        return f"{base}：{detail}" if detail else base
    return f"{error_code}: {advice}" + (f" | 细节: {detail}" if detail else "")
