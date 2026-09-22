"""Failure vocabulary registry (失败词汇单一注册表).

2026-09-12 扫描发现"新增一种失败"要改 3-5 个文件：分类标记散在
``error_classification``，操作建议在 ``registration_policy``，OTP 封禁信号在
``registration_pulse``，批处理类集合在 ``batch_runner``——彼此靠子串字面量
保持一致。本模块把它们收拢为**一份有序数据**：

* ``FAILURE_CLASSES``：分类的优先序即元组顺序（cancelled 最先、auth_state
  最后），``error_classification.classify_error`` 按此顺序匹配；
* ``TERMINAL_ERROR_MARKERS``：硬停标记（跨类，见 ``is_terminal_registration_error``）；
* ``ADVICE``：操作者建议，按 code 是否出现在错误文本中匹配；
* ``OTP_BAN_MARKERS``：OTP 投递被 IP 级封禁的签名（pulse 调度用，回落判据）；
* ``OTP_UNDISPATCHED_MARKER`` / ``OTP_MAILBOX_SIDE_MARKER``：OTP 超时的两个互斥
  **根因**后缀，把「服务端没派发」（派发侧）与「邮箱侧没有码」（邮箱侧）分开；
* ``OTP_NO_RESEND_MARKER``：**能力**后缀，标出「该渠道没有重发能力」，与上面
  第二个后缀**组合**出现（不是第三个根因）；
* ``BATCH_RETRY_CLASSES`` / ``BATCH_DROPPED_CLASSES``：批处理对类别的
  重试/掉号语义。

依赖无关：``error_classification``（被 http_client 引用）必须保持轻导入。
历史拼写与子串格式是**线上数据**（session/progress 里已存在的文本），逐字
保留、不要"顺手规范化"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FailureClass:
    """One failure class: its markers, precedence, and batch semantics."""

    code: str
    markers: tuple[str, ...]
    attempt_retryable: bool = False
    # Preserve the candidate for a later batch after its guard disposition.
    retain_for_future_batch: bool = False
    # batch_runner: a failure of this class marks the account dropped.
    batch_dropped: bool = False

    @property
    def retryable(self) -> bool:
        """Compatibility alias for same-account immediate retry."""
        return self.attempt_retryable

    @property
    def batch_retry(self) -> bool:
        """Compatibility alias; this never promises an immediate retry."""
        return self.retain_for_future_batch


FAILURE_CLASSES: tuple[FailureClass, ...] = (
    FailureClass("cancelled", (
        "registration_cancelled",
        "cancelled_by_user",
    )),
    FailureClass("internal", (
        "registration_internal_error",
        "nameerror",
        "attributeerror",
        "typeerror",
        "keyerror",
        "importerror",
        "indexerror",
        "unboundlocalerror",
        "notimplementederror",
        "recursionerror",
        " is not defined",
    )),
    FailureClass("configuration", (
        "unsupported_registration_driver",
        "missing_dependency",
        "invalid_configuration",
        "configuration_error",
    )),
    FailureClass("account", (
        "account_deactivated",
        "account deactivated",
        "account has been deactivated",
        "deleted or deactivated",
        "registration_disallowed",
        "invalid_grant",
        "authenticationfailed",
        "invalid credentials",
        "wrong_email_otp_code",
        "password_verify_failed",
        "phone_recently_used",
        "unsupported_phone_number",
        "fraud_guard",
        "token_invalidated",
        # Added 2026-09-14: ``create_account`` answers this when the mailbox is
        # already a registered address.  It only reached the classifier after
        # ``registration_outcome`` stopped hiding it behind the re-login
        # fallback's OTP symptom -- before that every occurrence was reported as
        # ``existing_login_otp_send_failed:429``, which classifies ``unknown``.
        "user_already_exists",
        # Added 2026-09-16 17:3x（P1）: the **earliest** shape of the same verdict.
        # ``login_or_signup`` lands on ``/log-in/password`` and ``user/register``
        # answers 400 ``invalid_auth_step``.  It belongs in ``account`` for the
        # same reason ``user_already_exists`` does: the address is used, no
        # amount of retrying frees it, and every retry re-walks the whole
        # handshake to rediscover the same fact.
        #
        # 🔴 Do **not** merge it with ``password_step_unconfirmed``: that one is
        # ``auth_state`` (retryable) precisely because nothing was consumed.
        # This one means the address **is** consumed, so ``batch_dropped`` is the
        # correct response and blacklisting is not a false positive.
        "signup_routed_to_login",
    ), batch_dropped=True),
    FailureClass("mailbox", (
        "mailbox_auth_invalid",
        "mailbox_endpoint_unavailable",
        "remail_api_auth_invalid",
        "outlook otp timeout",
        "email_otp_poll_timeout",
        # Added 2026-09-16（P0-1 判据 B）: the auth transaction still carried
        # ``passwordless_email_otp_send_pending`` after the send step, so the
        # server never completed the dispatch and **no code can arrive**.  The
        # run aborts before the mailbox poll instead of burning its full budget.
        # It sits in ``mailbox`` because that class *is* the OTP-delivery family.
        # Immediate retry and future-batch retention are separate decisions;
        # the exact marker escalates through the retry guard. The
        # egress-vs-mailbox distinction is carried by
        # ``OTP_UNDISPATCHED_MARKER``, which ``registration_pulse`` reads.
        "email_otp_send_stuck",
        "mailbox otp timeout",
        "mailbox_transport_unavailable",
        "relogin_mailbox_transport_failed",
        "remail poll transport",
    ), retain_for_future_batch=True),
    FailureClass("rate_limit", (
        "rate_limit_exceeded",
        "registration_rate_limited",
        "registration_rate_limit_circuit_open",
        "too many requests",
        "http_429",
    ), retain_for_future_batch=True),
    FailureClass("network", (
        "tls",
        "ssl",
        "sslerror",
        "eof occurred",
        "connection",
        "connect error",
        "timeout",
        "timed out",
        "proxy",
        "socks",
        "dns",
        "name resolution",
        "winerror 10060",
        "curl: (35)",
        "curl: (28)",
        "curl: (6)",
        "curl: (7)",
        # Added 2026-09-18（最新一轮协议注册诊断）：补齐 transport curl 码。
        #
        # 🔴 ``CURL_TRANSPORT_MARKERS`` 是从本清单**派生**的
        # （``error_classification.py``：``m.startswith("curl:")``），而
        # ``internal`` 的降级判据是 ``internal_match and not curl_match`` ——
        # 所以**漏登记一个码 = 该码永远无法**把
        # ``registration_internal_error:<Type>:…`` 从 ``internal``（终态）
        # 降级成 ``network``（可重试）。
        #
        # 实测批次 29896（2026-09-18 20:40）：
        # ``registration_internal_error:RuntimeError:…curl: (56) Proxy CONNECT aborted``
        # 被判成 ``internal`` ⇒ **不重试、不进重试守卫、不触发 pulse 熔断**，
        # 两个 run 被静默吞掉（日志里查得到、守卫里查不到记录）。
        # 同批 ``curl: (35)`` 正常归 ``network``，证明缺的就是登记项本身。
        # → ``docs/audits/scan-2026-09-18-latest-protocol-batch-diagnosis.md``
        #
        # (52) Empty reply from server / (18) Partial file 与既有四项同属
        # 传输中途失败，一并登记，避免下次再从同一个洞里漏。
        "curl: (52)",
        "curl: (56)",
        "curl: (18)",
        "remote disconnected",
        "connection reset",
        "connection aborted",
        "session_circuit_open",
        "max retries exceeded",
        "/sentinel/req",
        "sentinel quickjs",
        "sentinel_extract_failed",
        # Added 2026-09-18（P1-b）: Sentinel 发放路径报"给不出 token"的三种说法。
        # 三个串此前**都没进注册表** ⇒ 全判 ``unknown`` ⇒ 而 ``unknown`` 在重试
        # 守卫眼里等同**终态** ⇒ 09-17 那批 42 个账号
        # （``sentinel_legacy_incomplete:oauth_create_account``）**从未被重试**，
        # 在"被误报成渠道故障"之上又叠了一层"直接丢号"。
        # → ``docs/audits/scan-2026-09-18-sentinel-runner-hash-mismatch.md``
        #
        # 🔴 为什么是 ``network`` 而不是 ``account``：**地址没有被消耗**。
        # token 根本没发出来，``create_account`` 从未带着可用载荷被发出，什么都没创建。
        # ``account`` 带 ``batch_dropped=True``，会把一个完全可注册的地址拉黑 ——
        # 与 ``user_already_exists`` / ``password_step_unconfirmed`` 分开的同一个理由。
        #
        # 🔴 为什么不是终态：底层条件可重试。runner 可以瞬时失败；而目前已知的
        # 唯一本地成因（随包资产损坏）修好资产重跑即可 ——
        # ``retain_for_future_batch`` 正是那 42 个地址当时**需要却没拿到**的处置。
        #
        # ``sentinel_issue_failed`` 与 ``sentinel_fallback_incomplete`` 会由
        # ``_root_reason`` 追加**根因**（形如
        # ``sentinel_fallback_incomplete:<flow>:SentinelBundleError(<原因>)``），
        # 所以标记取**前缀**，后缀只作诊断。
        "sentinel_legacy_incomplete",
        "sentinel_issue_failed",
        "sentinel_fallback_incomplete",
        "cloudflare",
        "just a moment",
    ), attempt_retryable=True, retain_for_future_batch=True),
    FailureClass("auth_state", (
        "browser_email_field_not_editable",
        "invalid_auth_step",
        "invalid_state",
        "sign-in session is no longer valid",
        "signup_auth_state",
        "browser_registration_state_unknown",
        "browser_email_verification_stuck",
        "browser_auth_state",
        # Added 2026-09-13 from the live failure log: these four were answered
        # as ``unknown`` (or, for the last one, ``network``) and therefore read
        # as *terminal*, so the retry guard never accumulated a cooldown for
        # them. Measured over 09-08..09-13: ``unknown`` covered 18 of 179
        # failures, all of them ``missing_auth_session_access_token``.
        #
        # ``missing_auth_session_access_token`` is the collapsed outcome when
        # an already-registered address cannot be logged back in;
        # ``registration_outcome._registration_outcome`` already documents that
        # it hides a retryable ``invalid_state`` behind a name that suggests a
        # code defect. ``browser_passwordless_otp_state_unknown`` and
        # ``browser_email_value_mismatch`` are raised by
        # ``browser_flow/form_steps.py`` when the page is in an unexpected
        # state. ``browser_profile_submit_timeout`` was already classified
        # ``auth_state`` by the browser lane (``session._browser_failure_class``
        # matches ``profile_``) and is listed here so the shared classifier
        # agrees instead of being stolen by the bare ``timeout`` marker.
        "missing_auth_session_access_token",
        "auth_session_recovery_",
        "browser_passwordless_otp_state_unknown",
        "browser_email_value_mismatch",
        "browser_profile_submit_timeout",
        # Added 2026-09-16 (拍板：密码优先模式**失败即 abort**，不回落 passwordless).
        # ``user/register`` answered ``invalid_auth_step`` -- the transaction was
        # not at the password step, so the server never accepted our password.
        # Continuing would reach ``create_account`` and produce an account with
        # **no password**, which is precisely what password-first registration
        # exists to prevent.  The run stops before spending an email OTP.
        #
        # ``auth_state`` because the address is *not* consumed: nothing was
        # created, so the current account may retry and a later batch may also
        # reconsider it. It must not land in ``account``,
        # whose ``batch_dropped`` would blacklist a perfectly registrable
        # address.
        #
        # 🔴 Note the marker is the **prefix**, not the whole error string.  The
        # error is emitted as ``password_step_unconfirmed:<reason>`` and today the
        # only reason is ``invalid_auth_step`` -- which is *itself* a marker two
        # lines up, so this entry changes nothing for that one string.  It is here
        # so the whole family stays ``auth_state`` when the suffix varies; the
        # suffix is diagnostic, the prefix is the contract.  Pinned by
        # ``tests/test_user_register_response_contract.py``.
        "password_step_unconfirmed",
    ), attempt_retryable=True, retain_for_future_batch=True),
    # Added 2026-09-17 (UPI 提链改造 P3-3): UPI 提链专有的失败说法原先**一个都没
    # 进注册表**，于是 ``classify_error`` 把它们全判成 ``unknown``。实测确认：
    #   generic_decline / approve blocked / checkout_not_active_session /
    #   submission_attempt_failed / stripe submission failed /
    #   checkout_approval_payment_failure  ->  全部 ``unknown``
    #
    # 后果不只是「分类难看」：``unknown`` 在重试守卫眼里等同**终态**，
    # 会让 ``upi_provider_declined``（真终态，换代理无用）与
    # ``upi_redirect_timeout``（可重试）拿到同样的处置。
    #
    # 🔴 分类边界（决定放哪个类）：
    #   - ``generic_decline`` / ``approve blocked`` 是上游**风控拒绝**，
    #     换账号或换代理都不改变结果 ⇒ 终态。放 ``auth_state`` 而不是
    #     ``account``：被拒的是**这次 checkout**，不是这个邮箱账号本身，
    #     归 ``account`` 会连带把可注册地址拉黑。
    #   - ``redirect url resolution timeout`` 是**超时**，属可重试；
    #     它自带 "timeout" 子串，会被下面的 ``network`` 类抢走，所以
    #     这里显式登记，保证 ``auth_state`` 的优先序（在本元组中靠后，
    #     但 ``upi_redirect_timeout`` 的完整串比 network 的裸 "timeout"
    #     更specific，靠 classify_error 的「先匹配到就算」顺序解决）。
    #   - ``checkout_not_active_session`` 说明 checkout 已失效 ⇒ 需重开。
    FailureClass("upi_payment", (
        "generic_decline",
        "approve blocked",
        "attempts blocked",
        "upi_provider_declined",
        "checkout_not_active_session",
        "upi_checkout_not_active",
        "submission_attempt_failed",
        "stripe submission failed",
        "checkout_approval_payment_failure",
        "stripe 风控拒绝",
        # 🔴 ``upi_redirect_timeout`` 必须在这里显式登记。它自己只含裸词
        # "timeout"，会被 ``network`` 的 GENERIC_TRANSPORT_MARKERS 机制
        # 降级成「弱证据」；若不登记，最终会落到 ``network``，把
        # 「checkout 提链超时」误报成「网络故障」。
        "upi_redirect_timeout",
        # ``upi_qr_failed`` 是 ``_upi_classify_failure`` 的兜底 code，语义是
        # 「提链跑完了但没拿到可用物」。它**不是** unknown：unknown 在重试
        # 守卫眼里等同终态，而这个明显还可重试。
        "upi_qr_failed",
        "upi_checkout_unauthorized",
    ), retain_for_future_batch=True),
)

# classify_error 找不到任何标记时的类别。
UNKNOWN_CLASS = "unknown"

# 硬停：重试无法改变结果（与类别正交——http_429 属 rate_limit、
# mailbox_auth_invalid 属 mailbox，但都是硬停）。
TERMINAL_ERROR_MARKERS: tuple[str, ...] = (
    "manual_challenge_required",
    "browser_proxy_blocked",
    "mailbox_auth_invalid",
    "mailbox_endpoint_unavailable",
    "remail_api_auth_invalid",
    "invalid_grant",
    "registration_cancelled",
    "session_circuit_open",
    "registration_rate_limit",
    "http_429",
    "stage_budget_exceeded",
    "auth_session_recovery_exhausted",
    "auth_session_recovery_expired",
    "auth_session_recovery_context_missing",
)

# 操作者建议：code 出现在错误文本中即命中（registration_policy 消费）。
ADVICE: dict[str, str] = {
    "manual_challenge_required": "需要人工验证，已停止自动重试。",
    "browser_proxy_blocked": "目标拒绝了当前连接，停止重试并检查服务访问政策。",
    "browser_email_field_missing": "未找到邮箱框，请检查页面结构和登录状态。",
    "browser_email_field_not_editable": "邮箱框尚不可编辑，请检查页面加载或人工验证状态。",
    "browser_registration_state_unknown": "页面状态未知，请保留脱敏诊断并人工检查。",
    "browser_email_verification_stuck": "邮箱验证后的页面未跳转，请检查验证状态，勿重复创建账号。",
    "browser_unexpected_identity_provider": "跳转到了意外身份提供方，已停止自动化。",
    "mailbox_auth_invalid": "邮箱凭据已隔离，请修复邮箱池后再试。",
    "mailbox_endpoint_unavailable": "邮箱端点不可用，暂停五分钟后可重新检查。",
    "remail_api_auth_invalid": "邮箱服务认证失败，请检查服务配置。",
    "registration_retry_cooldown": "该邮箱处于冷却期，请等待后重试。",
    "stage_budget_exceeded": "阶段超过预算，请检查耗时和阻塞点。",
}

# OTP 投递被 IP 级封禁的签名（registration_pulse 消费，决定换池重排）。
#
# ⚠ 这组是**子串**匹配，而 ``otp_poll_timeout`` 恰好命中
# ``email_otp_poll_timeout`` —— 那是我们自己的邮箱轮询超时，不是服务端封禁。
# 分辨化见下面的 ``OTP_UNDISPATCHED_MARKER`` / ``OTP_MAILBOX_SIDE_MARKER``；
# 本组现在只是「拿不到证据时的回落判据」。
OTP_BAN_MARKERS: tuple[str, ...] = (
    "otp_not_received",
    "otp_timeout",
    "email_otp_timeout",
    "mailbox_otp_not_received",
    "no_otp",
    "otp_poll_timeout",
)

# OTP 超时的两个**互斥根因**后缀 + 一个可选的**能力**后缀，由
# ``registration_handlers._otp_timeout_error`` 生成、``registration_pulse`` 消费。
#
# 2026-09-16 实测（批次 25288，21 账号）：6 个 ``email_otp_poll_timeout`` 的 run
# 在 ``after_otp_send`` 的事务键里都还留着
# ``passwordless_email_otp_send_pending``（服务端**没有完成**派发），而同批所有
# 拿到码的 run 从来没有这个键。旧判据靠上面的 ``otp_poll_timeout`` 子串命中，
# 把「邮箱侧没收到」也计成出口被封 ⇒ 每个 wave 白停 60s，且换出口无效。
#
#   * ``otp_send_stuck``     —— 服务端没完成派发（**派发侧**；出口只是可能原因之一）。
#   * ``mailbox_side_no_code`` —— 服务端侧发码事务已走完（dump 里没有挂起键），
#     但整个轮询窗口内**邮箱侧没有产出可用验证码**（**邮箱侧**，与出口无关）。
#
# 🔴 **2026-09-16 改名**：本后缀原名 ``code_not_delivered``，**名字在撒谎**。
# 它唯一的前提是 ``otp_dispatch_verdict() == "dispatched"``，而那是**服务端**
# 的判定（「发码事务没有挂起键」）—— 它推不出「邮件投递到了邮箱」，更推不出
# 「我们读得出来」。实测反例（批次 25116）：渠道 `ima3.52dfd.top` 返回 72 字节
# JSON，``mailbox_icloud_url`` 的 HTML 解析器**结构性读不到任何邮件**，10/10
# 失败仍被标成 ``code_not_delivered`` ⇒ 真实语义是「**我们没读出来**」，不是
# 「邮箱没收到」。改名后只陈述已知事实：服务端走完了，邮箱侧没有码。
# ⚠️ 它**也不**能读成「邮件一定到了」——「没收到」与「读不出」这两种根因
# 用当前数据**区分不了**（轮询器只回码/不回码，不回观测元数据）。
#
# ⚠ ``no_resend_for_channel``（第三个后缀）**不是**根因，是**渠道能力**：
# 该 provider 不在 ``otp_strategy.otp_resend_eligible()`` 的名单里 ⇒ 整段
# ``otp_timeout`` 只发过一次邮件，**没有第二次机会**。它只在已经确定是邮箱侧
# （``mailbox_side_no_code``）时才追加，语义是「邮箱侧没有码，而且我们连重发
# 这个补救手段都没有」。⇒ 它与第二个后缀**组合**出现，不是三选一。
#
# ⚠ 前两个后缀都只是**单账号**证据。够不够格叫「IP 封禁」由
# ``registration_pulse._detect_ip_ban`` 的整轮一致性决定 —— 每个账号钉在池里
# 各自的出口上，所以一轮里有账号拿到码就证明出口是通的。
OTP_UNDISPATCHED_MARKER = "otp_send_stuck"
OTP_MAILBOX_SIDE_MARKER = "mailbox_side_no_code"
#: ``email_otp_poll_timeout`` 的**能力**后缀：这次超时的渠道没有重发能力。
#: ⚠️ 与 ``OTP_MAILBOX_SIDE_MARKER`` 组合出现 ⇒ ``registration_pulse``
#: ``_is_otp_ban_signal`` 仍然先在 ``OTP_MAILBOX_SIDE_MARKER`` 上短路，
#: 不会因为它变成封禁信号。
OTP_NO_RESEND_MARKER = "no_resend_for_channel"

# 服务端在 ``error.code`` 里给出的「这个地址是用**非密码**方式注册的」判决。
#
# 2026-09-16 实测：同一个判决从**两个不同端点**回来，措辞一致 ——
#
#   * ``POST /api/accounts/email-otp/validate`` → 400（4 次 / 4 个 run）
#   * ``POST /api/accounts/create_account``    → 400（1 次，被报成
#     ``create_account_failed:identity_provider_mismatch``）
#
#   ``{"error": {"code": "identity_provider_mismatch",
#                "message": "You tried signing in as \"...\" using a password,
#                            which is not the authentication method you used
#                            during sign up. Try again using the authentication
#                            method you used during sign up."}}``
#
# 语义：服务端记得这个地址**注册过**，而且注册时用的不是密码 ⇒ 该账号是
# passwordless ⇒ ``create_account`` 对它永远不会成功。
#
# 🔴 判据必须落在**结构化的 ``code``** 上，不要匹配消息尾巴：
# ``registration_retry_guard`` 把 ``last_error`` 截到 160 字符，而
# ``email_otp_validate:{...}`` 的前缀就有 110+ 字符 ⇒ 消息尾巴会被**截断掉**
# （实测：``create_account_failed:identity_provider_mismatch: …`` 尾巴还在，
# ``email_otp_validate:{"endpoint": …}`` 尾巴已被切掉）。
PASSWORDLESS_SIGNUP_CODE = "identity_provider_mismatch"
#: 只有在拿不到 ``code`` 时才回落到消息匹配（老响应/代理改写过 body 的情形）。
PASSWORDLESS_SIGNUP_MESSAGE_MARKER = "not the authentication method you used during sign up"


def is_passwordless_signup_mismatch(value: Any) -> bool:
    """``value`` 是否在说「这个地址是用非密码方式注册的」。

    接受 ``{"error": {"code": ..., "message": ...}}`` 形状的响应体，也接受
    已经拼好的错误串（此时只能做消息匹配）。**只看结构化的 ``code``**，
    拿不到才回落到消息；两者都没有就答 False（不猜）。
    """
    if isinstance(value, Mapping):
        error = value.get("error")
        if isinstance(error, Mapping):
            if str(error.get("code") or "").strip() == PASSWORDLESS_SIGNUP_CODE:
                return True
            return PASSWORDLESS_SIGNUP_MESSAGE_MARKER in str(error.get("message") or "").casefold()
        return False
    return PASSWORDLESS_SIGNUP_MESSAGE_MARKER in str(value or "").casefold()


def failure_class(code: str) -> FailureClass:
    """Return the ``FailureClass`` for ``code``; raises KeyError when absent."""
    for cls in FAILURE_CLASSES:
        if cls.code == code:
            return cls
    raise KeyError(code)


# Future-batch eligibility is not same-account immediate retry. Keep the old
# exported set as a compatibility view while new code uses the precise name.
FUTURE_BATCH_CLASSES = frozenset(
    cls.code for cls in FAILURE_CLASSES if cls.retain_for_future_batch
)
BATCH_RETRY_CLASSES = FUTURE_BATCH_CLASSES
BATCH_DROPPED_CLASSES = frozenset(cls.code for cls in FAILURE_CLASSES if cls.batch_dropped)


__all__ = [
    "FailureClass",
    "FAILURE_CLASSES",
    "UNKNOWN_CLASS",
    "TERMINAL_ERROR_MARKERS",
    "ADVICE",
    "OTP_BAN_MARKERS",
    "OTP_UNDISPATCHED_MARKER",
    "OTP_MAILBOX_SIDE_MARKER",
    "OTP_NO_RESEND_MARKER",
    "PASSWORDLESS_SIGNUP_CODE",
    "PASSWORDLESS_SIGNUP_MESSAGE_MARKER",
    "is_passwordless_signup_mismatch",
    "failure_class",
    "FUTURE_BATCH_CLASSES",
    "BATCH_RETRY_CLASSES",
    "BATCH_DROPPED_CLASSES",
]
