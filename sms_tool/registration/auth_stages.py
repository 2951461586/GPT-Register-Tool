"""Auth stages: signin/authorize handshake and password-lane predicates.

Split out of ``registration_handlers`` on 2026-10-24. ``auth_flow`` is the wire
stage; the two predicates are here because that stage and the register stages
share them, and one owner keeps the call sites from drifting. The workflow class
keeps thin delegates, so ``patch.object(RegistrationEmailWorkflow, "auth_flow",
...)`` still intercepts the stage call.

Runtime imports never point back at ``handlers``; the workflow type is a
TYPE_CHECKING import.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from ..auth_state import signup_lane_verdict
from ..kernel.failure_registry import PASSWORD_CLAIMED_MARKER
from ..operator_output import emit as _emit
from . import concurrency as _registration_concurrency
from . import reference_lane as _reference_lane
from .protocol_helpers import _safe_float, _safe_int
from .state import RegistrationState

if TYPE_CHECKING:
    from .state import RegistrationWorkflow

_LOGGER = logging.getLogger(__name__)

__all__ = ["auth_flow", "password_lane_active", "with_password_claim"]


def with_password_claim(workflow, error: str) -> str:
    """Tag a failure that happened after ``user/register`` accepted the password.

    The stage after ``USER_REGISTER`` is only entered once that POST returned 200,
    and only the password lane POSTs it. From then on the address answers
    ``invalid_auth_step`` to every new session, so the batch must not retry it
    (``registration_policy``). Runs that already hold a token or an existing
    account are left to their own recovery path, and ``email_otp_send_stuck`` keeps
    its own guard/pulse semantics (quarantine TTL, arm-mismatch accounting).
    """
    s = workflow.runtime
    if PASSWORD_CLAIMED_MARKER in error or "email_otp_send_stuck" in error:
        return error
    if s.access_token or s.existing_account:
        return error
    history = getattr(workflow.machine, "history", None)
    if not isinstance(history, (list, tuple)):
        return error
    if not any(t.state is RegistrationState.EMAIL_OTP_SEND for t in history):
        return error
    if not workflow._password_lane_active():
        return error
    return f"{error}|{PASSWORD_CLAIMED_MARKER}"


def password_lane_active(workflow) -> bool:
    """True when this run must go through the password step.

    Two independent ways in, and they are **not** the same condition:

    * ``registration_mode != "passwordless"`` -- the operator opted into
      password-first registration (``email_registration.registration_mode``).
    * ``password_fallback`` -- the **server** moved the transaction to
      ``/log-in/password`` (``auth_flow._is_existing_login_redirect``), or the
      current step URL already is the password step.

    ``user_register`` and ``send_email_otp`` both branch on this.  It used to
    be written out twice as
    ``bool(signup_state.get("password_fallback")) or _is_signup_password_step(...)``;
    a single owner keeps the two call sites from drifting, and it removes the
    duplicated expression that made text-anchored mutation specs ambiguous.
    """
    s = workflow.runtime
    if s.registration_mode != "passwordless":
        return True
    return bool(s.signup_state.get("password_fallback")) or workflow.r._is_signup_password_step(
        s.signup_state.get("url", "")
    )


def auth_flow(workflow) -> None:
    r = workflow.r
    s = workflow.runtime
    s.auth_flow_started = _safe_int(time.time())
    if s.registration_mode == "passwordless":
        r.request_with_retry(
            s.session,
            "get",
            f"{s.chat_base}/",
            label="ChatGPT prime",
            headers={
                **r.chatgpt_headers(
                    s.device_id,
                    session_id=s.session_logging_id,
                    flow_invocation_id=s.flow_invocation_id,
                    accept="text/html,application/xhtml+xml",
                    referer=f"{s.chat_base}/",
                )
            },
            impersonate=r.auth_impersonate(),
            attempts=1,
        )
    elif workflow._reference_lane_active():
        _reference_lane.pre_signin(workflow)
    else:
        r.request_with_retry(
            s.session,
            "get",
            f"{s.auth_base}/create-account",
            label="Auth prime",
            headers={**s.base_headers, "Accept": "text/html,application/xhtml+xml"},
            impersonate=r.auth_impersonate(),
        )
    csrf_resp = r.request_with_retry(
        s.session,
        "get",
        f"{s.chat_base}/api/auth/csrf",
        label="Auth csrf",
        headers=r.nextauth_headers(
            s.device_id,
            session_id=s.session_logging_id,
            referer=_reference_lane.LOGIN_PAGE_REFERER if workflow._reference_lane_active() else f"{s.chat_base}/",
            origin=s.chat_base,
        ),
        impersonate=r.auth_impersonate(),
    )
    s.csrf_token = (r._json_or_raw(csrf_resp).get("csrfToken") or "").strip()
    s.signup_state = r._prepare_signup_auth_state(
        s.session,
        s.username,
        s.device_id,
        s.session_logging_id,
        s.auth_base,
        s.chat_base,
        s.base_headers,
        s.csrf_token,
        sentinel_token=s.sentinel_token,
        authorize_sentinel_token=s.sentinel_authorize_token,
        sentinel_so_token=s.sentinel_so_token,
        proxy=s.proxy,
        passwordless_web=s.registration_mode == "passwordless",
        attempts=r._passwordless_signin_attempts()
        if s.registration_mode == "passwordless"
        else r._signup_signin_attempts(),
        **({"reference_navigation": True} if workflow._reference_lane_active() else {}),
    )
    # P0-1 判据 A 的**取证埋点**（先扩样本，暂不硬止损）。
    #
    # ``passwordless_login_magic_link_sent`` 出现在**取码之前**，意味着服务端
    # 把这次 authorize 当成**登录**处理 ⇒ 该地址已存在，注册必然以
    # ``user_already_exists`` 收场。实测（批次 25288）精确率 3/3，但召回率只有
    # 3/11 —— n 太小，还不能拿它去写死路账本（误判的代价是把一个可注册地址
    # 永久拉黑）。所以这里只保留 dump + 打一行**每 run 可归因**的判定，让后续
    # 批次把精确率/召回率补到能拍板为止。
    #
    # 不要用日志里的 ``client_auth_session_dump`` 行做这件事：那些行按 stage
    # 做进程级降噪，跨 run 共享（见 ``auth_state._LAST_DUMP_TEXT``）。
    signup_dump = r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_signup_state")
    s.signup_dump = signup_dump if isinstance(signup_dump, dict) else {}
    s.signup_lane = signup_lane_verdict(s.signup_dump)
    if s.signup_lane == "login":
        _emit(_LOGGER, "  Signup lane hint: login (transaction says this address already has a login magic link)")
    if _safe_int(s.signup_state.get("status") or 0) == 429:
        retry_after = _safe_float(s.signup_state.get("retry_after_seconds") or 300, 300.0)
        _registration_concurrency.mark_registration_rate_limited(retry_after)
        workflow._abort(f"registration_rate_limited:retry_after={retry_after:.0f}s")
    if not s.signup_state.get("ok"):
        workflow._abort(f"signup_auth_state:{json.dumps(s.signup_state, ensure_ascii=False)[:300]}")
    if r._is_chatgpt_auth_login_landing(s.signup_state.get("url", "")):
        workflow._abort("signup_auth_state:redirected_to_chatgpt_login")
    workflow._persist_checkpoint("auth_flow")
