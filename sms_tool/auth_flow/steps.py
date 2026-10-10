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
from typing import Mapping
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


def _is_signup_password_page(url):
    """Return True only for the concrete signup password page."""
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    return host.endswith("auth.openai.com") and path.endswith("/create-account/password")


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


# ─── registration toggles ─────────────────────────────────────────────────────
#
# The eight ``registration.*`` switches below are A/B levers.  Each keeps its own
# long docstring -- that is where the measurement that earned it is recorded --
# and shares only the four lines that read a boolean out of the frozen config.
#
# The parser is deliberately two-sided.  ``_existing_login_continue_enabled``
# predates this helper and is default-**on**, so an unrecognised value has to
# stay truthy for it; every later toggle is default-**off** and treats an
# unrecognised value as falsy.  Returning ``default`` for an unknown value
# reproduces both exactly, and keeps "an unreadable config fails to the toggle's
# own default" in one place instead of eight.
_FALSY_FLAG_VALUES = (False, 0, "0", "false", "False", "no", "No", "off")
_TRUTHY_FLAG_VALUES = (True, 1, "1", "true", "True", "yes", "Yes", "on")


def _registration_flag(key: str, default: bool) -> bool:
    """Read one ``registration.<key>`` boolean toggle from the frozen config.

    ``default`` answers both a missing key and an unrecognised value, which is
    what the eight hand-written bodies did before they were converged here.

    The ``Mapping`` guard is load bearing, and it is the reason this helper
    exists rather than eight copies of itself: the sharded production config
    freezes every section into a ``mappingproxy`` (``_freeze`` in
    ``sms_tool/config.py``), which is **not** a ``dict``.  A ``dict`` check made
    these toggles read their default in every production run while the tests,
    which pass plain dicts, stayed green (2026-10-07; see
    ``docs/audits/scan-2026-10-07-protocol-registration.md`` P1-D).  One copy of
    the guard cannot drift from itself.
    """
    try:
        cfg = deps.current_config_data().get("registration", {})
    except Exception:
        return default
    if not isinstance(cfg, Mapping):
        return default
    value = cfg.get(key, default)
    if value in _FALSY_FLAG_VALUES:
        return False
    if value in _TRUTHY_FLAG_VALUES:
        return True
    return default


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
    return _registration_flag("existing_login_continue_on_verified_page", True)


def _signup_continue_screen_hint_enabled():
    """Whether the signup lane's ``authorize/continue`` body declares ``screen_hint: "signup"``.

    ``registration.signup_continue_screen_hint`` (default **False**).

    **Why this exists.**  ``_continue_signup_username`` posts a body of only
    ``{"username": {...}}`` -- which this repo's own docstring (see
    ``_existing_login_continue_enabled``) reads as "what a *signup* continue
    sends".  But the two reference protocol clients that can complete a
    password registration both declare the screen explicitly:

    * SunnyRegister ``_authorize_email`` sends ``"screen_hint": "login" if
      existing_account else "signup"`` -- always one of the two, never absent.
    * The login lane here (``_existing_login_continue``) gained
      ``screen_hint: "login"`` behind ``existing_login_continue_on_verified_page``
      after the 09-14/09-16 measurements pinned the missing field as the
      difference between a signup-shaped session and a login-shaped one.

    The signup lane's own symptom ("发码 200 且落点正确，但派发未发生",
    2026-10-06, ~11 attempts / 3 configurations, byte-identical) is recorded on
    ``_prime_create_account_password_page_enabled``.  Declaring ``screen_hint``
    is the *other* wire difference between this client and the working
    reference, so it earns its own toggle instead of being bundled with the
    password-page prime -- one variable per comparison.

    Off by default: the signup continue currently *works* on some exits
    (addresses land on ``/email-verification`` and codes dispatch), and this
    adds a field to that working request.  It earns a default only after a
    controlled A/B (``p1-5-signup-continue-screen-hint``) shows the failure
    shape moving without the success rate dropping.

    ⚠ **Reachability.**  This toggle only fires when the continue POST itself
    fires.  When ``authorize`` lands on ``/email-verification`` (the measured
    2026-10-06/07 failure shape) ``_prepare_signup_auth_state`` returns early,
    so the flag is never read -- an arm with it on is indistinguishable from
    the default.  ``scripts/registration_ab.py`` flags that arm
    ``manipulation_failed`` instead of comparing rates.  To test the declared
    screen *on that landing*, use
    ``_signup_email_verification_continue_hint_enabled``.
    """
    return _registration_flag("signup_continue_screen_hint", False)


def _signup_email_verification_continue_hint_enabled():
    """Whether the signup lane re-posts ``authorize/continue`` from ``/email-verification``.

    ``registration.signup_email_verification_continue_hint`` (default **False**).

    **Why this exists -- and why the sibling toggle could not answer it.**
    ``signup_continue_screen_hint`` (above) declares the screen on the continue
    the lane *already* posts.  Its only read point is ``_continue_signup_username``
    (``signup.py``), and that function is reached only when ``authorize`` lands
    *off* the password and email-verification steps.  On the measured failure
    shape the authorize redirect lands exactly on ``/email-verification``, so
    ``_prepare_signup_auth_state`` returns early and the sibling toggle never
    executes -- a manipulation that never ran, not a hypothesis that failed
    (``run_p15_hint.log``, 2026-10-07: 0/5, zero ``Signup username continue``
    lines; see ``docs/audits/scan-2026-10-07-protocol-registration.md`` P0-A).
    ``scripts/registration_ab.py`` now refuses to judge an arm whose mechanism
    line is absent, so that gap can no longer masquerade as a null result.

    **What this toggle does instead.**  When ``authorize`` lands on
    ``/email-verification`` on the **password** lane, it issues one extra
    ``authorize/continue`` carrying ``screen_hint: "signup"`` (the body shape
    ``signup_continue_screen_hint`` describes), in the hope that declaring the
    screen after the fact moves the server's transaction arm from
    ``passwordless_login``/``passwordless_signup`` back to the signup arm.  The
    measured signal to watch is ``client_auth_session.email_verification_mode``,
    not the landing URL: the 2026-10-06/07 runs already landed correctly while
    the arm stayed passwordless.

    Password lane only: the passwordless Web lane deliberately sends no
    ``authorize/continue`` from that landing (the browser sends the OTP from
    that state), so enabling this there would change a different flow.  Off by
    default -- an extra state-changing POST to a working lane earns a default
    only after its own controlled A/B
    (``p1-8-email-verification-continue-hint``).
    """
    return _registration_flag("signup_email_verification_continue_hint", False)


def _prime_create_account_password_page_enabled():
    """Whether the password lane GETs ``/create-account/password`` before ``user/register``.

    ``registration.prime_create_account_password`` (default **False**).

    **Why this exists.**  ``_post_user_register`` already sends
    ``Referer: {auth_base}/create-account/password`` and mints a
    ``username_password_create`` Sentinel -- i.e. it *claims* to be on the
    password page -- but nothing ever navigates there.  Measured 2026-10-06 on a
    fresh ReMail ``icloud.com`` address: the signup lane lands on
    ``/email-verification`` and POSTs ``user/register`` from that state.  The
    POST answers **200** with ``{"continue_url": ".../email-otp/send",
    "method": "GET", "page": {"type": "email_otp_send"}}``, the GET that
    follows answers **200** and lands on ``/email-verification`` -- and the
    server still never dispatches the code.

    The dispatch never happening is not an inference: a direct
    ``GET /v1/pickup`` on all five mailboxes returned **zero** messages, and
    ``client_auth_session_dump[after_otp_send]`` kept its ``_pending`` marker.
    The outcome was byte-identical on the passwordless lane, on the password
    lane, and on an IN *and* a US exit family (~11 attempts, 3 configurations),
    which excludes lane, ordering, send method, ``continue_url`` handling, exit
    and mailbox health.

    turb's protocol client takes the other route and says why: its
    ``navigate_create_account_password`` -- "进入注册密码页，确保协议注册不再走无密码
    OTP 分支" -- runs *unconditionally* between ``follow_authorize`` and
    ``register_user``, and ``main.py`` labels that block "强制走邮箱+密码注册，
    不使用 passwordless OTP-only 分支".  A session whose auth-step state is
    ``/email-verification`` may arm the OTP transaction on the passwordless arm
    even though the POST carried a password.

    Off by default: this adds a request to a lane that currently fails one step
    later, so it earns a default only after a controlled comparison shows the
    dispatch happening.

    **Live result (2026-10-06 22:39, directed rerun, single arm).**  With the
    toggle ON, the prime GET executed on **5/5** attempts and answered 200 with
    the correct ``/create-account/password`` landing every time -- the page
    state was established exactly as designed.  The server *still* armed the
    transaction on the passwordless branch: every ``after_otp_send`` dump kept
    ``passwordless_email_otp_send_pending``, and all 5 runs ended
    ``email_otp_send_stuck`` -- byte-identical to the same mailboxes' afternoon
    runs with the toggle OFF (9 stuck / 11 attempts, prime never executed).
    Verdict: the password-page state is **not** the discriminator the server
    uses to pick the transaction branch.  turb's unconditional navigation works
    for turb for some other reason (its signin shape differs: it sends
    ``login_hint + screen_hint=login_or_signup`` and no ``authorize/continue``
    POST).  The next wire candidate is P1-5 (declared screen hint), then the
    signin/continue shape itself.  The toggle stays off; do not re-test this
    variable in isolation again.
    """
    return _registration_flag("prime_create_account_password", False)


def _prime_navigation_headers_enabled():
    """Whether the password-page prime GET carries real navigation headers.

    ``registration.prime_navigation_headers`` (default **False**).

    **Why this exists (2026-10-07 scan P1-A).**  ``_prime_create_account_password_page``
    follows the password URL through ``http_utils._follow_continue_url``, which
    sends only ``Accept`` + ``Referer``.  A real top-level navigation sends
    ``Sec-Fetch-Site: same-origin`` and ``Sec-Fetch-User: ?1``; turb's
    ``navigate_create_account_password`` sends both and treats a wrong landing
    as fatal.  So P1-4's conclusion ("the prime ran 5/5 with the correct
    landing, yet the transaction stayed passwordless") rests on "landing", not
    on "a navigation the server recognised as top-level" -- the header gap is
    an unexcluded explanation, not a settled one.

    This is a **separate** toggle from ``prime_create_account_password`` on
    purpose: turning it on changes what the *prime* request looks like, so the
    P1-4 arm stays reproducible and the comparison stays single-variable
    (``prime`` on in both arms, headers the only delta).  Off by default; it
    earns a default only after a controlled A/B
    (``p1-9-prime-navigation-headers``).
    """
    return _registration_flag("prime_navigation_headers", False)


def _signin_screen_hint_login_or_signup_enabled():
    """Whether the password lane's signin declares ``screen_hint=login_or_signup``.

    ``registration.signin_screen_hint_login_or_signup`` (default **False**).

    **Why this exists (2026-10-07 scan §5 step 6).**  The password lane's first
    signin attempt declares ``screen_hint=signup`` (``_signup_signin_attempts``),
    and on the measured failure shape ``authorize`` lands on
    ``/email-verification`` with the transaction armed on
    ``email_verification_mode=passwordless_signup`` /
    ``passwordless_signup_from_default_redirect=true``.  Two live runs on
    2026-10-07/08 (hint arm on the lajiao pool, default arm on the fireside
    pool, different mailboxes) produced the **same** arm and the same
    ``email_otp_send_stuck``, so exit, mailbox and the declared continue screen
    are all excluded -- what is left is the signin shape itself.

    turb's ``signin_openai`` sends ``screen_hint=login_or_signup`` (with
    ``prompt=login`` and ``login_hint``) and never posts
    ``authorize/continue``.  This toggle changes **only** the ``screen_hint``
    value of the first signin attempt, so the A/B is single-variable: the lane,
    the exit pool and the ``authorize/continue`` behaviour all stay constant.
    The later fallback attempts keep ``signup`` -- they are only reached when
    the first attempt does not land on a recognised step.

    Off by default: it changes the request that currently produces a correct
    landing, so it earns a default only after a controlled comparison
    (``p1-10-signin-screen-hint``) moves ``email_verification_mode`` off
    ``passwordless_*`` without dropping the success rate.
    """
    return _registration_flag("signin_screen_hint_login_or_signup", False)


def _signin_prompt_login_enabled():
    """Whether the password lane's signin declares ``prompt=login``.

    ``registration.signin_prompt_login`` (default **False**).

    **Why this exists.**  turb's ``signin_openai`` sends ``prompt=login`` *and*
    ``screen_hint=login_or_signup``.  Four 2026-10-07/08 live runs (lajiao /
    fireside pools, burned / brand-new mailboxes, ``signup`` /
    ``login_or_signup`` / a re-declared continue) all landed on
    ``email_verification_mode=passwordless_signup`` with
    ``passwordless_signup_from_default_redirect=true`` and a non-dispatching
    OTP, so the declared *screen* is recorded by the server
    (``original_screen_hint`` follows it) but does not pick the transaction arm.
    ``prompt`` is the next untested field of turb's signin shape.

    Single variable: it changes **only** the first attempt's ``prompt`` (``""``
    -> ``"login"``).  The ``p1-11`` comparison holds
    ``signin_screen_hint_login_or_signup=true`` in **both** arms, so the pair is
    ``login_or_signup`` vs ``login_or_signup + prompt=login`` -- exactly turb's
    shape as the treatment.

    Off by default; it earns a default only after a controlled comparison
    (``p1-11-signin-prompt-login``) moves ``email_verification_mode`` off
    ``passwordless_*`` without dropping the success rate.
    """
    return _registration_flag("signin_prompt_login", False)


def _signin_locale_ja_jp_enabled():
    """Whether the password lane's signin declares ``locale=ja-JP``.

    ``registration.signin_locale_ja_jp`` (default **False**).

    **Why this exists.**  SunnyRegister's ``_start_next_auth`` puts
    ``locale: "ja-JP"`` in the ``/api/auth/signin/openai`` query (alongside
    ``prompt=login`` and ``screen_hint``); this repo's ``_openai_signin_url``
    declares no locale at all.  Four 2026-10-07/08 live runs showed the declared
    screen (and ``prompt=login``) reach the server -- ``original_screen_hint``
    follows the declaration, and ``prompt=login`` even moved the arm to
    ``passwordless_login`` -- but every arm still failed (``passwordless_signup``
    + non-dispatching OTP, or ``passwordless_login`` + ``invalid_auth_step``).
    ``locale`` is the last field of SunnyRegister's signin shape.

    Single variable: it adds only the ``locale`` query parameter, on the
    password lane, uniformly across the attempts.  It is declared as a **boolean**
    (not a locale string) so the A/B harness's mechanism gate can tell which arm
    the marker belongs to -- a string arm value would set ``mechanism_ok=null``.

    Off by default; it earns a default only after a controlled comparison
    (``p1-12-signin-locale``).
    """
    return _registration_flag("signin_locale_ja_jp", False)


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


def _ensure_authorize_context(url, did, session_logging_id, login_hint, *, screen_hint="", prompt="", locale=""):
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
    if locale:
        required["locale"] = locale
    for key, value in required.items():
        if value and not values.get(key):
            values[key] = [str(value)]
    return parsed._replace(query=urlencode(values, doseq=True)).geturl()


def _openai_signin_url(chat_base, did, session_logging_id, login_hint, *, screen_hint="", prompt="", locale=""):
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
    if locale:
        params["locale"] = locale
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
    """The password lane's signin attempts, in order.

    The first attempt carries both variables the signin-shape toggles own:
    ``screen_hint`` (``signup`` or turb's ``login_or_signup``, from
    ``_signin_screen_hint_login_or_signup_enabled``) and ``prompt`` (``""`` or
    ``login``, from ``_signin_prompt_login_enabled``).  The fallbacks keep
    ``signup`` -- they exist for exits where the first shape does not reach a
    recognised step, and changing them would add variables to the comparisons.
    """
    login_or_signup = _signin_screen_hint_login_or_signup_enabled()
    prompt_login = _signin_prompt_login_enabled()
    locale = "ja-JP" if _signin_locale_ja_jp_enabled() else ""
    screen_label = "login_or_signup" if login_or_signup else "signup"
    first = {
        "name": f"{screen_label}_prompt_login" if prompt_login else f"{screen_label}_screen_hint",
        "screen_hint": screen_label,
        "prompt": "login" if prompt_login else "",
        "locale": locale,
    }
    return (
        first,
        {"name": "signup_prompt_signup", "screen_hint": "signup", "prompt": "signup", "locale": locale},
        {"name": "signup_legacy_prompt_login", "screen_hint": "signup", "prompt": "login", "locale": locale},
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
    raw_error = data.get("error")
    error = raw_error if isinstance(raw_error, dict) else {}
    code = str(error.get("code") or "").strip().lower()
    message = str(error.get("message") or "").strip().lower()
    # The rule matches the " is " inside the quoted sentence below, not an
    # identity operator.
    # pi-lens-ignore: no-identity-operator-on-literals
    return code == "invalid_state" or "session is no longer valid" in message


LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"

#: The Sentinel flow the existing-login lane mints its tokens under.  The SO is
#: flow-bound (``sentinel/client.issue_sentinel_token`` writes ``flow`` into it),
#: so a lane may only replay an SO that was minted for its own flow.
AUTHORIZE_CONTINUE_FLOW = "authorize_continue"


def so_token_flow(token):
    """The flow an SO token declares, or ``""`` when it declares none.

    Both producers write a JSON object carrying ``flow``: the Node runner
    (``sentinel/client.issue_sentinel_token``) and the legacy issuer
    (``sentinel_tokens._extract_sentinel``).  A token that does not parse, is not
    an object, or carries no ``flow`` is reported as *unknown* -- never guessed,
    because the only safe action on an unknown flow is to leave the token alone
    (see :func:`same_flow_so_token`).
    """
    try:
        payload = json.loads(str(token or ""))
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("flow") or "").strip()


def same_flow_so_token(token, expected_flow):
    """Drop *token* when it declares a flow other than *expected_flow*.

    🔴 The cross-flow fallback this closes (2026-10-08 scan F2): ``create_account``
    mints the ``oauth_create_account`` pair and the workflow writes its SO into
    ``runtime.sentinel_so_token``; the existing-login lane then falls back to
    that value on ``email-otp/send`` whenever its own ``authorize_continue`` mint
    returns no SO.  An SO is bound to the flow that minted it, so replaying the
    create-account SO on the OTP endpoint is a cross-flow reuse rather than a
    fallback -- the request would carry a token and an SO from two different
    flows.

    A token that declares **no** flow is kept: the guard exists to stop a
    *declared* mismatch, and dropping an undeclared legacy token would change
    behaviour on evidence we do not have (the same "no evidence, do not act"
    rule ``otp_dispatch_verdict`` follows).
    """
    declared = so_token_flow(token)
    if declared and declared != str(expected_flow or "").strip():
        return ""
    return token


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
