"""Account creation, session readiness, and access-token probe stages.

Split out of ``registration_handlers`` so the orchestrator module owns stage
ordering and workflow lifecycle, while this module owns the post-OTP account
creation and session/AT verification stages:

* ``prime_about_you_page_enabled`` / ``prime_about_you_page`` -- optional
  ``/about-you`` navigation prime before ``create_account``
* ``create_account_disallowed_backoff_delays`` / ``post_create_account_with_backoff``
  -- bounded retry ladder for ``registration_disallowed`` with fresh Sentinel proofs
* ``create_account`` / ``record_create_account_response`` / ``mark_partial_registration``
  -- ``create_account`` wire execution, response folding, and dead-end ledger marking
* ``fetch_auth_session`` -- NextAuth session fetch and existing-account login fallback
* ``probe_access_token`` -- post-session access token validation probe

Collaborators are reached via the workflow instance passed as ``workflow`` (its
``runtime``, ``r`` operations interface, and delegating methods), never by
importing ``registration_handlers`` at runtime, avoiding any import cycle. The
parameter is annotated with ``RegistrationEmailWorkflow`` under ``TYPE_CHECKING``
so the type checker validates every attribute and delegate the stages touch.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from curl_cffi import requests as curl_requests

from . import registration_checkpoint
from .desktop_ipc import emit_event
from .kernel.failure_registry import PASSWORDLESS_SIGNUP_CODE, is_passwordless_signup_mismatch
from .operator_output import emit as _emit
from .registration_cancel import RegistrationCancelled, cancellable_sleep
from .registration_flags import registration_flag
from .registration_protocol_helpers import (
    _create_account_response_line,
    _is_existing_login_dead_end_error,
    _login_probe_password,
    _safe_int,
)
from .registration_retry_guard import RegistrationRetryGuard
from .kernel.sanitizer import account_reference, describe_exception
from .telemetry import current_run_id

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .registration_handlers import RegistrationEmailWorkflow

#: Keep the ``sms_tool.registration_handlers`` logger channel so ``sms_tool.log``
#: category tagging (``注册``) and ``assertLogs("sms_tool.registration_handlers")``
#: contract tests observe the same stream after the module split.
_LOGGER = logging.getLogger("sms_tool.registration_handlers")


def prime_about_you_page_enabled(workflow: RegistrationEmailWorkflow) -> bool:
    """``registration.prime_about_you_page`` (default **False**).

    **Why this exists.**  ``create_account`` POSTs with
    ``Referer: {auth_base}/about-you`` -- the request *claims* the profile
    page state, but nothing ever navigates there.  This is the same shape
    of gap the password page had (``_prime_create_account_password_page``):
    a Referer asserting a page state that no GET established.  turb's
    protocol client navigates to about-you first and says why
    (``navigate_about_you``: "先真实导航到 about-you，让 auth session/page
    state 与 create_account 一致"), and SunnyRegister issues a
    ``client_auth_session_dump`` GET for the same purpose.

    Off by default: create_account currently answers 200 on healthy runs;
    adding a request to a working stage needs its own A/B
    (``p1-6-prime-about-you-page``) first.  Non-fatal by contract: an
    unexpected landing is reported, never raised, so this cannot turn a
    healthy stage into a new failure mode.
    """
    return registration_flag(workflow.config, "prime_about_you_page", False)


def prime_about_you_page(workflow: RegistrationEmailWorkflow) -> None:
    """GET ``/about-you`` so the profile-page state exists before the POST.

    Only the navigation gap turb closes with ``navigate_about_you``; the
    OTP continue step may already have landed here, in which case the GET
    is idempotent navigation, not a new state.  Deliberately **non-fatal**:
    the POST's own verdict is the contract, and a failed prime must not
    abort a stage that works without it.
    """
    s = workflow.runtime
    try:
        response = workflow.r._follow_continue_url(
            s.session,
            f"{s.auth_base}/about-you",
            s.base_headers,
            referer=f"{s.auth_base}/email-verification",
            label="About you page prime",
        )
        final_url = str(getattr(response, "url", "") or "")
        if "/about-you" not in final_url:
            _emit(_LOGGER, "  About you page prime landed off the profile step: %s", final_url[:120])
    except Exception as exc:
        _emit(_LOGGER, "  About you page prime warning: %s", describe_exception(exc))


def create_account_disallowed_backoff_delays(workflow: RegistrationEmailWorkflow) -> tuple[int, ...]:
    """Bounded retry delays for ``registration_disallowed`` on create_account.

    ``registration.create_account_disallowed_backoff`` (default **False**).

    **Why this exists.**  SunnyRegister ``_create_account`` measures that
    OpenAI can temporarily reject a fresh registration right after the OTP
    is accepted while its IP/Sentinel risk window settles, and Remail
    addresses are especially sensitive to that window; its protocol client
    retries with a bounded long backoff (``[8, 20, 45]`` seconds, refreshing
    the Sentinel proof each round) and only for that mailbox family.
    This repo classifies ``registration_disallowed`` as the terminal
    ``account`` class (``failure_registry``), so one transient rejection
    permanently dead-ends the address.

    Off by default: the classification as ``account`` is the current
    contract and every retry spends a Sentinel proof; whether the risk
    window is real on this repo's exits needs its own A/B
    (``p1-7-create-disallowed-backoff``).  When enabled, the retry keeps
    the same ``account``-class terminal verdict if the final attempt also
    fails -- the toggle changes *when* the verdict is reached, never *what*
    it eventually says.
    """
    if not registration_flag(workflow.config, "create_account_disallowed_backoff", False):
        return ()
    return (8, 20, 45)


def create_account(workflow: RegistrationEmailWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    # ``RegistrationRuntimeState`` attaches its flat fields from the group
    # dataclasses at import time (see ``registration_runtime``), so read this
    # one through ``getattr`` with an explicit ``str`` guard: a state double
    # that models only the fields it touches stays valid, and the access does
    # not depend on the ``TYPE_CHECKING`` mirror or on a ``Mock`` attribute
    # (which would be truthy).
    external_url = getattr(s, "otp_external_url", "")
    if isinstance(external_url, str) and external_url:
        # ``registration.otp_external_url_branch``: the validated OTP
        # transaction already finished on a callback/external URL (the
        # ``Email OTP continue`` step followed it), so POSTing
        # ``create_account`` next is what turb guards against -- it answers
        # ``invalid_auth_step`` for a transaction the server considers done.
        # Mark the create step acknowledged and let ``fetch_auth_session``
        # read the NextAuth session the callback established.
        _emit(_LOGGER, "  Create account: skipped, the OTP transaction already finished on an external URL")
        s.create_ok = True
        s.create_data = {"_external_url": external_url}
        r.think_stage("post_create_account")
        return
    if workflow._prime_about_you_page_enabled():
        workflow._prime_about_you_page()
    response = workflow._post_create_account_with_backoff()
    workflow._record_create_account_response(response)
    try:
        r._follow_continue_url(
            s.session,
            r._create_account_continue_url(s.create_data),
            s.base_headers,
            referer=f"{s.auth_base}/about-you",
            label="Create account continue",
        )
    except Exception as exc:
        _emit(_LOGGER, "  Create account continue transport warning: %s", describe_exception(exc))


def post_create_account_with_backoff(
    workflow: RegistrationEmailWorkflow,
    *,
    sleep_fn: Callable[[float], bool] = cancellable_sleep,
) -> Any:
    """POST ``create_account``, retrying ``registration_disallowed`` per the backoff flag."""
    r = workflow.r
    s = workflow.runtime
    backoff_delays = workflow._create_account_disallowed_backoff_delays()
    response = None
    create_sentinel = None
    for attempt in range(len(backoff_delays) + 1):
        # Round 0 may consume a pre-minted bundle token; every retry round
        # must mint a genuinely fresh proof (see ``_issue_sentinel``).
        create_sentinel = workflow._issue_sentinel("oauth_create_account", force_fresh=attempt > 0)
        response = r.request_with_retry(
            s.session,
            "post",
            f"{s.auth_base}/api/accounts/create_account",
            label="Create account",
            json={"name": s.full_name, "birthdate": s.birthdate},
            headers=r._auth_request_headers(
                s.base_headers,
                did=s.device_id,
                referer=f"{s.auth_base}/about-you",
                origin=s.auth_base,
                sentinel_token=create_sentinel.token,
                sentinel_so_token=create_sentinel.so_token,
            ),
            impersonate=r.auth_impersonate(),
        )
        if response.status_code == 200 or attempt >= len(backoff_delays):
            break
        body = ""
        try:
            body = json.dumps(r._json_or_raw(response, limit=600), ensure_ascii=False)
        except Exception:
            body = str(getattr(response, "text", "") or "")[:600]
        if "registration_disallowed" not in body:
            break
        delay = backoff_delays[attempt]
        # ``emit``, not ``print``: this is P1-7's registered mechanism
        # marker, and a bare ``print`` only reaches the stdout mirror
        # (``backend_stdout.jsonl``) while the runbook's ``collect`` reads
        # ``sms_tool.log``.  ``emit`` feeds both channels from one call.
        _emit(
            _LOGGER,
            "  Create account temporarily disallowed; retrying in %ss with a fresh Sentinel proof (%s/%s)",
            delay,
            attempt + 1,
            len(backoff_delays),
        )
        if sleep_fn(delay):
            raise RegistrationCancelled()
    # The loop always runs at least once, so the type-checker can't infer
    # that ``response`` is bound here; name the invariant instead of
    # sprinkling Optional guards through the parsing block below.
    assert response is not None and create_sentinel is not None
    return response


def record_create_account_response(workflow: RegistrationEmailWorkflow, response: Any) -> None:
    """Fold the ``create_account`` answer into the runtime state and dead-end ledger."""
    r = workflow.r
    s = workflow.runtime
    try:
        s.create_data = response.json()
    except (ValueError, TypeError):
        s.create_data = {"_raw": response.text[:300]}
    _emit(_LOGGER, "  Status: %s", response.status_code)
    # 600, not 300: ``user_already_exists`` carries a structured
    # ``userAlreadyExistsRecovery`` object past the 300-char cut, and that
    # object is the only place the server states the recovery action.
    # Measured 2026-09-14 -- the truncated line read
    # ``"action": "continue_to_`` with no way to see the rest.
    # A 200 keeps the same budget but spends it differently; see
    # ``_create_account_response_line``.
    _emit(
        _LOGGER,
        "  Response: %s",
        _create_account_response_line(response.status_code, s.create_data, r._sanitize_text),
    )
    s.create_ok = response.status_code == 200
    r.think_stage("post_create_account")
    s.existing_account = r._is_user_already_exists(s.create_data)
    if s.existing_account:
        workflow._mark_partial_registration()
    elif is_passwordless_signup_mismatch(s.create_data):
        # 🔴 The **second** code the server uses for "this address is already
        # registered", measured 2026-09-15 07:33:37 (run ``b93f598d``):
        # ``create_account`` answered 400 ``identity_provider_mismatch`` for
        # an address whose signup record used a non-password method.
        #
        # ``_is_user_already_exists`` only knows ``user_already_exists``, so
        # without this branch the address got **none** of the three
        # protections: no ``partial_registered`` state, no dead-end ledger
        # row, and the reported cause was classified ``unknown`` (advice
        # empty) -- so the next batch re-drove it and burned a fresh email
        # OTP to rediscover the same fact.
        #
        # 🔴 Deliberately **not** setting ``s.existing_account = True`` here.
        # That flag flips ``create_ok`` to ``True`` twenty lines below and
        # ``_create_account_error`` then returns ``""``, so the run would
        # blame whatever the re-login fallback last hit -- exactly the
        # mis-attribution ``registration_outcome._existing_account_error``
        # documents (three 09-06 addresses reported as
        # ``existing_login_otp_send_failed:429`` while the signup lane had
        # already answered).  Keeping the flag ``False`` preserves
        # ``create_account_failed:identity_provider_mismatch: …`` as the
        # reported cause while still taking the two protections we want.
        #
        # Zero risk of killing a fresh address: this code can only come back
        # from ``create_account`` on an address the server already has a
        # signup record for.
        workflow._mark_partial_registration(reason=PASSWORDLESS_SIGNUP_CODE)
    c = s.context
    if s.create_ok and not s.existing_account:
        s.session_recovery_started_at = _safe_int(time.time())
        workflow._persist_checkpoint(registration_checkpoint.SESSION_PENDING_STATE)
    if not s.create_ok and s.existing_account:
        # The server has stated this address is already registered.  Keep
        # ``create_ok`` true so the outcome wording stays
        # ``existing_account_user_already_exists:continue_to_login``.
        _emit(
            _LOGGER,
            "  Account already exists; the generated password is not evidence "
            "about this account (no stored credential is modified).",
        )
        s.create_ok = True
        s.password_unknown = True
        s.existing_account_password_known = bool(c is not None and (c.explicit_password or c.password_from_storage))


def mark_partial_registration(
    workflow: RegistrationEmailWorkflow,
    *,
    reason: str = "user_already_exists",
    retry_guard_cls: Any = RegistrationRetryGuard,
) -> None:
    """Record the server's existence verdict as a permanent dead end."""
    s = workflow.runtime
    retry_guard_cls(workflow.config).mark_dead_end(s.username, reason=reason)
    _LOGGER.info(
        "Server reports existing account; registration_status=partial_registered",
        extra={"event": "registration_status_changed", "account_ref": account_reference(s.username)},
    )
    emit_event(
        {
            "domain": "registration",
            "operation": "registration",
            "stage": "registration_status_changed",
            "status": "running",
            "detail": "半注册",
            "registration_status": "partial_registered",
            "account_ref": account_reference(s.username),
            "run_id": current_run_id.get(),
        }
    )


def fetch_auth_session(
    workflow: RegistrationEmailWorkflow,
    *,
    retry_guard_cls: Any = RegistrationRetryGuard,
) -> None:
    r = workflow.r
    s = workflow.runtime
    s.auth_session = r._fetch_auth_session(s.session, s.chat_base, s.base_headers)
    s.auth_body = s.auth_session.get("body") or {}
    s.access_token = r._auth_session_access_token(s.auth_body)
    workflow._persist_checkpoint(
        "at_probe_pending" if s.access_token else registration_checkpoint.SESSION_PENDING_STATE
    )
    if not s.existing_account or s.access_token:
        return
    _emit(
        _LOGGER,
        "  Existing account has no ChatGPT session yet; probing the login method before spending an email code...",
    )
    s.login_session = curl_requests.Session()
    if s.proxy:
        s.login_session.proxies = {"http": s.proxy, "https": s.proxy}
    workflow._install_edge_challenge_hook(s.login_session)
    r._set_oai_did_cookie(s.login_session, s.device_id)
    try:
        existing_login = r._login_existing_account_with_email_otp(
            session=s.login_session,
            username=s.username,
            mailbox=s.mailbox,
            did=s.device_id,
            session_logging_id=s.session_logging_id,
            auth_base=s.auth_base,
            chat_base=s.chat_base,
            base_headers=s.base_headers,
            csrf_token=s.csrf_token,
            proxy=s.proxy,
            sentinel_token=s.sentinel_token,
            sentinel_so_token=s.sentinel_so_token,
            # The probe can only *offer* the password step; a login still
            # needs a password to submit.  ``_login_probe_password`` owns
            # the "is it this account's own password?" decision.
            password=_login_probe_password(s),
            # A password login is followed by the account's *own* MFA
            # challenge, so the secret has to come along or the lane can
            # only answer ``existing_login_totp_secret_missing``.  Nothing
            # has been enrolled in this run yet, so read it from storage.
            totp_secret=r._stored_registration_totp(s.username),
            # ``existing_account`` is only ever set by the
            # ``user_already_exists`` answer, and for that address the
            # passwordless lane is a measured dead end (5/5 landings on the
            # signup profile step, 0 sessions).  So an inconclusive probe
            # must not fall back to it either.
            allow_passwordless=not s.existing_account,
        )
    except Exception as exc:
        existing_login = {"ok": False, "error": f"existing_login_transport:{exc}"}
    if not existing_login.get("ok"):
        s.existing_login_error = r._sanitize_text(existing_login.get("error") or "unknown")
        _emit(_LOGGER, "  Existing account login failed: %s", s.existing_login_error)
        if _is_existing_login_dead_end_error(existing_login.get("error")):
            try:
                retry_guard_cls(workflow.config).mark_dead_end(
                    s.username,
                    reason="user_already_exists",
                    error=str(s.existing_login_error),
                )
            except Exception as exc:
                _emit(_LOGGER, "  Dead-end bookkeeping warning: %s", describe_exception(exc))
        return
    s.auth_session = r._fetch_auth_session(s.login_session, s.chat_base, s.base_headers)
    s.auth_body = s.auth_session.get("body") or {}
    s.access_token = r._auth_session_access_token(s.auth_body)
    workflow._persist_checkpoint("at_probe_pending")
    if s.access_token:
        old_session = s.session
        s.session = s.login_session
        s.login_session = old_session


def probe_access_token(workflow: RegistrationEmailWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    if not s.access_token:
        return
    r.think_stage("pre_at_probe")
    s.at_probe = r._probe_registration_access_token(
        s.access_token,
        s.auth_body,
        proxy=s.proxy,
        cfg=workflow.config,
    )
    _emit(_LOGGER, "  Access token probe: HTTP %s", s.at_probe.get("status_code") or "unknown")
    workflow._persist_checkpoint(
        "at_probe_complete" if s.at_probe.get("status_code") == 200 else "at_probe_transport_unknown"
    )
