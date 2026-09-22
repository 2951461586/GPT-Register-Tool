"""OTP-pending 隔离的 TTL：到期必须释放，且**重新计时**。

背景（2026-09-18，最新一轮协议注册诊断）：

``RegistrationRetryGuard`` 原先用 ``quarantined: true`` 配合 ``cooldown_until: 0``
表示「永久隔离」。那只在「服务端对这个**地址**永久拒绝派发」的假设下成立，而
``passwordless_email_otp_send_pending`` 是**服务端事务状态** —— 它完全可能只反映
一次灰度 / 限流窗口。实测批次 29896 被隔离的地址，首次 stuck 与本次相隔数小时、
换了出口，是**地址属性**没错，但当时**没有任何机制让它们回来**。

本文件钉住四条不变量：

1. 隔离写入一个 ``quarantine_until`` 截止时刻；
2. 到期后 ``check`` / ``quarantined_emails`` / ``blocked_email_states`` 三处**同时**
   释放（四处读点共用一个 owner，不许各自漂移）；
3. 🔴 到期后的**第一次** stuck 只拿到冷却，**不得**立刻重新隔离 —— 否则
   ``otp_pending_count`` 从旧值继续累加，TTL 等于白加；
4. 加 TTL **之前**写下的记录（没有 ``quarantine_until``）也要能到期，否则需要
   一次手工清理。

→ ``docs/audits/scan-2026-09-18-latest-protocol-batch-diagnosis.md``
"""
from __future__ import annotations

import json
import time

from sms_tool.registration_retry_guard import RegistrationRetryGuard

TTL = 3600


def _guard(tmp_path, **kwargs):
    kwargs.setdefault("otp_pending_quarantine_seconds", TTL)
    return RegistrationRetryGuard({}, path=tmp_path / "guard.json", **kwargs)


def _stuck(guard, email):
    """把 ``email`` 推到「第二次 OTP 派发挂起」= 隔离。"""
    guard.record(email, failure_class="mailbox", error="email_otp_send_stuck")


def _quarantine(guard, email):
    _stuck(guard, email)
    _stuck(guard, email)


def _rewind(tmp_path, email, *, seconds):
    """把某行的两个时间戳一起往回拨，模拟 TTL 到期（不 sleep）。"""
    path = tmp_path / "guard.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    row = data[email]
    for field in ("last_attempt_at", "quarantine_until"):
        if row.get(field):
            row[field] = int(row[field]) - seconds
    path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------
# 写入侧：隔离必须带截止时刻
# --------------------------------------------------------------------------

def test_quarantine_records_a_ttl_deadline(tmp_path):
    guard = _guard(tmp_path)
    _quarantine(guard, "a@example.com")

    row = guard._read()["a@example.com"]
    assert row["quarantined"] is True
    assert row["quarantine_reason"] == "email_otp_send_stuck"
    assert row["quarantine_until"] == row["last_attempt_at"] + TTL
    # ``cooldown_until: 0`` 现在只表示「不走冷却那条路」，不再等于「永久」。
    assert row["cooldown_until"] == 0


def test_the_first_observation_is_a_cooldown_not_a_quarantine(tmp_path):
    guard = _guard(tmp_path)
    _stuck(guard, "a@example.com")

    row = guard._read()["a@example.com"]
    assert row["quarantined"] is False
    assert row["quarantine_until"] == 0
    assert row["cooldown_until"] == row["last_attempt_at"] + guard.cooldown_seconds


# --------------------------------------------------------------------------
# 读取侧：三处必须同时释放
# --------------------------------------------------------------------------

def test_a_quarantine_inside_its_ttl_still_defers(tmp_path):
    guard = _guard(tmp_path)
    _quarantine(guard, "a@example.com")

    state = guard.check("a@example.com")
    assert state["quarantined"] is True
    assert state["deferred"] is True
    assert guard.quarantined_emails() == {"a@example.com"}
    assert guard.blocked_email_states()["a@example.com"] == "otp_pending_quarantine"


def test_an_expired_quarantine_is_released_by_the_clock(tmp_path):
    guard = _guard(tmp_path)
    _quarantine(guard, "a@example.com")
    _rewind(tmp_path, "a@example.com", seconds=TTL + 1)

    state = guard.check("a@example.com")
    assert state["quarantined"] is False
    assert state["deferred"] is False
    assert state["remaining_seconds"] == 0


def test_expired_quarantine_leaves_the_pool_filters(tmp_path):
    """按池过滤的调用方不需要任何清理步骤。"""
    guard = _guard(tmp_path)
    _quarantine(guard, "a@example.com")
    _quarantine(guard, "b@example.com")
    _rewind(tmp_path, "a@example.com", seconds=TTL + 1)

    assert guard.quarantined_emails() == {"b@example.com"}
    states = guard.blocked_email_states()
    assert "a@example.com" not in states
    assert states["b@example.com"] == "otp_pending_quarantine"


# --------------------------------------------------------------------------
# 🔴 承重用例：到期 = 重新计时，不是永久豁免
# --------------------------------------------------------------------------

def test_expiry_restarts_the_count_instead_of_re_quarantining_at_once(tmp_path):
    """到期后第一次 stuck 必须只拿到冷却。

    如果实现只把 ``quarantined`` 读成 False 而**不清空** ``otp_pending_count``，
    那么下一次 stuck 会把它累加到 3 ⇒ ``3 >= threshold(2)`` ⇒ 立刻又隔离，
    TTL 完全失效。这条断言就是为此存在。
    """
    guard = _guard(tmp_path)
    _quarantine(guard, "a@example.com")
    assert guard.check("a@example.com")["quarantined"] is True

    _rewind(tmp_path, "a@example.com", seconds=TTL + 1)
    assert guard.check("a@example.com")["quarantined"] is False

    _stuck(guard, "a@example.com")
    row = guard._read()["a@example.com"]
    assert row["otp_pending_count"] == 1
    assert row["quarantined"] is False
    assert guard.check("a@example.com")["quarantined"] is False

    # 而且它仍然**会**被重新隔离 —— TTL 是重新计时，不是永久豁免。
    _stuck(guard, "a@example.com")
    assert guard.check("a@example.com")["quarantined"] is True


# --------------------------------------------------------------------------
# 向后兼容与保守兜底
# --------------------------------------------------------------------------

def test_rows_written_before_the_ttl_existed_still_expire(tmp_path):
    """09-18 及更早隔离的记录没有 ``quarantine_until``。

    没有这条回落，那批地址需要一次手工清理才能回来 —— 而「加 TTL」的整个目的
    就是让它们自己回来。
    """
    path = tmp_path / "guard.json"
    now = int(time.time())
    path.write_text(
        json.dumps(
            {
                "old@example.com": {
                    "consecutive": 2,
                    "otp_pending_count": 2,
                    "failure_class": "mailbox",
                    "last_error": "email_otp_send_stuck",
                    "cooldown_until": 0,
                    "quarantined": True,
                    "quarantine_reason": "email_otp_send_stuck",
                    "last_attempt_at": now - TTL - 1,
                }
            }
        ),
        encoding="utf-8",
    )

    guard = _guard(tmp_path)
    assert guard.check("old@example.com")["quarantined"] is False
    assert "old@example.com" not in guard.quarantined_emails()


def test_a_row_with_neither_deadline_stays_quarantined(tmp_path):
    """两个时间字段都读不到 ⇒ 保持隔离。

    保守方向是刻意的：误放行一个地址要烧一个邮箱 OTP，多关一会儿没有代价。
    """
    path = tmp_path / "guard.json"
    path.write_text(
        json.dumps(
            {
                "unknown@example.com": {
                    "otp_pending_count": 2,
                    "quarantined": True,
                    "quarantine_reason": "email_otp_send_stuck",
                    "cooldown_until": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    guard = _guard(tmp_path)
    assert guard.check("unknown@example.com")["quarantined"] is True


def test_a_dead_end_is_unaffected_by_the_quarantine_ttl(tmp_path):
    """``user_already_exists`` 是永久死路，不能被 TTL 顺手放出来。"""
    guard = _guard(tmp_path)
    guard.mark_dead_end("dead@example.com", reason="user_already_exists")

    assert guard.check("dead@example.com")["dead_end"] is True
    assert guard.check("dead@example.com")["deferred"] is True
    assert guard.blocked_email_states()["dead@example.com"] == "dead_end"


def test_a_success_still_clears_the_row(tmp_path):
    guard = _guard(tmp_path)
    _quarantine(guard, "a@example.com")
    guard.record("a@example.com", success=True)

    assert guard.check("a@example.com")["quarantined"] is False
    assert guard.quarantined_emails() == set()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def test_ttl_comes_from_the_retry_policy_and_is_floored(tmp_path):
    configured = RegistrationRetryGuard(
        {"registration": {"retry_policy": {"otp_pending_quarantine_seconds": 7200}}},
        path=tmp_path / "a.json",
    )
    assert configured.otp_pending_quarantine_seconds == 7200

    # 下限 60s：写 0 会让「到期时刻」与「写入时刻」重合，而
    # ``_quarantine_active`` 用 ``until <= 0`` 表示「未设 TTL」⇒ 语义打架。
    floored = RegistrationRetryGuard(
        {"registration": {"retry_policy": {"otp_pending_quarantine_seconds": 0}}},
        path=tmp_path / "b.json",
    )
    assert floored.otp_pending_quarantine_seconds == 60

    default = RegistrationRetryGuard({}, path=tmp_path / "c.json")
    assert default.otp_pending_quarantine_seconds == 86400
