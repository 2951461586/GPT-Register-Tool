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
| Persistence seam, stage executor, pure helpers, email-OTP stages, edge-challenge hook, resume stage, Sentinel stages | `sms_tool/registration_persistence.py`, `registration_stage_runner.py`, `registration_protocol_helpers.py`, `registration_otp_stages.py`, `registration_edge_challenge.py`, `registration_resume.py`, `registration_sentinel_stages.py` |
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

- **Checkout create** mints `chatgpt_checkout` — `CHECKOUT_SENTINEL_FLOW` at `sms_tool/sentinel/client.py:44`, issued by `issue_checkout_sentinel` at `sms_tool/sentinel/client.py:510`.
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

## In-flow edge-challenge judgement (P0-B, S0 + S1)

Until 2026-10-07 the protocol lane had **no** exit-level challenge judgement after
preflight: every in-flow `403`/`429` collapsed into `session_circuit_open`
(`sms_tool/http_client.py`), with no way to tell "this exit was challenged" from
"the request was refused". The gap is expensive because it opens *after* a
mailbox and its OTP were already spent, so a missed challenge loses a mailbox
slot permanently while a false positive costs one extra request.

`sms_tool/proxy_edge_probe.edge_challenge_verdict(response)` is that judgement.
It is **pure and total** and answers one of three values:

| Value | Meaning |
| --- | --- |
| `challenge` | an exit-level Cloudflare challenge — the reply classifies `BLOCKED` in the probe's own vocabulary, i.e. reachable but useless for registration |
| `not_challenge` | the `403`/`429` is not about the exit: an already-deactivated account (excluded first, per `accounts/account_terminal.py`), or a plain `429` the origin answered |
| `unknown` | any status other than `403`/`429` — never guess, because a successful OTP reply legitimately contains the word "challenge" (`auth_flow/otp.py`'s `login_challenge` transaction arm) |

Three rules are load-bearing: only `403`/`429` get a yes/no answer; the
judgement **reuses** `classify_edge_response` instead of a second set of
host/header matchers; and a deactivated account is excluded before the challenge
read, because rotating the egress cannot change that answer.

**What it does today — nothing but a name.**
`registration.edge_challenge_discrimination` (default **true**) appends a
trailing `:edge_challenge` to `SessionCircuitOpen`'s message:

```text
session_circuit_open:http_403:retry_after=900s:edge_challenge
```

The leading `session_circuit_open` token never moves, so `failure_registry`
classifies it exactly as before. Rotation and browser handoff are separate
switches that stay **off**; the judgement is observation only, which is why the
suffix can ship on by default (the A/B in
`plan-2026-10-05-inflow-challenge-handoff.md` §4 has no other data source for
"did a challenge happen at all").

🔴 **Where it lives, and why not in `auth_state.py`.**
`plan-2026-10-05` suggested `auth_state.py`; that is a cycle, because
`auth_state` already imports `http_client`, and `http_client` is the owner of the
`session_circuit_open` name the suffix rides on. The judgement therefore sits
next to `classify_edge_response`, and `auth_state` will re-export it when the
first consumer needs it (S2) rather than carrying an unused import.

### The three invariants (§3.5)

| # | Invariant | Pinned by |
| --- | --- | --- |
| 1 | the circuit is written only *after* the challenge path has failed, never before it — a challenge-labelled `403` still opens the breaker | `tests/test_edge_challenge_verdict.py::CircuitInvariantTests::test_a_challenge_labelled_403_still_opens_the_circuit` |
| 2 | after rotating the exit the circuit must be cleared, **including the challenge label** (`clear_session_circuit`), or the next exit inherits a challenge that never happened there | `...::test_clear_session_circuit_resets_the_challenge_label` |
| 3 | the challenge path adds no new failure **class** — the suffix must not re-classify or change terminality | `...::test_the_suffix_does_not_change_the_failure_class` |

One deliberate deviation from the plan's stage table: S0's judgement reaches the
error string through `http_client`'s circuit rather than through the `auth_flow`
steps, because that is where the `session_circuit_open` name is produced. The
`auth_flow`-side consumer arrives with S2's `rotate_exit`, through `deps`'s
existing `from ..auth_state import (...)` statement (which costs no new
cross-directory edge).

### Rotating the exit once (S2)

`registration.edge_challenge_rotate_exit` (default **false**) turns the
judgement into one action: on a challenge, move the run to a new exit **in the
same pool** and retry the one request, exactly once.

| Piece | Owner | Why there |
| --- | --- | --- |
| the retry, and the once-per-request cap | `http_client.request_with_retry` | it owns the response and the attempt budget |
| the decision + the exit move | the registration handler, through the session's `_openai_edge_challenge_hook` | it owns `s.proxy`, the pool cursor, the audit counters and `clear_session_circuit` |
| the counters | `proxy_audit` (`edge_challenge_hits` / `edge_challenge_unknown` / `edge_challenge_rotations` / `edge_challenge_rotate_failed`) → the batch funnel's `edge_challenge` block | they are **counts, not identities**: the before/after exit is a sticky-session credential and is never audited |

Two consequences worth stating plainly:

* **A single-slot exit short-circuits and says so.** A provider with no sticky
  session id leaves the URL unchanged; that is counted as a *failed* rotation,
  not as a successful one, or the A/B's `rotate` arm would be an `observe` arm
  wearing a label.
* **`unknown` rotates too, under its own key** (§7-5). The asymmetry says spend
  the uncertainty on the cheap side, but merging it into `edge_challenge_hits`
  would dilute the number the manipulation check reads. From
  `request_with_retry` the `unknown` branch is currently unreachable (the policy
  only runs on `403`/`429`, and the verdict answers `unknown` only off those
  statuses); it is written and tested so the contract is complete for the next
  caller.

**Not landed: the browser handoff (S4).** The sign-off chose "reuse
`browser_flow`, default to returning to the protocol lane", and that needs a
browser-driver mode which does not exist yet — `run_browser_registration` runs a
*complete* registration and accepts no cookie jar. Landing
`registration.edge_challenge_browser_handoff` before that mode exists would be a
switch that nothing executes, which is the fake-comparison failure the mechanism
gate above exists to prevent. See `plan-2026-10-05` §3.4.2.

## Validation limits

Offline tests cover step ordering, redirect classification and Sentinel header
plumbing. They do not establish a live registration or login success rate.
Changing the lane choice, the `send` method, the continue-on-verified-page
toggle, the password-page prime, the signup continue's declared screen (on the
lane's own continue or on the extra one issued from `/email-verification`), or
Sentinel flow selection requires a controlled live comparison, not an offline
transaction test. The pre-registered A/B design, collection and comparison for
the outstanding comparisons (preflight login endpoint, Cloudflare-challenge
observation, Sentinel password bundle, password-page prime, signup-continue
screen hint, about-you page prime, create-account disallowed backoff,
email-verification continue hint) is
[`registration-ab-runbook.md`](registration-ab-runbook.md),
driven by `scripts/registration_ab.py`.

🔴 **A comparison needs two manipulation checks, not one.** Verifying that the
config carried the toggle is not enough: on 2026-10-07 the
`signup_continue_screen_hint` arm ran with the toggle on while the code path was
unreachable, so the arm measured nothing and its 5/5 failure was almost read as
"the hypothesis is false". `collect` therefore also records whether the arm's
**log** shows the toggle's own line (`mechanism_ok`), and `compare` returns
`manipulation_failed` -- ahead of `underpowered` -- when it does not. New
toggles must register a stdout mechanism line in `scripts/registration_ab.py`;
see the runbook's mechanism table.

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
