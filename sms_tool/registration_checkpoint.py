"""Post-create checkpoint contract for the protocol registration workflow.

从 ``registration_handlers`` 拆出的数据形职责（2026-09-12 扫描候选1）：检查点
payload 的形状、落盘、以及"什么算可续跑"的判定。工作流编排（何时存、续跑时
重跑哪些 stage）仍归 ``registration_handlers``——本模块只拥有数据契约。

RESUMABLE_STATES 是线上数据：checkpoint payload 里已存在的 registration_state
字符串，逐字保留、不要规范化。
"""

from __future__ import annotations

import time
from http.cookiejar import Cookie
from typing import Any, Callable, Mapping

# 拥有 access_token 且停在这些状态上的 checkpoint 可以跳过邮箱/OTP 阶段续跑。
RESUMABLE_STATES = frozenset({"at_probe_pending", "at_probe_transport_unknown"})
SESSION_PENDING_STATE = "auth_session_pending"
SESSION_RECOVERY_LIMIT = 2
SESSION_RECOVERY_TTL_SECONDS = 900


def build_checkpoint_payload(runtime: Any, mailbox_snapshot: Callable[[], dict]) -> dict[str, Any]:
    """Checkpoint payload 与最终 result 共享的键形状（邮箱快照由调用方注入，
    保持本模块与 mailbox 秘密白名单解耦）。"""
    s = runtime
    cookies = []
    if s.session is not None:
        jar = getattr(getattr(s.session, "cookies", None), "jar", ())
        cookies = [
            {"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
             "path": cookie.path, "expires": cookie.expires, "secure": cookie.secure,
             "domain_specified": cookie.domain_specified, "domain_initial_dot": cookie.domain_initial_dot,
             "path_specified": cookie.path_specified}
            for cookie in jar
        ]
    return {
        "email": s.username,
        "source": "register",
        "register_method": "email" if s.registration_mode != "phone" else "phone",
        "session_type": "at_only" if s.registration_mode == "at_only" else "web",
        "plan_type": "unknown",
        "success": False,
        "status": "at_probe_pending",
        "password": "" if s.password_unknown else s.password,
        "device_id": s.device_id,
        "auth_session_logging_id": s.session_logging_id,
        "access_token": s.access_token,
        "id_token": s.id_token,
        "cookie_header": s.auth_session.get("cookie_header", "") if isinstance(s.auth_session, dict) else "",
        "auth_session": s.auth_body,
        "mailbox": mailbox_snapshot(),
        "registration_mode": s.registration_mode,
        "create_ok": s.create_ok and not s.existing_account,
        "name": s.full_name,
        "birthdate": s.birthdate,
        "session_cookies": cookies,
        "auth_headers": dict(s.base_headers),
        "session_recovery_attempts": s.session_recovery_attempts,
        "session_recovery_started_at": s.session_recovery_started_at,
    }


def persist_checkpoint(
    persistence: Any,
    config: Mapping[str, Any] | None,
    runtime: Any,
    payload: dict[str, Any],
    state: str,
    *,
    upsert_with_access_token: bool = True,
) -> None:
    """Atomically save the checkpoint (+ account row once an AT exists)."""
    username = str(runtime.username or "")
    if not username:
        return
    payload["registration_state"] = state
    persistence.save_checkpoint(username, state, payload, runtime_config=config)
    if upsert_with_access_token and runtime.access_token:
        persistence.upsert_account(payload, runtime_config=config)


def load_resumable_checkpoint(
    persistence: Any,
    mailbox_email: str,
    config: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the payload when a resumable post-create checkpoint exists."""
    if not mailbox_email:
        return None
    try:
        checkpoint = persistence.get_checkpoint(mailbox_email, runtime_config=config)
        payload = checkpoint.get("payload") if isinstance(checkpoint, dict) else {}
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    state = str(payload.get("registration_state") or (checkpoint or {}).get("state") or "")
    if state == SESSION_PENDING_STATE and payload.get("create_ok"):
        return payload
    if not payload.get("access_token"):
        return None
    if state not in RESUMABLE_STATES:
        return None
    return payload


def apply_resume_payload(runtime: Any, payload: Mapping[str, Any]) -> None:
    """Rehydrate the runtime state object from a resumable checkpoint.

    ``username`` 归调用方设置（它来自 mailbox 属性而非 payload——两者可能在
    导入/改名场景下不一致，编排层拥有这个决策）。
    """
    s = runtime
    s.password = str(payload.get("password") or "")
    s.device_id = str(payload.get("device_id") or "")
    s.session_logging_id = str(payload.get("auth_session_logging_id") or "")
    s.access_token = str(payload.get("access_token") or "")
    s.id_token = str(payload.get("id_token") or "")
    s.auth_body = payload.get("auth_session") if isinstance(payload.get("auth_session"), dict) else {}
    s.auth_session = {"cookie_header": str(payload.get("cookie_header") or "")}
    s.registration_mode = str(payload.get("registration_mode") or "passwordless")
    s.create_ok = True
    s.full_name = str(payload.get("name") or "")
    s.birthdate = str(payload.get("birthdate") or "")
    s.session_recovery_attempts = int(payload.get("session_recovery_attempts") or 0)
    s.session_recovery_started_at = int(payload.get("session_recovery_started_at") or 0)


def session_recovery_error(payload: Mapping[str, Any]) -> str:
    """Never fall back to signup after a known successful account creation."""
    try:
        attempts = int(payload.get("session_recovery_attempts") or 0)
        started = int(payload.get("session_recovery_started_at") or 0)
    except (TypeError, ValueError):
        return "auth_session_recovery_context_missing"
    if attempts >= SESSION_RECOVERY_LIMIT:
        return "auth_session_recovery_exhausted"
    if started <= 0 or time.time() - started > SESSION_RECOVERY_TTL_SECONDS:
        return "auth_session_recovery_expired"
    cookies = payload.get("session_cookies")
    if not isinstance(cookies, list) or not cookies or any(
        not isinstance(row, dict) or not row.get("name") or not row.get("domain")
        or not isinstance(row.get("value"), str) for row in cookies
    ):
        return "auth_session_recovery_context_missing"
    return ""


def candidate_checkpoint_error(checkpoint: Mapping[str, Any] | None) -> str:
    """Return a permanent recovery verdict before a candidate claims a slot."""
    value = checkpoint if isinstance(checkpoint, Mapping) else {}
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    state = str(payload.get("registration_state") or value.get("state") or "")
    if (
        state != SESSION_PENDING_STATE
        or not payload.get("create_ok")
        or payload.get("access_token")
    ):
        return ""
    return session_recovery_error(payload)


def restore_session_cookies(session: Any, payload: Mapping[str, Any]) -> None:
    """Keep cookie scope/security attributes, not just the name/value pair."""
    for row in payload["session_cookies"]:
        expires = row.get("expires")
        if expires is not None and int(expires) <= time.time():
            continue
        domain = row["domain"]
        session.cookies.jar.set_cookie(Cookie(
            version=0, name=row["name"], value=row["value"], port=None, port_specified=False,
            domain=domain, domain_specified=bool(row.get("domain_specified", domain.startswith("."))),
            domain_initial_dot=bool(row.get("domain_initial_dot", domain.startswith("."))),
            path=row.get("path") or "/", path_specified=bool(row.get("path_specified", True)), secure=bool(row.get("secure")),
            expires=expires, discard=expires is None, comment=None, comment_url=None, rest={},
        ))
