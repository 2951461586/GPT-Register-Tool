import json
import logging
import re
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .accounts.account_creation import _validate_email_otp
from .auth_headers import auth_impersonate, nextauth_headers, openai_auth_headers
from .auth_state import fetch_client_auth_session_dump as _fetch_client_auth_session_dump
from .config import current_config_data
from .http_client import _retry_after_seconds, request_with_retry
from .http_utils import (
    _absolute_url,
    _cookie_presence,
    _follow_continue_url,
    _json_or_raw,
)
from .mailbox import _poll_email_otp
from .phone_proxy import redact_proxy_url
from .registration_concurrency import mark_registration_rate_limited


logger = logging.getLogger(__name__)

_PASSKEY_CLIENT_CAPABILITIES = "11111"
_CC_CAPS = "login_methods"


def _is_existing_login_redirect(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if host and host != "auth.openai.com" and not host.endswith(".auth.openai.com"):
        return False
    path = (parsed.path or url or "").lower()
    if not path:
        return False
    # Normalize: strip trailing slashes
    path = path.rstrip("/")
    return path in {"/log-in", "/login"} or path.startswith("/log-in/") or path.startswith("/login/")


def _is_chatgpt_auth_login_landing(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    return host.endswith("chatgpt.com") and path in {"/auth/login", "/auth/log-in"}


def _is_signup_password_step(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if not host.endswith("auth.openai.com"):
        return False
    return path.endswith("/create-account/password") or path.endswith("/create-account")


def _is_email_verification_step(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if not host.endswith("auth.openai.com"):
        return False
    return path.endswith("/email-verification") or "email-otp" in path


def _existing_login_continue_enabled():
    """Whether the login lane may POST ``authorize/continue`` from the OTP page.

    ``registration.existing_login_continue_on_verified_page`` (default **True**).

    **Why this exists.**  Until 2026-09-16 the lane dropped that POST whenever
    ``authorize`` landed on ``/email-verification``, on the grounds that posting
    it corrupted the session (measured 2026-09-14: 3/3 ``email-otp/validate``
    answered ``401 login_failed`` afterwards).  That decision is what made the
    existing-account lane **deterministically unwinnable**: the password-step
    probe reads the transaction state the POST is supposed to return, so with
    the POST dropped its ``continue_url`` was always empty, the probe always
    answered ``None`` (``no_transaction_state``), and -- because the signup lane
    passes ``allow_passwordless=False`` for an address the server already
    reported as registered -- ``None`` terminated instead of falling back.
    Measured 2026-09-16 (PID 32088): 19/19 attempts died on exactly that path,
    with zero exceptions.

    abai's protocol client takes the opposite decision: ``_submit_login_email``
    posts ``authorize/continue`` *unconditionally* and its docstring states why
    -- "the ``authorize/continue`` response carries the transaction-bound
    ``continue_url`` for the password form; navigating to ``/log-in/password``
    without that state returns HTTP 400".

    **The 09-14 corruption and abai's working client differ in one field:**
    abai sends ``screen_hint: "login"`` in the POST body.  Our body carries only
    ``username``, which is what a *signup* continue sends -- consistent with the
    09-14 observation that the session kept signup-era state afterwards.  This
    flag turns the POST back on **with** that field, so the hypothesis can be
    tested in production without editing code.

    Set it to ``false`` to restore the 09-14 skip behaviour exactly.
    """
    try:
        cfg = current_config_data().get("registration", {})
    except Exception:
        return True
    if not isinstance(cfg, dict):
        return True
    value = cfg.get("existing_login_continue_on_verified_page", True)
    return value not in (False, 0, "0", "false", "False", "no", "No", "off")


#: ``page.type`` values that mean the auth transaction is offering the
#: *password* step.  Same set abai's protocol client gates on
#: (``_password_registration_step`` in ``platforms/chatgpt/protocol_register.py``).
LOGIN_PASSWORD_STEP_TYPES = frozenset({"password", "login_password", "create_account_password"})

#: ``source`` values for ``_probe_login_password_step``: *which* piece of
#: evidence produced ``password_step``.
#:
#: The two ``True`` paths are **not** equally trustworthy, and conflating them
#: is how a registration lane would kill every fresh signup:
#:
#: ``SOURCE_TRANSACTION_STEP``
#:     the auth transaction's own declared next step.  ``LOGIN_PASSWORD_STEP_TYPES``
#:     includes ``create_account_password`` -- the *new-account* "choose a
#:     password" screen -- so a ``True`` from here cannot tell a registered
#:     account from a brand-new one.
#: ``SOURCE_SIBLING_FORM``
#:     the HTML the ``/log-in/password`` step actually served.  Only a login
#:     transaction reaches that step with a password input, so this one *is*
#:     specific to an existing account.
#:
#: Neither constant says the password is *ours*: a reachable login form still
#: needs a password we hold (``_login_probe_password`` owns that decision).
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
        response = request_with_retry(
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
            impersonate=auth_impersonate(),
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


def _is_about_you_step(url, payload=None):
    """True when the auth transaction routed into the *signup* profile step.

    ``about-you`` is where ``create_account`` collects name/birthdate, so a
    *login* transaction that lands here was classified by the server as signup
    continuation rather than login -- it is never followed by a NextAuth
    session, and the caller's ``/api/auth/session`` poll answers
    ``keys=['WARNING_BANNER']`` with no ``accessToken``.

    ``turb-gpt-free-register`` reaches the same verdict from the same signal
    (``account_liveness.py``): ``"该邮箱登录后进入资料页，疑似不是完整已注册账号"``.
    """
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if host.endswith("auth.openai.com") and "about-you" in path:
        return True
    page = payload.get("page") if isinstance(payload, dict) else None
    page_type = str((page if isinstance(page, dict) else {}).get("type") or "").strip().lower()
    return page_type in {"about_you", "about-you"}


def _response_next_url(response, base_url):
    body = _json_or_raw(response, limit=1000)
    if isinstance(body, dict):
        value = body.get("continue_url") or body.get("url")
        if value:
            return _absolute_url(base_url, value)
    location = getattr(response, "headers", {}).get("location") or getattr(response, "headers", {}).get("Location")
    if location:
        return _absolute_url(base_url, location)
    return str(getattr(response, "url", "") or "")


def _with_query_param(url, key, value):
    if not value or f"{key}=" in (url or ""):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={quote(str(value), safe='')}"


def _ensure_authorize_context(url, did, session_logging_id, login_hint, *, screen_hint="", prompt=""):
    parsed = urlparse(str(url or ""))
    if not parsed.netloc.endswith("auth.openai.com"):
        return str(url or "")
    values = parse_qs(parsed.query, keep_blank_values=True)
    required = {
        "device_id": did,
        "ext-oai-did": did,
        "auth_session_logging_id": session_logging_id,
        "ext-passkey-client-capabilities": _PASSKEY_CLIENT_CAPABILITIES,
        "ccaps": _CC_CAPS,
        "login_hint": login_hint,
    }
    if screen_hint:
        required["screen_hint"] = screen_hint
    if prompt:
        required["prompt"] = prompt
    for key, value in required.items():
        if value and not values.get(key):
            values[key] = [str(value)]
    return parsed._replace(query=urlencode(values, doseq=True)).geturl()


def _openai_signin_url(chat_base, did, session_logging_id, login_hint, *, screen_hint="", prompt=""):
    params = {
        "ext-oai-did": did,
        "device_id": did,
        "auth_session_logging_id": session_logging_id,
        "ext-passkey-client-capabilities": _PASSKEY_CLIENT_CAPABILITIES,
        "ccaps": _CC_CAPS,
        "login_hint": login_hint,
    }
    if screen_hint:
        params["screen_hint"] = screen_hint
    if prompt:
        params["prompt"] = prompt
    return f"{chat_base}/api/auth/signin/openai?{urlencode(params)}"


def _protocol_diagnostic(*, response=None, final_url="", session=None, sentinel_source="", sentinel_flow="", proxy="", **extra):
    status = int(getattr(response, "status_code", 0) or 0) if response is not None else 0
    raw_url = str(final_url or getattr(response, "url", "") or "")
    parsed_url = urlparse(raw_url)
    safe_url = (
        f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        if parsed_url.scheme and parsed_url.netloc
        else str(parsed_url.path or "")
    )
    return {
        "final_url": safe_url,
        "cookie_presence": _cookie_presence(session) if session is not None else {},
        "sentinel_source": str(sentinel_source or ""),
        "sentinel_flow": str(sentinel_flow or ""),
        "http_status": status,
        "proxy": redact_proxy_url(proxy),
        **extra,
    }


def _print_protocol_diagnostic(stage, diagnostic):
    """Surface the cookie/URL/protocol snapshot for one auth stage.

    The snapshot is a diagnosis aid, not operator signal: on a healthy stage it
    is a wall of cookie-presence booleans, and because it is emitted as
    ``Protocol diagnostic[...]: {json}`` -- line head is not ``{`` -- the host's
    JSON folder never folds it, so every stage of every account lands in the
    panel as ~330 characters. Keep healthy stages on the debug log; print only
    when the stage did not behave, which is when someone actually needs to read
    it.
    """
    safe = dict(diagnostic or {})
    url = urlparse(str(safe.get("final_url") or ""))
    safe["final_url"] = f"{url.scheme}://{url.netloc}{url.path}" if url.scheme and url.netloc else str(url.path or "")
    line = f"  Protocol diagnostic[{stage}]: {json.dumps(safe, ensure_ascii=False, sort_keys=True)}"
    status = safe.get("http_status")
    status = status if isinstance(status, int) and not isinstance(status, bool) else 0
    # 2xx is success, 3xx is the ordinary OAuth redirect hop. Anything else --
    # including a missing status, meaning no usable response -- is worth a line.
    if 200 <= status < 400:
        logger.debug(line.strip())
        return
    print(line)


def _authorize_continue_sentinel(
    session,
    did,
    proxy="",
    sentinel_token="",
    sentinel_so_token="",
):
    """Fetch a fresh, same-flow Sentinel challenge for API continue calls."""
    from .sentinel import issue_sentinel_flow

    issued = issue_sentinel_flow(
        flow="authorize_continue",
        device_id=did,
        session=session,
        proxy=proxy,
        supplied_data={
            "sentinel_authorize_continue_token": sentinel_token,
            "sentinel_authorize_continue_so_token": sentinel_so_token,
        },
    )
    data = {
        "sentinel_authorize_continue_token": issued.token,
        "sentinel_authorize_continue_so_token": issued.so_token,
        "sentinel_source": "node_sdk_runner",
        "oai_did": issued.device_id,
    }
    return data, issued.token, issued.so_token


def _signup_signin_attempts():
    return (
        {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},
        {"name": "signup_prompt_signup", "screen_hint": "signup", "prompt": "signup"},
        {"name": "signup_legacy_prompt_login", "screen_hint": "signup", "prompt": "login"},
    )


def _passwordless_signin_attempts():
    return (
        # Match the stable browser signup entry. The prompt is intentionally
        # omitted; prompt=login selects the existing-login password page for
        # unregistered mailboxes on some exits.
        {"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": ""},
        {"name": "login_or_signup_prompt_signup", "screen_hint": "login_or_signup", "prompt": "signup"},
        {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},
    )


def _invalid_state_auth_response(data):
    if not isinstance(data, dict):
        return False
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = str(error.get("code") or "").strip().lower()
    message = str(error.get("message") or "").strip().lower()
    return code == "invalid_state" or "session is no longer valid" in message


def _continue_signup_username(session, username, did, auth_base, base_headers, current_url, sentinel_token="", sentinel_so_token="", proxy=""):
    """Ensure auth.openai.com has an active signup state before user/register.

    Recent auth flows may bounce the initial NextAuth authorize request back to
    chatgpt.com/auth/login.  Posting user/register from that landing page always
    returns invalid_state, so advance the auth session with the username first.
    """
    if _is_signup_password_step(current_url) or _is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}

    fresh_data, fresh_token, fresh_so = _authorize_continue_sentinel(
        session,
        did,
        proxy=proxy,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
    )
    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/create-account"
    headers = {
        **base_headers,
        **openai_auth_headers(
            did,
            referer=referer,
            origin=auth_base,
            sentinel_token=fresh_token,
            sentinel_so_token=fresh_so,
            extra={"Content-Type": "application/json"},
        ),
    }

    response = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/authorize/continue",
        label="Signup username continue",
        json={"username": {"value": username, "kind": "email"}},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    body = _json_or_raw(response, limit=1000)
    next_url = _response_next_url(response, auth_base)
    print(f"  Signup username continue: {response.status_code}" + (f" {next_url}" if next_url else ""))
    diagnostic = _protocol_diagnostic(response=response, final_url=next_url, session=session,
                                      sentinel_source=fresh_data.get("sentinel_source", ""),
                                      sentinel_flow="authorize_continue", proxy=proxy)
    _print_protocol_diagnostic("authorize_continue", diagnostic)
    if response.status_code != 200:
        circuit = getattr(session, "_openai_registration_circuit", {})
        retry_after = circuit.get("retry_after", 0) if isinstance(circuit, dict) else 0
        return {
            "ok": False,
            "status": response.status_code,
            "body": body,
            "url": next_url,
            "retry_after_seconds": retry_after,
        }

    final_url = next_url
    if next_url and not next_url.endswith("/api/accounts/authorize/continue"):
        try:
            follow = _follow_continue_url(
                session,
                next_url,
                base_headers,
                referer=referer,
                label="Signup username continue follow",
            )
            final_url = str(getattr(follow, "url", "") or next_url)
        except Exception as exc:
            return {"ok": False, "status": response.status_code, "body": body, "url": next_url, "error": f"continue_follow_failed:{exc}"}
    diagnostic["final_url"] = final_url
    return {"ok": True, "status": response.status_code, "body": body, "url": final_url,
            "diagnostic": diagnostic}


def _prime_email_verification_page(session, auth_base, base_headers, current_url):
    """Load /email-verification once before posting OTP resend/send.

    Browser HAR shows the 302 from /api/accounts/authorize is followed by a
    real navigation to /email-verification, and then the page issues
    /api/accounts/email-otp/resend.  If protocol mode stops at the 302 only,
    the auth session can be present but not fully advanced for the resend
    endpoint, which commonly returns HTTP 400.
    """
    if not _is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}
    url = _absolute_url(auth_base, current_url)
    try:
        response = request_with_retry(
            session,
            "get",
            url,
            label="Email verification page",
            headers={
                **base_headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": url,
            },
            allow_redirects=False,
            impersonate=auth_impersonate(),
        )
        next_url = _response_next_url(response, auth_base)
        if response.status_code in (200, 204, 304) or _is_email_verification_step(next_url):
            print(f"  Email verification page: {response.status_code}")
            return {"ok": True, "status": response.status_code, "url": url}
        print(f"  Email verification page: {response.status_code} {next_url}")
        return {"ok": False, "status": response.status_code, "url": next_url}
    except Exception as exc:
        print(f"  Email verification page warning: {exc}")
        return {"ok": False, "error": str(exc), "url": current_url}


def _prepare_signup_auth_state(
    session,
    username,
    did,
    session_logging_id,
    auth_base,
    chat_base,
    base_headers,
    csrf_token,
    sentinel_token="",
    authorize_sentinel_token="",
    sentinel_so_token="",
    proxy="",
    passwordless_web=False,
    attempts=None,
):
    signin_payload = {
        "csrfToken": csrf_token,
        "callbackUrl": f"{chat_base}/",
        "json": "true",
    }
    last_state = {"ok": False, "error": "signup_auth_not_started"}

    for attempt in (attempts or _signup_signin_attempts()):
        name = attempt["name"]
        signin_url = _openai_signin_url(
            chat_base,
            did,
            session_logging_id,
            username,
            screen_hint=attempt.get("screen_hint", ""),
            prompt=attempt.get("prompt", ""),
        )
        signin_resp = request_with_retry(
            session,
            "post",
            signin_url,
            label=f"Auth signin {name}",
            data=urlencode(signin_payload),
            headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate=auth_impersonate(),
        )
        signin_body = _json_or_raw(signin_resp, limit=1000)
        auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
        auth_session_url = _ensure_authorize_context(
            auth_session_url,
            did,
            session_logging_id,
            username,
            screen_hint=attempt.get("screen_hint", ""),
            prompt=attempt.get("prompt", ""),
        )
        if not auth_session_url:
            last_state = {"ok": False, "attempt": name, "error": "missing_auth_session_url", "body": signin_body}
            continue

        authorize_resp = request_with_retry(
            session,
            "get",
            auth_session_url,
            label=f"Auth authorize {name}",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": f"{chat_base}/"},
            allow_redirects=True,
            impersonate=auth_impersonate(),
        )
        current_url = str(authorize_resp.url or "")
        location = (
            getattr(authorize_resp, "headers", {}).get("location")
            or getattr(authorize_resp, "headers", {}).get("Location")
            or ""
        )
        # Some HTTP adapters retain the authorize URL even after a redirect;
        # use Location as a compatibility fallback when no navigation occurred.
        if location and urlparse(current_url).path.rstrip("/") in {"", "/api/accounts/authorize"}:
            current_url = _absolute_url(auth_base, location)
        redirect_path = urlparse(current_url).path or "/"
        diagnostic = _protocol_diagnostic(response=authorize_resp, final_url=current_url, session=session,
                                          sentinel_source="", sentinel_flow="", proxy=proxy,
                                          signin_attempt=name)
        print(f"  Redirect[{name}]: {authorize_resp.status_code} {redirect_path} "
              f"cookies={diagnostic['cookie_presence']}")
        _print_protocol_diagnostic("authorize", diagnostic)

        login_redirect_seen = _is_existing_login_redirect(current_url)

        if _is_chatgpt_auth_login_landing(current_url):
            last_state = {"ok": False, "attempt": name, "error": "redirected_to_chatgpt_login", "url": current_url}
            continue

        if _is_signup_password_step(current_url) or _is_email_verification_step(current_url):
            return {"ok": True, "attempt": name, "status": authorize_resp.status_code, "url": current_url,
                    "skipped": True, "diagnostic": diagnostic}

        # Passwordless Web/HAR flow is complete after authorize navigation.
        # The browser sends the OTP from this state; do not POST authorize/continue.
        if passwordless_web:
            if _is_chatgpt_auth_login_landing(current_url):
                last_state = {"ok": False, "attempt": name, "status": authorize_resp.status_code,
                              "url": current_url, "error": "authorize_redirect_not_advanced",
                              "diagnostic": diagnostic}
                continue
            if _is_existing_login_redirect(current_url):
                # The server can explicitly disable passwordless signup for an
                # exit (the client_auth_session dump reports
                # passwordless_disabled=true) and route to /log-in/password.
                # This is no longer the passwordless Web path; use the legacy
                # username transition only for this explicit password fallback.
                signup_state = _continue_signup_username(
                    session, username, did, auth_base, base_headers, current_url,
                    sentinel_token=authorize_sentinel_token or sentinel_token,
                    sentinel_so_token=sentinel_so_token, proxy=proxy,
                )
                signup_state["attempt"] = name
                signup_state["password_fallback"] = True
                signup_state.setdefault("diagnostic", diagnostic)
                if signup_state.get("ok") and not _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
                    return signup_state
                last_state = {**signup_state, "error": "authorize_login_page"}
                continue
            # The normal Web path receives the OTP from authorize and proceeds
            # via email verification without /authorize/continue.
            return {"ok": True, "attempt": name, "status": authorize_resp.status_code,
                    "url": current_url, "diagnostic": diagnostic}

        signup_state = _continue_signup_username(
            session,
            username,
            did,
            auth_base,
            base_headers,
            current_url,
            sentinel_token=authorize_sentinel_token or sentinel_token,
            sentinel_so_token=sentinel_so_token,
            proxy=proxy,
        )
        signup_state["attempt"] = name
        signup_state["login_redirect_seen"] = login_redirect_seen
        signup_state.setdefault("diagnostic", diagnostic)
        if login_redirect_seen and _is_existing_login_redirect(signup_state.get("url", "")):
            last_state = {
                **signup_state,
                "ok": False,
                "error": "login_redirect_not_advanced",
            }
            continue
        if signup_state.get("ok") and not _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            return signup_state

        last_state = signup_state
        if signup_state.get("status") == 409 and _invalid_state_auth_response(signup_state.get("body")):
            continue
        if _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            continue
        return signup_state

    return last_state


# ==========================================
# Existing-account login flow (email OTP + TOTP challenge)
# ==========================================
LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"


def _auth_request_headers(base_headers, did="", referer="", origin="", sentinel_token="", sentinel_so_token="", extra=None):
    return {
        **(base_headers or {}),
        **openai_auth_headers(
            did,
            referer=referer,
            origin=origin,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
            extra=extra or {},
        ),
    }


def _otp_challenge_established(body):
    """Return True when an email-OTP send response carries a real challenge.

    ``/api/accounts/email-otp/resend`` can answer ``200 {"success": true}`` --
    a bare acknowledgement that does **not** move the auth session into the
    login email-verification state.  Treating that as success returned before
    ``/api/accounts/email-otp/send`` was ever tried, and ``send`` is the
    endpoint that returns the actual challenge (recorded live on 2026-09-12)::

        {"continue_url": "https://auth.openai.com/email-verification",
         "method": "GET",
         "page": {"type": "email_otp_verification", ...,
                  "payload": {"email_verification_mode": "login..."}}}

    Without the challenge the OTP mail is not bound to a login transaction, so
    ``/api/accounts/email-otp/validate`` answers ``401 login_failed`` -- which
    is what every existing-login attempt did on 2026-09-14.

    The ordering that this check is paired with has since moved on: the caller
    now posts ``email-otp/send`` **first** and stops after a bare
    acknowledgement rather than falling through to the sibling endpoint (see
    ``_send_existing_login_otp``).  This predicate is unchanged.
    """
    if not isinstance(body, dict):
        return False
    if str(body.get("continue_url") or "").strip():
        return True
    page = body.get("page")
    page = page if isinstance(page, dict) else {}
    if str(page.get("type") or "").strip() == "email_otp_verification":
        return True
    payload = page.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    for source in (body, payload):
        if str(source.get("email_verification_mode") or "").strip():
            return True
    return False


def _send_existing_login_otp(session, auth_base, base_headers, current_url, did, sentinel_token="", sentinel_so_token=""):
    headers = _auth_request_headers(
        base_headers,
        did=did,
        referer=current_url or f"{auth_base}/email-verification",
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    last_response = None
    acknowledged_response = None
    # Browser order is: authorize -> GET /email-verification -> the page issues
    # the OTP POST.  That POST is ``email-otp/send``: it dispatches the first
    # code for the login challenge and it is the endpoint that returns the
    # challenge payload itself.
    #
    # ``email-otp/resend`` only re-arms an *existing* challenge.  Measured over
    # the whole of ``runtime/logs/backend_stdout.log`` (2026-09-14): resend was
    # called 24 times and established a challenge **0** times -- 15x
    # ``200 {"success": true}`` bare acknowledgement, 6x ``409 "Your sign-in
    # session is no longer valid. Please start over."``, 3x ``429`` -- while
    # ``send`` established it 18/18 times it was reached.  It was also the only
    # endpoint that ever answered 429, so having it first made the *useless*
    # call the one that aborted the attempt: three recorded
    # ``existing_login_otp_send_failed:429`` runs never reached ``send`` at all.
    # The six ``409`` runs prove ``send`` does not depend on a prior ``resend``
    # having succeeded.
    #
    # Hence ``send`` first.  ``resend`` is kept only as the fallback for the
    # endpoint-level rejections below (400/404/405), which ``send`` has never
    # returned on this lane.  ``passwordless/send-otp`` is deliberately NOT in
    # this list: it belongs to account creation and answers 409 invalid_state.
    #
    # 🔴 2026-09-21 -- the method is **per endpoint**, and ``send`` must be
    # ``GET``.  It used to be ``POST`` for both, which armed the *wrong*
    # challenge: ``send`` answered 200 with a well-formed
    # ``page.type=email_otp_verification`` payload whose
    # ``email_verification_mode`` was ``login_challenge``, the transaction dump
    # stayed healthy right up to the validate call, and then
    # ``/api/accounts/email-otp/validate`` **always** answered
    # ``409 {"code": "invalid_state"}`` ("Your sign-in session is no longer
    # valid") -- after which the dump answered 404, i.e. the POST'd challenge
    # had never been bound to the login transaction at all.
    #
    # Measured on 2026-09-21, single account, clean log:
    #     POST send -> mode=login_challenge -> validate 409   (5/5 in the batch
    #                  runner, plus 4/4 in targeted probes)
    #     GET  send -> mode=passwordless_login -> validate 200
    #                  ({"continue_url": ".../api/auth/callback/openai?code=ac_..."})
    #
    # The 409-vs-401 discriminator is what pinned it: with the mailbox poll
    # replaced by a garbage code the same lane answers ``401
    # wrong_email_otp_code`` (session provably valid) while 20s of idle, a real
    # mailbox read, and an unchanged egress IP all leave it at 401.  Only
    # submitting a *genuinely issued* code turns it into 409 -- because that
    # code belongs to the ``passwordless_login`` challenge the GET creates, not
    # to the ``login_challenge`` the POST created.
    #
    # This is the same class of bug the signup lane already documents at
    # ``registration_handlers.user_register``: the server names the method it
    # wants (``"method": "GET"`` in its own response bodies) and ignoring it
    # makes every later ``email-otp/validate`` fail.  Do not "simplify" this
    # back to a single POST.
    for endpoint, method in (
        ("/api/accounts/email-otp/send", "get"),
        ("/api/accounts/email-otp/resend", "post"),
    ):
        response = request_with_retry(
            session,
            method,
            _absolute_url(auth_base, endpoint),
            label=f"Existing account OTP send {endpoint}",
            **({} if method == "get" else {"json": {}}),
            headers=headers,
            impersonate=auth_impersonate(),
        )
        last_response = response
        body = None
        try:
            body = response.json()
        except Exception:
            body = None
        body_preview = ""
        try:
            body_preview = json.dumps(body, ensure_ascii=False)[:200]
        except Exception:
            body_preview = (response.text or "")[:200]
        print(f"  Existing account OTP send: {endpoint} {response.status_code} {body_preview}")
        if response.status_code in (200, 202, 204):
            if _otp_challenge_established(body):
                print(f"  Existing account OTP challenge established by {endpoint}")
                return True, response
            if acknowledged_response is None:
                acknowledged_response = response
            # A bare 2xx acknowledgement carries no challenge.  Posting the
            # *other* endpoint now would dispatch a second code for the same
            # transaction, which abai's protocol client treats as
            # transaction-invalidating -- and it cannot help, because ``resend``
            # has never carried a challenge (0/24 measured) while ``send``
            # carries it 18/18.  Stop here and let the caller continue on the
            # acknowledgement rather than paying a second dispatch to learn
            # nothing.
            print(
                f"  Existing account OTP {endpoint} acknowledged without a challenge; "
                f"not trying the other endpoint (would dispatch a second code)"
            )
            break
        # 409 may mean "OTP already sent recently" — treat as success but
        # only when the response body confirms a pending OTP. Otherwise keep
        # trying alternate endpoints.
        if response.status_code == 409:
            body_lower = body_preview.lower()
            if "already" in body_lower or "pending" in body_lower or "rate" in body_lower or "too_many" in body_lower:
                return True, response
            # Ambiguous 409: try next endpoint
            continue
        if response.status_code not in (400, 404, 405):
            return False, response
    # No endpoint carried a challenge.  Fall back to the first bare
    # acknowledgement so this can never be worse than the previous behaviour.
    if acknowledged_response is not None:
        print("  Existing account OTP: no endpoint returned a challenge payload; continuing with acknowledgement")
        return True, acknowledged_response
    # All endpoints exhausted; return last response so caller can decide
    if last_response is not None:
        return False, last_response
    return False, None


def _fetch_session_csrf_token(session, chat_base, base_headers, did, session_logging_id):
    """Mint a NextAuth CSRF token on ``session`` itself.

    NextAuth binds ``csrfToken`` to the ``__Host-next-auth.csrf-token`` cookie
    of the *same* client.  A token minted on another session is rejected, and
    NextAuth answers with ``chatgpt.com/auth/login`` instead of the
    auth.openai.com authorize URL -- after which every later
    ``authorize/continue`` is answered with ``409 invalid_state``.

    The signup lane never hits this because it primes ``chat_base`` and reads
    ``/api/auth/csrf`` on the very session it then posts with.  The
    existing-login lane used to reuse the caller's token across a freshly built
    session, so ``registration_handlers``' two call sites behaved differently:
    the one passing ``s.session`` was self-consistent, the one passing a brand
    new ``s.login_session`` was not.

    Returns ``""`` on any transport failure so the caller can fall back to the
    token it was given -- this must never turn a recoverable login into a hard
    transport error.
    """
    try:
        request_with_retry(
            session,
            "get",
            f"{chat_base}/",
            label="Existing account prime",
            headers={
                **base_headers,
                "Accept": "text/html,application/xhtml+xml",
                "Referer": f"{chat_base}/",
            },
            impersonate=auth_impersonate(),
        )
    except Exception as exc:
        print(f"  Existing account prime transport warning: {exc}")
    try:
        response = request_with_retry(
            session,
            "get",
            f"{chat_base}/api/auth/csrf",
            label="Existing account csrf",
            headers=nextauth_headers(
                did,
                session_id=session_logging_id,
                referer=f"{chat_base}/",
                origin=chat_base,
            ),
            impersonate=auth_impersonate(),
        )
        body = _json_or_raw(response, limit=1000)
        if not isinstance(body, dict):
            return ""
        return str(body.get("csrfToken") or "").strip()
    except Exception as exc:
        print(f"  Existing account csrf transport warning: {exc}")
        return ""


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
    headers = _auth_request_headers(
        base_headers,
        did=did,
        referer=current_url or f"{auth_base}/log-in/password",
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    response = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/password/verify",
        label="Existing account password verify",
        json={"password": password},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    print(f"  Existing account password verify: {response.status_code}")
    if response.status_code != 200:
        return {
            "ok": False,
            "error": f"existing_login_password_verify_failed:{response.status_code}",
            "login_method": "password",
        }

    payload = _json_or_raw(response, limit=1000)
    next_url = _response_next_url(response, auth_base)
    if next_url:
        try:
            followed = _follow_continue_url(
                session,
                next_url,
                base_headers,
                referer=current_url or f"{auth_base}/log-in/password",
                label="Existing account password continue",
            )
        except Exception as exc:
            print(f"  Existing account password continue transport warning: {exc}")
        else:
            followed_body = _json_or_raw(followed, limit=1000)
            if isinstance(followed_body, dict) and followed_body:
                payload = followed_body

    mfa_result = _complete_existing_login_totp(
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
    if _is_about_you_step(final_url, data):
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
            _follow_continue_url(
                session,
                final_url,
                base_headers,
                referer=f"{auth_base}/log-in/password",
                label="Existing account password landing",
            )
        except Exception as exc:
            print(f"  Existing account password landing transport warning: {exc}")
    return {"ok": True, "login_method": "password"}


def _existing_login_signin(session, username, did, session_logging_id, auth_base, chat_base, base_headers, csrf_token):
    print("  Existing account login: probing the login method before spending an email code")
    # The caller may hand us a token minted on a *different* session: one of
    # registration_handlers' two call sites builds a brand new ``login_session``
    # and still passes the main session's ``s.csrf_token``.  Mint the token on
    # the session we are about to post with, and only fall back to the caller's
    # value when that fails -- so this can never be worse than the old
    # behaviour, only more consistent.
    session_csrf_token = _fetch_session_csrf_token(
        session, chat_base, base_headers, did, session_logging_id
    )
    if session_csrf_token:
        csrf_token = session_csrf_token
    signin_url = (
        f"{chat_base}/api/auth/signin/openai"
        f"?prompt=login&ext-oai-did={did}"
        f"&auth_session_logging_id={session_logging_id}"
        f"&screen_hint=login"
        f"&login_hint={quote(username, safe='')}"
    )
    signin_payload = {
        "csrfToken": csrf_token,
        "callbackUrl": f"{chat_base}/",
        "json": "true",
    }
    signin_resp = request_with_retry(
        session,
        "post",
        signin_url,
        label="Existing account signin",
        data=urlencode(signin_payload),
        headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded", "Origin": chat_base, "Referer": f"{chat_base}/"},
        impersonate=auth_impersonate(),
    )
    signin_body = _json_or_raw(signin_resp, limit=1000)
    auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
    # ``_ensure_authorize_context`` fills the same context parameters the signup
    # lane relies on (device_id / ext-oai-did / auth_session_logging_id /
    # passkey capabilities / ccaps / login_hint / screen_hint).  Adding only
    # ``device_id`` left the authorize request unattributable to this mailbox's
    # login transaction.  It is a no-op for non auth.openai.com URLs and never
    # overwrites a parameter the URL already carries.
    auth_session_url = _ensure_authorize_context(
        auth_session_url,
        did,
        session_logging_id,
        username,
        screen_hint="login",
        prompt="login",
    )
    authorize_resp = request_with_retry(
        session,
        "get",
        auth_session_url,
        label="Existing account authorize",
        headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Origin": auth_base, "Referer": f"{chat_base}/"},
        impersonate=auth_impersonate(),
    )
    current_url = str(authorize_resp.url or "")
    print(f"  Existing account authorize: {authorize_resp.status_code} {current_url}")

    current_lower = current_url.lower()
    if "chatgpt.com" in current_lower and ("/api/auth/callback/openai" in current_lower or current_lower.rstrip("/") == chat_base.lower().rstrip("/")):
        return ({"ok": True}, None)

    if _is_chatgpt_auth_login_landing(current_url):
        # Observed live on 2026-09-13: when NextAuth does not establish a
        # session on this client, the authorize request bounces to
        # ``chatgpt.com/auth/login`` instead of auth.openai.com.  Posting
        # authorize/continue from that landing page is answered with
        # ``409 invalid_state`` -- and so is every OTP call after it, which made
        # the whole failure read as an OTP problem (``existing_login_otp_validate``)
        # while burning ~90s of mailbox polling per account.  Stop here instead.
        # The signup lane already treats this landing as fatal
        # (``_prepare_signup_auth_state`` retries the next attempt); this lane
        # used to walk straight into the doomed continue.
        # ``invalid_state`` appears in the message on purpose: it keeps this
        # classified as the retryable ``auth_state`` class, exactly like the
        # failure it pre-empts, instead of degrading to ``unknown`` (terminal).
        return (
    {        "ok": False,
            "error": f"existing_login_signin_not_established:invalid_state:{current_url[:120]}",
        }        , None)

    # The signup lane documents the opposite rule for this exact landing:
    # ``_prepare_signup_auth_state`` returns early when the authorize redirect
    # already landed on ``/email-verification`` -- "the browser sends the OTP
    # from this state; do not POST authorize/continue".  Posting it here was
    # added as a workaround for "OTP send returns 409", a symptom of the CSRF
    # bug that ``_fetch_session_csrf_token`` has since removed.  It now corrupts
    # the session instead: after it, the client auth session still carries
    # signup-era state (``passwordless_login_magic_link_sent``) and a *different*
    # ``email_verification_mode`` than the 2026-09-12 successful login, and is
    # missing ``username`` / ``passwordless_disabled`` /
    # ``passwordless_otp_from_password_redirect`` -- after which every
    # ``email-otp/validate`` answers ``401 login_failed`` (3/3 on 2026-09-14).
    # The transaction's own answer is the cheapest signal available, so keep the
    # payload and the continue target -- not just the status code.  The password
    # probe below reads them before any email code is spent.
    return (None, {'csrf_token': csrf_token, 'current_url': current_url})


def _existing_login_continue(session, username, did, auth_base, base_headers, proxy, current_url):
    continue_payload = {}
    continue_next_url = ""
    on_verified_page = _is_email_verification_step(current_url)
    if on_verified_page and not _existing_login_continue_enabled():
        # Only the POST is dropped -- the sentinel is still minted so the OTP
        # send and TOTP steps keep using a token fresher than the caller's.
        fresh_data, fresh_token, fresh_so = _authorize_continue_sentinel(session, did, proxy=proxy)
        print("  Existing account continue: skipped (already at email-verification)")
    else:
        fresh_data, fresh_token, fresh_so = _authorize_continue_sentinel(session, did, proxy=proxy)
        continue_body_json = {"username": {"value": username, "kind": "email"}}
        if on_verified_page:
            # 🔴 2026-09-16: the OTP page is a *generic shell*, not the
            # transaction's answer.  Dropping this POST **was** the whole bug:
            # the password probe reads the ``continue_url`` this response
            # carries, so with the POST dropped the probe could only ever
            # answer ``None`` (``no_transaction_state``) -- and on the signup
            # lane, which passes ``allow_passwordless=False`` for an address
            # the server already reported as registered, ``None`` is terminal
            # rather than a fallback.  Measured that day: 19/19 attempts died
            # on exactly this path, zero exceptions.
            #
            # ``screen_hint: "login"`` is the one field abai's
            # ``_submit_login_email`` sends that this body did not.  The
            # 09-14 corruption the POST was dropped for ("the session still
            # carries signup-era state ... missing username /
            # passwordless_disabled / passwordless_otp_from_password_redirect")
            # is precisely what a *signup* continue produces -- and a body of
            # ``username`` alone is what the signup lane posts
            # (``_prepare_signup_auth_state``).  Declaring the screen hint is
            # therefore the minimal change that can restore the transaction
            # state without asking the server to continue a signup.
            #
            # Toggle: ``registration.existing_login_continue_on_verified_page``
            # (default True).  Set it to false to restore the 09-14 skip.
            continue_body_json["screen_hint"] = "login"
        continue_resp = None
        try:
            continue_resp = request_with_retry(
                session,
                "post",
                f"{auth_base}/api/accounts/authorize/continue",
                label="Existing account continue",
                json=continue_body_json,
                headers=_auth_request_headers(
                    base_headers,
                    did=did,
                    referer=current_url or f"{auth_base}/log-in",
                    origin=auth_base,
                    sentinel_token=fresh_token,
                    sentinel_so_token=fresh_so,
                    extra={"Content-Type": "application/json"},
                ),
                impersonate=auth_impersonate(),
            )
        except Exception as exc:
            # On the verified page this POST is an *addition* to the old
            # behaviour, so it must never be able to fail the lane: fall back
            # to the skip -- no transaction state, probe answers
            # ``no_transaction_state`` -- exactly as before.  Off that page the
            # POST is the original path and a transport error still propagates.
            if not on_verified_page:
                raise
            print(f"  Existing account continue transport warning: {exc}")
        if continue_resp is not None:
            print(f"  Existing account continue: {continue_resp.status_code}")
            continue_body = _json_or_raw(continue_resp, limit=1000)
            if isinstance(continue_body, dict):
                continue_payload = continue_body
            _print_protocol_diagnostic(
                "existing_authorize_continue",
                _protocol_diagnostic(
                    response=continue_resp,
                    final_url=_response_next_url(continue_resp, auth_base),
                    session=session,
                    sentinel_source=fresh_data.get("sentinel_source", ""),
                    sentinel_flow="authorize_continue",
                    proxy=proxy,
                ),
            )
            if continue_resp.status_code == 200:
                next_url = _response_next_url(continue_resp, auth_base)
                continue_next_url = next_url
                if next_url:
                    try:
                        follow_resp = _follow_continue_url(
                            session,
                            next_url,
                            base_headers,
                            referer=next_url,
                            label="Existing account continue follow",
                        )
                        current_url = str(getattr(follow_resp, "url", "") or next_url)
                    except Exception as e:
                        print(f"  Existing account continue follow transport warning: {e}")
            elif on_verified_page:
                # A refusal here is not new information -- the old behaviour
                # never asked, so it must not become a new failure mode.  Keep
                # the skip and let the probe report why it could not answer.
                print(
                    "  Existing account continue refused on the verified page: "
                    f"{continue_resp.status_code} (keeping the skip)"
                )
            elif continue_resp.status_code not in (409, 400):
                return ({"ok": False, "error": f"existing_login_continue_failed:{continue_resp.status_code}"}, None)

    # ------------------------------------------------------------------ probe
    # ``/log-in/password`` is the sibling auth step of email OTP inside this
    # same transaction, so the transaction can answer "does this account have a
    # password?" without consuming an email code.  Ask that first: the OTP lane
    # is a measured dead end (5/5 landings on the signup profile step
    # ``/about-you``, 0 NextAuth sessions) and costs a ~59s mailbox poll plus a
    # code to rediscover.  abai's protocol client reaches the same verdict from
    # the same signal and refuses to fall back to email OTP at all.
    return (None, {'current_url': current_url, 'continue_payload': continue_payload, 'continue_next_url': continue_next_url, 'fresh_token': fresh_token, 'fresh_so': fresh_so})


def _existing_login_probe(session, did, auth_base, base_headers, sentinel_token, sentinel_so_token, totp_secret, password, allow_passwordless, current_url, continue_payload, continue_next_url, fresh_token, fresh_so):
    probe = _probe_login_password_step(
        session,
        auth_base,
        base_headers,
        current_url,
        payload=continue_payload,
        continue_url=continue_next_url,
    )
    print(
        f"  Existing account login method probe: password_step={probe['password_step']} "
        f"({probe['signal'] or 'no_signal'}; source={probe.get('source') or 'none'})"
    )
    if probe["password_step"] is False:
        # The transaction answered and carried no password form.  The account is
        # passwordless, so an email code is the only remaining method -- and it
        # lands on the signup profile step and yields no session.  Stop here
        # rather than spending the code to learn the same thing.
        return (
            {
                "ok": False,
                "error": f"existing_login_no_password_step:{probe['signal']}",
                "login_method": "probe",
                "password_probe": probe,
            },
            None,
        )
    if probe["password_step"] is True:
        # A password step is reachable, so the email lane cannot produce a
        # session on this transaction -- spend the password we hold instead.
        if not str(password or "").strip():
            return (
                {
                    "ok": False,
                    "error": "existing_login_password_required",
                    "login_method": "probe",
                    "password_probe": probe,
                },
                None,
            )
        return (
            _password_login_existing_account(
                session,
                auth_base,
                base_headers,
                str(password).strip(),
                current_url,
                did,
                sentinel_token=fresh_token or sentinel_token,
                sentinel_so_token=fresh_so or sentinel_so_token,
                totp_secret=totp_secret,
            ),
            None,
        )
    # ``None`` -- the probe could not tell.  A probe that cannot answer is not
    # evidence of absence, so keep the email lane when the caller still allows
    # it.  A caller that already knows this address is registered
    # (``allow_passwordless=False``) refuses instead: for that address the email
    # lane is a measured dead end, and spending a code to rediscover it is the
    # exact cost this probe exists to avoid.
    if not allow_passwordless:
        return (
    {        "ok": False,
            "error": "existing_login_password_step_unknown",
            "login_method": "probe",
            "password_probe": probe,
        }        , None)

    return (None, {})


def _existing_login_otp(session, mailbox, did, auth_base, base_headers, proxy, sentinel_token, sentinel_so_token, totp_secret, otp_timeout, current_url, fresh_token, fresh_so):
    otp_send_started = int(time.time())
    ok, otp_send_response = _send_existing_login_otp(
        session,
        auth_base,
        base_headers,
        current_url,
        did,
        sentinel_token=fresh_token or sentinel_token,
        sentinel_so_token=fresh_so or sentinel_so_token,
    )
    if not ok:
        status = getattr(otp_send_response, "status_code", 0)
        if status == 429:
            # The signup lane already answers this exact 429 by arming the
            # process-wide registration rate-limit circuit
            # (``registration_handlers._prepare_signup_auth_state``).  This
            # lane used to collapse it to a bare status code instead, which
            # classified ``unknown`` and left the batch marching: the throttle
            # is per-exit, not per-address, so every later account in the run
            # walked into it.  Measured 2026-09-14: three addresses in one run,
            # each preceded by a full signup attempt, with no pause between
            # them.
            #
            # Reuse the signup lane's circuit, its ``Retry-After`` parser and
            # its wording rather than inventing a second policy here.  The
            # string keeps the hard-stop semantics it already has
            # (``registration_rate_limited`` is both a ``rate_limit`` marker and
            # a terminal marker), so the address is still not retried -- what
            # changes is that the rest of the batch waits instead of being
            # spent against a throttled endpoint.
            retry_after = float(_retry_after_seconds(otp_send_response, default=300.0))
            mark_registration_rate_limited(retry_after)
            return ({"ok": False, "error": f"registration_rate_limited:retry_after={retry_after:.0f}s"}, None)
        return ({"ok": False, "error": f"existing_login_otp_send_failed:{status}"}, None)

    email_cfg = current_config_data().get("email_registration", {})
    poll_timeout = int(otp_timeout or email_cfg.get("otp_timeout", 300))
    code = _poll_email_otp(
        mailbox,
        subject_keyword=LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
        timeout=poll_timeout,
        issued_after_unix=otp_send_started,
        proxy=proxy,
    )
    if not code:
        return ({"ok": False, "error": "existing_login_otp_poll_timeout"}, None)

    # Match the signup lane, which is the only OTP path that returns 200 in the
    # same run: ``registration_handlers.validate_email_otp`` calls
    # ``_validate_email_otp(..., use_sentinel=False)``.  This lane instead
    # attached the sentinel minted for ``authorize/continue``, and every
    # validate came back ``401 login_failed`` on 2026-09-14 -- even after
    # ``email-otp/send`` had established a correct login challenge
    # (``continue_url`` + ``page.type=email_otp_verification``).
    # ``codex_oauth`` proves a sentinel is not inherently fatal on this endpoint,
    # but it attaches one cached for the passwordless flow rather than the
    # authorize/continue one, so the *flow binding* is the suspect and the
    # proven lane's choice is the safest parity target.
    otp_ok, otp_data = _validate_email_otp(
        session,
        auth_base,
        base_headers,
        code,
        sentinel_data={
            "sentinel_token": fresh_token or sentinel_token,
            "sentinel_so_token": fresh_so or sentinel_so_token,
        },
        use_sentinel=False,
    )
    if not otp_ok:
        # The signup lane dumps the client auth session on this exact failure
        # (``registration_handlers.py`` ``after_otp_validate_failed``).  Without
        # it a 401 here is indistinguishable from a plain wrong-code rejection.
        try:
            _fetch_client_auth_session_dump(
                session, auth_base, base_headers, "existing_login_after_otp_validate_failed"
            )
        except Exception as exc:
            print(f"  Existing account otp-validate dump warning: {exc}")
        # 🔴 2026-09-16: ``identity_provider_mismatch`` is deliberately **not**
        # checked here.  It is a real server verdict, but not one this endpoint
        # can produce: its message reads *"You tried signing in as \"…\" **using
        # a password**"*, while this lane posts only ``{"code": code}`` to
        # ``email-otp/validate``.  It was observed exactly once, from
        # ``create_account`` (400, 2026-09-15 07:33:37, run ``b93f598d``), and
        # zero times under any ``email_otp_validate:`` /
        # ``existing_login_otp_validate:`` prefix across every process log.
        # ``registration_handlers.create_account`` owns that branch; see the
        # comment there and ``failure_registry.PASSWORDLESS_SIGNUP_CODE``.
        return ({"ok": False, "error": f"existing_login_otp_validate:{json.dumps(otp_data, ensure_ascii=False)[:200]}"}, None)
    mfa_result = _complete_existing_login_totp(
        session,
        auth_base,
        base_headers,
        otp_data,
        did=did,
        sentinel_token=fresh_token or sentinel_token,
        sentinel_so_token=fresh_so or sentinel_so_token,
        totp_secret=totp_secret,
    )
    if not mfa_result.get("ok"):
        return (mfa_result, None)
    otp_data = mfa_result.get("data") if isinstance(mfa_result.get("data"), dict) else otp_data
    final_url = str(otp_data.get("continue_url") or "")
    if _is_about_you_step(final_url, otp_data):
        # The OTP verified, but the server routed this login into the *signup*
        # profile step.  Following it cannot produce a NextAuth session -- the
        # client auth session is still the signup transaction -- so the
        # caller's readiness poll would spend four requests to learn
        # ``keys=['WARNING_BANNER']``.  Measured 2026-09-14: 5/5 landings on
        # ``/about-you``, 0 sessions.  Report it as a failure so the caller
        # stops instead of polling a known-dead state.
        print(f"  Existing account OTP continue: profile step, not a login landing ({final_url[:80]})")
        return (
    {        "ok": False,
            "error": f"existing_login_landed_on_profile_step:{final_url[:120]}",
        }        , None)
    try:
        _follow_continue_url(
            session,
            final_url,
            base_headers,
            referer=f"{auth_base}/email-verification",
            label="Existing account OTP continue",
        )
    except Exception as e:
        print(f"  Existing account OTP continue transport warning: {e}")
    return ({"ok": True}, None)


def _login_existing_account_with_email_otp(
    session,
    username,
    mailbox,
    did,
    session_logging_id,
    auth_base,
    chat_base,
    base_headers,
    csrf_token,
    proxy=None,
    sentinel_token="",
    sentinel_so_token="",
    totp_secret="",
    password="",
    allow_passwordless=True,
    otp_timeout=None,
):
    """Orchestrate the existing-account login: signin -> continue -> probe -> otp.

    Split 2026-09-19 (P1) from a single 415-line body into four phase
    functions.  Each phase returns ``(terminal, state)``: ``terminal`` is the
    final result dict when the phase settles the login, ``None`` while the
    pipeline should continue; ``state`` carries the locals the next phase
    reads (``current_url``, the continue payload, the fresh sentinel tokens).
    The signature and the returned dicts are unchanged, so the six production
    call sites and the seven test files need no edits.
    """
    terminal, state = _existing_login_signin(
        session, username, did, session_logging_id, auth_base, chat_base, base_headers, csrf_token
    )
    if terminal is not None:
        return terminal
    current_url = state["current_url"]

    terminal, state = _existing_login_continue(
        session, username, did, auth_base, base_headers, proxy, current_url
    )
    if terminal is not None:
        return terminal
    current_url = state["current_url"]
    continue_payload = state["continue_payload"]
    continue_next_url = state["continue_next_url"]
    fresh_token = state["fresh_token"]
    fresh_so = state["fresh_so"]

    terminal, _ = _existing_login_probe(
        session, did, auth_base, base_headers, sentinel_token, sentinel_so_token,
        totp_secret, password, allow_passwordless,
        current_url, continue_payload, continue_next_url, fresh_token, fresh_so,
    )
    if terminal is not None:
        return terminal

    terminal, _ = _existing_login_otp(
        session, mailbox, did, auth_base, base_headers, proxy, sentinel_token,
        sentinel_so_token, totp_secret, otp_timeout, current_url, fresh_token, fresh_so,
    )
    return terminal



def _complete_existing_login_totp(
    session,
    auth_base,
    base_headers,
    payload,
    *,
    did,
    sentinel_token="",
    sentinel_so_token="",
    totp_secret="",
):
    """Complete a saved TOTP challenge after email OTP verification."""
    if not _is_mfa_challenge_payload(payload):
        return {"ok": True, "data": payload}
    secret = str(totp_secret or "").strip()
    if not secret:
        return {"ok": False, "error": "existing_login_totp_secret_missing"}
    factor_id = _totp_factor_id(payload)
    if not factor_id:
        return {"ok": False, "error": "existing_login_totp_factor_missing"}
    try:
        import pyotp

        code = pyotp.TOTP(secret).now()
    except Exception:
        return {"ok": False, "error": "existing_login_totp_code_failed"}

    referer = _response_next_url_from_data(payload, auth_base) or f"{auth_base}/mfa-challenge/{factor_id}"
    headers = _auth_request_headers(
        base_headers,
        did=did,
        referer=referer,
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    issue = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/mfa/issue_challenge",
        label="Existing account TOTP challenge",
        json={"type": "totp", "id": factor_id, "force_fresh_challenge": False},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    if issue.status_code not in (200, 201, 202, 204):
        return {"ok": False, "error": f"existing_login_totp_issue_failed:{issue.status_code}"}
    verify = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/mfa/verify",
        label="Existing account TOTP verify",
        json={"type": "totp", "id": factor_id, "code": code},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    verify_data = _json_or_raw(verify, limit=1000)
    if verify.status_code != 200:
        return {"ok": False, "error": f"existing_login_totp_verify_failed:{verify.status_code}"}
    return {"ok": True, "data": verify_data}


def _is_mfa_challenge_payload(payload):
    if not isinstance(payload, dict):
        return False
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    if str(page.get("type") or "").strip().lower() == "mfa_challenge":
        return True
    return "/mfa-challenge/" in str(_response_next_url_from_data(payload, "") or "").lower()


def _totp_factor_id(payload):
    auth_session = payload.get("oai-client-auth-session") if isinstance(payload, dict) else {}
    if not isinstance(auth_session, dict):
        return ""
    factors = []
    for key in ("mfa_challenge_factors", "mfa_factors"):
        values = auth_session.get(key)
        if isinstance(values, list):
            factors.extend(item for item in values if isinstance(item, dict))
    for factor in factors:
        if str(factor.get("factor_type") or "").strip().lower() == "totp":
            factor_id = str(factor.get("id") or "").strip()
            if factor_id:
                return factor_id
    return ""


def _response_next_url_from_data(payload, auth_base):
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    value = str(payload.get("continue_url") or page_payload.get("url") or "").strip()
    return _absolute_url(auth_base, value) if value and auth_base else value
