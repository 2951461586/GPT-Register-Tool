"""Explicit state and context model for the registration orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Callable, Mapping

from .sanitizer import sanitize_text


class RegistrationState(str, Enum):
    CREATED = "created"
    MAILBOX_READY = "mailbox_ready"
    SENTINEL = "sentinel"
    IDENTITY_READY = "identity_ready"
    AUTH_FLOW = "auth_flow"
    USER_REGISTER = "user_register"
    EMAIL_OTP_SEND = "email_otp_send"
    EMAIL_OTP_WAIT = "email_otp_wait"
    EMAIL_OTP_VALIDATE = "email_otp_validate"
    CREATE_ACCOUNT = "create_account"
    AUTH_SESSION = "auth_session"
    CODEX_OAUTH = "codex_oauth"
    ACCESS_TOKEN_PROBE = "access_token_probe"
    TOTP_ENROLL = "totp_enroll"
    FINALIZE = "finalize"
    COMPLETED = "completed"
    FAILED = "failed"


_STATE_ORDER = {
    state: index
    for index, state in enumerate(
        state for state in RegistrationState if state is not RegistrationState.FAILED
    )
}


@dataclass(frozen=True)
class RegistrationTransition:
    state: RegistrationState
    detail: str = ""


@dataclass
class RegistrationStateMachine:
    stage_callback: Callable[[str, str, str], None]
    state: RegistrationState = RegistrationState.CREATED
    history: list[RegistrationTransition] = field(default_factory=list)

    def transition(self, state: RegistrationState, detail: str = "") -> None:
        if self.state is RegistrationState.FAILED:
            raise RuntimeError("registration state machine is terminal")
        if state is RegistrationState.FAILED:
            self.fail(detail)
            return
        if _STATE_ORDER[state] < _STATE_ORDER[self.state]:
            raise ValueError(f"invalid registration transition: {self.state.value} -> {state.value}")
        safe_detail = sanitize_text(detail)
        self.state = state
        self.history.append(RegistrationTransition(state, safe_detail))
        self.stage_callback(state.value, "running", safe_detail)

    def fail(self, detail: str = "") -> None:
        detail = sanitize_text(detail)
        self.state = RegistrationState.FAILED
        self.history.append(RegistrationTransition(self.state, detail))
        self.stage_callback(self.state.value, "failed", detail)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "history": [
                {"state": transition.state.value, "detail": transition.detail}
                for transition in self.history
            ],
        }


@dataclass(frozen=True)
class RegistrationStage:
    state: RegistrationState
    handler: Callable[["RegistrationContext"], Any]
    timeout_seconds: float | None = None

    def run(self, context: "RegistrationContext", machine: RegistrationStateMachine) -> Any:
        """Run one stage and enforce its deadline without orphan worker threads.

        Network and browser operations remain responsible for transport-level
        cancellation. The stage deadline classifies an overrun after control
        returns, so cleanup always happens in the calling thread.
        """
        machine.transition(self.state)
        started = time.monotonic()
        value = self.handler(context)
        if self.timeout_seconds is not None and time.monotonic() - started > max(0.0, float(self.timeout_seconds)):
            machine.fail(f"{self.state.value}_timeout")
            raise TimeoutError(f"registration stage timed out: {self.state.value}")
        return value

@dataclass(frozen=True)
class RegistrationContext:
    proxy: str
    mailbox: Any
    sentinel_data: Mapping[str, Any] = field(repr=False)
    auth_base: str
    chat_base: str
    username: str
    password: str = field(repr=False)
    explicit_password: bool
    password_from_storage: bool
    first_name: str
    last_name: str
    birthdate: str
    registration_mode: str
    device_id: str
    session_logging_id: str
    reused_device_context: bool
    browser_headless: bool | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


def prepare_registration_context(
    *,
    proxy: str,
    mailbox: Any,
    sentinel_data: Mapping[str, Any],
    password: str | None,
    registration_mode: str | None,
    auth_base: str,
    chat_base: str,
    stored_password: Callable[[str], str],
    generate_password: Callable[[], str],
    random_name: Callable[[], tuple[str, str]],
    random_birthdate: Callable[[], str],
    normalize_mode: Callable[[str | None], str],
    get_device_context: Callable[[str], Mapping[str, Any]],
    sentinel_device_id: Callable[[Mapping[str, Any]], str],
    new_uuid: Callable[[], str],
    browser_headless: bool | None = None,
) -> RegistrationContext:
    username = str(getattr(mailbox, "email", "") or "").strip()
    explicit_password = bool(str(password or "").strip())
    stored = "" if explicit_password else stored_password(username)
    resolved_password = str(password or stored or generate_password())
    first_name, last_name = random_name()

    device_context = dict(get_device_context(username) or {})
    device_id = str(device_context.get("device_id") or sentinel_device_id(sentinel_data) or new_uuid())
    logging_id = str(device_context.get("auth_session_logging_id") or new_uuid().replace("-", ""))

    return RegistrationContext(
        proxy=proxy,
        mailbox=mailbox,
        sentinel_data=sentinel_data,
        auth_base=auth_base,
        chat_base=chat_base,
        username=username,
        password=resolved_password,
        explicit_password=explicit_password,
        password_from_storage=bool(stored),
        first_name=first_name,
        last_name=last_name,
        birthdate=random_birthdate(),
        registration_mode=normalize_mode(registration_mode),
        device_id=device_id,
        session_logging_id=logging_id,
        reused_device_context=bool(
            device_context.get("device_id") or device_context.get("auth_session_logging_id")
        ),
        browser_headless=browser_headless,
    )
