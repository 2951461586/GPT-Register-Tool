"""Email-OTP stages of the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-01, following the
``registration_finalize`` precedent): the send / wait / validate trio plus the
two diagnostics that decide *why* a code never arrived form one cohesive unit.
They are module-level functions whose first parameter is the workflow instance,
so the class keeps thin delegates and no caller (or test) changes.

The bodies are moved verbatim. They reach the workflow only through the public
seam (``self.r`` / ``self.runtime`` / ``self.config`` and the
``_password_lane_active`` / ``_otp_poll_timeout`` / ``_otp_timeout_error`` /
``_abort`` collaborators), never by importing ``registration_handlers``, so
this module cannot form an import cycle.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .auth_state import otp_dispatch_verdict
from .sanitizer import describe_exception
from .failure_registry import (
    OTP_MAILBOX_SIDE_MARKER,
    OTP_NO_RESEND_MARKER,
)
from .registration_protocol_helpers import _safe_int
from .otp_strategy import otp_resend_eligible


def send_email_otp(self: Any) -> None:
    r = self.r
    s = self.runtime
    # Do NOT snapshot here.  The pre-OTP baseline is taken once in
    # ``_bootstrap``; re-snapshotting at this point would be a race --
    # passwordless sends the OTP during ``auth_flow``, so the code mail can
    # already be visible and would be recorded as "seen", permanently
    # hiding it from ``_latest_email_otp_candidate``.
    continue_url = r._email_otp_send_url(s.reg_data)
    otp_send_started = _safe_int(time.time())
    if not self._password_lane_active():
        # authorize with login_hint sends the first OTP itself. Do not
        # immediately POST resend: that endpoint is rate-limited for this
        # flow and the reference browser path only polls the pre-sent code.
        response = r.SyntheticResponse(
            204,
            {"assumed_pre_sent": True},
            url=s.signup_state.get("url", ""),
        )
    else:
        response = r._follow_continue_url(
            s.session,
            continue_url,
            s.base_headers,
            referer=f"{s.auth_base}/create-account/password",
            label="Email OTP send",
        )
    # 保留 summary，不要只打印后丢弃：``wait_email_otp`` 要用它分辨超时
    # 根因（服务端没派发 vs 邮箱没收到），见 ``_otp_timeout_error``。
    dump = r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_otp_send")
    s.otp_send_dump = dump if isinstance(dump, dict) else {}
    if response is None:
        self._abort("email_otp_send_missing_continue_url")
    status_code = getattr(response, "status_code", 0)
    if status_code not in (200, 202, 204):
        self._abort(f"email_otp_send_failed:{status_code}")
    s.otp_issued_after = otp_send_started
    if s.registration_mode == "passwordless" and r._json_or_raw(response).get("assumed_pre_sent"):
        s.otp_issued_after = max(0, s.auth_flow_started - 5)


def wait_email_otp(self: Any) -> None:
    r = self.r
    s = self.runtime
    email_cfg = self.config.get("email_registration", {})
    s.email_cfg = email_cfg if isinstance(email_cfg, dict) else {}
    # P0-1 判据 B：事务里还挂着 ``passwordless_email_otp_send_pending``
    # ⇒ 服务端**没有完成**派发 ⇒ 这一轮**不可能**拿到码。实测 6/6 超时的
    # run 都停在这个形状上（15 个拿到码的一个都没有），所以直接止损，
    # 不去烧满 300s 轮询。
    #
    # 单独一个错误名而不是给 ``email_otp_poll_timeout`` 加后缀：这条路径
    # **根本没轮询**，说「poll timeout」会让日志撒谎。
    if otp_dispatch_verdict(s.otp_send_dump) == "stuck":
        print("  Email OTP send is still pending on the server; skipping the mailbox poll")
        self._abort("email_otp_send_stuck")
    s.email_code = r.otp_poll.poll(
        s.mailbox,
        subject_keyword=r.otp_poll.subject_keywords,
        timeout=self._otp_poll_timeout(),
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
        self._abort(self._otp_timeout_error())


def _otp_provider(self: Any) -> str:
    """当前邮箱渠道名；拿不到就是空串（空串不在重发名单里 ⇒ 判「无重发」）。"""
    mailbox = getattr(self.runtime, "mailbox", None)
    return str(getattr(mailbox, "provider", "") or "").strip().lower()


def _otp_timeout_error(self: Any) -> str:
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
    if otp_dispatch_verdict(self.runtime.otp_send_dump) == "dispatched":
        suffix = OTP_MAILBOX_SIDE_MARKER
        if not otp_resend_eligible(self._otp_provider()):
            suffix = f"{suffix}:{OTP_NO_RESEND_MARKER}"
        return f"email_otp_poll_timeout:{suffix}"
    return "email_otp_poll_timeout"


def validate_email_otp(self: Any) -> None:
    r = self.r
    s = self.runtime
    otp_ok, s.otp_data = r._validate_email_otp(
        s.session,
        s.auth_base,
        s.base_headers,
        s.email_code,
        sentinel_data=s.sentinel_data,
        use_sentinel=False,
    )
    if not otp_ok and r._is_wrong_email_otp_code(s.otp_data):
        print("  Email OTP was rejected; retrying latest mailbox code once...")
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
                sentinel_data=s.sentinel_data,
                use_sentinel=False,
            )
    if not otp_ok:
        r._fetch_client_auth_session_dump(
            s.session,
            s.auth_base,
            s.base_headers,
            "after_otp_validate_failed",
        )
        self._abort(f"email_otp_validate:{json.dumps(s.otp_data, ensure_ascii=False)[:300]}")
    try:
        r._follow_continue_url(
            s.session,
            s.otp_data.get("continue_url", ""),
            s.base_headers,
            referer=f"{s.auth_base}/verify-email",
            label="Email OTP continue",
        )
    except Exception as exc:
        print(f"  Email OTP continue transport warning: {describe_exception(exc)}")


__all__ = [
    "send_email_otp",
    "wait_email_otp",
    "validate_email_otp",
    "_otp_provider",
    "_otp_timeout_error",
]
