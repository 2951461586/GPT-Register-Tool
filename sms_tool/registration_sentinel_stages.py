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

They are module-level functions whose first parameter is the workflow instance,
the same shape ``registration_otp_stages`` uses, so this module cannot form an
import cycle back into ``registration_handlers``.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .sanitizer import describe_exception

_LOGGER = logging.getLogger(__name__)


def password_sentinel_bundle_enabled(self: Any) -> bool:
    """``registration.sentinel_password_bundle`` (default off).

    Off by default because it changes the Sentinel payload shape -- the
    password page is then primed with one shared requirements proof, the
    way a browser iframe does it -- and that still owes a controlled live
    comparison.  See ``docs/current/protocol-registration.md``.
    """
    registration = (self.config or {}).get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    value = registration.get("sentinel_password_bundle", False)
    return value not in (False, 0, "0", "false", "False", "no", "No", "off", "")


def prime_password_sentinel_bundle(self: Any) -> None:
    """Pre-mint the password page's Sentinel flows from one shared proof.

    Only the password lane gets this: it is the page a browser primes a
    bundle for.  A failure is non-fatal -- the per-flow issuance in
    ``issue_sentinel`` is still there and is the untouched default path.
    """
    s = self.runtime
    if s.registration_mode == "passwordless" or not password_sentinel_bundle_enabled(self):
        return
    from .sentinel import issue_sentinel_bundle, sentinel_backend

    if sentinel_backend(self.config) != "node_runner":
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


def issue_sentinel(self: Any, flow: str, *, force_fresh: bool = False) -> Any:
    """Issue one flow-bound Sentinel token for this run.

    ``force_fresh`` bypasses the supplied-bundle short-circuit
    (``issue_sentinel_flow`` -> ``_token_from_bundle``). The default path
    deliberately reuses a pre-minted bundle token, but a retry that
    promises a *fresh* proof must not silently re-consume the token its own
    previous round wrote back into ``s.sentinel_data`` -- otherwise the
    "refreshing the proof" wording is a lie and the retry replays the exact
    token the server just rejected.
    """
    from .sentinel import issue_sentinel_flow, sentinel_backend

    s = self.runtime
    issued = issue_sentinel_flow(
        flow=flow,
        device_id=s.device_id,
        session=s.session,
        proxy=s.proxy,
        supplied_data=None if force_fresh else s.sentinel_data,
        config=self.config,
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
    data["sentinel_source"] = str(data.get("sentinel_source") or sentinel_backend(self.config))
    s.sentinel_data = data
    return issued


__all__ = [
    "issue_sentinel",
    "password_sentinel_bundle_enabled",
    "prime_password_sentinel_bundle",
]
