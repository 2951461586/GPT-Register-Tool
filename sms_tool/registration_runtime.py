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
    resume_email_verification: bool = False


@dataclass(repr=False)
class RegistrationOtp:
    otp_issued_after: int = 0
    email_cfg: dict[str, Any] = field(default_factory=dict)
    email_code: str = ""
    otp_data: dict[str, Any] = field(default_factory=dict)


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
    auth_session: dict[str, Any] = field(default_factory=dict)
    auth_body: dict[str, Any] = field(default_factory=dict)
    access_token: str = ""
    oauth_result: dict[str, Any] = field(default_factory=dict)
    oauth_tokens: dict[str, Any] = field(default_factory=dict)
    phone_result: dict[str, Any] = field(default_factory=dict)
    oauth_refresh_token: str = ""
    id_token: str = ""
    at_probe: dict[str, Any] = field(default_factory=dict)


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
