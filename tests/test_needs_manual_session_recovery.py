"""方案 B：把「已创建但无 session 且无密码」的账号从普通失败里分出来。

出处：``docs/audits/plan-2026-09-16-partial-account-protocol-login.md`` §五 方案 B。

为什么值得单独一个字段
----------------------

``registration_state='partial_registered'`` 已经说了「服务端有这个地址」，但它
**没有**说「我们还能不能拿到它的 session」。半注册里有一半是我们握着密码的
（``--password`` 或我们自己的历史落库），那些走密码登录泳道**零 OTP** 就能救回来；
剩下的才真的只能靠人工登录一次。两者混在一起，面板上就是一堵没有区别的错误墙。

本文件钉三件事：

1. 判据本身的真值表 —— 三个事实缺一不可，且**不是** ``password_unknown``。
2. 两条装配路径（``_abort_result`` 的失败装配、``finalize`` 的完成装配）都必须
   带上它，且都由同一个判据函数算出来。
3. 审计表那一侧 —— ``registration_audit.detail_json`` 是**封闭白名单**，
   结果里加了键而白名单没跟上就是**静默丢弃**（09-15 前 ``existing_login`` 在
   4377 行里出现 0 次就是这个原因）。所以有一条防漂移用例：拿**结果 dict 本体**
   去写审计行，再读回来比对。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sms_tool import storage
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_outcome import _failure_result, needs_manual_session_recovery
from sms_tool.registration_runtime import RegistrationRuntimeState

# ---------------------------------------------------------------------------
# 1) 判据本身
# ---------------------------------------------------------------------------

#: (success, access_token, existing_account, existing_account_password_known, expected)
_BUCKET_CASES = [
    (False, "", True, False, True),
    (False, "", True, True, False),
    (False, "", False, False, False),
    (False, "at", True, False, False),
    (True, "at", True, False, False),
    (False, "   ", True, False, True),
]

_BUCKET_IDS = [
    "half-registered-with-no-password-is-the-only-hit",
    "password-held-so-the-protocol-can-recover-it-for-free",
    "server-never-said-the-address-exists",
    "already-has-a-session",
    "succeeded",
    "whitespace-token-counts-as-no-session",
]


@pytest.mark.parametrize(
    "success,access_token,existing_account,password_known,expected",
    _BUCKET_CASES,
    ids=_BUCKET_IDS,
)
def test_the_bucket_needs_all_three_facts(
    success, access_token, existing_account, password_known, expected
):
    """三个事实缺一不可。

    🔴 特别钉住 ``existing_account_password_known`` 这一条：判据**不能**改用
    ``password_unknown``。后者对**每一个** ``user_already_exists`` 都会置位
    （它是落库卫生，见 ``RegistrationAccount``），拿它当「无密码」会把
    「我们握着密码、密码泳道能零 OTP 救回来」的那批地址一起卷进来 ——
    对每一个都误报「需要人工」。
    """
    assert (
        needs_manual_session_recovery(
            success=success,
            access_token=access_token,
            existing_account=existing_account,
            existing_account_password_known=password_known,
        )
        is expected
    )


def test_the_predicate_refuses_positional_arguments():
    """四个布尔量按位置传是等着被写反的，签名必须只收关键字。"""
    with pytest.raises(TypeError):
        needs_manual_session_recovery(False, "", True, False)


def test_a_successful_run_never_asks_for_a_human_even_with_a_stale_flag():
    """纵深防御：``success`` 一票否决，不管别的标志怎么残留。"""
    assert (
        needs_manual_session_recovery(
            success=True,
            access_token="",
            existing_account=True,
            existing_account_password_known=False,
        )
        is False
    )


# ---------------------------------------------------------------------------
# 2) 两条装配路径
# ---------------------------------------------------------------------------

def test_the_failure_assembly_carries_the_bucket():
    result = _failure_result(
        "existing_account_user_already_exists:continue_to_login",
        email="half@example.test",
        existing_account=True,
    )

    assert result["needs_manual_session_recovery"] is True


def test_the_failure_assembly_defaults_to_not_needing_a_human():
    """手机泳道等调用方不传这些事实 ⇒ 不许声称需要人工。

    ``phone_registration`` 有 9 个 ``_failure_result`` 调用点，全都不带
    ``existing_account`` 概念；默认 False 是它们的正确语义。
    """
    result = _failure_result("sms_code_timeout", email="+10000000000")

    assert result["needs_manual_session_recovery"] is False


def test_the_abort_path_hands_the_runtime_facts_to_the_predicate():
    """``_abort_result`` 必须把三个事实**都**递下去 —— 少递一个就是静默假阴性。"""
    w = object.__new__(RegistrationEmailWorkflow)
    w.runtime = RegistrationRuntimeState(username="half@example.test")
    w.runtime.existing_account = True
    w.runtime.existing_account_password_known = False
    w.runtime.access_token = ""
    w.machine = Mock()
    w._operations = SimpleNamespace(_failure_result=Mock(return_value={"error": "x"}))

    w._abort_result("existing_account_user_already_exists")

    kwargs = w._operations._failure_result.call_args.kwargs
    assert kwargs["existing_account"] is True
    assert kwargs["existing_account_password_known"] is False
    assert kwargs["access_token"] == ""


def _finalize_workflow(*, success, access_token, existing_account, password_known):
    """A workflow whose ``finalize`` can actually run.

    Mirrors ``tests/test_account_identity.py``: the operations object is a bare
    ``Mock`` because ``finalize`` only reads summaries off it, but the fingerprint
    and proxy have to be real enough for ``infer_proxy_country``.
    """
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
    config = {"proxy": {"registration": base_proxy, "pool": [base_proxy]}}
    machine = Mock()
    machine.snapshot.return_value = {"state": "completed"}
    operations = Mock()
    operations._sanitize_text.side_effect = lambda value: str(value or "")
    operations._oauth_result_summary.return_value = {}
    operations._timing_summary.return_value = {}
    operations._mailbox_snapshot.return_value = {}
    operations._retain_registration_checkpoint.return_value = True

    workflow = RegistrationEmailWorkflow(machine, config=config, operations=operations)
    workflow.runtime.proxy = base_proxy
    workflow.runtime.username = "half@example.test"
    workflow.runtime.registration_mode = "passwordless"
    workflow.runtime.success = success
    workflow.runtime.access_token = access_token
    workflow.runtime.existing_account = existing_account
    workflow.runtime.existing_account_password_known = password_known
    return workflow


def test_the_completed_assembly_carries_the_bucket_too():
    """半注册是**跑到 finalize** 的（``fetch_auth_session`` 对它不 abort，改问
    登录方式探针），所以完成路径这一侧也必须带 —— 只补 ``_abort_result`` 会漏掉
    实际产出的那批行。"""
    workflow = _finalize_workflow(
        success=False,
        access_token="",
        existing_account=True,
        password_known=False,
    )

    result = workflow.finalize()

    assert result["registration_state"] == "partial_registered"
    assert result["needs_manual_session_recovery"] is True


def test_the_completed_assembly_says_no_when_we_hold_the_password():
    workflow = _finalize_workflow(
        success=False,
        access_token="",
        existing_account=True,
        password_known=True,
    )

    result = workflow.finalize()

    assert result["registration_state"] == "partial_registered"
    assert result["needs_manual_session_recovery"] is False, (
        "握着密码的半注册走密码登录泳道就能救回来，不该进「待人工」桶"
    )


# ---------------------------------------------------------------------------
# 3) 审计表：封闭白名单 + 防漂移
# ---------------------------------------------------------------------------

def _write_and_read_audit(tmp_path, data, *, state):
    db_path = tmp_path / "accounts.sqlite3"
    with patch.object(storage, "database_path", return_value=db_path):
        assert storage.record_registration_audit(data, batch_id="b", state=state)
        conn = storage._connect()
        try:
            row = conn.execute("SELECT state,detail_json FROM registration_audit").fetchone()
        finally:
            conn.close()
    return row


def test_the_audit_row_keeps_the_bucket(tmp_path):
    """``detail_json`` 是**封闭白名单** —— 白名单没跟上就是静默丢弃。

    这正是 09-15 前 ``existing_login`` 在 4377 行里出现 0 次的原因。
    """
    row = _write_and_read_audit(
        tmp_path,
        {
            "email": "half@example.test",
            "success": False,
            "error": "existing_account_user_already_exists:continue_to_login",
            "needs_manual_session_recovery": True,
        },
        state="partial_registered",
    )

    assert row["state"] == "partial_registered"
    assert json.loads(row["detail_json"])["needs_manual_session_recovery"] is True


def test_the_audit_key_is_always_present_so_absent_never_means_false(tmp_path):
    """键永远在。这样「没进桶」和「这行早于该字段」才分得开。"""
    row = _write_and_read_audit(
        tmp_path,
        {"email": "fresh@example.test", "success": False, "error": "create_account_failed"},
        state="failed",
    )

    assert json.loads(row["detail_json"])["needs_manual_session_recovery"] is False


def test_the_bucket_the_result_emits_is_the_bucket_the_audit_stores(tmp_path):
    """防漂移：拿**结果 dict 本体**去写审计行，再读回来比对。

    两处键名只要有一边被改掉（或白名单漏加），这条就红 —— 否则结果侧写着
    ``True``、审计侧永远读不到，面板上什么都看不见。
    """
    result = _failure_result(
        "existing_account_user_already_exists:continue_to_login",
        email="half@example.test",
        existing_account=True,
    )
    assert result["needs_manual_session_recovery"] is True

    row = _write_and_read_audit(tmp_path, result, state="partial_registered")

    assert (
        json.loads(row["detail_json"])["needs_manual_session_recovery"]
        == result["needs_manual_session_recovery"]
    )
