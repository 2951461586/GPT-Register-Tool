"""Identity stages: Sentinel issuance, device context and fingerprint binding.

Split out of ``registration_handlers`` on 2026-10-24. ``prepare_identity`` writes
most of the registration runtime identity group, so it moves as one unit rather
than being split internally. ``_apply_protocol_fingerprint`` moved with it
because ``prepare_identity`` is its only production caller; ``handlers`` keeps a
re-export for the historical ``from ...handlers import _apply_protocol_fingerprint``
path and for the tests that import it directly.

Patch rule: the module-level helpers below resolve in *this* module's globals, so
a test that needs to replace ``_new_registration_session`` must patch
``sms_tool.registration.identity_stages``, not ``...handlers``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Mapping

from .. import fingerprint_pool as _fingerprint_pool
from ..auth_headers import set_auth_fingerprint
from ..operator_output import emit as _emit
from ..proxy import entry as _proxy_entry
from .flags import registration_flag
from .protocol_helpers import _new_registration_session, _safe_int
from .state import RegistrationState, prepare_registration_context, seeded_device_identity

if TYPE_CHECKING:
    from .state import RegistrationWorkflow

_LOGGER = logging.getLogger(__name__)

# The workflow surface used below is declared by ``state.RegistrationWorkflow``
# (extended 2026-10-24 with the identity/bootstrap attributes these stages reach).
# Keep the Protocol in step with this module: pyright validates every access
# against it, and a missing member is a type error rather than an AttributeError
# at registration time.

__all__ = [
    "device_context",
    "device_identity_seeded_by_email_enabled",
    "extract_sentinel",
    "fingerprint_geo_metadata",
    "prepare_identity",
    "verify_identity_context",
    "_apply_protocol_fingerprint",
]


def _apply_protocol_fingerprint(ops: Any, config: Any, proxy: str, metadata: dict[str, Any] | None = None) -> str:
    """Pick the pooled protocol fingerprint and bind *its* geo to this account.

    P1-1: the pool resolves the proxy's exit geo before handing back a profile
    (measuring it when the credential carries no region token).  This used to
    take only ``profile.name`` and drop the geo half, while the geo actually
    applied came from ``infer_proxy_country`` -- which only reads a region token
    in the proxy username.  A residential proxy has no such token, so it
    resolved to ``""`` and the account kept a US clock on, say, a Brazilian
    exit: the measurement was paid for and then ignored.

    A pool failure is **not** allowed to abort the run (the hint fallback is
    still better than nothing), but it is no longer silent: it emits a warning
    and bumps ``fingerprint_pool_failures`` in ``proxy_metadata``, which
    ``result.safe_proxy_audit`` allow-lists.  The silent version was the same
    failure class as P1-1 -- a measurement that never happened leaving no
    evidence anywhere -- and a run that falls back on every attempt must be
    distinguishable from one that never needed the fallback.
    """
    profile = None
    try:
        pool = _fingerprint_pool.shared_fingerprint_pool(config)
        if pool.size > 0:
            profile = pool.next(proxy)
    except Exception as exc:
        profile = None
        sanitize = getattr(ops, "_sanitize_text", None)
        detail = sanitize(exc) if callable(sanitize) else type(exc).__name__
        _emit(
            _LOGGER,
            "  [Fingerprint] pooled profile unavailable (%s); falling back to the proxy-country hint",
            detail,
        )
        if isinstance(metadata, dict):
            metadata["fingerprint_pool_failures"] = int(metadata.get("fingerprint_pool_failures") or 0) + 1
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


def device_identity_seeded_by_email_enabled(workflow: RegistrationWorkflow) -> bool:
    """``registration.device_identity_seeded_by_email`` (default **False**), S1 / ``p2-1``."""
    return registration_flag(workflow.config, "device_identity_seeded_by_email", False)


def device_context(workflow, email: Any) -> dict[str, Any]:
    """The persisted device context, or the address-derived one when S1 is on.

    A stored id always wins: an address that already registered keeps the id
    its account row holds, so S1 only changes addresses that have *never*
    succeeded -- which is every current failure sample.
    """
    stored = dict(workflow.persistence.get_device_context(email) or {})
    if stored.get("device_id") or stored.get("auth_session_logging_id"):
        return stored
    if not workflow._device_identity_seeded_by_email_enabled():
        return stored
    seeded = seeded_device_identity(str(email or ""))
    if not seeded:
        return stored
    workflow._device_seeded = True
    return {**stored, **seeded}


def extract_sentinel(workflow: RegistrationWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    from ..sentinel import sentinel_backend

    if workflow.input_sentinel:
        _emit(_LOGGER, "[*] Using provided sentinel tokens")
        s.sentinel_data = workflow.input_sentinel
    elif sentinel_backend(workflow.config) == "legacy":
        s.sentinel_data = r._extract_sentinel(
            proxy=s.proxy,
            force_fresh=True,
            persist=False,
            browser_headless=workflow.browser_headless,
        )
    else:
        device_context = workflow._device_context(s.username)
        s.sentinel_data = {
            "oai_did": str(device_context.get("device_id") or uuid.uuid4()),
            "sentinel_source": "node_sdk_runner",
        }
    if not s.sentinel_data or not r._sentinel_device_id(s.sentinel_data):
        workflow._abort("sentinel_extract_failed")
    r.think_stage("post_sentinel")


def prepare_identity(workflow: RegistrationWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    device_context = workflow._device_context(getattr(s.mailbox, "email", ""))
    stored_device_id = str(device_context.get("device_id") or "").strip()
    sentinel_device_id = str(r._sentinel_device_id(s.sentinel_data) or "").strip()
    if stored_device_id and stored_device_id != sentinel_device_id:
        from ..sentinel import sentinel_backend

        if sentinel_backend(workflow.config) == "legacy" or workflow.input_sentinel:
            _emit(_LOGGER, "  [Device] Regenerating Sentinel tokens for persisted device context")
            s.sentinel_data = r._extract_sentinel(
                proxy=s.proxy,
                force_fresh=True,
                persist=False,
                browser_headless=workflow.browser_headless,
                device_id=stored_device_id,
            )
            if not s.sentinel_data:
                workflow._abort("sentinel_extract_failed: persisted device token refresh failed")
        else:
            s.sentinel_data = {
                **dict(s.sentinel_data),
                "oai_did": stored_device_id,
            }

    s.context = prepare_registration_context(
        proxy=s.proxy,
        mailbox=s.mailbox,
        sentinel_data=s.sentinel_data,
        password=workflow.input_password,
        registration_mode=workflow.input_registration_mode,
        auth_base=s.auth_base,
        chat_base=s.chat_base,
        stored_password=r._stored_registration_password,
        generate_password=r._generate_password,
        random_name=r._random_name,
        random_birthdate=r._random_birthdate,
        normalize_mode=r._normalize_registration_mode,
        get_device_context=workflow._device_context,
        sentinel_device_id=r._sentinel_device_id,
        new_uuid=lambda: str(uuid.uuid4()),
        browser_headless=workflow.browser_headless,
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
    s.document_navigation_id = str(uuid.uuid4())
    workflow.browser_headless = c.browser_headless
    s.sentinel_token = str(s.sentinel_data.get("sentinel_token") or "")
    s.sentinel_authorize_token = str(s.sentinel_data.get("sentinel_authorize_continue_token") or "")
    s.sentinel_so_token = str(s.sentinel_data.get("sentinel_so_token") or "")
    try:
        r.assert_sentinel_device_id(s.sentinel_data, s.device_id)
    except ValueError as exc:
        workflow._abort(str(exc))
    # A seeded id arrives through the same context as a stored one, but it is
    # not a *reuse*: nothing was persisted for this address.
    reused = bool(c.reused_device_context) and not workflow._device_seeded
    seeded = "email" if workflow._device_seeded else "none"
    if reused:
        _emit(_LOGGER, "  [Device] Reusing persisted device context")
    workflow.device_identity = {"oai_did": s.device_id, "reused": reused, "seeded": seeded}
    # ``emit``, not ``print``: the device-layer A/B (plan-2026-10-10) reads these
    # lines from ``sms_tool.log``.  Only the first 8 characters of ``oai-did`` are
    # ever written; it is a credential-level identifier.
    if workflow._device_identity_seeded_by_email_enabled():
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
    _emit(_LOGGER, "[*] Username: %s  Password: [stored]  Name: %s  Birth: %s", s.username, s.full_name, s.birthdate)
    workflow._persist_checkpoint("identity_ready")
    s.session = _new_registration_session(s.proxy)
    workflow._install_edge_challenge_hook(s.session)
    if s.registration_mode == "passwordless":
        # Keep the Web/NextAuth flow isolated from the Sentinel extraction
        # prime session. Importing its auth.openai.com login cookies creates
        # a stale login transaction and routes authorize to /log-in/password.
        r._set_oai_did_cookie(s.session, s.device_id)
    else:
        r._import_sentinel_cookies(s.session, s.sentinel_data, s.device_id)
    r.set_fingerprint_device(s.device_id)
    workflow.fingerprint_country = _apply_protocol_fingerprint(r, workflow.config, s.proxy, workflow.proxy_metadata)
    s.base_headers = r.openai_auth_headers(
        s.device_id,
        accept="application/json",
        include_trace=True,
        session_id=s.session_logging_id,
        flow_invocation_id=s.flow_invocation_id,
    )
    if str(s.base_headers.get("oai-device-id") or "") != s.device_id:
        workflow._abort("sentinel_extract_failed: auth header device id mismatch")
    workflow._prime_password_sentinel_bundle()
    s.auth_flow_started = _safe_int(time.time())


def fingerprint_geo_metadata(workflow: RegistrationWorkflow) -> dict[str, Any]:
    config = getattr(workflow, "config", None)
    registration = (config or {}).get("registration") if isinstance(config, Mapping) else {}
    fingerprint = registration.get("fingerprint_pool") if isinstance(registration, Mapping) else {}
    allowed = fingerprint.get("allowed_countries") if isinstance(fingerprint, Mapping) else ()
    proxy_metadata = getattr(workflow, "proxy_metadata", {}) or {}
    return {
        "fingerprint_country": getattr(workflow, "fingerprint_country", ""),
        "exit_country": proxy_metadata.get("actual_country", ""),
        # Batch preflight samples a pool entry, not this attempt's rotated session.
        "source": "preflight",
        "allowed_countries": allowed,
    }


def verify_identity_context(workflow: "RegistrationWorkflow", state: Any) -> None:
    """Post-condition for ``IDENTITY_READY``: the stage created exactly one context.

    ``prepare_identity`` is the only writer of ``runtime.context``, and
    ``handlers._run_stage`` hands that object to every later handler -- before it
    (and on a post-create resume, which skips identity entirely) handlers receive
    the mutable ``RegistrationRuntimeState`` instead.  Every current stage body
    closes over ``self`` and ignores the parameter, so a drift here is invisible
    today: the first handler that actually *reads* its context would meet a
    runtime where its annotation promised a ``RegistrationContext``.

    Lives here rather than in ``handlers`` because it is the identity stage's own
    post-condition -- ``tests/registration/test_handlers_module_ownership.py``
    freezes that file's line budget and requires every stage body to sit in its
    owning module, with only a delegate left behind.

    Deliberately a warning, not an abort.  A hard failure would also fire on
    legitimate paths that run the stage idempotently with a no-op handler
    (``tests/registration/test_registration_bootstrap_timing.py`` drives
    ``IDENTITY_READY`` that way to pin the timing rows), and killing a live
    registration because a stub did not build a context is a worse trade than a
    greppable line in the operator log.  The marker is stable so it can be
    asserted and alerted on.
    """
    if state is not RegistrationState.IDENTITY_READY:
        return
    context = getattr(workflow.runtime, "context", None)
    if context is None:
        _LOGGER.warning(
            "registration_identity_context_missing: the identity stage finished "
            "without creating a RegistrationContext; every later stage will be "
            "handed the mutable runtime",
            extra={"event": "registration_identity_context_missing"},
        )
        return
    if not getattr(context, "device_id", ""):
        _LOGGER.warning(
            "registration_identity_context_without_device_id: identity context "
            "carries no device_id; the oai-did/header/cookie trio cannot be "
            "verified consistent for this run",
            extra={"event": "registration_identity_context_without_device_id"},
        )
