"""P0-2：OTP 超时根因分辨化 —— 「服务端没派发」vs「邮箱侧没有码」。

🔴 2026-09-16 改名：第二个后缀原名 ``code_not_delivered``，**名字在撒谎**。
它的判据只是 ``otp_dispatch_verdict() == "dispatched"``（服务端侧的发码事务
没有挂起键），推不出「邮件投递到了邮箱」。实测反例（批次 25116）：渠道
``ima3.52dfd.top`` 返回 72 字节 JSON，HTML 解析器结构性读不到，10/10 失败
仍被标成 ``code_not_delivered`` ⇒ 真实语义是「我们没读出来」。现名
``mailbox_side_no_code`` 只陈述已知事实。

背景（2026-09-16 实测，批次 25288）：脉冲调度把「一轮里 OTP 失败聚集」当成
出口被封禁的证据，命中就暂停 60s 换池。旧判据只做子串匹配
（``OTP_BAN_MARKERS`` 里的 ``otp_poll_timeout`` 命中 ``email_otp_poll_timeout``），
于是把**邮箱侧**超时也算成封禁 ⇒ 每个 wave 白停 60s，而且换出口无效（Wave 3
复发）。

实测依据：6 个 ``email_otp_poll_timeout`` 的 run 在
``client_auth_session_dump[after_otp_send]`` 的事务键里都还留着
``passwordless_email_otp_send_pending``（服务端**没完成**派发），而同批所有拿到
码的 run 从头到尾没有这个键。

另外，``batch_runner._run_one`` 把每个账号钉在池里各自的出口上，所以「一轮里
有几个 OTP 失败」本身不构成出口证据：wave 2 = 3 超时 + 1 拿到码，wave 3 =
2 超时 + 2 拿到码，而同一出口的 wave 4/5/6 是 0 超时。⇒ 只有**整轮一致**的
派发侧失败才叫 IP 封禁。

三段必须**同时**成立，否则就是「改了但没接上」：

  1. 判定器 ``auth_state.otp_dispatch_verdict`` 读 dump；
  2. 处理器 ``RegistrationEmailWorkflow._otp_timeout_error`` 把判定写进错误串；
  3. 脉冲 ``registration_pulse._is_otp_ban_signal`` 按后缀分辨、
     ``_detect_ip_ban`` 再要求整轮一致。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from sms_tool.auth_state import otp_dispatch_verdict, signup_lane_verdict
from sms_tool.failure_registry import (
    BATCH_RETRY_CLASSES,
    OTP_MAILBOX_SIDE_MARKER,
    OTP_NO_RESEND_MARKER,
    OTP_UNDISPATCHED_MARKER,
)
from sms_tool.registration_handlers import (
    RegistrationAbort,
    RegistrationEmailWorkflow,
)
from sms_tool.registration_pulse import _detect_ip_ban, _is_otp_ban_signal
from sms_tool.registration_state import RegistrationStateMachine


def _summary(keys):
    return {"top_keys": ["checksum", "client_auth_session", "session_id"],
            "client_auth_session_keys": list(keys), "signals": {}}


# 线上真实形状（批次 25288）：注册泳道拿到码 = 17 键，无挂起标记；
# 6 个超时的 run = 18 键，多一个 ``passwordless_email_otp_send_pending``。
_DISPATCHED = _summary([
    "app_name_enum", "auth_session_logging_id", "country_code_hint",
    "destination_app_name", "email", "email_verification_mode",
    "openai_client_id", "original_screen_hint", "passwordless_disabled",
    "passwordless_otp_from_password_redirect", "passwordless_signup_from_default_redirect",
    "promo", "requested_oauth_scopes", "session_id", "signup_mode",
    "signup_source", "username",
])
_STUCK = _summary(list(_DISPATCHED["client_auth_session_keys"]) + [
    "passwordless_email_otp_send_pending",
])
# 登录泳道形状（13 键）：服务端按登录处理，且**码照常到达**
# （实测 da7df644 20:39:04 取码 → validate 200）。
_LOGIN_LANE = _summary([
    "app_name_enum", "auth_session_logging_id", "country_code_hint",
    "destination_app_name", "email", "openai_client_id", "original_screen_hint",
    "passwordless_login_magic_link_sent", "promo", "requested_oauth_scopes",
    "session_id", "signup_mode", "signup_source",
])


# --------------------------------------------------------------------------
# 1) 判定器
# --------------------------------------------------------------------------

def test_verdict_reports_stuck_when_the_transaction_key_is_still_pending():
    assert otp_dispatch_verdict(_STUCK) == "stuck"


def test_a_login_magic_link_is_not_a_stuck_send():
    """🔴 ``passwordless_login_magic_link_sent`` **不属于**「发码挂起」。

    它表示服务端按登录泳道发了 magic link，实测那三个 run 里有 2 个正常收到了码。
    把它算成挂起会让整个登录泳道被误判为「不可能拿到码」。
    """
    assert otp_dispatch_verdict(_LOGIN_LANE) == "dispatched"


def test_verdict_reports_dispatched_when_no_pending_key_remains():
    assert otp_dispatch_verdict(_DISPATCHED) == "dispatched"


@pytest.mark.parametrize("dump", [
    None,
    {},
    "not a summary",
    {"status": 404, "body": {}},          # 非 200 的 dump 没有键列表
    {"client_auth_session_keys": []},      # body 里压根没有 client_auth_session
])
def test_verdict_refuses_to_guess_on_unusable_dumps(dump):
    """空键列表**不能**当 ``dispatched`` —— 那是「读不到」，不是「派发完成」。"""
    assert otp_dispatch_verdict(dump) == "unknown"


# --------------------------------------------------------------------------
# 1b) 泳道判定（P0-1 判据 A：取码前止损的取证埋点）
# --------------------------------------------------------------------------

def test_login_lane_verdict_only_fires_on_the_magic_link_key():
    assert signup_lane_verdict(_LOGIN_LANE) == "login"
    assert signup_lane_verdict(_DISPATCHED) == "signup"
    assert signup_lane_verdict(_STUCK) == "signup"


def test_signup_lane_does_not_mean_unregistered():
    """🔴 「没有 magic-link 键」**推不出**「未注册」。

    实测：10 个这种形状的 run 里 9 个最终仍是 ``user_already_exists``。
    服务端只在 ``create_account`` 给出存在性判决，而那时 OTP 已经花掉了。
    """
    assert signup_lane_verdict(_DISPATCHED) == "signup"  # 但它可能是已注册地址


@pytest.mark.parametrize("dump", [None, {}, {"status": 500, "body": {}},
                                  {"client_auth_session_keys": []}])
def test_lane_verdict_refuses_to_guess_on_unusable_dumps(dump):
    assert signup_lane_verdict(dump) == "unknown"


# --------------------------------------------------------------------------
# 2) 处理器：判据 B 在轮询之前止损
# --------------------------------------------------------------------------

def _workflow() -> RegistrationEmailWorkflow:
    ops = Mock()
    ops._poll_registration_email_otp = Mock(return_value="")
    ops._sanitize_text = Mock(side_effect=lambda value: str(value))
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None), operations=ops, config={},
    )
    workflow.runtime.resources.mailbox_service = Mock()
    return workflow


def _wait(dump, provider=None):
    """``provider=None`` = 运行态里没有 mailbox（渠道名取不到 ⇒ 空串）。"""
    workflow = _workflow()
    workflow.runtime.otp_send_dump = dump
    if provider is not None:
        workflow.runtime.mailbox = type("Mailbox", (), {"provider": provider})()
    workflow.wait_email_otp()
    return workflow


def test_a_stuck_send_aborts_before_polling_the_mailbox():
    """P0-1 判据 B：服务端没派发 ⇒ 不去烧满 300s 轮询。"""
    with pytest.raises(RegistrationAbort) as excinfo:
        _wait(_STUCK)
    assert str(excinfo.value) == "email_otp_send_stuck"


def test_a_stuck_send_never_calls_the_poller():
    """止损必须发生在轮询**之前** —— 否则它只是换了个错误名。"""
    workflow = _workflow()
    workflow.runtime.otp_send_dump = _STUCK
    with pytest.raises(RegistrationAbort):
        workflow.wait_email_otp()
    workflow.r._poll_registration_email_otp.assert_not_called()


def test_a_stuck_send_is_classified_and_counted_as_a_dispatch_side_signal():
    """新错误名必须进注册表，否则分类成 ``unknown``（既不重试也不记账）。"""
    from sms_tool.error_classification import classify_error
    from sms_tool.registration_policy import registration_retry_decision

    assert classify_error("email_otp_send_stuck") == "mailbox"
    assert "mailbox" in BATCH_RETRY_CLASSES
    assert registration_retry_decision("email_otp_send_stuck").failure_class == "mailbox"
    # 且脉冲把它算作**派发侧**候选信号（名字里含 ``otp_send_stuck``）。
    assert _is_otp_ban_signal(
        {"success": False, "error": "email_otp_send_stuck", "failure_class": "mailbox"}
    ) is True


def test_the_poll_is_still_reached_when_the_dump_is_unusable():
    """没有证据时不许止损 —— 否则一次 dump 抖动就会跳过所有取码。"""
    with pytest.raises(RegistrationAbort) as excinfo:
        _wait({"status": 500, "body": {}})
    assert str(excinfo.value) == "email_otp_poll_timeout"


@pytest.mark.parametrize("dump, provider, expected", [
    # 渠道在重发名单里 ⇒ 只有邮箱侧后缀（这一轮**有**第二次发码机会）。
    (_DISPATCHED, "remail", f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}"),
    (_DISPATCHED, "icloud_url", f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}"),
    # 渠道不在名单里 ⇒ 追加**能力**后缀（不是第三个根因）。
    (_DISPATCHED, "gmail", f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}:{OTP_NO_RESEND_MARKER}"),
    (_LOGIN_LANE, "imap", f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}:{OTP_NO_RESEND_MARKER}"),
    # 拿不到渠道名（运行态里没有 mailbox）⇒ 空串不在名单里 ⇒ 也标「无重发」。
    (_DISPATCHED, None, f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}:{OTP_NO_RESEND_MARKER}"),
    # dump 不可用 ⇒ 没有证据 ⇒ 裸名，**不许**加任何后缀（连能力后缀也不加：
    # 那会让人以为「根因已判定」）。
    ({"status": 500, "body": {}}, "gmail", "email_otp_poll_timeout"),
    ({}, "remail", "email_otp_poll_timeout"),
])
def test_wait_email_otp_suffixes_its_timeout_with_the_dump_verdict(dump, provider, expected):
    with pytest.raises(RegistrationAbort) as excinfo:
        _wait(dump, provider)
    assert str(excinfo.value) == expected


def test_the_no_resend_suffix_is_channel_capability_not_a_third_root_cause():
    """``no_resend_for_channel`` 只在**已经确定是邮箱侧**时才追加。

    它描述的是「这个渠道没有重发能力」，所以它必须与 ``mailbox_side_no_code``
    **组合**出现；单独出现就说明判据写反了（把能力后缀当成了根因）。
    """
    with pytest.raises(RegistrationAbort) as excinfo:
        _wait(_DISPATCHED, "gmail")
    error = str(excinfo.value)
    assert OTP_MAILBOX_SIDE_MARKER in error
    assert OTP_NO_RESEND_MARKER in error
    assert error.index(OTP_MAILBOX_SIDE_MARKER) < error.index(OTP_NO_RESEND_MARKER)


def test_send_email_otp_keeps_the_dump_instead_of_discarding_it():
    """``send_email_otp`` 以前只打印 dump、把返回值丢掉 —— 判据就没了输入。"""
    workflow = _workflow()
    workflow.runtime.reg_data = {}
    workflow.runtime.registration_mode = "passwordless"
    workflow.runtime.signup_state = {"url": "https://auth.openai.com/email-verification"}
    workflow.r.send_email_otp_stub = None
    workflow.r._email_otp_send_url = Mock(return_value="https://auth.openai.com/x")
    workflow.r._follow_continue_url = Mock()
    workflow.r._is_signup_password_step = Mock(return_value=False)
    workflow.r._fetch_client_auth_session_dump = Mock(return_value=_STUCK)
    workflow.r.SyntheticResponse = Mock(return_value=Mock(status_code=204))
    workflow.r._json_or_raw = Mock(return_value={"assumed_pre_sent": True})

    workflow.send_email_otp()

    assert workflow.runtime.otp_send_dump == _STUCK


def _stub_auth_flow(workflow, dump):
    """把 ``auth_flow`` 的请求面全部桩掉，只留 P0-1 埋点那条路径真实执行。"""
    r = workflow.r
    r.request_with_retry = Mock(return_value=Mock(status_code=200))
    r._json_or_raw = Mock(return_value={"csrfToken": "csrf-abc"})
    r.chatgpt_headers = Mock(return_value={})
    r.nextauth_headers = Mock(return_value={})
    r.auth_impersonate = Mock(return_value=None)
    r._passwordless_signin_attempts = Mock(return_value=1)
    r._prepare_signup_auth_state = Mock(
        return_value={"status": 200, "ok": True, "url": "https://auth.openai.com/email-verification"}
    )
    r._is_chatgpt_auth_login_landing = Mock(return_value=False)
    r._fetch_client_auth_session_dump = Mock(return_value=dump)
    workflow.runtime.registration_mode = "passwordless"
    workflow.runtime.username = ""  # 让 _persist_checkpoint 早退，不碰真实存储


def test_auth_flow_records_the_signup_lane_hint_before_spending_a_code():
    """P0-1 判据 A 的取证埋点：**取码之前**就把泳道判定记进运行态。

    这里只断言「记下来了」。刻意**不**断言它写死路账本 —— 精确率 3/3 但召回率
    只有 3/11，样本还不足以承担误判代价（误判会把一个可注册地址永久拉黑）。
    """
    workflow = _workflow()
    _stub_auth_flow(workflow, _LOGIN_LANE)

    workflow.auth_flow()

    assert workflow.runtime.signup_dump == _LOGIN_LANE
    assert workflow.runtime.signup_lane == "login"
    assert workflow.r.request_with_retry.call_args_list[0].kwargs["attempts"] == 1


def test_auth_flow_leaves_the_lane_unknown_when_the_dump_is_unusable():
    workflow = _workflow()
    _stub_auth_flow(workflow, None)

    workflow.auth_flow()

    assert workflow.runtime.signup_dump == {}
    assert workflow.runtime.signup_lane == "unknown"


def test_send_email_otp_stores_an_empty_dump_when_the_probe_returns_junk():
    workflow = _workflow()
    workflow.runtime.reg_data = {}
    workflow.runtime.registration_mode = "passwordless"
    workflow.runtime.signup_state = {"url": "https://auth.openai.com/email-verification"}
    workflow.r._email_otp_send_url = Mock(return_value="https://auth.openai.com/x")
    workflow.r._follow_continue_url = Mock()
    workflow.r._is_signup_password_step = Mock(return_value=False)
    workflow.r._fetch_client_auth_session_dump = Mock(return_value=None)
    workflow.r.SyntheticResponse = Mock(return_value=Mock(status_code=204))
    workflow.r._json_or_raw = Mock(return_value={"assumed_pre_sent": True})

    workflow.send_email_otp()

    assert workflow.runtime.otp_send_dump == {}


# --------------------------------------------------------------------------
# 3) 脉冲按后缀分辨
# --------------------------------------------------------------------------

def _fail(error, failure_class="mailbox"):
    return {"success": False, "error": error, "failure_class": failure_class}


def test_mailbox_side_timeout_is_not_a_ban_signal():
    """旧判据在这条上答 True（子串 ``otp_poll_timeout`` 命中），这是 P0-2 的修复点。"""
    assert _is_otp_ban_signal(
        _fail(f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}")
    ) is False


def test_stuck_send_is_a_dispatch_side_signal():
    """``otp_send_stuck`` 是**派发侧**候选信号 —— 够不够格叫封禁另由整轮一致性判。"""
    assert _is_otp_ban_signal(
        _fail(f"email_otp_poll_timeout:{OTP_UNDISPATCHED_MARKER}")
    ) is True


def test_bare_timeout_falls_back_to_the_legacy_substring_rule():
    """dump 不可用 ⇒ 没有证据 ⇒ 宁可多停 60s，也不要整轮撞在被封的出口上。"""
    assert _is_otp_ban_signal(_fail("email_otp_poll_timeout")) is True


def test_the_combined_no_resend_suffix_is_still_not_a_ban_signal():
    """组合串里仍然含 ``otp_poll_timeout`` 子串 ⇒ 短路顺序是**承重**的。

    ``_is_otp_ban_signal`` 必须在 ``OTP_BAN_MARKERS`` **之前**先判
    ``mailbox_side_no_code``；顺序反了，带能力后缀的邮箱侧超时会被重新读成
    「出口被封」，每个 wave 白停 60s（就是 P0-2 要修的那个坑）。
    """
    from sms_tool.error_classification import classify_error

    combined = f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}:{OTP_NO_RESEND_MARKER}"
    assert _is_otp_ban_signal(_fail(combined)) is False
    # 且它仍然落 ``mailbox`` 类 —— 否则批处理不会换出口重试，失败原因也进不了账本。
    assert classify_error(combined) == "mailbox"
    assert "mailbox" in BATCH_RETRY_CLASSES


# --------------------------------------------------------------------------
# 3b) 整轮一致性：混合结局证伪出口封禁
# --------------------------------------------------------------------------

def test_a_mixed_wave_is_never_an_ip_ban():
    """每个账号钉在池里各自的出口上（``account_proxy_index = i % len(pool)``）。

    所以一轮里只要有一个账号拿到了码，池子/目标/发码链路就都是通的 —— 剩下的
    失败是账号级的。实测批次 25288 的 wave 2（3 超时 + 1 拿到码）与 wave 3
    （2 超时 + 2 拿到码）就是这样被误判成封禁、白停了 60s×2。
    """
    stuck = _fail(f"email_otp_poll_timeout:{OTP_UNDISPATCHED_MARKER}")
    wave = [dict(stuck), dict(stuck), dict(stuck), {"success": True}]
    assert _detect_ip_ban(wave, threshold=2) is False

    # 同一轮里换成一个「非 OTP 侧」的失败，结论不变。
    wave = [dict(stuck), dict(stuck), _fail("existing_account_user_already_exists", "account"), dict(stuck)]
    assert _detect_ip_ban(wave, threshold=2) is False


def test_a_unanimous_wave_is_an_ip_ban():
    stuck = _fail(f"email_otp_poll_timeout:{OTP_UNDISPATCHED_MARKER}")
    assert _detect_ip_ban([dict(stuck)] * 4, threshold=2) is True


def test_unanimity_does_not_replace_the_threshold():
    """wave_size=1 时「整轮一致」是白送的 —— 阈值必须仍然拦得住。"""
    stuck = _fail(f"email_otp_poll_timeout:{OTP_UNDISPATCHED_MARKER}")
    assert _detect_ip_ban([dict(stuck)], threshold=2) is False


def test_the_suffix_decision_is_load_bearing_in_a_real_wave(no_sleep):
    """端到端：邮箱侧超时不该触发 60s 暂停；整轮一致的派发侧失败必须触发。

    只测 ``_is_otp_ban_signal`` 不够 —— 它被 ``_detect_ip_ban`` 包一层，
    真正花钱的是 wave 之间的 ``ban_pause_seconds``。
    """
    from sms_tool.registration_pulse import PulseConfig, run_pulse_batch

    def run_wave(run_one, *, count, wave_size):
        run_pulse_batch(
            count,
            run_one_fn=run_one,
            workers=4,
            pulse_config=PulseConfig(
                enabled=True, wave_size=wave_size, wave_delay_seconds=3,
                ban_threshold=2, ban_pause_seconds=30, canary_enabled=False,
            ),
        )

    # (a) 邮箱侧超时：不是派发侧失败 ⇒ 只有 wave gap 3s。
    run_wave(
        lambda idx: (idx, _fail(f"email_otp_poll_timeout:{OTP_MAILBOX_SIDE_MARKER}")),
        count=4, wave_size=2,
    )
    assert abs(sum(no_sleep) - 3) < 0.01

    # (b) 整轮一致的服务端挂起：后续降为 canary，连续失败会再次冷却。
    no_sleep.clear()
    run_wave(
        lambda idx: (idx, _fail(f"email_otp_poll_timeout:{OTP_UNDISPATCHED_MARKER}")),
        count=4, wave_size=2,
    )
    assert abs(sum(no_sleep) - 66) < 0.01

    # (c) 线上真实形状（批次 25288 wave 2）：一轮 4 个账号里 2 个挂起、2 个拿到码。
    #     失败数 = 2 已达阈值，但出口被证明是通的 ⇒ 不暂停，只留 wave gap。
    #     count 必须 > wave_size，否则最后一轮没有「下一轮」，暂停和 gap 都不会
    #     发生 —— 那样这条断言在变异下照样通过，等于没测。
    no_sleep.clear()
    run_wave(
        lambda idx: (idx, _fail(f"email_otp_poll_timeout:{OTP_UNDISPATCHED_MARKER}")
                     if idx % 2 == 0 else {"success": True}),
        count=8, wave_size=4,
    )
    assert abs(sum(no_sleep) - 3) < 0.01


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """记录 sleep 而不真的等 —— 与 ``test_registration_pulse`` 同款。"""
    from sms_tool import registration_pulse as pulse_module

    calls: list[float] = []
    monkeypatch.setattr(pulse_module.time, "sleep", calls.append)
    return calls
