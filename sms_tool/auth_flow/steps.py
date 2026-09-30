"""Pure step predicates, URL/response helpers and diagnostics.

Leaf module: it imports no sibling, and every other auth_flow submodule may
import it. Callers reach it module-qualified (``from . import steps`` then
``steps._is_about_you_step(...)``) so a test that patches
``sms_tool.auth_flow.steps._response_next_url`` is seen by every caller; a
direct ``from .steps import _response_next_url`` would bind at import time and
make the patch silently ineffective.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, quote, urlencode, urlparse

from . import deps

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
        cfg = deps.current_config_data().get("registration", {})
    except Exception:
        return True
    if not isinstance(cfg, dict):
        return True
    value = cfg.get("existing_login_continue_on_verified_page", True)
    return value not in (False, 0, "0", "false", "False", "no", "No", "off")


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
    body = deps._json_or_raw(response, limit=1000)
    if isinstance(body, dict):
        value = body.get("continue_url") or body.get("url")
        if value:
            return deps._absolute_url(base_url, value)
    location = getattr(response, "headers", {}).get("location") or getattr(response, "headers", {}).get("Location")
    if location:
        return deps._absolute_url(base_url, location)
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


def _protocol_diagnostic(
    *, response=None, final_url="", session=None, sentinel_source="", sentinel_flow="", proxy="", **extra
):
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
        "cookie_presence": deps._cookie_presence(session) if session is not None else {},
        "sentinel_source": str(sentinel_source or ""),
        "sentinel_flow": str(sentinel_flow or ""),
        "http_status": status,
        "proxy": deps.redact_proxy_url(proxy),
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


LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"


def _auth_request_headers(
    base_headers, did="", referer="", origin="", sentinel_token="", sentinel_so_token="", extra=None
):
    return {
        **(base_headers or {}),
        **deps.openai_auth_headers(
            did,
            referer=referer,
            origin=origin,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
            extra=extra or {},
        ),
    }
