"""Password-step detection and the password probe for the login lane."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import deps, steps, totp

LOGIN_PASSWORD_STEP_TYPES = frozenset({"password", "login_password", "create_account_password"})

SOURCE_TRANSACTION_STEP = "transaction_step"

SOURCE_SIBLING_FORM = "sibling_form"

_FORM_TAG_RE = re.compile(r"<form\b", re.IGNORECASE)

_PASSWORD_INPUT_RE = re.compile(r"(?:type=[\"']password[\"']|name=[\"']password[\"'])", re.IGNORECASE)

def _login_password_page_type(payload):
    """``page.type`` (or the flat ``page_type``) of an authorize response."""
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page")
    page = page if isinstance(page, dict) else {}
    return str(page.get("type") or payload.get("page_type") or "").strip().lower()

def _is_login_password_step(url, payload=None):
    """True when ``url`` / ``payload`` positively expose the password login step."""
    parsed = urlparse(str(url or ""))
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if host.endswith("auth.openai.com") and (
        path.endswith("/log-in/password") or path.endswith("/create-account/password")
    ):
        return True
    return _login_password_page_type(payload) in LOGIN_PASSWORD_STEP_TYPES

def _has_password_form(response):
    """True when ``response`` is an HTML page carrying a password input.

    Mirrors abai's ``_has_password_form``: a ``<form>`` plus either an input of
    ``type=password`` or one named ``password``.  The sibling
    ``/log-in/password`` step serves that form only when the transaction has a
    password step, which is what makes it a usable discriminator.
    """
    text = str(getattr(response, "text", "") or "")
    if not _FORM_TAG_RE.search(text):
        return False
    return bool(_PASSWORD_INPUT_RE.search(text))

def _probe_login_password_step(
    session,
    auth_base,
    base_headers,
    current_url,
    *,
    payload=None,
    continue_url="",
):
    """Zero-cost probe: does this login transaction offer a *password* step?

    ``/log-in/password`` is the **sibling auth step** of email OTP inside the
    same transaction.  OpenAI lands the generic login transaction on the OTP
    page *even for accounts that have a password* -- abai's protocol client
    documents exactly that at ``_load_login_password_page`` -- so an OTP page is
    not evidence of a passwordless account.  Reading the transaction's own
    answer costs one GET instead of a mailbox poll plus an email code.

    ``password_step`` is deliberately three-valued:

    ``True``
        a password step is reachable -- the caller should attempt password
        login instead of spending an email code.  **Read ``source`` before
        acting on it.**  Only ``SOURCE_SIBLING_FORM`` separates an existing
        account's login form from a new account's ``/create-account/password``
        setup screen, which shares the page type; a registration lane that
        aborts on a bare ``True`` would stop every fresh signup.
    ``False``
        the server showed a login form with **no** password input, so the
        account is passwordless.  An email code is then the only remaining
        method, and it is a measured dead end (5/5 landings on the signup
        profile step ``/about-you``, 0 sessions);
    ``None``
        the probe could not get an answer -- a transport failure, a non-200
        from the sibling step (which answers 400 without the transaction's own
        state), or a 200 that carries no form at all.  The caller must then
        keep its existing behaviour rather than terminate on an unknown: a
        probe that cannot tell is not evidence of absence.

    ``signal`` names the reason, and for the ``None`` case it is the only thing
    that distinguishes "the server said no" from "this transaction never had
    state to ask about" (``no_transaction_state``).
    """
    result = {"password_step": None, "signal": "", "source": "", "url": "", "status": 0}
    page_type = _login_password_page_type(payload)
    if _is_login_password_step(continue_url, payload):
        result["password_step"] = True
        result["source"] = SOURCE_TRANSACTION_STEP
        result["signal"] = (
            f"transaction_page_type={page_type}"
            if page_type
            else f"transaction_url={str(continue_url)[:80]}"
        )
        return result
    if not str(continue_url or "").strip() and not page_type:
        # 🔴 2026-09-15: no transaction state ⇒ the sibling step cannot answer.
        #
        # An empty ``continue_url`` means the caller deliberately *skipped*
        # ``authorize/continue`` -- see ``_login_existing_account_with_email_otp``:
        # "Existing account continue: skipped (already at email-verification)".
        # Without that POST the session carries no login transaction, and
        # ``/log-in/password`` answers 400.  Measured 2026-09-15: 97 of 101
        # probes returned ``None`` for exactly this reason, each after paying a
        # doomed GET.  Report the *reason* instead of the symptom, and do not
        # spend the request.
        result["signal"] = "no_transaction_state"
        return result
    target = f"{auth_base}/log-in/password"
    result["url"] = target
    try:
        response = deps.request_with_retry(
            session,
            "get",
            target,
            label="Existing account password step probe",
            headers={
                **base_headers,
                "Accept": "text/html,application/xhtml+xml",
                "Referer": current_url or f"{auth_base}/email-verification",
                "Origin": auth_base,
            },
            impersonate=deps.auth_impersonate(),
        )
    except Exception as exc:
        result["signal"] = f"transport:{exc}"
        return result
    result["status"] = int(getattr(response, "status_code", 0) or 0)
    if result["status"] != 200:
        # ``/log-in/password`` without the transaction's own state answers 400,
        # so a non-200 is not proof of anything -- report it as unknown.
        result["signal"] = f"http_{result['status']}"
        return result
    if not _FORM_TAG_RE.search(str(getattr(response, "text", "") or "")):
        # A 200 that carries no form at all (an empty shell, a JSON body, or
        # the generic email-verification shell abai documents) says nothing
        # about whether this account has a password.  Report unknown instead of
        # turning a blank page into a terminal verdict.
        result["signal"] = "response_has_no_form"
        return result
    result["password_step"] = _has_password_form(response)
    result["source"] = SOURCE_SIBLING_FORM
    result["signal"] = "password_form_present" if result["password_step"] else "password_form_absent"
    return result

def _password_login_existing_account(
    session,
    auth_base,
    base_headers,
    password,
    current_url,
    did,
    *,
    sentinel_token="",
    sentinel_so_token="",
    totp_secret="",
):
    """Submit the account password on the login transaction's password step.

    Mirrors ``codex_oauth._password_login_and_exchange`` for the protocol login
    lane: POST the password, follow the transaction, then complete a saved TOTP
    challenge when the server asks for one.

    A rejected password is **terminal** here.  abai's client refuses to fall
    back to an email code from this state ("拒绝改走邮箱验证码") and our own
    measurements agree -- the OTP lane re-verifies a code and lands on the
    signup profile step ``/about-you``, which never issues a NextAuth session.
    """
    headers = steps._auth_request_headers(
        base_headers,
        did=did,
        referer=current_url or f"{auth_base}/log-in/password",
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    response = deps.request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/password/verify",
        label="Existing account password verify",
        json={"password": password},
        headers=headers,
        impersonate=deps.auth_impersonate(),
    )
    print(f"  Existing account password verify: {response.status_code}")
    if response.status_code != 200:
        return {
            "ok": False,
            "error": f"existing_login_password_verify_failed:{response.status_code}",
            "login_method": "password",
        }

    payload = deps._json_or_raw(response, limit=1000)
    next_url = steps._response_next_url(response, auth_base)
    if next_url:
        try:
            followed = deps._follow_continue_url(
                session,
                next_url,
                base_headers,
                referer=current_url or f"{auth_base}/log-in/password",
                label="Existing account password continue",
            )
        except Exception as exc:
            print(f"  Existing account password continue transport warning: {exc}")
        else:
            followed_body = deps._json_or_raw(followed, limit=1000)
            if isinstance(followed_body, dict) and followed_body:
                payload = followed_body

    mfa_result = totp._complete_existing_login_totp(
        session,
        auth_base,
        base_headers,
        payload,
        did=did,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        totp_secret=totp_secret,
    )
    if not mfa_result.get("ok"):
        mfa_result.setdefault("login_method", "password")
        return mfa_result
    data = mfa_result.get("data") if isinstance(mfa_result.get("data"), dict) else payload
    final_url = str((data if isinstance(data, dict) else {}).get("continue_url") or "")
    if steps._is_about_you_step(final_url, data):
        # Same wall the OTP lane hits: the server classified this transaction as
        # signup continuation, so no NextAuth session will be issued.
        print(f"  Existing account password continue: profile step, not a login landing ({final_url[:80]})")
        return {
            "ok": False,
            "error": f"existing_login_landed_on_profile_step:{final_url[:120]}",
            "login_method": "password",
        }
    if final_url:
        try:
            deps._follow_continue_url(
                session,
                final_url,
                base_headers,
                referer=f"{auth_base}/log-in/password",
                label="Existing account password landing",
            )
        except Exception as exc:
            print(f"  Existing account password landing transport warning: {exc}")
    return {"ok": True, "login_method": "password"}
