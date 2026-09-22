r"""S0 契约锁：``user/register`` 的 200 **必须被跟**，且跟的是响应体里的 URL。

为什么这条契约值得单独钉住
--------------------------

2026-09-16 判定（H1 成立）。生产日志里存在一个**天然对照组** —— 两组用同一个
``_post_user_register()``、同一份 payload，唯一差别是「跟不跟 200 的响应」：

    | 组 | 路径                       | run | validate 200 | validate 409  |
    |----|----------------------------|-----|--------------|---------------|
    | A  | 未 POST（passwordless）     | 360 | 355          | 1             |
    | B  | POST 被接受 + **没跟**响应   |  85 | 0            | **85（100%）** |
    | C  | POST 被接受 + **跟了**响应   |  12 | **12（100%）** | 0             |

    Fisher 单侧 p = 1/C(97,12) = 1.403e-15。

机制：200 响应体**自带跟法** ——

    {"continue_url": ".../api/accounts/email-otp/send",
     "method": "GET", "page": {"type": "email_otp_send"}}

服务端明说 ``"method": "GET"``，而 ``_follow_continue_url`` 本来就是 GET
⇒ 当年的缺陷**不是跟错了方法，是根本没跟**（``[4-Trigger email OTP]`` 走了合成的
``assumed_pre_sent`` 分支）。B 组那 85 个 409 就是这么来的。

本文件钉住四件事，任何一件被改回都会静默重新制造 409：

1. 密码泳道：200 之后**必须**用 ``_email_otp_send_url`` 从**响应体**取 URL 并跟。
2. passwordless 泳道：**不得** POST ``user/register``，**不得**跟任何 continue URL
   （只能走合成 ``assumed_pre_sent``）—— 这是「别把探针加回来」的守卫。
3. ``password_fallback``（服务端把 passwordless 路由到 ``/log-in/password``）必须
   把地址推回密码泳道，而不是让它继续走合成分支。
4. 跟的那次请求的**日志标签**必须保持 ``Email OTP send`` —— H1 的判决就是用
   ``grep "Email OTP send: (\d{3})"`` 从生产日志里数出来的；改标签等于毁掉证据通道。

2026-09-16 追加两条（同一次拍板：**密码步失败即 abort，不回落 passwordless**）：

5. ``user/register`` 回 ``invalid_auth_step`` + 事务停在 ``email-verification``
   ⇒ 必须 abort 成 ``password_step_unconfirmed:<code>``，且**在发码之前**停手。
   往下走就是 ``create_account``，那会建出一个无密码账号。
6. 该错误名归 ``auth_state``（``retryable`` + ``batch_retry``，**不** ``batch_dropped``）：
   地址没被消费掉，不该被拉黑。

跟的动词必须是 GET 这一半，在 ``tests/test_http_utils_pure.py`` 里另有独立用例。

🔴 本文件刻意**不**测「探针答不答 ``user_already_exists``」—— 那部分已结案
（168 次探针答 **0** 次，同批 ``create_account`` 答 70 次），见
``sms_tool/registration_handlers.user_register`` 的注释与 ``runtime-and-registration.md`` ⑬。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sms_tool import auth_flow, http_utils
from sms_tool.accounts.account_creation import _email_otp_send_url
from sms_tool.error_classification import classify_error
from sms_tool.failure_registry import BATCH_DROPPED_CLASSES, BATCH_RETRY_CLASSES
from sms_tool.registration_handlers import RegistrationAbort, RegistrationEmailWorkflow
from sms_tool.registration_runtime import RegistrationRuntimeState

#: 服务端 200 体里 ``continue_url`` 实际给的落点。
_CONTINUE_URL = "https://auth.openai.com/api/accounts/email-otp/send"

#: 第二个落点，用来杀「把 URL 写死」的变异体。
_OTHER_URL = "https://auth.openai.com/api/accounts/email-otp/resend"

#: ``auth_flow_started`` 的取值；合成分支会把 ``otp_issued_after`` 回填成它减 5。
_AUTH_FLOW_STARTED = 1000

#: 触发 ``invalid_auth_step`` 止损分支的事务落点（``user_register`` 用子串判定）。
_EMAIL_VERIFICATION_URL = "https://auth.openai.com/email-verification"

#: ``_password_lane_active()`` 的真值表。``password_step`` 列传的是**真的**
#: ``auth_flow._is_signup_password_step``，所以「URL 自己就是密码步」这一条
#: 走的是生产判据，不是 Mock。
_PASSWORD_LANE_CASES = [
    # registration_mode, signup url, password_fallback, expected
    ("password", "https://auth.openai.com/log-in", False, True),
    ("password", "https://auth.openai.com/create-account/password", False, True),
    ("passwordless", "https://auth.openai.com/log-in", True, True),
    ("passwordless", "https://auth.openai.com/create-account/password", False, True),
    ("passwordless", "https://auth.openai.com/log-in", False, False),
    ("passwordless", _EMAIL_VERIFICATION_URL, False, False),
]


def _response(body=None, status=200, url="https://auth.openai.com/landed"):
    payload = {} if body is None else body
    return SimpleNamespace(status_code=status, json=lambda: payload, url=url)


def _error_response(code, *, status=400, message="boom"):
    """``user/register`` 的错误体，形状取自生产日志。"""
    return _response({"error": {"code": code, "message": message}}, status=status)


def _follow(*, response=None):
    """Faithful stand-in for ``http_utils._follow_continue_url``.

    It mirrors the real short-circuit (``if not url: return None``) so the
    "no continue URL" abort path is exercised for real.  The real helper's own
    behaviour -- including the empty-URL short-circuit -- is pinned separately in
    ``tests/test_http_utils_pure.py::FollowContinueUrlTests``.
    """
    landed = response if response is not None else _response()
    return Mock(side_effect=lambda session, url, headers, **kw: landed if url else None)


def _state(**over):
    values = dict(
        username="lane@example.test",
        auth_base="https://auth.openai.com",
        registration_mode="password",
        signup_state={
            "url": "https://auth.openai.com/create-account/password",
            "attempt": "a1",
            "status": 200,
        },
        reg_data={},
        session=object(),
        base_headers={"X-Base": "1"},
        auth_flow_started=_AUTH_FLOW_STARTED,
        password_unknown=False,
    )
    values.update(over)
    return RegistrationRuntimeState(**values)


def _workflow(
    state, *, follow=None, user_register=None, dump=None, password_step=None,
    existing_login_redirect=None,
):
    w = object.__new__(RegistrationEmailWorkflow)
    w.config = {}
    w.runtime = state
    w._issue_sentinel = Mock(return_value=SimpleNamespace(token="T", so_token="S"))
    w._operations = SimpleNamespace(
        # The real URL helper: the contract under test is that the URL is read
        # out of the response body, so faking it here would test nothing.
        _email_otp_send_url=_email_otp_send_url,
        SyntheticResponse=lambda status, body, url="": SimpleNamespace(
            status_code=status, json=lambda: body, url=url,
        ),
        _follow_continue_url=follow if follow is not None else _follow(),
        _fetch_client_auth_session_dump=Mock(return_value={} if dump is None else dump),
        _json_or_raw=lambda resp: resp.json(),
        request_with_retry=Mock(
            return_value=user_register
            if user_register is not None
            else _response({"continue_url": _CONTINUE_URL})
        ),
        _auth_request_headers=Mock(return_value={}),
        auth_impersonate=Mock(return_value=""),
        _sanitize_text=str,
        # ``_password_lane_active`` calls this, so tests about the lane gate pass
        # the **real** ``auth_flow._is_signup_password_step``; the default Mock
        # keeps the URL-tracking tests independent of it.
        _is_signup_password_step=password_step
        if password_step is not None
        else Mock(return_value=False),
        # Same reasoning for the login-page landing: the P1 contract *is* the
        # landing predicate, so its default is the real pure function.  It only
        # answers True for ``/log-in*``, which no other case in this file uses
        # as a ``user/register`` landing -- so it cannot silently re-route them.
        _is_existing_login_redirect=existing_login_redirect
        if existing_login_redirect is not None
        else auth_flow._is_existing_login_redirect,
    )
    return w


# ---------------------------------------------------------------------------
# 1) 密码泳道：200 必须被跟，且 URL 来自响应体
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [_CONTINUE_URL, _OTHER_URL])
def test_the_followed_url_tracks_the_response_body(url):
    """🔴 换一个 ``continue_url``，跟的目标必须跟着换。

    这条用例专门杀「把 URL 写死成常量」的变异体 —— 那种写法在单值样本上
    看起来完全正确，只有换第二个值才会露出来。
    """
    state = _state(registration_mode="password", reg_data={"continue_url": url})
    follow = _follow()
    w = _workflow(state, follow=follow)

    w.send_email_otp()

    follow.assert_called_once()
    assert follow.call_args.args[1] == url


def test_the_follow_keeps_the_log_label_that_the_verdict_was_measured_with():
    """H1 的判决靠 ``grep "Email OTP send: (\\d{3})"`` 数出来的。

    改标签 = 毁掉证据通道，下一次再做同类判定时数不出数。
    """
    state = _state(registration_mode="password", reg_data={"continue_url": _CONTINUE_URL})
    follow = _follow()
    w = _workflow(state, follow=follow)

    w.send_email_otp()

    assert follow.call_args.kwargs.get("label") == "Email OTP send"
    assert follow.call_args.kwargs.get("referer") == "https://auth.openai.com/create-account/password"


def test_the_password_lane_does_not_backdate_the_otp_issue_time():
    """合成分支会把 ``otp_issued_after`` 回填到 ``auth_flow_started - 5``。

    密码泳道跟的是**真实**请求，所以时间戳必须是当下，不能被回填 ——
    回填会让 ``wait_email_otp`` 把「服务端刚发的码」当成早已存在。
    """
    state = _state(registration_mode="password", reg_data={"continue_url": _CONTINUE_URL})
    w = _workflow(state)

    w.send_email_otp()

    assert state.otp_issued_after > _AUTH_FLOW_STARTED


def test_a_200_without_a_continue_url_aborts_instead_of_guessing(monkeypatch):
    """没有落点就停手 —— 不许回落到某个「看起来对」的默认端点。

    这里接的是**真的** ``_follow_continue_url``（只把传输层换成记录器），
    所以「一个请求都没发出去」来自真实短路，而不是被假对象模拟出来的。
    """
    sent: list[dict] = []

    def recorder(session, method, url, *, label="", **kwargs):
        sent.append({"method": method, "url": url, "label": label})
        return _response()

    monkeypatch.setattr(
        http_utils, "CFG", {"chatgpt": {"auth_base_url": "https://auth.openai.com"}}
    )
    monkeypatch.setattr(http_utils, "auth_impersonate", lambda: "")
    monkeypatch.setattr(http_utils, "request_with_retry", recorder)

    state = _state(
        registration_mode="password",
        reg_data={},
    )
    w = _workflow(state, follow=http_utils._follow_continue_url)

    with pytest.raises(RegistrationAbort) as exc:
        w.send_email_otp()

    assert "email_otp_send_missing_continue_url" in str(exc.value)
    assert sent == [], "空 continue_url 不许发出任何请求"


# 🔴 ``test_resume_mode_falls_back_to_the_canonical_send_endpoint`` lived here
# until 2026-09-16.  It covered the ``resume_email_verification`` fallback to
# ``/api/accounts/email-otp/send``; the flag **and** the fallback are both gone
# (owner decision -- the flag had lost its only writer, so the branch was
# unreachable).  The surviving contract -- *the URL comes from the response
# body, never from a guess* -- is pinned by
# ``test_the_followed_url_tracks_the_response_body`` here and by
# ``test_email_otp_send_url_only_ever_reads_the_response_body`` in
# ``test_registration_concurrency``.


def test_a_rejected_follow_surfaces_the_status_code():
    """跟了但被拒（如 409）必须报出来，不能被吞成「成功」。"""
    state = _state(registration_mode="password", reg_data={"continue_url": _CONTINUE_URL})
    w = _workflow(state, follow=Mock(return_value=_response(status=409)))

    with pytest.raises(RegistrationAbort) as exc:
        w.send_email_otp()

    assert "email_otp_send_failed:409" in str(exc.value)


# ---------------------------------------------------------------------------
# 2) passwordless 泳道：不得 POST，不得跟
# ---------------------------------------------------------------------------

def test_passwordless_lane_never_posts_user_register():
    """「别把探针加回来」的守卫。

    探针就是在这个分支里插进来的。它一旦回来，被接受的 POST 会推进服务端
    事务，而随后的 OTP 走的是合成分支（不跟响应）⇒ 每次 ``validate`` 都 409。
    """
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/log-in", "attempt": "a1", "status": 200},
    )
    w = _workflow(state)

    w.user_register()

    w._operations.request_with_retry.assert_not_called()
    assert state.reg_data["mode"] == "passwordless_signup"
    assert state.password_unknown is True


def test_passwordless_lane_never_follows_a_continue_url():
    """合成分支是 passwordless 的**唯一**合法路径。"""
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/log-in", "attempt": "a1", "status": 200},
    )
    follow = _follow()
    w = _workflow(state, follow=follow)

    w.send_email_otp()

    follow.assert_not_called()
    # 回填到 ``auth_flow_started - 5``：合成分支的可观测指纹。
    assert state.otp_issued_after == _AUTH_FLOW_STARTED - 5


def test_passwordless_lane_has_no_continue_url_to_follow_even_by_accident():
    """纵深防御：即使有人把 ``send_email_otp`` 改成无条件跟，也跟不动。

    passwordless 分支合成的 ``reg_data`` 里**没有** ``continue_url``，而
    ``_email_otp_send_url`` 只认响应体（回落分支已随 ``resume_email_verification``
    一起删除）⇒ 返回空串。
    """
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/log-in"},
    )
    w = _workflow(state)
    w.user_register()

    assert _email_otp_send_url(state.reg_data) == ""


# ---------------------------------------------------------------------------
# 3) 服务端强制密码路由：必须推回密码泳道
# ---------------------------------------------------------------------------

def test_a_server_forced_password_route_still_posts_and_follows():
    """``password_fallback`` = 服务端把 passwordless 路由到 ``/log-in/password``。

    这种地址**已经**被服务端推进了密码事务。若它还留在合成分支（不 POST、不跟），
    就会重现 B 组那个 85/85 全 409 的形状。
    """
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/log-in/password", "password_fallback": True},
        reg_data={"continue_url": _CONTINUE_URL},
    )
    follow = _follow()
    w = _workflow(state, follow=follow)

    w.user_register()
    w.send_email_otp()

    w._operations.request_with_retry.assert_called_once()
    assert follow.call_args.args[1] == _CONTINUE_URL


def test_the_password_step_url_alone_is_enough_to_take_the_password_lane():
    """``password_fallback`` 没置位，但当前 URL 已是密码步 ⇒ 同样走密码泳道。"""
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/create-account/password"},
        reg_data={"continue_url": _CONTINUE_URL},
    )
    w = _workflow(state)
    w._operations._is_signup_password_step = Mock(return_value=True)

    w.user_register()

    w._operations.request_with_retry.assert_called_once()


def test_a_non_password_url_does_not_hijack_the_passwordless_lane():
    """反面：普通的 ``/log-in`` 不能被误判成密码步，否则 passwordless 被架空。"""
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/log-in"},
    )
    w = _workflow(state)
    w._operations._is_signup_password_step = Mock(return_value=False)

    w.user_register()

    w._operations.request_with_retry.assert_not_called()


# ---------------------------------------------------------------------------
# 4) 密码步未被服务端确认 ⇒ 发码之前 abort（2026-09-16 拍板）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,url,fallback,expected", _PASSWORD_LANE_CASES)
def test_the_password_lane_has_two_independent_ways_in(mode, url, fallback, expected):
    """门禁本身：**运营选择**（``registration_mode``）与**服务端改道**是两件事。

    这两条路以前写成同一个表达式复制在两处，漂移过一次；现在只有一个 owner。
    这条用例同时是「URL 自己就是密码步」那一支的生产判据 —— 传进去的是真的
    ``auth_flow._is_signup_password_step``。
    """
    state = _state(
        registration_mode=mode,
        signup_state={"url": url, "attempt": "a1", "status": 200, "password_fallback": fallback},
    )
    w = _workflow(state, password_step=auth_flow._is_signup_password_step)

    assert w._password_lane_active() is expected


def test_an_unconfirmed_password_aborts_before_any_otp_is_sent():
    """核心契约：服务端没接受密码 ⇒ 停手，且**一个 OTP 都没烧**。

    ``invalid_auth_step`` 的意思是「事务不在密码步」。再往下走就是 ``create_account``，
    它会建出一个**无密码账号** —— 而「不产生无密码账号」正是密码优先模式存在的
    全部理由。与 ``else`` 分支的唯一区别就是**时机**：这里在 ``send_email_otp``
    之前，所以不会为一个注定被拒的地址白烧一个邮箱 OTP。
    """
    state = _state(
        registration_mode="password",
        signup_state={"url": _EMAIL_VERIFICATION_URL, "attempt": "a1", "status": 200},
        reg_data={},
    )
    follow = _follow()
    w = _workflow(
        state,
        follow=follow,
        user_register=_error_response("invalid_auth_step"),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert str(exc.value) == "password_step_unconfirmed:invalid_auth_step"
    follow.assert_not_called()
    assert state.otp_issued_after == 0, "止损必须发生在发码之前"


def test_the_deleted_resume_flag_cannot_be_smuggled_back_into_the_state():
    """被删掉的 ``resume_email_verification`` 不许从构造器悄悄回来。

    它原先在这里被置真，然后继续走 ``create_account`` —— 照样建无密码账号；
    即使不建号，对已存在账号也只会落在 ``/about-you``（NextAuth session cookie
    从不下发，2026-09-14 实测 5/5），属于白烧 OTP。三条理由见
    ``registration_handlers.user_register`` 的注释。

    🔴 ``RegistrationRuntimeState`` 只接受 ``_FIELD_GROUPS`` 里的键 ⇒「复活它」
    必须先改 ``registration_runtime``。本用例就是那道闸：变异 ``MI`` 把字段加
    回去，这里立刻红。
    """
    with pytest.raises(TypeError) as exc:
        RegistrationRuntimeState(resume_email_verification=True)

    assert "resume_email_verification" in str(exc.value)


def test_a_200_on_the_password_lane_is_not_an_abort():
    """对照：200 是正常路径，不许被止损逻辑误伤。"""
    state = _state(
        registration_mode="password",
        signup_state={"url": _EMAIL_VERIFICATION_URL},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_response({"continue_url": _CONTINUE_URL}),
        password_step=auth_flow._is_signup_password_step,
    )

    w.user_register()

    assert state.reg_data["continue_url"] == _CONTINUE_URL


def test_the_stop_requires_both_the_code_and_the_step_url():
    """同一个 code，但事务**不在** email-verification ⇒ 走 ``else``。

    这半边条件不能省：``invalid_auth_step`` 也可能来自别的事务位置，那时
    「密码没被接受」这个结论并不成立。

    2026-09-16（P0）后 ``else`` 报的是**服务端 code**（``err_code or err_msg``），
    不再是 message —— 分类器按 code 做子串匹配，拼 message 会让分类掉进
    ``unknown``（既不重试也不记掉号）。这里落点是 ``/create-account/password``，
    是**注册**密码步而不是登录页，所以既不进 ``password_step_unconfirmed``，
    也不进 ``signup_routed_to_login``（后者要求 ``/log-in*``）。
    """
    state = _state(
        registration_mode="password",
        signup_state={"url": "https://auth.openai.com/create-account/password"},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_auth_step", message="not at email verification"),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert str(exc.value) == "user_register:invalid_auth_step"
    assert state.existing_account is False, "注册密码步不是登录页，不许拉黑"


def test_another_error_code_is_not_dressed_up_as_an_unconfirmed_password():
    """反过来：URL 对但 code 不对，也不许套用新错误名。

    P0 之后 ``else`` 报的是 code（``invalid_state``）而不是 message
    （``session gone``）—— 这正是 P0 要的效果：``invalid_state`` 在 marker 表里
    是 ``auth_state``，而 ``session gone`` 什么都匹配不上。
    """
    state = _state(
        registration_mode="password",
        signup_state={"url": _EMAIL_VERIFICATION_URL},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_state", status=409, message="session gone"),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert str(exc.value) == "user_register:invalid_state"
    assert classify_error(str(exc.value)) == "auth_state"


def test_the_unconfirmed_password_abort_is_unreachable_from_the_passwordless_lane():
    """passwordless 泳道连 POST 都不发，自然碰不到这条止损。

    这条用例是把「门禁在函数入口」这个事实钉死：如果哪天有人把 ``user_register``
    的入口门禁挪走、让 passwordless 也 POST，这里会立刻红。
    """
    state = _state(
        registration_mode="passwordless",
        signup_state={"url": "https://auth.openai.com/log-in", "attempt": "a1", "status": 200},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_auth_step"),
        password_step=auth_flow._is_signup_password_step,
    )

    w.user_register()  # 不抛

    w._operations.request_with_retry.assert_not_called()
    assert state.reg_data["mode"] == "passwordless_signup"


@pytest.mark.parametrize("suffix", ["invalid_auth_step", "http_400", "unclassified"])
def test_the_unconfirmed_password_family_is_retryable_and_never_blacklists(suffix):
    """``auth_state`` 而不是 ``account``：地址**没被消费**，不许被拉黑。

    ``account`` 类的 ``batch_dropped`` 会把一个本来可注册的地址永久踢出池子。
    这里什么都没建成，只是这一轮没谈成，下一批换出口应当再试。

    ⚠️ 判据钉的是**前缀** ``password_step_unconfirmed``，不是完整错误串。今天的
    唯一后缀 ``invalid_auth_step`` 本身就是 ``auth_state`` 的标记（所以它对这一个
    字符串「顺带」成立）；``http_400`` 才真正检验前缀标记在起作用。
    """
    error = f"password_step_unconfirmed:{suffix}"

    assert classify_error(error) == "auth_state"
    assert "auth_state" in BATCH_RETRY_CLASSES
    assert "auth_state" not in BATCH_DROPPED_CLASSES


# ---------------------------------------------------------------------------
# 7) 2026-09-16 P0/P1：错误串必须带 code；登录页落点 = 地址已存在
# ---------------------------------------------------------------------------


def test_the_error_string_carries_the_machine_code_not_the_human_message():
    """P0：分类器按 **code** 匹配，所以错误串的骨架必须是 code。

    生产实测（批次 34632，2026-09-16 11:00，**21/22 失败**）：``else`` 分支拼的是
    ``err_msg``，串里是 ``Invalid authorization step.`` —— 而 marker 表里写的是
    ``invalid_auth_step``。两者**没有共同子串**，于是分类掉进兜底类 ``unknown``，
    而 ``unknown`` 既不在 ``BATCH_RETRY_CLASSES`` 也不在 ``BATCH_DROPPED_CLASSES``
    ⇒ 不重试、不记掉号、**静默蒸发 21 个地址**。

    同一个服务端响应，只改拼法就能让分类从 ``unknown`` 回到 ``auth_state``。
    """
    state = _state(
        registration_mode="password",
        signup_state={"url": "https://auth.openai.com/create-account/password"},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_auth_step", message="Invalid authorization step."),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    error = str(exc.value)
    assert error == "user_register:invalid_auth_step"
    assert classify_error(error) == "auth_state"


def test_the_code_is_used_even_when_the_server_sends_no_message():
    """服务端只给 code、不给 message 时，串里也必须带 code。

    ``err_code or err_msg`` 的右半边只在 code 为空时才轮到 message，
    所以 message 缺失不能把 code 挤掉。
    """
    state = _state(
        registration_mode="password",
        signup_state={"url": "https://auth.openai.com/create-account/password"},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_response({"error": {"code": "invalid_auth_step"}}, status=400),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert str(exc.value) == "user_register:invalid_auth_step"


def test_the_message_is_still_kept_when_there_is_no_code():
    """code 缺失时才回落到 message —— 不许连服务端原文都丢掉。"""
    state = _state(
        registration_mode="password",
        signup_state={"url": "https://auth.openai.com/create-account/password"},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_response({"error": {"message": "boom without a code"}}, status=400),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert str(exc.value) == "user_register:boom without a code"


def test_a_login_page_landing_is_recorded_as_an_existing_account(monkeypatch):
    """P1：落点 ``/log-in/password`` ⇒ 服务端在注册入口就说「这个地址要登录」。

    生产实测（批次 34632 + 28512，2026-09-16）：``login_or_signup`` 落
    ``/log-in/password`` 的账号**全部**在 ``user/register`` 收到
    ``invalid_auth_step``；两批合计 31 个落点、31 个失败、**0 例外**。

    处置必须是**不重试**（重试改变不了「地址已存在」），终态 ``partial_registered``
    —— 与 09-15 那 305 个 ``user_already_exists`` 同类。

    ⚠️ 判据是 ``_is_existing_login_redirect``（``/log-in*``），**不是**
    ``_is_signup_password_step``（``/create-account/password``）。两者都会让
    ``_password_lane_active`` 为真，但只有前者表达「地址已存在」。
    """
    state = _state(
        registration_mode="passwordless",
        signup_state={
            "url": "https://auth.openai.com/log-in/password",
            "password_fallback": True,
            "attempt": "a1",
            "status": 200,
        },
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_auth_step", message="Invalid authorization step."),
        password_step=auth_flow._is_signup_password_step,
    )
    marked = []
    monkeypatch.setattr(w, "_mark_partial_registration", lambda **kw: marked.append(kw))

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert state.existing_account is True, "终态装配靠这个标志翻成 partial_registered"
    error = str(exc.value)
    assert classify_error(error) == "account"
    assert "account" in BATCH_DROPPED_CLASSES
    assert "account" not in BATCH_RETRY_CLASSES
    assert marked == [{"reason": "signup_routed_to_login"}]
    # 🔴 账本不许借用**别的错误串**的名字。服务端在这里说的是
    # ``invalid_auth_step``；``user_already_exists`` 是同一 verdict 的另一种
    # （更晚出现的）表达方式。串里若带上它，「这次到底是哪种形状」就无法从存储
    # 回答 —— 而 ``classify_error`` 恰好也认它，所以光看分类是抓不住的。
    assert "user_already_exists" not in error


def test_a_signup_password_step_is_not_treated_as_an_existing_account():
    """对照：``/create-account/password`` 是**注册**密码步，不是登录页。

    同一个 ``invalid_auth_step``，落点不同则结论不同 —— 这条用例杀「把两个
    判据合并成一个」的变异体（合并后这个地址会被错误地拉黑）。
    """
    state = _state(
        registration_mode="passwordless",
        signup_state={
            "url": "https://auth.openai.com/create-account/password",
            "password_fallback": True,
            "attempt": "a1",
            "status": 200,
        },
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_auth_step", message="Invalid authorization step."),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert state.existing_account is False
    assert str(exc.value) == "user_register:invalid_auth_step"


def test_the_login_page_dead_end_names_a_marker_the_guard_recognises():
    """``mark_dead_end`` 对不认识的 reason 会**静默**回落成 ``user_already_exists``。

    那等于让账本用**另一个错误串的名字**记录本次事件（服务端说的是
    ``invalid_auth_step``）。所以新 marker 必须先进 ``DEAD_END_MARKERS``。
    """
    from sms_tool.registration_retry_guard import DEAD_END_MARKERS, _dead_end_reason

    assert "signup_routed_to_login" in DEAD_END_MARKERS
    assert _dead_end_reason("existing_account_signup_routed_to_login") == "signup_routed_to_login"


def test_the_email_verification_stop_is_untouched_by_the_login_page_branch():
    """回归：落点含 ``email-verification`` ⇒ 仍走 ``password_step_unconfirmed``。

    这是 2026-09-16 早先拍板的契约（``auth_state``，可重试，不拉黑）——
    P1 只加了一条**新的**落点分支，不许把它吃掉。
    """
    state = _state(
        registration_mode="password",
        signup_state={"url": _EMAIL_VERIFICATION_URL},
        reg_data={},
    )
    w = _workflow(
        state,
        user_register=_error_response("invalid_auth_step", message="Invalid authorization step."),
        password_step=auth_flow._is_signup_password_step,
    )

    with pytest.raises(RegistrationAbort) as exc:
        w.user_register()

    assert str(exc.value) == "password_step_unconfirmed:invalid_auth_step"
    assert state.existing_account is False

