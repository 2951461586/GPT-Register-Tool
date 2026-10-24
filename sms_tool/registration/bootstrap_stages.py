"""Bootstrap stages: config/proxy/mailbox setup, reference-lane overlay.

Split out of ``registration_handlers`` on 2026-10-24. ``run_bootstrap`` owns the
stage failure taxonomy for the pre-stage work; ``bootstrap`` owns the body. The
workflow class keeps thin delegates, so ``patch.object(RegistrationEmailWorkflow,
"_bootstrap", ...)`` still intercepts the call from ``run()``.

This module never imports ``handlers`` at runtime; the workflow type is a
TYPE_CHECKING import, the same shape ``account_stages`` uses to avoid a cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import endpoints
from ..mailbox import service as _mailbox_service
from ..mailbox.errors import MailboxEndpointUnavailableError
from ..operator_output import emit as _emit
from ..providers.mailbox_graph import MailboxAuthInvalidError
from ..proxy import entry as _proxy_entry
from . import checkpoint as registration_checkpoint
from . import reference_lane as _reference_lane
from .cancel import RegistrationCancelled
from .protocol_helpers import tock_named
from .stage_runner import RegistrationAbort
from .state import RegistrationState

if TYPE_CHECKING:
    from .state import RegistrationWorkflow

_LOGGER = logging.getLogger(__name__)

__all__ = ["apply_reference_lane", "bootstrap", "run_bootstrap"]


def apply_reference_lane(workflow: RegistrationWorkflow) -> None:
    """Overlay the ``registration.reference_lane`` preset before the config scope is entered."""
    base = workflow.config if workflow.config is not None else workflow.r.current_config_data()
    effective = _reference_lane.apply_reference_lane(base)
    if effective is base:
        return
    workflow.config = effective
    workflow.input_registration_mode = "password"
    _emit(_LOGGER, "[*] Reference lane: password registration in the reference project's stage order")


def run_bootstrap(workflow: RegistrationWorkflow) -> None:
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
    r = workflow.r
    r._tick("0-Bootstrap")
    try:
        workflow._bootstrap()
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
        # ``_bootstrap`` runs two nested ``_run_stage`` calls whose own
        # ``finally`` blocks close their entries and clear the shared
        # ``_timing_open`` flag, so this outer row must be closed *by name*:
        # by now the last entry is the identity stage, not this one.
        tock_named(r._tl(), "0-Bootstrap")


def bootstrap(workflow: RegistrationWorkflow) -> None:
    r = workflow.r
    s = workflow.runtime
    # Local alias: pyright does not narrow a mutable attribute across statements,
    # and the original method's ``self.config`` access only type-checked because
    # the file predates the ratchet.
    config = workflow.config if workflow.config is not None else r.current_config_data()
    workflow.config = config
    s.email_cfg = dict(config.get("email_registration") or {})
    r.validate_config(config, workflow="registration")
    s.proxy = r._resolve_proxy_scheme(workflow.input_proxy, cfg=config)
    preflight = r.registration_network_preflight(proxy=s.proxy, proxy_attempts=2)
    s.proxy = str(preflight.get("proxy") or s.proxy or "")
    s.mailbox = r.ensure_mailbox_account(workflow.input_mailbox)
    if not s.mailbox or not s.mailbox.email:
        workflow._abort("mailbox_required")
    s.username = str(getattr(s.mailbox, "email", "") or "").strip()
    s.resume_checkpoint = (
        registration_checkpoint.load_resumable_checkpoint(workflow.persistence, s.username, workflow.config) or {}
    )
    if not s.resume_checkpoint:
        workflow._persist_checkpoint("mailbox_ready")
    s.mailbox_service = _mailbox_service.MailboxService.create(config)
    chatgpt_cfg = config.get("chatgpt", {})
    s.auth_base = chatgpt_cfg.get("auth_base_url", endpoints.AUTH_BASE)
    s.chat_base = chatgpt_cfg.get("chat_base_url", endpoints.CHATGPT_BASE)
    r.set_fingerprint_geo(_proxy_entry.infer_proxy_country(s.proxy))
    workflow.machine.transition(RegistrationState.MAILBOX_READY)
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
    # into ``seen_message_ids`` -- which ``latest_email_otp_candidate``
    # then skips.  The poll drained its full 300s window while the code sat
    # in plain sight (2026-09-11, batch 5e32aa85: 10 of 12 runs failed).
    #
    # Taking the snapshot up front makes it a true "what was already in the
    # inbox before this attempt" marker, matching the reference design
    # where ``before_ids`` is captured during identity resolution.
    _emit(_LOGGER, "[*] Snapshotting mailbox (pre-OTP baseline)")
    r.snapshot_mailbox_message(s.mailbox, proxy=s.proxy)
    _emit(_LOGGER, "[*] ChatGPT Email Registration Started")
    workflow._run_stage(RegistrationState.SENTINEL, "0-Extract sentinel token", workflow.extract_sentinel)
    workflow._run_stage(RegistrationState.IDENTITY_READY, "1-Prepare registration identity", workflow.prepare_identity)
