"""Protocol (email/AT) registration stage orchestrator and workflow.

Owns the ordered email-registration pipeline (``run()`` sequences the stages).
The stage *bodies* live in sibling modules and are re-exported here so every
historical import path keeps working. Peripheral units were split out in two
waves -- 2026-10-01 (``persistence`` / ``stage_runner`` / ``protocol_helpers`` /
``otp_stages`` / ``account_stages`` / ``edge_challenge`` / ``resume`` /
``sentinel_stages``) and 2026-10-24 (``bootstrap_stages`` / ``identity_stages`` /
``auth_stages`` / ``register_stages``):

* ``bootstrap_stages`` -- reference-lane overlay, bootstrap failure taxonomy, config/proxy/mailbox setup
* ``identity_stages`` -- fingerprint/geo, sentinel extraction, device context, identity preparation
* ``auth_stages`` -- the signin/authorize handshake plus the password-lane predicates
* ``register_stages`` -- password/passwordless ``user/register`` handling and its failure taxonomy
* ``otp_stages`` -- the send/wait/validate email-OTP trio
* ``account_stages`` -- create_account, session fetch, and AT probe stages
* ``edge_challenge`` -- the in-flow edge-challenge hook and exit move
* ``resume`` -- checkpoint persistence and the post-create resume stage
* ``sentinel_stages`` -- Sentinel switch, password-page bundle and per-flow issuance
* ``persistence`` -- the checkpoint/account persistence seam
* ``stage_runner`` -- ``RegistrationAbort`` and the stage executor
* ``protocol_helpers`` -- pure predicates/formatters + safe int/float

This module owns stage **ordering**, the lifecycle and the two-line delegates;
the stage bodies are not here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from . import account_stages as _account_stages
from . import auth_stages as _auth_stages
from . import bootstrap_stages as _bootstrap_stages
from . import edge_challenge as _edge_challenge
from . import finalize as _registration_finalize
from . import identity_stages as _identity_stages
from . import otp_stages as _otp_stages
from . import register_stages as _register_stages
from . import reference_lane as _reference_lane
from . import resume as _resume
from . import sentinel_stages as _sentinel_stages
from . import warmup as _warmup
from ..codex_oauth import collect_codex_oauth_tokens
from ..kernel.error_classification import INTERNAL_EXCEPTION_TYPES
from ..mailbox.errors import MailboxEndpointUnavailableError
from ..providers.mailbox_graph import MailboxAuthInvalidError
from .cancel import RegistrationCancelled, cancellable_sleep, ensure_not_cancelled
from .flags import registration_flag
from .operations import RegistrationOperations
from .persistence import RegistrationPersistence, StorageRegistrationPersistence
from .protocol_helpers import (
    _create_account_response_line,
    _is_existing_login_dead_end_error,
    _login_probe_password,
    _new_registration_session,
    _safe_float,
    _safe_int,
)
from .result import (
    attach_fingerprint_geo_audit,
    record_failed_mailbox_purchase,
    safe_device_identity,
    safe_proxy_audit,
)
from .retry_guard import RegistrationRetryGuard
from .runtime import RegistrationRuntimeState
from .stage_runner import RegistrationAbort, RegistrationStageRunner
from .state import (
    RegistrationStageOverrun,
    RegistrationState,
    RegistrationStateMachine,
)

#: Print budget for the ``user/register`` response line (see
#: ``_post_user_register``). Named so the observability contract is greppable.

__all__ = [
    "RegistrationAbort",
    "RegistrationEmailWorkflow",
    "RegistrationPersistence",
    "RegistrationStageRunner",
    "StorageRegistrationPersistence",
    "_apply_protocol_fingerprint",
    "_create_account_response_line",
    "_is_existing_login_dead_end_error",
    "_login_probe_password",
    "_new_registration_session",
    "_safe_float",
    "_safe_int",
    "collect_codex_oauth_tokens",
]


from .identity_stages import _apply_protocol_fingerprint


_LOGGER = logging.getLogger(__name__)


class RegistrationEmailWorkflow:
    """Ordered email-registration pipeline. Owns stage ordering; not payment/recovery.

    Stage-boundary contract (kept executable by the tests in
    ``tests/registration/test_registration_*`` and by ``docs/architecture.md``):

    - ``run()`` is the ONLY place stages are sequenced; stages never call each
      other directly. Shared state flows through ``self.runtime``
      (``RegistrationRuntimeState``) and side effects through ``self.r``
      (``RegistrationOperations``, injected — a Mock in tests stays honest).
    - Each ``*_stage`` / stage-named method is independently addressable and
      independently testable. ``probe_access_token`` had zero direct tests until
      ``tests/registration/test_registration_at_probe.py``; when you change a stage, add or
      extend its own contract test rather than relying on full-run integration.
    - Stage methods communicate *verdicts* via ``self.runtime`` fields and the
      checkpoint payload, never via return values (they all return ``None`` or
      a checkpoint dict). ``finalize()`` is the single place the terminal
      result is assembled.
    - This file owns stage ordering and the delegates; the stage *bodies* live
      in the sibling modules (``bootstrap_stages`` / ``identity_stages`` /
      ``auth_stages`` / ``register_stages`` since 2026-10-24, plus
      ``otp_stages`` / ``account_stages`` / ``sentinel_stages`` / ``resume`` /
      ``edge_challenge`` / ``warmup`` / ``finalize``). Every method below is a
      two-line forwarder so ``patch.object(RegistrationEmailWorkflow, name,
      ...)`` still intercepts the call;
      ``tests/registration/test_handlers_module_ownership.py`` asserts each body
      exists in exactly one module and that this file only forwards. Do not move
      a body back here -- it trips that guard, and the shared
      ``self.runtime``/``self.r`` coupling it was always claimed to need never
      materialised. The enforced seam remains ``RegistrationOperations``
      (workflow → provider) and ``store`` (persistence); see
      ``docs/architecture.md`` "Dependency Direction".
    """

    def __init__(
        self,
        machine: RegistrationStateMachine,
        *,
        proxy: Any = None,
        password: Any = None,
        sentinel_data: Mapping[str, Any] | None = None,
        mailbox: Any = None,
        phone_pool: Any = None,
        codex_oauth: bool = False,
        registration_mode: Any = None,
        browser_headless: bool | None = None,
        enroll_2fa: bool = True,
        config: Mapping[str, Any] | None = None,
        proxy_metadata: Mapping[str, Any] | None = None,
        operations: RegistrationOperations,
        persistence: RegistrationPersistence | None = None,
        post_process_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.machine = machine
        self.input_proxy = proxy
        self.input_password = password
        self.input_sentinel = sentinel_data
        self.input_mailbox = mailbox
        self.phone_pool = phone_pool
        # Protocol registration is AT-only. Keep the legacy argument accepted
        # for callers, but normalize it away instead of entering a dead stage.
        self.codex_oauth = False
        self.input_registration_mode = registration_mode
        self.browser_headless = browser_headless
        self.enroll_2fa = bool(enroll_2fa)
        self.config = config
        self.proxy_metadata = dict(proxy_metadata or {})
        self.fingerprint_country = ""
        self.device_identity: dict[str, Any] = {}
        self._device_seeded = False
        self._operations = operations
        self.persistence = persistence or StorageRegistrationPersistence()
        self.post_process_result = post_process_result
        self.runtime = RegistrationRuntimeState()
        self.stage_runner = RegistrationStageRunner(self.runtime, machine)
        self._timing_open = False

    @property
    def r(self) -> RegistrationOperations:
        return self._operations

    def _fingerprint_geo_metadata(self) -> dict[str, Any]:
        return _identity_stages.fingerprint_geo_metadata(self)

    def run(self) -> dict[str, Any]:
        r = self.r
        r._tl().clear()
        r.select_auth_fingerprint(rotate=True)
        self._apply_reference_lane()
        # A real ``with``, not a hand-wired ``__enter__``/``__exit__`` pair. The
        # manual form had to pass ``(None, None, None)`` as the exit info, so the
        # context manager could never observe the exception unwinding this frame.
        # Nothing depends on that today (``runtime_config_scope`` only resets a
        # ContextVar in ``finally``), but the next ``except``/``throw`` added
        # inside it would silently never fire on the failure paths below.
        with r.runtime_config_scope(self.config, workflow="registration"):
            try:
                ensure_not_cancelled()
                self._run_bootstrap()
                resumed = self._resume_post_create()
                if resumed is not None:
                    return resumed
                self._run_stage(RegistrationState.AUTH_FLOW, "2-Auth flow", self.auth_flow)
                self._run_stage(RegistrationState.USER_REGISTER, "3-User register (email+password)", self.user_register)
                self._run_stage(RegistrationState.EMAIL_OTP_SEND, "4-Trigger email OTP", self.send_email_otp)
                self._run_stage(RegistrationState.EMAIL_OTP_WAIT, "5-Get email OTP", self.wait_email_otp)
                self._run_stage(RegistrationState.EMAIL_OTP_VALIDATE, "6-Validate email OTP", self.validate_email_otp)
                self._run_stage(RegistrationState.CREATE_ACCOUNT, "7-Create account", self.create_account)
                self._run_stage(RegistrationState.AUTH_SESSION, "8-Fetch auth session", self.fetch_auth_session)
                self._run_stage(
                    RegistrationState.ACCESS_TOKEN_PROBE, "8d-Validate access token", self.probe_access_token
                )
                self._set_outcome()
                # Opt-in (``registration.chatgpt_bootstrap.enabled``) and, like the
                # refresh token below, strictly after the verdict: decoration, never
                # a gate.
                self.post_signup_warmup()
                # Opt-in, and deliberately *after* ``_set_outcome``: it must never be
                # able to change the registration verdict (see the method docstring).
                self.obtain_oauth_refresh_token()
                self._run_stage(RegistrationState.TOTP_ENROLL, "9-Enroll TOTP", self.enroll_totp)
                return self._run_stage(RegistrationState.FINALIZE, "10-Finalize registration", self.finalize)
            except RegistrationAbort as exc:
                return self._abort_result(str(exc))
            except RegistrationCancelled:
                # Cooperative cancellation: report the same cancelled contract the
                # batch runner and the browser path use, not an internal error.
                return self._abort_result("registration_cancelled", cancelled=True)
            except (MailboxEndpointUnavailableError, MailboxAuthInvalidError) as exc:
                return self._abort_result(str(exc))
            except Exception as exc:
                error = f"registration_internal_error:{type(exc).__name__}:{exc}"
                return self._abort_result(error)
            finally:
                self._close_sessions()

    def _apply_reference_lane(self) -> None:
        return _bootstrap_stages.apply_reference_lane(self)

    def _abort_result(self, error: str, *, cancelled: bool = False) -> dict[str, Any]:
        """Single failure-result constructor for every run() exit path.

        三条 except 臂曾各拼一份结果（cancelled 臂多一个 registration_state），
        漂移只是时间问题——现在同一构造，cancelled 仅多打一个状态标记。
        """
        r = self.r
        if cancelled:
            if self.machine.state is not RegistrationState.FAILED:
                self.machine.fail("registration_cancelled")
        elif self.machine.state is not RegistrationState.FAILED:
            self.machine.fail(error)
        if not cancelled:
            error = self._with_password_claim(error)
        result = r._failure_result(
            error,
            email=self.runtime.username,
            mailbox=self.runtime.mailbox,
            password=self.runtime.password,
            existing_account=self.runtime.existing_account,
            existing_account_password_known=self.runtime.existing_account_password_known,
            access_token=self.runtime.access_token,
        )
        result["proxy_audit"] = safe_proxy_audit(getattr(self, "proxy_metadata", {}))
        result["device_identity"] = safe_device_identity(getattr(self, "device_identity", {}))
        attach_fingerprint_geo_audit(result, self._fingerprint_geo_metadata())
        if cancelled:
            result["registration_state"] = "cancelled"
        elif self.runtime.existing_account:
            result["registration_state"] = "partial_registered"
        if self.runtime.existing_login_error:
            # The re-login lane's own cause.  ``_registration_outcome`` prefers
            # ``user_already_exists`` over it -- correctly, that *is* the real
            # cause -- but the side effect is that whether the lane ran at all,
            # and whether it spent an email code, became unanswerable from
            # storage: measured 2026-09-15, ``existing_login`` appeared in 0 of
            # 4377 ``registration_audit.detail_json`` values, and the stdout log
            # that would have shown it stopped 09-15 04:18.  Carry it so the
            # "did this cost a code?" question stays answerable.
            result["existing_login_error"] = self.runtime.existing_login_error
        result["registration_machine"] = self.machine.snapshot()
        # Keep the purchased mailbox replayable (see ``registration.result`` and
        # ``mailbox.reclaim``): the sanitized result can no longer carry its
        # token, so the record has to be taken here while the raw mailbox lives
        # on ``self.runtime``. Deduplicated by email, so the ``finalize`` call
        # for a non-exception failure cannot write a second line.
        record_failed_mailbox_purchase(self, result)
        return result

    def _with_password_claim(self, error: str) -> str:
        return _auth_stages.with_password_claim(self, error)

    def _edge_challenge_rotate_exit_enabled(self) -> bool:
        return _edge_challenge.edge_challenge_rotate_exit_enabled(self)

    def _install_edge_challenge_hook(self, session: Any) -> None:
        return _edge_challenge.install_edge_challenge_hook(self, session)

    def _on_edge_challenge(self, session: Any, verdict: str, allow_rotate: bool) -> bool:
        return _edge_challenge.on_edge_challenge(self, session, verdict, allow_rotate)

    def _count_edge_challenge_rotate_failure(self, reason: str) -> None:
        return _edge_challenge.count_edge_challenge_rotate_failure(self, reason)

    def _run_stage(self, state: RegistrationState, label: str, handler: Callable[[], Any]) -> Any:
        r = self.r
        # Checked before _tick: cancellation must surface as
        # RegistrationCancelled, not be reclassified as a stage transport
        # failure, and must not leave an open timing entry behind.
        ensure_not_cancelled()
        r._tick(label)
        self._timing_open = True
        try:
            self.stage_runner.context = self.runtime.context or self.runtime
            value = self.stage_runner.run_stage(
                state,
                handler,
                timeout_seconds=self._stage_timeout(state),
            )
        except RegistrationAbort:
            raise
        except RegistrationCancelled:
            # Raised from inside a handler (e.g. the OTP poll loop). Must not
            # be reclassified as a stage transport failure.
            raise
        except RegistrationStageOverrun as exc:
            raise RegistrationAbort(f"{state.value}_stage_budget_exceeded:{exc}") from exc
        except INTERNAL_EXCEPTION_TYPES as exc:
            # Programming/contract errors only. IndexError used to fall into the
            # transport catch-all below and get retried as a network failure --
            # the same "error name hides root cause" class as the RuntimeError
            # demotion fixed in error_classification. The tuple itself is owned
            # by ``kernel.error_classification`` (INTERNAL_EXCEPTION_TYPES) and
            # matches INTERNAL_ERROR_MARKERS' ``{state}_internal:<Type>:``
            # label; do not re-inline it here.
            raise RegistrationAbort(f"{state.value}_internal:{type(exc).__name__}:{exc}") from exc
        except Exception as exc:
            # Transport/protocol failures keep their message so
            # classify_error's marker vocabulary decides retryability.
            raise RegistrationAbort(f"{state.value}_transport:{exc}") from exc
        finally:
            if self._timing_open:
                r._safe_tock()
                self._timing_open = False
        if state is RegistrationState.FINALIZE and isinstance(value, dict):
            value["timing"] = r._timing_summary()
            r._print_timings()
        return value

    def _stage_timeout(self, state: RegistrationState) -> float | None:
        if self.config is None:
            return None
        registration_cfg = self.config.get("registration", {})
        if not isinstance(registration_cfg, Mapping):
            return None
        values = registration_cfg.get("stage_timeouts", {})
        if not isinstance(values, Mapping) or state.value not in values:
            return None
        try:
            return float(values[state.value])
        except (TypeError, ValueError):
            return None

    def _otp_poll_timeout(self) -> int:
        """Mailbox poll budget for the OTP wait stage.

        The stage budget is only observable after a handler returns, so the
        stage that can legitimately block for minutes hands the smaller of the
        two limits to the poll that actually blocks.
        """
        timeout = _safe_int(self.runtime.otp.email_cfg.get("otp_timeout", 300) or 300, 300)
        budget = self._stage_timeout(RegistrationState.EMAIL_OTP_WAIT)
        if budget is None:
            return timeout
        return max(1, min(timeout, _safe_int(budget, timeout)))

    def _abort(self, error: str) -> None:
        raise RegistrationAbort(error)

    def _checkpoint_payload(self) -> dict[str, Any]:
        return _resume.checkpoint_payload(self)

    def _persist_checkpoint(self, state: str) -> None:
        return _resume.persist_checkpoint(self, state)

    def _resume_post_create(self) -> dict[str, Any] | None:
        return _resume.resume_post_create(self)

    def _has_resume_checkpoint(self) -> bool:
        return _resume.has_resume_checkpoint(self)

    def _run_bootstrap(self) -> None:
        return _bootstrap_stages.run_bootstrap(self)

    def _bootstrap(self) -> None:
        return _bootstrap_stages.bootstrap(self)

    def _device_identity_seeded_by_email_enabled(self) -> bool:
        return _identity_stages.device_identity_seeded_by_email_enabled(self)

    def _device_context(self, email: Any) -> dict[str, Any]:
        return _identity_stages.device_context(self, email)

    def extract_sentinel(self) -> None:
        return _identity_stages.extract_sentinel(self)

    def prepare_identity(self) -> None:
        return _identity_stages.prepare_identity(self)

    def _reference_lane_active(self) -> bool:
        return _reference_lane.reference_lane_enabled(self.config)

    def _password_sentinel_bundle_enabled(self) -> bool:
        return _sentinel_stages.password_sentinel_bundle_enabled(self)

    def _prime_password_sentinel_bundle(self) -> None:
        return _sentinel_stages.prime_password_sentinel_bundle(self)

    def _issue_sentinel(self, flow: str, *, force_fresh: bool = False) -> Any:
        return _sentinel_stages.issue_sentinel(self, flow, force_fresh=force_fresh)

    def auth_flow(self) -> None:
        return _auth_stages.auth_flow(self)

    def _password_lane_active(self) -> bool:
        return _auth_stages.password_lane_active(self)

    def user_register(self) -> None:
        return _register_stages.user_register(self)

    def _user_register_passwordless(self) -> None:
        return _register_stages.user_register_passwordless(self)

    def _abort_user_register_failure(self) -> None:
        return _register_stages.abort_user_register_failure(self)

    def _post_user_register(self) -> Any:
        return _register_stages.post_user_register(self)

    def send_email_otp(self) -> None:
        return _otp_stages.send_email_otp(self)

    def wait_email_otp(self) -> None:
        return _otp_stages.wait_email_otp(self)

    def _otp_provider(self) -> str:
        return _otp_stages._otp_provider(self)

    def _otp_timeout_error(self) -> str:
        return _otp_stages._otp_timeout_error(self)

    def validate_email_otp(self) -> None:
        return _otp_stages.validate_email_otp(self)

    def _prime_about_you_page_enabled(self) -> bool:
        return _account_stages.prime_about_you_page_enabled(self)

    def _prime_about_you_page(self) -> None:
        return _account_stages.prime_about_you_page(self)

    def _create_account_disallowed_backoff_delays(self) -> tuple[int, ...]:
        return _account_stages.create_account_disallowed_backoff_delays(self)

    def create_account(self) -> None:
        return _account_stages.create_account(self)

    def _post_create_account_with_backoff(self) -> Any:
        return _account_stages.post_create_account_with_backoff(self, sleep_fn=cancellable_sleep)

    def _record_create_account_response(self, response: Any) -> None:
        return _account_stages.record_create_account_response(self, response)

    def _mark_partial_registration(self, *, reason: str = "user_already_exists") -> None:
        return _account_stages.mark_partial_registration(
            self,
            reason=reason,
            retry_guard_cls=RegistrationRetryGuard,
        )

    def fetch_auth_session(self) -> None:
        return _account_stages.fetch_auth_session(self, retry_guard_cls=RegistrationRetryGuard)

    def probe_access_token(self) -> None:
        return _account_stages.probe_access_token(self)

    def _obtain_refresh_token_enabled(self) -> bool:
        # ``registration.obtain_refresh_token`` (default **False**).  Read
        # through the shared two-sided parser: ``bool(cfg.get(...))`` made the
        # string ``"false"`` truthy, so a hand-edited config could turn this
        # default-off opt-in **on** (2026-10-11 scan P1-B).  A ``None`` config
        # (the bare-handler path some tests build) reads as the default, off.
        return registration_flag(self.config, "obtain_refresh_token", False)

    def post_signup_warmup(self) -> None:
        self.warmup_result = _warmup.run_post_signup_warmup(self)

    def obtain_oauth_refresh_token(self) -> None:
        return _registration_finalize.obtain_oauth_refresh_token(self)

    def _set_outcome(self) -> None:
        r = self.r
        s = self.runtime
        s.success, s.error, s.registration_warning = r._registration_outcome(
            s.create_ok,
            s.create_data,
            s.access_token,
            s.at_probe,
            s.existing_login_error,
        )
        # Email registration is AT-only. OAuth/phone recovery remains in its
        # own entry points; these impossible branches added hidden dependencies.
        s.post_registration_ready = True

    def enroll_totp(self) -> None:
        return _registration_finalize.enroll_totp(self)

    def finalize(self) -> dict[str, Any]:
        return _registration_finalize.finalize(self)

    def _close_sessions(self) -> None:
        seen: set[int] = set()
        resources = self.runtime.resources
        for session in (resources.session, resources.login_session):
            if session is None or id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
