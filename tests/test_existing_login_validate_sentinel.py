"""The existing-login lane must not post ``authorize/continue`` at the OTP step.

Same run, same mailbox, same proxy (``runtime/_rerun_409_stdout5.txt`` /
``stdout6.txt``): the signup lane's ``email-otp/validate`` returned **200**
while the existing-login lane's returned **401 login_failed** -- even after
``email-otp/send`` had established a correct login challenge, and even with the
sentinel removed.

Three things differ at the call site, and the third is the one that matters:

* ``registration_handlers.validate_email_otp`` -> ``use_sentinel=False``
* the signup lane dumps the client auth session on failure
* **the signup lane does not POST ``authorize/continue`` in the passwordless
  Web path** -- ``_prepare_signup_auth_state`` returns early when the authorize
  redirect already landed on ``/email-verification``, documented as "the browser
  sends the OTP from this state; do not POST authorize/continue".

After the existing-login lane posts it, the dumped client auth session still
carries signup-era ``passwordless_login_magic_link_sent`` and a *different*
``email_verification_mode`` than the 2026-09-12 successful login, and is missing
``username`` / ``passwordless_disabled`` / ``passwordless_otp_from_password_redirect``.

**2026-09-16 update -- the skip is now conditional, and defaults to ON (post).**

The paragraph above is still why the POST was dropped, and it is still why this
file exists.  What changed is the discovery that the skip was also the *cause*
of a second and larger failure: ``authorize`` lands on ``/email-verification``
for **every** attempt (measured 2026-09-16, PID 32088: 21/21), and the
password-step probe reads the transaction state that ``authorize/continue``
returns -- so with the POST dropped the probe answered
``None``/``no_transaction_state`` on every single attempt, and the signup lane's
``allow_passwordless=False`` turned that ``None`` into a terminal verdict.
Measured: **19/19 existing-account attempts died on that path, zero
exceptions.**  A lane that cannot succeed is not "safe".

abai's ``_submit_login_email`` posts ``authorize/continue`` unconditionally and
its docstring says why: "the ``authorize/continue`` response carries the
transaction-bound ``continue_url`` for the password form; navigating to
``/log-in/password`` without that state returns HTTP 400".  It also declares
``screen_hint: "login"`` -- the one field our body did not carry, and exactly
what separates a *login* continue from the *signup* continue whose signup-era
state the paragraph above blames.

So the POST is back on the verified page, with ``screen_hint: "login"`` added,
behind ``registration.existing_login_continue_on_verified_page`` (default True).
The 401 observation above is **not** retired: it is why this is a toggle, and
why the verified-page POST is wrapped so a refusal or a transport error falls
back to the skip instead of failing the lane.  If a run shows
``email-otp/validate`` going back to ``401 login_failed``, turn the toggle off --
that is what it is for.
"""

from unittest.mock import Mock, patch

from sms_tool import auth_flow

AUTH_BASE = "https://auth.openai.com"
CHAT_BASE = "https://chatgpt.com"
USERNAME = "probe@example.com"
DUMP_LABEL = "existing_login_after_otp_validate_failed"
VERIFICATION_URL = f"{AUTH_BASE}/email-verification"
CONTINUE_URL = f"{AUTH_BASE}/api/accounts/authorize/continue"


class _Response:
    def __init__(self, status_code=200, body=None, url=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.url = url
        self.text = "{}"
        self.headers = {}

    def json(self):
        return self._body


def _drive(*, validate_result, validate_calls, dumps, calls=None, bodies=None,
           authorize_landing=VERIFICATION_URL):
    """Walk the real existing-login lane up to ``email-otp/validate``."""
    calls = calls if calls is not None else []
    bodies = bodies if bodies is not None else []

    def handler(*args, **kwargs):
        method, url = args[1], args[2]
        calls.append((method, url))
        bodies.append((url, kwargs.get("json")))
        if url == f"{CHAT_BASE}/api/auth/csrf":
            return _Response(body={"csrfToken": "session-token"})
        if url == f"{CHAT_BASE}/":
            return _Response(url=f"{CHAT_BASE}/")
        if url.startswith(f"{CHAT_BASE}/api/auth/signin/openai"):
            return _Response(body={"url": f"{AUTH_BASE}/api/accounts/authorize?x=1"})
        if url.startswith(f"{AUTH_BASE}/api/accounts/authorize"):
            return _Response(url=authorize_landing)
        if url == CONTINUE_URL:
            return _Response(body={"continue_url": VERIFICATION_URL})
        if url == VERIFICATION_URL:
            return _Response(url=VERIFICATION_URL)
        if url.endswith("/api/accounts/email-otp/resend"):
            # The bare acknowledgement that used to end the flow early.
            return _Response(body={"success": True})
        if url.endswith("/api/accounts/email-otp/send"):
            return _Response(
                body={
                    "continue_url": VERIFICATION_URL,
                    "page": {
                        "type": "email_otp_verification",
                        "payload": {"email_verification_mode": "login"},
                    },
                }
            )
        raise AssertionError(f"unexpected call: {method} {url}")

    def fake_validate(session, auth_base, base_headers, code, **kwargs):
        validate_calls.append({"code": code, **kwargs})
        return validate_result

    def fake_dump(session, auth_base, base_headers, label):
        dumps.append(label)

    with patch.object(auth_flow, "request_with_retry", side_effect=handler), patch.object(
        auth_flow, "_authorize_continue_sentinel", lambda *a, **k: ({}, "", "")
    ), patch.object(auth_flow, "_poll_email_otp", lambda *a, **k: "123456"), patch.object(
        auth_flow, "_validate_email_otp", side_effect=fake_validate
    ), patch.object(
        auth_flow, "_fetch_client_auth_session_dump", side_effect=fake_dump
    ):
        return auth_flow._login_existing_account_with_email_otp(
            session=Mock(),
            username=USERNAME,
            mailbox=Mock(),
            did="did-1",
            session_logging_id="slog-1",
            auth_base=AUTH_BASE,
            chat_base=CHAT_BASE,
            base_headers={},
            csrf_token="caller-token",
            proxy="",
        )


def test_continue_is_posted_at_the_email_verification_landing():
    """The probe needs the transaction state only this POST can return.

    This **reverses** the 2026-09-14 rule the module docstring describes.  That
    rule was self-defeating: the POST is what carries ``continue_url``, so
    dropping it left the password probe with ``no_transaction_state`` on every
    attempt, and ``allow_passwordless=False`` made that ``None`` terminal.
    Measured 2026-09-16: 19/19 attempts on this path, zero exceptions.
    """
    calls: list[tuple[str, str]] = []
    _drive(validate_result=(True, {}), validate_calls=[], dumps=[], calls=calls)

    posted = [url for method, url in calls if url == CONTINUE_URL]
    assert posted == [CONTINUE_URL]
    # The rest of the lane must still run.
    assert any(url.endswith("/api/accounts/email-otp/send") for _, url in calls)


def test_the_continue_post_declares_screen_hint_login():
    """The one field abai sends that our body did not.

    A body of ``username`` alone is what the *signup* lane posts
    (``_prepare_signup_auth_state``), which is exactly the "signup-era state"
    the module docstring blames for the 401.  Declaring ``screen_hint: "login"``
    is what makes this a login continue.
    """
    bodies: list = []
    _drive(validate_result=(True, {}), validate_calls=[], dumps=[], bodies=bodies)

    body = next(payload for url, payload in bodies if url == CONTINUE_URL)
    assert body["screen_hint"] == "login"
    assert body["username"] == {"value": USERNAME, "kind": "email"}


def test_the_toggle_restores_the_2026_09_14_skip():
    """``registration.existing_login_continue_on_verified_page=false``.

    The escape hatch for the risk the module docstring measures: if
    ``email-otp/validate`` starts answering ``401 login_failed`` again, this is
    how the previous behaviour comes back without editing code.
    """
    calls: list[tuple[str, str]] = []
    with patch.object(auth_flow, "_existing_login_continue_enabled", lambda: False):
        _drive(validate_result=(True, {}), validate_calls=[], dumps=[], calls=calls)

    posted = [url for method, url in calls if url == CONTINUE_URL]
    assert posted == []
    assert any(url.endswith("/api/accounts/email-otp/send") for _, url in calls)


def test_continue_still_runs_for_a_non_verification_landing():
    """The skip must be scoped to the landing, not a blanket removal."""
    calls: list[tuple[str, str]] = []
    _drive(
        validate_result=(True, {}),
        validate_calls=[],
        dumps=[],
        calls=calls,
        authorize_landing=f"{AUTH_BASE}/log-in",
    )

    posted = [url for method, url in calls if url == CONTINUE_URL]
    assert posted == [CONTINUE_URL]


def test_validate_runs_without_a_sentinel():
    """Parity with ``registration_handlers.validate_email_otp``."""
    validate_calls: list[dict] = []
    _drive(validate_result=(True, {}), validate_calls=validate_calls, dumps=[])

    assert len(validate_calls) == 1
    assert validate_calls[0]["use_sentinel"] is False
    assert validate_calls[0]["code"] == "123456"


def test_failed_validate_dumps_the_client_auth_session():
    """A 401 here must not be indistinguishable from a wrong-code rejection."""
    dumps: list[str] = []
    result = _drive(
        validate_result=(False, {"endpoint": "/api/accounts/email-otp/validate", "status": 401}),
        validate_calls=[],
        dumps=dumps,
    )

    assert dumps == [DUMP_LABEL]
    assert result["ok"] is False
    assert result["error"].startswith("existing_login_otp_validate:")


def test_successful_validate_does_not_dump():
    dumps: list[str] = []
    result = _drive(validate_result=(True, {}), validate_calls=[], dumps=dumps)

    assert dumps == []
    assert result == {"ok": True}


# --------------------------------------------------------------------------
# a profile-step landing is a dead end, not a login
# --------------------------------------------------------------------------

def test_a_profile_step_landing_is_reported_as_a_failure():
    """``/about-you`` is a *signup* step, so this transaction never logs in.

    Measured 2026-09-14: 5/5 existing-login runs ended here and none produced a
    session -- the caller's readiness poll then spent four requests to learn
    ``keys=['WARNING_BANNER']``.  ``turb-gpt-free-register`` raises on the same
    signal ("该邮箱登录后进入资料页，疑似不是完整已注册账号").
    """
    result = _drive(
        validate_result=(True, {"continue_url": f"{AUTH_BASE}/about-you"}),
        validate_calls=[],
        dumps=[],
    )

    assert result["ok"] is False
    assert result["error"].startswith("existing_login_landed_on_profile_step:")


def test_a_profile_step_landing_is_not_followed():
    followed: list = []
    with patch.object(auth_flow, "_follow_continue_url", lambda *a, **k: followed.append(a)):
        result = _drive(
            validate_result=(True, {"continue_url": f"{AUTH_BASE}/about-you"}),
            validate_calls=[],
            dumps=[],
        )

    # The verified-page continue POST follows its own ``continue_url`` first,
    # so ``followed`` is no longer empty -- what must never be followed is the
    # *profile* step that ``email-otp/validate`` returned.
    assert not any("about-you" in str(call) for call in followed)
    assert result["ok"] is False


def test_a_page_type_of_about_you_also_stops_the_lane():
    """The URL is not always carried; the page type is the other signal."""
    result = _drive(
        validate_result=(True, {"page": {"type": "about_you"}}),
        validate_calls=[],
        dumps=[],
    )

    assert result["ok"] is False
    assert result["error"].startswith("existing_login_landed_on_profile_step:")


def test_a_real_login_landing_still_completes():
    """The inverse guard: a rule that always fires would break real logins."""
    followed: list = []
    with patch.object(auth_flow, "_follow_continue_url", lambda *a, **k: followed.append(a)):
        result = _drive(
            validate_result=(True, {"continue_url": f"{CHAT_BASE}/api/auth/callback/openai"}),
            validate_calls=[],
            dumps=[],
        )

    assert result == {"ok": True}
    assert any(f"{CHAT_BASE}/api/auth/callback/openai" in str(call) for call in followed)
