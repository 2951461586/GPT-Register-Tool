"""Stage-owned registration state with a compatibility view for flat callers.

Checkpoint and result schemas remain flat. New handlers can consume one group
instead of depending on the entire workflow's mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from .registration_state import RegistrationContext


@dataclass(repr=False)
class RegistrationResources:
    proxy: str = ""
    mailbox: Any = None
    mailbox_service: Any = None
    context: RegistrationContext | None = None
    session: Any = None
    login_session: Any = None
    auth_base: str = ""
    chat_base: str = ""
    base_headers: dict[str, Any] = field(default_factory=dict)
    resume_checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(repr=False)
class RegistrationIdentity:
    username: str = ""
    password: str = ""
    password_unknown: bool = False
    full_name: str = ""
    birthdate: str = ""
    registration_mode: str = ""
    device_id: str = ""
    session_logging_id: str = ""
    flow_invocation_id: str = ""


@dataclass(repr=False)
class RegistrationAuth:
    sentinel_data: Mapping[str, Any] = field(default_factory=dict)
    sentinel_token: str = ""
    sentinel_authorize_token: str = ""
    sentinel_so_token: str = ""
    auth_flow_started: int = 0
    csrf_token: str = ""
    signup_state: dict[str, Any] = field(default_factory=dict)
    reg_response: Any = None
    reg_data: dict[str, Any] = field(default_factory=dict)
    # 🔴 ``resume_email_verification`` was **deleted** on 2026-09-16.  Its only
    # writer was the ``invalid_auth_step`` + ``email-verification`` recovery
    # branch in ``user_register``, which went away when password-first
    # registration adopted fail-fast; the flag was then permanently ``False``
    # and both of its reads were dead.  Do not add it back: the capability it
    # offered (resume the OTP step for an address the server already has) is
    # worthless -- it does not skip ``create_account``, and for an existing
    # address the OTP round trip lands on ``/about-you`` with no NextAuth
    # session (measured 2026-09-14, 5/5).  ``RegistrationRuntimeState`` rejects
    # unknown keyword arguments, so reintroducing it needs a deliberate change
    # here, and ``tests/test_user_register_response_contract.py`` pins that.
    # ``client_auth_session_dump[after_signup_state]`` 的 summary 与它给出的泳道
    # 判定（``auth_state.signup_lane_verdict``：``login`` / ``signup`` / ``unknown``）。
    # P0-1 判据 A 的取证埋点 —— 目前只记录，不据此写死路账本（实测精确率 3/3
    # 但召回率仅 3/11，样本不足以承担误判代价）。
    signup_dump: dict[str, Any] = field(default_factory=dict)
    signup_lane: str = ""


@dataclass(repr=False)
class RegistrationOtp:
    otp_issued_after: int = 0
    email_cfg: dict[str, Any] = field(default_factory=dict)
    email_code: str = ""
    otp_data: dict[str, Any] = field(default_factory=dict)
    # ``client_auth_session_dump[after_otp_send]`` 的 summary —— ``send_email_otp``
    # 保留 ``auth_state.fetch_client_auth_session_dump`` 的返回值（此前只打印、
    # 丢弃）。``wait_email_otp`` 用它分辨超时根因：服务端**没完成**派发（出口
    # 侧证据）还是**已派发、邮箱没收到**（邮箱侧）。判据在
    # ``auth_state.otp_dispatch_verdict``，消费在 ``registration_pulse``。
    otp_send_dump: dict[str, Any] = field(default_factory=dict)


@dataclass(repr=False)
class RegistrationAccount:
    create_data: dict[str, Any] = field(default_factory=dict)
    create_ok: bool = False
    existing_account: bool = False
    # Why the existing-account re-login failed, when it did. Kept separate from
    # ``error`` because ``_registration_outcome`` must not overwrite a real
    # create_account error with it, but must surface it when the only other
    # explanation left is the generic "no access token" fallback.
    existing_login_error: str = ""
    # ``create_account`` answered ``user_already_exists`` *and* we hold the
    # account's own password (an explicit ``--password`` or one persisted by an
    # earlier run of ours).  ``password_unknown`` is about persistence hygiene
    # and is set for every ``user_already_exists``; this flag answers the
    # different question the login probe needs -- is a password login even
    # possible?  Without it the probe's positive verdict has nothing to submit.
    existing_account_password_known: bool = False
    auth_session: dict[str, Any] = field(default_factory=dict)
    auth_body: dict[str, Any] = field(default_factory=dict)
    access_token: str = ""
    oauth_result: dict[str, Any] = field(default_factory=dict)
    oauth_tokens: dict[str, Any] = field(default_factory=dict)
    phone_result: dict[str, Any] = field(default_factory=dict)
    oauth_refresh_token: str = ""
    id_token: str = ""
    at_probe: dict[str, Any] = field(default_factory=dict)
    session_recovery_attempts: int = 0
    session_recovery_started_at: int = 0


@dataclass(repr=False)
class RegistrationOutcome:
    success: bool = False
    error: str = ""
    registration_warning: str = ""
    post_registration_ready: bool = False
    totp_secret: str = ""
    twofa_result: dict[str, Any] = field(default_factory=dict)


_GROUPS = {
    "resources": RegistrationResources,
    "identity": RegistrationIdentity,
    "auth": RegistrationAuth,
    "otp": RegistrationOtp,
    "account": RegistrationAccount,
    "outcome": RegistrationOutcome,
}
_FIELD_GROUPS = {item.name: group for group, cls in _GROUPS.items() for item in fields(cls)}


class RegistrationRuntimeState:
    """Grouped storage; legacy flat properties address the same single value."""

    def __init__(self, **values: Any) -> None:
        unknown = values.keys() - _FIELD_GROUPS.keys()
        if unknown:
            raise TypeError(f"unknown registration state fields: {', '.join(sorted(unknown))}")
        self.resources = RegistrationResources()
        self.identity = RegistrationIdentity()
        self.auth = RegistrationAuth()
        self.otp = RegistrationOtp()
        self.account = RegistrationAccount()
        self.outcome = RegistrationOutcome()
        for name, value in values.items():
            setattr(self, name, value)


def _compat_property(group: str, name: str) -> property:
    return property(
        lambda state: getattr(getattr(state, group), name),
        lambda state, value: setattr(getattr(state, group), name, value),
    )


# One-way migration seam, not duplicate state. Remove these properties only
# after all flat callers (including downstream integrations) have migrated.
for _name, _group in _FIELD_GROUPS.items():
    setattr(RegistrationRuntimeState, _name, _compat_property(_group, _name))
