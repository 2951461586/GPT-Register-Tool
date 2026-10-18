"""Email-OTP stages of the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-01, following the
``registration_finalize`` precedent): the send / wait / validate trio plus the
two diagnostics that decide *why* a code never arrived form one cohesive unit.
They are module-level functions that take the workflow instance as their first
parameter, named ``workflow`` and annotated with ``RegistrationEmailWorkflow``
under ``TYPE_CHECKING``, so the class keeps thin delegates and no caller (or
test) changes.

The bodies are moved verbatim. They reach the workflow only through the public
seam (``workflow.r`` / ``workflow.runtime`` / ``workflow.config`` and the
``_password_lane_active`` / ``_otp_poll_timeout`` / ``_otp_timeout_error`` /
``_abort`` collaborators), never by importing ``registration_handlers``, so
this module cannot form an import cycle.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Mapping as MappingABC
from typing import TYPE_CHECKING, Any

from .auth_state import otp_dispatch_verdict, passwordless_arm_armed
from .operator_output import emit as _emit
from .kernel.sanitizer import describe_exception
from .kernel.failure_registry import (
    OTP_ARM_MISMATCH_MARKER,
    OTP_MAILBOX_SIDE_MARKER,
    OTP_NO_RESEND_MARKER,
)
from .registration_protocol_helpers import _safe_int, registration_flag
from .otp_strategy import otp_resend_eligible

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .registration_handlers import RegistrationEmailWorkflow

_LOGGER = logging.getLogger(__name__)


def _otp_navigation_headers_enabled(workflow: RegistrationEmailWorkflow) -> bool:
    """``registration.otp_navigation_headers`` (default **False**).

    turb sends ``sec-fetch-site: same-origin`` + ``sec-fetch-user: ?1`` on the
    ``email-otp/send`` navigation; this repo sends only ``Accept`` + ``Referer``
    (``http_utils._follow_continue_url``).  Those two headers are how a real
    top-level navigation declares itself, and the repo already learned that
    lesson for the password page (2026-10-07 scan P1-A), so the OTP send gets
    the same treatment as its own single-variable switch.
    """
    return registration_flag(workflow.config, "otp_navigation_headers", False)


def _otp_validate_sentinel_enabled(workflow: RegistrationEmailWorkflow) -> bool:
    """``registration.otp_validate_sentinel`` (default **False**).

    turb mints a fresh ``authorize_continue`` Sentinel and attaches it (plus its
    SO) to ``email-otp/validate`` -- its config comments the measurement
    ("2026-09-14 Roxy 成功样本中 email-otp/validate 同时携带 Sentinel 与 SO").
    This repo passes ``use_sentinel=False`` there.  Single variable: it only
    decides whether the validate request carries the pair.
    """
    return registration_flag(workflow.config, "otp_validate_sentinel", False)


def _otp_external_url_branch_enabled(workflow: RegistrationEmailWorkflow) -> bool:
    """``registration.otp_external_url_branch`` (default **False**).

    After a successful ``email-otp/validate`` the server's own answer decides
    whether ``create_account`` should run at all.  turb reads ``page.type`` and
    the ``continue_url`` and, when the transaction already finished on an
    external/callback URL, skips ``create_account`` with the stated reason that
    POSTing it anyway "会触发 invalid_auth_step".  This repo has no such branch
    (the stage list always runs ``create_account``), so this toggle adds it.
    """
    return registration_flag(workflow.config, "otp_external_url_branch", False)


def _passwordless_explicit_send_otp_enabled(workflow: RegistrationEmailWorkflow) -> bool:
    """``registration.passwordless_explicit_send_otp`` (default **True** since 2026-10-11).

    Live, same exit (IN), fresh addresses: 5/5 explicit registrations versus 1/5
    legacy, the legacy four all stuck on ``pending=true`` (n is far below the
    pre-registered 30 per arm; see the audit scan section 7).  ``false`` restores
    the legacy ``assumed_pre_sent`` path and is the A/B baseline arm.

    The passwordless lane assumed ``authorize`` with ``login_hint`` already sent
    the first code and only polls.  The 2026-10-11 browser HAR shows the page
    itself POSTing ``/api/accounts/passwordless/send-otp`` ten seconds after
    ``/email-verification`` loads -- empty body, a fresh ``authorize_continue``
    Sentinel, ``x-access-flow-invocation-id`` and ``x-openai-document-navigation-id``
    -- and nothing else on ``/api/accounts`` in between.  Single variable: only
    whether that POST is made.
    """
    return registration_flag(workflow.config, "passwordless_explicit_send_otp", True)


def _explicit_passwordless_send(workflow: RegistrationEmailWorkflow) -> Any:
    """POST ``passwordless/send-otp`` the way the browser's verification page does."""
    r = workflow.r
    s = workflow.runtime
    # Mechanism line for ``p3-1-passwordless-explicit-send-otp``: unconditional
    # inside the toggle's branch so the A/B gate can see the arm.
    _emit(_LOGGER, "  Passwordless explicit send-otp")
    try:
        issued = workflow._issue_sentinel("authorize_continue", force_fresh=True)
    except Exception as exc:
        # Falling back to the assumed pre-send would silently turn this arm into
        # the default one, so the arm fails instead.
        workflow._abort(f"email_otp_send_failed:sentinel_unavailable:{describe_exception(exc)}")
    referer = f"{s.auth_base}/email-verification"
    headers = {
        **s.base_headers,
        **r.openai_auth_headers(
            s.device_id,
            referer=referer,
            origin=s.auth_base,
            accept="*/*",
            sentinel_token=issued.token,
            session_id=s.session_logging_id,
            flow_invocation_id=s.flow_invocation_id,
            extra={"x-openai-document-navigation-id": str(uuid.uuid4())},
        ),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    # libcurl labels a body-less POST ``application/x-www-form-urlencoded`` and the
    # server answers 400 ``unsupported_content_type``; the browser sends none.
    # ``None`` is what makes curl_cffi drop the header.
    for key in [k for k in headers if str(k).lower() == "content-type"]:
        del headers[key]
    headers["Content-Type"] = None
    response = r.request_with_retry(
        s.session,
        "post",
        f"{s.auth_base}/api/accounts/passwordless/send-otp",
        label="Passwordless send-otp",
        headers=headers,
        impersonate=r.auth_impersonate(),
        attempts=1,
    )
    _raw_body: object = r._json_or_raw(response)
    body = _raw_body if isinstance(_raw_body, MappingABC) else {}
    _raw_session_state: object = body.get("oai-client-auth-session")
    session_state = _raw_session_state if isinstance(_raw_session_state, MappingABC) else {}
    pending = session_state.get("passwordless_email_otp_send_pending")
    mode = str(session_state.get("email_verification_mode") or "")
    _emit(
        _LOGGER,
        "  Passwordless send-otp: status=%s pending=%s mode=%s",
        getattr(response, "status_code", 0),
        str(pending).lower() if isinstance(pending, bool) else "unknown",
        mode if mode in _SEND_MODE_ENUM else ("unknown" if not mode else "other"),
    )
    if getattr(response, "status_code", 0) != 200:
        error = body.get("error")
        code = str(error.get("code") if isinstance(error, MappingABC) else error or "")
        _emit(
            _LOGGER,
            "  Passwordless send-otp rejected: error_code=%s",
            code if _SEND_ERROR_CODE_RE.fullmatch(code) else ("none" if not code else "other"),
        )
    return response


_SEND_MODE_ENUM = frozenset({"passwordless_signup", "passwordless_login", "login_challenge"})
_SEND_ERROR_CODE_RE = re.compile(r"[a-z0-9_.\-]{1,64}")


def send_email_otp(workflow: RegistrationEmailWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    # Do NOT snapshot here.  The pre-OTP baseline is taken once in
    # ``_bootstrap``; re-snapshotting at this point would be a race --
    # passwordless sends the OTP during ``auth_flow``, so the code mail can
    # already be visible and would be recorded as "seen", permanently
    # hiding it from ``_latest_email_otp_candidate``.
    continue_url = r._email_otp_send_url(s.reg_data)
    otp_send_started = _safe_int(time.time())
    if not workflow._password_lane_active() and _passwordless_explicit_send_otp_enabled(workflow):
        response = _explicit_passwordless_send(workflow)
    elif not workflow._password_lane_active():
        # authorize with login_hint sends the first OTP itself. Do not
        # immediately POST resend: that endpoint is rate-limited for this
        # flow and the reference browser path only polls the pre-sent code.
        response = r.SyntheticResponse(
            204,
            {"assumed_pre_sent": True},
            url=s.signup_state.get("url", ""),
        )
    else:
        otp_send_headers = s.base_headers
        if _otp_navigation_headers_enabled(workflow):
            # Mechanism line for ``p1-15-otp-navigation-headers``: unconditional
            # inside the toggle's branch (the send only runs on the password
            # lane), so the A/B mechanism gate can see the arm.
            _emit(_LOGGER, "  OTP navigation headers")
            otp_send_headers = {**s.base_headers, "sec-fetch-site": "same-origin", "sec-fetch-user": "?1"}
        response = r._follow_continue_url(
            s.session,
            continue_url,
            otp_send_headers,
            referer=f"{s.auth_base}/create-account/password",
            label="Email OTP send",
        )
    # 保留 summary，不要只打印后丢弃：``wait_email_otp`` 要用它分辨超时
    # 根因（服务端没派发 vs 邮箱没收到），见 ``_otp_timeout_error``。
    dump = r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_otp_send")
    s.otp_send_dump = dump if isinstance(dump, dict) else {}
    if response is None:
        workflow._abort("email_otp_send_missing_continue_url")
    status_code = getattr(response, "status_code", 0)
    if status_code not in (200, 202, 204):
        workflow._abort(f"email_otp_send_failed:{status_code}")
    s.otp_issued_after = otp_send_started
    if s.registration_mode == "passwordless" and r._json_or_raw(response).get("assumed_pre_sent"):
        s.otp_issued_after = max(0, s.auth_flow_started - 5)


def _reference_lane_polls_anyway(workflow: RegistrationEmailWorkflow) -> bool:
    probe = getattr(workflow, "_reference_lane_active", None)
    return bool(callable(probe) and probe() is True)


def wait_email_otp(workflow: RegistrationEmailWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    config = workflow.config if isinstance(workflow.config, MappingABC) else {}
    email_cfg = config.get("email_registration", {})
    # RuntimeConfig freezes nested sections as MappingProxyType. Keep the
    # operator's OTP timeout/resend settings instead of silently replacing the
    # whole section with defaults.
    s.email_cfg = dict(email_cfg) if isinstance(email_cfg, MappingABC) else {}
    # P0-1 判据 B：事务里还挂着 ``passwordless_email_otp_send_pending``
    # ⇒ 服务端**没有完成**派发 ⇒ 这一轮**不可能**拿到码。实测 6/6 超时的
    # run 都停在这个形状上（15 个拿到码的一个都没有），所以直接止损，
    # 不去烧满 300s 轮询。
    #
    # 单独一个错误名而不是给 ``email_otp_poll_timeout`` 加后缀：这条路径
    # **根本没轮询**，说「poll timeout」会让日志撒谎。
    if otp_dispatch_verdict(s.otp_send_dump) == "stuck" and _reference_lane_polls_anyway(workflow):
        # 2026-10-12 lane5：三个被本判据止损的地址，邮箱里**都**收到了验证码邮件
        # （04:41–04:42），而此时 ``user/register`` 已 200、地址已被消耗。参考项目
        # 从不读这个标记，发完 OTP 直接轮询邮箱。lane 下 pending 因此不是「码不会来」
        # 的证据，照常轮询；标记仍留在 dump 里供超时归因。
        _emit(_LOGGER, "  Email OTP send still shows pending; reference lane polls the mailbox anyway")
    elif otp_dispatch_verdict(s.otp_send_dump) == "stuck":
        # 事务臂判据（F3，2026-10-08）：本 run 走密码泳道、而 dump 显示服务端把这次
        # 注册架在了 passwordless 验证臂上。这是**事务臂**证据，不是出口证据 ——
        # 换出口改不了服务端选臂，所以错误串带 ``arm_mismatch`` 后缀，
        # ``registration_pulse._is_otp_ban_signal`` 据此不把它算作派发侧封禁。
        arm_mismatch = workflow._password_lane_active() and passwordless_arm_armed(s.otp_send_dump)
        reason = (
            "the server armed a passwordless arm on the password lane" if arm_mismatch else "skipping the mailbox poll"
        )
        _emit(_LOGGER, "  Email OTP send is still pending on the server; %s", reason)
        workflow._abort(f"email_otp_send_stuck:{OTP_ARM_MISMATCH_MARKER}" if arm_mismatch else "email_otp_send_stuck")
    s.email_code = r.otp_poll.poll(
        s.mailbox,
        subject_keyword=r.otp_poll.subject_keywords,
        timeout=workflow._otp_poll_timeout(),
        issued_after_unix=s.otp_issued_after,
        proxy=s.proxy,
        resend_callback=lambda: r.otp_poll.resend(
            s.session,
            s.auth_base,
            s.base_headers,
            current_url=s.signup_state.get("url", ""),
            mode="passwordless" if s.registration_mode == "passwordless" else "send",
        ),
        resend_after_seconds=s.email_cfg.get("remail_otp_resend_after_seconds", 30),
        poll_otp_fn=s.mailbox_service.poll_otp,
    )
    if not s.email_code:
        workflow._abort(workflow._otp_timeout_error())


def _otp_provider(workflow: RegistrationEmailWorkflow) -> str:
    """当前邮箱渠道名；拿不到就是空串（空串不在重发名单里 ⇒ 判「无重发」）。"""
    mailbox = getattr(workflow.runtime, "mailbox", None)
    return str(getattr(mailbox, "provider", "") or "").strip().lower()


def _otp_timeout_error(workflow: RegistrationEmailWorkflow) -> str:
    """``email_otp_poll_timeout`` 加后缀，说明**码为什么没到**。

    脉冲调度（``registration_pulse``）把「一轮里 OTP 失败聚集」当成出口被
    封禁的证据，命中就暂停换池。旧判据只做子串匹配（``OTP_BAN_MARKERS``
    里的 ``otp_poll_timeout`` 命中 ``email_otp_poll_timeout``），于是把两种
    完全不同的根因混为一谈。

    走到这里说明**已经轮询过**（判据 B 在 ``wait_email_otp`` 里拦掉了
    「服务端没派发」那种），所以只剩两种可能：

    * ``mailbox_side_no_code`` —— dump 可读且没有挂起键（服务端侧发码事务
      走完了），但整个轮询窗口内**邮箱侧没有产出可用验证码**（**邮箱侧**，
      与出口无关，不该触发封禁暂停）；
    * 裸名 —— dump 不可用，**没有证据**，回落到旧行为（算派发侧）：
      没有证据时宁可多停 60s，也不要让一整轮撞在被封的出口上。

    🔴 **2026-09-16 改名（原 ``code_not_delivered``）**：那个名字在撒谎。
    判据只是 ``otp_dispatch_verdict() == "dispatched"``，而那是**服务端**的
    判定，推不出「邮件投递到了邮箱」。实测反例：批次 25116 的 10 个账号
    全走 ``ima3.52dfd.top``（返回 72 字节 JSON，HTML 解析器结构性读不到），
    10/10 仍被标成 ``code_not_delivered`` ⇒ 真实语义是「**我们没读出来**」。
    新名字只陈述已知事实，且**不能**反读成「邮件一定到了」——「没收到」与
    「读不出」用当前数据区分不了（轮询器只回码，不回观测元数据）。

    ⚠ ``no_resend_for_channel`` 是**渠道能力**后缀，不是第三个根因：该渠道
    不在 ``otp_strategy.otp_resend_eligible()`` 名单里 ⇒ 整段 ``otp_timeout``
    只发过一次邮件。它只与 ``mailbox_side_no_code`` **组合**出现，语义是
    「邮箱侧没有码，而且连重发这个补救手段都没有」。

    ⚠ 后缀只描述**这一个账号**为什么没拿到码；够不够格叫「IP 封禁」由
    ``registration_pulse._detect_ip_ban`` 的整轮一致性决定（每个账号钉在池里
    各自的出口上，一轮里有账号拿到码就证明出口是通的）。
    """
    if otp_dispatch_verdict(workflow.runtime.otp_send_dump) == "dispatched":
        suffix = OTP_MAILBOX_SIDE_MARKER
        if not otp_resend_eligible(workflow._otp_provider()):
            suffix = f"{suffix}:{OTP_NO_RESEND_MARKER}"
        return f"email_otp_poll_timeout:{suffix}"
    return "email_otp_poll_timeout"


def _external_url_after_otp(otp_data: Any) -> str:
    """The callback/external URL the validated transaction wants followed, or "".

    turb's rule (``main.py::run_registration``): the server's own answer decides
    whether ``create_account`` runs at all.  ``page.type == "external_url"``, or
    a ``continue_url`` that is a ChatGPT OAuth callback / an authorize continue
    and is **not** the profile step, means the transaction already finished --
    POSTing ``create_account`` next is what answers ``invalid_auth_step``.
    Pure and total: a malformed payload answers ``""`` (the default path).
    """
    if not isinstance(otp_data, MappingABC):
        return ""
    _raw_page: object = otp_data.get("page")
    page = _raw_page if isinstance(_raw_page, MappingABC) else {}
    page_type = str(page.get("type") or otp_data.get("page_type") or "").strip().lower()
    target = str(
        otp_data.get("continue_url")
        or otp_data.get("external_url")
        or page.get("continue_url")
        or page.get("external_url")
        or ""
    ).strip()
    if page_type == "external_url":
        return target
    if not target or "about-you" in target:
        return ""
    if "chatgpt.com/api/auth/callback" in target or "auth.openai.com/authorize/continue" in target:
        return target
    return ""


def validate_email_otp(workflow: RegistrationEmailWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    use_sentinel = False
    sentinel_data = s.sentinel_data
    if _otp_validate_sentinel_enabled(workflow):
        # Mechanism line for ``p1-16-otp-validate-sentinel``: unconditional
        # inside the toggle's branch.
        _emit(_LOGGER, "  OTP validate sentinel")
        try:
            issued = workflow._issue_sentinel("authorize_continue", force_fresh=True)
            sentinel_data = {"sentinel_token": issued.token, "sentinel_so_token": issued.so_token}
            use_sentinel = True
        except Exception as exc:
            # Non-fatal by contract: the toggle adds a header to a request that
            # already works without it, so a mint failure must not fail the run.
            _emit(_LOGGER, "  OTP validate sentinel unavailable: %s", describe_exception(exc))
    otp_ok, s.otp_data = r._validate_email_otp(
        s.session,
        s.auth_base,
        s.base_headers,
        s.email_code,
        sentinel_data=sentinel_data,
        use_sentinel=use_sentinel,
    )
    if not otp_ok and r._is_wrong_email_otp_code(s.otp_data):
        _emit(_LOGGER, "  Email OTP was rejected; retrying latest mailbox code once...")
        retry_code = s.mailbox_service.poll_otp(
            s.mailbox,
            subject_keyword=r.otp_poll.subject_keywords,
            timeout=min(60, _safe_int(s.email_cfg.get("otp_timeout", 300), 300)),
            issued_after_unix=max(0, s.auth_flow_started - 5),
            proxy=s.proxy,
            excluded_otps={s.email_code},
        )
        if retry_code and retry_code != s.email_code:
            s.email_code = retry_code
            otp_ok, s.otp_data = r._validate_email_otp(
                s.session,
                s.auth_base,
                s.base_headers,
                s.email_code,
                sentinel_data=sentinel_data,
                use_sentinel=use_sentinel,
            )
    if not otp_ok:
        r._fetch_client_auth_session_dump(
            s.session,
            s.auth_base,
            s.base_headers,
            "after_otp_validate_failed",
        )
        workflow._abort(f"email_otp_validate:{json.dumps(s.otp_data, ensure_ascii=False)[:300]}")
    if _otp_external_url_branch_enabled(workflow):
        # Mechanism line for ``p1-17-otp-external-url-branch``: unconditional
        # inside the toggle's branch (the branch itself only fires on the shape).
        _emit(_LOGGER, "  OTP external_url branch")
        external_url = _external_url_after_otp(s.otp_data)
        if external_url:
            s.otp_external_url = external_url
            _emit(
                _LOGGER,
                "  OTP external_url branch: the transaction already finished; skipping create_account",
            )
    try:
        r._follow_continue_url(
            s.session,
            s.otp_data.get("continue_url", ""),
            s.base_headers,
            referer=f"{s.auth_base}/verify-email",
            label="Email OTP continue",
        )
    except Exception as exc:
        _emit(_LOGGER, "  Email OTP continue transport warning: %s", describe_exception(exc))


__all__ = [
    "send_email_otp",
    "wait_email_otp",
    "validate_email_otp",
    "_otp_provider",
    "_otp_timeout_error",
]
