"""Email OTP request strategy for auth.openai.com.

The OTP seam is deliberately deeper than one endpoint call: it owns the
passwordless resend/send ordering, the "pre-sent OTP" fallback, and the JSON
request shape so registration code does not duplicate state-sensitive details.
"""

import json
import logging
import time
from collections.abc import Mapping

from .config import CFG, current_config_data
from .auth_headers import AUTH_IMPERSONATE, auth_impersonate, openai_auth_headers
from .auth_flow import _absolute_url, _invalid_state_auth_response, _json_or_raw
from .http_client import request_with_retry
from .mailbox import _poll_email_otp
from .registration_progress import registration_stage


_LOGGER = logging.getLogger(__name__)


def _otp_poll_log(event: str, provider: str, message: str, **fields: object) -> None:
    """Structured breadcrumb bracketing the OTP wait window.

    The OTP wait used to be a blank gap in the operator log: nothing was
    written until the poll resolved, so "the mail never arrived" and "we never
    looked" were indistinguishable -- exactly the question raised by the
    2026-09-11 triage, where failures all sat at 300-303s.  These records name
    the provider and the budget so the two cases separate.
    """
    _LOGGER.info(
        "Email OTP poll %s provider=%s %s",
        event,
        provider or "unknown",
        message,
        extra={"event": f"email_otp_poll_{event}", "provider": provider, **fields},
    )


class SyntheticResponse:
    def __init__(self, status_code=204, body=None, url=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body, ensure_ascii=False)
        self.url = url
        self.headers = {}

    def json(self):
        return self._body


def otp_fallback_send_enabled():
    cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    value = cfg.get("otp_fallback_send_on_resend_failure", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


#: 允许「两段式轮询 + 重发」的邮箱 provider 默认名单。
#:
#: 2026-09-16 之前这里的门槛是硬编码的 ``provider != "remail"``，于是
#: ``resend_callback`` / ``resend_after_seconds`` 对 gmail / imap / icloud /
#: icloud_url / smailr **全是死参数**（round-2 审计 P1-2）：邮件只是晚到，也
#: 只能烧满整个 ``otp_timeout`` 然后失败 —— 哪怕再等 30 秒就能收到。
#:
#: ``icloud_url`` 进名单的依据（2026-09-16 批次 25116 取证）：它是**转发 URL**
#: 渠道，邮件要先经上游转发才可见，晚到是常态。那一轮 10 个账号、18/18 次轮询
#: 全是 ``matched=False``；而**同一份代码**在 4.5 小时前的批次 25288 上是
#: 6 次命中（7.9–25.4s）⇒ 给它第二次发码机会是有意义的。
#:
#: 🔴 ``remail`` 必须留在名单里 —— 它原本就走这条路，改成名单不能把老行为丢掉。
DEFAULT_OTP_RESEND_PROVIDERS: tuple[str, ...] = ("remail", "icloud_url")


def otp_resend_providers() -> tuple[str, ...]:
    """重发名单，可由 ``email_registration.otp_resend_providers`` 覆盖。

    接受 list 或逗号分隔字符串。**写坏了就回落默认值**（与
    ``registration_pulse._coerce`` 同一条原则：一个笔误不该让整批注册起不来）。
    显式写空（``[]`` / ``""``）也回落默认值 —— 要停用重发请用
    ``remail_otp_resend_after_seconds=0``，那条路径本来就有。
    """
    cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    value = cfg.get("otp_resend_providers")
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return DEFAULT_OTP_RESEND_PROVIDERS
    providers = tuple(str(item).strip().lower() for item in value if str(item).strip())
    return providers or DEFAULT_OTP_RESEND_PROVIDERS


def otp_resend_eligible(provider) -> bool:
    """该邮箱渠道有没有「第二段轮询 + 重发」的能力。

    🔴 这是**渠道级**判据，**不是**「这一轮真的重发过」：重发窗口还会被
    ``remail_otp_resend_after_seconds`` 关掉（见 ``_poll_registration_email_otp``
    的第二条单次分支）。两处共用这一个 owner —— ``registration_handlers``
    也调它，决定失败原因里要不要标「该渠道无重发」。
    """
    return str(provider or "").strip().lower() in otp_resend_providers()


def send_registration_email_otp(session, auth_base, base_headers, current_url="", mode="passwordless"):
    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/email-verification"
    did = str((base_headers or {}).get("oai-device-id") or (base_headers or {}).get("Oai-Device-Id") or "").strip()
    headers = {
        **(base_headers or {}),
        **openai_auth_headers(did, referer=referer, origin=auth_base, accept="*/*"),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if mode == "passwordless":
        endpoints = [("/api/accounts/email-otp/resend", {})]
        if otp_fallback_send_enabled():
            endpoints.extend([
                ("/api/accounts/passwordless/send-otp", {}),
                ("/api/accounts/email-otp/send", {}),
            ])
    else:
        endpoints = [
            ("/api/accounts/email-otp/send", {}),
            ("/api/accounts/email-otp/resend", {}),
        ]
    last = None
    for endpoint, payload in endpoints:
        kwargs = {
            "headers": headers,
            # Follow the profile the rest of this registration already picked.
            # This used to be pinned to AUTH_IMPERSONATE (firefox144) while
            # every other stage used the pooled profile, so a single signup
            # could present two different TLS/UA identities -- exactly the
            # mismatch the reference implementation warns about.  The pool is
            # still Firefox-majority by weight (CF edge 403s on Chrome), so
            # this stays Firefox in the common case; when it does not, the
            # whole signup is consistent instead of split.
            "impersonate": auth_impersonate() or AUTH_IMPERSONATE,
        }
        if payload is not None:
            kwargs["json"] = payload
            kwargs["headers"] = {**headers, "Content-Type": "application/json"}
        response = request_with_retry(
            session,
            "post",
            _absolute_url(auth_base, endpoint),
            label=f"Email OTP {endpoint}",
            **kwargs,
        )
        print(f"  Email OTP {endpoint}: {response.status_code}")
        last = response
        if response.status_code in (200, 202, 204):
            return response
        body = _json_or_raw(response, limit=500)
        print(f"    Response: {json.dumps(body, ensure_ascii=False)[:500]}")
        if mode == "passwordless" and _invalid_state_auth_response(body):
            return response
        if mode == "passwordless" and endpoint.endswith("/resend") and response.status_code in (400, 404, 405):
            if otp_fallback_send_enabled():
                print("    Resend was not accepted; trying opt-in fallback OTP send")
                continue
            print("    Resend was not accepted; preserving current auth state and polling for pre-sent OTP")
            return SyntheticResponse(
                204,
                {"assumed_pre_sent": True, "resend_status": response.status_code, "resend_body": body},
                url=_absolute_url(auth_base, endpoint),
            )
        if response.status_code not in (404, 405):
            return response
    return last


def _poll_registration_email_otp(
    mailbox,
    *,
    subject_keyword,
    timeout,
    issued_after_unix,
    proxy=None,
    excluded_otps=None,
    resend_callback=None,
    resend_after_seconds=None,
    poll_otp_fn=None,
):
    from .registration_cancel import ensure_not_cancelled

    ensure_not_cancelled()
    poll_otp_fn = poll_otp_fn or _poll_email_otp
    total_timeout = max(0, int(timeout or 0))
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    started = time.monotonic()

    def poll(window: int):
        return poll_otp_fn(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=window,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
        )

    def finish(code):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _otp_poll_log(
            "end",
            provider,
            f"matched={bool(code)} elapsed_ms={elapsed_ms}",
            otp_matched=bool(code),
            otp_elapsed_ms=elapsed_ms,
        )
        return code

    if resend_callback is None:
        reason = "no_callback"
    elif not otp_resend_eligible(provider):
        reason = "provider_not_eligible"
    else:
        reason = ""
    if reason:
        # Single-shot window: this channel has no second-chance resend, so the
        # whole budget is one poll.  If the mail is late the run burns the
        # entire timeout and dies -- record the budget *and why there was no
        # resend*.  ``resend=none`` alone used to conflate two different fixes:
        # "the caller wired no callback" (a bug here) vs "this channel cannot
        # resend" (a capability gap, see ``DEFAULT_OTP_RESEND_PROVIDERS``).
        _otp_poll_log(
            "start",
            provider,
            f"timeout={total_timeout}s resend=none reason={reason}",
            otp_timeout_s=total_timeout,
            otp_resend_enabled=False,
            otp_resend_reason=reason,
        )
        return finish(poll(total_timeout))
    if resend_after_seconds is None:
        value = current_config_data().get("email_registration")
        email_cfg = value if isinstance(value, Mapping) else {}
        resend_after_seconds = email_cfg.get("remail_otp_resend_after_seconds", 30)
    try:
        first_window = max(0, int(resend_after_seconds or 0))
    except (TypeError, ValueError):
        first_window = 30
    if first_window <= 0 or first_window >= total_timeout:
        _otp_poll_log(
            "start",
            provider,
            f"timeout={total_timeout}s resend=disabled reason=window_disabled",
            otp_timeout_s=total_timeout,
            otp_resend_enabled=False,
            otp_resend_reason="window_disabled",
        )
        # ``poll()``，不是模块级的 ``_poll_email_otp`` —— 这条分支以前绕过
        # ``poll_otp_fn`` 直连默认轮询器，于是注册泳道注入的
        # ``MailboxService.poll_otp`` 在「重发窗口被关掉」时被静默跳过。
        # 同一件事两条路径（本项目铁律 5）：三条分支必须都走 ``poll``。
        return finish(poll(total_timeout))
    _otp_poll_log(
        "start",
        provider,
        f"timeout={total_timeout}s resend_after={first_window}s",
        otp_timeout_s=total_timeout,
        otp_resend_enabled=True,
        otp_resend_after_s=first_window,
    )
    code = poll(first_window)
    if code:
        return finish(code)
    # Cancellation arriving during the first poll window must not pay for a
    # resend request plus the remaining window.
    ensure_not_cancelled()
    registration_stage("email_otp_resend")
    try:
        response = resend_callback()
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in (200, 202, 204, 409):
            print(f"  ReMail OTP resend was not accepted: {status}")
    except Exception as exc:
        print(f"  ReMail OTP resend warning: {exc}")
    registration_stage("email_otp_wait")
    return finish(poll(total_timeout - first_window))
