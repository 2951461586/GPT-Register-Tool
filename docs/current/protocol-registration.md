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

### Checkout lanes share this vocabulary but do not share the create flow

`FLOW_PAGE_URLS` in `sms_tool/sentinel/client.py` is the **single** flow-to-page
map shared by the registration and payment lanes; `issue_sentinel_flow` resolves
through it at call time. Registered there are both checkout flows, and they are
**not** interchangeable:

- **Checkout create** mints `chatgpt_checkout` — `CHECKOUT_SENTINEL_FLOW` at `sms_tool/sentinel/client.py:44`, issued by `issue_checkout_sentinel` at `sms_tool/sentinel/client.py:499`.
- **Checkout approve** mints `checkout_session_approval` — its own constant `UPI_SENTINEL_APPROVAL_FLOW` at `sms_tool/upi_link/constants.py:42`. Two rails request it explicitly: the UPI approve stage passes `sentinel_flow=UPI_SENTINEL_APPROVAL_FLOW` at `sms_tool/upi_link/stages.py:712`, and `PPLinkExtractor._fresh_approval_sentinel` mints it at `sms_tool/paypal_extract.py:523`.

  🔴 **How to write a guardable pointer here** — two rules, both learned the hard
  way by watching this pair drift by 13 lines when an unrelated `paypal_extract.py`
  edit landed in the same series, with neither gate reporting it:

  1. **The symbol name and the line number must sit on the *same* line.**
     `scripts/docs_consistency_scan.py` is the weak tier for prose (any in-range
     number passes), and `scripts/refresh_doc_symbol_lines.py` is the fixer -- but
     `_rewrite` runs `_prose_edits` **per line**, so a pointer whose symbol is on the
     previous line is unattributable and silently unguarded. That is why the
     bullets above are single long lines instead of wrapped.
  2. **A class method must be spelled dotted** (`Class.method`).
     `_symbol_lines` records module-level names plus direct methods keyed as
     `Class.method`, so an undotted `_fresh_approval_sentinel` maps to nothing.

`CHECKOUT_SENTINEL_FLOW` is the **create** gate only. Do not collapse the approve
flow onto it: the create token and the approve token are minted under different
flows and the server reads them differently.

**Token and SO need not come from the same flow.** The approve gate reads a main
token minted under `checkout_session_approval`, but the SO it accepts carries an
internal `flow: chatgpt_checkout` — the checkout-phase SO is *reused*, not
re-minted. `sms_tool/upi_link/sentinel.py:443` records this as HAR #228 and
re-consults the checkout mint for the SO even when the approval mint returned
one. A flat one-flow-per-endpoint table is therefore *less* accurate than this
code, not more.

### The subprocess extractors now receive the Sentinel pair

Until 2026-10-06 the seven extractors under `services/protocol-payment/` sent
**no** Sentinel pair anywhere. `sms_tool/pay_link/adapters.py` passed no
`SENTINEL*` value into their environment, none of them set the header by hand,
and their approve was authenticated only by the account's own cookie jar
(`oai-did` + `__Secure-next-auth.session-token`) plus the
`x-openai-target-path` / `x-openai-target-route` markers and a per-endpoint
`Referer`. Four of them (blik / ideal / twint / pix) also POST
`backend-api/sentinel/ping` before approve — with **no token either**; that is a
connectivity warm-up, and the in-process counterpart `_upi_sentinel_ping` sends
the identical token-less header set.

That silence was a **real gap, not a different gate**. A controlled A/B on the
create endpoint — same account, same country-verified exit, same body, varying
only the headers — settled it:

| Headers sent | Result |
| --- | --- |
| none | `400` "Our systems have detected unusual activity" |
| `openai-sentinel-token` alone | `200` `custom_checkout_session` |

The pair now travels by **environment**, because Rule 10 forbids `services/` from
importing `sms_tool`: `services/protocol-payment/common/protocol_core.py` owns the
contract (`OPENAI_SENTINEL_TOKEN` / `_SO_TOKEN` / `_DEVICE_ID`,
`sentinel_device_id()`, `openai_sentinel_headers()`) and
`sms_tool/pay_link/adapters.py` (`_sentinel_env`) mints and injects it. The
adapter chooses the device id **first** and hands it down, because a token binds
to the `oai-did` that requested the challenge.

🔴 **How the earlier review was boxed in (kept for the method, not the
verdict).** An earlier revision of this section claimed "that is a different
gate, not a missing one". A 2026-10-05 re-review could not support *or* refute
that from the retained data, and the reason is worth keeping:

| Source | Result |
| --- | --- |
| Source code | Conclusive for the **fact**: 6 of 7 call approve with no Sentinel (`direct_card` never calls approve). UPI and PayPal do mint `checkout_session_approval` (`sms_tool/upi_link/stages.py`, `sms_tool/paypal_extract.py:537`) |
| 391 retained `runtime/logs/processes/*/backend_stdout.jsonl` | The extractor approve **failure path never fired**: `approve 未通过`, `approve_http_`, `approve 失败`, `approve_result_blocked` = **0 occurrences**. Says nothing about whether a token would have been needed, because success is not logged |
| Same logs, in-process side | The mint is exercised and healthy: **65** `checkout_session_approval Sentinel ready (bridge …)` lines and **0** `Sentinel unavailable` — but all 125 `Sentinel ready` lines are UPI's bridge form, so this is **UPI-only** evidence; PayPal's `_fresh_approval_sentinel` success line appears **0** times, so that rail did not reach approve in the window |
| Extractor `dumps/` (`ideal/dumps`, `twint/dumps`) | **Empty** — the `approve` dump is written with `force=True`, so it existed, but it was purged |
| `protocol_payment.v1` terminal lines in logs | Not retained: `_finish_extractor` keeps stdout for parsing and only surfaces `_tail(output)` on the **failure** path, by design |

**Why the logs could not answer it**: extractor success was silent. A run whose
approve quietly succeeded was byte-identical in the logs to one that never
reached approve, so neither hypothesis was supported — and the compensating
mechanism was suspicious in a way the A/B later vindicated: the extractors answer
`blocked` by **rotating the proxy up to 10 times** (`IDEAL_APPROVE_RETRY_MAX`,
default 10, with a comment about `approve 返回 blocked`). Rotating an exit is the
wrong lever when the real cause is a missing gate token, exactly as the
`blocked_count` retries had previously been measured as wasted time.

⇒ **Observability, not a header — landed 2026-10-05.** `_log_extractor_terminal` in
`sms_tool/pay_link/adapters.py` emits exactly one INFO line per subprocess-extractor
run recording `payment_method` / `ok` / `error_code` plus `contract=` and
`exit_code=`, and **never** the contract's `error` text, `url` or `artifacts`.
It is called from the single funnel every extractor passes through
(`_finish_extractor`), including the timeout branch, so a new extractor cannot
skip it.

The line distinguishes four states that used to look identical: `ok=ok`,
`ok=failed`, `ok=timeout`, and `ok=no_terminal_contract` (with `contract=absent`
covering both a non-contract dict — e.g. direct_card's cancellation print — and
a genuinely silent extractor). A mismatch between the contract's own
`payment_method` and the method that was dispatched stays visible rather than
being masked.

⇒ **What to do next — and why five extractors are deliberately left unwired.**
Two extractors are wired: **pix** and **direct_card**. The other five
(blik / ideal / twint / kakao / momo) are not, because injecting the pair there
cannot help:

🔴 **Six of the seven cannot work on this endpoint any more.** The create call no
longer issues `cs_*` sessions at all. Measured 2026-10-06 across every
`checkout_ui_mode` the server accepts:

| `checkout_ui_mode` | Result |
| --- | --- |
| `hosted` / `custom` / omitted | `200`, always `tag=custom_checkout_session`, always an `oaics_*` id |
| `deferred` / `stripe_hosted` / `elements` | `422` |

and Stripe answers an `oaics_*` id with `404 resource_missing: No such
payment_page`. blik / ideal / twint / pix / kakao / momo drive Stripe
`payment_pages` + `payment_methods` throughout, so **no prefix or header change
can revive them**: they need the custom Checkout flow
(`custom_payment_method/start` → `payment_intents/{pi}/confirm` →
`checkout/confirm` → `checkout/approve`), or retirement in favour of the
in-process machinery that already implements it (UPI, direct_card, gopay,
gcash). That fork is open and deliberately undecided here.

`direct_card` is the exception because it accepts `oaics_*` and never touches
`payment_pages`. With a country-correct exit and the injected pair it produces a
real link end to end:

```text
extractor terminal: payment_method=direct_card ok=ok error_code=- exit_code=0
url: https://chatgpt.com/checkout/openai_llc/oaics_<id>
```

See [account-health.md](account-health.md) for the session-family split this
rests on, and for the create-gate token question.

**Closeout (2026-10-05).** A cross-project review of `pxygit/SunnyRegister`
(its `payment_proof_contracts.py` `ENDPOINT_FLOW` table) proposed mapping
`checkout/confirm` and `checkout/approve` to a distinct
`checkout_session_approval` flow. This repository already does exactly that, and
the UPI rail additionally models the token/SO split above. **No change
required.** Recorded here so the proposal is not re-litigated, and so nobody
"simplifies" `CHECKOUT_SENTINEL_FLOW` into the approve flow.

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
