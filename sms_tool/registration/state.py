"""Explicit state and context model for the registration orchestrator."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

from ..config import current_config_data
from ..kernel.sanitizer import sanitize_text

if TYPE_CHECKING:  # pragma: no cover - type-only, never executed
    from .operations import RegistrationOperations
    from .persistence import RegistrationPersistence
    from .runtime import RegistrationRuntimeState


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
    ACCESS_TOKEN_PROBE = "access_token_probe"
    TOTP_ENROLL = "totp_enroll"
    FINALIZE = "finalize"
    COMPLETED = "completed"
    FAILED = "failed"


_STATE_ORDER = {
    state: index
    for index, state in enumerate(state for state in RegistrationState if state is not RegistrationState.FAILED)
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
        # ``COMPLETED`` names the last *stage*, not the run's verdict: the
        # pipeline ran out of steps, and whether an account was actually
        # registered is decided by the caller after the final stage returns.
        # ``registration_progress.persist`` is the only layer that knows the
        # outcome, so it owns the terminal event.
        #
        # Announcing ``success`` here is what produced the false success signal:
        # the operator log read "阶段 · 完成 (completed) — 成功" and then, in the
        # same second, "阶段 · 失败 (failed) — 失败" for the same attempt. That
        # hit 27 of 179 failures between 09-08 and 09-13 (14 of 26 on 09-12
        # alone, once the existing-login fallback became the dominant failure
        # mode and started failing *after* ``finalize``). It also made the WPF
        # batch counter mark those attempts complete, because the desktop
        # envelope treats ``status=success`` as terminal.
        if state is not RegistrationState.COMPLETED:
            self.stage_callback(state.value, "running", safe_detail)

    def fail(self, detail: str = "") -> None:
        detail = sanitize_text(detail)
        self.state = RegistrationState.FAILED
        self.history.append(RegistrationTransition(self.state, detail))
        self.stage_callback(self.state.value, "failed", detail)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "history": [{"state": transition.state.value, "detail": transition.detail} for transition in self.history],
        }


class RegistrationWorkflow(Protocol):
    """The workflow surface the split stage modules actually use.

    ``handlers.RegistrationEmailWorkflow`` satisfies it structurally. The stage
    modules used to import that concrete class under ``TYPE_CHECKING`` and reach
    through it for 35 attributes (20 private). Annotating them against this
    Protocol instead documents the seam and lets a test drive one stage with a
    small fake rather than constructing the whole workflow.

    Kept in ``state.py`` because every stage module already imports this module;
    a new module would need a directory-map/layer-manifest entry for no benefit.
    The ``TYPE_CHECKING`` imports above keep this module free of runtime cycles.
    """

    @property
    def r(self) -> RegistrationOperations: ...

    runtime: RegistrationRuntimeState
    config: Mapping[str, Any] | None
    persistence: RegistrationPersistence
    machine: RegistrationStateMachine
    proxy_metadata: dict[str, Any]
    enroll_2fa: bool
    input_mailbox: Any
    input_proxy: Any
    input_password: Any
    input_sentinel: Mapping[str, Any] | None
    input_registration_mode: Any
    browser_headless: bool | None
    device_identity: dict[str, Any]
    fingerprint_country: str
    post_process_result: Callable[[dict[str, Any]], dict[str, Any]] | None
    _timing_open: bool
    _device_seeded: bool

    def _run_stage(self, state: RegistrationState, label: str, handler: Callable[[], Any]) -> Any: ...

    def _abort(self, error: str) -> None: ...

    def _bootstrap(self) -> None: ...

    def extract_sentinel(self) -> None: ...

    def prepare_identity(self) -> None: ...

    def _device_context(self, email: Any) -> dict[str, Any]: ...

    def _device_identity_seeded_by_email_enabled(self) -> bool: ...

    def _prime_password_sentinel_bundle(self) -> None: ...

    def _persist_checkpoint(self, state: str) -> None: ...

    def _issue_sentinel(self, flow: str, *, force_fresh: bool = False) -> Any: ...

    def _otp_poll_timeout(self) -> int: ...

    def _otp_provider(self) -> str: ...

    def _otp_timeout_error(self) -> str: ...

    def _password_lane_active(self) -> bool: ...

    def _fingerprint_geo_metadata(self) -> dict[str, Any]: ...

    def _edge_challenge_rotate_exit_enabled(self) -> bool: ...

    def _install_edge_challenge_hook(self, session: Any) -> None: ...

    def _on_edge_challenge(self, session: Any, verdict: str, allow_rotate: bool) -> bool: ...

    def _count_edge_challenge_rotate_failure(self, reason: str) -> None: ...

    def _mark_partial_registration(self, *, reason: str = "user_already_exists") -> None: ...

    def _obtain_refresh_token_enabled(self) -> bool: ...

    def _prime_about_you_page(self) -> None: ...

    def _prime_about_you_page_enabled(self) -> bool: ...

    def _post_create_account_with_backoff(self) -> Any: ...

    def _record_create_account_response(self, response: Any) -> None: ...

    def _create_account_disallowed_backoff_delays(self) -> tuple[int, ...]: ...

    def _set_outcome(self) -> None: ...

    def fetch_auth_session(self) -> None: ...

    def probe_access_token(self) -> None: ...

    def enroll_totp(self) -> None: ...

    def finalize(self) -> dict[str, Any]: ...

    def obtain_oauth_refresh_token(self) -> None: ...

    def post_signup_warmup(self) -> None: ...


class RegistrationStageOverrun(TimeoutError):
    """A stage exceeded its configured budget, detected after it returned.

    Distinct from a transport ``TimeoutError`` raised inside a stage, which the
    orchestrator must keep classifying as a network failure.
    """

    def __init__(self, state: RegistrationState, elapsed_seconds: float, budget_seconds: float):
        self.state = state
        self.elapsed_seconds = elapsed_seconds
        self.budget_seconds = budget_seconds
        super().__init__(
            f"registration stage exceeded its budget: {state.value} ({elapsed_seconds:.1f}s > {budget_seconds:.1f}s)"
        )


@dataclass(frozen=True)
class RegistrationStage:
    state: RegistrationState
    # The handler receives whatever opaque state object the caller supplies and
    # is free to ignore it. The email workflow keeps its mutable outputs on a
    # shared ``RegistrationRuntimeState`` and closes over it, so this is
    # deliberately ``Any`` rather than ``RegistrationContext``.
    handler: Callable[[Any], Any]
    timeout_seconds: float | None = None

    def run(self, context: Any, machine: RegistrationStateMachine) -> Any:
        """Run one stage and enforce its budget without orphan worker threads.

        This is a budget check, not a cancellation: the handler runs to
        completion and the overrun is classified once control returns, so
        cleanup always happens in the calling thread. Stages that can block for
        minutes (mailbox OTP polling) receive the same budget as their own
        operation timeout so the limit is also enforced while they run.
        """
        machine.transition(self.state)
        started = time.monotonic()
        value = self.handler(context)
        elapsed = time.monotonic() - started
        budget = None if self.timeout_seconds is None else max(0.0, float(self.timeout_seconds))
        if budget is not None and elapsed > budget:
            machine.fail(f"{self.state.value}_stage_budget_exceeded")
            raise RegistrationStageOverrun(self.state, elapsed, budget)
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


# Private salt on purpose (plan-2026-10-10-fingerprint-device-layer §10 q1): the
# hypothesis under test is "a stable per-address ``oai-did``", not "turb's exact
# function".  With turb's public literal anyone could derive a victim address's
# ``oai-did`` from the email alone; with this one only this repository can.
_DEVICE_SEED_SALT = "gpt-register-tool:registration:"


def seeded_device_identity(email: str | None) -> dict[str, str]:
    """``oai-did`` and ``auth_session_logging_id`` as a pure function of the address.

    S1 of the device-layer plan.  Same shape as the random ids they replace
    (UUID string for ``device_id``, 32 hex characters for the logging id), so
    nothing downstream can tell the source from the value.  Empty for a blank
    address: there is nothing to derive from.
    """
    key = str(email or "").strip().casefold()
    if not key:
        return {}
    base = f"{_DEVICE_SEED_SALT}{key}"
    return {
        "device_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"device_id:{base}")),
        "auth_session_logging_id": uuid.uuid5(uuid.NAMESPACE_URL, f"auth_session_logging_id:{base}").hex,
    }


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
        reused_device_context=bool(device_context.get("device_id") or device_context.get("auth_session_logging_id")),
        browser_headless=browser_headless,
    )


def _normalize_registration_mode(value=None):
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        value = current_config_data().get("email_registration")
        _raw_cfg: object = value
        cfg: Mapping = _raw_cfg if isinstance(_raw_cfg, Mapping) else {}
        raw = (
            str(cfg.get("registration_mode") or cfg.get("signup_mode") or "passwordless")
            .strip()
            .lower()
            .replace("-", "_")
        )
    if raw in {"password", "password_signup", "user_register", "legacy"}:
        return "password"
    if raw in {"passwordless", "passwordless_signup", "login_or_signup", "har"}:
        return "passwordless"
    return "passwordless"


def _stored_registration_record(email):
    """The stored account row for ``email``, or ``{}`` when unavailable.

    One import site on purpose: the delayed-import ratchet counts every nested
    import, so the two readers below share this helper instead of each reaching
    for ``storage`` themselves.
    """
    try:
        from ..storage import get_account_record

        return get_account_record(email) or {}
    except Exception:
        return {}


def _stored_registration_password(email):
    row = _stored_registration_record(email)
    if not row:
        return ""
    error = str(row.get("error") or "").lower()
    if "password_verify_failed" in error:
        return ""
    password = str(row.get("password") or "").strip()
    if password:
        return password
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    return str(raw.get("password") or "").strip()


def _stored_registration_totp(email):
    """The saved TOTP secret for ``email``, or ``""`` when we do not have one.

    A password login is followed by the account's own MFA challenge, so the
    password alone is not enough to complete it -- without the secret the lane
    can only answer ``existing_login_totp_secret_missing``.
    """
    row = _stored_registration_record(email)
    if not row:
        return ""
    secret = str(row.get("totp_secret") or "").strip()
    if secret:
        return secret
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    return str(raw.get("totp_secret") or "").strip()
