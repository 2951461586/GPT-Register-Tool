"""Pure and transport helpers for the protocol registration workflow.

Split out of ``registration_handlers`` (2026-10-01). Everything here is either
a pure predicate/formatter or a session factory -- none of it touches the
workflow's ``self.runtime`` / ``self.r`` bus, so it is safe to unit test on its
own and it cannot create an import cycle back into the workflow.

``registration_handlers`` re-imports every public name below, so the historical
``from sms_tool.registration_handlers import _login_probe_password`` (and the
``patch("sms_tool.registration_handlers._new_registration_session")`` targets)
keep resolving.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests


#: Existing-login lane failures that mean this address can never produce a
#: session for us, so the retry guard should skip it instead of spending an
#: email code to rediscover the same verdict.
#:
#: ``no_password_step`` is the probe's definitive answer -- the transaction
#: served a login form with no password input, so the account is passwordless.
#: ``password_step_unknown`` is the signup lane's refusal to guess: the address
#: is known-registered and the probe could not answer, and the email lane is a
#: measured dead end for that address.  ``password_required`` is deliberately
#: absent: that state is "the account has a password we do not hold", which a
#: later run supplying ``--password`` could still resolve, so blacklisting it
#: would skip an address we can actually log into.
EXISTING_LOGIN_DEAD_END_ERRORS = (
    "existing_login_no_password_step",
    "existing_login_password_step_unknown",
)


def _is_existing_login_dead_end_error(error: Any) -> bool:
    text = str(error or "")
    return any(text.startswith(marker) for marker in EXISTING_LOGIN_DEAD_END_ERRORS)


def _login_probe_password(state: Any) -> str:
    """The password the existing-login probe is allowed to submit, or ``""``.

    The probe can only *offer* the password step -- a login still needs a
    password to hand it, so every caller of
    ``_login_existing_account_with_email_otp`` has to answer "is the password we
    hold this account's own password?".  Answering it in three places invites
    three different answers, so it is answered once, here.

    ``password_unknown`` is the wrong gate on its own: ``create_account`` sets
    it for *every* ``user_already_exists`` answer, including the ones where the
    caller passed ``--password`` and we therefore do own the account's password
    (that case is recorded in ``existing_account_password_known``).  For a fresh
    registration the password is the one we just set, so it is submittable
    unless this run resumed an email verification without owning one.
    """
    password = str(getattr(state, "password", "") or "")
    if getattr(state, "existing_account", False):
        return password if getattr(state, "existing_account_password_known", False) else ""
    return "" if getattr(state, "password_unknown", False) else password


def _create_account_response_line(status: int, data: Any, sanitize: Callable[[Any], str]) -> str:
    """One line saying what the server *decided*, not everything it said.

    The 600-character budget exists for the failure case: ``user_already_exists``
    carries its ``userAlreadyExistsRecovery`` object past the 300-char mark, and
    that object is the only place the server states the recovery action
    (measured 2026-09-14).  A non-200 therefore still prints the raw body.

    A 200 does not.  There the budget goes to a URL the body repeats twice: the
    top-level ``continue_url`` in full, then ``page.payload.url`` with the same
    value cut off mid-query at character 182 (7/7 dumps measured 2026-09-14).
    That is unusable for a human *and* unparseable for a script, and the query
    string is a single-use OAuth code rather than a signal -- so a success prints
    the two fields the caller branches on, with the URL reduced to host + path.
    """
    try:
        rendered = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(data)
    if status != 200 or not isinstance(data, dict):
        return sanitize(rendered[:600])
    page = data.get("page")
    if not isinstance(page, dict):
        page = {}
    page_type = str(page.get("type") or data.get("page_type") or "")
    target = str(data.get("continue_url") or "")
    if not page_type and not target:
        # An unrecognised 200 shape: keep the raw body rather than print a line
        # that says nothing about why the caller got here.
        return sanitize(rendered[:600])
    parts = [f"page.type={page_type or '?'}"]
    if target:
        parsed = urlsplit(target)
        parts.append(f"continue_url={parsed.scheme}://{parsed.netloc}{parsed.path}")
    error = data.get("error")
    if error:
        parts.append(f"error={json.dumps(error, ensure_ascii=False, default=str)[:200]}")
    return sanitize(" ".join(parts))


def _safe_int(value: Any, default: int = 0) -> int:
    """``int(value)`` that never raises (missing/garbage -> ``default``).

    Config and server payloads are not trusted to be numeric: ``otp_timeout``
    comes from operator config and ``signup_state[\"status\"]`` from the wire, so
    a bare ``int()`` there turns a formatting mistake into a stage failure.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """``float(value)`` that never raises (missing/garbage -> ``default``)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _new_registration_session(proxy: str = "") -> Any:
    """Build the protocol session for one registration.

    P1-8: curl_cffi honours ``trust_env`` by default, so a machine-wide
    ``HTTP(S)_PROXY`` in the process environment silently *overrides* the
    per-account proxy set here -- every account would then exit through one
    shared IP, which defeats the whole proxy pool.  The preflight already pins
    this (``registration_preflight``), as do the other transports
    (``paypal_protocol``, ``gcash_transport``, ``phone_proxy``).  With no proxy
    configured we leave the default alone so an env-provided proxy still
    applies.
    """
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        session.trust_env = False
    return session


__all__ = [
    "EXISTING_LOGIN_DEAD_END_ERRORS",
    "_is_existing_login_dead_end_error",
    "_login_probe_password",
    "_create_account_response_line",
    "_new_registration_session",
    "_safe_int",
    "_safe_float",
]
