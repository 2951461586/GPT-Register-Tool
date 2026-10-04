# Protocol Registration Contract

The current contract for the **email / AT protocol lane** — the non-browser
flow that registers or logs into a ChatGPT account and ends with an access
token. The browser lane is documented in
[registration architecture](registration-architecture.md).

This document records the wire contract only. It does not claim a live success
rate; see [Validation limits](#validation-limits).

## Owners

| Concern | Module |
| --- | --- |
| Protocol step functions (signin, authorize, continue, OTP, TOTP) | `sms_tool/auth_flow/` (`steps`, `signup`, `login`, `otp`, `totp`) |
| Stage order, runtime state, persistence, create account | `sms_tool/registration_handlers.py` |
| Persistence seam, stage executor, pure helpers, email-OTP stages | `sms_tool/registration_persistence.py`, `registration_stage_runner.py`, `registration_protocol_helpers.py`, `registration_otp_stages.py` |
| Account creation and OTP validate wire calls | `sms_tool/accounts/account_creation.py` |
| Sentinel token issuance | `sms_tool/sentinel/` (facade `sms_tool/sentinel_tokens.py`) |
| AT probe, result contract, funnel | `sms_tool/registration_outcome.py`, `registration_result.py`, `registration_funnel.py` |
| `sms_tool/registration.py` | compatibility facade only — re-exports, no implementation |

`registration_handlers.py` owns stage order; the `auth_flow/` package owns only
the step functions. A change to endpoint order belongs in the handler, not in a
step.

## Two lanes

The **server** chooses the lane, not the client. Both lanes begin with the same
signin → authorize handshake and diverge on the landing page.

- **Signup lane** — `_prepare_signup_auth_state`. The mailbox is new to the
  server. It walks `_signup_signin_attempts()` (or
  `_passwordless_signin_attempts()` on the passwordless Web path), then
  `_continue_signup_username`, and settles on `/email-verification` or
  `/create-account/password`.
- **Existing-login lane** — `_login_existing_account_with_email_otp`, four
  phases: `_existing_login_signin` → `_existing_login_continue` →
  `_existing_login_probe` → `_existing_login_otp`. It is entered when the
  signup lane lands on `/log-in/password` (`_is_existing_login_redirect`) or
  the server answers `user_already_exists`.

## Endpoints

`{auth_base}` is `auth.openai.com`; `{chat_base}` is `chatgpt.com`. All paths
are relative to those hosts.

| Step | Method | Path | Owner |
| --- | --- | --- | --- |
| Prime (signup) | GET | `{auth_base}/create-account` | `registration_handlers.auth_flow` |
| Prime (passwordless) | GET | `{chat_base}/` | `registration_handlers.auth_flow` |
| CSRF | GET | `{chat_base}/api/auth/csrf` | `registration_handlers.auth_flow` |
| Signin | POST | `{chat_base}/api/auth/signin/openai` | `auth_flow` (`_prepare_signup_auth_state`, `_existing_login_signin`) |
| Authorize | GET | `{auth_base}/api/accounts/authorize` | `auth_flow` |
| Authorize continue | POST | `{auth_base}/api/accounts/authorize/continue` | `auth_flow` (`_continue_signup_username`, `_existing_login_continue`) |
| User register | POST | `{auth_base}/api/accounts/user/register` | `registration_handlers.user_register` |
| OTP send | **GET** | `{auth_base}/api/accounts/email-otp/send` | `auth_flow` (`_send_existing_login_otp`) |
| OTP resend | POST | `{auth_base}/api/accounts/email-otp/resend` | `auth_flow` (fallback only) |
| OTP validate | POST | `{auth_base}/api/accounts/email-otp/validate` | `accounts/account_creation._validate_email_otp` |
| Create account | POST | `{auth_base}/api/accounts/create_account` | `registration_handlers.create_account` |
| Callback | GET | `{chat_base}/api/auth/callback/openai` | server redirect (terminal success) |

The `send` endpoint's **method is per endpoint and must stay `GET`**. A POST
arms a `login_challenge` that every later `email-otp/validate` rejects with
`409 invalid_state`; the GET arms `passwordless_login`, which validates. Do not
"simplify" the pair back to one method.

`resend` is a fallback for endpoint-level rejections (`400`/`404`/`405`) only.
It has never carried a challenge payload, and calling it after a bare `2xx`
`send` acknowledgement dispatches a second code for the same transaction — stop
on the acknowledgement instead.

## Existing-login lane phases

1. `_existing_login_signin` mints a CSRF token on the session that will post
   (`_fetch_session_csrf_token`), posts signin, follows authorize, and returns
   `(terminal, state)`. A `chatgpt.com/auth/login` landing is terminal with
   `existing_login_signin_not_established:invalid_state` — never fall through
   to `authorize/continue` from there.
2. `_existing_login_continue` mints a fresh `authorize_continue` Sentinel, posts
   the continue body, follows `continue_url`, and carries the payload forward.
   On `/email-verification` the POST is gated by
   `registration.existing_login_continue_on_verified_page`; when skipped, the
   Sentinel is still minted so later OTP steps use a fresher token.
3. `_existing_login_probe` asks `/log-in/password` whether the account has a
   password, **before** spending an email code (`_probe_login_password_step`).
4. `_existing_login_otp` sends the code, polls the mailbox, validates, and may
   complete TOTP (`_complete_existing_login_totp`).

Each phase returns `(terminal, state)`: a final result dict when it settles the
login, `None` to continue; `state` carries `current_url`, the continue payload,
and fresh Sentinel tokens.

## Sentinel contract

`_authorize_continue_sentinel` issues a **same-flow** token through
`issue_sentinel_flow(flow="authorize_continue", ...)`. The result carries:

- `sentinel_authorize_continue_token`
- `sentinel_authorize_continue_so_token`
- `sentinel_source` — `node_sdk_runner` on the current path
- `oai_did`

`_auth_request_headers` attaches the token pair to the continue, OTP and TOTP
requests. The signup lane uses `oauth_create_account` for account creation
(`registration_handlers.create_account`). Token issuance is validated against
the pinned runner bundle before a batch starts; see
[registration architecture](registration-architecture.md).

## Landing-page vocabulary

Each predicate has exactly one owner in `auth_flow/steps.py`; do not re-derive a
path test inline.

| Predicate | Meaning |
| --- | --- |
| `_is_existing_login_redirect` | `/log-in` or `/login` — the server treats the address as registered |
| `_is_chatgpt_auth_login_landing` | `chatgpt.com/auth/login` — NextAuth did not establish a session; fatal for the login lane |
| `_is_signup_password_step` | `/create-account/password` |
| `_is_email_verification_step` | `/email-verification` |
| `_is_about_you_step` | `/about-you` — the signup profile step |
| `_is_login_password_step` | `/log-in/password` (via `_login_password_page_type`) |
| `_invalid_state_auth_response` | `invalid_state` / "session is no longer valid" — retryable `auth_state` class |

## Validation limits

Offline tests cover step ordering, redirect classification and Sentinel header
plumbing. They do not establish a live registration or login success rate.
Changing the lane choice, the `send` method, the continue-on-verified-page
toggle, or Sentinel flow selection requires a controlled live comparison, not
an offline transaction test. The pre-registered A/B design, collection and
comparison for the three outstanding comparisons (preflight login endpoint,
Cloudflare-challenge observation, Sentinel password bundle) is
[`registration-ab-runbook.md`](registration-ab-runbook.md), driven by
`scripts/registration_ab.py`.

## Read-only registration probe

`sms_tool/registration_probe.py` answers "is this mailbox new to OpenAI?" with
the same `signin -> authorize` handshake the signup lane uses, and stops at the
authorize landing. It never posts `authorize/continue`, registers a password,
sends an OTP or creates an account. `classify_registration_landing` maps an
auth `/create-account[/password]` landing to `unregistered`, `/log-in*` and the
`login_password` / `mfa_challenge` page types to `registered`, and an
`/email-verification` landing without a verification mode to `unknown` — the
follow-up `authorize/continue` needed to disambiguate may dispatch an OTP, so it
is deliberately not taken. A transport failure is `unknown` with an `error`
field; "not probed" is never reported as "registered".
