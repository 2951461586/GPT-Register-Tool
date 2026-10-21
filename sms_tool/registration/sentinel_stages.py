"""Sentinel token issuance stages for the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-08).  The three functions here
own *when and how* a Sentinel token is minted for this run:

* ``password_sentinel_bundle_enabled`` -- the ``registration.sentinel_password_bundle``
  switch (default off; it changes the payload shape, so it owes its own A/B).
* ``prime_password_sentinel_bundle`` -- the optional password-lane bundle, minted
  once from a shared requirements proof the way a browser iframe does it.
* ``issue_sentinel`` -- the per-flow issuance that is the untouched default path,
  with ``force_fresh`` for a retry that must not replay its own rejected token.

Boundary: the ``sentinel`` package owns the Node runner, the flow-to-page map and
the wire call; this module owns only the workflow's stage decisions and the
write-back into ``RegistrationRuntimeState``'s token fields.  The
``from .sentinel import ...`` calls stay function-local, which is this
codebase's documented way to keep the Node-runner dependency off the import
graph until a run actually needs it.

They are module-level functions that take the workflow instance as their first
parameter, named ``workflow`` and annotated with ``RegistrationEmailWorkflow``
under ``TYPE_CHECKING``, so the type checker resolves the workflow's attributes
without a runtime import cycle back into ``registration_handlers``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .flags import registration_flag
from ..kernel.sanitizer import describe_exception

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from .handlers import RegistrationEmailWorkflow

_LOGGER = logging.getLogger(__name__)


def password_sentinel_bundle_enabled(workflow: RegistrationEmailWorkflow) -> bool:
    """``registration.sentinel_password_bundle`` (default off).

    Off by default because it changes the Sentinel payload shape -- the
    password page is then primed with one shared requirements proof, the
    way a browser iframe does it -- and that still owes a controlled live
    comparison.  See ``docs/current/protocol-registration.md``.
    """
    # ``registration_flag`` is the shared, Mapping-safe, two-sided parser.  The
    # old hand-rolled body failed **open** (an unrecognised value turned this
    # default-off switch on) -- fixed by the 2026-10-10 P1-C′ consolidation.
    return registration_flag(workflow.config, "sentinel_password_bundle", False)


def prime_password_sentinel_bundle(workflow: RegistrationEmailWorkflow, *, reference_page: bool = False) -> None:
    """Pre-mint the password page's Sentinel flows from one shared proof.

    Only the password lane gets this: it is the page a browser primes a
    bundle for.  A failure is non-fatal -- the per-flow issuance in
    ``issue_sentinel`` is still there and is the untouched default path.
    """
    s = workflow.runtime
    lane = registration_flag(workflow.config, "reference_lane", False)
    if s.registration_mode == "passwordless" or not (lane or password_sentinel_bundle_enabled(workflow)):
        return
    # `reference_lane` mints this bundle right before `user/register` (see
    # `registration_reference_lane`), not at identity time like the legacy switch.
    if lane != reference_page:
        return
    from ..sentinel import issue_password_page_bundle, issue_sentinel_bundle, sentinel_backend

    if sentinel_backend(workflow.config) != "node_runner":
        return
    if lane:
        try:
            issued = issue_password_page_bundle(device_id=s.device_id, session=s.session, proxy=s.proxy)
        except Exception as exc:
            _LOGGER.warning(
                "Password page Sentinel bundle unavailable; per-flow issuance applies: %s", describe_exception(exc)
            )
            return
        s.sentinel_data = {**dict(s.sentinel_data), "sentinel_token": issued.token}
        s.sentinel_token = issued.token
        _LOGGER.info("Reference lane: password page Sentinel bundle primed")
        return
    try:
        bundle = issue_sentinel_bundle(
            flows=("authorize_continue", "username_password_create"),
            device_id=s.device_id,
            session=s.session,
            proxy=s.proxy,
        )
    except Exception as exc:
        _LOGGER.warning(
            "Sentinel bundle for the credential flow unavailable; falling back to per-flow issuance: %s",
            describe_exception(exc),
        )
        return
    merged = dict(s.sentinel_data)
    for key, value in bundle.items():
        if key in {"cookie_str", "oai_did", "sentinel_source"} or not str(value or "").strip():
            continue
        merged[key] = value
    s.sentinel_data = merged
    s.sentinel_token = str(s.sentinel_data.get("sentinel_token") or s.sentinel_token)
    s.sentinel_authorize_token = str(
        s.sentinel_data.get("sentinel_authorize_continue_token") or s.sentinel_authorize_token
    )
    s.sentinel_so_token = str(s.sentinel_data.get("sentinel_so_token") or s.sentinel_so_token)
    _LOGGER.info("Sentinel password flow bundle primed (shared requirements proof)")


def issue_sentinel(workflow: RegistrationEmailWorkflow, flow: str, *, force_fresh: bool = False) -> Any:
    """Issue one flow-bound Sentinel token for this run.

    ``force_fresh`` bypasses the supplied-bundle short-circuit
    (``issue_sentinel_flow`` -> ``_token_from_bundle``). The default path
    deliberately reuses a pre-minted bundle token, but a retry that
    promises a *fresh* proof must not silently re-consume the token its own
    previous round wrote back into ``s.sentinel_data`` -- otherwise the
    "refreshing the proof" wording is a lie and the retry replays the exact
    token the server just rejected.
    """
    from ..sentinel import issue_sentinel_flow, sentinel_backend

    s = workflow.runtime
    issued = issue_sentinel_flow(
        flow=flow,
        device_id=s.device_id,
        session=s.session,
        proxy=s.proxy,
        supplied_data=None if force_fresh else s.sentinel_data,
        config=workflow.config,
    )
    data = dict(s.sentinel_data)
    if flow == "username_password_create":
        data["sentinel_token"] = issued.token
        s.sentinel_token = issued.token
    elif flow == "authorize_continue":
        data["sentinel_authorize_continue_token"] = issued.token
        data["sentinel_authorize_continue_so_token"] = issued.so_token
        s.sentinel_authorize_token = issued.token
    elif flow == "oauth_create_account":
        data["sentinel_oauth_token"] = issued.token
        data["sentinel_so_token"] = issued.so_token
        s.sentinel_so_token = issued.so_token
    data["oai_did"] = issued.device_id
    data["sentinel_source"] = str(data.get("sentinel_source") or sentinel_backend(workflow.config))
    s.sentinel_data = data
    return issued


__all__ = [
    "issue_sentinel",
    "password_sentinel_bundle_enabled",
    "prime_password_sentinel_bundle",
]
