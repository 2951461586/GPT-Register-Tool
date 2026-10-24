"""Register stages: password/passwordless ``user/register`` handling.

Split out of ``registration_handlers`` on 2026-10-24. The four functions are the
whole ``USER_REGISTER`` stage; they call each other directly inside this module,
while the workflow class keeps thin delegates so ``patch.object`` on the class
still intercepts the stage. ``_str_or_empty`` and the print budget moved with
their only caller.

Runtime imports never point back at ``handlers``; the workflow type is a
TYPE_CHECKING import.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Mapping

from ..operator_output import emit as _emit
from . import reference_lane as _reference_lane
from .retry_guard import DEAD_END_SIGNUP_ROUTED_TO_LOGIN

if TYPE_CHECKING:
    from .state import RegistrationWorkflow

_LOGGER = logging.getLogger(__name__)

#: Print budget for the ``user/register`` response line (see ``post_user_register``).
#: Named so the observability contract is greppable.
_USER_REGISTER_PRINT_BUDGET = 1200

__all__ = [
    "abort_user_register_failure",
    "post_user_register",
    "user_register",
    "user_register_passwordless",
]


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def user_register(workflow) -> None:
    s = workflow.runtime
    if not workflow._password_lane_active():
        workflow._user_register_passwordless()
        return
    # Navigation-first: establish the password page *before* the POST that
    # claims it.  Gated because it adds a request to a lane whose current
    # failure is one step later; see the switch's docstring for the
    # measurement and for what it is meant to separate.
    if workflow.r._prime_create_account_password_page_enabled():
        prime = workflow.r._prime_create_account_password_page(
            s.session,
            s.auth_base,
            s.base_headers,
            str(s.signup_state.get("url") or ""),
        )
        # ``registration.prime_password_page_fatal`` (turb's contract): the
        # prime reports a wrong landing as ``fatal`` so this caller can stop
        # before the POST that claims a page state which was never
        # established.  Off by default -> the report-only behaviour stands.
        if isinstance(prime, Mapping) and prime.get("fatal"):
            workflow._abort(f"password_page_not_reached:{str(prime.get('url') or '')[:120]}")
    if _reference_lane.reference_lane_enabled(workflow.config):
        _reference_lane.prime_password_bundle(workflow)
    workflow._post_user_register()
    if s.reg_response.status_code != 200:
        workflow._abort_user_register_failure()


def user_register_passwordless(workflow) -> None:
    """Passwordless lane: record the signup state instead of POSTing ``user/register``."""
    s = workflow.runtime
    # The passwordless lane must not POST ``user/register``.
    #
    # It used to probe it first (shipped 2026-09-15) so an
    # already-registered address would be recognised before a mailbox
    # poll and a spent email code.  That probe was removed the next day.
    #
    #   * 🔴 The destructiveness is **not** in the POST -- it is in
    #     ignoring the POST's *response*.  The 200 body carries its own
    #     follow instructions:
    #         {"continue_url": ".../api/accounts/email-otp/send",
    #          "method": "GET", "page": {"type": "email_otp_send"}}
    #     The probe never followed it -- ``[4-Trigger email OTP]`` took
    #     the synthetic ``assumed_pre_sent`` branch below -- so every
    #     later ``email-otp/validate`` answered 409 "Your sign-in session
    #     is no longer valid".  Measured on batch 28260: 89 accepted
    #     probes -> 85 such 409s, against 75 of 77 rejected probes
    #     validating fine.
    #     Confirmed 2026-09-16 against a natural control group built on
    #     the *same* ``_post_user_register`` payload: follow the response
    #     -> 0 of 12 validations 409 (and all 12 finalized); ignore it ->
    #     85 of 85.  Fisher one-sided p = 1.4e-15.  See
    #     ``docs/audits/plans/plan-2026-09-16-password-first-registration.md``
    #     §2.0.  The password lane below already follows the response:
    #     ``send_email_otp`` -> ``_email_otp_send_url`` ->
    #     ``_follow_continue_url``, which is a GET and therefore matches
    #     the ``"method": "GET"`` the server asks for.
    #   * It never answered the question it was built for: across 168
    #     probe runs ``user/register`` returned ``user_already_exists``
    #     **zero** times, while ``create_account`` returned it 70 times
    #     in that same batch.  ``user/register`` is not an existence
    #     oracle, so no response-shape logic could have rescued it.
    #
    # Do not reintroduce the probe **as an existence oracle** -- that part
    # is settled.  Raw counts live in the 2026-09-15 work log.
    s.reg_data = {
        "mode": "passwordless_signup",
        "auth_state": {
            "attempt": s.signup_state.get("attempt", ""),
            "url": s.signup_state.get("url", ""),
            "status": s.signup_state.get("status", 0),
        },
    }
    s.password_unknown = True
    _emit(_LOGGER, "  Registration mode: passwordless_signup (HAR login_or_signup)")


def abort_user_register_failure(workflow) -> None:
    """Classify a non-200 ``user/register`` answer and abort with the matching error."""
    s = workflow.runtime
    err_code = s.reg_data.get("error", {}).get("code", "")
    err_msg = s.reg_data.get("error", {}).get("message", str(s.reg_data))
    state_url = str(s.signup_state.get("url") or "")
    if err_code == "invalid_auth_step" and "email-verification" in state_url:
        # 🔴 2026-09-16 拍板：密码泳道**失败即 abort，不回落 passwordless**。
        #
        # ``invalid_auth_step`` 的意思是「事务不在密码步」——服务端
        # **没有接受我们的密码**。再往下走就是 ``create_account``，那会建出
        # 一个**无密码账号**，而「不产生无密码账号」正是密码优先模式存在的
        # 全部理由（aBai 同一处选择 ``raise``：宁可不注册）。
        #
        # 与下面 ``else`` 分支的唯一区别是**时机**：这里在发码之前就停手，
        # 所以不会为一个注定要被拒绝的地址烧掉一个邮箱 OTP。
        # passwordless 泳道**不受影响** —— 它的门禁在
        # ``_password_lane_active()`` 那里就返回了，根本走不到这个分支。
        #
        # 🔴 这里原先还有一条恢复路径（``print("...resuming OTP step...")``
        # + ``s.resume_email_verification = True``），已删除。三条独立理由，
        # 任何一条单独成立就足以删掉它：
        #
        #   1. **它已不可达。** 函数入口的 ``_password_lane_active()`` 门禁
        #      为假时直接 ``return``，而 ``_post_user_register()`` 既不写
        #      ``registration_mode`` 也不写 ``signup_state`` ⇒ 走到这里时该
        #      谓词必然仍为真。旧写法把同一个谓词又判了一遍，那个 ``if``
        #      恒真、``else`` 之后的恢复分支恒不可达。
        #   2. **它本来就是我们要禁的那条路。** 那个标志只改 OTP 的发码
        #      端点，**不跳过** ``run()`` 的第 7 步 ``create_account``
        #      ⇒ 恢复下去照样建出无密码账号。
        #   3. **即使不建号也不值得做。** 对已存在账号，email OTP 能走通但
        #      ``continue_url`` 仍落 ``/about-you``，NextAuth session cookie
        #      **从不下发**（2026-09-14 实测 5/5）⇒ 恢复 = 白烧一个 OTP。
        #
        # 零成本佐证：全量留存日志（5919 个 .log/.jsonl/.txt/.json，含
        # ``runtime/logs/processes/*/``）里 ``resuming OTP step`` **0 次命中**
        # ⇒ 删除不改变任何已观测行为。
        #
        # ✅ 2026-09-16 收尾（老板拍板）：该标志 ``resume_email_verification``
        # **已整体删除** —— 状态字段、两处读点、以及 ``_email_otp_send_url``
        # 里那个回落分支和它唯一的消费者 ``auth_base`` 参数。此前它挂在
        # ``READ_BUT_NEVER_WRITTEN`` 里「待拍板」，现在白名单只剩
        # ``phone_result`` 一条。要复活它，先推翻 ``account_creation``
        # 那句「缺 ``continue_url`` 必须报错，不许猜端点」。
        #
        # ``err_code`` 在这里**必然**等于 ``"invalid_auth_step"``（上面那个
        # ``if`` 的条件），所以不要写成 ``err_code or f"http_{...}"`` ——
        # 那个回退是死代码。仍然插值 ``err_code`` 而不是写死字符串，是为了
        # 将来放宽条件（例如把 ``invalid_state`` 也收进来）时后缀自动跟随。
        workflow._abort(f"password_step_unconfirmed:{err_code}")
    elif err_code == "invalid_auth_step" and workflow.r._is_existing_login_redirect(state_url):
        # 🔴 2026-09-16（P1）：服务端把这次注册**路由到了登录页** ——
        # 这是「地址已存在」的**第三种、也是最早的一种**表达方式。
        #
        # 前两种都出现在 ``create_account``（``user_already_exists`` /
        # ``identity_provider_mismatch``，见 ``DEAD_END_MARKERS``）；这一种
        # 提前到了 ``login_or_signup`` 的**路由落点**：服务端直接把事务送进
        # ``/log-in/password``，随后对 ``user/register`` 回 400
        # ``invalid_auth_step``。
        #
        # 生产实测（2026-09-16 两批）：``login_or_signup`` 落
        # ``/log-in/password`` 的账号**全部**以这个 code 收场 ——
        # 批次 34632 是 21/21，批次 28512 是 10/10，合计 **31/31，0 例外**；
        # 同期落 ``/email-verification`` 的那个成功了。落点与结果完全分离，
        # 且两批共用同一个出口（预检都选中 ``global.9http.com:9091``）
        # ⇒ 这是**地址**属性，不是出口、也不是时间窗。
        #
        # 为什么不能沿用上面那条 ``password_step_unconfirmed``：那条是
        # ``auth_state``（``retryable`` + ``batch_retry``），前提是「什么都没
        # 被消费，下一批换个出口再试」。这里正相反 —— 服务端已明说地址归它
        # 所有，重试只会把整个握手重走一遍再拿到同一个答案。归 ``account``
        # （``batch_dropped``）才对，也不会误伤：本批唯一的
        # ``/email-verification`` 落点碰不到这条分支。
        #
        # 🔴 判据是 ``_is_existing_login_redirect``（``/log-in*``），**不是**
        # ``_is_signup_password_step``（``/create-account/password``）。两者都
        # 会让 ``_password_lane_active`` 为真，但只有前者表达「地址已存在」；
        # 后者落进下面的 ``else``，保持既有行为。
        #
        # ``existing_account`` 置真会让 ``_abort_result`` 把终态装成
        # ``partial_registered`` —— 与 09-15 那 305 个 ``user_already_exists``
        # 同类，只是发现得更早、不烧 OTP。
        s.existing_account = True
        workflow._mark_partial_registration(reason=DEAD_END_SIGNUP_ROUTED_TO_LOGIN)
        workflow._abort(f"existing_account_{DEAD_END_SIGNUP_ROUTED_TO_LOGIN}")
    else:
        # 🔴 2026-09-16（P0）：拼 **code**，不要拼 message。
        #
        # 分类器（``failure_registry`` / ``error_classification``）按 **code**
        # 形式做子串匹配，marker 表里写的是 ``invalid_auth_step``。原先这里拼
        # ``err_msg``，串里只有 ``Invalid authorization step.`` —— 两者**没有
        # 共同子串**，分类掉进兜底类 ``unknown``；而 ``unknown`` 既不在
        # ``BATCH_RETRY_CLASSES`` 也不在 ``BATCH_DROPPED_CLASSES`` ⇒ 不重试、
        # 不记掉号、不告警，**整批静默蒸发**。
        #
        # 生产实测（批次 34632，2026-09-16 11:00）：21 个地址全部
        # ``user_register:Invalid authorization step.`` +
        # ``failure_class=unknown``，成功率 1/22，而日志上只留一行看不出原因
        # 的错误。同一个响应，改拼 ``user_register:invalid_auth_step`` 后分类
        # 是 ``auth_state``（可重试）。
        #
        # ``err_code or err_msg``：code 缺失（服务端只给 message）时仍保留
        # 服务端原文，不丢诊断信息。Pinned by
        # ``tests/registration/test_user_register_response_contract.py``.
        workflow._abort(f"user_register:{err_code or err_msg}")


def post_user_register(workflow) -> Any:
    """POST ``user/register`` (password + username) and stash the parsed body.

    Only the password lane calls this.  The passwordless lane must not: an
    accepted POST advances the server-side auth transaction, and the email
    OTP that follows then validates against a session the server has already
    moved past.  See ``user_register`` for the measured counts.
    """
    r = workflow.r
    s = workflow.runtime
    username_sentinel = workflow._issue_sentinel("username_password_create")
    s.reg_response = r.request_with_retry(
        s.session,
        "post",
        f"{s.auth_base}/api/accounts/user/register",
        label="User register",
        json={"password": s.password, "username": s.username},
        headers=r._auth_request_headers(
            s.base_headers,
            did=s.device_id,
            referer=f"{s.auth_base}/create-account/password",
            origin=s.auth_base,
            sentinel_token=username_sentinel.token,
            sentinel_so_token=_str_or_empty(getattr(username_sentinel, "so_token", "")),
        ),
        impersonate=r.auth_impersonate(),
    )
    try:
        s.reg_data = s.reg_response.json()
    except (ValueError, TypeError):
        s.reg_data = {"_raw": s.reg_response.text[:_USER_REGISTER_PRINT_BUDGET]}
    _emit(_LOGGER, "  Status: %s", s.reg_response.status_code)
    # 1200, not 300: the 200 body's ``oai-client-auth-session`` carries
    # ``email_verification_mode`` in cleartext, and the server switches it
    # between the signup-state value (passwordless_signup) and a third,
    # still-unnamed 10-char enum right at the send step (measured 2026-10-07,
    # batch p1_5_hint_arm_5addr: the enhanced dump shows
    # "[REDACTED](len=10)" at after_otp_send). The 300 budget cut the wire
    # body at exactly the country_code_hint key, before the mode key -- so
    # the one value that would name the transaction arm never reached the
    # log. The sanitizer still redacts secrets inside this line; only the
    # print budget changes.
    _emit(
        _LOGGER,
        "  Response: %s",
        r._sanitize_text(json.dumps(s.reg_data, ensure_ascii=False)[:_USER_REGISTER_PRINT_BUDGET]),
    )
    return s.reg_response
