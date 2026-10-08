"""In-flow Cloudflare edge-challenge handling for the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-08).  The four functions here
form one cohesive unit: they own the session's ``_openai_edge_challenge_hook``,
the decision to move the run to another exit **in the same pool**, the
``proxy_audit`` counters and the ``clear_session_circuit`` invariant.

The split is by ownership, not by size:

* ``http_client`` owns the response, the retry budget and the circuit itself
  (``request_with_retry`` calls the hook at most once per request).
* This module owns only the *decision* and the *exit move* -- the proxy string,
  the pool cursor and the audit counters all live on the workflow instance.
* ``registration_handlers`` keeps thin delegates so every historical call site
  (``self._install_edge_challenge_hook(...)``) and the tests that drive the
  workflow object keep working unchanged.

They are module-level functions whose first parameter is the workflow instance,
the same shape ``registration_otp_stages`` uses, so this module cannot form an
import cycle back into ``registration_handlers``.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .http_client import EDGE_CHALLENGE_HOOK_ATTR, clear_session_circuit
from .operator_output import emit as _emit
from .proxy_edge_probe import EDGE_UNKNOWN
from .proxy_entry import rotate_session
from .sanitizer import describe_exception

_LOGGER = logging.getLogger(__name__)


def edge_challenge_rotate_exit_enabled(self: Any) -> bool:
    """``registration.edge_challenge_rotate_exit`` (default **False**).

    On a Cloudflare challenge the transport asks this handler's hook whether
    to move the run to a new exit **in the same pool** and retry the one
    request; see ``plan-2026-10-05-inflow-challenge-handoff.md`` §3.1/§3.3.
    Off by default because it changes control flow: one extra state-changing
    request on a different egress, which needs its own A/B
    (``p0-2b-inflow-challenge-handoff``) before it earns a default.

    Note the deliberate split from
    ``http_client.edge_challenge_discrimination_enabled``: that switch only
    labels the failure *name* (observation, default on); this one moves
    traffic.  They are orthogonal -- a run may label without rotating.
    """
    registration = (self.config or {}).get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    value = registration.get("edge_challenge_rotate_exit", False)
    return value in (True, 1, "1", "true", "True", "yes", "Yes", "on")


def install_edge_challenge_hook(self: Any, session: Any) -> None:
    """Let ``http_client`` ask this handler before acting on a challenge.

    Installed on every session this run makes requests with (the protocol
    session and the existing-login session), because either can be the one
    that gets challenged.  The hook counts hits even when rotation is off --
    that count is the A/B's ``edge_challenge_count``, and an observation
    that only exists when the treatment is on measures nothing.
    """
    if session is None:
        return
    try:
        setattr(session, EDGE_CHALLENGE_HOOK_ATTR, self._on_edge_challenge)
    except Exception:
        return


def on_edge_challenge(self: Any, session: Any, verdict: str, allow_rotate: bool) -> bool:
    """Hook body: count the reply, then maybe rotate the exit once.

    Returns True only when the exit actually moved.  The two ways it does
    not move are both reported rather than silently counted as a rotation:
    the switch is off, or the provider has no sticky session id (a
    single-slot exit leaves the URL unchanged) -- the plan requires that
    case to short-circuit honestly, otherwise the A/B's ``rotate`` arm is an
    ``observe`` arm wearing a different label.

    §7-5: ``unknown`` (an unreadable verdict) is rotated too, because the
    cost asymmetry points that way, but it is counted under its own key --
    merging it into ``edge_challenge_hits`` would dilute the exact number
    the A/B's manipulation check rests on.
    """
    metadata = getattr(self, "proxy_metadata", None)
    counter = "edge_challenge_unknown" if verdict == EDGE_UNKNOWN else "edge_challenge_hits"
    if isinstance(metadata, dict):
        metadata[counter] = int(metadata.get(counter) or 0) + 1
    if not allow_rotate or not self._edge_challenge_rotate_exit_enabled():
        return False
    s = self.runtime
    current = str(getattr(s, "proxy", "") or "")
    rotated = rotate_session(current)
    if not rotated or rotated == current:
        self._count_edge_challenge_rotate_failure("single_slot")
        return False
    try:
        session.proxies = {"http": rotated, "https": rotated}
    except Exception as exc:
        self._count_edge_challenge_rotate_failure("rebind_failed")
        _LOGGER.warning("Edge-challenge exit rotation could not rebind the session: %s", describe_exception(exc))
        return False
    s.proxy = rotated
    # §3.5 invariant 2: the session may already carry a circuit written by an
    # earlier request; the new exit must not inherit it, or the retry below
    # would raise before it ever leaves the machine.
    clear_session_circuit(session)
    metadata = getattr(self, "proxy_metadata", None)
    if isinstance(metadata, dict):
        metadata["edge_challenge_rotations"] = int(metadata.get("edge_challenge_rotations") or 0) + 1
    _emit(
        _LOGGER,
        "  [Edge] Cloudflare challenge on this exit; rotated to a new session in the same pool and retrying once",
    )
    return True


def count_edge_challenge_rotate_failure(self: Any, reason: str) -> None:
    """Count one honest rotation failure (single-slot pool, or a rebind error)."""
    metadata = getattr(self, "proxy_metadata", None)
    if isinstance(metadata, dict):
        metadata["edge_challenge_rotate_failed"] = int(metadata.get("edge_challenge_rotate_failed") or 0) + 1
    _emit(_LOGGER, "  [Edge] Cloudflare challenge but the exit cannot rotate (%s); keeping this exit", reason)


__all__ = [
    "count_edge_challenge_rotate_failure",
    "edge_challenge_rotate_exit_enabled",
    "install_edge_challenge_hook",
    "on_edge_challenge",
]
