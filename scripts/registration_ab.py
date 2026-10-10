"""Registration A/B harness for the live comparisons that are still owed.

``docs/current/protocol-registration.md`` ("Validation limits") requires a
controlled live comparison before trusting a landed-but-unvalidated change.
``plan`` prints the full pre-registered set; the current arms are:

* **P0-1** — preflight login entry: ``browser`` (``chatgpt.com/auth/login``)
  vs ``legacy`` (``auth.openai.com/log-in``).
* **P0-2** — Cloudflare-challenge discrimination on the preflight failure path.
  No toggle: the classification is always on, so this arm is *observe-only*.
* **P1-3** — Sentinel password-page bundle: ``sentinel_password_bundle`` off
  (default) vs on.
* **P1-4** — password-page prime before ``user/register``.
* **P1-5** — ``screen_hint`` on the signup lane's existing ``authorize/continue``.
* **P1-6** — ``/about-you`` prime before ``create_account``.
* **P1-7** — bounded backoff on ``registration_disallowed``.
* **P1-8** — extra declared-screen ``authorize/continue`` from
  ``/email-verification`` (the landing P1-5 cannot reach).

This script **never performs live traffic**. Only the operator runs the real
batch, because it needs paid mailboxes, a live exit pool, and a controlled
environment (offline tests cannot establish a registration success rate). The
harness does three things:

``plan``     print the pre-registered design: arms, exact config overrides,
             held-constant variables, metrics and decision rule.
``collect``  normalise one operator-run arm's artifacts (run log + batch report
             + config snapshot) into a single record under
             ``runtime/registration_ab/``. Two manipulation checks are recorded:
             the config snapshot must match the arm's toggle value
             (``toggle_verified``), and the arm's **log** must show the code path
             the toggle owns actually ran (``mechanism_ok``). Without the first
             the arm is ``unverified``; without the second it is a fake
             comparison and ``compare`` says ``manipulation_failed`` rather than
             reporting a rate.
``compare``  join the arm records, compute the per-metric deltas and apply the
             pre-registered decision rule. It refuses a verdict for an arm whose
             toggle was not verified or whose mechanism never ran, and it never
             reports a success *rate* as established by this offline tooling.

Parsing and comparison are pure functions so they can be unit-tested without
running anything (``tests/test_registration_ab.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "runtime" / "registration_ab"

#: Minimum attempted registrations (and preflight probes) per arm before a
#: comparison is allowed to return anything but ``underpowered``. Pre-registered
#: so it cannot be moved after seeing the data.
MIN_ARM_ATTEMPTED = 30
#: Absolute difference in a rate that counts as a real move, pre-registered.
RATE_DELTA = 0.05

EXPERIMENTS: dict[str, dict[str, Any]] = {
    "p0-1-preflight-endpoint": {
        "hypothesis": (
            "Probing chatgpt.com/auth/login (browser entry) draws fewer "
            "Cloudflare challenges than probing auth.openai.com/log-in, which a "
            "real browser never requests standalone."
        ),
        "toggle": "registration.preflight_login_page",
        "arms": {"browser": "browser", "legacy": "legacy"},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "preflight.cloudflare_rate (challenges / probes)",
            "preflight.no_healthy_route",
            "preflight.probes_ok",
            "funnel.registered_per_attempted",
        ),
        "decision": (
            "favor the arm with the lower cloudflare_rate when the gap exceeds "
            f"{RATE_DELTA:.2f} and its no_healthy_route count is not higher; "
            "otherwise inconclusive."
        ),
    },
    "p0-2-cloudflare-observation": {
        "hypothesis": (
            "The Cloudflare discrimination path (CLOUDFLARE_CHALLENGE_MARKER) "
            "actually classifies exit-level refusals instead of generic 4xx."
        ),
        "toggle": None,
        "arms": {"observe": None},
        "hold_constant": ("same exit pool and mailbox batch as the observation window",),
        "metrics": (
            "preflight.cloudflare_hosts (per-exit challenge counts)",
            "preflight.skipped_hosts",
            "funnel.registration_failures_by_class",
        ),
        "decision": (
            "observation only: report the challenge rate per exit. A change to "
            "in-flow rotation needs its own toggle and its own A/B before landing."
        ),
    },
    "p1-3-sentinel-password-bundle": {
        "hypothesis": (
            "Priming the password page's Sentinel flows from one shared proof "
            "does not reduce registration success and does not introduce "
            "sentinel-specific failures."
        ),
        "toggle": "registration.sentinel_password_bundle",
        "arms": {"default": False, "bundle": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool",
            "same registration lane (password lane only) and driver",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (sentinel_* codes)",
            "sentinel.bundle_primed (manipulation check)",
        ),
        "decision": (
            "favor bundle only when it is not worse by more than "
            f"{RATE_DELTA:.2f} AND no sentinel-specific failure class grows; a "
            "regression of any size is a reason to keep the default off."
        ),
    },
    "p1-4-prime-password-page": {
        "hypothesis": (
            "GETting /create-account/password before POSTing user/register "
            "establishes the password-step transaction state, so the server "
            "dispatches the email OTP instead of arming the passwordless arm "
            "-- the measured failure shape is user/register 200 + email-otp/send "
            "200 + a landing on /email-verification with zero messages across "
            "every mailbox (2026-10-06, ~11 attempts, 3 configurations, "
            "byte-identical). Both reference protocol clients (turb "
            "navigate_create_account_password, SunnyRegister _submit_password) "
            "navigate there unconditionally."
        ),
        "toggle": "registration.prime_create_account_password",
        "arms": {"default": False, "prime": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family: "
            "email_otp_send_stuck / email_otp_poll_timeout)",
            "funnel.registration_failures_by_stage (email_otp_wait / user_register)",
        ),
        "mechanism": "Create account password page",
        "decision": (
            "favor_prime when the prime arm's success rate exceeds the default "
            f"arm's by more than {RATE_DELTA:.2f}; keep_default_off when it is "
            "worse by more than the same delta; otherwise inconclusive. The "
            "mailbox-class failure counts are the mechanism read: a working "
            "prime should shrink email_otp_send_stuck / email_otp_poll_timeout, "
            "and a prime arm whose mailbox-class failures GROW is a red flag "
            "regardless of the rate verdict."
        ),
    },
    "p1-5-signup-continue-screen-hint": {
        "hypothesis": (
            "Declaring screen_hint=signup in the signup lane's "
            "authorize/continue body does not reduce registration success and "
            "may fix the no-dispatch shape: both working reference clients "
            "declare the screen on every continue (SunnyRegister _authorize_email "
            "sends login-if-existing-else-signup; the login lane here added "
            "screen_hint=login after the 09-14/09-16 measurements), while this "
            "lane's body carries only username."
        ),
        "toggle": "registration.signup_continue_screen_hint",
        "arms": {"default": False, "hint": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "same time-of-day window (Cloudflare load is diurnal)",
            "registration.signup_email_verification_continue_hint false in BOTH arms "
            "(its forced POST reuses the same body-building branch, so leaving it on "
            "would let P1-8's path emit P1-5's marker)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
        ),
        "mechanism": "Signup continue declares screen_hint=signup",
        "decision": (
            "favor_hint when the hint arm's success rate exceeds the default "
            f"arm's by more than {RATE_DELTA:.2f}; keep_default_off when it is "
            "worse by more than the same delta; otherwise inconclusive. An "
            "auth_state-class growth in the hint arm means the declared screen "
            "conflicts with the server's transaction reading -- that alone is a "
            "reason to keep the default off."
        ),
    },
    "p1-6-prime-about-you-page": {
        "hypothesis": (
            "GETting /about-you before POSTing create_account aligns the auth "
            "session's page state with the Referer the POST already claims, and "
            "does not reduce registration success. turb navigates there first "
            "(navigate_about_you); this repo currently POSTs from a state it "
            "never established -- the same gap the password page had."
        ),
        "toggle": "registration.prime_about_you_page",
        "arms": {"default": False, "prime": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "same lane (password or passwordless) across both arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_stage (create_account)",
            "funnel.registration_failures_by_class (account = user_already_exists / registration_disallowed family)",
        ),
        "mechanism": "About you page prime",
        "decision": (
            "favor_prime when the prime arm's success rate exceeds the default "
            f"arm's by more than {RATE_DELTA:.2f}; keep_default_off when it is "
            "worse by more than the same delta; otherwise inconclusive. An "
            "account-class growth in the prime arm means the navigation "
            "disturbed the transaction -- a red flag regardless of the rate."
        ),
    },
    "p1-7-create-disallowed-backoff": {
        "hypothesis": (
            "Retrying create_account with a bounded backoff ([8, 20, 45]s, a "
            "fresh Sentinel proof each round) on registration_disallowed "
            "recovers addresses the server rejected transiently while its "
            "IP/Sentinel risk window settles (SunnyRegister's measured reason "
            "for exactly this retry, sensitive on Remail), instead of "
            "permanently dead-ending them under the terminal account class."
        ),
        "toggle": "registration.create_account_disallowed_backoff",
        "arms": {"default": False, "backoff": True},
        "hold_constant": (
            "same mailbox batch and provider (Remail-heavy if the operator wants the measured population)",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "same time-of-day window",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (account, specifically "
            "create_account_failed:registration_disallowed occurrences)",
            "stage duration (create_account retries spend up to ~73s of sleeps plus a Sentinel proof per round)",
        ),
        "mechanism": "Create account temporarily disallowed",
        "decision": (
            "favor_backoff when the backoff arm's success rate exceeds the "
            f"default arm's by more than {RATE_DELTA:.2f}; keep_default_off "
            "when it is worse by more than the same delta; otherwise "
            "inconclusive. An account-class failure count that fails to shrink "
            "means the rejections were permanent, not risk-window transience -- "
            "the retries only added latency and spent Sentinel proofs."
        ),
    },
    "p1-8-email-verification-continue-hint": {
        "hypothesis": (
            "On the password lane, when authorize lands on /email-verification, "
            "issuing one extra authorize/continue that declares "
            "screen_hint=signup moves the server's transaction arm off "
            "passwordless_login/passwordless_signup and back onto the signup "
            "arm, so email-otp/send dispatches a code instead of leaving "
            "passwordless_email_otp_send_pending set. The measured failure shape "
            "(2026-10-06/07, ~16 attempts) is a correct landing plus a 200 send "
            "with zero dispatched messages, and the client_auth_session dump "
            "reads original_screen_hint=signup while "
            "email_verification_mode stays passwordless. The sibling toggle "
            "p1-5-signup-continue-screen-hint cannot test this: its read point "
            "is skipped by the same early return that produces the landing, so "
            "its hint arm never posts (run_p15_hint.log: 0/5, no continue line)."
        ),
        "toggle": "registration.signup_email_verification_continue_hint",
        "arms": {"default": False, "hint": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "registration.signup_continue_screen_hint false in BOTH arms (the extra "
            "POST carries the declaration; the sibling toggle must not also be on)",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read -- the only value that separated success from failure across the "
            "2026-10-08 signin runs; the code cannot dispatch while the key is present)",
            "client_auth_session.email_verification_mode (secondary: which arm the server picked)",
        ),
        "mechanism": "Email verification continue hint",
        "decision": (
            "favor_hint when the hint arm's success rate exceeds the default "
            f"arm's by more than {RATE_DELTA:.2f}; keep_default_off when it is "
            "worse by more than the same delta; otherwise inconclusive. An "
            "auth_state-class growth in the hint arm means the extra POST "
            "conflicts with the server's transaction reading -- that alone is a "
            "reason to keep the default off. A rate that does not move while "
            "the pending key also fails to clear means the declaration "
            "after the fact cannot re-arm the transaction, which closes the "
            "screen_hint family and points the next experiment at the signin "
            "shape (login_or_signup) instead."
        ),
    },
    "p1-9-prime-navigation-headers": {
        "hypothesis": (
            "The password-page prime's navigation headers were incomplete (no "
            "sec-fetch-site / sec-fetch-user), so the GET did not carry real "
            "top-level navigation semantics -- the unexcluded explanation for "
            "P1-4's 'prime landed correctly but the transaction stayed "
            "passwordless'. Sending the two headers turb sends should make the "
            "prime establish the password-page state the server actually "
            "reads, so email-otp/send dispatches a code. Independent single "
            "variable: prime_create_account_password is ON in both arms."
        ),
        "toggle": "registration.prime_navigation_headers",
        "arms": {"default": False, "headers": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "registration.prime_create_account_password true in BOTH arms "
            "(the headers are a delta on the prime, not a substitute for it)",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read -- the only value that separated success from failure across the "
            "2026-10-08 signin runs; the code cannot dispatch while the key is present)",
            "client_auth_session.email_verification_mode (secondary: which arm the server picked)",
        ),
        "mechanism": "Password page navigation headers",
        "decision": (
            "favor_headers when the headers arm's success rate exceeds the "
            f"default arm's by more than {RATE_DELTA:.2f}; keep_default_off when "
            "it is worse by more than the same delta; otherwise inconclusive. "
            "An auth_state-class growth in the headers arm means the navigation "
            "disturbed the transaction -- that alone keeps the default off. A "
            "rate that does not move while the pending key also fails to "
            "clear closes the prime family entirely and points the next "
            "experiment at the signin shape (login_or_signup)."
        ),
    },
    "p1-10-signin-screen-hint": {
        "hypothesis": (
            "The password lane's first signin declares screen_hint=signup, and the "
            "server arms the transaction on email_verification_mode="
            "passwordless_signup / passwordless_signup_from_default_redirect=true, "
            "so email-otp/send never dispatches. Two 2026-10-07/08 live runs "
            "(hint arm on lajiao, default arm on fireside, different mailboxes) "
            "produced the same arm and the same email_otp_send_stuck, excluding "
            "exit, mailbox and the declared continue screen. turb's signin sends "
            "screen_hint=login_or_signup; declaring that instead should move the "
            "transaction arm off passwordless_* so the code dispatches."
        ),
        "toggle": "registration.signin_screen_hint_login_or_signup",
        "arms": {"default": False, "login_or_signup": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "authorize/continue behaviour unchanged in BOTH arms (the declared "
            "continue screen toggles stay off; only the signin screen_hint moves)",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read -- the only value that separated success from failure across the "
            "2026-10-08 signin runs; the code cannot dispatch while the key is present)",
            "client_auth_session.email_verification_mode (secondary: which arm the server picked)",
        ),
        "mechanism": "Signin screen_hint=login_or_signup",
        "decision": (
            "favor_login_or_signup when that arm's success rate exceeds the "
            f"default arm's by more than {RATE_DELTA:.2f}; keep_default_off when "
            "it is worse by more than the same delta; otherwise inconclusive. "
            "An auth_state-class growth in that arm means the declared screen "
            "conflicts with the server's reading -- that alone keeps the default "
            "off. A rate that does not move while the pending key also "
            "fails to clear closes the client-side screen_hint family entirely "
            "and points at the server's own routing (passwordless_signup_from_"
            "default_redirect) rather than another wire field."
        ),
    },
    "p1-11-signin-prompt-login": {
        "hypothesis": (
            "turb's signin sends prompt=login *and* screen_hint=login_or_signup. "
            "Four 2026-10-07/08 live runs (lajiao/fireside pools, burned/brand-new "
            "mailboxes, signup/login_or_signup/a re-declared continue) all landed "
            "on email_verification_mode=passwordless_signup with "
            "passwordless_signup_from_default_redirect=true and a non-dispatching "
            "OTP -- the declared screen is recorded (original_screen_hint follows "
            "it) but does not pick the arm. Declaring prompt=login as well should "
            "move the transaction arm off passwordless_* so the code dispatches."
        ),
        "toggle": "registration.signin_prompt_login",
        "arms": {"default": False, "prompt_login": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "registration.signin_screen_hint_login_or_signup true in BOTH arms "
            "(turb pairs prompt=login with login_or_signup; the pair must vary only prompt)",
            "authorize/continue behaviour unchanged in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read -- the only value that separated success from failure across the "
            "2026-10-08 signin runs; the code cannot dispatch while the key is present)",
            "client_auth_session.email_verification_mode (secondary: which arm the server picked)",
        ),
        "mechanism": "Signin prompt=login",
        "decision": (
            "favor_prompt_login when that arm's success rate exceeds the "
            f"default arm's by more than {RATE_DELTA:.2f}; keep_default_off when "
            "it is worse by more than the same delta; otherwise inconclusive. "
            "An auth_state-class growth in that arm means the declared prompt "
            "conflicts with the server's reading -- that alone keeps the default "
            "off. A rate that does not move while the pending key also "
            "fails to clear closes turb's signin shape (screen_hint + prompt) and "
            "points at the remaining deltas: not posting authorize/continue, and "
            "locale."
        ),
    },
    "p1-12-signin-locale": {
        "hypothesis": (
            "SunnyRegister's signin shape is prompt=login + screen_hint=signup + "
            "locale=ja-JP (protocol_auth.py _start_next_auth), and it is the only "
            "reference that declares a locale. Four 2026-10-07/08 live runs showed "
            "the declared screen reach the server (original_screen_hint follows "
            "it) without picking the transaction arm, and prompt=login alone moved "
            "it to passwordless_login + invalid_auth_step. Adding locale=ja-JP on "
            "top of SunnyRegister's shape should arm the signup (password) "
            "transaction so email-otp/send dispatches."
        ),
        "toggle": "registration.signin_locale_ja_jp",
        "arms": {"default": False, "locale": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "registration.signin_prompt_login true in BOTH arms (SunnyRegister pairs locale with prompt=login)",
            "registration.signin_screen_hint_login_or_signup false in BOTH arms "
            "(SunnyRegister sends screen_hint=signup, not login_or_signup)",
            "authorize/continue behaviour unchanged in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read -- the only value that separated success from failure across the "
            "2026-10-08 signin runs; the code cannot dispatch while the key is present)",
            "client_auth_session.email_verification_mode (secondary: which arm the server picked)",
        ),
        "mechanism": "Signin locale=ja-JP",
        "decision": (
            "favor_locale when that arm's success rate exceeds the default arm's "
            f"by more than {RATE_DELTA:.2f}; keep_default_off when it is worse by "
            "more than the same delta; otherwise inconclusive. An auth_state-class "
            "growth in that arm means the declared locale conflicts with the "
            "server's reading -- that alone keeps the default off. A rate that "
            "does not move while the pending key also fails to clear closes "
            "the whole client-side signin-shape family (screen_hint + prompt + "
            "locale) and points at the server's own routing "
            "(passwordless_signup_from_default_redirect) or at the fingerprint / "
            "device layer instead of another wire field."
        ),
    },
    "p1-13-turb-signin-authorize-context": {
        "hypothesis": (
            "turb's signin/authorize context differs from ours in five parameters, "
            "and its own comment dates the change to a successful 2026-09-14 "
            "sample: the signin query carries no device_id / passkey capabilities / "
            "ccaps, and the authorize URL drops ext-passkey-client-capabilities, "
            "sets ccaps='login_methods chatgpt_login_finalizer_v1', and adds "
            "auth_return_target_category=chatgpt_home plus ui_locales. The signin "
            "series (p1-10..p1-12) tested screen_hint/prompt/locale one field at a "
            "time; this is the rest of the context, as one coherent manipulation. "
            "Switching to turb's shape should arm the signup (password) transaction "
            "so email-otp/send dispatches."
        ),
        "toggle": "registration.turb_signin_authorize_context",
        "arms": {"default": False, "turb": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "registration.signin_screen_hint_login_or_signup / signin_prompt_login / "
            "signin_locale_ja_jp unchanged in BOTH arms",
            "authorize/continue behaviour unchanged in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read)",
            "client_auth_session.email_verification_mode (secondary: which arm the server picked)",
        ),
        "mechanism": "Turb signin/authorize context",
        "decision": (
            "favor_turb when that arm's success rate exceeds the default arm's by "
            f"more than {RATE_DELTA:.2f}; keep_default_off when it is worse by more "
            "than the same delta; otherwise inconclusive. An auth_state-class "
            "growth in the turb arm means the context conflicts with the server's "
            "reading -- that alone keeps the default off. A rate that does not move "
            "while the pending key also fails to clear means the signin/authorize "
            "context is not the lever either, and the next candidate is the "
            "fingerprint / device layer."
        ),
    },
    "p1-14-prime-password-page-fatal": {
        "hypothesis": (
            "turb makes the password-page navigation fatal: if the final URL is not "
            "/create-account/password it raises, because the navigation exists to "
            "establish the page state before user/register claims it. Ours reports "
            "a wrong landing and continues. Making the landing fatal should either "
            "separate 'the state was never established' from 'the state was "
            "established and the server still did not dispatch', or show that the "
            "landing is always correct and the prime is not the lever."
        ),
        "toggle": "registration.prime_password_page_fatal",
        "arms": {"default": False, "fatal": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "registration.prime_create_account_password true in BOTH arms (there is no prime to make fatal otherwise)",
            "registration.prime_navigation_headers true in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (auth_state = the abort family)",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read)",
            "count of password_page_not_reached aborts (the manipulation's own effect)",
        ),
        "mechanism": "Password page landing is fatal",
        "decision": (
            "favor_fatal when that arm's success rate exceeds the default arm's by "
            f"more than {RATE_DELTA:.2f}; keep_default_off when it is worse by more "
            "than the same delta; otherwise inconclusive. A nonzero abort count "
            "with no rate change is not a failure of the hypothesis -- it is the "
            "measurement that the landing was not being reached, and the next "
            "variable is the prime itself (p1-9) rather than its fatality."
        ),
    },
    "p1-15-otp-navigation-headers": {
        "hypothesis": (
            "turb sends sec-fetch-site: same-origin + sec-fetch-user: ?1 on the "
            "email-otp/send navigation; ours sends only Accept + Referer. Those "
            "two headers are how a real top-level navigation declares itself, and "
            "the repo already measured that lesson for the password page (p1-9). "
            "Sending them on the OTP send should let the server treat it as the "
            "page's own dispatch and actually send the code."
        ),
        "toggle": "registration.otp_navigation_headers",
        "arms": {"default": False, "headers": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "password lane only (email_registration.registration_mode != passwordless)",
            "registration.prime_create_account_password / prime_password_page_fatal unchanged in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (mailbox = the OTP-delivery family)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT "
            "(primary mechanism read)",
        ),
        "mechanism": "OTP navigation headers",
        "decision": (
            "favor_headers when that arm's success rate exceeds the default arm's "
            f"by more than {RATE_DELTA:.2f}; keep_default_off when it is worse by "
            "more than the same delta; otherwise inconclusive. A rate that does "
            "not move while the pending key also fails to clear means the send "
            "navigation's own headers are not the lever."
        ),
    },
    "p1-16-otp-validate-sentinel": {
        "hypothesis": (
            "turb attaches a freshly minted authorize_continue Sentinel (plus its "
            "SO) to email-otp/validate; ours passes use_sentinel=False. The "
            "current failure is one step earlier (the code never dispatches), so "
            "this is a latent wire difference rather than a candidate cause -- it "
            "is pre-registered so that the day a code does arrive, the validate "
            "request shape is already controlled."
        ),
        "toggle": "registration.otp_validate_sentinel",
        "arms": {"default": False, "sentinel": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "registration.otp_navigation_headers unchanged in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (sentinel = mint/validation family)",
            "funnel.registration_failures_by_class (auth_state)",
            "client_auth_session_dump[after_otp_send]: passwordless_email_otp_send_pending ABSENT",
        ),
        "mechanism": "OTP validate sentinel",
        "decision": (
            "favor_sentinel when that arm's success rate exceeds the default arm's "
            f"by more than {RATE_DELTA:.2f}; keep_default_off when it is worse by "
            "more than the same delta, or when a sentinel-class failure grows; "
            "otherwise inconclusive. Not judgeable while no arm receives a code "
            "at all (the validate step is never reached)."
        ),
    },
    "p1-17-otp-external-url-branch": {
        "hypothesis": (
            "After a successful email-otp/validate the server's own answer decides "
            "whether create_account should run. turb reads page.type and the "
            "continue_url and, when the transaction already finished on an "
            "external/callback URL, skips create_account -- POSTing it anyway is "
            "what answers invalid_auth_step. Ours always runs create_account. "
            "Adding the branch should stop those transactions failing a step they "
            "had already finished."
        ),
        "toggle": "registration.otp_external_url_branch",
        "arms": {"default": False, "branch": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "registration.otp_navigation_headers / otp_validate_sentinel unchanged in BOTH arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (auth_state = invalid_auth_step family)",
            "count of create_account steps skipped by the branch (the manipulation's own effect)",
        ),
        "mechanism": "OTP external_url branch",
        "decision": (
            "favor_branch when that arm's success rate exceeds the default arm's by "
            f"more than {RATE_DELTA:.2f}; keep_default_off when it is worse by more "
            "than the same delta; otherwise inconclusive. Not judgeable while the "
            "branch never fires (no arm reaches a validated OTP), which the "
            "decision rule reports as not_judgeable rather than as no-effect."
        ),
    },
    "p0-2b-inflow-challenge-handoff": {
        "hypothesis": (
            "H1: a challenge that arrives after preflight can be recovered by "
            "rotating to a new exit in the same pool and retrying the one "
            "request, so the rotate arm's registered_per_attempted is higher "
            "(and its mailboxes_consumed_per_registered lower) than observe's. "
            "H2: when rotation still does not get through, handing the protocol "
            "session to a browser driver lets the account finish. Both are from "
            "plan-2026-10-05-inflow-challenge-handoff.md; the cost asymmetry is "
            "that a missed challenge burns a mailbox and its OTP, while a false "
            "positive costs one request."
        ),
        "toggle": "registration.edge_challenge_rotate_exit",
        # The harness verifies ONE toggle per arm.  ``rotate_exit`` is the axis
        # this experiment is named for, and ``handoff`` is its superset (all-on),
        # so the three arms stay ordered on that one key.  The handoff switch
        # itself does not exist yet (S4) -- see the decision text.
        "arms": {"observe": False, "rotate": True, "handoff": True},
        "hold_constant": (
            "same mailbox batch and provider",
            "same exit pool (same proxy_seeds / lanes)",
            "same registration driver, concurrency and stage timeouts",
            "same registration_mode across all three arms",
            "same time-of-day window (Cloudflare load is diurnal)",
        ),
        "metrics": (
            "preflight.cloudflare_rate (operational check: the three arms must not differ, or the pool changed)",
            "funnel.edge_challenge.hits (per-arm challenge count -- the manipulation read)",
            "funnel.edge_challenge.rotations / rotate_failed (did the exit really move?)",
            "funnel.registered_per_attempted",
            "funnel.registration_failures_by_class (a NEW class must be zero)",
            "mailboxes_consumed_per_registered = attempted / registered (derived; this is the real benefit metric)",
        ),
        # No ``mechanism`` marker on purpose: this experiment's manipulation
        # check is CONDITIONAL (a rotation line can only appear if a challenge
        # happened), so it lives in the decision rule as ``not_judgeable``
        # rather than in the always-on mechanism gate, which would call a
        # challenge-free window ``manipulation_failed``.
        "decision": (
            "not_judgeable when no arm observed a challenge (an empty window "
            "measured nothing -- that is not 'no effect'), when the rotate arm "
            "observed challenges but rotated zero times (a single-slot pool "
            "makes it an observe arm), or when the counters are absent from the "
            "funnel. stop_all_arms when any new failure class appears or the "
            "handoff arm writes a registrable address into the dead-end "
            "ledger. favor_rotate when rotate's registered_per_attempted beats "
            f"observe's by more than {RATE_DELTA:.2f} with no new class; "
            "keep_default_off when it is worse by the same margin; otherwise "
            "inconclusive. H2 (handoff) is judged separately and never merged "
            "into the rotate verdict."
        ),
    },
}

# Preflight progress lines emitted by ``commands/registration`` (verbatim
# markers; the success line uses full-width parens, the failure lines ASCII
# parens -- match on the marker, not the punctuation).
_PREFLIGHT_PROGRESS = re.compile(r"注册预检\s+(\d+)/(\d+)\s+(.*?)\s+(可用|被 Cloudflare 挑战|失败)")
_PREFLIGHT_COMPLETED = re.compile(r"注册预检完成：(\d+)/(\d+)\s*条实测可用")
_PREFLIGHT_SKIPPED = re.compile(r"剩余候选已跳过：(.+)")
_NO_HEALTHY_ROUTE = re.compile(r"registration_preflight_failed:no_healthy_route:(\w+)")
_HOST_COUNT = re.compile(r"([^,×]+?)\s*×\s*(\d+)")


def parse_preflight_log(text: str) -> dict[str, Any]:
    """Extract preflight counters from a registration run log.

    Pure: the returned shape is what ``collect`` persists and ``compare`` joins.
    """
    probes_ok = 0
    probes_cloudflare = 0
    probes_failed = 0
    cloudflare_hosts: dict[str, int] = {}
    failed_hosts: dict[str, int] = {}
    completed: tuple[int, int] | None = None
    skipped_hosts: dict[str, int] = {}
    no_healthy_route = ""
    for line in str(text or "").splitlines():
        progress = _PREFLIGHT_PROGRESS.search(line)
        if progress:
            label, outcome = progress.group(3), progress.group(4)
            if outcome == "可用":
                probes_ok += 1
            elif outcome == "被 Cloudflare 挑战":
                probes_cloudflare += 1
                cloudflare_hosts[label] = cloudflare_hosts.get(label, 0) + 1
            else:
                probes_failed += 1
                failed_hosts[label] = failed_hosts.get(label, 0) + 1
            continue
        done = _PREFLIGHT_COMPLETED.search(line)
        if done:
            completed = (int(done.group(1)), int(done.group(2)))
            continue
        skipped = _PREFLIGHT_SKIPPED.search(line)
        if skipped:
            for label, count in _HOST_COUNT.findall(skipped.group(1)):
                skipped_hosts[label.strip()] = int(count)
            continue
        route = _NO_HEALTHY_ROUTE.search(line)
        if route:
            no_healthy_route = route.group(1)
    probes = probes_ok + probes_cloudflare + probes_failed
    return {
        "probes": probes,
        "probes_ok": probes_ok,
        "probes_cloudflare": probes_cloudflare,
        "probes_failed": probes_failed,
        "cloudflare_rate": round(probes_cloudflare / probes, 4) if probes else None,
        "cloudflare_hosts": dict(sorted(cloudflare_hosts.items())),
        "failed_hosts": dict(sorted(failed_hosts.items())),
        "skipped_hosts": dict(sorted(skipped_hosts.items())),
        "completed_ok": completed[0] if completed else None,
        "completed_attempted": completed[1] if completed else None,
        "no_healthy_route": no_healthy_route,
    }


def load_funnel(path: Path | None) -> dict[str, Any] | None:
    """Read the safe funnel block from a batch report.

    Accepts either the whole report (``{"funnel": {...}}``) or the funnel dict
    itself. Returns ``None`` when there is no usable funnel.
    """
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable or malformed batch report is "no usable funnel", not a
        # crash: the run itself may still be comparable on preflight counters,
        # and an operator typo in ``--funnel`` must not take down the tool
        # before it can say ``underpowered``.
        return None
    if not isinstance(payload, Mapping):
        return None
    funnel = payload.get("funnel")
    if isinstance(funnel, Mapping):
        return dict(funnel)
    if "attempted" in payload or "registered" in payload:
        return dict(payload)
    return None


#: Registration keys the manipulation check records although no experiment
#: declares them as its toggle -- observation-only designs such as ``p0-2``.
_TOGGLE_KEYS_WITHOUT_AN_EXPERIMENT = ("edge_challenge_discrimination",)


def _registration_toggle_keys() -> tuple[str, ...]:
    """Every ``registration.*`` key a config snapshot must expose to ``compare``.

    Derived from ``EXPERIMENTS`` instead of hand-listed, because the hand-listed
    version drifted the moment it mattered: the four signin-shape toggles
    (P1-9..P1-12) were added to the design table but not to the reader, so
    ``collect`` recorded ``toggle_actual=None`` for them and ``compare``
    answered ``unverified`` no matter what ``--config`` held.  The arm could not
    be judged at all, and the mechanism gate added for the 2026-10-07 fake
    comparison was never even reached (``unverified`` returns first).  A derived
    reader cannot drift from the design table it derives from.
    """
    keys = {
        str(design["toggle"]).split(".", 1)[1]
        for design in EXPERIMENTS.values()
        if design.get("toggle") and str(design["toggle"]).startswith("registration.")
    }
    keys.update(_TOGGLE_KEYS_WITHOUT_AN_EXPERIMENT)
    return tuple(sorted(keys))


def read_toggles(config_path: Path | None) -> dict[str, Any]:
    """Read the A/B toggles from a config snapshot (the manipulation check)."""
    if config_path is None:
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Fail closed: without a snapshot the manipulation check cannot pass, so
        # ``compare`` refuses the verdict as ``unverified`` rather than crashing.
        return {}
    registration = payload.get("registration") if isinstance(payload, Mapping) else None
    registration = registration if isinstance(registration, Mapping) else {}
    return {f"registration.{key}": registration[key] for key in _registration_toggle_keys() if key in registration}


def build_record(
    *,
    experiment: str,
    arm: str,
    log_text: str,
    funnel: Mapping[str, Any] | None,
    toggles: Mapping[str, Any],
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble one arm's normalised record.

    Two manipulation checks ride along:

    * ``toggle_verified`` -- the config snapshot this arm was run under matches
      the arm's declared toggle value.
    * ``mechanism_*`` -- the arm's **log** shows the code path the toggle owns
      actually ran (or, for the control arm, that it did not).  ``mechanism_ok``
      is ``None`` when the experiment registers no marker, or when the arm's
      expected value is not a boolean (``p0-1``'s arms are endpoint names).
      A marker must be a stdout line (``print``/``emit``) so it survives either
      log channel the operator may hand to ``collect``.
    """
    design = EXPERIMENTS.get(experiment)
    if design is None:
        raise ValueError(f"unknown experiment: {experiment}")
    arm_expected = design["arms"].get(arm, "<missing>")
    toggle_name = design["toggle"]
    expected = arm_expected
    actual = toggles.get(toggle_name) if toggle_name else None
    # ``None`` expected means the arm does not own a toggle (observe-only).
    verified = bool(toggle_name) and str(actual) == str(expected)
    marker = str(design.get("mechanism") or "")
    # Only a boolean arm value says which direction the marker should go; a
    # string arm (p0-1's ``browser``/``legacy``) is truthy either way, so
    # enforcing it there would demand the marker in both arms.
    marker_expected = expected if isinstance(expected, bool) else None
    marker_seen = bool(marker) and marker in str(log_text or "")
    if marker and marker_expected is not None:
        mechanism_ok = marker_seen if marker_expected else not marker_seen
    else:
        mechanism_ok = None
    return {
        "experiment": experiment,
        "arm": arm,
        "collected_at": int(time.time()),
        "toggle": toggle_name,
        "toggle_expected": expected,
        "toggle_actual": actual,
        "toggle_verified": verified,
        "mechanism_marker": marker,
        "mechanism_expected": marker_expected,
        "mechanism_seen": marker_seen,
        "mechanism_ok": mechanism_ok,
        "preflight": parse_preflight_log(log_text),
        "funnel": dict(funnel) if isinstance(funnel, Mapping) else None,
        "log_path": str(log_path) if log_path else "",
        "log_sha256": hashlib.sha256(str(log_text or "").encode("utf-8")).hexdigest()[:16],
    }


def _rate(funnel: Mapping[str, Any] | None, key: str) -> float | None:
    if not isinstance(funnel, Mapping):
        return None
    value = funnel.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _per_registered(attempted: Any, registered: Any) -> float | None:
    """``attempted / registered``: mailbox slots spent per account won.  Pure."""
    try:
        won = int(registered or 0)
        if won <= 0:
            return None
        return round(int(attempted or 0) / won, 4)
    except (TypeError, ValueError):
        return None


def _new_failure_class_keys(default: Mapping[str, Any] | None, candidate: Mapping[str, Any] | None) -> dict[str, int]:
    """Failure classes that appear in the candidate arm but not the control.  Pure.

    §4's stop rule is about *new terminal categories*, not about counts moving:
    a class the control never produced means the treatment opened a new way to
    fail, which no success-rate delta can justify.
    """
    before = default if isinstance(default, Mapping) else {}
    after = candidate if isinstance(candidate, Mapping) else {}
    return {
        str(key): _class_count(after, str(key))
        for key in sorted(after)
        if _class_count(after, str(key)) > 0 and str(key) not in before
    }


def _mechanism_failed(row: Mapping[str, Any]) -> bool:
    """True only for a real boolean ``False`` mechanism verdict.

    ``mechanism_ok`` is ``None`` when the experiment registers no marker (or the
    arm's expected value is not a boolean), and ``None`` means "not checked",
    never "failed".  An ``isinstance`` test states that three-state contract
    without an identity comparison against a literal.
    """
    ok = row.get("mechanism_ok")
    return isinstance(ok, bool) and not ok


def compare_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Join arm records and apply the pre-registered decision rule.

    Pure. Returns ``{"experiment", "arms", "verdict", "reason", "powered"}``.
    A verdict of ``unverified`` means at least one arm's config snapshot did not
    match the arm it claims to be -- the comparison is not interpretable.
    """
    rows = [dict(row) for row in records]
    if not rows:
        return {"experiment": "", "arms": [], "verdict": "no_data", "reason": "no arm records", "powered": False}
    experiments = {str(row.get("experiment") or "") for row in rows}
    if len(experiments) != 1:
        return {
            "experiment": "",
            "arms": [row.get("arm") for row in rows],
            "verdict": "mixed_experiments",
            "reason": f"records span {sorted(experiments)}; compare one experiment at a time",
            "powered": False,
        }
    experiment = experiments.pop()
    design = EXPERIMENTS.get(experiment, {})
    unverified = [str(row.get("arm")) for row in rows if not row.get("toggle_verified")]
    if design.get("toggle") and unverified:
        return {
            "experiment": experiment,
            "arms": [row.get("arm") for row in rows],
            "verdict": "unverified",
            "reason": (
                "config snapshot missing or does not match the arm for: "
                + ", ".join(unverified)
                + " -- pass --config so the toggle is verified before comparing"
            ),
            "powered": False,
        }

    # Second manipulation check, and it must run *before* the power test: a
    # toggle whose code path never executed is a fake comparison, not a
    # low-sample one.  ``run_p15_hint.log`` is the motivating case -- its 5/5
    # ``email_otp_send_stuck`` was reported as ``underpowered`` when the hint
    # arm had in fact never posted a continue at all.
    manipulation_failed = [str(row.get("arm")) for row in rows if _mechanism_failed(row)]
    if manipulation_failed:
        return {
            "experiment": experiment,
            "arms": [row.get("arm") for row in rows],
            "verdict": "manipulation_failed",
            "reason": (
                "the toggle's mechanism never ran (or ran in the control) for: "
                + ", ".join(manipulation_failed)
                + f" -- its log lacks the expected marker {design.get('mechanism')!r}; "
                "re-collect with the arm's real run log before comparing rates"
            ),
            "powered": False,
        }

    metrics = {}
    for row in rows:
        arm = str(row.get("arm"))
        preflight = row.get("preflight") or {}
        funnel = row.get("funnel")
        funnel_map = funnel if isinstance(funnel, Mapping) else {}
        edge = funnel_map.get("edge_challenge")
        edge = edge if isinstance(edge, Mapping) else None
        attempted = funnel_map.get("attempted") if isinstance(funnel, Mapping) else None
        registered = funnel_map.get("registered") if isinstance(funnel, Mapping) else None
        metrics[arm] = {
            "attempted": attempted,
            "registered": registered,
            "registered_per_attempted": _rate(funnel, "registered_per_attempted"),
            # Derived, not a second measurement: one attempt spends one fresh
            # mailbox slot, so ``attempted / registered`` is what the plan calls
            # ``mailboxes_consumed_per_registered`` -- the benefit metric, since
            # a recovered challenge is a mailbox that was not burned.
            "mailboxes_consumed_per_registered": _per_registered(attempted, registered),
            "probes": preflight.get("probes"),
            "probes_ok": preflight.get("probes_ok"),
            "probes_cloudflare": preflight.get("probes_cloudflare"),
            "cloudflare_rate": preflight.get("cloudflare_rate"),
            "no_healthy_route": str(preflight.get("no_healthy_route") or ""),
            "failure_classes": (funnel or {}).get("registration_failures_by_class")
            if isinstance(funnel, Mapping)
            else None,
            # ``None`` = the funnel carried no such block, which is *not* zero:
            # it means this run could not answer the question at all.
            "edge_challenge_hits": _class_count(edge, "hits") if edge is not None else None,
            "edge_challenge_rotations": _class_count(edge, "rotations") if edge is not None else None,
            "edge_challenge_rotate_failed": _class_count(edge, "rotate_failed") if edge is not None else None,
            "mechanism_marker": row.get("mechanism_marker"),
            "mechanism_seen": row.get("mechanism_seen"),
            "mechanism_ok": row.get("mechanism_ok"),
        }

    powered = all(
        isinstance(item.get("attempted"), int) and item["attempted"] >= MIN_ARM_ATTEMPTED for item in metrics.values()
    )
    verdict, reason = _rule(experiment, metrics, powered)
    return {
        "experiment": experiment,
        "arms": [str(row.get("arm")) for row in rows],
        "metrics": metrics,
        "powered": powered,
        "min_arm_attempted": MIN_ARM_ATTEMPTED,
        "rate_delta": RATE_DELTA,
        "verdict": verdict,
        "reason": reason,
    }


def _rule_inflow_challenge_handoff(metrics: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    """§4's three-arm rule, with §4.1's invalid-result criteria applied first.

    Split out because it is the only rule with a *conditional* manipulation
    check: "the exit really moved" can only be asked of a window in which a
    challenge actually happened, so a challenge-free run is ``not_judgeable``
    rather than a fake ``manipulation_failed``.
    """
    observe = metrics.get("observe") or {}
    rotate = metrics.get("rotate") or {}
    handoff = metrics.get("handoff") or {}
    h2_note = "H2 (handoff) is not judged: its switch does not exist yet (S4)"

    if any(item.get("edge_challenge_hits") is None for item in (observe, rotate, handoff)):
        return "inconclusive", "the batch funnel carries no edge_challenge block; re-collect from a run that emits it"
    total_hits = sum(int(item.get("edge_challenge_hits") or 0) for item in (observe, rotate, handoff))
    if total_hits == 0:
        # §4.1: an empty window measured nothing -- it is not "no effect".
        return (
            "not_judgeable",
            "no edge challenge occurred in any arm; the window measured nothing (change window/exit pool and re-run), "
            + h2_note,
        )

    observe_rate, rotate_rate = observe.get("registered_per_attempted"), rotate.get("registered_per_attempted")
    if observe_rate is None or rotate_rate is None:
        return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"

    # §4 manipulation check: an arm that observed challenges but never moved the
    # exit is an ``observe`` arm wearing a label, and its rate proves nothing.
    if int(rotate.get("edge_challenge_rotations") or 0) == 0:
        return (
            "not_judgeable",
            "the rotate arm observed challenges but rotated 0 times "
            f"(failed={rotate.get('edge_challenge_rotate_failed')}): a single-slot pool or the switch did not take effect, "
            "so this arm is a copy of observe, " + h2_note,
        )

    new_classes = _new_failure_class_keys(observe.get("failure_classes"), rotate.get("failure_classes"))
    if new_classes:
        return (
            "stop_all_arms",
            f"the rotate arm produced failure classes the control never did ({new_classes}); stop all three arms and return to observe, "
            + h2_note,
        )

    observe_slots, rotate_slots = (
        observe.get("mailboxes_consumed_per_registered"),
        rotate.get("mailboxes_consumed_per_registered"),
    )
    if rotate_rate > observe_rate + RATE_DELTA:
        if rotate_slots is not None and observe_slots is not None and rotate_slots > observe_slots:
            # Monotone in the rate, so this is a bookkeeping contradiction, not
            # a close call -- surface it instead of declaring a win.
            return (
                "inconclusive",
                f"rotate rate {rotate_rate} > observe {observe_rate} + {RATE_DELTA} but mailbox cost rose "
                f"({rotate_slots} > {observe_slots}); the derived metric disagrees with the rate, " + h2_note,
            )
        return (
            "favor_rotate",
            f"rotate {rotate_rate} > observe {observe_rate} + {RATE_DELTA} with no new failure class "
            f"(mailboxes per registration {observe_slots} -> {rotate_slots}); " + h2_note,
        )
    if observe_rate > rotate_rate + RATE_DELTA:
        return "keep_default_off", f"rotate {rotate_rate} < observe {observe_rate} - {RATE_DELTA}; " + h2_note
    return "inconclusive", f"success rates within {RATE_DELTA}: observe={observe_rate} rotate={rotate_rate}; " + h2_note


def _rule(experiment: str, metrics: Mapping[str, Mapping[str, Any]], powered: bool) -> tuple[str, str]:
    if not powered:
        return "underpowered", f"each arm needs >= {MIN_ARM_ATTEMPTED} attempted registrations"

    if experiment == "p0-1-preflight-endpoint":
        browser = metrics.get("browser") or {}
        legacy = metrics.get("legacy") or {}
        b_rate, l_rate = browser.get("cloudflare_rate"), legacy.get("cloudflare_rate")
        if b_rate is None or l_rate is None:
            return "inconclusive", "cloudflare_rate unavailable (no preflight lines in the log)"
        if b_rate < l_rate - RATE_DELTA and not browser.get("no_healthy_route"):
            return "favor_browser", f"browser challenge rate {b_rate} < legacy {l_rate} - {RATE_DELTA}"
        if l_rate < b_rate - RATE_DELTA and not legacy.get("no_healthy_route"):
            return "favor_legacy", f"legacy challenge rate {l_rate} < browser {b_rate} - {RATE_DELTA}"
        return "inconclusive", f"challenge rates within {RATE_DELTA}: browser={b_rate} legacy={l_rate}"

    if experiment == "p1-3-sentinel-password-bundle":
        default = metrics.get("default") or {}
        bundle = metrics.get("bundle") or {}
        d_rate, b_rate = default.get("registered_per_attempted"), bundle.get("registered_per_attempted")
        if d_rate is None or b_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        sentinel_growth = _sentinel_failure_growth(default.get("failure_classes"), bundle.get("failure_classes"))
        if sentinel_growth:
            return "keep_default_off", f"sentinel-specific failures grew: {sentinel_growth}"
        if b_rate > d_rate + RATE_DELTA:
            return "favor_bundle", f"bundle {b_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > b_rate + RATE_DELTA:
            return "keep_default_off", f"bundle {b_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} bundle={b_rate}"

    if experiment == "p1-4-prime-password-page":
        default = metrics.get("default") or {}
        prime = metrics.get("prime") or {}
        d_rate, p_rate = default.get("registered_per_attempted"), prime.get("registered_per_attempted")
        if d_rate is None or p_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        mailbox_growth = _failure_class_growth(default.get("failure_classes"), prime.get("failure_classes"), "mailbox")
        if p_rate > d_rate + RATE_DELTA:
            if mailbox_growth:
                return (
                    "favor_prime_with_red_flag",
                    f"prime {p_rate} > default {d_rate} + {RATE_DELTA}, but mailbox-class failures grew: {mailbox_growth}",
                )
            return "favor_prime", f"prime {p_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > p_rate + RATE_DELTA:
            return "keep_default_off", f"prime {p_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} prime={p_rate}"

    if experiment in {"p1-5-signup-continue-screen-hint", "p1-8-email-verification-continue-hint"}:
        default = metrics.get("default") or {}
        hint = metrics.get("hint") or {}
        d_rate, h_rate = default.get("registered_per_attempted"), hint.get("registered_per_attempted")
        if d_rate is None or h_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        auth_growth = _failure_class_growth(default.get("failure_classes"), hint.get("failure_classes"), "auth_state")
        if auth_growth:
            return (
                "keep_default_off",
                f"declared screen conflicts with the server's transaction reading; auth_state failures grew: {auth_growth}",
            )
        if h_rate > d_rate + RATE_DELTA:
            return "favor_hint", f"hint {h_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > h_rate + RATE_DELTA:
            return "keep_default_off", f"hint {h_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} hint={h_rate}"

    if experiment == "p1-9-prime-navigation-headers":
        default = metrics.get("default") or {}
        headers = metrics.get("headers") or {}
        d_rate, h_rate = default.get("registered_per_attempted"), headers.get("registered_per_attempted")
        if d_rate is None or h_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        auth_growth = _failure_class_growth(
            default.get("failure_classes"), headers.get("failure_classes"), "auth_state"
        )
        if auth_growth:
            return (
                "keep_default_off",
                f"the navigation headers disturbed the transaction; auth_state failures grew: {auth_growth}",
            )
        if h_rate > d_rate + RATE_DELTA:
            return "favor_headers", f"headers {h_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > h_rate + RATE_DELTA:
            return "keep_default_off", f"headers {h_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} headers={h_rate}"

    if experiment == "p1-10-signin-screen-hint":
        default = metrics.get("default") or {}
        candidate = metrics.get("login_or_signup") or {}
        d_rate, c_rate = default.get("registered_per_attempted"), candidate.get("registered_per_attempted")
        if d_rate is None or c_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        auth_growth = _failure_class_growth(
            default.get("failure_classes"), candidate.get("failure_classes"), "auth_state"
        )
        if auth_growth:
            return (
                "keep_default_off",
                f"the declared signin screen conflicts with the server's transaction reading; auth_state failures grew: {auth_growth}",
            )
        if c_rate > d_rate + RATE_DELTA:
            return "favor_login_or_signup", f"login_or_signup {c_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > c_rate + RATE_DELTA:
            return "keep_default_off", f"login_or_signup {c_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} login_or_signup={c_rate}"

    if experiment == "p1-11-signin-prompt-login":
        default = metrics.get("default") or {}
        candidate = metrics.get("prompt_login") or {}
        d_rate, c_rate = default.get("registered_per_attempted"), candidate.get("registered_per_attempted")
        if d_rate is None or c_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        auth_growth = _failure_class_growth(
            default.get("failure_classes"), candidate.get("failure_classes"), "auth_state"
        )
        if auth_growth:
            return (
                "keep_default_off",
                f"the declared prompt conflicts with the server's transaction reading; auth_state failures grew: {auth_growth}",
            )
        if c_rate > d_rate + RATE_DELTA:
            return "favor_prompt_login", f"prompt_login {c_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > c_rate + RATE_DELTA:
            return "keep_default_off", f"prompt_login {c_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} prompt_login={c_rate}"

    if experiment == "p1-12-signin-locale":
        default = metrics.get("default") or {}
        candidate = metrics.get("locale") or {}
        d_rate, c_rate = default.get("registered_per_attempted"), candidate.get("registered_per_attempted")
        if d_rate is None or c_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        auth_growth = _failure_class_growth(
            default.get("failure_classes"), candidate.get("failure_classes"), "auth_state"
        )
        if auth_growth:
            return (
                "keep_default_off",
                f"the declared locale conflicts with the server's transaction reading; auth_state failures grew: {auth_growth}",
            )
        if c_rate > d_rate + RATE_DELTA:
            return "favor_locale", f"locale {c_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > c_rate + RATE_DELTA:
            return "keep_default_off", f"locale {c_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} locale={c_rate}"

    if experiment == "p1-6-prime-about-you-page":
        default = metrics.get("default") or {}
        prime = metrics.get("prime") or {}
        d_rate, p_rate = default.get("registered_per_attempted"), prime.get("registered_per_attempted")
        if d_rate is None or p_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        account_growth = _failure_class_growth(default.get("failure_classes"), prime.get("failure_classes"), "account")
        if account_growth:
            return (
                "keep_default_off",
                f"the navigation disturbed the transaction; account-class failures grew: {account_growth}",
            )
        if p_rate > d_rate + RATE_DELTA:
            return "favor_prime", f"prime {p_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > p_rate + RATE_DELTA:
            return "keep_default_off", f"prime {p_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} prime={p_rate}"

    if experiment == "p1-7-create-disallowed-backoff":
        default = metrics.get("default") or {}
        backoff = metrics.get("backoff") or {}
        d_rate, b_rate = default.get("registered_per_attempted"), backoff.get("registered_per_attempted")
        if d_rate is None or b_rate is None:
            return "inconclusive", "registered_per_attempted unavailable (no batch funnel provided)"
        if b_rate > d_rate + RATE_DELTA:
            return "favor_backoff", f"backoff {b_rate} > default {d_rate} + {RATE_DELTA}"
        if d_rate > b_rate + RATE_DELTA:
            return "keep_default_off", f"backoff {b_rate} < default {d_rate} - {RATE_DELTA}"
        return "inconclusive", f"success rates within {RATE_DELTA}: default={d_rate} backoff={b_rate}"

    if experiment == "p0-2b-inflow-challenge-handoff":
        return _rule_inflow_challenge_handoff(metrics)

    if experiment == "p0-2-cloudflare-observation":
        observe = metrics.get("observe") or {}
        return (
            "observation_only",
            "no toggle: report per-exit challenge counts; in-flow rotation needs its own A/B before landing "
            f"(challenges={observe.get('probes_cloudflare')}, rate={observe.get('cloudflare_rate')})",
        )

    return "inconclusive", "no decision rule registered for this experiment"


def _class_count(mapping: Mapping[str, Any] | None, key: str) -> int:
    """Coerce one funnel failure-class count; absent or non-numeric JSON is 0.

    Funnel maps come from ``registration_funnel`` (always ints), but an arm
    record is a JSON file an operator can hand-edit; one bad key must not kill
    the whole comparison with a ValueError deep inside ``compare_records``.
    """
    value = mapping.get(key) if isinstance(mapping, Mapping) else None
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _failure_class_growth(
    default: Mapping[str, Any] | None, candidate: Mapping[str, Any] | None, class_name: str
) -> dict[str, tuple[int, int]]:
    """Counts of one failure *class* whose count grew in the candidate arm. Pure.

    Funnel failure maps are keyed by class code (``mailbox``, ``auth_state``,
    ...), so "did the OTP-delivery family grow?" is a lookup of the one key --
    unlike ``_sentinel_failure_growth``, which scans every key whose name
    contains ``sentinel`` inside the class map.
    """
    before = _class_count(default, class_name)
    after = _class_count(candidate, class_name)
    return {class_name: (before, after)} if after > before else {}


def _sentinel_failure_growth(
    default: Mapping[str, Any] | None, bundle: Mapping[str, Any] | None
) -> dict[str, tuple[int, int]]:
    """Failure classes whose count grew in the bundle arm. Pure."""
    left = default if isinstance(default, Mapping) else {}
    right = bundle if isinstance(bundle, Mapping) else {}
    grown: dict[str, tuple[int, int]] = {}
    for key in set(left) | set(right):
        if "sentinel" not in str(key).lower():
            continue
        before = int(left.get(key) or 0)
        after = int(right.get(key) or 0)
        if after > before:
            grown[str(key)] = (before, after)
    return grown


def _cmd_plan(_args: argparse.Namespace) -> int:
    print(json.dumps(EXPERIMENTS, ensure_ascii=False, indent=2))
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    log_path = Path(args.log)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    funnel = load_funnel(Path(args.funnel)) if args.funnel else None
    toggles = read_toggles(Path(args.config)) if args.config else {}
    record = build_record(
        experiment=args.experiment,
        arm=args.arm,
        log_text=log_text,
        funnel=funnel,
        toggles=toggles,
        log_path=log_path,
    )
    if args.note:
        record["note"] = args.note
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.experiment}__{args.arm}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    if not record["toggle_verified"]:
        print(
            "WARNING: toggle not verified "
            f"(expected {record['toggle_expected']!r}, saw {record['toggle_actual']!r}); "
            "pass --config pointing at the config used for this run",
            file=sys.stderr,
        )
    if _mechanism_failed(record):
        print(
            "WARNING: the toggle's mechanism never ran in this log "
            f"(expected marker {record['mechanism_marker']!r}, seen={record['mechanism_seen']}); "
            "the arm is a fake comparison -- pass --log pointing at this arm's real run log",
            file=sys.stderr,
        )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    records = []
    for spec in args.arm:
        if "=" not in spec:
            raise SystemExit(f"--arm must be NAME=FILE, got {spec!r}")
        _name, path = spec.split("=", 1)
        record_path = Path(path)
        try:
            records.append(json.loads(record_path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise SystemExit(f"arm record not found: {record_path}") from exc
        except (ValueError, OSError) as exc:
            raise SystemExit(f"arm record unreadable as JSON ({record_path}): {exc}") from exc
    report = compare_records(records)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(
        "\nNote: this is a decision aid for a controlled live run, not a "
        "success-rate claim. Offline tests cannot establish a live registration "
        "rate (see docs/current/protocol-registration.md, Validation limits).",
        file=sys.stderr,
    )
    return 0 if report["verdict"] not in {"unverified", "manipulation_failed", "mixed_experiments", "no_data"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the pre-registered A/B design")
    plan.set_defaults(func=_cmd_plan)

    collect = sub.add_parser("collect", help="normalise one operator-run arm into a record")
    collect.add_argument("--experiment", required=True, choices=sorted(EXPERIMENTS))
    collect.add_argument("--arm", required=True)
    collect.add_argument("--log", required=True, help="the registration run log to parse")
    collect.add_argument("--funnel", help="batch report JSON containing the funnel")
    collect.add_argument("--config", help="config snapshot used for the run (manipulation check)")
    collect.add_argument("--note", help="free-form operator note stored with the record")
    collect.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    collect.set_defaults(func=_cmd_collect)

    compare = sub.add_parser("compare", help="join arm records and apply the decision rule")
    compare.add_argument(
        "--arm",
        action="append",
        required=True,
        metavar="NAME=FILE",
        help="an arm record written by collect; repeat per arm",
    )
    compare.set_defaults(func=_cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
