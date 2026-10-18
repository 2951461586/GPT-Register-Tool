"""Protocol (email/AT) registration stage orchestrator and workflow.

Owns the ordered email-registration pipeline (``run()`` sequences the stages).
Cohesive peripheral units were split into sibling modules on 2026-10-01 and are
re-exported here so every historical import path keeps working:

* ``registration_persistence`` -- the checkpoint/account persistence seam
* ``registration_stage_runner`` -- ``RegistrationAbort`` and the stage executor
* ``registration_protocol_helpers`` -- pure predicates/formatters + safe int/float
* ``registration_otp_stages`` -- the send/wait/validate email-OTP trio
* ``registration_account_stages`` -- create_account, session fetch, and AT probe stages
* ``registration_edge_challenge`` -- the in-flow edge-challenge hook and exit move
* ``registration_resume`` -- checkpoint persistence and the post-create resume stage
* ``registration_sentinel_stages`` -- Sentinel switch, password-page bundle and per-flow issuance
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Mapping

from . import endpoints, registration_checkpoint
from . import fingerprint_pool as _fingerprint_pool
from . import mailbox_service as _mailbox_service
from . import proxy_entry as _proxy_entry
from . import registration_account_stages as _account_stages
from . import registration_concurrency as _registration_concurrency
from . import registration_edge_challenge as _edge_challenge
from . import registration_finalize as _registration_finalize
from . import registration_otp_stages as _otp_stages
from . import registration_reference_lane as _reference_lane
from . import registration_resume as _resume
from . import registration_sentinel_stages as _sentinel_stages
from . import registration_warmup as _warmup
from .auth_headers import set_auth_fingerprint
from .auth_state import signup_lane_verdict
from .codex_oauth import collect_codex_oauth_tokens
from .failure_registry import PASSWORD_CLAIMED_MARKER
from .mailbox_errors import MailboxEndpointUnavailableError
from .operator_output import emit as _emit
from .providers.mailbox_graph import MailboxAuthInvalidError
from .registration_cancel import RegistrationCancelled, cancellable_sleep, ensure_not_cancelled
from .registration_flags import registration_flag
from .registration_operations import RegistrationOperations
from .registration_persistence import RegistrationPersistence, StorageRegistrationPersistence
from .registration_protocol_helpers import (
    _create_account_response_line,
    _is_existing_login_dead_end_error,
    _login_probe_password,
    _new_registration_session,
    _safe_float,
    _safe_int,
)
from .registration_result import attach_fingerprint_geo_audit, safe_device_identity, safe_proxy_audit
from .registration_retry_guard import DEAD_END_SIGNUP_ROUTED_TO_LOGIN, RegistrationRetryGuard
from .registration_runtime import RegistrationRuntimeState
from .registration_stage_runner import RegistrationAbort, RegistrationStageRunner
from .registration_state import (
    RegistrationStageOverrun,
    RegistrationState,
    RegistrationStateMachine,
    prepare_registration_context,
    seeded_device_identity,
)

#: Print budget for the ``user/register`` response line (see
#: ``_post_user_register``). Named so the observability contract is greppable.
_USER_REGISTER_PRINT_BUDGET = 1200

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


def _apply_protocol_fingerprint(ops: Any, config: Any, proxy: str) -> str:
    """Pick the pooled protocol fingerprint and bind *its* geo to this account.

    P1-1: the pool resolves the proxy's exit geo before handing back a profile
    (measuring it when the credential carries no region token).  This used to
    take only ``profile.name`` and drop the geo half, while the geo actually
    applied came from ``infer_proxy_country`` -- which only reads a region token
    in the proxy username.  A residential proxy has no such token, so it
    resolved to ``""`` and the account kept a US clock on, say, a Brazilian
    exit: the measurement was paid for and then ignored.
    """
    profile = None
    try:
        pool = _fingerprint_pool.shared_fingerprint_pool(config)
        if pool.size > 0:
            profile = pool.next(proxy)
    except Exception:
        profile = None
    if profile is not None:
        ops.set_fingerprint_geo(
            profile.country,
            timezone=profile.timezone,
            lang=profile.lang,
            lang_full=profile.lang_full,
        )
        set_auth_fingerprint(profile.name)
        return str(profile.country or "")
    else:
        country = str(_proxy_entry.infer_proxy_country(proxy) or "")
        ops.set_fingerprint_geo(country)
        return country


_LOGGER = logging.getLogger(__name__)


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


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
    - The file owns stage ordering; the *stages* themselves are not split into
      per-stage modules (they share ``self.runtime``/``self.r`` too tightly for
      that to be anything but an import cycle). What *is* extracted is the
      cohesive periphery -- ``registration_otp_stages`` (the OTP trio),
      ``registration_persistence`` (the storage seam),
      ``registration_stage_runner`` (stage execution) and
      ``registration_protocol_helpers`` (pure helpers) -- each re-exported here
      with a thin delegate so no caller changes. The enforced seam remains
      ``RegistrationOperations`` (workflow → provider) and ``store``
      (persistence); see ``docs/architecture.md`` "Dependency Direction".
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
        config = getattr(self, "config", None)
        registration = (config or {}).get("registration") if isinstance(config, Mapping) else {}
        fingerprint = registration.get("fingerprint_pool") if isinstance(registration, Mapping) else {}
        allowed = fingerprint.get("allowed_countries") if isinstance(fingerprint, Mapping) else ()
        proxy_metadata = getattr(self, "proxy_metadata", {}) or {}
        return {
            "fingerprint_country": getattr(self, "fingerprint_country", ""),
            "exit_country": proxy_metadata.get("actual_country", ""),
            # Batch preflight samples a pool entry, not this attempt's rotated session.
            "source": "preflight",
            "allowed_countries": allowed,
        }

    def run(self) -> dict[str, Any]:
        r = self.r
        r._tl().clear()
        r.select_auth_fingerprint(rotate=True)
        self._apply_reference_lane()
        config_scope = r.runtime_config_scope(self.config, workflow="registration")
        config_scope.__enter__()
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
            self._run_stage(RegistrationState.ACCESS_TOKEN_PROBE, "8d-Validate access token", self.probe_access_token)
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
            config_scope.__exit__(None, None, None)

    def _apply_reference_lane(self) -> None:
        """Overlay the ``registration.reference_lane`` preset before the config scope is entered."""
        base = self.config if self.config is not None else self.r.current_config_data()
        effective = _reference_lane.apply_reference_lane(base)
        if effective is base:
            return
        self.config = effective
        self.input_registration_mode = "password"
        _emit(_LOGGER, "[*] Reference lane: password registration in the reference project's stage order")

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
        return result

    def _with_password_claim(self, error: str) -> str:
        """Tag a failure that happened after ``user/register`` accepted the password.

        The stage after ``USER_REGISTER`` is only entered once that POST returned 200,
        and only the password lane POSTs it. From then on the address answers
        ``invalid_auth_step`` to every new session, so the batch must not retry it
        (``registration_policy``). Runs that already hold a token or an existing
        account are left to their own recovery path, and ``email_otp_send_stuck`` keeps
        its own guard/pulse semantics (quarantine TTL, arm-mismatch accounting).
        """
        s = self.runtime
        if PASSWORD_CLAIMED_MARKER in error or "email_otp_send_stuck" in error:
            return error
        if s.access_token or s.existing_account:
            return error
        history = getattr(self.machine, "history", None)
        if not isinstance(history, (list, tuple)):
            return error
        if not any(t.state is RegistrationState.EMAIL_OTP_SEND for t in history):
            return error
        if not self._password_lane_active():
            return error
        return f"{error}|{PASSWORD_CLAIMED_MARKER}"

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
        except (
            NameError,
            AttributeError,
            ImportError,
            KeyError,
            TypeError,
            IndexError,
            UnboundLocalError,
            NotImplementedError,
            RecursionError,
        ) as exc:
            # Programming/contract errors only. IndexError used to fall into the
            # transport catch-all below and get retried as a network failure --
            # the same "error name hides root cause" class as the RuntimeError
            # demotion fixed in error_classification. Keep this tuple in sync
            # with error_classification.INTERNAL_ERROR_MARKERS, which matches
            # the `{state}_internal:<Type>:` label text.
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
        """Wrap ``_bootstrap`` in the stage failure taxonomy.

        ``_bootstrap`` runs before the first ``RegistrationState`` stage, but
        its body makes the same kind of transport calls the wrapped stages do
        (proxy preflight, mailbox claim, mailbox snapshot). A transport
        failure there used to escape to ``run()``'s catch-all and be reported
        as ``registration_internal_error:RuntimeError:...`` — a name that
        means "code defect" for a class of failure that is a pure network
        event. Measured 2026-10-06 (runs 25e5aa9c / 91f8fc69 / 525df910):
        three such 30s silent deaths, all curl transport errors, all only
        recovered because the batch layer's ``network`` class happened to
        catch them. The failure goes through the same classification as every
        stage's transport arm instead.
        """
        r = self.r
        r._tick("0-Bootstrap")
        self._timing_open = True
        try:
            self._bootstrap()
        except (RegistrationAbort, RegistrationCancelled):
            raise
        except (
            NameError,
            AttributeError,
            ImportError,
            KeyError,
            TypeError,
            IndexError,
            UnboundLocalError,
            NotImplementedError,
            RecursionError,
        ) as exc:
            # Same tuple as ``_run_stage``: programming/contract errors keep
            # the internal label (see the comment there for why IndexError
            # must not fall into the transport arm).
            raise RegistrationAbort(f"bootstrap_internal:{type(exc).__name__}:{exc}") from exc
        except (MailboxEndpointUnavailableError, MailboxAuthInvalidError):
            # ``run()`` owns these two as a dedicated arm (their messages are
            # operator-facing); re-raise unchanged so that arm keeps them.
            raise
        except Exception as exc:
            raise RegistrationAbort(f"bootstrap_transport:{exc}") from exc
        finally:
            if self._timing_open:
                r._safe_tock()
                self._timing_open = False

    def _bootstrap(self) -> None:
        r = self.r
        s = self.runtime
        if self.config is None:
            self.config = r.current_config_data()
        s.email_cfg = dict(self.config.get("email_registration") or {})
        r.validate_config(self.config, workflow="registration")
        s.proxy = r._resolve_proxy_scheme(self.input_proxy, cfg=self.config)
        preflight = r.registration_network_preflight(proxy=s.proxy, proxy_attempts=2)
        s.proxy = str(preflight.get("proxy") or s.proxy or "")
        s.mailbox = r._ensure_mailbox_account(self.input_mailbox)
        if not s.mailbox or not s.mailbox.email:
            self._abort("mailbox_required")
        s.username = str(getattr(s.mailbox, "email", "") or "").strip()
        s.resume_checkpoint = (
            registration_checkpoint.load_resumable_checkpoint(self.persistence, s.username, self.config) or {}
        )
        if not s.resume_checkpoint:
            self._persist_checkpoint("mailbox_ready")
        s.mailbox_service = _mailbox_service.MailboxService.create(self.config)
        chatgpt_cfg = self.config.get("chatgpt", {})
        s.auth_base = chatgpt_cfg.get("auth_base_url", endpoints.AUTH_BASE)
        s.chat_base = chatgpt_cfg.get("chat_base_url", endpoints.CHATGPT_BASE)
        r.set_fingerprint_geo(_proxy_entry.infer_proxy_country(s.proxy))
        self.machine.transition(RegistrationState.MAILBOX_READY)
        if s.resume_checkpoint:
            _emit(_LOGGER, "[*] Resumable post-create checkpoint found; skipping mailbox/OTP stages")
            return
        # Snapshot the mailbox here, at the earliest point the mailbox is known
        # and before any OTP can be issued.
        #
        # This used to run inside ``send_email_otp``.  That was too late: in
        # passwordless mode the OTP is sent by the earlier ``auth_flow``
        # (authorize) step, so by the time ``send_email_otp`` snapshotted, the
        # code mail was already visible on the forwarding page and got written
        # into ``seen_message_ids`` -- which ``_latest_email_otp_candidate``
        # then skips.  The poll drained its full 300s window while the code sat
        # in plain sight (2026-09-11, batch 5e32aa85: 10 of 12 runs failed).
        #
        # Taking the snapshot up front makes it a true "what was already in the
        # inbox before this attempt" marker, matching the reference design
        # where ``before_ids`` is captured during identity resolution.
        _emit(_LOGGER, "[*] Snapshotting mailbox (pre-OTP baseline)")
        r._snapshot_mailbox_message(s.mailbox, proxy=s.proxy)
        _emit(_LOGGER, "[*] ChatGPT Email Registration Started")
        self._run_stage(RegistrationState.SENTINEL, "0-Extract sentinel token", self.extract_sentinel)
        self._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", self.prepare_identity)

    def _device_identity_seeded_by_email_enabled(self) -> bool:
        """``registration.device_identity_seeded_by_email`` (default **False**), S1 / ``p2-1``."""
        return registration_flag(self.config, "device_identity_seeded_by_email", False)

    def _device_context(self, email: Any) -> dict[str, Any]:
        """The persisted device context, or the address-derived one when S1 is on.

        A stored id always wins: an address that already registered keeps the id
        its account row holds, so S1 only changes addresses that have *never*
        succeeded -- which is every current failure sample.
        """
        stored = dict(self.persistence.get_device_context(email) or {})
        if stored.get("device_id") or stored.get("auth_session_logging_id"):
            return stored
        if not self._device_identity_seeded_by_email_enabled():
            return stored
        seeded = seeded_device_identity(str(email or ""))
        if not seeded:
            return stored
        self._device_seeded = True
        return {**stored, **seeded}

    def extract_sentinel(self) -> None:
        r = self.r
        s = self.runtime
        from .sentinel import sentinel_backend

        if self.input_sentinel:
            _emit(_LOGGER, "[*] Using provided sentinel tokens")
            s.sentinel_data = self.input_sentinel
        elif sentinel_backend(self.config) == "legacy":
            s.sentinel_data = r._extract_sentinel(
                proxy=s.proxy,
                force_fresh=True,
                persist=False,
                browser_headless=self.browser_headless,
            )
        else:
            device_context = self._device_context(s.username)
            s.sentinel_data = {
                "oai_did": str(device_context.get("device_id") or uuid.uuid4()),
                "sentinel_source": "node_sdk_runner",
            }
        if not s.sentinel_data or not r._sentinel_device_id(s.sentinel_data):
            self._abort("sentinel_extract_failed")
        r.think_stage("post_sentinel")

    def prepare_identity(self) -> None:
        r = self.r
        s = self.runtime
        device_context = self._device_context(getattr(s.mailbox, "email", ""))
        stored_device_id = str(device_context.get("device_id") or "").strip()
        sentinel_device_id = str(r._sentinel_device_id(s.sentinel_data) or "").strip()
        if stored_device_id and stored_device_id != sentinel_device_id:
            from .sentinel import sentinel_backend

            if sentinel_backend(self.config) == "legacy" or self.input_sentinel:
                _emit(_LOGGER, "  [Device] Regenerating Sentinel tokens for persisted device context")
                s.sentinel_data = r._extract_sentinel(
                    proxy=s.proxy,
                    force_fresh=True,
                    persist=False,
                    browser_headless=self.browser_headless,
                    device_id=stored_device_id,
                )
                if not s.sentinel_data:
                    self._abort("sentinel_extract_failed: persisted device token refresh failed")
            else:
                s.sentinel_data = {
                    **dict(s.sentinel_data),
                    "oai_did": stored_device_id,
                }

        s.context = prepare_registration_context(
            proxy=s.proxy,
            mailbox=s.mailbox,
            sentinel_data=s.sentinel_data,
            password=self.input_password,
            registration_mode=self.input_registration_mode,
            auth_base=s.auth_base,
            chat_base=s.chat_base,
            stored_password=r._stored_registration_password,
            generate_password=r._generate_password,
            random_name=r._random_name,
            random_birthdate=r._random_birthdate,
            normalize_mode=r._normalize_registration_mode,
            get_device_context=self._device_context,
            sentinel_device_id=r._sentinel_device_id,
            new_uuid=lambda: str(uuid.uuid4()),
            browser_headless=self.browser_headless,
        )
        c = s.context
        s.username = c.username
        s.password = c.password
        s.full_name = c.full_name
        s.birthdate = c.birthdate
        s.registration_mode = c.registration_mode
        s.device_id = c.device_id
        s.session_logging_id = c.session_logging_id
        s.flow_invocation_id = str(uuid.uuid4())
        self.browser_headless = c.browser_headless
        s.sentinel_token = str(s.sentinel_data.get("sentinel_token") or "")
        s.sentinel_authorize_token = str(s.sentinel_data.get("sentinel_authorize_continue_token") or "")
        s.sentinel_so_token = str(s.sentinel_data.get("sentinel_so_token") or "")
        try:
            r.assert_sentinel_device_id(s.sentinel_data, s.device_id)
        except ValueError as exc:
            self._abort(str(exc))
        # A seeded id arrives through the same context as a stored one, but it is
        # not a *reuse*: nothing was persisted for this address.
        reused = bool(c.reused_device_context) and not self._device_seeded
        seeded = "email" if self._device_seeded else "none"
        if reused:
            _emit(_LOGGER, "  [Device] Reusing persisted device context")
        self.device_identity = {"oai_did": s.device_id, "reused": reused, "seeded": seeded}
        # ``emit``, not ``print``: the device-layer A/B (plan-2026-10-10) reads these
        # lines from ``sms_tool.log``.  Only the first 8 characters of ``oai-did`` are
        # ever written; it is a credential-level identifier.
        if self._device_identity_seeded_by_email_enabled():
            # Unconditional inside the toggle branch (the P1-7 lesson): the marker
            # says the arm was *armed*, even for an address whose stored id won.
            _emit(_LOGGER, "  Device identity seeded by email")
        _emit(
            _LOGGER,
            "  Device identity: oai-did=%s reused=%s seeded=%s",
            s.device_id[:8],
            str(reused).lower(),
            seeded,
        )
        _emit(
            _LOGGER, "[*] Username: %s  Password: [stored]  Name: %s  Birth: %s", s.username, s.full_name, s.birthdate
        )
        self._persist_checkpoint("identity_ready")
        s.session = _new_registration_session(s.proxy)
        self._install_edge_challenge_hook(s.session)
        if s.registration_mode == "passwordless":
            # Keep the Web/NextAuth flow isolated from the Sentinel extraction
            # prime session. Importing its auth.openai.com login cookies creates
            # a stale login transaction and routes authorize to /log-in/password.
            r._set_oai_did_cookie(s.session, s.device_id)
        else:
            r._import_sentinel_cookies(s.session, s.sentinel_data, s.device_id)
        r.set_fingerprint_device(s.device_id)
        self.fingerprint_country = _apply_protocol_fingerprint(r, self.config, s.proxy)
        s.base_headers = r.openai_auth_headers(
            s.device_id,
            accept="application/json",
            include_trace=True,
            session_id=s.session_logging_id,
            flow_invocation_id=s.flow_invocation_id,
        )
        if str(s.base_headers.get("oai-device-id") or "") != s.device_id:
            self._abort("sentinel_extract_failed: auth header device id mismatch")
        self._prime_password_sentinel_bundle()
        s.auth_flow_started = _safe_int(time.time())

    def _reference_lane_active(self) -> bool:
        return _reference_lane.reference_lane_enabled(self.config)

    def _password_sentinel_bundle_enabled(self) -> bool:
        return _sentinel_stages.password_sentinel_bundle_enabled(self)

    def _prime_password_sentinel_bundle(self) -> None:
        return _sentinel_stages.prime_password_sentinel_bundle(self)

    def _issue_sentinel(self, flow: str, *, force_fresh: bool = False) -> Any:
        return _sentinel_stages.issue_sentinel(self, flow, force_fresh=force_fresh)

    def auth_flow(self) -> None:
        r = self.r
        s = self.runtime
        s.auth_flow_started = _safe_int(time.time())
        if s.registration_mode == "passwordless":
            r.request_with_retry(
                s.session,
                "get",
                f"{s.chat_base}/",
                label="ChatGPT prime",
                headers={
                    **r.chatgpt_headers(
                        s.device_id,
                        session_id=s.session_logging_id,
                        flow_invocation_id=s.flow_invocation_id,
                        accept="text/html,application/xhtml+xml",
                        referer=f"{s.chat_base}/",
                    )
                },
                impersonate=r.auth_impersonate(),
                attempts=1,
            )
        elif self._reference_lane_active():
            _reference_lane.pre_signin(self)
        else:
            r.request_with_retry(
                s.session,
                "get",
                f"{s.auth_base}/create-account",
                label="Auth prime",
                headers={**s.base_headers, "Accept": "text/html,application/xhtml+xml"},
                impersonate=r.auth_impersonate(),
            )
        csrf_resp = r.request_with_retry(
            s.session,
            "get",
            f"{s.chat_base}/api/auth/csrf",
            label="Auth csrf",
            headers=r.nextauth_headers(
                s.device_id,
                session_id=s.session_logging_id,
                referer=_reference_lane.LOGIN_PAGE_REFERER if self._reference_lane_active() else f"{s.chat_base}/",
                origin=s.chat_base,
            ),
            impersonate=r.auth_impersonate(),
        )
        s.csrf_token = (r._json_or_raw(csrf_resp).get("csrfToken") or "").strip()
        s.signup_state = r._prepare_signup_auth_state(
            s.session,
            s.username,
            s.device_id,
            s.session_logging_id,
            s.auth_base,
            s.chat_base,
            s.base_headers,
            s.csrf_token,
            sentinel_token=s.sentinel_token,
            authorize_sentinel_token=s.sentinel_authorize_token,
            sentinel_so_token=s.sentinel_so_token,
            proxy=s.proxy,
            passwordless_web=s.registration_mode == "passwordless",
            attempts=r._passwordless_signin_attempts()
            if s.registration_mode == "passwordless"
            else r._signup_signin_attempts(),
            **({"reference_navigation": True} if self._reference_lane_active() else {}),
        )
        # P0-1 判据 A 的**取证埋点**（先扩样本，暂不硬止损）。
        #
        # ``passwordless_login_magic_link_sent`` 出现在**取码之前**，意味着服务端
        # 把这次 authorize 当成**登录**处理 ⇒ 该地址已存在，注册必然以
        # ``user_already_exists`` 收场。实测（批次 25288）精确率 3/3，但召回率只有
        # 3/11 —— n 太小，还不能拿它去写死路账本（误判的代价是把一个可注册地址
        # 永久拉黑）。所以这里只保留 dump + 打一行**每 run 可归因**的判定，让后续
        # 批次把精确率/召回率补到能拍板为止。
        #
        # 不要用日志里的 ``client_auth_session_dump`` 行做这件事：那些行按 stage
        # 做进程级降噪，跨 run 共享（见 ``auth_state._LAST_DUMP_TEXT``）。
        signup_dump = r._fetch_client_auth_session_dump(s.session, s.auth_base, s.base_headers, "after_signup_state")
        s.signup_dump = signup_dump if isinstance(signup_dump, dict) else {}
        s.signup_lane = signup_lane_verdict(s.signup_dump)
        if s.signup_lane == "login":
            _emit(_LOGGER, "  Signup lane hint: login (transaction says this address already has a login magic link)")
        if _safe_int(s.signup_state.get("status") or 0) == 429:
            retry_after = _safe_float(s.signup_state.get("retry_after_seconds") or 300, 300.0)
            _registration_concurrency.mark_registration_rate_limited(retry_after)
            self._abort(f"registration_rate_limited:retry_after={retry_after:.0f}s")
        if not s.signup_state.get("ok"):
            self._abort(f"signup_auth_state:{json.dumps(s.signup_state, ensure_ascii=False)[:300]}")
        if r._is_chatgpt_auth_login_landing(s.signup_state.get("url", "")):
            self._abort("signup_auth_state:redirected_to_chatgpt_login")
        self._persist_checkpoint("auth_flow")

    def _password_lane_active(self) -> bool:
        """True when this run must go through the password step.

        Two independent ways in, and they are **not** the same condition:

        * ``registration_mode != "passwordless"`` -- the operator opted into
          password-first registration (``email_registration.registration_mode``).
        * ``password_fallback`` -- the **server** moved the transaction to
          ``/log-in/password`` (``auth_flow._is_existing_login_redirect``), or the
          current step URL already is the password step.

        ``user_register`` and ``send_email_otp`` both branch on this.  It used to
        be written out twice as
        ``bool(signup_state.get("password_fallback")) or _is_signup_password_step(...)``;
        a single owner keeps the two call sites from drifting, and it removes the
        duplicated expression that made text-anchored mutation specs ambiguous.
        """
        s = self.runtime
        if s.registration_mode != "passwordless":
            return True
        return bool(s.signup_state.get("password_fallback")) or self.r._is_signup_password_step(
            s.signup_state.get("url", "")
        )

    def user_register(self) -> None:
        s = self.runtime
        if not self._password_lane_active():
            self._user_register_passwordless()
            return
        # Navigation-first: establish the password page *before* the POST that
        # claims it.  Gated because it adds a request to a lane whose current
        # failure is one step later; see the switch's docstring for the
        # measurement and for what it is meant to separate.
        if self.r._prime_create_account_password_page_enabled():
            prime = self.r._prime_create_account_password_page(
                s.session,
                s.auth_base,
                s.base_headers,
                str(s.signup_state.get("url") or ""),
            )
            # ``registration.prime_password_page_fatal`` (turb's contract): the
            # prime reports a wrong landing as ``fatal`` so this caller can stop
            # before the POST that claims a page state which was never
            # established.  Off by default -> the report-only behaviour stands.
            if isinstance(prime, Mapping) and prime.get("fatal"):
                self._abort(f"password_page_not_reached:{str(prime.get('url') or '')[:120]}")
        if _reference_lane.reference_lane_enabled(self.config):
            _reference_lane.prime_password_bundle(self)
        self._post_user_register()
        if s.reg_response.status_code != 200:
            self._abort_user_register_failure()

    def _user_register_passwordless(self) -> None:
        """Passwordless lane: record the signup state instead of POSTing ``user/register``."""
        s = self.runtime
        # The passwordless lane must not POST ``user/register``.
        #
        # It used to probe it first (shipped 2026-09-15) so an
        # already-registered address would be recognised before a mailbox
        # poll and a spent email code.  That probe was removed the next day.
        #
        #   * 🔴 The destructiveness is **not** in the POST -- it is in
        #     ignoring the POST's *response*.  The 200 body carries its own
        #     follow instructions:
        #         {"continue_url": ".../api/accounts/email-otp/send",
        #          "method": "GET", "page": {"type": "email_otp_send"}}
        #     The probe never followed it -- ``[4-Trigger email OTP]`` took
        #     the synthetic ``assumed_pre_sent`` branch below -- so every
        #     later ``email-otp/validate`` answered 409 "Your sign-in session
        #     is no longer valid".  Measured on batch 28260: 89 accepted
        #     probes -> 85 such 409s, against 75 of 77 rejected probes
        #     validating fine.
        #     Confirmed 2026-09-16 against a natural control group built on
        #     the *same* ``_post_user_register`` payload: follow the response
        #     -> 0 of 12 validations 409 (and all 12 finalized); ignore it ->
        #     85 of 85.  Fisher one-sided p = 1.4e-15.  See
        #     ``docs/audits/plans/plan-2026-09-16-password-first-registration.md``
        #     §2.0.  The password lane below already follows the response:
        #     ``send_email_otp`` -> ``_email_otp_send_url`` ->
        #     ``_follow_continue_url``, which is a GET and therefore matches
        #     the ``"method": "GET"`` the server asks for.
        #   * It never answered the question it was built for: across 168
        #     probe runs ``user/register`` returned ``user_already_exists``
        #     **zero** times, while ``create_account`` returned it 70 times
        #     in that same batch.  ``user/register`` is not an existence
        #     oracle, so no response-shape logic could have rescued it.
        #
        # Do not reintroduce the probe **as an existence oracle** -- that part
        # is settled.  Raw counts live in the 2026-09-15 work log.
        s.reg_data = {
            "mode": "passwordless_signup",
            "auth_state": {
                "attempt": s.signup_state.get("attempt", ""),
                "url": s.signup_state.get("url", ""),
                "status": s.signup_state.get("status", 0),
            },
        }
        s.password_unknown = True
        _emit(_LOGGER, "  Registration mode: passwordless_signup (HAR login_or_signup)")

    def _abort_user_register_failure(self) -> None:
        """Classify a non-200 ``user/register`` answer and abort with the matching error."""
        s = self.runtime
        err_code = s.reg_data.get("error", {}).get("code", "")
        err_msg = s.reg_data.get("error", {}).get("message", str(s.reg_data))
        state_url = str(s.signup_state.get("url") or "")
        if err_code == "invalid_auth_step" and "email-verification" in state_url:
            # 🔴 2026-09-16 拍板：密码泳道**失败即 abort，不回落 passwordless**。
            #
            # ``invalid_auth_step`` 的意思是「事务不在密码步」——服务端
            # **没有接受我们的密码**。再往下走就是 ``create_account``，那会建出
            # 一个**无密码账号**，而「不产生无密码账号」正是密码优先模式存在的
            # 全部理由（aBai 同一处选择 ``raise``：宁可不注册）。
            #
            # 与下面 ``else`` 分支的唯一区别是**时机**：这里在发码之前就停手，
            # 所以不会为一个注定要被拒绝的地址烧掉一个邮箱 OTP。
            # passwordless 泳道**不受影响** —— 它的门禁在
            # ``_password_lane_active()`` 那里就返回了，根本走不到这个分支。
            #
            # 🔴 这里原先还有一条恢复路径（``print("...resuming OTP step...")``
            # + ``s.resume_email_verification = True``），已删除。三条独立理由，
            # 任何一条单独成立就足以删掉它：
            #
            #   1. **它已不可达。** 函数入口的 ``_password_lane_active()`` 门禁
            #      为假时直接 ``return``，而 ``_post_user_register()`` 既不写
            #      ``registration_mode`` 也不写 ``signup_state`` ⇒ 走到这里时该
            #      谓词必然仍为真。旧写法把同一个谓词又判了一遍，那个 ``if``
            #      恒真、``else`` 之后的恢复分支恒不可达。
            #   2. **它本来就是我们要禁的那条路。** 那个标志只改 OTP 的发码
            #      端点，**不跳过** ``run()`` 的第 7 步 ``create_account``
            #      ⇒ 恢复下去照样建出无密码账号。
            #   3. **即使不建号也不值得做。** 对已存在账号，email OTP 能走通但
            #      ``continue_url`` 仍落 ``/about-you``，NextAuth session cookie
            #      **从不下发**（2026-09-14 实测 5/5）⇒ 恢复 = 白烧一个 OTP。
            #
            # 零成本佐证：全量留存日志（5919 个 .log/.jsonl/.txt/.json，含
            # ``runtime/logs/processes/*/``）里 ``resuming OTP step`` **0 次命中**
            # ⇒ 删除不改变任何已观测行为。
            #
            # ✅ 2026-09-16 收尾（老板拍板）：该标志 ``resume_email_verification``
            # **已整体删除** —— 状态字段、两处读点、以及 ``_email_otp_send_url``
            # 里那个回落分支和它唯一的消费者 ``auth_base`` 参数。此前它挂在
            # ``READ_BUT_NEVER_WRITTEN`` 里「待拍板」，现在白名单只剩
            # ``phone_result`` 一条。要复活它，先推翻 ``account_creation``
            # 那句「缺 ``continue_url`` 必须报错，不许猜端点」。
            #
            # ``err_code`` 在这里**必然**等于 ``"invalid_auth_step"``（上面那个
            # ``if`` 的条件），所以不要写成 ``err_code or f"http_{...}"`` ——
            # 那个回退是死代码。仍然插值 ``err_code`` 而不是写死字符串，是为了
            # 将来放宽条件（例如把 ``invalid_state`` 也收进来）时后缀自动跟随。
            self._abort(f"password_step_unconfirmed:{err_code}")
        elif err_code == "invalid_auth_step" and self.r._is_existing_login_redirect(state_url):
            # 🔴 2026-09-16（P1）：服务端把这次注册**路由到了登录页** ——
            # 这是「地址已存在」的**第三种、也是最早的一种**表达方式。
            #
            # 前两种都出现在 ``create_account``（``user_already_exists`` /
            # ``identity_provider_mismatch``，见 ``DEAD_END_MARKERS``）；这一种
            # 提前到了 ``login_or_signup`` 的**路由落点**：服务端直接把事务送进
            # ``/log-in/password``，随后对 ``user/register`` 回 400
            # ``invalid_auth_step``。
            #
            # 生产实测（2026-09-16 两批）：``login_or_signup`` 落
            # ``/log-in/password`` 的账号**全部**以这个 code 收场 ——
            # 批次 34632 是 21/21，批次 28512 是 10/10，合计 **31/31，0 例外**；
            # 同期落 ``/email-verification`` 的那个成功了。落点与结果完全分离，
            # 且两批共用同一个出口（预检都选中 ``global.9http.com:9091``）
            # ⇒ 这是**地址**属性，不是出口、也不是时间窗。
            #
            # 为什么不能沿用上面那条 ``password_step_unconfirmed``：那条是
            # ``auth_state``（``retryable`` + ``batch_retry``），前提是「什么都没
            # 被消费，下一批换个出口再试」。这里正相反 —— 服务端已明说地址归它
            # 所有，重试只会把整个握手重走一遍再拿到同一个答案。归 ``account``
            # （``batch_dropped``）才对，也不会误伤：本批唯一的
            # ``/email-verification`` 落点碰不到这条分支。
            #
            # 🔴 判据是 ``_is_existing_login_redirect``（``/log-in*``），**不是**
            # ``_is_signup_password_step``（``/create-account/password``）。两者都
            # 会让 ``_password_lane_active`` 为真，但只有前者表达「地址已存在」；
            # 后者落进下面的 ``else``，保持既有行为。
            #
            # ``existing_account`` 置真会让 ``_abort_result`` 把终态装成
            # ``partial_registered`` —— 与 09-15 那 305 个 ``user_already_exists``
            # 同类，只是发现得更早、不烧 OTP。
            s.existing_account = True
            self._mark_partial_registration(reason=DEAD_END_SIGNUP_ROUTED_TO_LOGIN)
            self._abort(f"existing_account_{DEAD_END_SIGNUP_ROUTED_TO_LOGIN}")
        else:
            # 🔴 2026-09-16（P0）：拼 **code**，不要拼 message。
            #
            # 分类器（``failure_registry`` / ``error_classification``）按 **code**
            # 形式做子串匹配，marker 表里写的是 ``invalid_auth_step``。原先这里拼
            # ``err_msg``，串里只有 ``Invalid authorization step.`` —— 两者**没有
            # 共同子串**，分类掉进兜底类 ``unknown``；而 ``unknown`` 既不在
            # ``BATCH_RETRY_CLASSES`` 也不在 ``BATCH_DROPPED_CLASSES`` ⇒ 不重试、
            # 不记掉号、不告警，**整批静默蒸发**。
            #
            # 生产实测（批次 34632，2026-09-16 11:00）：21 个地址全部
            # ``user_register:Invalid authorization step.`` +
            # ``failure_class=unknown``，成功率 1/22，而日志上只留一行看不出原因
            # 的错误。同一个响应，改拼 ``user_register:invalid_auth_step`` 后分类
            # 是 ``auth_state``（可重试）。
            #
            # ``err_code or err_msg``：code 缺失（服务端只给 message）时仍保留
            # 服务端原文，不丢诊断信息。Pinned by
            # ``tests/registration/test_user_register_response_contract.py``.
            self._abort(f"user_register:{err_code or err_msg}")

    def _post_user_register(self) -> Any:
        """POST ``user/register`` (password + username) and stash the parsed body.

        Only the password lane calls this.  The passwordless lane must not: an
        accepted POST advances the server-side auth transaction, and the email
        OTP that follows then validates against a session the server has already
        moved past.  See ``user_register`` for the measured counts.
        """
        r = self.r
        s = self.runtime
        username_sentinel = self._issue_sentinel("username_password_create")
        s.reg_response = r.request_with_retry(
            s.session,
            "post",
            f"{s.auth_base}/api/accounts/user/register",
            label="User register",
            json={"password": s.password, "username": s.username},
            headers=r._auth_request_headers(
                s.base_headers,
                did=s.device_id,
                referer=f"{s.auth_base}/create-account/password",
                origin=s.auth_base,
                sentinel_token=username_sentinel.token,
                sentinel_so_token=_str_or_empty(getattr(username_sentinel, "so_token", "")),
            ),
            impersonate=r.auth_impersonate(),
        )
        try:
            s.reg_data = s.reg_response.json()
        except (ValueError, TypeError):
            s.reg_data = {"_raw": s.reg_response.text[:_USER_REGISTER_PRINT_BUDGET]}
        _emit(_LOGGER, "  Status: %s", s.reg_response.status_code)
        # 1200, not 300: the 200 body's ``oai-client-auth-session`` carries
        # ``email_verification_mode`` in cleartext, and the server switches it
        # between the signup-state value (passwordless_signup) and a third,
        # still-unnamed 10-char enum right at the send step (measured 2026-10-07,
        # batch p1_5_hint_arm_5addr: the enhanced dump shows
        # "[REDACTED](len=10)" at after_otp_send). The 300 budget cut the wire
        # body at exactly the country_code_hint key, before the mode key -- so
        # the one value that would name the transaction arm never reached the
        # log. The sanitizer still redacts secrets inside this line; only the
        # print budget changes.
        _emit(
            _LOGGER,
            "  Response: %s",
            r._sanitize_text(json.dumps(s.reg_data, ensure_ascii=False)[:_USER_REGISTER_PRINT_BUDGET]),
        )
        return s.reg_response

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
